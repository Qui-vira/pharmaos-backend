"""
PharmaOS AI - Automated Reminder Engine
Processes pending reminders and sends them via WhatsApp (with SMS fallback placeholder).

Called by:
  - Celery beat (check_due_reminders task, every 15 min)
  - FastAPI background task (fallback when Celery is unavailable)

Reminder types:
  - refill: Medication refill due
  - adherence: Take-your-meds nudge
  - follow_up: Post-consultation follow-up
  - pickup: Order ready for pickup
  - abandoned: Consultation abandoned (no activity for 24h+ while in intake/awaiting_payment)
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    Consultation, ConsultationStatus, Organization, Patient,
    Reminder, ReminderStatus, ReminderType,
)
from app.services.whatsapp import whatsapp_service

logger = logging.getLogger(__name__)


def _normalize_phone_for_wa(phone: str) -> str:
    return phone.strip().lstrip("+")


async def process_due_reminders(db: AsyncSession) -> dict:
    """
    Find all pending reminders where scheduled_at <= now.
    Send via WhatsApp and update status. Returns stats.
    """
    now = datetime.now(timezone.utc)
    stats = {"checked": 0, "sent": 0, "failed": 0}

    result = await db.execute(
        select(Reminder).where(
            Reminder.status == ReminderStatus.pending,
            Reminder.scheduled_at <= now,
        ).limit(100)  # Process in batches
    )
    reminders = result.scalars().all()
    stats["checked"] = len(reminders)

    for reminder in reminders:
        try:
            # Load patient
            patient_result = await db.execute(
                select(Patient).where(Patient.id == reminder.patient_id)
            )
            patient = patient_result.scalar_one_or_none()
            if not patient:
                logger.warning("Reminder %s: patient not found, marking failed", reminder.id)
                reminder.status = ReminderStatus.failed
                continue

            wa_phone = _normalize_phone_for_wa(patient.phone)
            message = _build_reminder_message(reminder, patient)

            # Send via WhatsApp
            if reminder.reminder_type == ReminderType.refill:
                # Use interactive buttons for refill reminders
                wa_result = await whatsapp_service.send_refill_reminder(
                    to=wa_phone,
                    patient_name=patient.full_name,
                    medication=reminder.message_template or "your medication",
                )
            else:
                wa_result = await whatsapp_service.send_text(wa_phone, message)

            if wa_result.get("status") == "sent":
                reminder.status = ReminderStatus.sent
                reminder.sent_at = now
                stats["sent"] += 1
                logger.info("Reminder %s sent to %s (type=%s)", reminder.id, wa_phone, reminder.reminder_type.value)
            elif wa_result.get("status") == "skipped":
                # WhatsApp not configured — leave as pending for retry
                logger.warning("Reminder %s skipped (WhatsApp not configured)", reminder.id)
            else:
                reminder.status = ReminderStatus.failed
                stats["failed"] += 1
                logger.error("Reminder %s failed: %s", reminder.id, wa_result.get("error", "unknown"))

        except Exception:
            logger.exception("Error processing reminder %s", reminder.id)
            reminder.status = ReminderStatus.failed
            stats["failed"] += 1

    await db.flush()
    logger.info("Reminder engine: checked=%d sent=%d failed=%d", stats["checked"], stats["sent"], stats["failed"])
    return stats


def _build_reminder_message(reminder: Reminder, patient: Patient) -> str:
    """Build the reminder message text based on type."""
    name = patient.full_name.split()[0]  # First name

    if reminder.message_template:
        return reminder.message_template.replace("{name}", name)

    if reminder.reminder_type == ReminderType.refill:
        return f"Hi {name}, your medication refill is due. Please visit your pharmacy or start a consultation here."

    elif reminder.reminder_type == ReminderType.adherence:
        return f"Hi {name}, this is a friendly reminder to take your medication as prescribed."

    elif reminder.reminder_type == ReminderType.follow_up:
        return f"Hi {name}, how are you feeling? If you have any concerns, you can start a new consultation here."

    elif reminder.reminder_type == ReminderType.pickup:
        return f"Hi {name}, your order is ready for pickup at the pharmacy."

    elif reminder.reminder_type == ReminderType.abandoned:
        return (
            f"Hi {name}, we noticed you started a consultation but didn't complete it. "
            f"Reply here if you'd like to continue."
        )

    return f"Hi {name}, you have a notification from your pharmacy."


async def generate_abandoned_reminders(db: AsyncSession) -> int:
    """
    Find consultations stuck in intake or awaiting_payment for 24h+.
    Create abandoned reminders for the patients if none exists yet.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    created = 0

    result = await db.execute(
        select(Consultation).where(
            Consultation.status.in_([
                ConsultationStatus.intake,
                ConsultationStatus.awaiting_payment,
            ]),
            Consultation.updated_at < cutoff,
        )
    )
    stale_consultations = result.scalars().all()

    for consult in stale_consultations:
        # Check if we already sent an abandoned reminder for this patient recently
        existing = await db.execute(
            select(Reminder.id).where(
                Reminder.patient_id == consult.patient_id,
                Reminder.reminder_type == ReminderType.abandoned,
                Reminder.created_at > cutoff,
            ).limit(1)
        )
        if existing.scalar_one_or_none():
            continue

        reminder = Reminder(
            org_id=consult.org_id,
            patient_id=consult.patient_id,
            reminder_type=ReminderType.abandoned,
            scheduled_at=datetime.now(timezone.utc),  # Send immediately
            status=ReminderStatus.pending,
        )
        db.add(reminder)
        created += 1

        # Cancel the stale consultation
        consult.status = ConsultationStatus.cancelled

    await db.flush()
    if created:
        logger.info("Generated %d abandoned reminders", created)
    return created


async def generate_followup_reminders(db: AsyncSession) -> int:
    """
    For consultations completed in the last 24h without a follow-up reminder,
    schedule a follow-up reminder 3 days out.
    """
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(hours=24)
    created = 0

    result = await db.execute(
        select(Consultation).where(
            Consultation.status == ConsultationStatus.completed,
            Consultation.updated_at.between(yesterday, now),
        )
    )
    completed = result.scalars().all()

    for consult in completed:
        # Check if follow-up already scheduled
        existing = await db.execute(
            select(Reminder.id).where(
                Reminder.patient_id == consult.patient_id,
                Reminder.reminder_type == ReminderType.follow_up,
                Reminder.created_at > yesterday,
            ).limit(1)
        )
        if existing.scalar_one_or_none():
            continue

        reminder = Reminder(
            org_id=consult.org_id,
            patient_id=consult.patient_id,
            reminder_type=ReminderType.follow_up,
            scheduled_at=now + timedelta(days=3),
            status=ReminderStatus.pending,
        )
        db.add(reminder)
        created += 1

    await db.flush()
    if created:
        logger.info("Generated %d follow-up reminders", created)
    return created


async def run_reminder_cycle(db: AsyncSession) -> dict:
    """
    Full reminder cycle: generate auto-reminders then process due ones.
    Called by both Celery beat and FastAPI background task.
    """
    # Generate automated reminders
    abandoned = await generate_abandoned_reminders(db)
    followups = await generate_followup_reminders(db)

    # Process all due reminders
    stats = await process_due_reminders(db)
    stats["abandoned_generated"] = abandoned
    stats["followups_generated"] = followups

    return stats
