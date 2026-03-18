"""
PharmaOS AI - Admin Endpoints
Super admin platform management: pharmacies, consultations, revenue, subscriptions.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_roles, TokenData
from app.models.models import (
    Organization, OrgType, User, UserRole, Order, Sale, Consultation,
    ConsultationStatus, AuditLog, Patient, Reminder,
)
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


# ─── Pharmacy Management ──────────────────────────────────────────────────


@router.get("/organizations/{org_id}")
async def get_organization_detail(
    org_id: UUID,
    current_user: TokenData = Depends(require_roles("super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Get detailed info about a specific organization."""
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")

    # Get counts
    user_count = (await db.execute(
        select(func.count(User.id)).where(User.org_id == org_id)
    )).scalar() or 0

    patient_count = (await db.execute(
        select(func.count(Patient.id)).where(Patient.org_id == org_id)
    )).scalar() or 0

    consultation_count = (await db.execute(
        select(func.count(Consultation.id)).where(Consultation.org_id == org_id)
    )).scalar() or 0

    sale_count = (await db.execute(
        select(func.count(Sale.id)).where(Sale.org_id == org_id)
    )).scalar() or 0

    return {
        **OrgResponse.model_validate(org).model_dump(),
        "settings": org.settings or {},
        "user_count": user_count,
        "patient_count": patient_count,
        "consultation_count": consultation_count,
        "sale_count": sale_count,
    }


@router.put("/organizations/{org_id}/activate")
async def activate_organization(
    org_id: UUID,
    current_user: TokenData = Depends(require_roles("super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Activate an organization."""
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")

    org.is_active = True
    await db.flush()
    return {"message": f"Organization '{org.name}' activated.", "is_active": True}


@router.put("/organizations/{org_id}/deactivate")
async def deactivate_organization(
    org_id: UUID,
    current_user: TokenData = Depends(require_roles("super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate an organization (blocks logins and new consultations)."""
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")

    org.is_active = False
    await db.flush()
    return {"message": f"Organization '{org.name}' deactivated.", "is_active": False}


@router.put("/organizations/{org_id}/settings")
async def update_organization_settings(
    org_id: UUID,
    payload: dict,
    current_user: TokenData = Depends(require_roles("super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update organization settings (consultation_fee, subscription_tier, etc.)."""
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")

    current_settings = org.settings or {}
    current_settings.update(payload)
    org.settings = current_settings
    await db.flush()
    return {"message": "Settings updated.", "settings": org.settings}


# ─── Cross-Pharmacy Consultations ────────────────────────────────────────


@router.get("/consultations", response_model=dict)
async def list_all_consultations(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: str = Query(None, alias="status"),
    org_id: UUID = Query(None),
    current_user: TokenData = Depends(require_roles("super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """List consultations across all pharmacies (super admin view)."""
    query = select(Consultation)

    if status_filter:
        query = query.where(Consultation.status == ConsultationStatus(status_filter))
    if org_id:
        query = query.where(Consultation.org_id == org_id)

    query = query.order_by(Consultation.created_at.desc())

    result = await paginate(db, query, page, page_size)
    items = []
    for c in result["items"]:
        items.append({
            "id": str(c.id),
            "org_id": str(c.org_id),
            "patient_id": str(c.patient_id),
            "status": c.status.value,
            "symptom_summary": c.symptom_summary,
            "consultation_fee_paid": c.consultation_fee_paid,
            "channel": c.channel.value,
            "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat(),
        })
    result["items"] = items
    return result


# ─── Revenue Dashboard ───────────────────────────────────────────────────


@router.get("/revenue")
async def platform_revenue(
    days: int = Query(30, ge=1, le=365),
    current_user: TokenData = Depends(require_roles("super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Platform-wide revenue metrics for the given period."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Total sales revenue
    total_revenue_result = await db.execute(
        select(func.sum(Sale.total_amount)).where(Sale.sale_date >= cutoff)
    )
    total_revenue = float(total_revenue_result.scalar() or 0)

    # Sales count
    sales_count_result = await db.execute(
        select(func.count(Sale.id)).where(Sale.sale_date >= cutoff)
    )
    sales_count = sales_count_result.scalar() or 0

    # Revenue by org (top 10)
    revenue_by_org = await db.execute(
        select(
            Sale.org_id,
            Organization.name,
            func.sum(Sale.total_amount).label("revenue"),
            func.count(Sale.id).label("sale_count"),
        )
        .join(Organization, Organization.id == Sale.org_id)
        .where(Sale.sale_date >= cutoff)
        .group_by(Sale.org_id, Organization.name)
        .order_by(func.sum(Sale.total_amount).desc())
        .limit(10)
    )
    top_pharmacies = [
        {
            "org_id": str(row.org_id),
            "name": row.name,
            "revenue": float(row.revenue),
            "sale_count": row.sale_count,
        }
        for row in revenue_by_org.all()
    ]

    # Consultations completed in period
    consultations_completed = (await db.execute(
        select(func.count(Consultation.id)).where(
            Consultation.status == ConsultationStatus.completed,
            Consultation.updated_at >= cutoff,
        )
    )).scalar() or 0

    # New patients in period
    new_patients = (await db.execute(
        select(func.count(Patient.id)).where(Patient.created_at >= cutoff)
    )).scalar() or 0

    return {
        "period_days": days,
        "total_revenue": total_revenue,
        "total_sales": sales_count,
        "average_sale": round(total_revenue / sales_count, 2) if sales_count else 0,
        "consultations_completed": consultations_completed,
        "new_patients": new_patients,
        "top_pharmacies": top_pharmacies,
    }


# ─── Subscription Management ─────────────────────────────────────────────


@router.get("/subscriptions")
async def list_subscriptions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: TokenData = Depends(require_roles("super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all pharmacies with their subscription status."""
    query = (
        select(Organization)
        .where(Organization.org_type == OrgType.pharmacy)
        .order_by(Organization.created_at.desc())
    )

    result = await paginate(db, query, page, page_size)
    items = []
    for org in result["items"]:
        settings = org.settings or {}
        items.append({
            "org_id": str(org.id),
            "name": org.name,
            "is_active": org.is_active,
            "subscription_tier": settings.get("subscription_tier", "trial"),
            "subscription_expires_at": settings.get("subscription_expires_at"),
            "consultation_fee": settings.get("consultation_fee", 0),
            "created_at": org.created_at.isoformat(),
        })
    result["items"] = items
    return result


@router.put("/subscriptions/{org_id}")
async def update_subscription(
    org_id: UUID,
    payload: dict,
    current_user: TokenData = Depends(require_roles("super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update a pharmacy's subscription tier and expiry."""
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")

    current_settings = org.settings or {}

    allowed_fields = {"subscription_tier", "subscription_expires_at", "consultation_fee", "max_consultations_per_month"}
    for key, value in payload.items():
        if key in allowed_fields:
            current_settings[key] = value

    org.settings = current_settings
    await db.flush()
    return {"message": "Subscription updated.", "settings": {k: current_settings.get(k) for k in allowed_fields}}


# TODO: Remove this debug endpoint — for testing only
@router.post("/consultations/reset-patient/{phone}")
async def reset_patient_consultations(
    phone: str,
    current_user: TokenData = Depends(require_roles("super_admin", "pharmacy_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Cancel all active consultations for a phone number so a fresh test can start."""
    # Find patient by phone (handle +/no-+ mismatch)
    normalized = phone.strip().lstrip("+")
    with_plus = f"+{normalized}"
    result = await db.execute(
        select(Patient).where(Patient.phone.in_([phone, normalized, with_plus])).limit(1)
    )
    patient = result.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail=f"No patient found for phone {phone}")

    active_statuses = [
        ConsultationStatus.intake,
        ConsultationStatus.ai_processing,
        ConsultationStatus.awaiting_payment,
        ConsultationStatus.pending_review,
        ConsultationStatus.pharmacist_reviewing,
        ConsultationStatus.approved,
    ]
    consults = await db.execute(
        select(Consultation).where(
            Consultation.patient_id == patient.id,
            Consultation.status.in_(active_statuses),
        )
    )
    cancelled = 0
    for c in consults.scalars().all():
        c.status = ConsultationStatus.cancelled
        cancelled += 1

    await db.flush()
    return {
        "message": f"Cancelled {cancelled} active consultation(s) for {phone}",
        "patient_id": str(patient.id),
        "patient_name": patient.full_name,
    }
