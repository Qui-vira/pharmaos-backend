"""
PharmaOS AI - Snap to Stock Endpoints
AI camera-based inventory management using GPT-4 Vision.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, TokenData
from app.models.models import Product, Inventory
from app.schemas.schemas import (
    SnapAnalyzeRequest,
    SnapAnalyzeResponse,
    SnapMatchRequest,
    SnapMatchResponse,
    SnapConfirmRequest,
    SnapConfirmResponse,
    MatchedProduct,
)
from app.services.vision import analyze_shelf_image, match_products
from app.middleware.audit import log_audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/snap-to-stock", tags=["Snap to Stock"])


@router.post("/analyze", response_model=SnapAnalyzeResponse)
async def analyze_image(
    payload: SnapAnalyzeRequest,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Analyze a shelf/product image using GPT-4 Vision."""
    extracted = await analyze_shelf_image(payload.image_base64, payload.mime_type)

    if not extracted:
        raise HTTPException(
            status_code=422,
            detail="Could not extract any products from the image. Try a clearer photo.",
        )

    return SnapAnalyzeResponse(
        products=[
            {
                "product_name": p.get("product_name", "Unknown"),
                "strength": p.get("strength"),
                "manufacturer": p.get("manufacturer"),
                "batch_number": p.get("batch_number"),
                "expiry_date": p.get("expiry_date"),
            }
            for p in extracted
        ]
    )


@router.post("/match", response_model=SnapMatchResponse)
async def match_to_catalog(
    payload: SnapMatchRequest,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Match extracted product names against the NAFDAC product catalog."""
    results = await match_products(payload.product_names, db)

    return SnapMatchResponse(
        matches=[
            MatchedProduct(
                extracted_name=r["extracted_name"],
                matched_product_id=r.get("matched_product_id"),
                matched_product_name=r.get("matched_product_name"),
                generic_name=r.get("generic_name"),
                strength=r.get("strength"),
                manufacturer=r.get("manufacturer"),
                confidence=r["confidence"],
                alternatives=r.get("alternatives", []),
            )
            for r in results
        ]
    )


@router.post("/confirm", response_model=SnapConfirmResponse)
async def confirm_to_inventory(
    payload: SnapConfirmRequest,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Confirm matched products and add/update inventory."""
    added = 0
    updated = 0
    errors = []

    for item in payload.items:
        product = (await db.execute(select(Product).where(Product.id == item.product_id))).scalar_one_or_none()
        if not product:
            errors.append(f"Product {item.product_id} not found")
            continue

        inv = (await db.execute(
            select(Inventory).where(
                and_(
                    Inventory.org_id == current_user.org_id,
                    Inventory.product_id == item.product_id,
                )
            )
        )).scalar_one_or_none()

        if inv:
            inv.quantity_on_hand += item.quantity
            if item.cost_price is not None:
                inv.cost_price = item.cost_price
            if item.selling_price is not None:
                inv.selling_price = item.selling_price
            updated += 1
        else:
            inv = Inventory(
                org_id=current_user.org_id,
                product_id=item.product_id,
                quantity_on_hand=item.quantity,
                cost_price=item.cost_price or 0,
                selling_price=item.selling_price or 0,
            )
            db.add(inv)
            added += 1

    await db.flush()

    await log_audit(
        db, current_user.org_id, current_user.user_id,
        "snap_to_stock_confirm", "inventory",
        changes={"added": added, "updated": updated},
    )

    return SnapConfirmResponse(
        added=added,
        updated=updated,
        errors=errors,
    )
