"""
PharmaOS AI - Application Configuration (v3)
Added: Paystack, Flutterwave, frontend URL settings.
All secrets MUST be provided via environment variables or .env file.
"""

import logging
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    APP_NAME: str = "PharmaOS AI"
    APP_VERSION: str = "3.0.0"
    DEBUG: bool = False
    API_PREFIX: str = "/api/v1"
    ALLOWED_ORIGINS: list[str] = ["http://localhost:3000", "https://pharmaos-frontend.vercel.app"]
    FRONTEND_URL: str = "https://pharmaos-frontend.vercel.app"  # v3: for payment callbacks

    # Database — MUST be set via environment variable
    DATABASE_URL: str = Field(default="postgresql+asyncpg://localhost/pharmaos")
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT — MUST be set via environment variable
    JWT_SECRET_KEY: str = Field(default="CHANGE-ME-set-a-strong-secret-in-env-vars-at-least-32-chars-long")
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # WhatsApp
    WHATSAPP_PHONE_NUMBER_ID: Optional[str] = None
    WHATSAPP_ACCESS_TOKEN: Optional[str] = None
    WHATSAPP_VERIFY_TOKEN: Optional[str] = None
    WHATSAPP_APP_SECRET: Optional[str] = None

    # Twilio
    TWILIO_ACCOUNT_SID: Optional[str] = None
    TWILIO_AUTH_TOKEN: Optional[str] = None
    TWILIO_PHONE_NUMBER: Optional[str] = None

    # AI / LLM
    LLM_API_KEY: Optional[str] = None
    LLM_MODEL: str = "gpt-4o"

    # v3: Paystack (primary payment provider)
    PAYSTACK_SECRET_KEY: Optional[str] = None
    PAYSTACK_PUBLIC_KEY: Optional[str] = None

    # v3: Flutterwave (fallback payment provider)
    FLUTTERWAVE_SECRET_KEY: Optional[str] = None
    FLUTTERWAVE_PUBLIC_KEY: Optional[str] = None
    FLUTTERWAVE_WEBHOOK_HASH: Optional[str] = None

    # File Storage
    S3_BUCKET: Optional[str] = None
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None
    AWS_REGION: str = "us-east-1"

    # SMTP (email verification)
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_FROM_EMAIL: Optional[str] = None

    # Google OAuth
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None

    # SMS Providers (phone OTP)
    TERMII_API_KEY: Optional[str] = None
    TERMII_SENDER_ID: str = "PharmaOS"
    AT_API_KEY: Optional[str] = None
    AT_USERNAME: Optional[str] = None

    # 2FA encryption key (for encrypting TOTP secrets at rest)
    TWO_FACTOR_ENCRYPTION_KEY: Optional[str] = None

    # Celery
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "case_sensitive": True}


settings = Settings()

# Startup warnings for missing optional service configs
_optional_services = {
    "WHATSAPP_PHONE_NUMBER_ID": "WhatsApp integration",
    "TWILIO_ACCOUNT_SID": "Twilio SMS",
    "LLM_API_KEY": "AI/LLM features",
    "PAYSTACK_SECRET_KEY": "Paystack payments",
    "FLUTTERWAVE_SECRET_KEY": "Flutterwave payments",
    "S3_BUCKET": "S3 file storage",
    "SMTP_HOST": "Email verification",
    "GOOGLE_CLIENT_ID": "Google OAuth",
    "TERMII_API_KEY": "Termii SMS OTP",
    "TWO_FACTOR_ENCRYPTION_KEY": "2FA TOTP encryption",
}
for var, service in _optional_services.items():
    if getattr(settings, var, None) is None:
        logger.info("Optional config %s not set — %s will be disabled.", var, service)

# Warn if using default (insecure) JWT secret
if "CHANGE-ME" in settings.JWT_SECRET_KEY:
    logger.warning("JWT_SECRET_KEY is using the default placeholder. Set a strong secret via environment variable!")

# Warn if using default database URL
if settings.DATABASE_URL == "postgresql+asyncpg://localhost/pharmaos":
    logger.warning("DATABASE_URL is using default. Set it via environment variable for production.")
