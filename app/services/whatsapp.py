"""
PharmaOS AI - WhatsApp Cloud API Service
Handles all outbound messaging: text, templates, interactive buttons.

HUMAN ACTION REQUIRED:
- Set up Meta Business Account
- Configure WhatsApp Business API
- Set environment variables: WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN, WHATSAPP_APP_SECRET
- Supports per-org WhatsApp numbers (future: org.whatsapp_phone_number_id)
"""

import hashlib
import hmac
import json
import logging
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

WHATSAPP_API_BASE = "https://graph.facebook.com/v23.0"


class WhatsAppService:
    """Client for WhatsApp Cloud API."""

    def __init__(self, phone_number_id: str = None, access_token: str = None):
        self.phone_number_id = phone_number_id or settings.WHATSAPP_PHONE_NUMBER_ID
        self.access_token = access_token or settings.WHATSAPP_ACCESS_TOKEN

    @property
    def is_configured(self) -> bool:
        return bool(self.phone_number_id and self.access_token)

    @property
    def _url(self) -> str:
        return f"{WHATSAPP_API_BASE}/{self.phone_number_id}/messages"

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    async def _send(self, payload: dict) -> dict:
        """Send a message via WhatsApp Cloud API."""
        recipient = payload.get("to", "?")
        msg_type = payload.get("type", "?")

        if not self.is_configured:
            logger.warning(
                "WA_SEND SKIPPED: not configured. "
                "PHONE_NUMBER_ID=%s ACCESS_TOKEN=%s",
                "SET" if self.phone_number_id else "MISSING",
                "SET" if self.access_token else "MISSING",
            )
            return {"status": "skipped", "reason": "not_configured"}

        # Log outbound attempt with credential diagnostics
        token_preview = (self.access_token or "")[:12] + "..." if self.access_token else "NONE"
        logger.info(
            "WA_SEND ATTEMPT: to=%s type=%s phone_number_id=%s token=%s url=%s",
            recipient, msg_type, self.phone_number_id, token_preview, self._url,
        )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(self._url, json=payload, headers=self._headers)

                # Log EVERY response regardless of status
                logger.info(
                    "WA_SEND RESPONSE: to=%s status_code=%d body=%s",
                    recipient, response.status_code, response.text[:500],
                )

                response.raise_for_status()
                data = response.json()

                # Extract message ID for tracking
                wa_msg_id = None
                if "messages" in data:
                    wa_msg_id = data["messages"][0].get("id") if data["messages"] else None

                logger.info(
                    "WA_SEND OK: to=%s type=%s wamid=%s",
                    recipient, msg_type, wa_msg_id,
                )
                return {"status": "sent", "wamid": wa_msg_id, "response": data}

        except httpx.HTTPStatusError as e:
            logger.error(
                "WA_SEND FAILED: to=%s status_code=%d error=%s phone_number_id=%s",
                recipient, e.response.status_code, e.response.text[:500], self.phone_number_id,
            )
            return {"status": "failed", "status_code": e.response.status_code, "error": e.response.text}
        except httpx.ConnectError as e:
            logger.error("WA_SEND CONNECT_ERROR: to=%s error=%s", recipient, str(e))
            return {"status": "failed", "error": f"Connection error: {e}"}
        except Exception as e:
            logger.error("WA_SEND EXCEPTION: to=%s error=%s", recipient, str(e), exc_info=True)
            return {"status": "failed", "error": str(e)}

    # ─── Text Messages ──────────────────────────────────────────────────

    async def send_text(self, to: str, message: str) -> dict:
        """Send a plain text message."""
        return await self._send({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": message},
        })

    # ─── Interactive Messages ───────────────────────────────────────────

    async def send_button_message(
        self, to: str, body: str, buttons: list[dict], header: str = None,
    ) -> dict:
        """
        Send a message with up to 3 reply buttons.
        buttons: [{"id": "btn_1", "title": "PICKUP"}, ...]
        """
        message = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": btn["id"], "title": btn["title"]}}
                        for btn in buttons[:3]  # Max 3 buttons
                    ]
                },
            },
        }

        if header:
            message["interactive"]["header"] = {"type": "text", "text": header}

        return await self._send(message)

    async def send_list_message(
        self, to: str, body: str, button_text: str, sections: list[dict],
    ) -> dict:
        """
        Send a message with a list menu.
        sections: [{"title": "Category", "rows": [{"id": "r1", "title": "Item", "description": "..."}]}]
        """
        return await self._send({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body},
                "action": {
                    "button": button_text,
                    "sections": sections,
                },
            },
        })

    # ─── Template Messages ──────────────────────────────────────────────

    async def send_template(
        self, to: str, template_name: str, language: str = "en",
        components: list = None,
    ) -> dict:
        """
        Send a pre-approved template message.
        Required for initiating conversations (24h window rule).
        """
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
            },
        }

        if components:
            payload["template"]["components"] = components

        return await self._send(payload)

    # ─── Specific PharmaOS Messages ─────────────────────────────────────

    async def send_refill_reminder(self, to: str, patient_name: str, medication: str) -> dict:
        """Send a medication refill reminder with PICKUP/DELIVERY buttons."""
        body = (
            f"Hello {patient_name}. Your {medication} refill is due today. "
            f"Would you like to pick it up or have it delivered?"
        )
        return await self.send_button_message(
            to=to,
            body=body,
            header="💊 Medication Refill Reminder",
            buttons=[
                {"id": "reminder_pickup", "title": "PICKUP"},
                {"id": "reminder_delivery", "title": "DELIVERY"},
                {"id": "reminder_later", "title": "REMIND LATER"},
            ],
        )

    async def send_order_confirmation(
        self, to: str, order_number: str, total: float, items_count: int,
    ) -> dict:
        """Send order confirmation to pharmacy."""
        body = (
            f"✅ Your order {order_number} has been confirmed!\n\n"
            f"Items: {items_count}\n"
            f"Total: ₦{total:,.2f}\n\n"
            f"You'll be notified when it's ready for pickup."
        )
        return await self.send_text(to=to, message=body)

    async def send_order_ready(self, to: str, order_number: str, pickup_time: str = None) -> dict:
        """Notify pharmacy that order is ready for pickup."""
        body = f"📦 Order {order_number} is ready for pickup!"
        if pickup_time:
            body += f"\n\nSuggested pickup time: {pickup_time}"

        return await self.send_button_message(
            to=to,
            body=body,
            buttons=[
                {"id": "order_coming", "title": "ON MY WAY"},
                {"id": "order_reschedule", "title": "RESCHEDULE"},
            ],
        )

    async def send_consultation_intake_question(
        self, to: str, question: str,
    ) -> dict:
        """Send an AI-generated follow-up question during consultation intake."""
        return await self.send_text(to=to, message=question)

    async def send_consultation_response(
        self, to: str, patient_name: str, total_price: float,
    ) -> dict:
        """
        Send the pharmacist-approved consultation response.
        THIS ONLY FIRES AFTER pharmacist_actions.is_approved = True.

        COMPLIANCE: Sends ONLY the total price. Does NOT include drug names.
        """
        body = (
            f"Hello {patient_name},\n\n"
            f"Your prescription is ready.\n"
            f"Total: \u20A6{total_price:,.2f}\n\n"
            f"Tap below to proceed."
        )
        return await self.send_button_message(
            to=to,
            body=body,
            buttons=[
                {"id": "pay_now", "title": "Pay Now"},
                {"id": "ask_question", "title": "Ask a Question"},
            ],
        )

    async def send_delivery_update(
        self, to: str, order_number: str, driver_name: str, eta: str,
    ) -> dict:
        """Send delivery tracking update to customer."""
        body = (
            f"🚚 Your order {order_number} is on the way!\n\n"
            f"Driver: {driver_name}\n"
            f"Estimated arrival: {eta}"
        )
        return await self.send_text(to=to, message=body)


# ─── Webhook Verification ──────────────────────────────────────────────────


def verify_webhook_signature(body: bytes, signature_header: str) -> bool:
    """Verify the HMAC SHA256 signature of an inbound WhatsApp webhook."""
    if not settings.WHATSAPP_APP_SECRET:
        return True  # Skip verification if not configured

    expected = "sha256=" + hmac.new(
        settings.WHATSAPP_APP_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature_header or "", expected)


# ─── Message Parser ────────────────────────────────────────────────────────


def parse_inbound_message(data: dict) -> list[dict]:
    """
    Parse WhatsApp webhook payload into normalized messages.
    Returns list of: {"from": "234...", "type": "text|button|list", "body": "...", "button_id": "..."}
    """
    messages = []

    try:
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        raw_messages = value.get("messages", [])
    except (IndexError, KeyError):
        return messages

    for msg in raw_messages:
        parsed = {
            "from": msg.get("from", ""),
            "message_id": msg.get("id", ""),
            "timestamp": msg.get("timestamp", ""),
            "type": msg.get("type", ""),
            "body": "",
            "button_id": None,
            "list_id": None,
        }

        if msg["type"] == "text":
            parsed["body"] = msg.get("text", {}).get("body", "")

        elif msg["type"] == "interactive":
            interactive = msg.get("interactive", {})
            if "button_reply" in interactive:
                parsed["body"] = interactive["button_reply"].get("title", "")
                parsed["button_id"] = interactive["button_reply"].get("id", "")
            elif "list_reply" in interactive:
                parsed["body"] = interactive["list_reply"].get("title", "")
                parsed["list_id"] = interactive["list_reply"].get("id", "")

        elif msg["type"] == "location":
            loc = msg.get("location", {})
            parsed["body"] = f"Location: {loc.get('latitude')}, {loc.get('longitude')}"

        if parsed["body"] or parsed["button_id"]:
            messages.append(parsed)

    return messages


# ─── Singleton ──────────────────────────────────────────────────────────────

whatsapp_service = WhatsAppService()

# Startup diagnostic — this logs once when the module loads
_phone_id = whatsapp_service.phone_number_id or ""
_token = whatsapp_service.access_token or ""
_is_test_number = _phone_id in (
    "15551727791", "15551234567",  # Meta's known test phone number IDs
    # Meta test WABAs use specific phone_number_ids — add yours if known
)
logger.info(
    "WA_INIT: configured=%s phone_number_id=%s token_len=%d token_prefix=%s api=%s%s",
    whatsapp_service.is_configured,
    _phone_id if _phone_id else "MISSING",
    len(_token),
    _token[:12] + "..." if _token else "NONE",
    WHATSAPP_API_BASE,
    " *** WARNING: This may be a Meta TEST number — cannot send to real users ***" if _is_test_number else "",
)
