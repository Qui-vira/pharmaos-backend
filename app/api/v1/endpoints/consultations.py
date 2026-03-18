"""
PharmaOS AI - Consultation Endpoints
Consultation listing, detail, pharmacist actions, and approval gate.
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_roles, TokenData
from app.core.subscription import require_active_subscription
from app.models.models import (
    Consultation, ConsultationMessage, PharmacistAction,
    ConsultationStatus, MessageSender, Notification, User,
)
from app.schemas.schemas import (
    ConsultationResponse, PharmacistActionRequest, PharmacistActionResponse,
)
from app.utils.helpers import paginate
from app.middleware.audit import log_audit

router = APIRouter(prefix="/consultations", tags=["Consultations"])


@router.get("", response_model=dict)
async def list_consultations(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: str = Query(None, alias="status"),
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist")),
    db: AsyncSession = Depends(get_db),
):
    """List consultations for the current pharmacy, filterable by status.
    Excludes awaiting_payment consultations (fee not yet paid) unless explicitly filtered."""
    query = select(Consultation).where(Consultation.org_id == current_user.org_id)

    if status_filter:
        query = query.where(Consultation.status == ConsultationStatus(status_filter))
    else:
        # Hide unpaid consultations from the default view
        query = query.where(Consultation.status != ConsultationStatus.awaiting_payment)

    query = query.order_by(Consultation.created_at.desc())

    result = await paginate(db, query, page, page_size)
    result["items"] = [ConsultationResponse.model_validate(c) for c in result["items"]]
    return result


@router.get("/{consultation_id}", response_model=ConsultationResponse)
async def get_consultation(
    consultation_id: UUID,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist")),
    db: AsyncSession = Depends(get_db),
):
    """Get full consultation detail including messages and pharmacist action."""
    result = await db.execute(
        select(Consultation).where(
            Consultation.id == consultation_id,
            Consultation.org_id == current_user.org_id,
        )
    )
    consultation = result.scalar_one_or_none()
    if not consultation:
        raise HTTPException(status_code=404, detail="Consultation not found.")

    # If pharmacist is viewing, assign them if not yet assigned
    if (
        current_user.role == "pharmacist"
        and consultation.status == ConsultationStatus.pending_review
        and consultation.assigned_pharmacist_id is None
    ):
        consultation.assigned_pharmacist_id = current_user.user_id
        consultation.status = ConsultationStatus.pharmacist_reviewing
        await db.flush()

    return ConsultationResponse.model_validate(consultation)


@router.post("/{consultation_id}/action", response_model=PharmacistActionResponse, status_code=status.HTTP_201_CREATED)
async def submit_pharmacist_action(
    consultation_id: UUID,
    payload: PharmacistActionRequest,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist")),
    db: AsyncSession = Depends(get_db),
):
    """
    Pharmacist submits diagnosis and drug plan.
    This does NOT yet communicate anything to the customer.
    Approval is a separate step.
    """
    result = await db.execute(
        select(Consultation).where(
            Consultation.id == consultation_id,
            Consultation.org_id == current_user.org_id,
        )
    )
    consultation = result.scalar_one_or_none()
    if not consultation:
        raise HTTPException(status_code=404, detail="Consultation not found.")

    if consultation.status not in (
        ConsultationStatus.pending_review,
        ConsultationStatus.pharmacist_reviewing,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Consultation in '{consultation.status.value}' status cannot receive pharmacist actions.",
        )

    # Check if action already exists
    existing = await db.execute(
        select(PharmacistAction).where(PharmacistAction.consultation_id == consultation_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Pharmacist action already submitted. Use approval endpoint.")

    action = PharmacistAction(
        consultation_id=consultation_id,
        pharmacist_id=current_user.user_id,
        diagnosis=payload.diagnosis,
        drug_plan=payload.drug_plan,
        total_price=payload.total_price,
        notes=payload.notes,
        is_approved=False,
    )
    db.add(action)

    # Update consultation
    consultation.assigned_pharmacist_id = current_user.user_id
    consultation.status = ConsultationStatus.pharmacist_reviewing

    await log_audit(
        db, current_user.org_id, current_user.user_id, "create", "pharmacist_action", action.id,
    )
    await db.flush()
    return PharmacistActionResponse.model_validate(action)


@router.post("/{consultation_id}/messages", response_model=ConsultationResponse)
async def send_pharmacist_message(
    consultation_id: UUID,
    payload: dict,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist")),
    db: AsyncSession = Depends(get_db),
):
    """Pharmacist sends a direct message in the consultation chat."""
    message_text = (payload.get("message") or "").strip()
    if not message_text:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    result = await db.execute(
        select(Consultation).where(
            Consultation.id == consultation_id,
            Consultation.org_id == current_user.org_id,
        )
    )
    consultation = result.scalar_one_or_none()
    if not consultation:
        raise HTTPException(status_code=404, detail="Consultation not found.")

    if consultation.status in (ConsultationStatus.completed, ConsultationStatus.cancelled):
        raise HTTPException(status_code=400, detail="Cannot send messages to a closed consultation.")

    msg = ConsultationMessage(
        consultation_id=consultation_id,
        sender_type=MessageSender.pharmacist,
        message=message_text,
    )
    db.add(msg)

    # Auto-assign pharmacist if not yet assigned
    if not consultation.assigned_pharmacist_id:
        consultation.assigned_pharmacist_id = current_user.user_id

    await db.flush()

    # Forward pharmacist message to patient on WhatsApp
    try:
        from app.services.consultation_flow import send_pharmacist_reply_to_whatsapp
        await send_pharmacist_reply_to_whatsapp(db, consultation, message_text)
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "Failed to forward pharmacist message to WhatsApp for consultation=%s", consultation_id
        )

    return ConsultationResponse.model_validate(consultation)


@router.post("/{consultation_id}/approve", response_model=ConsultationResponse)
async def approve_consultation(
    consultation_id: UUID,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist")),
    db: AsyncSession = Depends(get_db),
):
    """
    APPROVAL GATE: Pharmacist approves the consultation.
    Only after this step is the customer notified with the pharmacist's response.
    
    This is the critical safety gate — AI cannot bypass this.
    """
    result = await db.execute(
        select(Consultation).where(
            Consultation.id == consultation_id,
            Consultation.org_id == current_user.org_id,
        )
    )
    consultation = result.scalar_one_or_none()
    if not consultation:
        raise HTTPException(status_code=404, detail="Consultation not found.")

    if consultation.status != ConsultationStatus.pharmacist_reviewing:
        raise HTTPException(
            status_code=400,
            detail="Consultation must be in 'pharmacist_reviewing' status to approve.",
        )

    # Verify pharmacist action exists
    action_result = await db.execute(
        select(PharmacistAction).where(PharmacistAction.consultation_id == consultation_id)
    )
    action = action_result.scalar_one_or_none()
    if not action:
        raise HTTPException(
            status_code=400,
            detail="Must submit diagnosis and drug plan before approving.",
        )

    # Approve
    action.is_approved = True
    action.approved_at = datetime.now(timezone.utc)
    consultation.status = ConsultationStatus.approved

    # Add the pharmacist-approved message to the dashboard conversation history.
    # NOTE: Drug names are shown ONLY in the dashboard, never sent to patient via WhatsApp.
    drug_lines = []
    for drug in action.drug_plan:
        name = drug.get("drug_name") or drug.get("product_name", "Medication")
        dosage = drug.get("dosage", "")
        instructions = drug.get("instructions", "")
        drug_lines.append(f"- {name} {dosage}: {instructions}")

    newline = "\n"
    response_message = (
        f"Prescription approved.\n\n"
        f"{newline.join(drug_lines)}\n\n"
        f"Total: \u20A6{action.total_price:,.2f}\n\n"
        f"Sent to patient (price only, no drug names)."
    )

    msg = ConsultationMessage(
        consultation_id=consultation_id,
        sender_type=MessageSender.pharmacist,
        message=response_message,
    )
    db.add(msg)

    await log_audit(
        db, current_user.org_id, current_user.user_id, "approve", "consultation", consultation.id,
    )
    await db.flush()

    # Send prescription price to patient via WhatsApp (no drug names — compliance)
    try:
        from app.services.consultation_flow import send_prescription_to_patient
        await send_prescription_to_patient(db, consultation)
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "Failed to send prescription to WhatsApp for consultation=%s", consultation_id
        )

    return ConsultationResponse.model_validate(consultation)
