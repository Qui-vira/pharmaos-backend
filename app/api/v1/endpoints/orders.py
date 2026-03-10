"""
PharmaOS AI - Order Management Endpoints
Order creation, listing, status updates, pickup scheduling.
"""

from typing import Optional
from uuid import UUID
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_roles, TokenData
from app.models.models import (
    Order, OrderItem, OrderStatus, OrderChannel,
    Organization, SupplierProduct, Notification,
    Inventory, Delivery, DeliveryStatus,
)
from app.schemas.schemas import (
    OrderCreateRequest, OrderResponse, OrderItemResponse,
    OrderStatusUpdate, OrderPickupTimeUpdate,
)
from app.utils.helpers import generate_order_number, paginate
from app.middleware.audit import log_audit

router = APIRouter(prefix="/orders", tags=["Orders"])


@router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderCreateRequest,
    current_user: TokenData = Depends(require_roles("pharmacy_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Create a new order from a pharmacy to a distributor."""

    # Verify seller org exists and is a distributor/wholesaler
    seller_result = await db.execute(
        select(Organization).where(Organization.id == payload.seller_org_id)
    )
    seller = seller_result.scalar_one_or_none()
    if not seller or seller.org_type.value not in ("distributor", "wholesaler"):
        raise HTTPException(status_code=400, detail="Invalid supplier organization.")

    # Calculate totals and validate items
    total = Decimal("0")
    order_items = []

    for item in payload.items:
        line_total = item.unit_price * item.quantity
        total += line_total
        order_items.append(
            OrderItem(
                product_id=item.product_id,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=line_total,
            )
        )

    order = Order(
        order_number=generate_order_number(),
        buyer_org_id=current_user.org_id,
        seller_org_id=payload.seller_org_id,
        status=OrderStatus.submitted,
        channel=OrderChannel(payload.channel),
        total_amount=total,
        delivery_address=payload.delivery_address,
        notes=payload.notes,
        items=order_items,
    )
    db.add(order)
    await db.flush()

    # Create notification for the distributor admin
    notification = Notification(
        org_id=payload.seller_org_id,
        user_id=current_user.user_id,  # Will be routed to distributor admins
        type="new_order",
        title=f"New order {order.order_number}",
        body=f"New order worth ₦{total:,.2f} from a pharmacy. {len(payload.items)} item(s).",
        link=f"/orders/{order.id}",
    )
    db.add(notification)

    await log_audit(db, current_user.org_id, current_user.user_id, "create", "order", order.id)
    await db.flush()
    return OrderResponse.model_validate(order)


@router.get("", response_model=dict)
async def list_orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    List orders. Shows orders where the current org is either buyer or seller.
    """
    query = select(Order).where(
        or_(
            Order.buyer_org_id == current_user.org_id,
            Order.seller_org_id == current_user.org_id,
        )
    )

    if status_filter:
        query = query.where(Order.status == OrderStatus(status_filter))

    query = query.order_by(Order.created_at.desc())

    result = await paginate(db, query, page, page_size)
    result["items"] = [OrderResponse.model_validate(o) for o in result["items"]]
    return result


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: UUID,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get order details. Accessible by buyer or seller org."""
    result = await db.execute(
        select(Order).where(
            Order.id == order_id,
            or_(
                Order.buyer_org_id == current_user.org_id,
                Order.seller_org_id == current_user.org_id,
            ),
        )
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    return OrderResponse.model_validate(order)


@router.put("/{order_id}/status", response_model=OrderResponse)
async def update_order_status(
    order_id: UUID,
    payload: OrderStatusUpdate,
    current_user: TokenData = Depends(require_roles(
        "pharmacy_admin", "distributor_admin", "warehouse_staff"
    )),
    db: AsyncSession = Depends(get_db),
):
    """Update order status. Validates transitions and permissions."""
    result = await db.execute(
        select(Order).where(
            Order.id == order_id,
            or_(
                Order.buyer_org_id == current_user.org_id,
                Order.seller_org_id == current_user.org_id,
            ),
        )
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    # Status transition validation
    valid_transitions = {
        "submitted": ["confirmed", "cancelled"],
        "confirmed": ["processing", "cancelled"],
        "processing": ["ready", "cancelled"],
        "ready": ["picked_up", "delivered"],
        "picked_up": ["delivered"],
    }

    current_status = order.status.value
    new_status = payload.status

    if new_status not in valid_transitions.get(current_status, []):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition from '{current_status}' to '{new_status}'.",
        )

    # Seller-only actions
    seller_actions = {"confirmed", "processing", "ready"}
    if new_status in seller_actions and order.seller_org_id != current_user.org_id:
        raise HTTPException(status_code=403, detail="Only the seller can perform this action.")

    old_status = order.status.value
    order.status = OrderStatus(new_status)

    # ──── MODIFICATION 4: Auto-update inventory on delivery ────────────────
    # When status → 'delivered':
    #   - INCREASE pharmacy (buyer) inventory for each item
    #   - DECREASE distributor (seller) inventory for each item
    if new_status == "delivered":
        for item in order.items:
            # Increase buyer (pharmacy) inventory
            buyer_inv = await db.execute(
                select(Inventory).where(
                    Inventory.org_id == order.buyer_org_id,
                    Inventory.product_id == item.product_id,
                )
            )
            buyer_inv_record = buyer_inv.scalar_one_or_none()
            if buyer_inv_record:
                buyer_inv_record.quantity_on_hand += item.quantity
            else:
                # Auto-create inventory record for the pharmacy
                new_inv = Inventory(
                    org_id=order.buyer_org_id,
                    product_id=item.product_id,
                    quantity_on_hand=item.quantity,
                    cost_price=item.unit_price,
                )
                db.add(new_inv)

            # Decrease seller (distributor) inventory
            seller_inv = await db.execute(
                select(Inventory).where(
                    Inventory.org_id == order.seller_org_id,
                    Inventory.product_id == item.product_id,
                )
            )
            seller_inv_record = seller_inv.scalar_one_or_none()
            if seller_inv_record:
                seller_inv_record.quantity_on_hand = max(
                    0, seller_inv_record.quantity_on_hand - item.quantity
                )
    # ───────────────────────────────────────────────────────────────────────

    await log_audit(
        db, current_user.org_id, current_user.user_id, "update_status", "order",
        order.id, {"status": {"old": old_status, "new": new_status}},
    )
    await db.flush()
    return OrderResponse.model_validate(order)


@router.put("/{order_id}/pickup-time", response_model=OrderResponse)
async def set_pickup_time(
    order_id: UUID,
    payload: OrderPickupTimeUpdate,
    current_user: TokenData = Depends(require_roles("distributor_admin", "warehouse_staff")),
    db: AsyncSession = Depends(get_db),
):
    """Assign a pickup time to an order. Distributor-only action."""
    result = await db.execute(
        select(Order).where(
            Order.id == order_id,
            Order.seller_org_id == current_user.org_id,
        )
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    order.pickup_time = payload.pickup_time

    await log_audit(
        db, current_user.org_id, current_user.user_id, "set_pickup_time", "order", order.id,
    )
    await db.flush()
    return OrderResponse.model_validate(order)
