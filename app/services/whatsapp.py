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
        if not self.is_configured:
            logger.warning("WhatsApp not configured — skipping message send")
            return {"status": "skipped", "reason": "not_configured"}

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(self._url, json=payload, headers=self._headers)
                response.raise_for_status()
                data = response.json()
                logger.info(f"WhatsApp message sent: {data}")
                return {"status": "sent", "response": data}
        except httpx.HTTPStatusError as e:
            logger.error(f"WhatsApp API error: {e.response.status_code} — {e.response.text}")
            return {"status": "failed", "error": e.response.text}
        except Exception as e:
            logger.error(f"WhatsApp send error: {e}")
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
