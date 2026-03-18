"""
PharmaOS AI - Webhook Endpoints (WhatsApp + Twilio Voice)
Handles inbound messages, voice calls, verification, and routing.
"""

import hashlib
import hmac
import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.models.models import (
    Patient, Consultation, ConsultationMessage, ConsultationStatus,
    MessageSender, Organization, VoiceCallLog, Transcript,
)
from app.services.twilio_voice import twiml, twilio_service, VoiceCallState

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


@router.get("/whatsapp")
async def verify_whatsapp_webhook(
    mode: str = Query(None, alias="hub.mode"),
    token: str = Query(None, alias="hub.verify_token"),
    challenge: str = Query(None, alias="hub.challenge"),
):
    """
    WhatsApp webhook verification endpoint.
    Meta sends a GET request with a challenge to verify the webhook URL.
    """
    if mode == "subscribe" and token == settings.WHATSAPP_VERIFY_TOKEN:
        return int(challenge) if challenge else ""

    raise HTTPException(status_code=403, detail="Verification failed.")


@router.post("/whatsapp")
async def handle_whatsapp_message(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Handle inbound WhatsApp messages.
    Routes messages to:
    - Consultation flow (symptom intake)
    - Order flow (pharmacy ordering)
    - Reminder responses
    """
    body = await request.body()

    # Verify webhook signature if app_secret is configured
    if settings.WHATSAPP_APP_SECRET:
        signature = request.headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(
            settings.WHATSAPP_APP_SECRET.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=403, detail="Invalid signature.")

    data = json.loads(body)

    # Extract message data from WhatsApp payload
    try:
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
    except (IndexError, KeyError):
        return {"status": "ok"}

    for message in messages:
        from_number = message.get("from", "")
        msg_type = message.get("type", "")
        msg_body = ""
        button_id = None

        if msg_type == "text":
            msg_body = message.get("text", {}).get("body", "")
        elif msg_type == "interactive":
            # Button or list reply
            interactive = message.get("interactive", {})
            if "button_reply" in interactive:
                msg_body = interactive["button_reply"].get("title", "")
                button_id = interactive["button_reply"].get("id", "")
            elif "list_reply" in interactive:
                msg_body = interactive["list_reply"].get("title", "")
                button_id = interactive["list_reply"].get("id", "")

        logger.info("WhatsApp webhook: from=%s type=%s body=%r button_id=%s",
                     from_number, msg_type, msg_body[:200] if msg_body else "", button_id)

        if not msg_body:
            logger.info("Skipping message from %s: empty body (type=%s)", from_number, msg_type)
            continue

        # Route message
        await route_inbound_message(db, from_number, msg_body, button_id=button_id)

    return {"status": "ok"}


def _normalize_phone(phone: str) -> str:
    """
    Normalize phone number for comparison.
    WhatsApp sends '2347065092434', patients may be stored as '+2347065092434'.
    Strip leading '+' and any non-digit characters so both formats match.
    """
    return phone.strip().lstrip("+").strip()


async def _find_patient_by_phone(db: AsyncSession, phone: str) -> Optional["Patient"]:
    """
    Find a patient by phone, handling format mismatches.
    Tries exact match first, then normalized variants (with/without '+' prefix).
    """
    normalized = _normalize_phone(phone)
    with_plus = f"+{normalized}"

    # Search for both formats in one query
    result = await db.execute(
        select(Patient).where(
            Patient.phone.in_([normalized, with_plus, phone])
        ).limit(1)
    )
    return result.scalar_one_or_none()


async def route_inbound_message(
    db: AsyncSession, phone: str, message: str, button_id: str = None,
):
    """
    Route an inbound WhatsApp message to the correct handler.

    Decision tree:
    1. Check if patient has an active consultation → route to consultation handler
    2. Check for reminder responses (PICKUP, DELIVERY, STOP)
    3. Check for ordering keywords → route to order handler
    4. Default → start new consultation
    """
    logger.info("Routing inbound message: phone=%s message=%r button_id=%s", phone, message[:200], button_id)

    message_lower = message.strip().lower()

    # Find patient across all pharmacies (by phone) — handles +/no-+ mismatch
    try:
        patient = await _find_patient_by_phone(db, phone)
    except Exception:
        logger.exception("Error looking up patient for phone=%s", phone)
        patient = None

    if patient:
        logger.info("Patient found: id=%s name=%s org=%s (stored_phone=%s, incoming=%s)",
                     patient.id, patient.full_name, patient.org_id, patient.phone, phone)
    else:
        logger.warning("No patient found for phone=%s (tried: %s, +%s)",
                       phone, _normalize_phone(phone), _normalize_phone(phone))

    if patient:
        # Check for active consultation (include pending_review/pharmacist_reviewing
        # so "ask_question" messages are routed correctly)
        consult_result = await db.execute(
            select(Consultation).where(
                Consultation.patient_id == patient.id,
                Consultation.status.in_([
                    ConsultationStatus.intake,
                    ConsultationStatus.ai_processing,
                    ConsultationStatus.awaiting_payment,
                    ConsultationStatus.pending_review,
                    ConsultationStatus.pharmacist_reviewing,
                    ConsultationStatus.approved,
                ]),
            ).order_by(Consultation.created_at.desc()).limit(1)
        )
        active_consult = consult_result.scalar_one_or_none()

        if active_consult:
            logger.info("Routing to existing consultation: id=%s status=%s patient=%s",
                        active_consult.id, active_consult.status.value, patient.id)
            await handle_consultation_message(db, patient, active_consult, message, button_id=button_id)
            return

    # Check for reminder responses (only if no active consultation)
    if message_lower in ("pickup", "delivery", "stop"):
        logger.info("Reminder response detected: %s from phone=%s", message_lower, phone)
        # TODO: Handle reminder response via Celery task
        return

    # Check for order keywords
    order_keywords = ["order", "need", "buy", "reorder", "i need", "i want"]
    if any(kw in message_lower for kw in order_keywords):
        logger.info("Order keyword detected in message from phone=%s", phone)
        # TODO: Trigger order intent processing via Celery
        return

    # Default: Start new consultation if patient exists
    if patient:
        logger.info("Starting new consultation for patient=%s org=%s", patient.id, patient.org_id)
        try:
            await start_new_consultation(db, patient, message)
        except Exception:
            logger.exception("Failed to create consultation for patient=%s", patient.id)
    else:
        # Unregistered number — send registration guidance via WhatsApp
        logger.warning("Unregistered sender phone=%s — sending registration link. Message: %r", phone, message[:100])
        try:
            from app.services.whatsapp import whatsapp_service
            wa_phone = _normalize_phone(phone)
            await whatsapp_service.send_text(
                wa_phone,
                "Welcome to PharmaOS! 👋\n\n"
                "You are not yet registered with a pharmacy. "
                "Please visit your pharmacy and scan their QR code to register, "
                "or ask your pharmacist to register you.\n\n"
                "Once registered, you can start a consultation here.",
            )
        except Exception:
            logger.exception("Failed to send registration message to phone=%s", phone)


async def handle_consultation_message(
    db: AsyncSession,
    patient: "Patient",
    consultation: "Consultation",
    message: str,
    button_id: str = None,
):
    """Handle a message within an active consultation."""
    from app.services.consultation_flow import (
        handle_intake_message, handle_approved_message, handle_awaiting_payment_message,
    )

    # Record customer message first
    try:
        msg = ConsultationMessage(
            consultation_id=consultation.id,
            sender_type=MessageSender.customer,
            message=message,
        )
        db.add(msg)
        await db.flush()
        logger.info("Recorded message on consultation=%s from patient=%s (msg_id=%s)",
                     consultation.id, patient.id, msg.id)
    except Exception:
        logger.exception("Failed to record message on consultation=%s", consultation.id)
        raise

    # Route by consultation status
    try:
        if consultation.status in (ConsultationStatus.intake, ConsultationStatus.ai_processing):
            await handle_intake_message(db, patient, consultation, message, button_id=button_id)

        elif consultation.status == ConsultationStatus.awaiting_payment:
            await handle_awaiting_payment_message(db, patient, consultation, message, button_id=button_id)

        elif consultation.status == ConsultationStatus.approved:
            await handle_approved_message(db, patient, consultation, message, button_id=button_id)

        elif consultation.status in (ConsultationStatus.pending_review, ConsultationStatus.pharmacist_reviewing):
            # Patient sent a message while waiting for pharmacist — it's recorded above.
            # Acknowledge so they know we got it.
            from app.services.whatsapp import whatsapp_service
            wa_phone = patient.phone.strip().lstrip("+")
            await whatsapp_service.send_text(
                wa_phone,
                "Your message has been received. A pharmacist is currently reviewing your case.",
            )
            bot_msg = ConsultationMessage(
                consultation_id=consultation.id,
                sender_type=MessageSender.ai,
                message="Your message has been received. A pharmacist is currently reviewing your case.",
            )
            db.add(bot_msg)
            await db.flush()

    except Exception:
        logger.exception("Error processing consultation message: consultation=%s status=%s",
                         consultation.id, consultation.status.value)


async def start_new_consultation(
    db: AsyncSession,
    patient: "Patient",
    initial_message: str,
):
    """Start a new consultation from a WhatsApp message."""
    from app.services.consultation_flow import handle_new_consultation

    consultation = Consultation(
        org_id=patient.org_id,
        patient_id=patient.id,
        status=ConsultationStatus.intake,
    )
    db.add(consultation)
    await db.flush()

    logger.info("Consultation created: id=%s patient=%s org=%s", consultation.id, patient.id, patient.org_id)

    # Record initial message
    msg = ConsultationMessage(
        consultation_id=consultation.id,
        sender_type=MessageSender.customer,
        message=initial_message,
    )
    db.add(msg)
    await db.flush()

    logger.info("Initial message recorded: consultation=%s msg_id=%s", consultation.id, msg.id)

    # Send the greeting and first intake question
    try:
        await handle_new_consultation(db, patient, consultation, initial_message)
    except Exception:
        logger.exception("Failed to send intake greeting for consultation=%s", consultation.id)


# ═══════════════════════════════════════════════════════════════════════════
#  TWILIO VOICE WEBHOOKS
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/twilio/voice", response_class=PlainTextResponse)
async def handle_twilio_voice_call(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Handle inbound Twilio voice call.
    Returns TwiML that greets the caller and starts speech gathering.

    Flow: Call → Greeting → <Gather speech> → /twilio/gather
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")
    caller = form.get("From", "")
    called = form.get("To", "")

    logger.info(f"Inbound voice call: {caller} → {called} (SID: {call_sid})")

    # Create voice call log
    call_log = VoiceCallLog(
        org_id=await _resolve_org_from_phone(db, called),
        twilio_call_sid=call_sid,
        caller_phone=caller,
        direction="inbound",
        status="in_progress",
    )
    db.add(call_log)
    await db.flush()

    # Initialize call state
    await VoiceCallState.set(call_sid, {
        "state": "greeting",
        "call_log_id": str(call_log.id),
        "caller": caller,
        "items": [],
        "transcript": [],
    })

    # Generate gather URL
    base_url = str(request.url).split("/webhooks/twilio/voice")[0]
    gather_url = f"{base_url}/webhooks/twilio/gather"

    return twiml.greeting(gather_url)


@router.post("/twilio/gather", response_class=PlainTextResponse)
async def handle_twilio_gather(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Handle speech/DTMF input from Twilio <Gather>.
    This is the main conversational loop.

    1. Receive speech transcription from Twilio
    2. Process with AI (intent detection / order extraction)
    3. Return TwiML with next action (question, confirmation, transfer, hangup)
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")
    speech_result = form.get("SpeechResult", "")
    digits = form.get("Digits", "")
    fallback = request.query_params.get("fallback", "")
    confidence = float(form.get("Confidence", "0") or "0")

    base_url = str(request.url).split("/webhooks/twilio/gather")[0]
    gather_url = f"{base_url}/webhooks/twilio/gather"
    confirm_url = f"{base_url}/webhooks/twilio/confirm"

    logger.info(f"Voice gather: SID={call_sid}, speech='{speech_result}', digits='{digits}', confidence={confidence}")

    # Fallback to human
    if fallback == "human":
        return twiml.transfer_to_human(settings.TWILIO_PHONE_NUMBER or "+0000000000")

    # Get call state
    state = await VoiceCallState.get(call_sid)
    caller_input = speech_result or digits

    if not caller_input:
        return twiml.speak_and_gather(
            "Sorry, I didn't catch that. Could you please repeat?",
            gather_url,
        )

    # Add to transcript
    state["transcript"].append({"speaker": "caller", "text": caller_input})

    # ── State: GREETING / INTENT DETECTION ──────────────────────────────
    if state["state"] in ("greeting", "collecting_items"):

        # Use AI to detect intent
        from app.services.ai_provider import detect_intent, extract_order_items

        intent_result = await detect_intent(caller_input)
        intent = intent_result.get("intent", "unknown")
        intent_confidence = intent_result.get("confidence", 0)

        logger.info(f"Voice intent: {intent} (confidence: {intent_confidence})")

        # Update call log with detected intent
        if state.get("call_log_id"):
            log_result = await db.execute(
                select(VoiceCallLog).where(VoiceCallLog.id == state["call_log_id"])
            )
            log_entry = log_result.scalar_one_or_none()
            if log_entry:
                log_entry.intent_detected = intent

        # Route by intent
        if intent == "place_order":
            # Extract order items
            items = await extract_order_items(caller_input)

            if items:
                state["items"] = items
                state["state"] = "confirming_order"
                await VoiceCallState.set(call_sid, state)

                # Build order summary for TTS
                items_text = ". ".join([
                    f"{item.get('quantity', 1)} packs of {item.get('name', 'item')}"
                    for item in items
                ])

                # Estimate total (in production, look up actual prices)
                estimated_total = sum(item.get("quantity", 1) * 500 for item in items)

                state["transcript"].append({"speaker": "system", "text": f"Order summary: {items_text}"})
                await VoiceCallState.set(call_sid, state)

                return twiml.order_summary(items_text, estimated_total, confirm_url)
            else:
                return twiml.speak_and_gather(
                    "I heard you want to place an order, but I couldn't identify the products. "
                    "Could you please tell me what medications you need and how many?",
                    gather_url,
                )

        elif intent == "check_stock":
            return twiml.speak_and_gather(
                "Let me check that for you. Which product would you like me to check availability for?",
                gather_url,
            )

        elif intent == "ask_price":
            return twiml.speak_and_gather(
                "Sure, which product would you like the price for?",
                gather_url,
            )

        elif intent == "speak_to_human":
            state["state"] = "transferred"
            await VoiceCallState.set(call_sid, state)
            return twiml.transfer_to_human(settings.TWILIO_PHONE_NUMBER or "+0000000000")

        elif intent == "ask_delivery_status":
            return twiml.speak_and_gather(
                "Please tell me your order number or your pharmacy name so I can check the status.",
                gather_url,
            )

        else:
            # Low confidence or unknown — ask to clarify
            if intent_confidence < 0.5:
                return twiml.speak_and_gather(
                    "I'm not sure I understood. You can say things like: "
                    "I want to order paracetamol, or check stock for amoxicillin, "
                    "or speak to a pharmacist.",
                    gather_url,
                )
            else:
                return twiml.speak_and_gather(
                    "I can help you place orders, check stock, or connect you with a pharmacist. "
                    "What would you like to do?",
                    gather_url,
                )

    # ── Unknown state fallback ──────────────────────────────────────────
    return twiml.speak_and_gather(
        "Sorry, something went wrong. Could you please try again?",
        gather_url,
    )


@router.post("/twilio/confirm", response_class=PlainTextResponse)
async def handle_twilio_order_confirm(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Handle order confirmation (yes/no) from the caller.
    If confirmed: create order, save transcript, send WhatsApp confirmation.
    If denied: cancel and hang up.
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")
    speech_result = form.get("SpeechResult", "").lower().strip()
    digits = form.get("Digits", "").strip()

    state = await VoiceCallState.get(call_sid)

    # Determine yes or no
    is_confirmed = False
    if digits == "1":
        is_confirmed = True
    elif digits == "2":
        is_confirmed = False
    elif any(word in speech_result for word in ["yes", "confirm", "okay", "sure", "yeah", "proceed"]):
        is_confirmed = True
    elif any(word in speech_result for word in ["no", "cancel", "stop", "don't", "nope"]):
        is_confirmed = False

    if is_confirmed:
        # Create order
        from app.utils.helpers import generate_order_number
        order_number = generate_order_number()

        logger.info(f"Voice order confirmed: {order_number} for call {call_sid}")

        # Save transcript
        state["transcript"].append({"speaker": "caller", "text": speech_result or f"DTMF: {digits}"})
        state["transcript"].append({"speaker": "system", "text": f"Order {order_number} confirmed"})
        state["state"] = "completed"

        # Save transcript entries to DB
        if state.get("call_log_id"):
            for entry in state["transcript"]:
                transcript = Transcript(
                    call_id=state["call_log_id"],
                    speaker=entry["speaker"],
                    text=entry["text"],
                )
                db.add(transcript)

            # Update call log status
            log_result = await db.execute(
                select(VoiceCallLog).where(VoiceCallLog.id == state["call_log_id"])
            )
            log_entry = log_result.scalar_one_or_none()
            if log_entry:
                log_entry.status = "completed"

        await db.flush()
        await VoiceCallState.delete(call_sid)

        # TODO: Actually create Order + OrderItems in DB using state["items"]
        # TODO: send_order_confirmation.delay(caller_phone, order_number, total, items_count)

        return twiml.order_confirmed(order_number)

    else:
        # Cancelled
        state["state"] = "completed"
        await VoiceCallState.set(call_sid, state)
        await VoiceCallState.delete(call_sid)

        return twiml.speak_and_end(
            "No problem, your order has been cancelled. Thank you for calling PharmaOS. Goodbye."
        )


@router.post("/twilio/status", response_class=PlainTextResponse)
async def handle_twilio_status_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Twilio status callback — called when call status changes.
    Used to update call duration and final status.
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")
    duration = form.get("CallDuration", "0")

    logger.info(f"Voice status callback: SID={call_sid}, status={call_status}, duration={duration}s")

    # Update call log
    result = await db.execute(
        select(VoiceCallLog).where(VoiceCallLog.twilio_call_sid == call_sid)
    )
    call_log = result.scalar_one_or_none()
    if call_log:
        call_log.status = call_status
        call_log.duration_seconds = int(duration) if duration else 0
        await db.flush()

    return ""


# ─── Helper ─────────────────────────────────────────────────────────────────


async def _resolve_org_from_phone(db: AsyncSession, phone: str) -> Optional[uuid.UUID]:
    """
    Resolve which organization a Twilio phone number belongs to.
    For now, uses the first pharmacy org. In production, map Twilio numbers to orgs.
    """
    from app.models.models import Organization, OrgType
    result = await db.execute(
        select(Organization).where(Organization.org_type == OrgType.pharmacy).limit(1)
    )
    org = result.scalar_one_or_none()
    return org.id if org else None

