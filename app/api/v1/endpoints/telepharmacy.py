"""
PharmaOS AI - Telepharmacy Endpoints
Remote pharmacist consultations via video, voice, or chat.
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, TokenData
from app.models.models import TelepharmacySession, TelepharmacySessionType, TelepharmacyStatus, Patient
from app.schemas.schemas import (
    TelepharmacySessionCreate,
    TelepharmacySessionResponse,
    TelepharmacyStatusUpdate,
    TelepharmacyPrescriptionRequest,
    PaginatedResponse,
)
from app.middleware.audit import log_audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telepharmacy", tags=["Telepharmacy"])


@router.post("/sessions", response_model=TelepharmacySessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: TelepharmacySessionCreate,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new telepharmacy session."""
    patient = await db.execute(
        select(Patient).where(
            and_(Patient.id == payload.patient_id, Patient.org_id == current_user.org_id)
        )
    )
    if not patient.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Patient not found in your organization.")

    session = TelepharmacySession(
        org_id=current_user.org_id,
        patient_id=payload.patient_id,
        pharmacist_id=payload.pharmacist_id,
        session_type=TelepharmacySessionType(payload.session_type),
        status=TelepharmacyStatus.waiting,
        notes=payload.notes,
        consultation_id=payload.consultation_id,
    )
    db.add(session)
    await db.flush()

    await log_audit(db, current_user.org_id, current_user.user_id, "create", "telepharmacy_session", session.id)

    return TelepharmacySessionResponse.model_validate(session)


@router.get("/sessions", response_model=PaginatedResponse)
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: str = Query(None, alias="status"),
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List telepharmacy sessions for the current organization."""
    query = select(TelepharmacySession).where(TelepharmacySession.org_id == current_user.org_id)
    count_query = select(func.count(TelepharmacySession.id)).where(TelepharmacySession.org_id == current_user.org_id)

    if status_filter:
        query = query.where(TelepharmacySession.status == TelepharmacyStatus(status_filter))
        count_query = count_query.where(TelepharmacySession.status == TelepharmacyStatus(status_filter))

    total = (await db.execute(count_query)).scalar() or 0
    pages = max(1, (total + page_size - 1) // page_size)

    query = query.order_by(TelepharmacySession.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    sessions = result.scalars().all()

    return PaginatedResponse(
        items=[TelepharmacySessionResponse.model_validate(s) for s in sessions],
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@router.get("/sessions/{session_id}", response_model=TelepharmacySessionResponse)
async def get_session(
    session_id: UUID,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a specific telepharmacy session."""
    result = await db.execute(
        select(TelepharmacySession).where(
            and_(TelepharmacySession.id == session_id, TelepharmacySession.org_id == current_user.org_id)
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    return TelepharmacySessionResponse.model_validate(session)


@router.put("/sessions/{session_id}/status", response_model=TelepharmacySessionResponse)
async def update_session_status(
    session_id: UUID,
    payload: TelepharmacyStatusUpdate,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update telepharmacy session status with automatic timestamp management."""
    result = await db.execute(
        select(TelepharmacySession).where(
            and_(TelepharmacySession.id == session_id, TelepharmacySession.org_id == current_user.org_id)
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    new_status = TelepharmacyStatus(payload.status)

    valid_transitions = {
        TelepharmacyStatus.waiting: [TelepharmacyStatus.ringing, TelepharmacyStatus.cancelled],
        TelepharmacyStatus.ringing: [TelepharmacyStatus.active, TelepharmacyStatus.cancelled],
        TelepharmacyStatus.active: [TelepharmacyStatus.completed, TelepharmacyStatus.cancelled],
    }
    allowed = valid_transitions.get(session.status, [])
    if new_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition from '{session.status.value}' to '{new_status.value}'.",
        )

    now = datetime.now(timezone.utc)
    session.status = new_status

    if new_status == TelepharmacyStatus.active:
        session.started_at = now
    elif new_status in (TelepharmacyStatus.completed, TelepharmacyStatus.cancelled):
        session.ended_at = now
        if session.started_at:
            session.duration_seconds = int((now - session.started_at).total_seconds())

    session.updated_at = now
    await db.flush()

    await log_audit(
        db, current_user.org_id, current_user.user_id,
        "update_status", "telepharmacy_session", session.id,
        changes={"status": new_status.value},
    )

    return TelepharmacySessionResponse.model_validate(session)


@router.post("/sessions/{session_id}/prescription", response_model=TelepharmacySessionResponse)
async def enter_prescription(
    session_id: UUID,
    payload: TelepharmacyPrescriptionRequest,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Enter a prescription for a telepharmacy session."""
    result = await db.execute(
        select(TelepharmacySession).where(
            and_(TelepharmacySession.id == session_id, TelepharmacySession.org_id == current_user.org_id)
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    if session.status not in (TelepharmacyStatus.active, TelepharmacyStatus.completed):
        raise HTTPException(status_code=400, detail="Session must be active or completed to enter prescription.")

    session.prescription = {
        "diagnosis": payload.diagnosis,
        "drug_plan": [item.model_dump() for item in payload.drug_plan],
        "total_price": str(payload.total_price),
        "notes": payload.notes,
    }
    session.updated_at = datetime.now(timezone.utc)
    await db.flush()

    await log_audit(
        db, current_user.org_id, current_user.user_id,
        "enter_prescription", "telepharmacy_session", session.id,
    )

    return TelepharmacySessionResponse.model_validate(session)


@router.get("/sessions/{session_id}/recording")
async def get_recording(
    session_id: UUID,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get recording URL for a telepharmacy session."""
    result = await db.execute(
        select(TelepharmacySession).where(
            and_(TelepharmacySession.id == session_id, TelepharmacySession.org_id == current_user.org_id)
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    if not session.recording_url:
        raise HTTPException(status_code=404, detail="No recording available for this session.")

    return {"recording_url": session.recording_url}


@router.get("/stats")
async def get_stats(
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get telepharmacy statistics for the current organization."""
    org_id = current_user.org_id
    base = TelepharmacySession.org_id == org_id

    total = (await db.execute(select(func.count(TelepharmacySession.id)).where(base))).scalar() or 0

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today = (await db.execute(
        select(func.count(TelepharmacySession.id)).where(
            and_(base, TelepharmacySession.created_at >= today_start)
        )
    )).scalar() or 0

    avg_duration = (await db.execute(
        select(func.avg(TelepharmacySession.duration_seconds)).where(
            and_(base, TelepharmacySession.status == TelepharmacyStatus.completed)
        )
    )).scalar()

    active = (await db.execute(
        select(func.count(TelepharmacySession.id)).where(
            and_(base, TelepharmacySession.status == TelepharmacyStatus.active)
        )
    )).scalar() or 0

    return {
        "total_sessions": total,
        "sessions_today": today,
        "avg_duration_seconds": round(avg_duration) if avg_duration else 0,
        "active_sessions": active,
    }
