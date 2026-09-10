"""
הבנת מדיה (F-4): המרת סטיקרים מ-WebP ל-PNG, חישוב hash לצורך cache
(F-4.4 — סטיקר שכבר נותח לא נשלח שוב למודל), וקריאה ל-Claude Vision
עם הקשר 5 ההודעות הקודמות (F-4.3). התוצאה נשמרת כ-ai_analysis על ההודעה.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

from PIL import Image
import io

from app import db
from app.claude_client import analyze_image
from app.config import get_settings

logger = logging.getLogger("groupguard.media")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def webp_to_png(data: bytes) -> bytes:
    """סטיקרים מגיעים כ-WebP; ממירים ל-PNG לפני שליחה למודל (סעיף 9 — AI ראייה)."""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001
        logger.exception("נכשלה המרת WebP->PNG, משתמש בבייטים המקוריים")
        return data


def save_media_file(data: bytes, sha256: str, ext: str) -> str:
    settings = get_settings()
    folder = Path(settings.media_storage_path)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sha256}.{ext}"
    if not path.exists():
        path.write_bytes(data)
    return str(path)


def analyze_media_message(
    *, raw_bytes: bytes, msg_type: str, before_message_id: Optional[int] = None
) -> dict:
    """
    מריץ הבנת מדיה מלאה עבור תמונה/סטיקר: cache lookup -> Vision call -> cache write.
    מחזיר dict בפורמט ai_analysis, וגם media_path/media_sha256 לשמירה על ההודעה.
    """
    is_sticker = msg_type == "sticker"
    media_type = "image/png"
    image_bytes = webp_to_png(raw_bytes) if is_sticker else raw_bytes
    sha256 = sha256_bytes(image_bytes)
    media_path = save_media_file(image_bytes, sha256, "png" if is_sticker else "jpg")

    context = db.get_recent_messages(limit=5, before_id=before_message_id)

    if is_sticker:
        cached = db.get_cached_sticker(sha256)
        if cached:
            # F-4.4: התיאור לא מנותח שוב, אבל "האם פוגעני בהקשר" תלוי בהקשר הנוכחי —
            # ב-POC אנו מסתפקים בערך המטמון גם לשדה ההקשרי, לשם פשטות ועלות.
            import json as _json

            return {
                "media_path": media_path,
                "media_sha256": sha256,
                "description": cached.get("description"),
                "sentiment": cached.get("sentiment"),
                "flags": _json.loads(cached.get("flags") or "{}"),
                "from_cache": True,
            }

    analysis = analyze_image(
        image_bytes=image_bytes, media_type=media_type, context_messages=context, is_sticker=is_sticker
    )

    if is_sticker and "error" not in analysis:
        db.cache_sticker(
            sha256=sha256,
            description=analysis.get("description", ""),
            sentiment=analysis.get("sentiment", ""),
            flags=analysis.get("flags", {}),
        )

    analysis["media_path"] = media_path
    analysis["media_sha256"] = sha256
    analysis["from_cache"] = False
    return analysis
