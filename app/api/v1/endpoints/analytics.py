"""
PharmaOS AI - Predictive Analytics Endpoints
Demand forecasting, reorder predictions, revenue projections, and expiry risk scoring.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import TokenData, require_roles
from app.services.analytics import (
    demand_forecast,
    expiry_risk_scoring,
    reorder_predictions,
    revenue_forecast,
)

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("/demand-forecast")
async def get_demand_forecast(
    days: int = Query(30, ge=7, le=90, description="Days ahead to forecast"),
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist")),
    db: AsyncSession = Depends(get_db),
):
    """Predict product demand based on 90-day sales history."""
    return await demand_forecast(db, current_user.org_id, days)


@router.get("/reorder-predictions")
async def get_reorder_predictions(
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist")),
    db: AsyncSession = Depends(get_db),
):
    """Estimate days until stockout and suggest reorder quantities."""
    return await reorder_predictions(db, current_user.org_id)


@router.get("/revenue-forecast")
async def get_revenue_forecast(
    months: int = Query(3, ge=1, le=12, description="Months ahead to forecast"),
    current_user: TokenData = Depends(require_roles("pharmacy_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Project revenue using linear regression on 6-month history."""
    return await revenue_forecast(db, current_user.org_id, months)


@router.get("/expiry-risk")
async def get_expiry_risk(
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist")),
    db: AsyncSession = Depends(get_db),
):
    """Score batches expiring within 180 days by risk level."""
    return await expiry_risk_scoring(db, current_user.org_id)


@router.get("/summary")
async def get_analytics_summary(
    current_user: TokenData = Depends(require_roles("pharmacy_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Combined dashboard summary with key numbers from all analytics modules."""
    org_id = current_user.org_id

    demand = await demand_forecast(db, org_id, 30)
    reorders = await reorder_predictions(db, org_id)
    revenue = await revenue_forecast(db, org_id, 3)
    expiry = await expiry_risk_scoring(db, org_id)

    # Key summary numbers
    urgent_reorders = [r for r in reorders if r["reorder_urgency"] == "reorder_urgent"]
    soon_reorders = [r for r in reorders if r["reorder_urgency"] == "reorder_soon"]
    high_risk_batches = [e for e in expiry if e["risk_score"] > 80]
    total_potential_loss = sum(e["potential_loss"] for e in expiry)

    next_month_revenue = revenue["forecast"][0]["projected_revenue"] if revenue.get("forecast") else 0

    return {
        "demand": {
            "top_products": demand[:5],
            "total_products_tracked": len(demand),
        },
        "reorder": {
            "urgent_count": len(urgent_reorders),
            "soon_count": len(soon_reorders),
            "urgent_items": urgent_reorders[:5],
        },
        "revenue": {
            "trend": revenue.get("trend", "stable"),
            "confidence": revenue.get("confidence", "low"),
            "next_month_projected": next_month_revenue,
        },
        "expiry": {
            "high_risk_count": len(high_risk_batches),
            "total_at_risk_batches": len(expiry),
            "total_potential_loss": round(total_potential_loss, 2),
            "top_risks": expiry[:5],
        },
    }
