"""
PharmaOS AI - Predictive Analytics Service
Analyzes sales history, inventory data, and batch expiry dates to generate predictions.
All predictions are per-org (filtered by org_id).
"""

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Batch, Inventory, Product, Sale

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  1. DEMAND FORECAST
# ═══════════════════════════════════════════════════════════════════════════


async def demand_forecast(
    db: AsyncSession, org_id: UUID, days_ahead: int = 30
) -> list[dict[str, Any]]:
    """
    Forecast demand per product based on 90-day sales history.
    Parses the JSONB `items` field on each Sale to count per-product units sold.
    Returns list sorted by avg_daily_sales descending.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)

    # Fetch org sales from last 90 days
    result = await db.execute(
        select(Sale.items, Sale.sale_date).where(
            Sale.org_id == org_id,
            Sale.sale_date >= cutoff,
        )
    )
    rows = result.all()

    if not rows:
        return []

    # Aggregate sales per product from JSONB items
    product_sales: dict[str, dict] = {}  # product_id -> {total_qty, count, dates}
    for items_json, sale_date in rows:
        if not isinstance(items_json, list):
            continue
        for item in items_json:
            pid = str(item.get("product_id", ""))
            qty = item.get("quantity", 0)
            if not pid or qty <= 0:
                continue
            if pid not in product_sales:
                product_sales[pid] = {"total_qty": 0, "record_count": 0, "dates": set()}
            product_sales[pid]["total_qty"] += qty
            product_sales[pid]["record_count"] += 1
            product_sales[pid]["dates"].add(sale_date.date() if hasattr(sale_date, "date") else sale_date)

    if not product_sales:
        return []

    # Resolve product names
    product_ids = [UUID(pid) for pid in product_sales]
    prod_result = await db.execute(
        select(Product.id, Product.name).where(Product.id.in_(product_ids))
    )
    product_names = {str(r.id): r.name for r in prod_result.all()}

    # Calculate forecasts
    days_in_window = 90
    forecasts = []
    for pid, data in product_sales.items():
        avg_daily = data["total_qty"] / days_in_window
        record_count = data["record_count"]

        if record_count >= 30:
            confidence = "high"
        elif record_count >= 10:
            confidence = "medium"
        else:
            confidence = "low"

        forecasts.append({
            "product_id": pid,
            "product_name": product_names.get(pid, "Unknown"),
            "avg_daily_sales": round(avg_daily, 2),
            "forecast_7d": round(avg_daily * 7),
            "forecast_14d": round(avg_daily * 14),
            "forecast_30d": round(avg_daily * 30),
            "confidence": confidence,
        })

    forecasts.sort(key=lambda x: x["avg_daily_sales"], reverse=True)
    return forecasts


# ═══════════════════════════════════════════════════════════════════════════
#  2. REORDER PREDICTIONS
# ═══════════════════════════════════════════════════════════════════════════


async def reorder_predictions(
    db: AsyncSession, org_id: UUID
) -> list[dict[str, Any]]:
    """
    For each inventory item, estimate days until stockout and suggest reorder quantities.
    Uses 90-day sales history for avg daily sales rate.
    """
    # Get demand data first
    demand = await demand_forecast(db, org_id)
    sales_rate = {d["product_id"]: d["avg_daily_sales"] for d in demand}

    # Get current inventory
    result = await db.execute(
        select(Inventory).where(Inventory.org_id == org_id).options()
    )
    inventory_items = result.scalars().all()

    predictions = []
    for inv in inventory_items:
        pid = str(inv.product_id)
        current_stock = inv.quantity_on_hand
        avg_daily = sales_rate.get(pid, 0.0)
        product_name = inv.product.name if inv.product else "Unknown"

        if avg_daily > 0:
            days_until_stockout = round(current_stock / avg_daily, 1)
        else:
            days_until_stockout = None  # No sales data — can't predict

        # Determine urgency
        if days_until_stockout is not None:
            if days_until_stockout < 7:
                reorder_urgency = "reorder_urgent"
            elif days_until_stockout < 14:
                reorder_urgency = "reorder_soon"
            else:
                reorder_urgency = "adequate"
        else:
            reorder_urgency = "no_sales_data"

        # Suggest reorder qty: 30 days of stock minus current
        if avg_daily > 0:
            target_stock = round(avg_daily * 30)
            suggested_reorder_qty = max(0, target_stock - current_stock)
        else:
            suggested_reorder_qty = 0

        predictions.append({
            "product_id": pid,
            "product_name": product_name,
            "current_stock": current_stock,
            "avg_daily_sales": avg_daily,
            "days_until_stockout": days_until_stockout,
            "reorder_urgency": reorder_urgency,
            "suggested_reorder_qty": suggested_reorder_qty,
        })

    # Sort: urgent first, then by days_until_stockout ascending
    def sort_key(x):
        d = x["days_until_stockout"]
        if d is None:
            return (2, 999999)
        urgency_rank = 0 if x["reorder_urgency"] == "reorder_urgent" else (1 if x["reorder_urgency"] == "reorder_soon" else 2)
        return (urgency_rank, d)

    predictions.sort(key=sort_key)
    return predictions


# ═══════════════════════════════════════════════════════════════════════════
#  3. REVENUE FORECAST
# ═══════════════════════════════════════════════════════════════════════════


async def revenue_forecast(
    db: AsyncSession, org_id: UUID, months_ahead: int = 3
) -> dict[str, Any]:
    """
    Forecast revenue using simple linear regression on 6-month historical data.
    Returns historical monthly revenue, projections, trend direction, and confidence.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=180)

    # Monthly revenue aggregation
    result = await db.execute(
        select(
            func.date_trunc("month", Sale.sale_date).label("month"),
            func.sum(Sale.total_amount).label("revenue"),
        )
        .where(Sale.org_id == org_id, Sale.sale_date >= cutoff)
        .group_by(func.date_trunc("month", Sale.sale_date))
        .order_by(func.date_trunc("month", Sale.sale_date))
    )
    rows = result.all()

    historical = []
    for row in rows:
        month_dt = row.month
        if hasattr(month_dt, "strftime"):
            month_str = month_dt.strftime("%Y-%m")
        else:
            month_str = str(month_dt)[:7]
        historical.append({
            "month": month_str,
            "revenue": float(row.revenue or 0),
        })

    n = len(historical)
    if n < 2:
        # Not enough data for regression
        avg_rev = historical[0]["revenue"] if n == 1 else 0
        forecast = []
        for i in range(1, months_ahead + 1):
            future_month = (now + timedelta(days=30 * i)).strftime("%Y-%m")
            forecast.append({"month": future_month, "projected_revenue": round(avg_rev, 2)})
        return {
            "historical": historical,
            "forecast": forecast,
            "trend": "stable",
            "confidence": "low",
        }

    # Simple linear regression: y = a + b*x
    # x = 0, 1, 2, ... (month index)
    x_vals = list(range(n))
    y_vals = [h["revenue"] for h in historical]

    x_mean = sum(x_vals) / n
    y_mean = sum(y_vals) / n

    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, y_vals))
    denominator = sum((x - x_mean) ** 2 for x in x_vals)

    if denominator == 0:
        slope = 0.0
    else:
        slope = numerator / denominator
    intercept = y_mean - slope * x_mean

    # Project future months
    forecast = []
    for i in range(1, months_ahead + 1):
        x_future = n - 1 + i
        projected = max(0, intercept + slope * x_future)
        future_month = (now + timedelta(days=30 * i)).strftime("%Y-%m")
        forecast.append({
            "month": future_month,
            "projected_revenue": round(projected, 2),
        })

    # Determine trend
    if slope > y_mean * 0.02:
        trend = "growing"
    elif slope < -y_mean * 0.02:
        trend = "declining"
    else:
        trend = "stable"

    # Confidence based on data points
    if n >= 5:
        confidence = "high"
    elif n >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "historical": historical,
        "forecast": forecast,
        "trend": trend,
        "confidence": confidence,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  4. EXPIRY RISK SCORING
# ═══════════════════════════════════════════════════════════════════════════


async def expiry_risk_scoring(
    db: AsyncSession, org_id: UUID
) -> list[dict[str, Any]]:
    """
    Score batches expiring within 180 days by risk (0-100).
    Factors: days to expiry, remaining quantity, sales velocity.
    """
    today = date.today()
    horizon = today + timedelta(days=180)

    # Fetch batches expiring within 180 days
    result = await db.execute(
        select(Batch).where(
            Batch.org_id == org_id,
            Batch.expiry_date <= horizon,
            Batch.quantity > 0,
        )
    )
    batches = result.scalars().all()

    if not batches:
        return []

    # Get sales velocity for scoring
    demand = await demand_forecast(db, org_id)
    sales_rate = {d["product_id"]: d["avg_daily_sales"] for d in demand}

    scored = []
    for batch in batches:
        pid = str(batch.product_id)
        days_left = (batch.expiry_date - today).days
        quantity = batch.quantity
        avg_daily = sales_rate.get(pid, 0.0)
        cost_price = float(batch.cost_price or 0)
        product_name = batch.product.name if batch.product else "Unknown"

        # Risk score components (each 0-33, total 0-100 roughly)
        # 1. Expiry urgency: closer to expiry = higher risk
        if days_left <= 0:
            expiry_score = 40
        elif days_left <= 7:
            expiry_score = 35
        elif days_left <= 30:
            expiry_score = 25
        elif days_left <= 90:
            expiry_score = 15
        else:
            expiry_score = 5

        # 2. Quantity risk: more remaining = higher potential loss
        if quantity >= 100:
            qty_score = 30
        elif quantity >= 50:
            qty_score = 20
        elif quantity >= 20:
            qty_score = 15
        else:
            qty_score = 5

        # 3. Velocity risk: slower sales = harder to sell before expiry
        if avg_daily <= 0:
            velocity_score = 30  # No sales at all — highest risk
        elif days_left > 0:
            days_to_sell = quantity / avg_daily
            if days_to_sell > days_left * 2:
                velocity_score = 30  # Can't sell before expiry
            elif days_to_sell > days_left:
                velocity_score = 20
            else:
                velocity_score = 5  # Likely to sell in time
        else:
            velocity_score = 30  # Already expired

        risk_score = min(100, expiry_score + qty_score + velocity_score)

        if risk_score > 80:
            suggested_action = "discount_now"
        elif risk_score >= 50:
            suggested_action = "monitor"
        else:
            suggested_action = "safe"

        potential_loss = round(quantity * cost_price, 2)

        scored.append({
            "batch_id": str(batch.id),
            "product_id": pid,
            "product_name": product_name,
            "batch_number": batch.batch_number or "",
            "expiry_date": batch.expiry_date.isoformat(),
            "days_left": days_left,
            "quantity": quantity,
            "risk_score": risk_score,
            "suggested_action": suggested_action,
            "potential_loss": potential_loss,
        })

    scored.sort(key=lambda x: x["risk_score"], reverse=True)
    return scored
