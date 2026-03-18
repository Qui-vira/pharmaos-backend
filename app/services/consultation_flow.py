"""
PharmaOS AI - Consultation Flow State Machine
Drives the WhatsApp intake/prescription/delivery flow.

States:
  intake          → AI gathers symptoms via structured questions
  ai_processing   → AI producing summary (transitional)
  pending_review  → Summary ready, waiting for pharmacist
  pharmacist_reviewing → Pharmacist is working on it
  approved        → Prescription sent to patient, awaiting PICKUP/DELIVERY/PAY
  completed       → Patient chose delivery method, done
  cancelled       → Abandoned

COMPLIANCE:
  - Bot NEVER sends drug names to the patient. Only total price.
  - Structured button flows, not open-ended AI conversation.
  - Human-in-the-loop: pharmacist must approve every prescription.
"""

import json
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    Consultation, ConsultationMessage, ConsultationStatus,
    MessageSender, Patient, Organization,
)
from app.services.whatsapp import whatsapp_service
from app.services.ai_provider import process_consultation_message

logger = logging.getLogger(__name__)

# ── Intake step definitions ──────────────────────────────────────────────────
# We track the current intake step in consultation.ai_questions_asked JSONB
# as {"intake_step": "symptoms", ...collected_data...}

INTAKE_STEPS = [
    "greeting",
    "symptoms",
    "duration",
    "medications_ask",
    "medications_detail",
    "allergies_ask",
    "allergies_detail",
    "summary",
]


def _get_intake_state(consultation: Consultation) -> dict:
    """Read the intake tracking dict from the JSONB field."""
    data = consultation.ai_questions_asked
    if isinstance(data, dict) and "intake_step" in data:
        return data
    return {"intake_step": "greeting"}


def _set_intake_state(consultation: Consultation, state: dict):
    """Write the intake tracking dict back."""
    consultation.ai_questions_asked = state


def _normalize_phone_for_wa(phone: str) -> str:
    """Ensure phone is in WhatsApp format (digits only, no +)."""
    return phone.strip().lstrip("+")


async def _get_org_name(db: AsyncSession, org_id) -> str:
    """Get the org name for greeting messages."""
    result = await db.execute(
        select(Organization.name).where(Organization.id == org_id)
    )
    row = result.one_or_none()
    return row.name if row else "your pharmacy"


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API — called from webhooks.py and consultations.py
# ═══════════════════════════════════════════════════════════════════════════════


async def handle_new_consultation(
    db: AsyncSession,
    patient: Patient,
    consultation: Consultation,
    initial_message: str,
):
    """
    Called right after a new consultation is created and the first
    customer message is recorded. Sends the greeting + first question.
    """
    wa_phone = _normalize_phone_for_wa(patient.phone)
    org_name = await _get_org_name(db, patient.org_id)

    greeting = (
        f"Welcome to {org_name}! I'll help you with a quick consultation.\n\n"
        f"What symptoms are you experiencing?"
    )

    # Send greeting
    result = await whatsapp_service.send_text(wa_phone, greeting)
    logger.info("Sent intake greeting to %s: %s", wa_phone, result.get("status"))

    # Record bot message
    bot_msg = ConsultationMessage(
        consultation_id=consultation.id,
        sender_type=MessageSender.ai,
        message=greeting,
    )
    db.add(bot_msg)

    # Set intake state to waiting for symptoms
    _set_intake_state(consultation, {"intake_step": "symptoms"})
    await db.flush()


async def handle_intake_message(
    db: AsyncSession,
    patient: Patient,
    consultation: Consultation,
    message: str,
    button_id: Optional[str] = None,
):
    """
    Process a patient message during the intake phase.
    Routes through the structured intake steps.
    """
    wa_phone = _normalize_phone_for_wa(patient.phone)
    state = _get_intake_state(consultation)
    step = state.get("intake_step", "greeting")

    logger.info("Intake step=%s for consultation=%s message=%r button_id=%s",
                step, consultation.id, message[:100], button_id)

    if step == "greeting":
        # Shouldn't normally reach here (handle_new_consultation sends greeting)
        # but handle gracefully
        await handle_new_consultation(db, patient, consultation, message)
        return

    elif step == "symptoms":
        # Patient just described symptoms — store them
        state["symptoms"] = message
        state["intake_step"] = "duration"
        _set_intake_state(consultation, state)

        # Ask duration with buttons
        reply = "How long have you had these symptoms?"
        await whatsapp_service.send_button_message(
            to=wa_phone,
            body=reply,
            buttons=[
                {"id": "dur_less3", "title": "Less than 3 days"},
                {"id": "dur_3to7", "title": "3-7 days"},
                {"id": "dur_more7", "title": "More than a week"},
            ],
        )
        _record_bot_msg(db, consultation.id, reply)

    elif step == "duration":
        # Patient chose duration
        duration_map = {
            "dur_less3": "Less than 3 days",
            "dur_3to7": "3-7 days",
            "dur_more7": "More than a week",
        }
        state["duration"] = duration_map.get(button_id, message)
        state["intake_step"] = "medications_ask"
        _set_intake_state(consultation, state)

        reply = "Are you currently taking any medication?"
        await whatsapp_service.send_button_message(
            to=wa_phone,
            body=reply,
            buttons=[
                {"id": "meds_yes", "title": "Yes"},
                {"id": "meds_no", "title": "No"},
            ],
        )
        _record_bot_msg(db, consultation.id, reply)

    elif step == "medications_ask":
        if button_id == "meds_no" or message.strip().lower() == "no":
            state["current_medications"] = "None"
            state["intake_step"] = "allergies_ask"
            _set_intake_state(consultation, state)

            reply = "Do you have any drug allergies?"
            await whatsapp_service.send_button_message(
                to=wa_phone,
                body=reply,
                buttons=[
                    {"id": "allergy_yes", "title": "Yes"},
                    {"id": "allergy_no", "title": "No"},
                ],
            )
            _record_bot_msg(db, consultation.id, reply)
        else:
            # They said yes or tapped Yes button — ask for details
            state["intake_step"] = "medications_detail"
            _set_intake_state(consultation, state)

            reply = "Please list the medications you are currently taking."
            await whatsapp_service.send_text(wa_phone, reply)
            _record_bot_msg(db, consultation.id, reply)

    elif step == "medications_detail":
        state["current_medications"] = message
        state["intake_step"] = "allergies_ask"
        _set_intake_state(consultation, state)

        reply = "Do you have any drug allergies?"
        await whatsapp_service.send_button_message(
            to=wa_phone,
            body=reply,
            buttons=[
                {"id": "allergy_yes", "title": "Yes"},
                {"id": "allergy_no", "title": "No"},
            ],
        )
        _record_bot_msg(db, consultation.id, reply)

    elif step == "allergies_ask":
        if button_id == "allergy_no" or message.strip().lower() == "no":
            state["allergies"] = "None"
            await _finish_intake(db, consultation, patient, state, wa_phone)
        else:
            state["intake_step"] = "allergies_detail"
            _set_intake_state(consultation, state)

            reply = "Please list your allergies."
            await whatsapp_service.send_text(wa_phone, reply)
            _record_bot_msg(db, consultation.id, reply)

    elif step == "allergies_detail":
        state["allergies"] = message
        await _finish_intake(db, consultation, patient, state, wa_phone)

    else:
        # Unknown step — try AI fallback
        logger.warning("Unknown intake step %s for consultation=%s", step, consultation.id)
        await _ai_fallback(db, consultation, patient, message, wa_phone)

    await db.flush()


async def handle_approved_message(
    db: AsyncSession,
    patient: Patient,
    consultation: Consultation,
    message: str,
    button_id: Optional[str] = None,
):
    """
    Handle patient messages when consultation is in 'approved' state.
    Patient should be choosing Pay Now, Ask Question, Pickup, or Delivery.
    """
    wa_phone = _normalize_phone_for_wa(patient.phone)
    state = _get_intake_state(consultation)
    sub_state = state.get("post_approval_step", "price_sent")

    logger.info("Approved sub_state=%s consultation=%s button_id=%s message=%r",
                sub_state, consultation.id, button_id, message[:100])

    if button_id == "pay_now" or sub_state == "price_sent" and button_id == "pay_now":
        # Send payment link
        # TODO: Replace with actual Paystack/Flutterwave payment link generation
        payment_url = "https://paystack.com/pay/pharmaos-placeholder"
        reply = f"Pay here: {payment_url}"
        await whatsapp_service.send_text(wa_phone, reply)
        _record_bot_msg(db, consultation.id, reply)

        state["post_approval_step"] = "payment_pending"
        _set_intake_state(consultation, state)

    elif button_id == "ask_question":
        reply = "Please type your question and a pharmacist will respond shortly."
        await whatsapp_service.send_text(wa_phone, reply)
        _record_bot_msg(db, consultation.id, reply)
        # Stay in approved state — pharmacist will reply from dashboard
        # The dashboard message endpoint already handles sending messages

    elif button_id == "pickup" or (message.strip().lower() == "pickup"):
        org_name = await _get_org_name(db, patient.org_id)
        reply = (
            f"Your order is confirmed for pickup. "
            f"Please visit {org_name} with your phone for verification. Thank you!"
        )
        await whatsapp_service.send_text(wa_phone, reply)
        _record_bot_msg(db, consultation.id, reply)

        consultation.status = ConsultationStatus.completed
        logger.info("Consultation %s completed via pickup", consultation.id)

    elif button_id == "delivery" or (message.strip().lower() == "delivery"):
        reply = "Please share your delivery address."
        await whatsapp_service.send_text(wa_phone, reply)
        _record_bot_msg(db, consultation.id, reply)

        state["post_approval_step"] = "awaiting_address"
        _set_intake_state(consultation, state)

    elif sub_state == "awaiting_address":
        # Patient sent their delivery address
        state["delivery_address"] = message
        _set_intake_state(consultation, state)

        reply = (
            f"Your order is confirmed for delivery to:\n{message}\n\n"
            f"You will be contacted when it is on the way. Thank you!"
        )
        await whatsapp_service.send_text(wa_phone, reply)
        _record_bot_msg(db, consultation.id, reply)

        consultation.status = ConsultationStatus.completed
        logger.info("Consultation %s completed via delivery to %s", consultation.id, message[:50])

    elif sub_state == "payment_pending":
        # After payment, send delivery selection
        # For now, assume manual payment confirmation or auto-proceed
        # Send delivery selection buttons
        await _send_delivery_selection(wa_phone, consultation, db)
        state["post_approval_step"] = "delivery_selection"
        _set_intake_state(consultation, state)

    else:
        # Default: patient is asking a question or sending a message
        # Record it and let the pharmacist reply from dashboard
        logger.info("Patient message in approved state (no button match), recording for pharmacist")

    await db.flush()


async def send_prescription_to_patient(
    db: AsyncSession,
    consultation: Consultation,
):
    """
    Called after pharmacist approves. Sends ONLY the total price to the patient
    via WhatsApp with Pay Now / Ask Question buttons.

    COMPLIANCE: Does NOT send drug names.
    """
    # Load patient
    patient_result = await db.execute(
        select(Patient).where(Patient.id == consultation.patient_id)
    )
    patient = patient_result.scalar_one_or_none()
    if not patient:
        logger.error("Patient not found for consultation %s", consultation.id)
        return

    # Load pharmacist action
    from app.models.models import PharmacistAction
    action_result = await db.execute(
        select(PharmacistAction).where(
            PharmacistAction.consultation_id == consultation.id,
            PharmacistAction.is_approved == True,
        )
    )
    action = action_result.scalar_one_or_none()
    if not action:
        logger.error("No approved pharmacist action for consultation %s", consultation.id)
        return

    wa_phone = _normalize_phone_for_wa(patient.phone)
    total = float(action.total_price)

    # Send ONLY the total price — NO drug names (compliance)
    body = (
        f"Your prescription is ready.\n"
        f"Total: \u20A6{total:,.2f}\n\n"
        f"Tap below to proceed."
    )
    result = await whatsapp_service.send_button_message(
        to=wa_phone,
        body=body,
        buttons=[
            {"id": "pay_now", "title": "Pay Now"},
            {"id": "ask_question", "title": "Ask a Question"},
        ],
    )
    logger.info("Sent prescription price to %s: %s", wa_phone, result.get("status"))

    # Record in conversation
    _record_bot_msg(db, consultation.id, body)

    # Track post-approval state
    state = _get_intake_state(consultation)
    state["post_approval_step"] = "price_sent"
    _set_intake_state(consultation, state)
    await db.flush()


async def send_pharmacist_reply_to_whatsapp(
    db: AsyncSession,
    consultation: Consultation,
    message_text: str,
):
    """
    When a pharmacist sends a message from the dashboard,
    forward it to the patient on WhatsApp.
    """
    patient_result = await db.execute(
        select(Patient).where(Patient.id == consultation.patient_id)
    )
    patient = patient_result.scalar_one_or_none()
    if not patient:
        logger.error("Patient not found for consultation %s", consultation.id)
        return

    wa_phone = _normalize_phone_for_wa(patient.phone)
    result = await whatsapp_service.send_text(wa_phone, message_text)
    logger.info("Forwarded pharmacist message to %s: %s", wa_phone, result.get("status"))


async def send_payment_confirmed_delivery_selection(
    db: AsyncSession,
    consultation: Consultation,
):
    """Called when payment is confirmed. Sends delivery selection buttons."""
    patient_result = await db.execute(
        select(Patient).where(Patient.id == consultation.patient_id)
    )
    patient = patient_result.scalar_one_or_none()
    if not patient:
        return

    wa_phone = _normalize_phone_for_wa(patient.phone)
    await _send_delivery_selection(wa_phone, consultation, db)


# ═══════════════════════════════════════════════════════════════════════════════
#  INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


async def _finish_intake(
    db: AsyncSession,
    consultation: Consultation,
    patient: Patient,
    state: dict,
    wa_phone: str,
):
    """Finish the intake flow: generate summary, notify patient, push to pharmacist."""
    # Build summary from collected data
    symptoms = state.get("symptoms", "Not provided")
    duration = state.get("duration", "Not provided")
    medications = state.get("current_medications", "None")
    allergies = state.get("allergies", "None")

    summary = (
        f"Patient: {patient.full_name}\n"
        f"Symptoms: {symptoms}\n"
        f"Duration: {duration}\n"
        f"Current Medications: {medications}\n"
        f"Allergies: {allergies}"
    )

    # Try AI-enhanced summary if available
    try:
        # Build conversation history for AI
        msgs_result = await db.execute(
            select(ConsultationMessage)
            .where(ConsultationMessage.consultation_id == consultation.id)
            .order_by(ConsultationMessage.sent_at)
        )
        messages = msgs_result.scalars().all()

        conversation = []
        for m in messages:
            role = "user" if m.sender_type == MessageSender.customer else "assistant"
            conversation.append({"role": role, "content": m.message})

        ai_result = await process_consultation_message(conversation)
        if ai_result.get("type") == "summary":
            summary = ai_result["content"]
            logger.info("AI generated summary for consultation %s", consultation.id)
    except Exception:
        logger.exception("AI summary generation failed, using structured summary")

    # Store summary
    consultation.symptom_summary = summary
    state["intake_step"] = "summary"
    _set_intake_state(consultation, state)

    # Transition to pending_review
    consultation.status = ConsultationStatus.pending_review

    # Tell the patient
    reply = (
        "Thank you! A pharmacist is now reviewing your case. "
        "You will receive a response shortly."
    )
    await whatsapp_service.send_text(wa_phone, reply)
    _record_bot_msg(db, consultation.id, reply)

    logger.info("Intake complete for consultation=%s, now pending_review. Summary: %s",
                consultation.id, summary[:100])


async def _ai_fallback(
    db: AsyncSession,
    consultation: Consultation,
    patient: Patient,
    message: str,
    wa_phone: str,
):
    """Use AI to generate a contextual follow-up when we're in an unknown state."""
    try:
        msgs_result = await db.execute(
            select(ConsultationMessage)
            .where(ConsultationMessage.consultation_id == consultation.id)
            .order_by(ConsultationMessage.sent_at)
        )
        messages = msgs_result.scalars().all()

        conversation = []
        for m in messages:
            role = "user" if m.sender_type == MessageSender.customer else "assistant"
            conversation.append({"role": role, "content": m.message})

        ai_result = await process_consultation_message(conversation)

        if ai_result.get("type") == "summary":
            # AI says we have enough info — finish intake
            consultation.symptom_summary = ai_result["content"]
            consultation.status = ConsultationStatus.pending_review

            reply = (
                "Thank you! A pharmacist is now reviewing your case. "
                "You will receive a response shortly."
            )
            await whatsapp_service.send_text(wa_phone, reply)
            _record_bot_msg(db, consultation.id, reply)
        else:
            # Send AI's follow-up question
            reply = ai_result.get("content", "Could you tell me more about your symptoms?")
            await whatsapp_service.send_text(wa_phone, reply)
            _record_bot_msg(db, consultation.id, reply)

    except Exception:
        logger.exception("AI fallback failed for consultation %s", consultation.id)
        reply = "Could you please describe your symptoms in more detail?"
        await whatsapp_service.send_text(wa_phone, reply)
        _record_bot_msg(db, consultation.id, reply)


async def _send_delivery_selection(wa_phone: str, consultation: Consultation, db: AsyncSession):
    """Send delivery method selection buttons."""
    body = "How would you like to receive your medication?"
    await whatsapp_service.send_button_message(
        to=wa_phone,
        body=body,
        buttons=[
            {"id": "pickup", "title": "Pickup at Store"},
            {"id": "delivery", "title": "Home Delivery"},
        ],
    )
    _record_bot_msg(db, consultation.id, body)


def _record_bot_msg(db: AsyncSession, consultation_id, text: str):
    """Helper to record a bot/AI message in the conversation."""
    msg = ConsultationMessage(
        consultation_id=consultation_id,
        sender_type=MessageSender.ai,
        message=text,
    )
    db.add(msg)
