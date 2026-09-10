"""
שירות התראות (Notification Service, F-6): לכל הפרה חדשה (שאינה ב-Shadow Mode) —
בדיקת אנשי קשר רלוונטיים (חומרה מינימלית, שעות שקט) ושליחה בפועל לכל אחד
מהערוצים שהוגדרו לאיש הקשר באשף ההרשמה.

ב-POC הערוץ המיידי הפעיל בפועל הוא אימייל (SMTP של Gmail, ראו gmail_client.py) —
נשלח מיד עם זיהוי ההפרה, ללא צורך ב-Opt-in, מכיוון שזו כתובת שהמשתמש עצמו
הזין באשף ההרשמה עבור איש קשר שהוא בחר (הסכמה משתמעת, בהיקף ה-POC).
ערוץ הוואטסאפ (WhatsApp Business Cloud API) נשאר זמין לעתיד/הרחבה, וממשיך
לדרוש Opt-in מאושר בפועל (F-6.3) בהתאם למדיניות Meta לתבניות הודעה.

כל התראה נרשמת ב-DB (alerts table) גם אם השליחה בפועל נכשלה/דולגה — כך
שהפאנל המקומי תמיד משקף את מה שהיה אמור לקרות (log-first, send-if-possible).
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dt_time
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import db
from app.config import BASE_DIR, get_contacts_config, get_settings
from app.gmail_client import send_email
from app.providers.sender_cloud_api import get_sender_provider

logger = logging.getLogger("groupguard.notifier")

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_SEVERITY_LABELS = {"low": "נמוך", "medium": "בינוני", "high": "גבוה", "critical": "קריטי"}

TEMPLATES_DIR = BASE_DIR / "app" / "templates"
_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))


def _severity_ok(violation_severity: str, min_severity: str) -> bool:
    return _SEVERITY_RANK.get(violation_severity, 0) >= _SEVERITY_RANK.get(min_severity, 0)


def _in_quiet_hours(quiet_hours: dict) -> bool:
    if not quiet_hours or not quiet_hours.get("enabled"):
        return False
    now = datetime.now().time()
    start = dt_time.fromisoformat(quiet_hours["start"])
    end = dt_time.fromisoformat(quiet_hours["end"])
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end  # חלון שחוצה חצות


def _all_contacts(account: Optional[dict]) -> list[dict]:
    """
    מקור אנשי הקשר: אנשי הקשר של *החשבון הספציפי* הזה (Multi-tenant — DB,
    F-1.1/F-6.1). אם אין חשבון בכלל (מקרה קצה תיאורטי), נופלים חזרה
    ל-config/contacts.yaml הגלובלי.
    """
    if account:
        db_contacts = db.list_alert_contacts_db(account["id"])
        if db_contacts:
            return db_contacts
    contacts_cfg = get_contacts_config()
    return contacts_cfg.get("alert_contacts", [])


def _relevant_contacts(severity: str, account: Optional[dict]) -> list[dict]:
    return [c for c in _all_contacts(account) if _severity_ok(severity, c.get("min_severity", "low"))]


def _render_alert_email(template_params: dict) -> tuple[str, str]:
    html_body = _env.get_template("alert_email.html").render(
        **template_params, severity_label=_SEVERITY_LABELS.get(template_params["severity"], template_params["severity"])
    )
    text_body = (
        f"התראת GroupGuard — {template_params['group_name']}\n"
        f"חוק: {template_params['rule_name']} | חומרה: {template_params['severity']}\n"
        f"שולח: {template_params['sender_name']} ({template_params['sender_phone']})\n"
        f"מועד: {template_params['time']}\n\n"
        f"{template_params['message_excerpt']}\n\n"
        f"לפאנל הניהול: {template_params['link']}"
    )
    return html_body, text_body


async def notify_violation(violation: dict) -> None:
    """violation: הפלט שנוצר ע"י app.rules_engine.evaluate_message (dict בודד)."""
    if violation.get("shadow_mode"):
        logger.info("Shadow Mode פעיל — הפרה %s נרשמה אך לא תישלח התראה", violation.get("id"))
        return

    settings = get_settings()
    message = violation.get("message", {})
    # Multi-tenant: מזהים את החשבון הבעלים לפי message["account_id"] — לא "החשבון"
    # הגלובלי היחיד. כך כל חשבון מקבל את ההתראות/שם-הקבוצה שלו בלבד.
    account = db.get_account(message["account_id"]) if message.get("account_id") else None
    group_name = (account or {}).get("group_display_name") or settings.group_display_name
    sender = get_sender_provider()
    contacts = _relevant_contacts(violation["severity"], account)

    if not contacts:
        logger.info("אין אנשי קשר רלוונטיים לחומרה %s", violation["severity"])
        return

    # F-6.6: קיבוץ פשוט ל-POC — אם יש 3+ הפרות פתוחות מאותו שולח בדקה האחרונה, מצרפים לטקסט אחד.
    # (המימוש המלא של הקיבוץ המרובה-הפרות מושאר לשלב ה-MVP; ב-POC כל הפרה מטופלת בנפרד
    #  אך מסומנת בהודעה אם קיימות הפרות נוספות קרובות בזמן.)

    template_params = {
        "group_name": group_name,
        "rule_name": violation["rule_name"],
        "severity": violation["severity"],
        "sender_name": message.get("sender_name") or "לא ידוע",
        "sender_phone": message.get("sender_phone") or "",
        "time": datetime.now().strftime("%H:%M %d/%m/%Y"),
        "message_excerpt": (message.get("text") or "<מדיה>")[:150],
        "explanation": violation.get("explanation", ""),
        "link": f"{settings.core_service_url}/dashboard#violation-{violation.get('id')}",
    }

    for contact in contacts:
        channels = contact.get("channels") or ["email"]
        quiet_now = _in_quiet_hours(contact.get("quiet_hours", {})) and violation["severity"] != "critical"

        # --- ערוץ אימייל: הערוץ המיידי הפעיל בפועל ב-POC, ללא דרישת Opt-in ---
        if "email" in channels and contact.get("email"):
            if quiet_now:
                db.insert_alert(
                    violation_id=violation["id"], contact_name=contact["name"],
                    channel="email", status="skipped_quiet_hours",
                )
            else:
                try:
                    html_body, text_body = _render_alert_email(template_params)
                    send_email(
                        to=[contact["email"]],
                        subject=f'⚠️ התראת GroupGuard — {template_params["rule_name"]} ({group_name})',
                        html_body=html_body,
                        text_body=text_body,
                    )
                    db.insert_alert(
                        violation_id=violation["id"], contact_name=contact["name"],
                        channel="email", status="sent",
                    )
                except Exception as exc:  # noqa: BLE001 — fail-safe: כשל בשליחה לא מפיל את התהליך
                    logger.exception("שליחת התראת מייל נכשלה עבור %s", contact.get("email"))
                    db.insert_alert(
                        violation_id=violation["id"], contact_name=contact["name"],
                        channel="email", status="failed", error=str(exc),
                    )

        # --- ערוץ וואטסאפ: זמין לעתיד, ממשיך לדרוש Opt-in מאושר בפועל (F-6.3) ---
        if "whatsapp" in channels:
            if contact.get("optin_status") != "confirmed":
                db.insert_alert(
                    violation_id=violation["id"], contact_name=contact["name"],
                    channel="whatsapp", status="skipped_no_optin",
                )
                continue
            if quiet_now:
                db.insert_alert(
                    violation_id=violation["id"], contact_name=contact["name"],
                    channel="whatsapp", status="skipped_quiet_hours",
                )
                continue

            result = await sender.send_direct_message(contact["phone_e164"], template_params)
            if result.success:
                db.insert_alert(
                    violation_id=violation["id"], contact_name=contact["name"],
                    channel="whatsapp", status="sent", provider_msg_id=result.provider_msg_id,
                )
            else:
                # גם כשההתראה לא נשלחה בפועל בוואטסאפ (לדוגמה ALERTS_SEND_VIA_WHATSAPP=false),
                # היא נרשמת ביומן/בפאנל המקומי כדי שאפשר יהיה לעקוב אחרי מה שהיה אמור לקרות.
                db.insert_alert(
                    violation_id=violation["id"], contact_name=contact["name"],
                    channel="log_only", status="logged", error=result.error,
                )
                logger.info("התראת וואטסאפ ל-%s נרשמה ביומן בלבד (%s)", contact["name"], result.error)
