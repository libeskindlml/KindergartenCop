"""
תזמון הדו"ח היומי — Celery Beat יוחלף ב-APScheduler בשלב ה-POC (מספיק לתהליך
יחיד, קבוצה אחת). ריצה יומית בחצות שעון ישראל + buffer קטן (F-7.1/7.2).
"""
from __future__ import annotations

import logging
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import db
from app.config import get_settings
from app.reports import build_and_send_daily_report

logger = logging.getLogger("groupguard.scheduler")

_scheduler: Optional[AsyncIOScheduler] = None


def _run_daily_report_job() -> None:
    """Multi-tenant: מריץ ושולח דו"ח יומי בנפרד עבור כל חשבון שסיים אונבורדינג —
    לא דו"ח גלובלי יחיד. כשל בדו"ח של חשבון אחד לא עוצר את שאר החשבונות."""
    accounts = [a for a in db.list_accounts() if a.get("onboarding_completed")]
    logger.info("מריץ בניית דו\"חות יומיים מתוזמנים עבור %d חשבונות", len(accounts))
    for account in accounts:
        try:
            result = build_and_send_daily_report(account["id"])
            logger.info("דו\"ח יומי לחשבון %s (%s) הושלם: %s", account["id"], account.get("email"), result.get("status"))
        except Exception:  # noqa: BLE001 — fail-safe: כשל בדו"ח של חשבון אחד לא מפיל את השאר
            logger.exception("בניית/שליחת הדו\"ח היומי נכשלה עבור חשבון %s", account["id"])


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    settings = get_settings()
    _scheduler = AsyncIOScheduler(timezone=settings.timezone)
    trigger = CronTrigger(hour=settings.daily_report_hour, minute=settings.daily_report_minute)
    _scheduler.add_job(_run_daily_report_job, trigger, id="daily_report", replace_existing=True)
    _scheduler.start()
    logger.info(
        "מתזמן הדו\"ח היומי הופעל: %02d:%02d %s", settings.daily_report_hour, settings.daily_report_minute, settings.timezone
    )
    return _scheduler


def stop_scheduler() -> None:
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
