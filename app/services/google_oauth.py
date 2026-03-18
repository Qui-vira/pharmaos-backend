"""
PharmaOS AI - Google OAuth Service
Verifies Google id_tokens. Never logs tokens or secrets.
"""

import logging
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


async def verify_google_token(id_token: str) -> Optional[dict]:
    """
    Verify a Google id_token by calling Google's tokeninfo endpoint.
    Returns dict with {sub, email, name, picture, email_verified} or None.
    Never logs the id_token itself.
    """
    if not settings.GOOGLE_CLIENT_ID:
        logger.error("GOOGLE_CLIENT_ID not configured")
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"id_token": id_token},
            )

        if resp.status_code != 200:
            logger.warning("Google token verification failed with status %d", resp.status_code)
            return None

        data = resp.json()

        # Verify the token was issued for our app
        if data.get("aud") != settings.GOOGLE_CLIENT_ID:
            logger.warning("Google token audience mismatch")
            return None

        # Verify email is verified by Google
        if data.get("email_verified") not in ("true", True):
            logger.warning("Google email not verified for %s", data.get("email"))
            return None

        return {
            "sub": data["sub"],
            "email": data["email"],
            "name": data.get("name", ""),
            "picture": data.get("picture", ""),
            "email_verified": True,
        }
    except httpx.RequestError:
        logger.exception("Network error verifying Google token")
        return None
    except (KeyError, ValueError):
        logger.exception("Invalid response from Google tokeninfo")
        return None
