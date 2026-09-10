"""
עטיפה ל-Anthropic API (Claude): סיווג טקסט מול חוקים סמנטיים, הבנת מדיה (Vision),
וסיכום נושאים לדו"ח היומי. פלט מובנה (JSON), Timeout 20 שניות, Retry פעמיים,
ונפילה רכה (fail-safe) — כשל במודל לא מפיל את הקליטה (סעיף 10.4, F-5.5).
לא נשלחים מספרי טלפון למודל — רק שמות תצוגה / מזהים אנונימיים.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Optional

import anthropic

from app.config import get_settings

logger = logging.getLogger("groupguard.claude")

TIMEOUT_SECONDS = 20.0
MAX_RETRIES = 2


def _client() -> anthropic.Anthropic:
    settings = get_settings()
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES)


def _extract_json(text: str) -> Optional[dict]:
    """שולף JSON מתוך תשובת המודל, גם אם עטוף בטקסט/קוד."""
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        logger.warning("נכשל בפענוח JSON מתשובת Claude: %s", text[:300])
        return None


def _format_context(context_messages: list[dict]) -> str:
    lines = []
    for m in context_messages[-5:]:
        name = m.get("sender_name") or "אנונימי"
        text = m.get("text") or f"<{m.get('msg_type')}>"
        lines.append(f"[{name}]: {text}")
    return "\n".join(lines)


SEMANTIC_CLASSIFY_SYSTEM = """\
את/ה מנוע סיווג תוכן לקבוצת וואטסאפ של קטינים/נוער, לטובת אכיפת חוקי קבוצה ואיתור \
תוכן מסכן. תקבל/י רשימת חוקים, כמה הודעות הקשר קודמות, ואת ההודעה הנוכחית. \
עליך להחזיר אך ורק JSON תקין בפורמט הבא, ללא טקסט נוסף:
{"violations": [{"rule_id": "...", "confidence": 0.0-1.0, "explanation": "...", "quoted_evidence": "..."}]}
אם אין הפרות — החזר/י {"violations": []}.
היה/י שמרן/ית ביחס לרמת הביטחון: סמן/י הפרה רק כשההקשר תומך בבירור בכוונה הפוגענית/המסכנת, \
לא רק בגלל מילה בודדת. שים/י לב במיוחד לאיתות מצוקה נפשית, ניסיון קשר לא הולם עם קטין, או בקשה למפגש פרטי עם אדם זר.\
"""


def classify_message(
    *,
    message_text: str,
    sender_display_name: str,
    context_messages: list[dict],
    rules: list[dict],
    media_analysis: Optional[dict] = None,
) -> dict:
    """מסווג הודעה מול החוקים הסמנטיים. מחזיר {"violations": [...]} ; במקרה כשל — רשימה ריקה + דגל שגיאה."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        logger.warning("ANTHROPIC_API_KEY חסר — מדלג על סיווג סמנטי.")
        return {"violations": [], "error": "missing_api_key"}

    rules_desc = "\n".join(
        f"- rule_id={r['id']} | {r['name']} (חומרה: {r['severity']}, סף ביטחון: {r.get('confidence_threshold', 0.8)}): "
        f"{r['description']}"
        for r in rules
    )
    context_desc = _format_context(context_messages)
    media_desc = f"\nניתוח מדיה של ההודעה הנוכחית: {json.dumps(media_analysis, ensure_ascii=False)}" if media_analysis else ""

    user_prompt = f"""\
חוקי הקבוצה:
{rules_desc}

הקשר (5 הודעות אחרונות):
{context_desc or '(אין הקשר קודם)'}

ההודעה לבדיקה — מאת "{sender_display_name}":
"{message_text}"{media_desc}
"""

    try:
        resp = _client().messages.create(
            model=settings.anthropic_model_classify,
            max_tokens=1024,
            system=SEMANTIC_CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = resp.content[0].text if resp.content else ""
        parsed = _extract_json(raw)
        if parsed is None:
            return {"violations": [], "error": "unparseable_response", "raw": raw}
        return parsed
    except Exception as exc:  # noqa: BLE001 — fail-safe: לא מפיל את הקליטה
        logger.exception("שגיאה בקריאה ל-Claude לסיווג הודעה")
        return {"violations": [], "error": str(exc)}


IMAGE_ANALYSIS_SYSTEM = """\
את/ה מודל ראייה שמנתח תמונות/סטיקרים מקבוצת וואטסאפ של קטינים/נוער, לצורך בטיחות \
ואכיפת חוקי קבוצה. החזר/י אך ורק JSON תקין:
{"description": "עד 100 מילים", "ocr_text": "טקסט שמופיע בתמונה אם יש, אחרת ריק",
 "flags": {"nudity": bool, "violence": bool, "personal_document": bool,
           "private_chat_screenshot": bool, "advertisement": bool},
 "sentiment": "humor|mockery|support|threat|sexual|neutral",
 "is_offensive_in_context": bool}
"""


def analyze_image(
    *, image_bytes: bytes, media_type: str, context_messages: list[dict], is_sticker: bool = False
) -> dict:
    """הבנת תמונה/סטיקר + ניתוח בהקשר 5 ההודעות הקודמות (F-4.1/F-4.2/F-4.3)."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        return {"error": "missing_api_key"}

    context_desc = _format_context(context_messages)
    kind = "סטיקר" if is_sticker else "תמונה"
    b64 = base64.b64encode(image_bytes).decode("ascii")

    try:
        resp = _client().messages.create(
            model=settings.anthropic_model_classify,
            max_tokens=512,
            system=IMAGE_ANALYSIS_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"סוג: {kind}\nהקשר (5 הודעות אחרונות):\n{context_desc or '(אין)'}"},
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    ],
                }
            ],
        )
        raw = resp.content[0].text if resp.content else ""
        parsed = _extract_json(raw)
        return parsed if parsed is not None else {"error": "unparseable_response", "raw": raw}
    except Exception as exc:  # noqa: BLE001
        logger.exception("שגיאה בניתוח תמונה/סטיקר")
        return {"error": str(exc)}


def summarize_topics(messages_text: list[str]) -> list[str]:
    """3-5 נושאים מרכזיים שנדונו, לדו"ח היומי (F-7.2)."""
    settings = get_settings()
    if not settings.anthropic_api_key or not messages_text:
        return []
    joined = "\n".join(messages_text[:500])  # הגנה מפני קונטקסט ענק ב-POC
    try:
        resp = _client().messages.create(
            model=settings.anthropic_model_summarize,
            max_tokens=300,
            system='סכם/י את 3-5 הנושאים המרכזיים שנדונו בשיחה. החזר/י אך ורק JSON: {"topics": ["...", "..."]}',
            messages=[{"role": "user", "content": joined}],
        )
        raw = resp.content[0].text if resp.content else ""
        parsed = _extract_json(raw)
        return (parsed or {}).get("topics", [])
    except Exception:  # noqa: BLE001
        logger.exception("שגיאה בסיכום נושאים")
        return []
