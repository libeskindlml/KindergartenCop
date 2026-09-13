"""
נקודת הכניסה של שירות הליבה (FastAPI). אחראי על:
  - קליטת אירועים מ-connector-whatsapp (webhook פנימי, מאומת בסוד משותף).
  - הרצת הבנת מדיה + מנוע החוקים + שירות ההתראות על כל הודעה נכנסת.
  - אונבורדינג + התחברות משתמשי קצה (Multi-tenant: כל חשבון = משתמש + קבוצה נפרדים).
  - פאנל HTML לצפייה בהודעות/הפרות/התראות, לכל משתמש בנפרד (F: פאנל POC).
  - פאנל אדמין (CRM) לצפייה בכל המשתמשים/הקבוצות במערכת (סיסמת אדמין יחידה).
  - הרצת/הפעלת הדו"ח היומי (מתוזמן לכל החשבונות + ידני לחשבון בודד).
הרצה: uvicorn app.main:app --reload  (ראו scripts/run_core.sh)
"""
from __future__ import annotations

import base64
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime

import re
from typing import Optional

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.sessions import SessionMiddleware

from app import db
from app.auth import hash_password, verify_admin_password, verify_password
from app.config import BASE_DIR, ensure_dirs, get_settings
from app.media import analyze_media_message
from app.notifier import notify_violation
from app.providers.reader_baileys import get_reader_provider
from app.reports import build_and_send_daily_report
from app.rules_engine import STRICTNESS_LEVELS, evaluate_message
from app.scheduler import start_scheduler, stop_scheduler
from app.transcription import VoiceTranscriptionError, transcribe_voice_message

logging.basicConfig(level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("groupguard.main")

TEMPLATES_DIR = BASE_DIR / "app" / "templates"
_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))

_MEDIA_TYPES_WITH_VISION = {"image", "sticker"}
_VOICE_MSG_TYPES = {"audio"}


# ============================================================================
# אונבורדינג — פתיחת חשבון, איש קשר להתראות וחוקי קבוצה (F-1.1, F-2.x, F-6.1)
# Multi-tenant: אין הגבלת חשבון-יחיד יותר — כל אימייל (ייחודי) יכול להירשם
# בנפרד, עם הקבוצה/אנשי הקשר/החוקים שלו. ראו get_oldest_unbound_account
# ב-db.py לאופן שבו הודעה נכנסת "משויכת" לחשבון הנכון.
# ============================================================================
_PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_VALID_SEVERITIES = {"low", "medium", "high", "critical"}


class QuietHoursIn(BaseModel):
    enabled: bool = False
    start: str = "22:00"
    end: str = "07:00"


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone_e164: str
    # אימייל הוא כרגע ערוץ ההתראה המיידי בפועל (ראו notifier.py) — לכן חובה, לא
    # אופציונלי: זו הכתובת שאליה נשלחת כל התראה בזמן אמת, מיד עם זיהוי הפרה.
    email: str = Field(min_length=3, max_length=200)
    min_severity: str = "medium"
    channels: list[str] = ["email"]
    quiet_hours: QuietHoursIn = QuietHoursIn()

    @field_validator("phone_e164")
    @classmethod
    def _validate_phone(cls, v: str) -> str:
        if not _PHONE_RE.match(v):
            raise ValueError("מספר טלפון חייב להיות בפורמט בינלאומי, לדוגמה +972501234567")
        return v

    @field_validator("email")
    @classmethod
    def _validate_contact_email(cls, v: str) -> str:
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("כתובת מייל לא תקינה")
        return v

    @field_validator("min_severity")
    @classmethod
    def _validate_min_severity(cls, v: str) -> str:
        if v not in _VALID_SEVERITIES:
            raise ValueError("סף חומרה לא תקין")
        return v


class CustomRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=3, max_length=500)
    severity: str = "medium"

    @field_validator("severity")
    @classmethod
    def _validate_severity(cls, v: str) -> str:
        if v not in _VALID_SEVERITIES:
            raise ValueError("סף חומרה לא תקין")
        return v


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str
    password: str = Field(min_length=6, max_length=200)
    group_display_name: str = Field(min_length=1, max_length=100)
    timezone: str = "Asia/Jerusalem"

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("כתובת מייל לא תקינה")
        return v


class OnboardingRequest(BaseModel):
    account: AccountIn
    contacts: list[ContactIn] = Field(min_length=1, max_length=3)
    strictness_level: str = "beinoni"
    custom_rules: list[CustomRuleIn] = Field(default_factory=list, max_length=3)

    @field_validator("strictness_level")
    @classmethod
    def _validate_strictness(cls, v: str) -> str:
        if v not in STRICTNESS_LEVELS:
            raise ValueError("רמת הקפדה לא מוכרת")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str


class AdminLoginRequest(BaseModel):
    password: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    db.init_db()
    start_scheduler()
    logger.info("GroupGuard POC core service עלה (Multi-tenant). רשימת חוקים נטענה מ-config/.")
    yield
    stop_scheduler()


app = FastAPI(title="GroupGuard POC — Core Service", lifespan=lifespan)

_settings_at_import = get_settings()
if not _settings_at_import.secret_key:
    logger.warning(
        "SECRET_KEY לא הוגדר ב-.env — נעשה שימוש במפתח זמני לא בטוח. "
        "כל המשתמשים המחוברים ינותקו בהפעלה מחדש של השירות. יש להגדיר SECRET_KEY קבוע."
    )
app.add_middleware(
    SessionMiddleware,
    secret_key=_settings_at_import.secret_key or "dev-insecure-key-change-me-in-env",
    session_cookie="groupguard_session",
    max_age=60 * 60 * 24 * 30,  # חודש
)


def _verify_secret(x_connector_secret: Optional[str]) -> None:
    settings = get_settings()
    if not x_connector_secret or x_connector_secret != settings.connector_shared_secret:
        raise HTTPException(status_code=401, detail="invalid connector secret")


def _is_admin(request: Request) -> bool:
    return bool(request.session.get("is_admin"))


def _current_account(request: Request) -> Optional[dict]:
    """מחזיר את חשבון המשתמש המחובר (Multi-tenant: session cookie חתום, לא 'החשבון
    הגלובלי'). None אם אין session תקף או שהחשבון נמחק בינתיים."""
    account_id = request.session.get("account_id")
    if not account_id:
        return None
    return db.get_account(account_id)


# ============================================================================
# ניתוב ראשי / דפי כניסה
# ============================================================================
@app.get("/", include_in_schema=False)
async def root(request: Request) -> RedirectResponse:
    if _current_account(request):
        return RedirectResponse(url="/dashboard")
    if _is_admin(request):
        return RedirectResponse(url="/admin")
    if not db.list_accounts():
        # אין עדיין אף משתמש רשום במערכת — חוויית הרצה ראשונה שולחת ישר לאונבורדינג.
        return RedirectResponse(url="/onboarding")
    return RedirectResponse(url="/login")


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page() -> str:
    """אשף הרשמה למשתמש קצה: חשבון -> איש קשר להתראות -> חוקי קבוצה -> מספר לצירוף (F-1.1/F-2.x/F-6.1).
    פתוח לכולם (Multi-tenant) — כל אימייל ייחודי יכול להירשם ולפתוח חשבון/קבוצה נפרדים."""
    return _env.get_template("onboarding.html").render()


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> str:
    return _env.get_template("login.html").render()


@app.post("/api/login")
async def api_login(payload: LoginRequest, request: Request) -> JSONResponse:
    account = db.get_account_by_email(payload.email.strip().lower())
    if not account or not verify_password(payload.password, account["password_hash"]):
        raise HTTPException(status_code=401, detail="אימייל או סיסמה שגויים")
    request.session.clear()
    request.session["account_id"] = account["id"]
    return JSONResponse({"status": "ok"})


@app.get("/logout", include_in_schema=False)
async def logout(request: Request) -> RedirectResponse:
    request.session.pop("account_id", None)
    return RedirectResponse(url="/login")


@app.get("/api/account")
async def api_account(request: Request) -> JSONResponse:
    settings = get_settings()
    account = _current_account(request)
    if not account:
        return JSONResponse({"exists": False})
    monitored = account.get("monitored_group_id") or settings.monitored_group_id
    return JSONResponse(
        {
            "exists": True,
            "onboarding_completed": bool(account.get("onboarding_completed")),
            "owner_name": account.get("owner_name"),
            "email": account.get("email"),
            "group_display_name": account.get("group_display_name"),
            "strictness_level": account.get("strictness_level"),
            "reader_phone_display": settings.reader_whatsapp_number,
            "disclosure_text": settings.disclosure_message_text,
            # הקבוצה נקשרת אוטומטית (לא מוזנת ידנית) כשמגיעה הודעה עם קוד האימות
            # התואם, ראו main.whatsapp_reader_webhook. verification_code מוצג שוב
            # כאן כדי שאם המשתמש רענן/חזר לעמוד לפני שאימת את הקבוצה, הקוד עדיין
            # יוצג לו (ולא רק ברגע ההרשמה הראשוני).
            "monitored_group_configured": bool(monitored),
            "verification_code": account.get("verification_code"),
        }
    )


@app.post("/api/onboarding/complete")
async def onboarding_complete(payload: OnboardingRequest, request: Request) -> JSONResponse:
    """
    יוצר חשבון (F-1.1), איש/י קשר להתראות (F-6.1) וחוקי קבוצה (F-5.1/5.5.2) על
    בסיס אשף ההרשמה, ומחזיר את מספר הטלפון הווירטואלי שיש להוסיף לקבוצה (F-2.2).
    Multi-tenant: כל אימייל ייחודי יכול לפתוח חשבון נפרד משלו (עם קבוצה נפרדת
    משלו) — אין יותר הגבלת חשבון-יחיד. לאחר ההרשמה המשתמש מחובר אוטומטית
    (session), כך שהקישור "מעבר לפאנל" בסוף האשף עובד מיידית.
    """
    settings = get_settings()
    verification_code = db.generate_unique_verification_code()
    try:
        account_id = db.create_account(
            email=payload.account.email.strip().lower(),
            password_hash=hash_password(payload.account.password),
            owner_name=payload.account.name,
            group_display_name=payload.account.group_display_name,
            timezone=payload.account.timezone,
            strictness_level=payload.strictness_level,
            onboarding_completed=1,
            verification_code=verification_code,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="כתובת המייל הזו כבר רשומה במערכת. נסו להתחבר במקום.")

    for contact in payload.contacts:
        db.insert_alert_contact_row(
            account_id=account_id,
            name=contact.name,
            phone_e164=contact.phone_e164,
            email=contact.email,
            min_severity=contact.min_severity,
            channels=contact.channels,
            quiet_hours=contact.quiet_hours.model_dump(),
            optin_status="pending",
        )

    for idx, rule in enumerate(payload.custom_rules):
        db.insert_custom_rule(
            account_id=account_id,
            rule_id=f"custom_{account_id}_{idx}",
            name=rule.name,
            description=rule.description,
            severity=rule.severity,
            confidence_threshold=0.8,
        )

    request.session.clear()
    request.session["account_id"] = account_id

    logger.info("אונבורדינג הושלם עבור חשבון %s (%s)", account_id, payload.account.email)
    return JSONResponse(
        {
            "status": "ok",
            "account_id": account_id,
            "reader_phone_display": settings.reader_whatsapp_number,
            "group_display_name": payload.account.group_display_name,
            "disclosure_text": settings.disclosure_message_text,
            "verification_code": verification_code,
        }
    )


@app.get("/health")
async def health() -> dict:
    reader_status = await get_reader_provider().get_session_status()
    return {
        "status": "ok",
        "reader_connected": reader_status.connected,
        "reader_detail": reader_status.detail,
        "time": datetime.utcnow().isoformat(),
    }


# ============================================================================
# Webhook מה-connector — Multi-tenant: שיוך הודעה לחשבון הנכון לפי קבוצה
# ============================================================================
@app.post("/webhooks/whatsapp/reader")
async def whatsapp_reader_webhook(
    request: Request, x_connector_secret: Optional[str] = Header(default=None)
) -> JSONResponse:
    """
    מקבל אירועים מנורמלים מ-connector-whatsapp (Baileys). ראו connector-whatsapp/index.js
    למבנה המדויק של האירועים הנשלחים. תואם לרעיון WhatsAppProvider.parse_webhook
    (סעיף 10.1), אלא שכאן הנרמול מתבצע בצד ה-connector עצמו ולא בצד הליבה.

    Multi-tenant: מספר ה-Reader יכול להיות חבר בכמה קבוצות בו-זמנית — כל קבוצה
    שייכת לחשבון (משתמש) אחר. שיוך הודעה לחשבון הנכון:
      1. אם group_id כבר קשור לחשבון קיים (accounts.monitored_group_id) — זה החשבון.
      2. אחרת, אם זו הודעת טקסט שתוכנה תואם *בדיוק* לקוד האימות של חשבון שממתין
         (סיים אונבורדינג, עוד לא נקשר לאף קבוצה) — הקבוצה נקשרת לאותו חשבון כרגע.
         זהו אימות מפורש (F-1.1/F-2.2): לא סומכים על סדר ההרשמה בלבד, כי אם שני
         משתמשים נמצאים באונבורדינג במקביל אין שום ערובה מי הוסיף את המספר ראשון.
      3. אחרת (קבוצה לא ידועה, ואין קוד אימות תואם) — מתעלמים.
    """
    _verify_secret(x_connector_secret)
    event = await request.json()
    event_type = event.get("event_type")

    incoming_group = event.get("group_id")
    account = db.get_account_by_group_id(incoming_group) if incoming_group else None

    if not account and incoming_group and event_type == "message":
        candidate_code = (event.get("text") or "").strip()
        pending = db.get_unbound_account_by_verification_code(candidate_code) if candidate_code else None
        if pending:
            db.set_monitored_group_id(pending["id"], incoming_group)
            account = db.get_account(pending["id"])  # לרענן — כולל monitored_group_id/shadow_started_at
            logger.info(
                "הקבוצה המנוטרת אומתה ונקשרה לחשבון %s (%s) לפי קוד אימות: %s",
                account["id"], account.get("email"), incoming_group,
            )
            # הודעת קוד האימות היא טכנית בלבד (handshake) — לא נכנסת לזרם ההודעות
            # המנוטרות ולא עוברת דרך מנוע החוקים.
            return JSONResponse({"status": "group_verified_and_bound", "account_id": account["id"]})

    if not account:
        return JSONResponse({"status": "ignored_unclaimed_group"})

    if event_type == "message":
        return await _handle_incoming_message(event, account["id"])
    if event_type in ("message_edit", "message_delete"):
        return _handle_message_mutation(event)
    if event_type in ("group_join", "group_leave", "group_admin_change", "group_subject_change"):
        db.insert_group_event(
            account_id=account["id"],
            event_type=event_type.replace("group_", ""),
            actor_phone=event.get("sender_phone"),
            actor_name=event.get("sender_name"),
            detail=event.get("detail", ""),
        )
        return JSONResponse({"status": "ok"})

    logger.warning("event_type לא מוכר: %s", event_type)
    return JSONResponse({"status": "ignored_unknown_event_type"})


async def _handle_incoming_message(event: dict, account_id: int) -> JSONResponse:
    # אידמפוטנטיות: אם ה-provider (connector-whatsapp) כבר שלח לנו את ההודעה הזו
    # בעבר (ריטריי ברמת ה-webhook, לדוגמה אחרי timeout/ניתוק זמני), מדלגים לגמרי —
    # לא מתמללים שוב (עלות/כפילות), לא מריצים שוב את מנוע החוקים ולא שולחים שוב
    # התראות. insert_message לבדו לא מספיק כאן כי הוא רק מונע *כפילות שורה* ב-DB;
    # בלי הבדיקה המפורשת הזו הקוד שממשיך היה עדיין רץ שוב על ההודעה הקיימת.
    provider_message_id = event.get("provider_message_id")
    if provider_message_id and db.get_message_by_provider_id(provider_message_id):
        logger.info("הודעה עם provider_message_id שכבר טופלה התקבלה שוב — מדלגים (ריטריי מה-provider)")
        return JSONResponse({"status": "duplicate_ignored"})

    message_id = db.insert_message(
        account_id=account_id,
        provider_message_id=provider_message_id,
        group_id=event.get("group_id"),
        sender_phone=event.get("sender_phone"),
        sender_name=event.get("sender_name"),
        msg_type=event.get("msg_type", "text"),
        text=event.get("text"),
        is_forwarded=1 if event.get("is_forwarded") else 0,
        reply_to_provider_id=event.get("reply_to_provider_id"),
        sent_at=event.get("sent_at") or datetime.utcnow().isoformat(),
        processing_status="received",
    )
    if message_id is None:
        return JSONResponse({"status": "error", "detail": "insert_failed"}, status_code=500)

    media_analysis = None
    msg_type = event.get("msg_type", "text")

    if msg_type in _VOICE_MSG_TYPES and event.get("media_base64"):
        transcribed_ok = await _transcribe_and_store(message_id, event)
        if not transcribed_ok:
            # תמלול נכשל (הורדה/המרה/API ריקים/שגויים) — לא ממשיכים למנוע החוקים
            # עם טקסט ריק/שגוי; ההודעה כבר נרשמה ב-DB עם processing_status מתאים.
            return JSONResponse({"status": "ok", "message_id": message_id, "violations": 0})

    if msg_type in _MEDIA_TYPES_WITH_VISION and event.get("media_base64"):
        try:
            raw_bytes = base64.b64decode(event["media_base64"])
            media_analysis = analyze_media_message(
                raw_bytes=raw_bytes, msg_type=msg_type, before_message_id=message_id
            )
            db.update_message(
                message_id,
                ai_analysis=media_analysis,
                media_path=media_analysis.get("media_path"),
                media_sha256=media_analysis.get("media_sha256"),
                processing_status="analyzed" if "error" not in media_analysis else "ai_failed",
            )
        except Exception:  # noqa: BLE001 — fail-safe (סעיף 4.1: כשל ב-AI לא מפיל קליטה)
            logger.exception("נכשלה הבנת מדיה עבור הודעה %s", message_id)
            db.update_message(message_id, processing_status="ai_failed")

    try:
        violations = evaluate_message(message_id, media_analysis=media_analysis)
        db.update_message(message_id, processing_status="rules_checked")
        for v in violations:
            await notify_violation(v)
    except Exception:  # noqa: BLE001
        logger.exception("נכשל מנוע החוקים עבור הודעה %s", message_id)
        db.update_message(message_id, processing_status="ai_failed")
        violations = []

    return JSONResponse({"status": "ok", "message_id": message_id, "violations": len(violations)})


async def _transcribe_and_store(message_id: int, event: dict) -> bool:
    """
    מתמלל הודעה קולית נכנסת (F-3.1 הרחבה) ומעדכן את הטקסט על *אותה שורת הודעה*
    שכבר נוצרה ב-DB (msg_type נשאר 'audio' — מקור ההודעה תמיד נשמר; source_type
    מסומן 'voice'). ברגע שהטקסט נשמר, evaluate_message ממשיכה בדיוק כמו על כל
    הודעת טקסט רגילה — היא רק קוראת message['text'] מה-DB, ולא מבחינה בין המקורות.
    מחזיר True אם התמלול הצליח והטקסט נשמר; False בכל כשל (ואז ה-caller לא ממשיך
    למנוע החוקים עם טקסט ריק/שגוי).
    """
    try:
        raw_bytes = base64.b64decode(event["media_base64"])
    except Exception:  # noqa: BLE001
        logger.exception("נכשל פענוח Base64 של הודעה קולית %s", message_id)
        db.update_message(message_id, processing_status="transcription_failed")
        return False

    try:
        transcription_text = await transcribe_voice_message(
            raw_bytes=raw_bytes, mimetype=event.get("media_mimetype")
        )
    except VoiceTranscriptionError as exc:
        logger.warning("תמלול הודעה קולית %s נכשל בשלב '%s': %s", message_id, exc.stage, exc)
        db.update_message(message_id, processing_status="transcription_failed")
        return False
    except Exception:  # noqa: BLE001 — fail-safe: כשל תמלול לא מפיל את הקליטה
        logger.exception("שגיאה לא צפויה בתמלול הודעה קולית %s", message_id)
        db.update_message(message_id, processing_status="transcription_failed")
        return False

    db.update_message(
        message_id,
        text=transcription_text,
        source_type="voice",
        processing_status="transcribed",
    )
    logger.info("הודעה קולית %s תומללה בהצלחה", message_id)
    return True


def _handle_message_mutation(event: dict) -> JSONResponse:
    existing = db.get_message_by_provider_id(event.get("provider_message_id"))
    if not existing:
        return JSONResponse({"status": "ignored_unknown_message"})
    if event.get("event_type") == "message_edit":
        db.update_message(
            existing["id"],
            is_edited=1,
            original_text=existing.get("original_text") or existing.get("text"),
            text=event.get("text"),
        )
    else:
        db.update_message(existing["id"], is_deleted=1)
    return JSONResponse({"status": "ok"})


# ============================================================================
# פאנל משתמש קצה — מחייב התחברות (session), מוצג עבור *חשבון אחד* בלבד
# ============================================================================
def _render_account_dashboard(account: dict, *, admin_view: bool, logout_url: str) -> dict:
    """בונה את context ה-Jinja עבור dashboard.html, עבור *חשבון ספציפי* אחד.
    reader_connected/reader_detail נוספים בנפרד ע"י הקורא (async, ראו למטה)."""
    settings = get_settings()
    monitored_group_id = account.get("monitored_group_id") or settings.monitored_group_id
    context = {
        "account": account,
        "group_name": account.get("group_display_name") or settings.group_display_name,
        "group_bound": bool(monitored_group_id),
        "monitored_group_id": monitored_group_id,
        "messages": db.list_messages(limit=100, account_id=account["id"]),
        "violations": db.list_violations(limit=100, account_id=account["id"]),
        "alerts": db.list_alerts(limit=100, account_id=account["id"]),
        "contacts": db.list_alert_contacts_db(account["id"]),
        "custom_rules": db.list_custom_rules_db(account["id"]),
        "reports": db.list_reports(account_id=account["id"]),
        "admin_view": admin_view,
        "logout_url": logout_url,
        "send_report_url": (
            f"/api/admin/accounts/{account['id']}/send-report" if admin_view else "/api/reports/send-now"
        ),
    }
    return context


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    account = _current_account(request)
    if not account:
        return RedirectResponse(url="/login")
    reader_status = await get_reader_provider().get_session_status()
    context = _render_account_dashboard(account, admin_view=False, logout_url="/logout")
    context["reader_connected"] = reader_status.connected
    context["reader_detail"] = reader_status.detail
    return _env.get_template("dashboard.html").render(**context)


@app.post("/api/violations/{violation_id}/feedback")
async def violation_feedback(violation_id: int, status: str = Form(...), feedback: str = Form("")) -> JSONResponse:
    """F-5.5: סימון הפרה כ'נכון'/'שגוי' בפאנל — משמש כדוגמאות Few-Shot בעתיד."""
    if status not in ("handled", "false_positive", "open"):
        raise HTTPException(status_code=400, detail="invalid status")
    db.set_violation_feedback(violation_id, status, feedback)
    return JSONResponse({"status": "ok"})


@app.post("/api/reports/send-now")
async def send_report_now(request: Request) -> JSONResponse:
    """שולח מיידית את הדו"ח היומי של *המשתמש המחובר* (Multi-tenant — לא דו"ח גלובלי)."""
    account = _current_account(request)
    if not account:
        raise HTTPException(status_code=401, detail="יש להתחבר")
    result = build_and_send_daily_report(account["id"])
    return JSONResponse(result)


# ============================================================================
# פאנל אדמין (CRM) — סיסמת אדמין יחידה מ-.env, לראיית כל המשתמשים/הקבוצות
# ============================================================================
@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page() -> str:
    return _env.get_template("admin_login.html").render()


@app.post("/api/admin/login")
async def api_admin_login(payload: AdminLoginRequest, request: Request) -> JSONResponse:
    if not verify_admin_password(payload.password):
        raise HTTPException(status_code=401, detail="סיסמה שגויה")
    request.session["is_admin"] = True
    return JSONResponse({"status": "ok"})


@app.get("/admin/logout", include_in_schema=False)
async def admin_logout(request: Request) -> RedirectResponse:
    request.session.pop("is_admin", None)
    return RedirectResponse(url="/admin/login")


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    if not _is_admin(request):
        return RedirectResponse(url="/admin/login")

    settings = get_settings()
    accounts = []
    for a in db.list_accounts():
        contacts = db.list_alert_contacts_db(a["id"])
        accounts.append(
            {
                **a,
                "message_count": len(db.list_messages(limit=100000, account_id=a["id"])),
                "violation_count": len(db.list_violations(limit=100000, account_id=a["id"])),
                "alert_count": len(db.list_alerts(limit=100000, account_id=a["id"])),
                "contacts_count": len(contacts),
            }
        )
    return _env.get_template("admin_dashboard.html").render(accounts=accounts, settings=settings)


@app.get("/admin/accounts/{account_id}", response_class=HTMLResponse)
async def admin_account_detail(account_id: int, request: Request):
    if not _is_admin(request):
        return RedirectResponse(url="/admin/login")
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="חשבון לא נמצא")
    reader_status = await get_reader_provider().get_session_status()
    context = _render_account_dashboard(account, admin_view=True, logout_url="/admin/logout")
    context["reader_connected"] = reader_status.connected
    context["reader_detail"] = reader_status.detail
    return _env.get_template("dashboard.html").render(**context)


@app.post("/api/admin/accounts/{account_id}/send-report")
async def admin_send_report(account_id: int, request: Request) -> JSONResponse:
    if not _is_admin(request):
        raise HTTPException(status_code=401, detail="יש להתחבר כאדמין")
    if not db.get_account(account_id):
        raise HTTPException(status_code=404, detail="חשבון לא נמצא")
    result = build_and_send_daily_report(account_id)
    return JSONResponse(result)


@app.post("/api/admin/accounts/{account_id}/delete")
async def admin_delete_account(account_id: int, request: Request) -> JSONResponse:
    """מחיקת חשבון וכל הנתונים התלויים בו — לניקוי חשבונות בדיקה בזמן ה-POC. אינה הפיכה."""
    if not _is_admin(request):
        raise HTTPException(status_code=401, detail="יש להתחבר כאדמין")
    if not db.get_account(account_id):
        raise HTTPException(status_code=404, detail="חשבון לא נמצא")
    db.delete_account(account_id)
    logger.info("אדמין מחק את חשבון %s", account_id)
    return JSONResponse({"status": "ok"})
