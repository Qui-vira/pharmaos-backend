"""
PharmaOS AI - SMS Service
Provider-agnostic SMS OTP delivery. Supports Termii and Africa's Talking.
Never logs OTP codes.
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class SMSProvider(ABC):
    """Base class for SMS providers."""

    @abstractmethod
    async def send_sms(self, phone: str, message: str) -> bool:
        """Send an SMS. Returns True on success."""
        ...


class TermiiProvider(SMSProvider):
    """Termii API — popular Nigerian SMS provider."""

    async def send_sms(self, phone: str, message: str) -> bool:
        if not settings.TERMII_API_KEY:
            logger.warning("TERMII_API_KEY not configured")
            return False
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.ng.termii.com/api/sms/send",
                    json={
                        "to": phone,
                        "from": settings.TERMII_SENDER_ID,
                        "sms": message,
                        "type": "plain",
                        "channel": "generic",
                        "api_key": settings.TERMII_API_KEY,
                    },
                )
            if resp.status_code == 200:
                logger.info("SMS sent via Termii to %s", phone[-4:].rjust(len(phone), "*"))
                return True
            logger.warning("Termii SMS failed with status %d", resp.status_code)
            return False
        except httpx.RequestError:
            logger.exception("Network error sending SMS via Termii")
            return False


class AfricasTalkingProvider(SMSProvider):
    """Africa's Talking API."""

    async def send_sms(self, phone: str, message: str) -> bool:
        if not settings.AT_API_KEY or not settings.AT_USERNAME:
            logger.warning("Africa's Talking not configured")
            return False
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.africastalking.com/version1/messaging",
                    headers={
                        "apiKey": settings.AT_API_KEY,
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                    },
                    data={
                        "username": settings.AT_USERNAME,
                        "to": phone,
                        "message": message,
                    },
                )
            if resp.status_code == 201:
                logger.info("SMS sent via AT to %s", phone[-4:].rjust(len(phone), "*"))
                return True
            logger.warning("AT SMS failed with status %d", resp.status_code)
            return False
        except httpx.RequestError:
            logger.exception("Network error sending SMS via Africa's Talking")
            return False


def _get_provider() -> Optional[SMSProvider]:
    """Select the first configured SMS provider."""
    if settings.TERMII_API_KEY:
        return TermiiProvider()
    if settings.AT_API_KEY:
        return AfricasTalkingProvider()
    return None


async def send_otp_sms(phone: str, code: str) -> bool:
    """
    Send an OTP code via SMS. Never logs the code.
    Returns True if sent, False otherwise.
    """
    provider = _get_provider()
    if not provider:
        logger.warning("No SMS provider configured — OTP not sent to %s", phone[-4:].rjust(len(phone), "*"))
        return False

    message = f"Your PharmaOS verification code is: {code}. Expires in 5 minutes. Do not share this code."
    return await provider.send_sms(phone, message)
