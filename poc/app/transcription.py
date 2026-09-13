"""
תמלול הודעות קוליות מוואטסאפ (F-3.1 הרחבה): המרת האודיו שהתקבל (בד"כ OGG/Opus)
ל-MP3 באמצעות FFmpeg, ואז תמלול באמצעות OpenAI Audio Transcriptions API
(gpt-4o-mini-transcribe, שפה מצופה: עברית). התמלול בלבד מוחזר לקורא — אין כאן
תרגום, סיכום, מיון דוברים או כל עיבוד שמשנה את משמעות ההודעה המקורית.

מדיניות אחסון: קובץ האודיו המקורי והמומר נכתבים לתיקיית temp ייחודית ומאובטחת
(0700) ונמחקים תמיד ב-finally — גם בכשל המרה/תמלול. שום בייטים/Base64/נתיב-קובץ
לא נשמרים ב-DB או ביומן; רק טקסט התמלול חוזר לקורא (main.py הוא זה ששומר אותו
בטבלת messages, כמו כל טקסט אחר).
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from openai import AsyncOpenAI

from app.config import get_settings

logger = logging.getLogger("groupguard.transcription")

# מיפוי mimetype נפוץ להודעות קוליות בוואטסאפ (Baileys) -> סיומת קלט ל-FFmpeg.
# ברירת המחדל (ogg) נכונה לרוב המכריע של הודעות "לחצו והקליטו" (ptt).
_EXT_BY_MIMETYPE = {
    "audio/ogg": ".ogg",
    "audio/ogg; codecs=opus": ".ogg",
    "audio/opus": ".opus",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/amr": ".amr",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
}


class VoiceTranscriptionError(Exception):
    """שגיאה בתהליך תמלול הודעה קולית. ה-stage מאפשר לקורא (main.py) להבדיל בין
    כשל ולידציה/המרה/תמלול לצורכי לוגים ותגובה (ר' דרישת "לטפל בנפרד" באפיון)."""

    def __init__(self, stage: str, message: str):
        self.stage = stage
        super().__init__(message)


def _guess_input_suffix(mimetype: Optional[str]) -> str:
    if not mimetype:
        return ".ogg"
    key = mimetype.split(";")[0].strip().lower()
    for known, ext in _EXT_BY_MIMETYPE.items():
        if known.split(";")[0].strip().lower() == key:
            return ext
    return ".ogg"


def _validate_audio_bytes(raw_bytes: bytes) -> None:
    settings = get_settings()
    size = len(raw_bytes)
    if size < settings.min_voice_message_bytes:
        raise VoiceTranscriptionError("validation", f"קובץ אודיו ריק/קטן מדי ({size} בייטים)")
    if size > settings.max_voice_message_bytes:
        raise VoiceTranscriptionError(
            "validation", f"קובץ אודיו גדול מדי ({size} בייטים, סף: {settings.max_voice_message_bytes})"
        )


async def _run_ffmpeg(input_path: Path, output_path: Path, timeout_seconds: float) -> None:
    """ממיר את קובץ הקלט ל-MP3 באמצעות FFmpeg, אסינכרונית (לא חוסם את ה-event loop)."""
    settings = get_settings()
    proc = await asyncio.create_subprocess_exec(
        settings.ffmpeg_path,
        "-y",
        "-i", str(input_path),
        "-vn",
        "-acodec", "libmp3lame",
        "-ar", "16000",
        "-ac", "1",
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise VoiceTranscriptionError("conversion", "המרת FFmpeg חרגה מזמן המתנה מקסימלי") from exc
    except FileNotFoundError as exc:
        raise VoiceTranscriptionError("conversion", "FFmpeg לא מותקן/לא נמצא ב-PATH") from exc

    if proc.returncode != 0:
        # לא לוגים את תוכן stderr המלא (עלול לכלול נתיבי קבצים) — רק קוד יציאה.
        raise VoiceTranscriptionError("conversion", f"FFmpeg נכשל (exit code {proc.returncode})")

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise VoiceTranscriptionError("conversion", "פלט FFmpeg ריק")


async def _call_openai_transcribe(file_path: Path, timeout_seconds: float) -> str:
    """קורא ל-OpenAI Audio Transcriptions API. משתמש ב-SDK הרשמי, אסינכרונית."""
    settings = get_settings()
    if not settings.openai_api_key:
        raise VoiceTranscriptionError("transcription", "OPENAI_API_KEY חסר — לא ניתן לתמלל")

    client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=timeout_seconds)
    try:
        with open(file_path, "rb") as audio_file:
            result = await client.audio.transcriptions.create(
                model=settings.openai_transcription_model,
                file=audio_file,
                language=settings.openai_transcription_language,
            )
    except VoiceTranscriptionError:
        raise
    except Exception as exc:  # noqa: BLE001 — כל כשל API מתורגם לשגיאת תמלול מסווגת
        raise VoiceTranscriptionError("transcription", f"קריאה ל-OpenAI נכשלה: {exc}") from exc
    finally:
        await client.close()

    text = (getattr(result, "text", None) or "").strip()
    if not text:
        raise VoiceTranscriptionError("transcription", "OpenAI החזיר תמלול ריק")
    return text


async def transcribe_voice_message(*, raw_bytes: bytes, mimetype: Optional[str] = None) -> str:
    """
    זרימת התמלול המלאה עבור הודעה קולית נכנסת: ולידציה -> תיקיית temp מאובטחת
    וייחודית -> המרת FFmpeg ל-MP3 -> תמלול OpenAI (gpt-4o-mini-transcribe, עברית).
    מחזיר טקסט תמלול לא-ריק, או זורק VoiceTranscriptionError. הקבצים הזמניים
    (מקור וממיר) נמחקים תמיד — גם בנתיב הצלחה וגם בכל כשל (try/finally).
    לעולם לא נכתבים בייטי אודיו/Base64/נתיבי קבצים ל-DB או ליומן — רק בקריאה זו.
    """
    _validate_audio_bytes(raw_bytes)

    settings = get_settings()
    temp_dir = Path(tempfile.mkdtemp(prefix="groupguard_voice_"))
    try:
        unique = uuid.uuid4().hex
        input_path = temp_dir / f"{unique}_in{_guess_input_suffix(mimetype)}"
        output_path = temp_dir / f"{unique}_out.mp3"

        input_path.write_bytes(raw_bytes)

        await _run_ffmpeg(input_path, output_path, settings.ffmpeg_timeout_seconds)
        text = await _call_openai_transcribe(output_path, settings.openai_transcription_timeout_seconds)

        logger.info(
            "הודעה קולית תומללה בהצלחה (קלט %d בייטים -> פלט %d בייטים, %d תווי תמלול)",
            len(raw_bytes), output_path.stat().st_size, len(text),
        )
        return text
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
