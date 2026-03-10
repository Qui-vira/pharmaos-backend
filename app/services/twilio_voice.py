"""
PharmaOS AI - Twilio Voice Service
Handles voice call management, TwiML generation, speech-to-text, text-to-speech.

Call Flow:
1. Inbound call → Twilio hits /webhooks/twilio/voice
2. System plays greeting TwiML, starts <Gather> for speech input
3. Twilio sends speech transcription to /webhooks/twilio/gather
4. System processes intent via AI → generates TwiML response
5. Loop until order confirmed or caller transferred to human
6. On completion: save VoiceCallLog + Transcript, create order if applicable

HUMAN ACTION REQUIRED:
- Create Twilio account: https://www.twilio.com
- Provision a phone number
- Set webhook URL to: https://your-domain.com/api/v1/webhooks/twilio/voice
- Set environment variables: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER
"""

import hashlib
import hmac
import logging
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


class TwiMLBuilder:
    """
    Builds TwiML XML responses for Twilio Voice.
    TwiML controls what happens during a phone call.
    """

    @staticmethod
    def greeting(gather_url: str, language: str = "en-NG") -> str:
        """
        Initial greeting with speech gathering.
        Uses <Gather> to listen for speech input.
        """
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Aditi" language="{language}">
        Welcome to PharmaOS. You can place an order, check product availability, or ask about your delivery.
        Please tell me what you need.
    </Say>
    <Gather input="speech" action="{gather_url}" method="POST"
            speechTimeout="3" timeout="10" language="{language}"
            speechModel="phone_call" enhanced="true">
        <Say voice="Polly.Aditi" language="{language}">
            I'm listening.
        </Say>
    </Gather>
    <Say voice="Polly.Aditi" language="{language}">
        I didn't hear anything. Let me transfer you to a pharmacist.
    </Say>
    <Redirect>{gather_url}?fallback=human</Redirect>
</Response>"""

    @staticmethod
    def speak_and_gather(
        message: str,
        gather_url: str,
        language: str = "en-NG",
    ) -> str:
        """Speak a message then gather more speech input."""
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Aditi" language="{language}">
        {message}
    </Say>
    <Gather input="speech" action="{gather_url}" method="POST"
            speechTimeout="3" timeout="10" language="{language}"
            speechModel="phone_call" enhanced="true">
        <Say voice="Polly.Aditi" language="{language}">
            Please go ahead.
        </Say>
    </Gather>
    <Say voice="Polly.Aditi" language="{language}">
        I didn't catch that. Let me connect you to someone who can help.
    </Say>
    <Redirect>{gather_url}?fallback=human</Redirect>
</Response>"""

    @staticmethod
    def speak_and_confirm(
        message: str,
        gather_url: str,
        language: str = "en-NG",
    ) -> str:
        """Speak a message and ask for yes/no confirmation via DTMF or speech."""
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Aditi" language="{language}">
        {message}
    </Say>
    <Gather input="speech dtmf" action="{gather_url}" method="POST"
            numDigits="1" speechTimeout="3" timeout="10" language="{language}"
            speechModel="phone_call" enhanced="true">
        <Say voice="Polly.Aditi" language="{language}">
            Press 1 or say yes to confirm. Press 2 or say no to cancel.
        </Say>
    </Gather>
    <Say voice="Polly.Aditi" language="{language}">
        I'll take that as a no. Goodbye.
    </Say>
    <Hangup/>
</Response>"""

    @staticmethod
    def speak_and_end(message: str, language: str = "en-NG") -> str:
        """Speak a final message and hang up."""
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Aditi" language="{language}">
        {message}
    </Say>
    <Hangup/>
</Response>"""

    @staticmethod
    def transfer_to_human(pharmacy_phone: str, language: str = "en-NG") -> str:
        """Transfer the call to a human pharmacist."""
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Aditi" language="{language}">
        Let me connect you to a pharmacist. Please hold.
    </Say>
    <Dial timeout="30" callerId="{settings.TWILIO_PHONE_NUMBER or '+0000000000'}">
        <Number>{pharmacy_phone}</Number>
    </Dial>
    <Say voice="Polly.Aditi" language="{language}">
        Sorry, no one is available right now. Please try again later or send us a message on WhatsApp. Goodbye.
    </Say>
    <Hangup/>
</Response>"""

    @staticmethod
    def order_summary(
        items_text: str,
        total: float,
        gather_url: str,
        language: str = "en-NG",
    ) -> str:
        """Read back order summary and ask for confirmation."""
        message = (
            f"Here is your order summary. {items_text}. "
            f"The total is {total:,.0f} Naira. "
            f"Would you like to confirm this order?"
        )
        return TwiMLBuilder.speak_and_confirm(message, gather_url, language)

    @staticmethod
    def order_confirmed(order_number: str, language: str = "en-NG") -> str:
        """Confirm order placement and hang up."""
        return TwiMLBuilder.speak_and_end(
            f"Your order {order_number} has been placed successfully. "
            f"You will receive a WhatsApp message with the details. Thank you for calling PharmaOS. Goodbye.",
            language,
        )


class TwilioService:
    """Client for Twilio Voice API."""

    def __init__(self):
        self.account_sid = settings.TWILIO_ACCOUNT_SID
        self.auth_token = settings.TWILIO_AUTH_TOKEN
        self.phone_number = settings.TWILIO_PHONE_NUMBER

    @property
    def is_configured(self) -> bool:
        return bool(self.account_sid and self.auth_token and self.phone_number)

    async def make_outbound_call(
        self, to: str, twiml_url: str,
    ) -> dict:
        """
        Initiate an outbound voice call.
        Used for proactive notifications (e.g. order ready for pickup).
        """
        if not self.is_configured:
            return {"status": "skipped", "reason": "twilio_not_configured"}

        url = f"{TWILIO_API_BASE}/Accounts/{self.account_sid}/Calls.json"

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    url,
                    data={
                        "To": to,
                        "From": self.phone_number,
                        "Url": twiml_url,
                    },
                    auth=(self.account_sid, self.auth_token),
                )
                response.raise_for_status()
                data = response.json()
                return {"status": "initiated", "call_sid": data.get("sid")}
        except Exception as e:
            logger.error(f"Twilio outbound call error: {e}")
            return {"status": "failed", "error": str(e)}

    async def get_call_details(self, call_sid: str) -> dict:
        """Fetch call details from Twilio."""
        if not self.is_configured:
            return {}

        url = f"{TWILIO_API_BASE}/Accounts/{self.account_sid}/Calls/{call_sid}.json"

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(url, auth=(self.account_sid, self.auth_token))
                response.raise_for_status()
                return response.json()
        except Exception as e:
            logger.error(f"Twilio call details error: {e}")
            return {}


def verify_twilio_signature(url: str, params: dict, signature: str) -> bool:
    """
    Verify Twilio webhook request signature.
    https://www.twilio.com/docs/usage/security#validating-requests
    """
    if not settings.TWILIO_AUTH_TOKEN:
        return True  # Skip if not configured

    # Build the string to sign
    s = url
    for key in sorted(params.keys()):
        s += key + params[key]

    # HMAC SHA1
    computed = hmac.new(
        settings.TWILIO_AUTH_TOKEN.encode("utf-8"),
        s.encode("utf-8"),
        hashlib.sha1,
    ).digest()

    import base64
    expected = base64.b64encode(computed).decode("utf-8")

    return hmac.compare_digest(signature or "", expected)


# ─── Voice Call State Machine ───────────────────────────────────────────────


class VoiceCallState:
    """
    Manages the conversational state for an active voice call.
    Stored in Redis for cross-request persistence.

    States:
    - greeting: Initial greeting, waiting for first speech input
    - intent_detected: AI detected an intent, processing
    - collecting_items: Gathering order items from speech
    - confirming_order: Reading back order for confirmation
    - completed: Call finished
    - transferred: Caller transferred to human
    """

    @staticmethod
    def key(call_sid: str) -> str:
        return f"voice_call:{call_sid}"

    @staticmethod
    async def get(call_sid: str) -> dict:
        """Get call state from Redis."""
        # In production, use Redis:
        # redis_client = get_redis()
        # data = await redis_client.get(VoiceCallState.key(call_sid))
        # return json.loads(data) if data else {"state": "greeting", "items": [], "transcript": []}
        return {"state": "greeting", "items": [], "transcript": []}

    @staticmethod
    async def set(call_sid: str, state: dict):
        """Save call state to Redis with 1hr TTL."""
        # In production:
        # redis_client = get_redis()
        # await redis_client.setex(VoiceCallState.key(call_sid), 3600, json.dumps(state))
        pass

    @staticmethod
    async def delete(call_sid: str):
        """Clean up call state after completion."""
        # redis_client = get_redis()
        # await redis_client.delete(VoiceCallState.key(call_sid))
        pass


# ─── Singletons ─────────────────────────────────────────────────────────────

twiml = TwiMLBuilder()
twilio_service = TwilioService()
