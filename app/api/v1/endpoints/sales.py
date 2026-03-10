"""
PharmaOS AI - Sales & POS Analytics Endpoints
Sale recording, analytics, revenue tracking, anomaly detection.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func, and_, cast, Date
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_roles, TokenData
from app.models.models import Sale, Product, Inventory, PaymentMethod
from app.schemas.schemas import SaleCreateRequest, SaleResponse, SalesAnalytics
from app.utils.helpers import paginate
from app.middleware.audit import log_audit

router = APIRouter(prefix="/sales", tags=["Sales & Analytics"])


@router.post("", response_model=SaleResponse, status_code=status.HTTP_201_CREATED)
async def record_sale(
    payload: SaleCreateRequest,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "cashier")),
    db: AsyncSession = Depends(get_db),
):
    """Record a new sale and deduct inventory."""
    total = Decimal("0")
    items_data = []

    for item in payload.items:
        # Verify product exists in org
        prod_result = await db.execute(
            select(Product).where(Product.id == item.product_id, Product.org_id == current_user.org_id)
        )
        product = prod_result.scalar_one_or_none()
        if not product:
            raise HTTPException(status_code=404, detail=f"Product {item.product_id} not found.")

        # Check inventory
        inv_result = await db.execute(
            select(Inventory).where(
                Inventory.product_id == item.product_id,
                Inventory.org_id == current_user.org_id,
            )
        )
        inv = inv_result.scalar_one_or_none()
        if not inv or inv.quantity_on_hand < item.quantity:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient stock for {product.name}. Available: {inv.quantity_on_hand if inv else 0}",
            )

        # Deduct stock
        inv.quantity_on_hand -= item.quantity

        line_total = item.unit_price * item.quantity
        total += line_total

        items_data.append({
            "product_id": str(item.product_id),
            "product_name": product.name,
            "quantity": item.quantity,
            "unit_price": float(item.unit_price),
            "line_total": float(line_total),
            "batch_id": str(item.batch_id) if item.batch_id else None,
        })

    sale = Sale(
        org_id=current_user.org_id,
        cashier_id=current_user.user_id,
        patient_id=payload.patient_id,
        total_amount=total,
        payment_method=PaymentMethod(payload.payment_method),
        items=items_data,
        consultation_id=payload.consultation_id,
    )
    db.add(sale)
    await db.flush()

    await log_audit(db, current_user.org_id, current_user.user_id, "create", "sale", sale.id)
    return SaleResponse.model_validate(sale)


@router.get("", response_model=dict)
async def list_sales(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "cashier")),
    db: AsyncSession = Depends(get_db),
):
    """List sales with optional date range filter."""
    query = select(Sale).where(Sale.org_id == current_user.org_id)

    if date_from:
        query = query.where(Sale.sale_date >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.where(Sale.sale_date <= datetime.combine(date_to, datetime.max.time()))

    query = query.order_by(Sale.sale_date.desc())

    result = await paginate(db, query, page, page_size)
    result["items"] = [SaleResponse.model_validate(s) for s in result["items"]]
    return result


@router.get("/analytics", response_model=SalesAnalytics)
async def get_sales_analytics(
    days: int = Query(30, ge=1, le=365),
    current_user: TokenData = Depends(require_roles("pharmacy_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Get revenue analytics, top products, and daily trends."""
    start_date = datetime.now(timezone.utc) - timedelta(days=days)

    # Total revenue and count
    totals = await db.execute(
        select(
            func.coalesce(func.sum(Sale.total_amount), 0).label("revenue"),
            func.count(Sale.id).label("count"),
        ).where(Sale.org_id == current_user.org_id, Sale.sale_date >= start_date)
    )
    row = totals.one()
    total_revenue = row.revenue
    total_count = row.count
    avg_value = total_revenue / total_count if total_count > 0 else Decimal("0")

    # Daily revenue
    daily = await db.execute(
        select(
            cast(Sale.sale_date, Date).label("day"),
            func.sum(Sale.total_amount).label("revenue"),
            func.count(Sale.id).label("count"),
        )
        .where(Sale.org_id == current_user.org_id, Sale.sale_date >= start_date)
        .group_by(cast(Sale.sale_date, Date))
        .order_by(cast(Sale.sale_date, Date))
    )
    daily_data = [
        {"date": str(r.day), "revenue": float(r.revenue), "count": r.count}
        for r in daily.all()
    ]

    # Top products (extract from JSONB items)
    # This is a simplified version — for production, consider a sale_items table
    all_sales = await db.execute(
        select(Sale.items).where(
            Sale.org_id == current_user.org_id,
            Sale.sale_date >= start_date,
        )
    )
    product_totals = {}
    for (items,) in all_sales.all():
        if isinstance(items, list):
            for item in items:
                pid = item.get("product_id", "unknown")
                pname = item.get("product_name", "Unknown")
                qty = item.get("quantity", 0)
                rev = item.get("line_total", 0)
                if pid not in product_totals:
                    product_totals[pid] = {"product_id": pid, "product_name": pname, "quantity": 0, "revenue": 0}
                product_totals[pid]["quantity"] += qty
                product_totals[pid]["revenue"] += rev

    top_products = sorted(product_totals.values(), key=lambda x: x["revenue"], reverse=True)[:10]

    return SalesAnalytics(
        total_revenue=total_revenue,
        total_sales_count=total_count,
        average_sale_value=round(avg_value, 2),
        top_products=top_products,
        daily_revenue=daily_data,
    )


@router.get("/anomalies", response_model=list[dict])
async def detect_anomalies(
    current_user: TokenData = Depends(require_roles("pharmacy_admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Detect unusual sales activity.
    Flags days where revenue deviates > 2 standard deviations from the 30-day mean.
    """
    start_date = datetime.now(timezone.utc) - timedelta(days=30)

    daily = await db.execute(
        select(
            cast(Sale.sale_date, Date).label("day"),
            func.sum(Sale.total_amount).label("revenue"),
        )
        .where(Sale.org_id == current_user.org_id, Sale.sale_date >= start_date)
        .group_by(cast(Sale.sale_date, Date))
        .order_by(cast(Sale.sale_date, Date))
    )
    rows = daily.all()

    if len(rows) < 7:
        return []

    revenues = [float(r.revenue) for r in rows]
    mean_rev = sum(revenues) / len(revenues)
    variance = sum((r - mean_rev) ** 2 for r in revenues) / len(revenues)
    std_dev = variance ** 0.5

    if std_dev == 0:
        return []

    anomalies = []
    for row in rows:
        rev = float(row.revenue)
        z_score = (rev - mean_rev) / std_dev
        if abs(z_score) > 2:
            anomalies.append({
                "date": str(row.day),
                "revenue": rev,
                "mean": round(mean_rev, 2),
                "z_score": round(z_score, 2),
                "type": "spike" if z_score > 0 else "dip",
            })

    return anomalies
