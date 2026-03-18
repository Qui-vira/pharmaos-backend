"""
PharmaOS AI - Products & Inventory Endpoints (v2)

Products are GLOBAL (no org_id). Any authenticated user can browse products.
Admin/pharmacist can add products to the global catalog.
Inventory is per-org. Each org tracks its own stock, prices, reorder thresholds.
"""

from datetime import date, timedelta
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_roles, TokenData
from app.models.models import (
    Product, ProductAlias, AliasSource,
    Inventory, Batch, ExpiryTracking, ExpiryAlertType, Sale,
)
from app.schemas.schemas import (
    ProductResponse, ProductCreateRequest, ProductUpdateRequest,
    InventoryResponse, InventoryAdjustRequest, LowStockItem,
    BatchCreateRequest, BatchResponse, ExpiryAlertResponse,
)
from app.utils.helpers import normalize_product_name, paginate, sanitize_like
from app.middleware.audit import log_audit

router = APIRouter(tags=["Products & Inventory"])


# ═══════════════════════════════════════════════════════════════════════════
#  GLOBAL PRODUCT CATALOG
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/products", response_model=dict)
async def list_products(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    category: Optional[str] = None,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the global product catalog. All orgs see the same products."""
    query = select(Product).where(Product.is_active == True)

    if search:
        safe_search = sanitize_like(search)
        query = query.where(
            Product.name.ilike(f"%{safe_search}%") |
            Product.generic_name.ilike(f"%{safe_search}%") |
            Product.brand_name.ilike(f"%{safe_search}%")
        )
    if category:
        query = query.where(Product.category == category)

    query = query.order_by(Product.generic_name, Product.name)

    result = await paginate(db, query, page, page_size)
    result["items"] = [ProductResponse.model_validate(p) for p in result["items"]]
    return result


@router.post("/products", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductCreateRequest,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist", "distributor_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Add a product to the GLOBAL catalog.
    Also auto-creates an alias using the product name and adds it to the caller's inventory.
    """
    product = Product(
        name=payload.name,
        generic_name=payload.generic_name or payload.name,
        brand_name=payload.brand_name,
        dosage_form=payload.dosage_form,
        strength=payload.strength,
        manufacturer=payload.manufacturer,
        nafdac_number=payload.nafdac_number,
        category=payload.category,
        requires_prescription=payload.requires_prescription,
        controlled_substance=getattr(payload, 'controlled_substance', False),
        unit_of_measure=payload.unit_of_measure,
    )
    db.add(product)
    await db.flush()

    # Auto-create alias from the product name
    alias = ProductAlias(
        product_id=product.id,
        alias_name=payload.name,
        normalized_name=normalize_product_name(payload.name),
        source=AliasSource.manual,
    )
    db.add(alias)

    # Auto-create inventory record for the creating org
    inventory = Inventory(
        org_id=current_user.org_id,
        product_id=product.id,
        quantity_on_hand=0,
        quantity_reserved=0,
        reorder_threshold=payload.reorder_threshold,
    )
    db.add(inventory)

    await log_audit(db, current_user.org_id, current_user.user_id, "create", "product", product.id)
    await db.flush()
    return ProductResponse.model_validate(product)


@router.put("/products/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: UUID,
    payload: ProductUpdateRequest,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist", "distributor_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update a global product. Only admins and pharmacists can modify the catalog."""
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found.")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if hasattr(product, field):
            setattr(product, field, value)

    await log_audit(db, current_user.org_id, current_user.user_id, "update", "product", product.id, update_data)
    await db.flush()
    return ProductResponse.model_validate(product)


# ═══════════════════════════════════════════════════════════════════════════
#  PRODUCT ALIASES (drug name normalization)
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/products/{product_id}/aliases", status_code=status.HTTP_201_CREATED)
async def add_product_alias(
    product_id: UUID,
    alias_name: str = Query(..., min_length=2),
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist", "distributor_admin", "super_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Add an alias name for a product (e.g. 'PCM 500' for Paracetamol 500mg)."""
    # Verify product exists
    prod = await db.execute(select(Product).where(Product.id == product_id))
    if not prod.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Product not found.")

    # Check alias uniqueness
    existing = await db.execute(select(ProductAlias).where(ProductAlias.alias_name == alias_name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="This alias already exists.")

    alias = ProductAlias(
        product_id=product_id,
        alias_name=alias_name,
        normalized_name=normalize_product_name(alias_name),
        source=AliasSource.pharmacist_submitted,
    )
    db.add(alias)
    await db.flush()

    return {"id": str(alias.id), "alias_name": alias.alias_name, "normalized_name": alias.normalized_name}


@router.get("/products/resolve", response_model=dict)
async def resolve_product_name(
    name: str = Query(..., min_length=2),
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Resolve a drug name to a canonical product using the aliases table.
    Useful for WhatsApp ordering and CSV import name matching.
    """
    normalized = normalize_product_name(name)

    # Try exact alias match first
    alias_result = await db.execute(
        select(ProductAlias).where(ProductAlias.normalized_name == normalized)
    )
    alias = alias_result.scalar_one_or_none()

    if alias:
        product_result = await db.execute(select(Product).where(Product.id == alias.product_id))
        product = product_result.scalar_one_or_none()
        if product:
            return {
                "resolved": True,
                "product": ProductResponse.model_validate(product),
                "matched_alias": alias.alias_name,
            }

    # Try fuzzy match via LIKE on normalized name
    safe_normalized = sanitize_like(normalized)
    fuzzy_result = await db.execute(
        select(ProductAlias).where(ProductAlias.normalized_name.ilike(f"%{safe_normalized}%")).limit(5)
    )
    suggestions = fuzzy_result.scalars().all()

    if suggestions:
        return {
            "resolved": False,
            "suggestions": [
                {"alias_name": a.alias_name, "product_id": str(a.product_id)}
                for a in suggestions
            ],
        }

    return {"resolved": False, "suggestions": []}


# ═══════════════════════════════════════════════════════════════════════════
#  PER-ORG INVENTORY
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/inventory", response_model=dict)
async def list_inventory(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List current stock levels for the current organization."""
    query = select(Inventory).where(Inventory.org_id == current_user.org_id)
    result = await paginate(db, query, page, page_size)
    result["items"] = [InventoryResponse.model_validate(inv) for inv in result["items"]]
    return result


@router.post("/inventory/add-product", response_model=InventoryResponse, status_code=status.HTTP_201_CREATED)
async def add_product_to_inventory(
    product_id: UUID = Query(..., description="Global product ID to add"),
    reorder_threshold: int = Query(10, ge=0),
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "warehouse_staff", "distributor_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Add an existing global product to this org's inventory."""
    prod = await db.execute(select(Product).where(Product.id == product_id))
    if not prod.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Product not found in catalog.")

    existing = await db.execute(
        select(Inventory).where(
            Inventory.org_id == current_user.org_id,
            Inventory.product_id == product_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Product already in your inventory.")

    inv = Inventory(
        org_id=current_user.org_id,
        product_id=product_id,
        quantity_on_hand=0,
        quantity_reserved=0,
        cost_price=0,
        selling_price=0,
        reorder_threshold=reorder_threshold,
    )
    db.add(inv)
    await db.flush()

    result = await db.execute(select(Inventory).where(Inventory.id == inv.id))
    inv_loaded = result.scalar_one()
    return InventoryResponse.model_validate(inv_loaded)

@router.post("/inventory/adjust", response_model=InventoryResponse)
async def adjust_inventory(
    payload: InventoryAdjustRequest,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "warehouse_staff", "distributor_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Adjust stock quantity for a product in this org's inventory."""
    result = await db.execute(
        select(Inventory).where(
            Inventory.product_id == payload.product_id,
            Inventory.org_id == current_user.org_id,
        )
    )
    inv = result.scalar_one_or_none()
    if not inv:
        raise HTTPException(status_code=404, detail="Product not found in your inventory.")

    new_qty = inv.quantity_on_hand + payload.adjustment
    if new_qty < 0:
        raise HTTPException(status_code=400, detail="Adjustment would result in negative stock.")

    old_qty = inv.quantity_on_hand
    inv.quantity_on_hand = new_qty

    if payload.cost_price is not None:
        inv.cost_price = payload.cost_price

    if payload.selling_price is not None:
        inv.selling_price = payload.selling_price

    await log_audit(
        db,
        current_user.org_id,
        current_user.user_id,
        "adjust",
        "inventory",
        inv.id,
        {"quantity": {"old": old_qty, "new": new_qty}, "reason": payload.reason},
    )

    await db.flush()
    return InventoryResponse.model_validate(inv)


@router.get("/inventory/low-stock", response_model=list[LowStockItem])
async def get_low_stock(
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get products below their reorder threshold (per-org threshold)."""
    result = await db.execute(
        select(
            Inventory.product_id,
            Product.name.label("product_name"),
            Inventory.quantity_on_hand,
            Inventory.reorder_threshold,
        )
        .join(Product, Inventory.product_id == Product.id)
        .where(
            Inventory.org_id == current_user.org_id,
            Inventory.quantity_on_hand < Inventory.reorder_threshold,
            Product.is_active == True,
        )
        .order_by(
            (Inventory.reorder_threshold - Inventory.quantity_on_hand).desc()
        )
    )
    rows = result.all()

    return [
        LowStockItem(
            product_id=row.product_id,
            product_name=row.product_name,
            quantity_on_hand=row.quantity_on_hand,
            reorder_threshold=row.reorder_threshold,
            deficit=row.reorder_threshold - row.quantity_on_hand,
        )
        for row in rows
    ]


@router.get("/inventory/demand-forecast", response_model=list[dict])
async def get_demand_forecast(
    days: int = Query(7, ge=1, le=90),
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "pharmacist")),
    db: AsyncSession = Depends(get_db),
):
    """Predict demand for next N days based on 30-day sales history."""
    lookback_start = date.today() - timedelta(days=30)

    result = await db.execute(
        select(
            Product.id,
            Product.name,
            func.count(Sale.id).label("sale_count"),
        )
        .join(Sale, and_(
            Sale.org_id == current_user.org_id,
            Sale.sale_date >= lookback_start,
        ))
        .where(Product.is_active == True)
        .group_by(Product.id, Product.name)
        .order_by(func.count(Sale.id).desc())
        .limit(20)
    )
    rows = result.all()

    forecasts = []
    for row in rows:
        daily_avg = row.sale_count / 30.0
        predicted = round(daily_avg * days, 1)
        forecasts.append({
            "product_id": str(row.id),
            "product_name": row.name,
            "avg_daily_sales": round(daily_avg, 2),
            "predicted_demand": predicted,
            "forecast_days": days,
        })

    return forecasts


# ═══════════════════════════════════════════════════════════════════════════
#  BATCHES & EXPIRY
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/batches", response_model=BatchResponse, status_code=status.HTTP_201_CREATED)
async def create_batch(
    payload: BatchCreateRequest,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "warehouse_staff", "distributor_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Record a new batch receipt and auto-update inventory."""
    # Verify product exists
    result = await db.execute(select(Product).where(Product.id == payload.product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found in catalog.")

    # Create batch
    batch = Batch(
        org_id=current_user.org_id,
        product_id=payload.product_id,
        batch_number=payload.batch_number or None,
        quantity=payload.quantity,
        expiry_date=payload.expiry_date,
        received_date=payload.received_date or date.today(),
        supplier_org_id=payload.supplier_org_id,
        cost_price=getattr(payload, "cost_price", None),
        selling_price=getattr(payload, "selling_price", None),
    )
    db.add(batch)
    await db.flush()

    # Update inventory summary
    inv_result = await db.execute(
        select(Inventory).where(
            Inventory.product_id == payload.product_id,
            Inventory.org_id == current_user.org_id,
        )
    )
    inv = inv_result.scalar_one_or_none()

    if inv:
        inv.quantity_on_hand += payload.quantity

        if getattr(payload, "cost_price", None) is not None:
            inv.cost_price = payload.cost_price

        if getattr(payload, "selling_price", None) is not None:
            inv.selling_price = payload.selling_price
    else:
        inv = Inventory(
            org_id=current_user.org_id,
            product_id=payload.product_id,
            quantity_on_hand=payload.quantity,
            quantity_reserved=0,
            reorder_threshold=10,
            cost_price=getattr(payload, "cost_price", 0) or 0,
            selling_price=getattr(payload, "selling_price", 0) or 0,
        )
        db.add(inv)


    # Generate expiry alerts
    today = date.today()
    days_to_expiry = (payload.expiry_date - today).days

    if days_to_expiry <= 0:
        alert_type = ExpiryAlertType.expired
    elif days_to_expiry <= 7:
        alert_type = ExpiryAlertType.critical
    elif days_to_expiry <= 30:
        alert_type = ExpiryAlertType.warning
    elif days_to_expiry <= 90:
        alert_type = ExpiryAlertType.approaching
    else:
        alert_type = None

    if alert_type:
        alert = ExpiryTracking(
            org_id=current_user.org_id,
            batch_id=batch.id,
            alert_type=alert_type,
            alert_date=today,
        )
        db.add(alert)

    await log_audit(db, current_user.org_id, current_user.user_id, "create", "batch", batch.id)
    await db.flush()
    return BatchResponse.model_validate(batch)


@router.get("/inventory/expiry-alerts", response_model=list[ExpiryAlertResponse])
async def get_expiry_alerts(
    resolved: Optional[bool] = Query(False),
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get expiry alerts for batches."""
    query = (
        select(ExpiryTracking, Batch, Product.name)
        .join(Batch, ExpiryTracking.batch_id == Batch.id)
        .join(Product, Batch.product_id == Product.id)
        .where(
            ExpiryTracking.org_id == current_user.org_id,
            ExpiryTracking.is_resolved == resolved,
        )
        .order_by(ExpiryTracking.alert_date.desc())
        .limit(100)
    )

    result = await db.execute(query)
    rows = result.all()

    alerts = []
    for alert, batch, product_name in rows:
        item = ExpiryAlertResponse.model_validate(alert)
        item.batch = BatchResponse.model_validate(batch)
        item.product_name = product_name
        alerts.append(item)

    return alerts