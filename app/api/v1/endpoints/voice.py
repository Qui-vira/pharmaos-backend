"""
PharmaOS AI - Voice Call Management Endpoints
View call logs, transcripts, and voice ordering analytics.
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_roles, TokenData
from app.models.models import VoiceCallLog, Transcript
from app.utils.helpers import paginate

router = APIRouter(prefix="/voice", tags=["Voice Calls"])


@router.get("/calls", response_model=dict)
async def list_voice_calls(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "distributor_admin")),
    db: AsyncSession = Depends(get_db),
):
    """List voice call logs for the current organization."""
    query = select(VoiceCallLog).where(VoiceCallLog.org_id == current_user.org_id)

    if status_filter:
        query = query.where(VoiceCallLog.status == status_filter)

    query = query.order_by(VoiceCallLog.created_at.desc())

    result = await paginate(db, query, page, page_size)
    result["items"] = [
        {
            "id": str(call.id),
            "twilio_call_sid": call.twilio_call_sid,
            "caller_phone": call.caller_phone,
            "direction": call.direction,
            "duration_seconds": call.duration_seconds,
            "status": call.status,
            "intent_detected": call.intent_detected,
            "order_id": str(call.order_id) if call.order_id else None,
            "created_at": call.created_at.isoformat(),
        }
        for call in result["items"]
    ]
    return result


@router.get("/calls/{call_id}", response_model=dict)
async def get_voice_call_detail(
    call_id: UUID,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "distributor_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Get voice call detail including full transcript."""
    result = await db.execute(
        select(VoiceCallLog).where(
            VoiceCallLog.id == call_id,
            VoiceCallLog.org_id == current_user.org_id,
        )
    )
    call = result.scalar_one_or_none()
    if not call:
        raise HTTPException(status_code=404, detail="Voice call not found.")

    # Get transcript
    transcript_result = await db.execute(
        select(Transcript)
        .where(Transcript.call_id == call.id)
        .order_by(Transcript.created_at)
    )
    transcripts = transcript_result.scalars().all()

    return {
        "id": str(call.id),
        "twilio_call_sid": call.twilio_call_sid,
        "caller_phone": call.caller_phone,
        "direction": call.direction,
        "duration_seconds": call.duration_seconds,
        "status": call.status,
        "intent_detected": call.intent_detected,
        "order_id": str(call.order_id) if call.order_id else None,
        "created_at": call.created_at.isoformat(),
        "transcript": [
            {
                "speaker": t.speaker,
                "text": t.text,
                "timestamp_ms": t.timestamp_ms,
            }
            for t in transcripts
        ],
    }


@router.get("/analytics", response_model=dict)
async def voice_call_analytics(
    current_user: TokenData = Depends(require_roles("pharmacy_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Get voice call analytics: total calls, avg duration, intents breakdown."""
    # Total calls
    total = await db.execute(
        select(func.count(VoiceCallLog.id))
        .where(VoiceCallLog.org_id == current_user.org_id)
    )
    total_calls = total.scalar() or 0

    # Average duration
    avg_dur = await db.execute(
        select(func.avg(VoiceCallLog.duration_seconds))
        .where(
            VoiceCallLog.org_id == current_user.org_id,
            VoiceCallLog.duration_seconds.isnot(None),
        )
    )
    avg_duration = round(avg_dur.scalar() or 0, 1)

    # Completed calls
    completed = await db.execute(
        select(func.count(VoiceCallLog.id))
        .where(
            VoiceCallLog.org_id == current_user.org_id,
            VoiceCallLog.status == "completed",
        )
    )
    completed_calls = completed.scalar() or 0

    # Calls that resulted in orders
    ordered = await db.execute(
        select(func.count(VoiceCallLog.id))
        .where(
            VoiceCallLog.org_id == current_user.org_id,
            VoiceCallLog.order_id.isnot(None),
        )
    )
    orders_from_calls = ordered.scalar() or 0

    # Intent breakdown
    intents = await db.execute(
        select(
            VoiceCallLog.intent_detected,
            func.count(VoiceCallLog.id),
        )
        .where(
            VoiceCallLog.org_id == current_user.org_id,
            VoiceCallLog.intent_detected.isnot(None),
        )
        .group_by(VoiceCallLog.intent_detected)
    )
    intent_breakdown = {row[0]: row[1] for row in intents.all()}

    return {
        "total_calls": total_calls,
        "completed_calls": completed_calls,
        "avg_duration_seconds": avg_duration,
        "orders_from_calls": orders_from_calls,
        "conversion_rate": round(orders_from_calls / total_calls * 100, 1) if total_calls > 0 else 0,
        "intent_breakdown": intent_breakdown,
    }
