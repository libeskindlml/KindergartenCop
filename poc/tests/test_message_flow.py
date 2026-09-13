"""
בדיקות אינטגרציה של app.main._handle_incoming_message: מוודאות שהודעת טקסט
רגילה ממשיכה לעבוד בדיוק כמו קודם, ושהודעה קולית מתומללת (עם FFmpeg/OpenAI
ממוקים ברמת app.main.transcribe_voice_message — נבדקים בנפרד ב-
test_transcription.py) ממשיכה בדיוק לאותו צינור עיבוד/DB כמו טקסט רגיל.
"""
from __future__ import annotations

import base64

import pytest

from app import db, main
from app.transcription import VoiceTranscriptionError


def _voice_event(provider_message_id: str = "wamid.voice-1") -> dict:
    return {
        "event_type": "message",
        "provider_message_id": provider_message_id,
        "group_id": "123456789@g.us",
        "sender_phone": "+972500000001",
        "sender_name": "הורה א",
        "msg_type": "audio",
        "text": None,
        "media_base64": base64.b64encode(b"\x00" * 300).decode("ascii"),
        "media_mimetype": "audio/ogg; codecs=opus",
        "is_forwarded": False,
        "reply_to_provider_id": None,
        "sent_at": "2026-01-01T10:00:00",
    }


def _text_event(provider_message_id: str = "wamid.text-1", text: str = "שלום לכולם") -> dict:
    return {
        "event_type": "message",
        "provider_message_id": provider_message_id,
        "group_id": "123456789@g.us",
        "sender_phone": "+972500000002",
        "sender_name": "הורה ב",
        "msg_type": "text",
        "text": text,
        "is_forwarded": False,
        "reply_to_provider_id": None,
        "sent_at": "2026-01-01T10:00:00",
    }


async def test_regular_text_message_flow_is_unchanged(account, monkeypatch):
    calls = {"n": 0}

    async def _fail_if_called(**kwargs):
        calls["n"] += 1
        raise AssertionError("transcribe_voice_message לא אמור להיקרא עבור הודעת טקסט")

    monkeypatch.setattr(main, "transcribe_voice_message", _fail_if_called)

    resp = await main._handle_incoming_message(_text_event(), account["id"])

    assert resp.status_code == 200
    stored = db.get_message_by_provider_id("wamid.text-1")
    assert stored is not None
    assert stored["text"] == "שלום לכולם"
    assert stored["msg_type"] == "text"
    assert stored["source_type"] is None
    assert stored["processing_status"] == "rules_checked"
    assert calls["n"] == 0


async def test_voice_message_is_transcribed_and_enters_text_pipeline(account, monkeypatch):
    async def _fake_transcribe(*, raw_bytes: bytes, mimetype):
        assert raw_bytes == b"\x00" * 300
        assert mimetype == "audio/ogg; codecs=opus"
        return "האסיפה מחר בשמונה בערב"

    monkeypatch.setattr(main, "transcribe_voice_message", _fake_transcribe)

    resp = await main._handle_incoming_message(_voice_event(), account["id"])
    assert resp.status_code == 200

    stored = db.get_message_by_provider_id("wamid.voice-1")
    assert stored is not None
    # אותו צינור עיבוד כמו טקסט רגיל: evaluate_message רץ על message['text'] בלי
    # קשר למקור, ולכן processing_status מגיע ל-'rules_checked' בדיוק כמו טקסט.
    assert stored["processing_status"] == "rules_checked"
    assert stored["text"] == "האסיפה מחר בשמונה בערב"


async def test_only_transcription_is_persisted_no_audio_or_paths(account, monkeypatch):
    async def _fake_transcribe(*, raw_bytes: bytes, mimetype):
        return "טקסט לדוגמה"

    monkeypatch.setattr(main, "transcribe_voice_message", _fake_transcribe)

    await main._handle_incoming_message(_voice_event("wamid.voice-2"), account["id"])

    stored = db.get_message_by_provider_id("wamid.voice-2")
    assert stored["text"] == "טקסט לדוגמה"
    assert stored["msg_type"] == "audio"
    assert stored["source_type"] == "voice"
    # שום בייטים/Base64/נתיב קובץ לא נשמרים על ההודעה הקולית:
    assert stored["media_path"] is None
    assert stored["media_sha256"] is None
    assert stored["ai_analysis"] is None


async def test_transcription_failure_does_not_forward_empty_text(account, monkeypatch):
    async def _fake_transcribe_fails(*, raw_bytes: bytes, mimetype):
        raise VoiceTranscriptionError("transcription", "OpenAI החזיר תמלול ריק")

    monkeypatch.setattr(main, "transcribe_voice_message", _fake_transcribe_fails)

    resp = await main._handle_incoming_message(_voice_event("wamid.voice-3"), account["id"])
    assert resp.status_code == 200

    stored = db.get_message_by_provider_id("wamid.voice-3")
    assert stored["text"] is None
    assert stored["processing_status"] == "transcription_failed"
    # מנוע החוקים לא רץ בכלל על ההודעה הזו (לא הופקו הפרות מטקסט ריק/כושל)
    assert db.list_violations(account_id=account["id"]) == []


async def test_repeated_provider_message_id_is_not_processed_twice(account, monkeypatch):
    calls = {"n": 0}

    async def _fake_transcribe(*, raw_bytes: bytes, mimetype):
        calls["n"] += 1
        return "תוכן ההודעה הקולית"

    monkeypatch.setattr(main, "transcribe_voice_message", _fake_transcribe)

    event = _voice_event("wamid.voice-retry")
    first = await main._handle_incoming_message(event, account["id"])
    second = await main._handle_incoming_message(event, account["id"])

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls["n"] == 1, "תמלול היה אמור לרוץ פעם אחת בלבד גם כשה-provider שלח ריטריי לאותה הודעה"

    all_messages = [m for m in db.list_messages(limit=100) if m["provider_message_id"] == "wamid.voice-retry"]
    assert len(all_messages) == 1
