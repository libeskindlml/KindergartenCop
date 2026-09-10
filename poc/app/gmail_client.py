"""
שליחת מייל דרך SMTP של Gmail, עם App Password (16 תווים) — במקום Gmail API/OAuth
(פשוט יותר להגדרה ב-POC: לא דורש קובץ OAuth Client מ-Google Cloud Console ולא
דפדפן לאישור הרשאות, רק חשבון Gmail עם אימות דו-שלבי + App Password).

איך יוצרים App Password:
  1. ודאו שאימות דו-שלבי (2-Step Verification) פעיל בחשבון ה-Gmail.
  2. גשו ל-https://myaccount.google.com/apppasswords
  3. צרו סיסמה חדשה (בחרו "אחר" ותנו שם, לדוגמה "GroupGuard").
  4. Google תציג סיסמה בת 16 תווים (בפורמט "xxxx xxxx xxxx xxxx") — יש להזין
     אותה ב-.env תחת SMTP_PASSWORD, בלי רווחים.
  5. את כתובת ה-Gmail עצמה מזינים תחת SMTP_USER — היא משמשת גם ככתובת השולח.

הערה: סיסמת החשבון הרגילה לא תעבוד לאימות SMTP — Google חוסמת "אפליקציות
פחות מאובטחות"; App Password הוא הדרך הנתמכת היחידה כשיש אימות דו-שלבי.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import get_settings

logger = logging.getLogger("groupguard.smtp")


def send_email(*, to: list[str], subject: str, html_body: str, text_body: str) -> str:
    """שולח מייל MIME multipart (text/plain + text/html) דרך SMTP של Gmail. מחזיר Message-Id."""
    settings = get_settings()
    if not to:
        raise ValueError("רשימת נמענים (DAILY_REPORT_RECIPIENTS) ריקה.")
    if not settings.smtp_user or not settings.smtp_password:
        raise RuntimeError(
            "SMTP_USER / SMTP_PASSWORD חסרים ב-.env. יש להזין את כתובת ה-Gmail ואת "
            "ה-App Password בן 16 התווים (ראו את ההסבר בראש app/gmail_client.py)."
        )

    message = MIMEMultipart("alternative")
    message["From"] = settings.smtp_user
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()

    if settings.smtp_port == 465:
        # SSL ישיר מהחיבור הראשון
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=20) as server:
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_user, to, message.as_string())
    else:
        # ברירת המחדל: פורט 587 עם STARTTLS
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_user, to, message.as_string())

    message_id = message.get("Message-Id", "")
    logger.info('דו"ח יומי נשלח דרך SMTP, נמענים=%s', to)
    return message_id
