"""
PharmaOS AI - Middleware
Tenant filtering, audit logging, and request processing.
"""

from uuid import UUID
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AuditLog


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
    """Write an audit log entry."""
    entry = AuditLog(
        org_id=org_id,
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        changes=changes,
        ip_address=ip_address,
    )
    db.add(entry)
    # Don't commit here — let the endpoint transaction handle it
