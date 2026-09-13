"""
בדיקות יחידה ל-app/transcription.py. FFmpeg (asyncio.create_subprocess_exec)
ו-OpenAI (AsyncOpenAI) תמיד ממוקים — הבדיקות האלה לא תלויות בהתקנת FFmpeg
בפועל ולא מבצעות שום קריאת רשת אמיתית.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app import transcription
from app.config import get_settings
from app.transcription import VoiceTranscriptionError, transcribe_voice_message


class _FakeProc:
    def __init__(self, returncode: int = 0, hang: bool = False):
        self.returncode = returncode
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            import asyncio

            await asyncio.sleep(5)
        return b"", b""

    def kill(self):
        self.killed = True

    async def wait(self):
        return None


def _make_fake_create_subprocess_exec(*, returncode=0, hang=False, write_output=True):
    async def _fake(*args, **kwargs):
        output_path = Path(args[-1])
        if write_output and returncode == 0 and not hang:
            output_path.write_bytes(b"fake-converted-mp3-bytes")
        return _FakeProc(returncode=returncode, hang=hang)

    return _fake


class _FakeTranscriptionResult:
    def __init__(self, text: str):
        self.text = text


class _FakeAsyncOpenAI:
    """מחליף את openai.AsyncOpenAI — מחזיר טקסט קבוע או זורק שגיאה, בלי רשת."""

    def __init__(self, *, text: str = "", raise_error: Exception | None = None, **_kwargs):
        self._text = text
        self._raise_error = raise_error
        self.audio = self

    @property
    def transcriptions(self):
        return self

    async def create(self, **_kwargs):
        if self._raise_error:
            raise self._raise_error
        return _FakeTranscriptionResult(self._text)

    async def close(self):
        return None


def _spy_temp_dir(monkeypatch) -> dict:
    """עוקב אחרי תיקיית ה-temp שנוצרת בפועל, כדי לוודא שהיא נמחקת אחרי הקריאה."""
    captured: dict = {}
    real_mkdtemp = tempfile.mkdtemp

    def spy(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        captured["dir"] = path
        return path

    monkeypatch.setattr(transcription.tempfile, "mkdtemp", spy)
    return captured


@pytest.mark.usefixtures("account")
async def test_transcribe_voice_message_success_cleans_up_temp_files(monkeypatch):
    captured = _spy_temp_dir(monkeypatch)
    monkeypatch.setattr(
        transcription.asyncio, "create_subprocess_exec", _make_fake_create_subprocess_exec()
    )
    monkeypatch.setattr(
        transcription, "AsyncOpenAI", lambda **kw: _FakeAsyncOpenAI(text="שלום, מתי האסיפה?")
    )

    text = await transcribe_voice_message(raw_bytes=b"\x00" * 500, mimetype="audio/ogg; codecs=opus")

    assert text == "שלום, מתי האסיפה?"
    assert "dir" in captured
    assert not Path(captured["dir"]).exists(), "תיקיית ה-temp הייתה אמורה להימחק אחרי הצלחה"


@pytest.mark.usefixtures("account")
async def test_transcribe_voice_message_cleans_up_on_conversion_failure(monkeypatch):
    captured = _spy_temp_dir(monkeypatch)
    monkeypatch.setattr(
        transcription.asyncio,
        "create_subprocess_exec",
        _make_fake_create_subprocess_exec(returncode=1, write_output=False),
    )

    with pytest.raises(VoiceTranscriptionError) as exc_info:
        await transcribe_voice_message(raw_bytes=b"\x00" * 500, mimetype="audio/ogg")

    assert exc_info.value.stage == "conversion"
    assert not Path(captured["dir"]).exists(), "תיקיית ה-temp הייתה אמורה להימחק גם אחרי כשל המרה"


@pytest.mark.usefixtures("account")
async def test_transcribe_voice_message_cleans_up_on_transcription_failure(monkeypatch):
    captured = _spy_temp_dir(monkeypatch)
    monkeypatch.setattr(
        transcription.asyncio, "create_subprocess_exec", _make_fake_create_subprocess_exec()
    )
    monkeypatch.setattr(
        transcription,
        "AsyncOpenAI",
        lambda **kw: _FakeAsyncOpenAI(raise_error=RuntimeError("API הפילה 500")),
    )

    with pytest.raises(VoiceTranscriptionError) as exc_info:
        await transcribe_voice_message(raw_bytes=b"\x00" * 500, mimetype="audio/ogg")

    assert exc_info.value.stage == "transcription"
    assert not Path(captured["dir"]).exists(), "תיקיית ה-temp הייתה אמורה להימחק גם אחרי כשל תמלול"


@pytest.mark.usefixtures("account")
async def test_empty_openai_transcription_is_rejected_not_forwarded(monkeypatch):
    monkeypatch.setattr(
        transcription.asyncio, "create_subprocess_exec", _make_fake_create_subprocess_exec()
    )
    monkeypatch.setattr(transcription, "AsyncOpenAI", lambda **kw: _FakeAsyncOpenAI(text="   "))

    with pytest.raises(VoiceTranscriptionError) as exc_info:
        await transcribe_voice_message(raw_bytes=b"\x00" * 500, mimetype="audio/ogg")

    assert exc_info.value.stage == "transcription"


@pytest.mark.usefixtures("account")
async def test_rejects_empty_audio_before_any_ffmpeg_or_openai_call(monkeypatch):
    calls = {"ffmpeg": 0}

    async def _counting_fake(*args, **kwargs):
        calls["ffmpeg"] += 1
        return _FakeProc(returncode=0)

    monkeypatch.setattr(transcription.asyncio, "create_subprocess_exec", _counting_fake)

    with pytest.raises(VoiceTranscriptionError) as exc_info:
        await transcribe_voice_message(raw_bytes=b"", mimetype="audio/ogg")

    assert exc_info.value.stage == "validation"
    assert calls["ffmpeg"] == 0


@pytest.mark.usefixtures("account")
async def test_rejects_oversized_audio(monkeypatch):
    monkeypatch.setenv("MAX_VOICE_MESSAGE_BYTES", "1000")
    get_settings.cache_clear()
    try:
        with pytest.raises(VoiceTranscriptionError) as exc_info:
            await transcribe_voice_message(raw_bytes=b"\x00" * 2000, mimetype="audio/ogg")
        assert exc_info.value.stage == "validation"
    finally:
        get_settings.cache_clear()


@pytest.mark.usefixtures("account")
async def test_missing_openai_api_key_fails_gracefully(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    monkeypatch.setattr(
        transcription.asyncio, "create_subprocess_exec", _make_fake_create_subprocess_exec()
    )
    try:
        with pytest.raises(VoiceTranscriptionError) as exc_info:
            await transcribe_voice_message(raw_bytes=b"\x00" * 500, mimetype="audio/ogg")
        assert exc_info.value.stage == "transcription"
    finally:
        get_settings.cache_clear()
