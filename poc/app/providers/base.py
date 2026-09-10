"""
ממשק WhatsAppProvider (מסמך האפיון, סעיף 10.1/10.2).
מפוצל לשני Protocol-ים נפרדים בעקבות ההחלטה הארכיטקטונית ההיברידית:
  - ReaderProvider: מספר קורא בלבד (ספק לא-רשמי / Baileys) — קליטת הודעות מהקבוצה.
  - SenderProvider: מספר שולח בלבד (WhatsApp Business Cloud API הרשמי) — שליחת התראות פרטיות.
כל מימוש קונקרטי (connector-whatsapp/ עבור Reader, sender_cloud_api.py עבור Sender)
חייב לעמוד בממשק הזה, כדי שאפשר יהיה להחליף ספק מבלי לגעת בלוגיקה העסקית.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class SendResult:
    success: bool
    provider_msg_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class SessionStatus:
    connected: bool
    detail: str = ""


class ReaderProvider(Protocol):
    """מספר קורא — קליטה בלבד. לא שולח הודעות לקבוצה, למעט הודעת גילוי חד-פעמית."""

    async def get_session_status(self) -> SessionStatus: ...

    async def send_discovery_message(self, group_id: str, text: str) -> SendResult:
        """הודעת גילוי נאות בהצטרפות לקבוצה (F-2.3). הפעולה היחידה שהמספר הקורא מבצע כלפי הקבוצה."""
        ...


class SenderProvider(Protocol):
    """מספר שולח — WhatsApp Business Cloud API. שולח הודעות פרטיות (יזומות) לאנשי קשר בלבד."""

    async def send_direct_message(
        self,
        to_e164: str,
        template_params: dict,
    ) -> SendResult: ...
