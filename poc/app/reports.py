"""
בניית הדו"ח היומי (F-7): איסוף סטטיסטיקות מה-DB, סיכום נושאים ע"י Claude,
גרף התפלגות שעתית (matplotlib), רינדור HTML (Jinja2) ושליחה דרך Gmail API.
הדו"ח נשלח תמיד, גם ביום ללא פעילות (F-7.4), ואינו כולל תוכן מלא של הודעות,
רק קטעי הפרות (F-7.3).
"""
from __future__ import annotations

import base64
import io
import logging
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import db
from app.claude_client import summarize_topics
from app.config import BASE_DIR, get_settings
from app.gmail_client import send_email

logger = logging.getLogger("groupguard.reports")

TEMPLATES_DIR = BASE_DIR / "app" / "templates"
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _hourly_chart_base64(messages: list[dict]) -> str:
    hours = [0] * 24
    for m in messages:
        try:
            dt = datetime.fromisoformat(m["received_at"])
            hours[dt.hour] += 1
        except (ValueError, KeyError):
            continue

    fig, ax = plt.subplots(figsize=(6, 2.2))
    ax.bar(range(24), hours, color="#25D366")  # ירוק וואטסאפ, כנדרש בעיצוב (סעיף 8)
    ax.set_xticks(range(0, 24, 3))
    ax.set_xlabel("שעה ביום")
    ax.set_ylabel("הודעות")
    ax.set_title("התפלגות פעילות לפי שעה")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _top_participants(messages: list[dict], top_n: int = 10) -> list[dict]:
    counter = Counter(m.get("sender_name") or m.get("sender_phone") or "אנונימי" for m in messages)
    total = sum(counter.values()) or 1
    return [
        {"name": name, "count": count, "pct": round(100 * count / total, 1)}
        for name, count in counter.most_common(top_n)
    ]


def build_and_send_daily_report(account_id: int, report_date: Optional[date] = None) -> dict:
    """
    בונה ושולח את הדו"ח היומי (F-7) עבור *חשבון ספציפי* אחד (Multi-tenant —
    כל חשבון מנטר קבוצה נפרדת ומקבל דו"ח נפרד לנמען שלו). ראו scheduler.py
    להרצה הלילית על כל החשבונות, ו-main.py לנקודת הקצה הידנית (send-now).
    """
    settings = get_settings()
    account = db.get_account(account_id)
    report_date = report_date or (datetime.now().date() - timedelta(days=1))
    date_str = report_date.isoformat()

    messages = db.get_messages_for_date(date_str, account_id=account_id)
    violations = db.get_violations_for_date(date_str, account_id=account_id)

    yesterday_str = (report_date - timedelta(days=1)).isoformat()
    yesterday_count = len(db.get_messages_for_date(yesterday_str, account_id=account_id))
    last_7_days_counts = [
        len(db.get_messages_for_date((report_date - timedelta(days=i)).isoformat(), account_id=account_id))
        for i in range(1, 8)
    ]
    avg_7d = sum(last_7_days_counts) / 7 if last_7_days_counts else 0

    by_type = Counter(m.get("msg_type", "other") for m in messages)
    unique_senders = len({m.get("sender_phone") for m in messages if m.get("sender_phone")})
    deleted_count = sum(1 for m in messages if m.get("is_deleted"))
    unprocessed_count = sum(1 for m in messages if m.get("processing_status") == "ai_failed")

    by_severity: dict[str, int] = defaultdict(int)
    for v in violations:
        by_severity[v.get("severity", "unknown")] += 1

    topics = summarize_topics([m["text"] for m in messages if m.get("text")])

    no_activity_14d = all(
        len(db.get_messages_for_date((report_date - timedelta(days=i)).isoformat(), account_id=account_id)) == 0
        for i in range(0, 14)
    )

    chart_b64 = _hourly_chart_base64(messages)

    context = {
        "group_name": (account or {}).get("group_display_name") or settings.group_display_name,
        "date": date_str,
        "total_messages": len(messages),
        "yesterday_count": yesterday_count,
        "avg_7d": round(avg_7d, 1),
        "change_vs_yesterday_pct": (
            round(100 * (len(messages) - yesterday_count) / yesterday_count, 1) if yesterday_count else None
        ),
        "by_type": dict(by_type),
        "top_participants": _top_participants(messages),
        "unique_senders": unique_senders,
        "chart_base64": chart_b64,
        "topics": topics,
        "violation_count": len(violations),
        "by_severity": dict(by_severity),
        "violations": [
            {
                "rule_name": v["rule_name"],
                "severity": v["severity"],
                "sender_name": v.get("sender_name") or "אנונימי",
                "excerpt": (v.get("quoted_evidence") or v.get("explanation") or "")[:120],
                "link": f"{settings.core_service_url}/dashboard#violation-{v['id']}",
            }
            for v in violations
        ],
        "deleted_count": deleted_count,
        "unprocessed_count": unprocessed_count,
        "no_activity_14d": no_activity_14d,
        "dashboard_link": f"{settings.core_service_url}/dashboard",
    }

    html_body = _env.get_template("report_email.html").render(**context)
    text_body = (
        f"דו\"ח יומי - {context['group_name']} - {date_str}\n"
        f"סה\"כ הודעות: {context['total_messages']} | הפרות: {context['violation_count']}\n"
        f"לצפייה מלאה בפאנל: {context['dashboard_link']}"
    )

    reports_dir = Path(settings.database_path).parent / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    html_path = reports_dir / f"{date_str}.html"
    html_path.write_text(html_body, encoding="utf-8")

    status = "built_not_sent"
    # נמען הדו"ח היומי הוא כתובת המייל של המשתמש שנרשם באשף ההרשמה (F-1.1) —
    # תואם את הקבוצה שהחשבון הזה מנטר. DAILY_REPORT_RECIPIENTS ב-.env נשאר
    # כנפילה-לאחור בלבד למקרה שאין עדיין חשבון (למשל בהרצה ידנית לפני אונבורדינג).
    recipients = [account["email"]] if account and account.get("email") else settings.daily_report_recipient_list
    try:
        if recipients:
            send_email(
                to=recipients,
                subject=f'דו"ח יומי – {context["group_name"]} – {date_str}',
                html_body=html_body,
                text_body=text_body,
            )
            status = "sent"
        else:
            logger.warning("DAILY_REPORT_RECIPIENTS ריק — הדו\"ח נבנה ונשמר אך לא נשלח.")
    except Exception:  # noqa: BLE001 — fail-safe: כשל בשליחה לא מפיל את התהליך
        logger.exception("שליחת הדו\"ח היומי נכשלה")
        status = "send_failed"

    db.insert_report(
        account_id=account_id,
        date=date_str,
        html_path=str(html_path),
        sent_to=",".join(recipients),
        status=status,
    )
    return {"status": status, "html_path": str(html_path), "context": context}
