#!/usr/bin/env python3
"""
מרענן את קבצי התצוגה המקדימה הסטטית בתיקייה הזו (preview/), אחרי שעורכים
את app/templates/onboarding.html או app/templates/dashboard.html.
הרצה: python3 preview/regenerate.py   (מהתיקייה poc/, או מכל מקום — הנתיבים יחסיים לקובץ הזה)
תלות יחידה: jinja2 (מותקן כבר ב-.venv של הפרויקט, וב-Python המערכת של macOS בד"כ קיים גם כן).
"""
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "app" / "templates"
PREVIEW_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(ROOT))
from app.config import get_settings  # noqa: E402  (אחרי שינוי sys.path בכוונה)

# --- 1. onboarding.html: קובץ עצמאי לחלוטין (Offline Preview מובנה בתוכו), עם
#        מספר הסוכן האמיתי (READER_WHATSAPP_NUMBER מה-.env) מוטבע במסך הסיום
#        המדומה, כדי שתראו בתצוגה המקדימה בדיוק את מה שיוצג בהרצה האמיתית ---
onboarding_src = (TEMPLATES_DIR / "onboarding.html").read_text(encoding="utf-8")

settings = get_settings()
real_number = settings.reader_whatsapp_number or "(עדיין לא הוגדר ב-.env — READER_WHATSAPP_NUMBER)"

mock_placeholder = "+1 555 123 4567 (דוגמה בלבד — תצוגה מקדימה)"
onboarding_preview = onboarding_src.replace(
    mock_placeholder,
    f"{real_number} (מה-.env שלכם, נכון לרגע יצירת התצוגה המקדימה)",
)

(PREVIEW_DIR / "onboarding_preview.html").write_text(onboarding_preview, encoding="utf-8")
print("onboarding_preview.html עודכן (כולל מספר הסוכן האמיתי מה-.env).")

# --- 2. dashboard.html: תלוי ב-Jinja2, מרונדר כאן עם נתוני דוגמה קבועים ---
env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))

sample_messages = [
    {"received_at": "2026-09-05T18:42:10", "sender_name": "נועה", "sender_phone": "+972501112222",
     "msg_type": "text", "text": "מישהו יודע באיזו שעה יש חוג מחר?", "processing_status": "rules_checked",
     "is_deleted": 0, "is_edited": 0},
    {"received_at": "2026-09-05T18:40:02", "sender_name": "עידן", "sender_phone": "+972502223333",
     "msg_type": "sticker", "text": None, "processing_status": "analyzed", "is_deleted": 0, "is_edited": 0},
    {"received_at": "2026-09-05T18:35:44", "sender_name": "מאיה", "sender_phone": "+972503334444",
     "msg_type": "text", "text": "בואו נצטרף כולם לקבוצה החדשה: https://t.me/spamchannel",
     "processing_status": "rules_checked", "is_deleted": 0, "is_edited": 0},
    {"received_at": "2026-09-05T18:20:01", "sender_name": "תום", "sender_phone": "+972504445555",
     "msg_type": "text", "text": "מישהו רוצה להיפגש היום אחרי בית הספר?",
     "processing_status": "rules_checked", "is_deleted": 1, "is_edited": 0},
]

sample_violations = [
    {"id": 3, "created_at": "2026-09-05T18:35:45", "rule_name": "קישורים אסורים", "rule_kind": "deterministic",
     "shadow_mode": 0, "severity": "medium", "sender_name": "מאיה", "sender_phone": "+972503334444",
     "confidence": 1.0, "explanation": "נמצא קישור לדומיין חסום: t.me", "quoted_evidence": "https://t.me/spamchannel",
     "status": "open"},
    {"id": 2, "created_at": "2026-09-05T17:10:12", "rule_name": "בריונות / השפלה כלפי חבר אחר", "rule_kind": "semantic",
     "shadow_mode": 0, "severity": "high", "sender_name": "דני", "sender_phone": "+972505556666",
     "confidence": 0.91, "explanation": "הודעה שמלגלגת על חבר אחר בקבוצה בעקבות טעות שעשה", "quoted_evidence": "כולם צוחקים עליך, אתה כזה מביך",
     "status": "handled"},
    {"id": 1, "created_at": "2026-09-05T16:02:31", "rule_name": "סכנה / פנייה לא הולמת ממבוגר לקטין", "rule_kind": "semantic",
     "shadow_mode": 1, "severity": "critical", "sender_name": "לא ידוע", "sender_phone": "+972509998888",
     "confidence": 0.86, "explanation": "בקשה למפגש פרטי ללא ידיעת ההורים", "quoted_evidence": "בוא נפגש רק שנינו, אל תספר להורים",
     "status": "open"},
]

sample_alerts = [
    {"sent_at": "2026-09-05T18:35:46", "contact_name": "הורה ראשי", "channel": "email", "status": "sent", "error": None},
    {"sent_at": "2026-09-05T17:10:13", "contact_name": "הורה ראשי", "channel": "log_only", "status": "logged", "error": "alerts_disabled_by_config"},
]

# Multi-tenant (דוגמה בלבד): חשבון-דוגמה + אנשי קשר + חוקים מותאמים + דוחות,
# כדי שהתצוגה המקדימה תשקף את אותו dashboard.html שמוצג הן למשתמש קצה
# מחובר והן לאדמין (ראו app/main.py: _render_account_dashboard).
sample_account = {
    "id": 1,
    "owner_name": "לירון (דוגמה)",
    "email": "demo@example.com",
    "strictness_level": "beinoni",
    "monitored_group_id": "120363012345678901@g.us",
    "created_at": "2026-08-20T09:00:00",
}
sample_contacts = [
    {"name": "הורה ראשי", "email": "parent@example.com", "phone_e164": "+972521234567",
     "min_severity": "medium", "channels": ["email"]},
]
sample_custom_rules = [
    {"name": "אין לתאם מפגשים מחוץ לביה\"ס", "description": "כל הודעה שמתאמת מפגש פיזי מחוץ למסגרת מאורגנת",
     "severity": "high"},
]
sample_reports = [
    {"date": "2026-09-04", "sent_to": "demo@example.com", "status": "sent"},
]

html = env.get_template("dashboard.html").render(
    group_name="קבוצת הפיילוט (דוגמה)",
    reader_connected=True,
    reader_detail="מחובר",
    group_bound=True,
    monitored_group_id=sample_account["monitored_group_id"],
    account=sample_account,
    contacts=sample_contacts,
    custom_rules=sample_custom_rules,
    reports=sample_reports,
    admin_view=False,
    logout_url="/logout",
    send_report_url="/api/reports/send-now",
    messages=sample_messages,
    violations=sample_violations,
    alerts=sample_alerts,
)

banner = (
    '<div style="background:#eef5ff;border-bottom:2px solid #a9c7f5;color:#1a4a8a;'
    'padding:10px 24px;font-size:13px;text-align:center">'
    '🧪 תצוגה מקדימה סטטית עם נתוני דוגמה בלבד — נוצרה מתוך app/templates/dashboard.html. '
    'הכפתורים "נכון/טופל" ו-"שגוי" לא פעילים כאן (דורשים שרת). להרצה חיה עם נתונים אמיתיים: '
    '<code>./scripts/run_core.sh</code> ואז <a href="http://localhost:8000/dashboard" style="color:inherit">localhost:8000/dashboard</a>.'
    '</div>\n'
)
html = html.replace("<body>\n", "<body>\n" + banner, 1)

with open(PREVIEW_DIR / "dashboard_preview.html", "w", encoding="utf-8") as f:
    f.write(html)
print("dashboard_preview.html עודכן.")
