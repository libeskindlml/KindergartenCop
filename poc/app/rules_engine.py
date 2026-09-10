"""
מנוע חוקים היברידי (סעיף 5.5): חוקים דטרמיניסטיים (מהירים, ללא AI) +
חוקים סמנטיים (LLM, דרך app.claude_client). כולל רשימה לבנה, Cooldown,
ו-Shadow Mode (48 שעות ראשונות — הפרות נרשמות אך לא מתריעות).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import db
from app.claude_client import classify_message
from app.config import get_rules_config

logger = logging.getLogger("groupguard.rules")

_URL_RE = re.compile(r"https?://([\w.-]+)", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?:\+?\d{1,3}[-\s]?)?0?\d{2,3}[-\s]?\d{7}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# הודעות טריוויאליות שלא שוות קריאת LLM (חיסכון בעלויות, סעיף 5.5.2)
_TRIVIAL_TEXTS = {"תודה", "thanks", "thx", "ok", "בסדר", "👍", "😂", "haha", "חחח"}

# רמות הקפדה לחוקים דטרמיניסטיים, לפי סעיף 5.5.1. ה-POC כולל 3 חוקים דטרמיניסטיים
# בסה"כ (config/rules.yaml) — "מחמיר" זהה כרגע ל"בינוני" כי אין עוד חוקים לשלב הזה
# (חלונות שקט/סוגי מדיה אסורים הם MVP, סעיף 3.2/5.5.1); ההבחנה תתרחב שם.
STRICTNESS_LEVELS = {
    "kal": {"det_spam_flood"},
    "beinoni": {"det_spam_flood", "det_links_blocklist", "det_contact_info_leak"},
    "machmir": {"det_spam_flood", "det_links_blocklist", "det_contact_info_leak"},
}


def _is_trivial(text: Optional[str]) -> bool:
    if not text:
        return True
    stripped = text.strip().lower()
    return len(stripped) <= 2 or stripped in _TRIVIAL_TEXTS


def _in_shadow_mode(rules_cfg: dict, account: Optional[dict]) -> bool:
    """Multi-tenant: לכל חשבון חלון Shadow Mode אישי משלו, שמתחיל מהרגע שהקבוצה
    שלו נקשרה (accounts.shadow_started_at) — לא מרגע עליית השירות הגלובלי."""
    shadow = rules_cfg.get("shadow_mode", {})
    if not shadow.get("enabled"):
        return False
    started_at_str = (account or {}).get("shadow_started_at")
    if not started_at_str:
        # לא אמור לקרות בפועל (נקבע יחד עם monitored_group_id), אך ליתר ביטחון
        # מתייחסים כאילו הקבוצה זה עתה נקשרה — כלומר עדיין בתוך חלון ה-Shadow.
        return True
    started_at = datetime.fromisoformat(started_at_str)
    duration = timedelta(hours=shadow.get("duration_hours", 48))
    return datetime.utcnow() < started_at + duration


def _cooldown_active(
    rule_id: str, sender_phone: str, cooldown_minutes: int, severity: str, account_id: Optional[int]
) -> bool:
    if severity == "critical":
        return False  # F-5.3: קריטי תמיד מתריע, ללא cooldown
    last = db.last_violation_time(rule_id, sender_phone, account_id=account_id)
    if not last:
        return False
    last_dt = datetime.fromisoformat(last)
    return datetime.utcnow() < last_dt + timedelta(minutes=cooldown_minutes)


# --------------------------------------------------------- deterministic ---
def _check_links_blocklist(message: dict, params: dict) -> Optional[dict]:
    text = message.get("text") or ""
    domains = _URL_RE.findall(text)
    if not domains:
        return None
    blocked = set(params.get("blocked_domains", []))
    mode = params.get("mode", "blacklist")
    hit = None
    for d in domains:
        d_clean = d.lower().lstrip("www.")
        if mode == "blacklist" and any(d_clean.endswith(b) for b in blocked):
            hit = d_clean
            break
        if mode == "whitelist" and not any(d_clean.endswith(b) for b in blocked):
            hit = d_clean
            break
    if hit is None:
        return None
    return {"explanation": f"נמצא קישור לדומיין חסום: {hit}", "quoted_evidence": text[:200]}


def _check_spam_flood(message: dict, params: dict) -> Optional[dict]:
    sender = message.get("sender_phone")
    if not sender:
        return None
    window = params.get("window_seconds", 30)
    max_msgs = params.get("max_messages", 5)
    since = (datetime.utcnow() - timedelta(seconds=window)).isoformat()
    # Multi-tenant: סופרים רק הודעות מתוך הקבוצה/חשבון הזה (message["account_id"]),
    # כדי שספירת הצפה לא תדלוף בין חשבונות/קבוצות שונות של אותו שולח.
    count = db.count_messages_since(sender, since, account_id=message.get("account_id"))
    if count < max_msgs:
        return None
    return {"explanation": f"{count} הודעות מהמשתמש תוך {window} שניות (סף: {max_msgs})", "quoted_evidence": ""}


def _check_contact_info_leak(message: dict, params: dict) -> Optional[dict]:
    text = message.get("text") or ""
    findings = []
    if params.get("detect_phone") and _PHONE_RE.search(text):
        findings.append("מספר טלפון")
    if params.get("detect_email") and _EMAIL_RE.search(text):
        findings.append("כתובת מייל")
    if not findings:
        return None
    return {"explanation": f"זוהתה חשיפת פרטי קשר: {', '.join(findings)}", "quoted_evidence": text[:200]}


_DETERMINISTIC_HANDLERS = {
    "det_links_blocklist": _check_links_blocklist,
    "det_spam_flood": _check_spam_flood,
    "det_contact_info_leak": _check_contact_info_leak,
}


def _run_deterministic(message: dict, rules_cfg: dict, active_rule_ids: Optional[set[str]] = None) -> list[dict]:
    results = []
    for rule in rules_cfg.get("deterministic", []):
        if active_rule_ids is not None and rule["id"] not in active_rule_ids:
            continue
        handler = _DETERMINISTIC_HANDLERS.get(rule["id"])
        if not handler:
            logger.warning("אין handler לחוק דטרמיניסטי %s", rule["id"])
            continue
        hit = handler(message, rule.get("params", {}))
        if hit:
            results.append(
                {
                    "rule_id": rule["id"],
                    "rule_name": rule["name"],
                    "rule_kind": "deterministic",
                    "severity": rule["severity"],
                    "confidence": 1.0,
                    **hit,
                }
            )
    return results


# -------------------------------------------------------------- semantic ---
def _run_semantic(
    message: dict, rules_cfg: dict, media_analysis: Optional[dict], extra_rules: Optional[list[dict]] = None
) -> list[dict]:
    text = message.get("text") or ""
    if _is_trivial(text) and not media_analysis:
        return []

    semantic_rules = list(rules_cfg.get("semantic", [])) + list(extra_rules or [])
    if not semantic_rules:
        return []

    context = db.get_recent_messages(limit=5, before_id=message["id"])
    result = classify_message(
        message_text=text or "(הודעת מדיה, ראו ניתוח מצורף)",
        sender_display_name=message.get("sender_name") or "אנונימי",
        context_messages=context,
        rules=semantic_rules,
        media_analysis=media_analysis,
    )
    rules_by_id = {r["id"]: r for r in semantic_rules}
    violations = []
    for v in result.get("violations", []):
        rule = rules_by_id.get(v.get("rule_id"))
        if not rule:
            continue
        confidence = float(v.get("confidence", 0))
        if confidence < rule.get("confidence_threshold", 0.8):
            continue
        violations.append(
            {
                "rule_id": rule["id"],
                "rule_name": rule["name"],
                "rule_kind": "semantic",
                "severity": rule["severity"],
                "confidence": confidence,
                "explanation": v.get("explanation", ""),
                "quoted_evidence": v.get("quoted_evidence", ""),
            }
        )
    return violations


# ---------------------------------------------------------------- main ----
def evaluate_message(message_id: int, media_analysis: Optional[dict] = None) -> list[dict]:
    """
    מריץ את מלוא מנוע החוקים על הודעה: דטרמיניסטי -> סמנטי -> whitelist/cooldown/shadow.
    יוצר רשומות violations ומחזיר את הרשימה שנוצרה בפועל (כולל דגל shadow_mode).
    """
    message = db.get_message(message_id)
    if not message:
        return []

    rules_cfg = get_rules_config()
    sender_name = (message.get("sender_name") or "").strip()
    if sender_name and sender_name in set(rules_cfg.get("whitelist_display_names", [])):
        return []

    # Multi-tenant: מזהים את החשבון הבעלים של ההודעה הזאת (message["account_id"],
    # נקבע ב-main.whatsapp_reader_webhook לפי הקבוצה) — לא "החשבון" הגלובלי היחיד.
    account = db.get_account(message["account_id"]) if message.get("account_id") else None
    strictness = (account or {}).get("strictness_level", "beinoni")
    active_rule_ids = STRICTNESS_LEVELS.get(strictness, STRICTNESS_LEVELS["beinoni"])

    custom_rules_rows = db.list_custom_rules_db(account["id"]) if account else []
    extra_semantic_rules = [
        {
            "id": r["rule_id"],
            "name": r["name"],
            "severity": r["severity"],
            "confidence_threshold": r["confidence_threshold"],
            "description": r["description"],
        }
        for r in custom_rules_rows
    ]

    hits = _run_deterministic(message, rules_cfg, active_rule_ids)
    hits += _run_semantic(message, rules_cfg, media_analysis, extra_semantic_rules)

    if not hits:
        return []

    shadow = _in_shadow_mode(rules_cfg, account)
    cooldown_minutes = rules_cfg.get("cooldown_minutes", 10)
    created = []
    for hit in hits:
        if _cooldown_active(
            hit["rule_id"], message.get("sender_phone", ""), cooldown_minutes, hit["severity"], message.get("account_id")
        ):
            logger.info("Cooldown פעיל עבור חוק %s / %s — מדלג", hit["rule_id"], message.get("sender_phone"))
            continue
        vid = db.insert_violation(
            message_id=message_id,
            rule_id=hit["rule_id"],
            rule_name=hit["rule_name"],
            rule_kind=hit["rule_kind"],
            severity=hit["severity"],
            confidence=hit["confidence"],
            explanation=hit["explanation"],
            quoted_evidence=hit.get("quoted_evidence", ""),
            shadow_mode=1 if shadow else 0,
        )
        hit["id"] = vid
        hit["shadow_mode"] = shadow
        hit["message"] = message
        created.append(hit)
    return created
