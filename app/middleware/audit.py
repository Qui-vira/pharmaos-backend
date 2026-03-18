"""
PharmaOS AI - Middleware
Tenant filtering, audit logging, and request processing.
"""

import logging
from uuid import UUID
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AuditLog

logger = logging.getLogger("pharmaos.audit")

# Fields that must never appear in audit log changes
_SENSITIVE_FIELDS = {"password", "password_hash", "token", "access_token", "refresh_token", "secret"}


def _sanitize_changes(changes: dict | None) -> dict | None:
    """Remove sensitive fields from audit log data."""
    if not changes:
        return changes
    return {k: "***REDACTED***" if k in _SENSITIVE_FIELDS else v for k, v in changes.items()}


async def log_audit(
    db: AsyncSession,
    org_id: UUID,
    user_id: UUID,
    action: str,
    resource_type: str,
    resource_id: UUID = None,
    changes: dict = None,
    ip_address: str = None,
):
    """Write an audit log entry. Sensitive fields are automatically redacted."""
    sanitized = _sanitize_changes(changes)

    entry = AuditLog(
        org_id=org_id,
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        changes=sanitized,
        ip_address=ip_address,
    )
    db.add(entry)
    # Don't commit here — let the endpoint transaction handle it

    logger.info(
        "AUDIT org=%s user=%s action=%s resource=%s/%s",
        org_id, user_id, action, resource_type, resource_id,
    )


async def log_auth_event(
    db: AsyncSession,
    action: str,
    email: str,
    success: bool,
    ip_address: str = None,
    org_id: UUID = None,
    user_id: UUID = None,
):
    """Log authentication events (login, register, refresh)."""
    logger.info(
        "AUTH action=%s email=%s success=%s ip=%s",
        action, email, success, ip_address,
    )
    if org_id and user_id:
        entry = AuditLog(
            org_id=org_id,
            user_id=user_id,
            action=action,
            resource_type="auth",
            resource_id=None,
            changes={"email": email, "success": success},
            ip_address=ip_address,
        )
        db.add(entry)
