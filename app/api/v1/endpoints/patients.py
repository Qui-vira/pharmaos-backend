"""
PharmaOS AI - Patient & Reminder Endpoints
Patient registration, reminder scheduling and management.
"""

import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_roles, TokenData
from app.models.models import Organization, Patient, Reminder, ReminderType, ReminderStatus
from app.schemas.schemas import (
    PatientCreateRequest, PatientResponse, PatientSelfRegisterRequest,
    PatientUpdateRequest,
    ReminderCreateRequest, ReminderResponse, ReminderUpdateRequest,
)
from app.utils.helpers import paginate, sanitize_like
from app.middleware.audit import log_audit

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Patients & Reminders"])

# In-memory rate limiter for public self-registration: 10 per IP per hour
_self_reg_store: dict[str, list[float]] = defaultdict(list)
_SELF_REG_LIMIT = 10
_SELF_REG_WINDOW = 3600  # 1 hour


# ─── Patients ───────────────────────────────────────────────────────────────

@router.get("/patients", response_model=dict)
async def list_patients(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist", "cashier")),
    db: AsyncSession = Depends(get_db),
):
    """List patients for the current pharmacy."""
    query = select(Patient).where(Patient.org_id == current_user.org_id)

    if search:
        safe_search = sanitize_like(search)
        query = query.where(
            Patient.full_name.ilike(f"%{safe_search}%") | Patient.phone.ilike(f"%{safe_search}%")
        )

    query = query.order_by(Patient.full_name)

    result = await paginate(db, query, page, page_size)
    result["items"] = [PatientResponse.model_validate(p) for p in result["items"]]
    return result


@router.post("/patients", response_model=PatientResponse, status_code=status.HTTP_201_CREATED)
async def create_patient(
    payload: PatientCreateRequest,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist")),
    db: AsyncSession = Depends(get_db),
):
    """Register a new patient."""

    # One-phone-one-pharmacy rule: check if phone is registered at ANY pharmacy
    normalized_phone = payload.phone.strip().lstrip("+")
    with_plus = f"+{normalized_phone}"
    existing = await db.execute(
        select(Patient).where(
            Patient.phone.in_([payload.phone, normalized_phone, with_plus])
        ).limit(1)
    )
    existing_patient = existing.scalar_one_or_none()
    if existing_patient:
        if existing_patient.org_id == current_user.org_id:
            raise HTTPException(status_code=400, detail="Patient with this phone number already exists.")
        else:
            raise HTTPException(
                status_code=400,
                detail="This phone number is already registered at another pharmacy.",
            )

    patient = Patient(
        org_id=current_user.org_id,
        full_name=payload.full_name,
        phone=payload.phone,
        date_of_birth=payload.date_of_birth,
        gender=payload.gender,
        allergies=payload.allergies or [],
        chronic_conditions=payload.chronic_conditions or [],
        consent_given=payload.consent_given,
        consent_date=datetime.now(timezone.utc) if payload.consent_given else None,
    )
    db.add(patient)
    await db.flush()

    await log_audit(db, current_user.org_id, current_user.user_id, "create", "patient", patient.id)
    return PatientResponse.model_validate(patient)


@router.post("/patients/self-register", status_code=status.HTTP_201_CREATED)
async def self_register_patient(
    payload: PatientSelfRegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Public endpoint for patient self-registration (e.g. via QR code at pharmacy).
    No authentication required. Rate limited to 10 per IP per hour.
    """
    # Rate limit by IP
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not ip:
        ip = request.client.host if request.client else "unknown"

    now = time.time()
    _self_reg_store[ip] = [t for t in _self_reg_store[ip] if now - t < _SELF_REG_WINDOW]
    if len(_self_reg_store[ip]) >= _SELF_REG_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="Too many registration attempts. Please try again later.",
        )
    _self_reg_store[ip].append(now)

    # Validate org exists and is active
    org_result = await db.execute(
        select(Organization.id, Organization.is_active, Organization.whatsapp_phone_number_id).where(
            Organization.id == payload.org_id
        )
    )
    org = org_result.one_or_none()
    if not org or not org.is_active:
        raise HTTPException(status_code=400, detail="Invalid or inactive pharmacy.")

    # Check subscription status
    from app.core.subscription import check_subscription
    sub = await check_subscription(payload.org_id, db)
    if not sub["active"]:
        raise HTTPException(
            status_code=400,
            detail="This pharmacy's subscription is currently inactive. Please contact the pharmacy.",
        )

    # Validate phone format
    phone = payload.phone.strip()
    if not re.match(r"^\+?\d{10,15}$", phone):
        raise HTTPException(status_code=400, detail="Invalid phone number format.")

    # One-phone-one-pharmacy rule: check if phone is registered at ANY pharmacy
    normalized_phone = phone.strip().lstrip("+")
    with_plus = f"+{normalized_phone}"
    existing = await db.execute(
        select(Patient).where(
            Patient.phone.in_([phone, normalized_phone, with_plus])
        ).limit(1)
    )
    existing_patient = existing.scalar_one_or_none()
    if existing_patient:
        if existing_patient.org_id == payload.org_id:
            raise HTTPException(status_code=400, detail="You are already registered at this pharmacy.")
        else:
            raise HTTPException(
                status_code=400,
                detail="This phone number is already registered at another pharmacy. "
                       "Each phone number can only be linked to one pharmacy.",
            )

    patient = Patient(
        org_id=payload.org_id,
        full_name=payload.full_name.strip(),
        phone=phone,
        date_of_birth=payload.date_of_birth,
        gender=payload.gender,
        allergies=payload.allergies or [],
        chronic_conditions=payload.chronic_conditions or [],
        consent_given=True,
        consent_date=datetime.now(timezone.utc),
    )
    db.add(patient)
    await db.flush()

    logger.info("Patient self-registered at org %s", str(payload.org_id)[:8])

    response = {"message": "Registration successful", "patient_id": str(patient.id)}
    if org.whatsapp_phone_number_id:
        response["whatsapp_number"] = org.whatsapp_phone_number_id
    return response


@router.get("/patients/{patient_id}", response_model=PatientResponse)
async def get_patient(
    patient_id: UUID,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist", "cashier")),
    db: AsyncSession = Depends(get_db),
):
    """Get patient details."""
    result = await db.execute(
        select(Patient).where(Patient.id == patient_id, Patient.org_id == current_user.org_id)
    )
    patient = result.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found.")
    return PatientResponse.model_validate(patient)


@router.put("/patients/{patient_id}", response_model=PatientResponse)
async def update_patient(
    patient_id: UUID,
    payload: PatientUpdateRequest,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist")),
    db: AsyncSession = Depends(get_db),
):
    """Update patient information."""
    result = await db.execute(
        select(Patient).where(Patient.id == patient_id, Patient.org_id == current_user.org_id)
    )
    patient = result.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found.")

    update_data = payload.model_dump(exclude_unset=True)

    # Track consent changes
    if "consent_given" in update_data and update_data["consent_given"] and not patient.consent_given:
        update_data["consent_date"] = datetime.now(timezone.utc)

    for field, value in update_data.items():
        setattr(patient, field, value)

    await log_audit(db, current_user.org_id, current_user.user_id, "update", "patient", patient.id, update_data)
    await db.flush()
    return PatientResponse.model_validate(patient)


# ─── Reminders ──────────────────────────────────────────────────────────────

@router.get("/reminders/stats")
async def reminder_stats(
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist")),
    db: AsyncSession = Depends(get_db),
):
    """Get reminder statistics for the current pharmacy."""
    from sqlalchemy import func

    org_id = current_user.org_id

    # Total counts by status
    status_counts = {}
    for s in ReminderStatus:
        result = await db.execute(
            select(func.count(Reminder.id)).where(
                Reminder.org_id == org_id,
                Reminder.status == s,
            )
        )
        status_counts[s.value] = result.scalar() or 0

    # Total counts by type
    type_counts = {}
    for t in ReminderType:
        result = await db.execute(
            select(func.count(Reminder.id)).where(
                Reminder.org_id == org_id,
                Reminder.reminder_type == t,
            )
        )
        type_counts[t.value] = result.scalar() or 0

    # Due today (pending, scheduled_at <= end of today)
    from datetime import timedelta
    end_of_today = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59)
    due_today_result = await db.execute(
        select(func.count(Reminder.id)).where(
            Reminder.org_id == org_id,
            Reminder.status == ReminderStatus.pending,
            Reminder.scheduled_at <= end_of_today,
        )
    )
    due_today = due_today_result.scalar() or 0

    total = sum(status_counts.values())

    return {
        "total": total,
        "due_today": due_today,
        "by_status": status_counts,
        "by_type": type_counts,
    }


@router.get("/reminders", response_model=dict)
async def list_reminders(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    type_filter: Optional[str] = Query(None, alias="type"),
    patient_id: Optional[UUID] = Query(None),
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist")),
    db: AsyncSession = Depends(get_db),
):
    """List reminders for the current pharmacy with filtering."""
    query = select(Reminder).where(Reminder.org_id == current_user.org_id)

    if status_filter:
        query = query.where(Reminder.status == ReminderStatus(status_filter))

    if type_filter:
        query = query.where(Reminder.reminder_type == ReminderType(type_filter))

    if patient_id:
        query = query.where(Reminder.patient_id == patient_id)

    query = query.order_by(Reminder.scheduled_at.desc())

    result = await paginate(db, query, page, page_size)
    result["items"] = [ReminderResponse.model_validate(r) for r in result["items"]]
    return result


@router.post("/reminders", response_model=ReminderResponse, status_code=status.HTTP_201_CREATED)
async def create_reminder(
    payload: ReminderCreateRequest,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist")),
    db: AsyncSession = Depends(get_db),
):
    """Schedule a new patient reminder."""

    # Verify patient exists and has consent
    patient_result = await db.execute(
        select(Patient).where(Patient.id == payload.patient_id, Patient.org_id == current_user.org_id)
    )
    patient = patient_result.scalar_one_or_none()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found.")
    if not patient.consent_given:
        raise HTTPException(status_code=400, detail="Patient has not given consent for reminders.")

    reminder = Reminder(
        org_id=current_user.org_id,
        patient_id=payload.patient_id,
        reminder_type=ReminderType(payload.reminder_type),
        product_id=payload.product_id,
        scheduled_at=payload.scheduled_at,
        recurrence_rule=payload.recurrence_rule,
        message_template=payload.message_template,
    )
    db.add(reminder)
    await db.flush()

    await log_audit(db, current_user.org_id, current_user.user_id, "create", "reminder", reminder.id)
    return ReminderResponse.model_validate(reminder)


@router.put("/reminders/{reminder_id}", response_model=ReminderResponse)
async def update_reminder(
    reminder_id: UUID,
    payload: ReminderUpdateRequest,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist")),
    db: AsyncSession = Depends(get_db),
):
    """Update or cancel a reminder."""
    result = await db.execute(
        select(Reminder).where(Reminder.id == reminder_id, Reminder.org_id == current_user.org_id)
    )
    reminder = result.scalar_one_or_none()
    if not reminder:
        raise HTTPException(status_code=404, detail="Reminder not found.")

    update_data = payload.model_dump(exclude_unset=True)
    if "status" in update_data:
        update_data["status"] = ReminderStatus(update_data["status"])

    for field, value in update_data.items():
        setattr(reminder, field, value)

    await log_audit(db, current_user.org_id, current_user.user_id, "update", "reminder", reminder.id, update_data)
    await db.flush()
    return ReminderResponse.model_validate(reminder)
