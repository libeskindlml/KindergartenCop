"""
בדיקת אינטגרציה אמיתית — לא ממוקה: FFmpeg בפועל (מותקן במערכת) + קריאה אמיתית
ל-OpenAI Audio Transcriptions API, מול קובץ OGG/Opus אמיתי
(tests/fixtures/sample_voice_he.ogg — הודעה קולית עברית שסונתזה עם macOS `say`,
בדיוק באותו פורמט שוואטסאפ שולח הודעות קוליות: audio/ogg; codecs=opus).

בכוונה *לא* רצה כברירת מחדל (`pytest` רגיל מריץ רק בדיקות ממוקות, ר' pytest.ini
addopts) — יש להריץ אותה מפורשות עם `pytest -m integration`. הסיבה: זו הבדיקה
היחידה בפרויקט שמבצעת קריאת רשת אמיתית (עלות כספית קטנה) ותלויה ב-FFmpeg
מותקן בפועל ובמפתח OPENAI_API_KEY אמיתי ב-.env. שאר הבדיקות (test_transcription.py,
test_message_flow.py) ממוקות במלואן ורצות תמיד, כנדרש במפרט המקורי.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings
from app.transcription import transcribe_voice_message

pytestmark = pytest.mark.integration

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_voice_he.ogg"
# הטקסט שהוקרא בפועל ליצירת הקובץ (ר' תיעוד למעלה) — לא בדיקת התאמה מדויקת
# (ASR יכול לשנות ניקוד/כתיב/סימני פיסוק), רק שמשמעות המילים המרכזיות נשמרה.
_EXPECTED_KEYWORDS = ("אסיפה", "הורים", "שלישי", "שמונה", "גן")


async def test_real_ffmpeg_and_openai_transcribe_hebrew_voice_note():
    assert FIXTURE_PATH.exists(), f"קובץ הבדיקה חסר: {FIXTURE_PATH}"

    # מנקים cache כדי לוודא שנקרא ה-OPENAI_API_KEY האמיתי מ-.env (לא ערך מדומה
    # שאולי נשאר מ-fixture אחר שהריץ monkeypatch באותו תהליך pytest).
    get_settings.cache_clear()
    settings = get_settings()
    if not settings.openai_api_key:
        pytest.skip("OPENAI_API_KEY לא מוגדר ב-.env — אין מפתח אמיתי לבדיקת אינטגרציה זו")

    raw_bytes = FIXTURE_PATH.read_bytes()
    assert len(raw_bytes) > 0

    try:
        text = await transcribe_voice_message(raw_bytes=raw_bytes, mimetype="audio/ogg; codecs=opus")
    finally:
        get_settings.cache_clear()

    print(f"\n[real transcription result] {text!r}")

    assert isinstance(text, str)
    assert len(text.strip()) > 0
    assert any(word in text for word in _EXPECTED_KEYWORDS), (
        f"התמלול לא הכיל אף אחת מהמילים הצפויות {_EXPECTED_KEYWORDS}: {text!r}"
    )
