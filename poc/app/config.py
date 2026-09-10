"""
טעינת קונפיגורציה: משתני סביבה (.env) + קבצי YAML (rules.yaml, contacts.yaml).
כל הגישה למפתחות API ולהגדרות עוברת דרך המודול הזה.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # core service
    core_service_host: str = "0.0.0.0"
    core_service_port: int = 8000
    core_service_url: str = "http://localhost:8000"
    connector_shared_secret: str = "change-me-to-a-long-random-string"

    database_path: str = "./data/groupguard.db"
    media_storage_path: str = "./data/media"

    # anthropic
    anthropic_api_key: str = ""
    anthropic_model_classify: str = "claude-haiku-4-5"
    anthropic_model_summarize: str = "claude-sonnet-4-5"

    # reader (baileys connector)
    connector_auth_dir: str = "./connector-whatsapp/auth"
    # אין למלא ידנית! מזהה הקבוצה המנוטרת נקבע ונשמר אוטומטית על כל חשבון
    # (accounts.monitored_group_id) בפעם הראשונה שמגיע אירוע מהקבוצה שאליה
    # צירפו את מספר ה-Reader, לאחר שהאונבורדינג הושלם (ראו app/main.py:
    # whatsapp_reader_webhook, app/db.py: set_monitored_group_id). השדה כאן
    # נשאר כעקיפה ידנית לצורכי דיבוג/פיתוח בלבד.
    monitored_group_id: str = ""
    # מספר הטלפון הנייד (בפורמט E.164, לדוגמה +972501234567) שישמש את הסוכן —
    # זהו המספר הקורא (Reader) שיש לצרף לקבוצות הוואטסאפ המנוטרות. מוגדר כאן
    # ולא בקוד, מטעמי אבטחה/הפרדת סודות מהריפו. שני שימושים:
    #   1. connector-whatsapp (Baileys) משתמש בו כדי להתחבר באמצעות קוד צימוד
    #      (Pairing Code) במקום סריקת QR, בהרצה הראשונה בלבד (ראו index.js).
    #      אם משאירים ריק — ה-connector חוזר לזרימת QR הרגילה.
    #   2. אשף ההרשמה (F-2.2) מציג אותו למשתמש הקצה כמספר שיש להוסיף לקבוצה.
    # ה-POC אינו מקצה מספרים אוטומטית ממאגר (זה שלב MVP/SaaS, סעיף 5.2 F-2.1).
    reader_whatsapp_number: str = ""
    send_disclosure_message: bool = True
    disclosure_message_text: str = "קבוצה זו מנוטרת ע\"י GroupGuard לצורך אכיפת חוקי הקבוצה."

    # sender (meta cloud api)
    wa_cloud_api_token: str = ""
    wa_cloud_api_phone_number_id: str = ""
    wa_cloud_api_business_account_id: str = ""
    wa_cloud_api_version: str = "v20.0"
    wa_alert_template_name: str = "groupguard_alert"
    wa_alert_template_lang: str = "he"
    alerts_send_via_whatsapp: bool = False

    # שליחת מייל — SMTP של Gmail עם App Password (16 תווים), במקום Gmail API/OAuth.
    # יצירת App Password: מצריכה אימות דו-שלבי (2-Step Verification) פעיל בחשבון
    # ה-Gmail, ואז myaccount.google.com/apppasswords -> יצירת סיסמה ל"אפליקציה אחרת".
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587  # 587 = STARTTLS (מומלץ). 465 = SSL ישיר.
    smtp_user: str = ""       # כתובת ה-Gmail המלאה, גם משמשת ככתובת השולח
    smtp_password: str = ""   # ה-App Password בן 16 התווים (בלי רווחים)
    # אין למלא ידנית! נמען הדו"ח היומי הוא כתובת המייל של המשתמש שנרשם באשף
    # ההרשמה (ראו app/reports.py: build_and_send_daily_report). השדה כאן נשאר
    # כנפילה-לאחור בלבד למקרה שהדו"ח מורץ לפני שקיים חשבון כלשהו.
    daily_report_recipients: str = ""
    daily_report_hour: int = 0
    daily_report_minute: int = 5
    timezone: str = "Asia/Jerusalem"

    log_level: str = "INFO"
    group_display_name: str = "קבוצת הפיילוט"

    # Multi-tenant: התחברות משתמשי קצה (session cookie חתום) ופאנל אדמין (CRM).
    # SECRET_KEY חייב להיות מחרוזת אקראית וארוכה קבועה (אם משתנה — כל המשתמשים
    # המחוברים מנותקים). ADMIN_PASSWORD היא סיסמת אדמין יחידה, מספיקה ל-POC.
    secret_key: str = ""
    admin_password: str = ""

    @property
    def daily_report_recipient_list(self) -> list[str]:
        return [r.strip() for r in self.daily_report_recipients.split(",") if r.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_rules_config() -> dict:
    path = BASE_DIR / "config" / "rules.yaml"
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache
def get_contacts_config() -> dict:
    path = BASE_DIR / "config" / "contacts.yaml"
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def reload_configs() -> None:
    """מנקה cache — שימושי אחרי עריכת rules.yaml/contacts.yaml בזמן ריצה."""
    get_rules_config.cache_clear()
    get_contacts_config.cache_clear()


def ensure_dirs() -> None:
    s = get_settings()
    Path(s.database_path).parent.mkdir(parents=True, exist_ok=True)
    Path(s.media_storage_path).mkdir(parents=True, exist_ok=True)
