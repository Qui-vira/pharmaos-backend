"""
PharmaOS AI - SQLAlchemy ORM Models (v2)

MODIFICATIONS from v1:
1. Products are GLOBAL (no org_id) — shared platform catalog
2. Added ProductAlias table for drug name normalization
3. Added Delivery table for delivery logistics
4. Added SupplierRelationship table for pharmacy-supplier trust links
5. Added controlled_substance flag on Product
6. SupplierProduct now links to global product_id (not free-text names)
7. Inventory uniqueness is per (org_id, product_id)
8. reorder_threshold moved from Product to Inventory (per-org setting)
"""

import uuid
import enum
from datetime import datetime, timezone, date
from decimal import Decimal
from typing import Optional, List

from sqlalchemy import (
    String, Text, Boolean, Integer, Numeric, Date, DateTime, Enum as SAEnum,
    ForeignKey, JSON, ARRAY, Index, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utcnow():
    return datetime.now(timezone.utc)

def new_uuid():
    return uuid.uuid4()


# ─── Enums ──────────────────────────────────────────────────────────────────

class OrgType(str, enum.Enum):
    pharmacy = "pharmacy"
    distributor = "distributor"
    wholesaler = "wholesaler"
    system_admin = "system_admin"

class UserRole(str, enum.Enum):
    super_admin = "super_admin"
    pharmacy_admin = "pharmacy_admin"
    cashier = "cashier"
    pharmacist = "pharmacist"
    distributor_admin = "distributor_admin"
    warehouse_staff = "warehouse_staff"
    sales_rep = "sales_rep"

class OrderStatus(str, enum.Enum):
    draft = "draft"
    submitted = "submitted"
    confirmed = "confirmed"
    processing = "processing"
    ready = "ready"
    picked_up = "picked_up"
    delivered = "delivered"
    cancelled = "cancelled"

class OrderChannel(str, enum.Enum):
    web = "web"
    whatsapp = "whatsapp"
    voice = "voice"
    api = "api"

class PaymentMethod(str, enum.Enum):
    cash = "cash"
    card = "card"
    transfer = "transfer"
    credit = "credit"

class ExpiryAlertType(str, enum.Enum):
    approaching = "approaching"
    warning = "warning"
    critical = "critical"
    expired = "expired"

class ReminderType(str, enum.Enum):
    refill = "refill"
    adherence = "adherence"
    follow_up = "follow_up"
    pickup = "pickup"

class ReminderStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    delivered = "delivered"
    failed = "failed"
    responded = "responded"

class ConsultationStatus(str, enum.Enum):
    intake = "intake"
    ai_processing = "ai_processing"
    awaiting_payment = "awaiting_payment"
    pending_review = "pending_review"
    pharmacist_reviewing = "pharmacist_reviewing"
    approved = "approved"
    completed = "completed"
    cancelled = "cancelled"

class MessageSender(str, enum.Enum):
    customer = "customer"
    ai = "ai"
    pharmacist = "pharmacist"

class PriceSource(str, enum.Enum):
    manual = "manual"
    csv_upload = "csv_upload"
    invoice_scan = "invoice_scan"
    api = "api"

class DeliveryStatus(str, enum.Enum):
    pending = "pending"
    assigned = "assigned"
    in_transit = "in_transit"
    delivered = "delivered"
    failed = "failed"

class AliasSource(str, enum.Enum):
    manual = "manual"
    ai_detected = "ai_detected"
    csv_import = "csv_import"
    pharmacist_submitted = "pharmacist_submitted"


# ═══════════════════════════════════════════════════════════════════════════
#  CORE MODELS
# ═══════════════════════════════════════════════════════════════════════════


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    org_type: Mapped[OrgType] = mapped_column(SAEnum(OrgType, name="org_type_enum"), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    address: Mapped[Optional[str]] = mapped_column(Text)
    city: Mapped[Optional[str]] = mapped_column(String(100))
    state: Mapped[Optional[str]] = mapped_column(String(100))
    license_number: Mapped[Optional[str]] = mapped_column(String(100))
    whatsapp_phone_number_id: Mapped[Optional[str]] = mapped_column(String(100))
    settings: Mapped[Optional[dict]] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    users: Mapped[List["User"]] = relationship(back_populates="organization", lazy="selectin")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole, name="user_role_enum"), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    # Email verification
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    verification_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    verification_expires: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Phone OTP verification
    phone_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    phone_otp_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone_otp_expires: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Two-factor authentication
    two_factor_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    two_factor_secret_encrypted: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Google OAuth
    google_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True)
    avatar_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    organization: Mapped["Organization"] = relationship(back_populates="users", lazy="selectin")
    __table_args__ = (Index("ix_users_org_id", "org_id"),)


# ═══════════════════════════════════════════════════════════════════════════
#  MOD 1: GLOBAL PRODUCT CATALOG (no org_id)
# ═══════════════════════════════════════════════════════════════════════════


class Product(Base):
    """
    GLOBAL product catalog — shared across the entire platform.
    Organizations reference products via Inventory and SupplierProduct.
    """
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    generic_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    brand_name: Mapped[Optional[str]] = mapped_column(String(255))
    dosage_form: Mapped[Optional[str]] = mapped_column(String(100))
    strength: Mapped[Optional[str]] = mapped_column(String(100))
    manufacturer: Mapped[Optional[str]] = mapped_column(String(255))
    nafdac_number: Mapped[Optional[str]] = mapped_column(String(100), unique=True)
    category: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    requires_prescription: Mapped[bool] = mapped_column(Boolean, default=False)
    controlled_substance: Mapped[bool] = mapped_column(Boolean, default=False)
    unit_of_measure: Mapped[Optional[str]] = mapped_column(String(50), default="pack")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    aliases: Mapped[List["ProductAlias"]] = relationship(back_populates="product", lazy="selectin", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_products_generic_strength", "generic_name", "strength"),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  MOD 2: PRODUCT ALIASES (drug name normalization)
# ═══════════════════════════════════════════════════════════════════════════


class ProductAlias(Base):
    """
    Maps variant drug names to a canonical Product.
    e.g. 'Paracetamol 500mg', 'PCM 500', 'Emzor Para' → same product_id.
    """
    __tablename__ = "product_aliases"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    alias_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source: Mapped[AliasSource] = mapped_column(SAEnum(AliasSource, name="alias_source_enum"), default=AliasSource.manual)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    product: Mapped["Product"] = relationship(back_populates="aliases")

    __table_args__ = (
        Index("ix_alias_normalized", "normalized_name"),
        UniqueConstraint("alias_name", name="uq_alias_name"),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  INVENTORY & BATCHES (org-scoped, references global product)
# ═══════════════════════════════════════════════════════════════════════════


class Inventory(Base):
    """Per-organization inventory. reorder_threshold is per-org (not on Product)."""
    __tablename__ = "inventory"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    quantity_on_hand: Mapped[int] = mapped_column(Integer, default=0)
    quantity_reserved: Mapped[int] = mapped_column(Integer, default=0)
    reorder_threshold: Mapped[int] = mapped_column(Integer, default=10)
    cost_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    selling_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    location: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    product: Mapped["Product"] = relationship(lazy="selectin")

    table_args__ = (
        UniqueConstraint("org_id", "product_id", name="uq_inventory_org_product"),
        Index("ix_inventory_org_id", "org_id"),
    )


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    batch_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    received_date: Mapped[date] = mapped_column(Date, default=lambda: date.today())
    supplier_org_id: Mapped[Optional[uuid.UUID]] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"))
    cost_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), default=0)
    selling_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    product: Mapped["Product"] = relationship(lazy="selectin")
    expiry_alerts: Mapped[List["ExpiryTracking"]] = relationship(back_populates="batch", lazy="selectin")

    __table_args__ = (Index("ix_batches_org_expiry", "org_id", "expiry_date"),)


class ExpiryTracking(Base):
    __tablename__ = "expiry_tracking"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    batch_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("batches.id"), nullable=False)
    alert_type: Mapped[ExpiryAlertType] = mapped_column(SAEnum(ExpiryAlertType, name="expiry_alert_type_enum"), nullable=False)
    alert_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolution_action: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    batch: Mapped["Batch"] = relationship(back_populates="expiry_alerts", lazy="selectin")
    __table_args__ = (Index("ix_expiry_org_resolved", "org_id", "is_resolved"),)


# ═══════════════════════════════════════════════════════════════════════════
#  SALES
# ═══════════════════════════════════════════════════════════════════════════


class Sale(Base):
    __tablename__ = "sales"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    cashier_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    patient_id: Mapped[Optional[uuid.UUID]] = mapped_column(PGUUID(as_uuid=True), ForeignKey("patients.id"))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    payment_method: Mapped[PaymentMethod] = mapped_column(SAEnum(PaymentMethod, name="payment_method_enum"), nullable=False)
    sale_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    items: Mapped[dict] = mapped_column(JSONB, nullable=False)
    consultation_id: Mapped[Optional[uuid.UUID]] = mapped_column(PGUUID(as_uuid=True), ForeignKey("consultations.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_sales_org_date", "org_id", "sale_date"),)


# ═══════════════════════════════════════════════════════════════════════════
#  ORDERS + MOD 3: DELIVERY LOGISTICS
# ═══════════════════════════════════════════════════════════════════════════


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    order_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    buyer_org_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    seller_org_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(SAEnum(OrderStatus, name="order_status_enum"), default=OrderStatus.draft)
    channel: Mapped[OrderChannel] = mapped_column(SAEnum(OrderChannel, name="order_channel_enum"), default=OrderChannel.web)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    pickup_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    delivery_address: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    items: Mapped[List["OrderItem"]] = relationship(back_populates="order", lazy="selectin", cascade="all, delete-orphan")
    delivery: Mapped[Optional["Delivery"]] = relationship(back_populates="order", uselist=False, lazy="selectin")

    __table_args__ = (
        Index("ix_orders_buyer", "buyer_org_id", "status"),
        Index("ix_orders_seller", "seller_org_id", "status"),
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    order_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship(lazy="selectin")


class Delivery(Base):
    """MOD 3: Delivery logistics for customer delivery orders."""
    __tablename__ = "deliveries"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    order_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("orders.id"), unique=True, nullable=False)
    driver_name: Mapped[Optional[str]] = mapped_column(String(255))
    driver_phone: Mapped[Optional[str]] = mapped_column(String(20))
    delivery_status: Mapped[DeliveryStatus] = mapped_column(
        SAEnum(DeliveryStatus, name="delivery_status_enum"), default=DeliveryStatus.pending
    )
    estimated_arrival: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    order: Mapped["Order"] = relationship(back_populates="delivery")


# ═══════════════════════════════════════════════════════════════════════════
#  SUPPLIER PRODUCTS + RELATIONSHIPS
# ═══════════════════════════════════════════════════════════════════════════


class SupplierProduct(Base):
    """Supplier's listing for a global product. Links org_id + product_id."""
    __tablename__ = "supplier_products"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    quantity_available: Mapped[int] = mapped_column(Integer, default=0)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    product: Mapped["Product"] = relationship(lazy="selectin")
    price_history: Mapped[List["PriceRecord"]] = relationship(back_populates="supplier_product", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("org_id", "product_id", name="uq_supplier_product_org"),
        Index("ix_supplier_products_org", "org_id", "is_published"),
    )


class PriceRecord(Base):
    __tablename__ = "price_records"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    supplier_product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("supplier_products.id"), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[PriceSource] = mapped_column(SAEnum(PriceSource, name="price_source_enum"), default=PriceSource.manual)

    supplier_product: Mapped["SupplierProduct"] = relationship(back_populates="price_history")


class SupplierRelationship(Base):
    """EXTRA MOD: Pharmacy ↔ Supplier trust relationship with credit terms."""
    __tablename__ = "supplier_relationships"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    pharmacy_org_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    supplier_org_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    is_preferred: Mapped[bool] = mapped_column(Boolean, default=False)
    credit_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    payment_terms: Mapped[Optional[str]] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("pharmacy_org_id", "supplier_org_id", name="uq_supplier_relationship"),
        Index("ix_supplier_rel_pharmacy", "pharmacy_org_id"),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  PATIENTS, REMINDERS, CONSULTATIONS
# ═══════════════════════════════════════════════════════════════════════════


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    date_of_birth: Mapped[Optional[date]] = mapped_column(Date)
    gender: Mapped[Optional[str]] = mapped_column(String(10))
    allergies: Mapped[Optional[list]] = mapped_column(ARRAY(Text), default=list)
    chronic_conditions: Mapped[Optional[list]] = mapped_column(ARRAY(Text), default=list)
    consent_given: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_patients_org_phone", "org_id", "phone"),
        UniqueConstraint("org_id", "phone", name="uq_patient_org_phone"),
        UniqueConstraint("phone", name="uq_patient_phone_global"),
    )


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    reminder_type: Mapped[ReminderType] = mapped_column(SAEnum(ReminderType, name="reminder_type_enum"), nullable=False)
    product_id: Mapped[Optional[uuid.UUID]] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[ReminderStatus] = mapped_column(SAEnum(ReminderStatus, name="reminder_status_enum"), default=ReminderStatus.pending)
    response: Mapped[Optional[str]] = mapped_column(String(255))
    recurrence_rule: Mapped[Optional[str]] = mapped_column(String(100))
    message_template: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_reminders_org_status", "org_id", "status", "scheduled_at"),)


class Consultation(Base):
    __tablename__ = "consultations"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    status: Mapped[ConsultationStatus] = mapped_column(
        SAEnum(ConsultationStatus, name="consultation_status_enum"), default=ConsultationStatus.intake
    )
    symptom_summary: Mapped[Optional[str]] = mapped_column(Text)
    ai_questions_asked: Mapped[Optional[dict]] = mapped_column(JSONB, default=list)
    consultation_fee_paid: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    assigned_pharmacist_id: Mapped[Optional[uuid.UUID]] = mapped_column(PGUUID(as_uuid=True), ForeignKey("users.id"))
    channel: Mapped[OrderChannel] = mapped_column(SAEnum(OrderChannel, name="order_channel_enum"), default=OrderChannel.whatsapp)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    messages: Mapped[List["ConsultationMessage"]] = relationship(back_populates="consultation", lazy="selectin", order_by="ConsultationMessage.sent_at")
    pharmacist_action: Mapped[Optional["PharmacistAction"]] = relationship(back_populates="consultation", uselist=False, lazy="selectin")

    __table_args__ = (Index("ix_consultations_org_status", "org_id", "status"),)


class ConsultationMessage(Base):
    __tablename__ = "consultation_messages"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    consultation_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("consultations.id"), nullable=False)
    sender_type: Mapped[MessageSender] = mapped_column(SAEnum(MessageSender, name="message_sender_enum"), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    consultation: Mapped["Consultation"] = relationship(back_populates="messages")


class PharmacistAction(Base):
    """
    MOD 5 — AI GUARDRAIL ENFORCEMENT:
    ONLY pharmacists can create this record (enforced at API level via require_roles("pharmacist")).
    AI processes NEVER write to this table.
    is_approved is the ONLY gateway to sending medical info to customers.
    """
    __tablename__ = "pharmacist_actions"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    consultation_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("consultations.id"), unique=True, nullable=False)
    pharmacist_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    diagnosis: Mapped[str] = mapped_column(Text, nullable=False)
    drug_plan: Mapped[dict] = mapped_column(JSONB, nullable=False)
    total_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    consultation: Mapped["Consultation"] = relationship(back_populates="pharmacist_action")


# ═══════════════════════════════════════════════════════════════════════════
#  SYSTEM TABLES
# ═══════════════════════════════════════════════════════════════════════════


class VoiceCallLog(Base):
    __tablename__ = "voice_call_logs"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    twilio_call_sid: Mapped[Optional[str]] = mapped_column(String(100))
    caller_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), default="inbound")
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[Optional[str]] = mapped_column(String(50))
    intent_detected: Mapped[Optional[str]] = mapped_column(String(100))
    order_id: Mapped[Optional[uuid.UUID]] = mapped_column(PGUUID(as_uuid=True), ForeignKey("orders.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    call_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("voice_call_logs.id"), nullable=False)
    speaker: Mapped[str] = mapped_column(String(10), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp_ms: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[Optional[str]] = mapped_column(Text)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    link: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_notifications_user", "user_id", "is_read"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(PGUUID(as_uuid=True))
    changes: Mapped[Optional[dict]] = mapped_column(JSONB)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_audit_org_action", "org_id", "action", "created_at"),)
