"""
PharmaOS AI - TOTP 2FA Service
Generates and verifies TOTP codes for authenticator apps.
Encrypts secrets at rest using Fernet symmetric encryption.
"""

import base64
import hashlib
import logging

import pyotp
from cryptography.fernet import Fernet

from app.core.config import settings

logger = logging.getLogger(__name__)


def _get_fernet() -> Fernet:
    """
    Derive a Fernet key from TWO_FACTOR_ENCRYPTION_KEY.
    Fernet requires a 32-byte URL-safe base64-encoded key.
    We derive it from the configured key via SHA-256.
    """
    raw_key = settings.TWO_FACTOR_ENCRYPTION_KEY or "default-2fa-key-change-in-production"
    derived = hashlib.sha256(raw_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def generate_totp_secret() -> str:
    """Generate a new TOTP secret for authenticator apps."""
    return pyotp.random_base32()


def encrypt_secret(secret: str) -> str:
    """Encrypt a TOTP secret for database storage."""
    f = _get_fernet()
    return f.encrypt(secret.encode()).decode()


def decrypt_secret(encrypted: str) -> str:
    """Decrypt a TOTP secret from database storage."""
    f = _get_fernet()
    return f.decrypt(encrypted.encode()).decode()


def get_otpauth_uri(secret: str, email: str) -> str:
    """Generate an otpauth:// URI for authenticator app QR code scanning."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name="PharmaOS")


def verify_totp(secret: str, code: str) -> bool:
    """
    Verify a TOTP code. Allows 1-step window for clock drift.
    Never logs the code or secret.
    """
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)
