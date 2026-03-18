"""
PharmaOS AI - Subscription Enforcement
Checks org subscription status before allowing gated operations.

Subscription tiers stored in Organization.settings JSONB:
  - subscription_tier: "trial" | "basic" | "pro" | "enterprise"
  - subscription_expires_at: ISO 8601 datetime string or null (null = never expires)
  - trial_started_at: ISO 8601 datetime string
  - max_consultations_per_month: int (0 = unlimited)

Trial: 14 days from org creation (or trial_started_at).
Expired orgs cannot create new consultations but can view existing data.
"""

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, TokenData
from app.models.models import Consultation, Organization

logger = logging.getLogger(__name__)

TRIAL_DAYS = 14


async def check_subscription(
    org_id: UUID,
    db: AsyncSession,
) -> dict:
    """
    Check if an organization's subscription is active.
    Returns {"active": bool, "tier": str, "reason": str|None, "expires_at": str|None}.
    """
    result = await db.execute(
        select(Organization).where(Organization.id == org_id)
    )
    org = result.scalar_one_or_none()
    if not org:
        return {"active": False, "tier": "none", "reason": "Organization not found."}

    if not org.is_active:
        return {"active": False, "tier": "deactivated", "reason": "Organization has been deactivated."}

    settings = org.settings or {}
    tier = settings.get("subscription_tier", "trial")
    expires_at_str = settings.get("subscription_expires_at")

    now = datetime.now(timezone.utc)

    if tier == "trial":
        # Calculate trial expiry from trial_started_at or org creation
        trial_started = settings.get("trial_started_at")
        if trial_started:
            try:
                trial_start = datetime.fromisoformat(trial_started)
            except (ValueError, TypeError):
                trial_start = org.created_at
        else:
            trial_start = org.created_at

        trial_expires = trial_start + timedelta(days=TRIAL_DAYS)

        if now > trial_expires:
            return {
                "active": False,
                "tier": "trial",
                "reason": f"Trial period expired on {trial_expires.date().isoformat()}. Please upgrade.",
                "expires_at": trial_expires.isoformat(),
            }

        return {
            "active": True,
            "tier": "trial",
            "reason": None,
            "expires_at": trial_expires.isoformat(),
        }

    # Paid tiers
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            if now > expires_at:
                return {
                    "active": False,
                    "tier": tier,
                    "reason": f"Subscription expired on {expires_at.date().isoformat()}. Please renew.",
                    "expires_at": expires_at_str,
                }
        except (ValueError, TypeError):
            pass  # Invalid date — treat as active

    return {"active": True, "tier": tier, "reason": None, "expires_at": expires_at_str}


async def check_consultation_limit(org_id: UUID, db: AsyncSession) -> dict:
    """Check if the org has exceeded its monthly consultation limit."""
    result = await db.execute(
        select(Organization.settings).where(Organization.id == org_id)
    )
    row = result.one_or_none()
    settings = (row.settings if row else None) or {}

    max_per_month = settings.get("max_consultations_per_month", 0)
    if not max_per_month:
        return {"allowed": True, "used": 0, "limit": 0}

    # Count consultations created this calendar month
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    count_result = await db.execute(
        select(func.count(Consultation.id)).where(
            Consultation.org_id == org_id,
            Consultation.created_at >= month_start,
        )
    )
    used = count_result.scalar() or 0

    return {
        "allowed": used < max_per_month,
        "used": used,
        "limit": max_per_month,
    }


def require_active_subscription():
    """FastAPI dependency that blocks requests from expired orgs."""
    async def checker(
        current_user: TokenData = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> TokenData:
        # Super admins bypass subscription checks
        if current_user.role == "super_admin":
            return current_user

        sub = await check_subscription(current_user.org_id, db)
        if not sub["active"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=sub["reason"] or "Subscription expired.",
            )

        return current_user
    return checker
