"""
ReaderProvider — מימוש מול connector-whatsapp (שירות Node.js המבוסס Baileys,
ספק לא-רשמי מבוסס פרוטוקול Multi-Device — ראו סעיף 10.2 אופציה B במסמך האפיון).

הקורא עצמו (ה-WhatsApp session) רץ בתהליך Node נפרד (connector-whatsapp/index.js).
הצד הפייתוני לא מחזיק חיבור וואטסאפ ישיר; הוא (1) מקבל אירועים נכנסים מה-connector
דרך webhook ל-/webhooks/whatsapp/reader, ו-(2) שולח פקודות בקרה קלות (סטטוס,
הודעת גילוי) ל-HTTP הקטן שה-connector חושף מקומית.
"""
from __future__ import annotations

import logging
import os

import httpx

from app.config import get_settings
from app.providers.base import SendResult, SessionStatus

logger = logging.getLogger("groupguard.reader")

# ה-connector מאזין על פורט זה (control API, לא ה-webhook היוצא).
# בפריסה מכולות הליבה וה-connector הם שני מארחים נפרדים, ולכן הכתובת ניתנת
# להגדרה; ברירת המחדל היא הרצה מקומית שבה שני התהליכים על אותו מארח.
CONNECTOR_CONTROL_URL = os.environ.get("CONNECTOR_CONTROL_URL", "http://localhost:3001")


class BaileysReaderProvider:
    async def get_session_status(self) -> SessionStatus:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{CONNECTOR_CONTROL_URL}/status")
            data = resp.json()
            return SessionStatus(connected=data.get("connected", False), detail=data.get("detail", ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("לא ניתן להתחבר ל-connector-whatsapp: %s", exc)
            return SessionStatus(connected=False, detail=f"connector unreachable: {exc}")

    async def send_discovery_message(self, group_id: str, text: str) -> SendResult:
        s = get_settings()
        if not s.send_disclosure_message:
            return SendResult(success=False, error="disclosure_disabled_by_config")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{CONNECTOR_CONTROL_URL}/send-discovery",
                    json={"groupId": group_id, "text": text},
                )
            data = resp.json()
            if resp.status_code >= 400:
                return SendResult(success=False, error=str(data))
            return SendResult(success=True, provider_msg_id=data.get("id"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("שגיאה בשליחת הודעת גילוי")
            return SendResult(success=False, error=str(exc))


def get_reader_provider() -> BaileysReaderProvider:
    return BaileysReaderProvider()
