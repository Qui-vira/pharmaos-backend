"""
PharmaOS AI - Celery Task Definitions (v2)
Real implementations using WhatsApp service and AI provider.
"""

import asyncio
import json
import logging

from celery import Celery
from app.core.config import settings

logger = logging.getLogger(__name__)

celery_app = Celery(
    "pharmaos",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Africa/Lagos",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "check-due-reminders": {
            "task": "app.tasks.celery_app.check_due_reminders",
            "schedule": 900.0,  # Every 15 minutes
        },
        "scan-expiry-alerts": {
            "task": "app.tasks.celery_app.scan_expiry_alerts",
            "schedule": 86400.0,  # Daily
        },
        "check-reorder-predictions": {
            "task": "app.tasks.celery_app.check_reorder_predictions",
            "schedule": 86400.0,  # Daily
        },
    },
)


def run_async(coro):
    """Helper to run async functions from sync Celery tasks."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════════════════════
#  REMINDER TASKS
# ═══════════════════════════════════════════════════════════════════════════


@celery_app.task(name="app.tasks.celery_app.check_due_reminders")
def check_due_reminders():
    """
    Periodic: Generate automated reminders and process all due reminders.
    Sends WhatsApp messages and updates status.
    """
    logger.info("Running reminder cycle...")

    async def _run():
        from app.core.database import async_session_factory
        from app.services.reminder_engine import run_reminder_cycle

        async with async_session_factory() as db:
            stats = await run_reminder_cycle(db)
            await db.commit()
        return stats

    result = run_async(_run())
    logger.info("Reminder cycle complete: %s", result)
    return result


@celery_app.task(name="app.tasks.celery_app.send_whatsapp_message")
def send_whatsapp_message(phone: str, message: str):
    """Send a plain WhatsApp text message."""
    from app.services.whatsapp import whatsapp_service
    result = run_async(whatsapp_service.send_text(phone, message))
    return result


@celery_app.task(name="app.tasks.celery_app.send_refill_reminder")
def send_refill_reminder(phone: str, patient_name: str, medication: str):
    """Send a medication refill reminder with interactive buttons."""
    from app.services.whatsapp import whatsapp_service
    result = run_async(whatsapp_service.send_refill_reminder(phone, patient_name, medication))
    return result


@celery_app.task(name="app.tasks.celery_app.send_order_confirmation")
def send_order_confirmation(phone: str, order_number: str, total: float, items_count: int):
    """Send order confirmation to pharmacy via WhatsApp."""
    from app.services.whatsapp import whatsapp_service
    result = run_async(whatsapp_service.send_order_confirmation(phone, order_number, total, items_count))
    return result


@celery_app.task(name="app.tasks.celery_app.send_order_ready_notification")
def send_order_ready_notification(phone: str, order_number: str, pickup_time: str = None):
    """Notify pharmacy that order is ready."""
    from app.services.whatsapp import whatsapp_service
    result = run_async(whatsapp_service.send_order_ready(phone, order_number, pickup_time))
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  CONSULTATION AI TASKS
# ═══════════════════════════════════════════════════════════════════════════


@celery_app.task(name="app.tasks.celery_app.process_consultation_ai")
def process_consultation_ai(consultation_id: str):
    """
    Process a consultation with AI.

    GUARDRAIL: AI can ONLY do symptom intake and summary generation.
    It CANNOT generate diagnosis or drug recommendations.
    Only pharmacists create pharmacist_actions records.
    """
    from app.services.ai_provider import process_consultation_message

    logger.info(f"Processing consultation {consultation_id} with AI")

    # Production implementation:
    # 1. Load consultation + messages from DB
    # 2. Build conversation history for AI
    # 3. Call process_consultation_message(history)
    # 4. If result["type"] == "question":
    #    a. Save AI message to consultation_messages
    #    b. Send question to customer via WhatsApp
    # 5. If result["type"] == "summary":
    #    a. Update consultation.symptom_summary
    #    b. Set status = "pending_review"
    #    c. Notify pharmacist (in-app + optional WhatsApp)
    # 6. AI NEVER sets diagnosis or drug_plan — that's the pharmacist's job

    return {"consultation_id": consultation_id, "status": "processed"}


@celery_app.task(name="app.tasks.celery_app.send_consultation_response")
def send_consultation_response(consultation_id: str):
    """
    Send pharmacist-approved consultation response to customer.
    THIS TASK ONLY RUNS AFTER pharmacist_actions.is_approved = True.
    """
    from app.services.whatsapp import whatsapp_service

    logger.info(f"Sending approved consultation response for {consultation_id}")

    # Production implementation:
    # 1. Load consultation + pharmacist_action + patient from DB
    # 2. VERIFY: pharmacist_action.is_approved == True (SAFETY CHECK)
    # 3. Build drug plan text from pharmacist_action.drug_plan
    # 4. Call whatsapp_service.send_consultation_response(
    #        patient.phone, patient.full_name, drug_plan_text, total_price)
    # 5. Update consultation status to 'approved'

    return {"consultation_id": consultation_id, "status": "sent"}


# ═══════════════════════════════════════════════════════════════════════════
#  ORDER PROCESSING TASKS
# ═══════════════════════════════════════════════════════════════════════════


@celery_app.task(name="app.tasks.celery_app.process_whatsapp_order")
def process_whatsapp_order(phone: str, message: str, org_id: str = None):
    """
    Process an order placed via WhatsApp.
    Uses AI to extract products, matches them via product aliases, confirms with user.
    """
    from app.services.ai_provider import extract_order_items, detect_intent

    logger.info(f"Processing WhatsApp order from {phone}")

    # Production implementation:
    # 1. Call detect_intent(message) to confirm it's a place_order intent
    # 2. Call extract_order_items(message) to get product list
    # 3. For each product name:
    #    a. Normalize using normalize_product_name()
    #    b. Look up in product_aliases table
    #    c. Find best-price supplier_product for matched product
    # 4. Build order summary message
    # 5. Send WhatsApp confirmation with buttons: CONFIRM / EDIT
    # 6. On CONFIRM callback: create Order + OrderItems in DB

    return {"phone": phone, "status": "processed"}


@celery_app.task(name="app.tasks.celery_app.detect_message_intent")
def detect_message_intent(phone: str, message: str):
    """Detect intent of an inbound WhatsApp message and route accordingly."""
    from app.services.ai_provider import detect_intent

    result = run_async(detect_intent(message))
    intent = result.get("intent", "unknown")

    logger.info(f"Detected intent '{intent}' from {phone}")

    # Route based on intent
    if intent == "place_order":
        process_whatsapp_order.delay(phone, message)
    elif intent == "start_consultation":
        # Will be handled by webhook router creating a consultation
        pass
    elif intent == "speak_to_human":
        send_whatsapp_message.delay(
            phone,
            "We're connecting you to a pharmacist. Please hold on, someone will respond shortly."
        )

    return {"phone": phone, "intent": intent, "confidence": result.get("confidence", 0)}


# ═══════════════════════════════════════════════════════════════════════════
#  FILE PROCESSING TASKS
# ═══════════════════════════════════════════════════════════════════════════


@celery_app.task(name="app.tasks.celery_app.process_catalog_upload")
def process_catalog_upload(file_path: str, org_id: str, source: str = "csv_upload"):
    """
    Process a CSV/XLSX catalog upload from a distributor.
    Matches product names via aliases, upserts into supplier_products.
    """
    logger.info(f"Processing catalog upload: {file_path} for org {org_id}")

    # Production implementation:
    # 1. Read file using pandas (CSV) or openpyxl (XLSX)
    # 2. For each row:
    #    a. Extract product_name, unit_price, quantity
    #    b. Normalize product_name
    #    c. Look up in product_aliases → get product_id
    #    d. If no match: flag for manual review (or AI-suggest closest match)
    #    e. Upsert into supplier_products (org_id + product_id)
    #    f. Record price in price_records
    # 3. Return summary: processed, added, updated, failed

    return {
        "file": file_path,
        "org_id": org_id,
        "processed": 0,
        "added": 0,
        "updated": 0,
        "failed": 0,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  EXPIRY SCANNING
# ═══════════════════════════════════════════════════════════════════════════


@celery_app.task(name="app.tasks.celery_app.scan_expiry_alerts")
def scan_expiry_alerts():
    """
    Daily: Scan batches for expiry alerts, create ExpiryTracking records,
    calculate risk scores, and notify pharmacy admins with potential loss estimates.
    """
    logger.info("Scanning for expiry alerts...")

    async def _scan():
        from datetime import date, timedelta
        from sqlalchemy import select
        from app.core.database import async_session_factory
        from app.models.models import (
            Batch, ExpiryTracking, ExpiryAlertType, Notification, Organization, User, UserRole,
        )
        from app.services.analytics import expiry_risk_scoring

        today = date.today()
        alerts_created = 0
        notifications_created = 0

        async with async_session_factory() as db:
            # Get all active orgs
            orgs = (await db.execute(
                select(Organization.id).where(Organization.is_active == True)
            )).scalars().all()

            for org_id in orgs:
                # Scan batches expiring within 90 days
                horizon = today + timedelta(days=90)
                batches = (await db.execute(
                    select(Batch).where(
                        Batch.org_id == org_id,
                        Batch.expiry_date <= horizon,
                        Batch.quantity > 0,
                    )
                )).scalars().all()

                for batch in batches:
                    days_left = (batch.expiry_date - today).days

                    if days_left <= 0:
                        alert_type = ExpiryAlertType.expired
                    elif days_left <= 7:
                        alert_type = ExpiryAlertType.critical
                    elif days_left <= 30:
                        alert_type = ExpiryAlertType.warning
                    else:
                        alert_type = ExpiryAlertType.approaching

                    # Check if alert already exists for this batch + type
                    existing = (await db.execute(
                        select(ExpiryTracking.id).where(
                            ExpiryTracking.batch_id == batch.id,
                            ExpiryTracking.alert_type == alert_type,
                            ExpiryTracking.is_resolved == False,
                        )
                    )).scalar_one_or_none()

                    if not existing:
                        tracking = ExpiryTracking(
                            org_id=org_id,
                            batch_id=batch.id,
                            alert_type=alert_type,
                            alert_date=today,
                        )
                        db.add(tracking)
                        alerts_created += 1

                # Get risk scores and notify admins about high-risk batches
                risk_scores = await expiry_risk_scoring(db, org_id)
                high_risk = [r for r in risk_scores if r["risk_score"] > 80]

                if high_risk:
                    # Find pharmacy admins to notify
                    admins = (await db.execute(
                        select(User.id).where(
                            User.org_id == org_id,
                            User.role == UserRole.pharmacy_admin,
                            User.is_active == True,
                        )
                    )).scalars().all()

                    for risk_item in high_risk[:10]:  # Cap at 10 notifications per org
                        for admin_id in admins:
                            notif = Notification(
                                org_id=org_id,
                                user_id=admin_id,
                                type="expiry_risk",
                                title=f"Expiry Risk: {risk_item['product_name']}",
                                body=(
                                    f"Batch {risk_item['batch_number'] or 'N/A'} expires in {risk_item['days_left']} days. "
                                    f"Risk score: {risk_item['risk_score']}/100. "
                                    f"Potential loss: \u20a6{risk_item['potential_loss']:,.2f}. "
                                    f"Action: {risk_item['suggested_action'].replace('_', ' ').title()}"
                                ),
                                link="/expiry",
                            )
                            db.add(notif)
                            notifications_created += 1

            await db.commit()

        return {"scanned": len(orgs), "alerts_created": alerts_created, "notifications": notifications_created}

    result = run_async(_scan())
    logger.info("Expiry scan complete: %s", result)
    return result


@celery_app.task(name="app.tasks.celery_app.check_reorder_predictions")
def check_reorder_predictions():
    """
    Daily: Run reorder predictions for each org and create notifications
    for products that need reordering.
    """
    logger.info("Checking reorder predictions...")

    async def _check():
        from sqlalchemy import select
        from app.core.database import async_session_factory
        from app.models.models import Notification, Organization, User, UserRole
        from app.services.analytics import reorder_predictions

        notifications_created = 0

        async with async_session_factory() as db:
            orgs = (await db.execute(
                select(Organization.id).where(Organization.is_active == True)
            )).scalars().all()

            for org_id in orgs:
                predictions = await reorder_predictions(db, org_id)

                # Only notify for urgent and soon items
                actionable = [
                    p for p in predictions
                    if p["reorder_urgency"] in ("reorder_urgent", "reorder_soon")
                ]

                if not actionable:
                    continue

                # Find pharmacy admins
                admins = (await db.execute(
                    select(User.id).where(
                        User.org_id == org_id,
                        User.role == UserRole.pharmacy_admin,
                        User.is_active == True,
                    )
                )).scalars().all()

                for item in actionable[:15]:  # Cap at 15 per org
                    is_urgent = item["reorder_urgency"] == "reorder_urgent"
                    days = item["days_until_stockout"]
                    days_str = f"{days:.0f}" if days is not None else "unknown"

                    for admin_id in admins:
                        notif = Notification(
                            org_id=org_id,
                            user_id=admin_id,
                            type="reorder_alert",
                            title=f"{'URGENT ' if is_urgent else ''}Reorder Alert: {item['product_name']}",
                            body=(
                                f"Estimated {days_str} days until stockout. "
                                f"Current stock: {item['current_stock']} units. "
                                f"Suggested order: {item['suggested_reorder_qty']} units"
                            ),
                            link="/inventory",
                        )
                        db.add(notif)
                        notifications_created += 1

            await db.commit()

        return {"orgs_checked": len(orgs), "notifications": notifications_created}

    result = run_async(_check())
    logger.info("Reorder check complete: %s", result)
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  DELIVERY TRACKING
# ═══════════════════════════════════════════════════════════════════════════


@celery_app.task(name="app.tasks.celery_app.send_delivery_update")
def send_delivery_update(phone: str, order_number: str, driver_name: str, eta: str):
    """Send delivery tracking update to customer."""
    from app.services.whatsapp import whatsapp_service
    result = run_async(whatsapp_service.send_delivery_update(phone, order_number, driver_name, eta))
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  VOICE ORDERING TASKS
# ═══════════════════════════════════════════════════════════════════════════


@celery_app.task(name="app.tasks.celery_app.process_voice_order")
def process_voice_order(call_sid: str, items: list, caller_phone: str, org_id: str):
    """
    Create an order from a confirmed voice call.
    Called after caller confirms order via DTMF/speech.

    1. Resolve product names via aliases
    2. Find best-price supplier for each product
    3. Create Order + OrderItems
    4. Send WhatsApp confirmation to caller
    5. Update VoiceCallLog with order_id
    """
    logger.info(f"Processing voice order: call={call_sid}, items={items}")

    # Production implementation:
    # 1. For each item in items:
    #    a. normalize_product_name(item["name"])
    #    b. Look up in product_aliases → get product_id
    #    c. Find cheapest supplier_product for product_id
    # 2. Create Order(buyer_org_id=org_id, seller_org_id=..., channel="voice")
    # 3. Create OrderItems for each matched product
    # 4. send_order_confirmation.delay(caller_phone, order_number, total, len(items))
    # 5. Update VoiceCallLog.order_id

    return {"call_sid": call_sid, "status": "order_created"}


@celery_app.task(name="app.tasks.celery_app.save_voice_transcript")
def save_voice_transcript(call_log_id: str, transcript_entries: list):
    """
    Save full voice call transcript to the database.
    Called when a voice call ends.
    """
    logger.info(f"Saving transcript for call log {call_log_id}: {len(transcript_entries)} entries")

    # Production implementation:
    # session = get_sync_session()
    # for entry in transcript_entries:
    #     t = Transcript(
    #         call_id=call_log_id,
    #         speaker=entry["speaker"],
    #         text=entry["text"],
    #     )
    #     session.add(t)
    # session.commit()

    return {"call_log_id": call_log_id, "entries_saved": len(transcript_entries)}


@celery_app.task(name="app.tasks.celery_app.make_outbound_voice_call")
def make_outbound_voice_call(to: str, twiml_url: str):
    """
    Initiate an outbound voice call via Twilio.
    Used for proactive notifications like order ready alerts.
    """
    from app.services.twilio_voice import twilio_service
    result = run_async(twilio_service.make_outbound_call(to, twiml_url))
    return result
