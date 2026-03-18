"""
PharmaOS AI - Supplier Catalog & Price Intelligence Endpoints (v2)

SupplierProduct now links to global product_id (not free-text names).
Price comparison is done via product_id grouping.
Includes supplier relationship management.
"""

from typing import Optional
from uuid import UUID
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_roles, TokenData
from app.models.models import (
    SupplierProduct, PriceRecord, Product, Organization, PriceSource,
    SupplierRelationship,
)
from app.schemas.schemas import (
    SupplierProductCreateRequest, SupplierProductUpdateRequest,
    SupplierProductResponse, PriceComparisonResult, ProductResponse,
)
from app.middleware.audit import log_audit
from app.utils.helpers import paginate, sanitize_like

router = APIRouter(tags=["Catalog & Pricing"])


# ═══════════════════════════════════════════════════════════════════════════
#  PUBLIC CATALOG (for pharmacies)
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/catalog", response_model=dict)
async def browse_catalog(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    preferred_only: bool = Query(False),
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Browse all published supplier products. Optionally filter by preferred suppliers."""
    query = (
        select(SupplierProduct)
        .join(Product, SupplierProduct.product_id == Product.id)
        .where(SupplierProduct.is_published == True)
    )

    if search:
        safe_search = sanitize_like(search)
        query = query.where(
            Product.name.ilike(f"%{safe_search}%") |
            Product.generic_name.ilike(f"%{safe_search}%")
        )

    # Filter to preferred suppliers only
    if preferred_only:
        preferred_subq = (
            select(SupplierRelationship.supplier_org_id)
            .where(
                SupplierRelationship.pharmacy_org_id == current_user.org_id,
                SupplierRelationship.is_preferred == True,
                SupplierRelationship.is_active == True,
            )
        )
        query = query.where(SupplierProduct.org_id.in_(preferred_subq))

    query = query.order_by(Product.name)

    result = await paginate(db, query, page, page_size)
    result["items"] = [SupplierProductResponse.model_validate(sp) for sp in result["items"]]
    return result


@router.get("/catalog/compare", response_model=list[dict])
async def compare_prices(
    product_id: UUID = Query(...),
    current_user: TokenData = Depends(require_roles("pharmacy_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Compare prices across all suppliers for a specific product."""
    result = await db.execute(
        select(SupplierProduct, Organization.name.label("supplier_name"))
        .join(Organization, SupplierProduct.org_id == Organization.id)
        .where(
            SupplierProduct.product_id == product_id,
            SupplierProduct.is_published == True,
        )
        .order_by(SupplierProduct.unit_price)
    )
    rows = result.all()

    # Check which suppliers are preferred
    pref_result = await db.execute(
        select(SupplierRelationship.supplier_org_id).where(
            SupplierRelationship.pharmacy_org_id == current_user.org_id,
            SupplierRelationship.is_preferred == True,
        )
    )
    preferred_ids = {row[0] for row in pref_result.all()}

    suppliers = []
    for sp, supplier_name in rows:
        suppliers.append({
            "supplier_product_id": str(sp.id),
            "supplier_org_id": str(sp.org_id),
            "supplier_name": supplier_name,
            "unit_price": float(sp.unit_price),
            "quantity_available": sp.quantity_available,
            "is_preferred": sp.org_id in preferred_ids,
        })

    return suppliers


# ═══════════════════════════════════════════════════════════════════════════
#  SUPPLIER PRODUCT MANAGEMENT (for distributors)
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/supplier-products", response_model=dict)
async def list_my_supplier_products(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: TokenData = Depends(require_roles("distributor_admin", "sales_rep", "warehouse_staff")),
    db: AsyncSession = Depends(get_db),
):
    """List supplier's own products."""
    query = (
        select(SupplierProduct)
        .where(SupplierProduct.org_id == current_user.org_id)
    )
    result = await paginate(db, query, page, page_size)
    result["items"] = [SupplierProductResponse.model_validate(sp) for sp in result["items"]]
    return result


@router.post("/supplier-products", response_model=SupplierProductResponse, status_code=status.HTTP_201_CREATED)
async def create_supplier_product(
    payload: SupplierProductCreateRequest,
    current_user: TokenData = Depends(require_roles("distributor_admin", "sales_rep")),
    db: AsyncSession = Depends(get_db),
):
    """List a global product in this supplier's catalog with a price."""
    # Verify product exists
    prod = await db.execute(select(Product).where(Product.id == payload.product_id))
    if not prod.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Product not found in global catalog.")

    # Check if already listed
    existing = await db.execute(
        select(SupplierProduct).where(
            SupplierProduct.org_id == current_user.org_id,
            SupplierProduct.product_id == payload.product_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Product already listed in your catalog.")

    sp = SupplierProduct(
        org_id=current_user.org_id,
        product_id=payload.product_id,
        unit_price=payload.unit_price,
        quantity_available=payload.quantity_available,
        is_published=payload.is_published,
    )
    db.add(sp)
    await db.flush()

    # Record initial price
    price_record = PriceRecord(
        supplier_product_id=sp.id,
        price=payload.unit_price,
        source=PriceSource.manual,
    )
    db.add(price_record)

    await log_audit(db, current_user.org_id, current_user.user_id, "create", "supplier_product", sp.id)
    await db.flush()
    return SupplierProductResponse.model_validate(sp)


@router.put("/supplier-products/{product_id}", response_model=SupplierProductResponse)
async def update_supplier_product(
    product_id: UUID,
    payload: SupplierProductUpdateRequest,
    current_user: TokenData = Depends(require_roles("distributor_admin", "sales_rep", "warehouse_staff")),
    db: AsyncSession = Depends(get_db),
):
    """Update a supplier product (price, stock, etc.)."""
    result = await db.execute(
        select(SupplierProduct).where(
            SupplierProduct.id == product_id,
            SupplierProduct.org_id == current_user.org_id,
        )
    )
    sp = result.scalar_one_or_none()
    if not sp:
        raise HTTPException(status_code=404, detail="Supplier product not found.")

    update_data = payload.model_dump(exclude_unset=True)
    old_price = sp.unit_price

    for field, value in update_data.items():
        setattr(sp, field, value)

    if "unit_price" in update_data and update_data["unit_price"] != old_price:
        price_record = PriceRecord(
            supplier_product_id=sp.id,
            price=update_data["unit_price"],
            source=PriceSource.manual,
        )
        db.add(price_record)

    await log_audit(db, current_user.org_id, current_user.user_id, "update", "supplier_product", sp.id, update_data)
    await db.flush()
    return SupplierProductResponse.model_validate(sp)


# ═══════════════════════════════════════════════════════════════════════════
#  SUPPLIER RELATIONSHIPS
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/supplier-relationships", response_model=list[dict])
async def list_supplier_relationships(
    current_user: TokenData = Depends(require_roles("pharmacy_admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all supplier relationships for this pharmacy."""
    result = await db.execute(
        select(SupplierRelationship, Organization.name.label("supplier_name"))
        .join(Organization, SupplierRelationship.supplier_org_id == Organization.id)
        .where(
            SupplierRelationship.pharmacy_org_id == current_user.org_id,
            SupplierRelationship.is_active == True,
        )
    )
    rows = result.all()

    return [
        {
            "id": str(rel.id),
            "supplier_org_id": str(rel.supplier_org_id),
            "supplier_name": name,
            "is_preferred": rel.is_preferred,
            "credit_limit": float(rel.credit_limit) if rel.credit_limit else None,
            "payment_terms": rel.payment_terms,
        }
        for rel, name in rows
    ]


@router.post("/supplier-relationships", status_code=status.HTTP_201_CREATED)
async def create_supplier_relationship(
    supplier_org_id: UUID,
    is_preferred: bool = False,
    payment_terms: Optional[str] = None,
    credit_limit: Optional[float] = None,
    current_user: TokenData = Depends(require_roles("pharmacy_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Create a relationship with a supplier (mark as preferred, set credit terms)."""
    # Verify supplier exists and is a distributor/wholesaler
    supplier = await db.execute(select(Organization).where(Organization.id == supplier_org_id))
    org = supplier.scalar_one_or_none()
    if not org or org.org_type.value not in ("distributor", "wholesaler"):
        raise HTTPException(status_code=400, detail="Invalid supplier organization.")

    existing = await db.execute(
        select(SupplierRelationship).where(
            SupplierRelationship.pharmacy_org_id == current_user.org_id,
            SupplierRelationship.supplier_org_id == supplier_org_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Relationship already exists.")

    rel = SupplierRelationship(
        pharmacy_org_id=current_user.org_id,
        supplier_org_id=supplier_org_id,
        is_preferred=is_preferred,
        payment_terms=payment_terms,
        credit_limit=Decimal(str(credit_limit)) if credit_limit else None,
    )
    db.add(rel)
    await db.flush()

    return {"id": str(rel.id), "status": "created"}
