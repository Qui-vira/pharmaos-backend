"""
PharmaOS AI - Admin Endpoints
Super admin platform management.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_roles, TokenData
from app.models.models import Organization, User, Order, Sale, Consultation, AuditLog
from app.schemas.schemas import OrgResponse
from app.utils.helpers import paginate

router = APIRouter(prefix="/admin", tags=["Admin"])


@router.get("/organizations", response_model=dict)
async def list_all_organizations(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: TokenData = Depends(require_roles("super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all organizations on the platform."""
    query = select(Organization).order_by(Organization.created_at.desc())
    result = await paginate(db, query, page, page_size)
    result["items"] = [OrgResponse.model_validate(o) for o in result["items"]]
    return result


@router.get("/analytics", response_model=dict)
async def platform_analytics(
    current_user: TokenData = Depends(require_roles("super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Get platform-wide metrics."""
    org_count = await db.execute(select(func.count(Organization.id)))
    user_count = await db.execute(select(func.count(User.id)))
    order_count = await db.execute(select(func.count(Order.id)))
    sale_count = await db.execute(select(func.count(Sale.id)))
    consult_count = await db.execute(select(func.count(Consultation.id)))

    # Org breakdown by type
    org_breakdown = await db.execute(
        select(Organization.org_type, func.count(Organization.id))
        .group_by(Organization.org_type)
    )

    return {
        "total_organizations": org_count.scalar() or 0,
        "total_users": user_count.scalar() or 0,
        "total_orders": order_count.scalar() or 0,
        "total_sales": sale_count.scalar() or 0,
        "total_consultations": consult_count.scalar() or 0,
        "org_breakdown": {
            row[0].value: row[1] for row in org_breakdown.all()
        },
    }


@router.get("/audit-logs", response_model=dict)
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    action: str = Query(None),
    resource_type: str = Query(None),
    current_user: TokenData = Depends(require_roles("super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Browse platform audit logs."""
    query = select(AuditLog)

    if action:
        query = query.where(AuditLog.action == action)
    if resource_type:
        query = query.where(AuditLog.resource_type == resource_type)

    query = query.order_by(AuditLog.created_at.desc())

    result = await paginate(db, query, page, page_size)
    result["items"] = [
        {
            "id": str(log.id),
            "org_id": str(log.org_id),
            "user_id": str(log.user_id),
            "action": log.action,
            "resource_type": log.resource_type,
            "resource_id": str(log.resource_id) if log.resource_id else None,
            "changes": log.changes,
            "ip_address": log.ip_address,
            "created_at": log.created_at.isoformat(),
        }
        for log in result["items"]
    ]
    return result
