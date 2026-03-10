"""
PharmaOS AI - Payment Service (v3)
Dual provider: Paystack (primary) + Flutterwave (fallback).
Abstraction layer allows swapping providers.

FLOW:
1. Pharmacist approves consultation → system creates Payment record
2. System initializes payment with Paystack → gets checkout URL
3. Checkout URL sent to customer via WhatsApp
4. Customer pays → Paystack webhook hits /webhooks/paystack
5. System verifies → updates Payment status → triggers:
   a. Order status → "paid"
   b. Consultation status → "paid"
   c. Inventory reservation (quantity_reserved += ordered_qty)
   d. Medication names disclosed to customer

HUMAN ACTION REQUIRED:
- Paystack: https://dashboard.paystack.com → Settings → API Keys
- Flutterwave: https://dashboard.flutterwave.com → Settings → API Keys
"""

import logging
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class PaymentProvider(ABC):
    """Abstract payment provider interface."""

    @abstractmethod
    async def initialize_payment(
        self,
        amount: Decimal,
        email: str,
        reference: str,
        callback_url: str,
        metadata: dict = None,
    ) -> dict:
        """Initialize a payment and return checkout URL."""
        pass

    @abstractmethod
    async def verify_payment(self, reference: str) -> dict:
        """Verify a payment by reference."""
        pass

    @abstractmethod
    async def refund_payment(self, transaction_id: str, amount: Decimal = None) -> dict:
        """Refund a payment."""
        pass


class PaystackProvider(PaymentProvider):
    """Paystack integration — primary provider."""

    BASE_URL = "https://api.paystack.co"

    def __init__(self, secret_key: str = None):
        self.secret_key = secret_key or settings.PAYSTACK_SECRET_KEY

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        }

    @property
    def is_configured(self) -> bool:
        return bool(self.secret_key)

    async def initialize_payment(
        self,
        amount: Decimal,
        email: str,
        reference: str,
        callback_url: str,
        metadata: dict = None,
    ) -> dict:
        """
        Initialize Paystack payment.
        Amount is in kobo (multiply by 100).
        Returns: {"status": "success", "authorization_url": "...", "reference": "..."}
        """
        if not self.is_configured:
            return {"status": "error", "message": "Paystack not configured"}

        payload = {
            "amount": int(amount * 100),  # Kobo
            "email": email,
            "reference": reference,
            "callback_url": callback_url,
            "currency": "NGN",
        }
        if metadata:
            payload["metadata"] = metadata

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.BASE_URL}/transaction/initialize",
                    json=payload,
                    headers=self._headers,
                )
                response.raise_for_status()
                data = response.json()

                if data.get("status"):
                    return {
                        "status": "success",
                        "provider": "paystack",
                        "authorization_url": data["data"]["authorization_url"],
                        "reference": data["data"]["reference"],
                        "access_code": data["data"]["access_code"],
                    }
                return {"status": "error", "message": data.get("message", "Unknown error")}
        except Exception as e:
            logger.error(f"Paystack initialize error: {e}")
            return {"status": "error", "message": str(e)}

    async def verify_payment(self, reference: str) -> dict:
        """Verify a Paystack transaction."""
        if not self.is_configured:
            return {"status": "error", "message": "Paystack not configured"}

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{self.BASE_URL}/transaction/verify/{reference}",
                    headers=self._headers,
                )
                response.raise_for_status()
                data = response.json()

                if data.get("status") and data["data"]["status"] == "success":
                    return {
                        "status": "successful",
                        "provider": "paystack",
                        "amount": Decimal(str(data["data"]["amount"])) / 100,
                        "reference": reference,
                        "transaction_id": str(data["data"]["id"]),
                        "paid_at": data["data"]["paid_at"],
                        "channel": data["data"]["channel"],
                    }
                return {
                    "status": "failed",
                    "provider": "paystack",
                    "message": data.get("data", {}).get("gateway_response", "Verification failed"),
                }
        except Exception as e:
            logger.error(f"Paystack verify error: {e}")
            return {"status": "error", "message": str(e)}

    async def refund_payment(self, transaction_id: str, amount: Decimal = None) -> dict:
        """Refund via Paystack."""
        payload = {"transaction": transaction_id}
        if amount:
            payload["amount"] = int(amount * 100)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.BASE_URL}/refund",
                    json=payload,
                    headers=self._headers,
                )
                response.raise_for_status()
                return {"status": "success", "provider": "paystack"}
        except Exception as e:
            logger.error(f"Paystack refund error: {e}")
            return {"status": "error", "message": str(e)}


class FlutterwaveProvider(PaymentProvider):
    """Flutterwave integration — fallback provider."""

    BASE_URL = "https://api.flutterwave.com/v3"

    def __init__(self, secret_key: str = None):
        self.secret_key = secret_key or settings.FLUTTERWAVE_SECRET_KEY

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        }

    @property
    def is_configured(self) -> bool:
        return bool(self.secret_key)

    async def initialize_payment(
        self,
        amount: Decimal,
        email: str,
        reference: str,
        callback_url: str,
        metadata: dict = None,
    ) -> dict:
        if not self.is_configured:
            return {"status": "error", "message": "Flutterwave not configured"}

        payload = {
            "tx_ref": reference,
            "amount": float(amount),
            "currency": "NGN",
            "redirect_url": callback_url,
            "customer": {"email": email},
            "customizations": {
                "title": "PharmaOS Payment",
                "description": "Pharmacy order payment",
            },
        }
        if metadata:
            payload["meta"] = metadata

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.BASE_URL}/payments",
                    json=payload,
                    headers=self._headers,
                )
                response.raise_for_status()
                data = response.json()

                if data.get("status") == "success":
                    return {
                        "status": "success",
                        "provider": "flutterwave",
                        "authorization_url": data["data"]["link"],
                        "reference": reference,
                    }
                return {"status": "error", "message": data.get("message", "Unknown error")}
        except Exception as e:
            logger.error(f"Flutterwave initialize error: {e}")
            return {"status": "error", "message": str(e)}

    async def verify_payment(self, reference: str) -> dict:
        """Verify Flutterwave transaction by tx_ref."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{self.BASE_URL}/transactions/verify_by_reference?tx_ref={reference}",
                    headers=self._headers,
                )
                response.raise_for_status()
                data = response.json()

                if data.get("status") == "success" and data["data"]["status"] == "successful":
                    return {
                        "status": "successful",
                        "provider": "flutterwave",
                        "amount": Decimal(str(data["data"]["amount"])),
                        "reference": reference,
                        "transaction_id": str(data["data"]["id"]),
                        "paid_at": data["data"]["created_at"],
                    }
                return {"status": "failed", "provider": "flutterwave", "message": "Verification failed"}
        except Exception as e:
            logger.error(f"Flutterwave verify error: {e}")
            return {"status": "error", "message": str(e)}

    async def refund_payment(self, transaction_id: str, amount: Decimal = None) -> dict:
        payload = {}
        if amount:
            payload["amount"] = float(amount)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.BASE_URL}/transactions/{transaction_id}/refund",
                    json=payload,
                    headers=self._headers,
                )
                response.raise_for_status()
                return {"status": "success", "provider": "flutterwave"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
#  PAYMENT SERVICE (facade over providers)
# ═══════════════════════════════════════════════════════════════════════════


class PaymentService:
    """
    Facade that tries Paystack first, falls back to Flutterwave.
    """

    def __init__(self):
        self.paystack = PaystackProvider()
        self.flutterwave = FlutterwaveProvider()

    @property
    def primary(self) -> PaymentProvider:
        if self.paystack.is_configured:
            return self.paystack
        if self.flutterwave.is_configured:
            return self.flutterwave
        return self.paystack  # Will return "not configured" errors

    @property
    def is_configured(self) -> bool:
        return self.paystack.is_configured or self.flutterwave.is_configured

    async def initialize_payment(
        self,
        amount: Decimal,
        email: str,
        reference: str,
        callback_url: str,
        metadata: dict = None,
        provider: str = None,
    ) -> dict:
        """Initialize payment. Uses specified provider or primary."""
        if provider == "flutterwave" and self.flutterwave.is_configured:
            return await self.flutterwave.initialize_payment(amount, email, reference, callback_url, metadata)

        # Try Paystack first
        if self.paystack.is_configured:
            result = await self.paystack.initialize_payment(amount, email, reference, callback_url, metadata)
            if result["status"] == "success":
                return result

        # Fallback to Flutterwave
        if self.flutterwave.is_configured:
            return await self.flutterwave.initialize_payment(amount, email, reference, callback_url, metadata)

        return {"status": "error", "message": "No payment provider configured"}

    async def verify_payment(self, reference: str, provider: str = "paystack") -> dict:
        """Verify payment with the correct provider."""
        if provider == "flutterwave":
            return await self.flutterwave.verify_payment(reference)
        return await self.paystack.verify_payment(reference)

    async def refund_payment(self, transaction_id: str, amount: Decimal = None, provider: str = "paystack") -> dict:
        if provider == "flutterwave":
            return await self.flutterwave.refund_payment(transaction_id, amount)
        return await self.paystack.refund_payment(transaction_id, amount)


# Singleton
payment_service = PaymentService()
