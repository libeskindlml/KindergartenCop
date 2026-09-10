"""
SenderProvider — מימוש דרך WhatsApp Business Cloud API הרשמי של Meta.
משמש אך ורק לשליחת התראות פרטיות (יזומות) לאנשי קשר, דרך תבנית הודעה מאושרת
(Message Template) — כנדרש למספר עסקי שאינו בתוך חלון שיחה פתוח.
תיעוד: https://developers.facebook.com/docs/whatsapp/cloud-api/get-started

הערה: כדי לשלוח בפועל יש (1) למלא WA_CLOUD_API_* ב-.env, (2) ליצור ולאשר
מול Meta תבנית הודעה בשם WA_ALERT_TEMPLATE_NAME, (3) לוודא Opt-in של הנמען
(F-6.3). כל עוד ALERTS_SEND_VIA_WHATSAPP=false — הקריאה תיחסם ותירשם ביומן בלבד,
כדי לאפשר להריץ את שאר המערכת עוד לפני שההגדרות מול Meta מוכנות.
"""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.providers.base import SendResult

logger = logging.getLogger("groupguard.sender")


class CloudApiSenderProvider:
    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def _base_url(self) -> str:
        s = self.settings
        return f"https://graph.facebook.com/{s.wa_cloud_api_version}/{s.wa_cloud_api_phone_number_id}/messages"

    def is_configured(self) -> bool:
        s = self.settings
        return bool(s.wa_cloud_api_token and s.wa_cloud_api_phone_number_id)

    async def send_direct_message(self, to_e164: str, template_params: dict) -> SendResult:
        """
        שולח הודעת התראה פרטית לפי תבנית ה-Message Template (F-6.4).
        template_params: מילון עם placeholders לתבנית, לדוגמה:
            {"group_name": ..., "rule_name": ..., "severity": ..., "sender_name": ...,
             "sender_phone": ..., "time": ..., "message_excerpt": ..., "explanation": ..., "link": ...}
        """
        s = self.settings
        if not s.alerts_send_via_whatsapp:
            logger.info("ALERTS_SEND_VIA_WHATSAPP=false — התראה נרשמת ביומן בלבד, לא נשלחת בפועל ל-%s", to_e164)
            return SendResult(success=False, error="alerts_disabled_by_config")

        if not self.is_configured():
            logger.warning("WA_CLOUD_API_TOKEN / PHONE_NUMBER_ID חסרים ב-.env — לא ניתן לשלוח.")
            return SendResult(success=False, error="sender_not_configured")

        to_number = to_e164.lstrip("+")
        components = [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": str(v)} for v in template_params.values()],
            }
        ]
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "template",
            "template": {
                "name": s.wa_alert_template_name,
                "language": {"code": s.wa_alert_template_lang},
                "components": components,
            },
        }
        headers = {"Authorization": f"Bearer {s.wa_cloud_api_token}", "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(self._base_url, json=payload, headers=headers)
            data = resp.json()
            if resp.status_code >= 400:
                logger.error("שליחת התראה נכשלה (%s): %s", resp.status_code, data)
                return SendResult(success=False, error=str(data))
            msg_id = data.get("messages", [{}])[0].get("id")
            return SendResult(success=True, provider_msg_id=msg_id)
        except Exception as exc:  # noqa: BLE001 — POC: כשל ברשת/API לא יפיל את התהליך
            logger.exception("שגיאה בשליחת התראת וואטסאפ")
            return SendResult(success=False, error=str(exc))


def get_sender_provider() -> CloudApiSenderProvider:
    return CloudApiSenderProvider()
