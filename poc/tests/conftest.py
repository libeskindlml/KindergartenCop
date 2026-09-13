"""
Fixtures משותפים לבדיקות. כל בדיקה מקבלת DB זמני ומבודד משלה (קובץ SQLite
תחת tmp_path) ומפתחות API מדומים — כדי שלעולם לא תתבצע קריאת רשת אמיתית
(Anthropic/OpenAI) בזמן ריצת הבדיקות. קריאות ל-FFmpeg/OpenAI עצמן ממוקות
בכל טסט בנפרד (ר' test_transcription.py, test_message_flow.py).
"""
from __future__ import annotations

import pytest

from app import db
from app.config import get_settings


@pytest.fixture()
def account(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test_groupguard.db"))
    monkeypatch.setenv("MEDIA_STORAGE_PATH", str(tmp_path / "media"))
    # ריק בכוונה: מוודא שאין קריאת רשת אמיתית ל-Anthropic מתוך evaluate_message
    # (classify_message נכשל בעדינות ומדלג כשאין מפתח — ר' app/claude_client.py).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    # מפתח מדומה (לא אמיתי) — מספיק כדי ש-transcription.py לא ידלג מוקדם על
    # "missing_api_key"; קריאת ה-API בפועל תמיד ממוקה בבדיקות עצמן.
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy-key-not-real")
    get_settings.cache_clear()

    db.init_db()
    account_id = db.create_account(
        email="parent@example.com",
        password_hash="x",
        owner_name="הורה בדיקה",
        group_display_name="קבוצת בדיקה",
        timezone="Asia/Jerusalem",
        strictness_level="beinoni",
        onboarding_completed=1,
        verification_code="TEST01",
    )
    db.set_monitored_group_id(account_id, "123456789@g.us")

    yield db.get_account(account_id)

    get_settings.cache_clear()
