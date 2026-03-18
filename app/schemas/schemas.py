"""
PharmaOS AI - Pydantic Schemas
Request and response models for all API endpoints.
"""

from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, ConfigDict, field_validator


# ─── Auth Schemas ───────────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    org_name: str = Field(..., min_length=2, max_length=255)
    org_type: str = Field(..., pattern="^(pharmacy|distributor|wholesaler)$")
    admin_email: EmailStr
    admin_password: str = Field(..., min_length=8, max_length=128)
    admin_full_name: str = Field(..., min_length=2, max_length=255)
    phone: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=500)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    license_number: Optional[str] = Field(None, max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: "UserResponse"


class RefreshRequest(BaseModel):
    refresh_token: str


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    code: str = Field(..., min_length=6, max_length=6, pattern="^[0-9]{6}$")


class ResendCodeRequest(BaseModel):
    email: EmailStr


class GoogleAuthRequest(BaseModel):
    id_token: str = Field(..., min_length=10)
    org_type: str = Field("pharmacy", pattern="^(pharmacy|distributor|wholesaler)$")
    org_name: Optional[str] = Field(None, min_length=2, max_length=255)


class SendPhoneOtpRequest(BaseModel):
    phone: str = Field(..., min_length=10, max_length=20)


class VerifyPhoneRequest(BaseModel):
    phone: str = Field(..., min_length=10, max_length=20)
    code: str = Field(..., min_length=6, max_length=6, pattern="^[0-9]{6}$")


class Enable2FAResponse(BaseModel):
    secret: str
    otpauth_uri: str
    qr_code_url: str


class Verify2FARequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=6, pattern="^[0-9]{6}$")
    temp_token: Optional[str] = None


class LoginResponse(BaseModel):
    """Extended login response that may require additional verification steps."""
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    user: Optional["UserResponse"] = None
    requires_verification: bool = False
    requires_2fa: bool = False
    temp_token: Optional[str] = None
    message: Optional[str] = None
    email: Optional[str] = None


# ─── Organization Schemas ───────────────────────────────────────────────────


class OrgResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    org_type: str
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    license_number: Optional[str] = None
    settings: Optional[dict] = None
    is_active: bool
    created_at: datetime


class OrgUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=255)
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[EmailStr] = None
    address: Optional[str] = Field(None, max_length=500)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    license_number: Optional[str] = Field(None, max_length=100)
    settings: Optional[dict] = None


# ─── User Schemas ───────────────────────────────────────────────────────────


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    email: str
    full_name: str
    role: str
    phone: Optional[str] = None
    is_active: bool
    is_verified: bool = False
    phone_verified: bool = False
    two_factor_enabled: bool = False
    avatar_url: Optional[str] = None
    last_login: Optional[datetime] = None
    created_at: datetime


class UserCreateRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str = Field(..., min_length=2, max_length=255)
    role: str = Field(..., pattern="^(pharmacy_admin|cashier|pharmacist|distributor_admin|warehouse_staff|sales_rep)$")
    phone: Optional[str] = Field(None, max_length=20)


class UserUpdateRequest(BaseModel):
    full_name: Optional[str] = Field(None, min_length=2, max_length=255)
    role: Optional[str] = Field(None, pattern="^(pharmacy_admin|cashier|pharmacist|distributor_admin|warehouse_staff|sales_rep)$")
    phone: Optional[str] = Field(None, max_length=20)
    is_active: Optional[bool] = None


# ─── Product Schemas ────────────────────────────────────────────────────────


class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    generic_name: str
    brand_name: Optional[str] = None
    dosage_form: Optional[str] = None
    strength: Optional[str] = None
    manufacturer: Optional[str] = None
    nafdac_number: Optional[str] = None
    category: Optional[str] = None
    requires_prescription: bool
    controlled_substance: bool = False
    unit_of_measure: Optional[str] = None
    is_active: bool
    created_at: datetime


class ProductCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)
    generic_name: Optional[str] = Field(None, max_length=255)
    brand_name: Optional[str] = Field(None, max_length=255)
    dosage_form: Optional[str] = Field(None, max_length=100)
    strength: Optional[str] = Field(None, max_length=100)
    manufacturer: Optional[str] = Field(None, max_length=255)
    nafdac_number: Optional[str] = Field(None, max_length=50)
    category: Optional[str] = Field(None, max_length=100)
    requires_prescription: bool = False
    controlled_substance: bool = False
    unit_of_measure: str = Field("pack", max_length=50)
    reorder_threshold: int = Field(10, ge=0)


class ProductUpdateRequest(BaseModel):
    name: Optional[str] = None
    generic_name: Optional[str] = None
    brand_name: Optional[str] = None
    dosage_form: Optional[str] = None
    strength: Optional[str] = None
    manufacturer: Optional[str] = None
    nafdac_number: Optional[str] = None
    category: Optional[str] = None
    requires_prescription: Optional[bool] = None
    unit_of_measure: Optional[str] = None
    reorder_threshold: Optional[int] = None


# ─── Inventory Schemas ──────────────────────────────────────────────────────

class InventoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    product_id: UUID
    quantity_on_hand: int
    quantity_reserved: int = 0
    reorder_threshold: int = 10
    cost_price: Decimal = Decimal("0")
    selling_price: Decimal = Decimal("0")
    location: Optional[str] = None
    product: Optional[ProductResponse] = None


class InventoryAdjustRequest(BaseModel):
    product_id: UUID
    adjustment: int = Field(..., description="Positive to add, negative to subtract")
    reason: str = Field(..., min_length=2)
    cost_price: Optional[Decimal] = None
    selling_price: Optional[Decimal] = None


class LowStockItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: UUID
    product_name: str
    quantity_on_hand: int
    reorder_threshold: int
    deficit: int


# ─── Batch & Expiry Schemas ────────────────────────────────────────────────

class BatchCreateRequest(BaseModel):
    product_id: UUID
    batch_number: Optional[str] = None
    quantity: int = Field(..., gt=0)
    expiry_date: date
    received_date: Optional[date] = None
    supplier_org_id: Optional[UUID] = None
    cost_price: Optional[Decimal] = None
    selling_price: Optional[Decimal] = None


class BatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID
    batch_number: Optional[str] = None
    quantity: int
    expiry_date: date
    received_date: date
    supplier_org_id: Optional[UUID] = None
    cost_price: Optional[Decimal] = Decimal("0")
    selling_price: Optional[Decimal] = Decimal("0")
    created_at: datetime


class ExpiryAlertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    batch_id: UUID
    alert_type: str
    alert_date: date
    is_resolved: bool
    resolution_action: Optional[str] = None
    product_name: Optional[str] = None
    batch: Optional[BatchResponse] = None


# ─── Sales Schemas ──────────────────────────────────────────────────────────


class SaleItemInput(BaseModel):
    product_id: UUID
    quantity: int = Field(..., gt=0)
    unit_price: Decimal = Field(..., gt=0)
    batch_id: Optional[UUID] = None


class SaleCreateRequest(BaseModel):
    items: List[SaleItemInput]
    payment_method: str = Field(..., pattern="^(cash|card|transfer|credit)$")
    patient_id: Optional[UUID] = None
    consultation_id: Optional[UUID] = None


class SaleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    cashier_id: UUID
    patient_id: Optional[UUID] = None
    total_amount: Decimal
    payment_method: str
    sale_date: datetime
    items: list
    consultation_id: Optional[UUID] = None


class SalesAnalytics(BaseModel):
    total_revenue: Decimal
    total_sales_count: int
    average_sale_value: Decimal
    top_products: List[dict]
    daily_revenue: List[dict]


# ─── Order Schemas ──────────────────────────────────────────────────────────


class OrderItemInput(BaseModel):
    product_id: UUID
    quantity: int = Field(..., gt=0)
    unit_price: Decimal = Field(..., gt=0)


class OrderCreateRequest(BaseModel):
    seller_org_id: UUID
    items: List[OrderItemInput] = Field(..., min_length=1)
    channel: str = Field("web", pattern="^(web|whatsapp|phone)$")
    delivery_address: Optional[str] = Field(None, max_length=500)
    notes: Optional[str] = Field(None, max_length=1000)


class OrderItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID
    quantity: int
    unit_price: Decimal
    line_total: Decimal


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    order_number: str
    buyer_org_id: UUID
    seller_org_id: UUID
    status: str
    channel: str
    total_amount: Decimal
    pickup_time: Optional[datetime] = None
    delivery_address: Optional[str] = None
    notes: Optional[str] = None
    items: List[OrderItemResponse] = []
    created_at: datetime
    updated_at: datetime


class OrderStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(confirmed|processing|ready|picked_up|delivered|cancelled)$")


class OrderPickupTimeUpdate(BaseModel):
    pickup_time: datetime


# ─── Supplier Catalog Schemas ──────────────────────────────────────────────


class SupplierProductCreateRequest(BaseModel):
    product_id: UUID
    unit_price: Decimal = Field(..., gt=0)
    quantity_available: int = Field(..., ge=0)
    is_published: bool = True


class SupplierProductUpdateRequest(BaseModel):
    unit_price: Optional[Decimal] = Field(None, gt=0)
    quantity_available: Optional[int] = Field(None, ge=0)
    is_published: Optional[bool] = None


class SupplierProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    product_id: UUID
    unit_price: Decimal
    quantity_available: int
    is_published: bool
    product: Optional[ProductResponse] = None
    created_at: datetime


class PriceComparisonResult(BaseModel):
    normalized_name: str
    suppliers: List[dict]


# ─── Patient Schemas ────────────────────────────────────────────────────────


class PatientCreateRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=255)
    phone: str = Field(..., min_length=10, max_length=20)
    date_of_birth: Optional[date] = None
    gender: Optional[str] = Field(None, pattern="^(male|female|other)$")
    allergies: Optional[List[str]] = []
    chronic_conditions: Optional[List[str]] = []
    consent_given: bool = False


class PatientResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    full_name: str
    phone: str
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    allergies: Optional[List[str]] = None
    chronic_conditions: Optional[List[str]] = None
    consent_given: bool
    consent_date: Optional[datetime] = None
    created_at: datetime


class PatientUpdateRequest(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    allergies: Optional[List[str]] = None
    chronic_conditions: Optional[List[str]] = None
    consent_given: Optional[bool] = None


class PatientSelfRegisterRequest(BaseModel):
    org_id: UUID
    full_name: str = Field(..., min_length=2, max_length=255)
    phone: str = Field(..., min_length=10, max_length=20, pattern=r"^\+?\d{10,15}$")
    date_of_birth: Optional[date] = None
    gender: Optional[str] = Field(None, pattern="^(male|female|other)$")
    allergies: Optional[List[str]] = []
    chronic_conditions: Optional[List[str]] = []
    consent_given: bool

    @field_validator("consent_given")
    @classmethod
    def must_consent(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Consent must be given to register.")
        return v


# ─── Reminder Schemas ───────────────────────────────────────────────────────


class ReminderCreateRequest(BaseModel):
    patient_id: UUID
    reminder_type: str = Field(..., pattern="^(refill|adherence|follow_up|pickup)$")
    product_id: Optional[UUID] = None
    scheduled_at: datetime
    recurrence_rule: Optional[str] = None
    message_template: Optional[str] = None


class ReminderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    patient_id: UUID
    reminder_type: str
    product_id: Optional[UUID] = None
    scheduled_at: datetime
    sent_at: Optional[datetime] = None
    status: str
    response: Optional[str] = None
    recurrence_rule: Optional[str] = None
    created_at: datetime


class ReminderUpdateRequest(BaseModel):
    scheduled_at: Optional[datetime] = None
    status: Optional[str] = None
    recurrence_rule: Optional[str] = None
    message_template: Optional[str] = None


# ─── Consultation Schemas ───────────────────────────────────────────────────


class ConsultationMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sender_type: str
    message: str
    sent_at: datetime


class PharmacistActionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    pharmacist_id: UUID
    diagnosis: str
    drug_plan: dict
    total_price: Decimal
    is_approved: bool
    approved_at: Optional[datetime] = None
    notes: Optional[str] = None


class ConsultationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    patient_id: UUID
    status: str
    symptom_summary: Optional[str] = None
    channel: str
    assigned_pharmacist_id: Optional[UUID] = None
    messages: List[ConsultationMessageResponse] = []
    pharmacist_action: Optional[PharmacistActionResponse] = None
    created_at: datetime
    updated_at: datetime


class PharmacistActionRequest(BaseModel):
    diagnosis: str = Field(..., min_length=5)
    drug_plan: List[dict] = Field(..., min_length=1)
    total_price: Decimal = Field(..., gt=0)
    notes: Optional[str] = None


# ─── Notification Schemas ───────────────────────────────────────────────────


class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    type: str
    title: str
    body: Optional[str] = None
    is_read: bool
    link: Optional[str] = None
    created_at: datetime


# ─── Pagination ─────────────────────────────────────────────────────────────


class PaginatedResponse(BaseModel):
    items: List
    total: int
    page: int
    page_size: int
    pages: int


# Fix forward references
TokenResponse.model_rebuild()
LoginResponse.model_rebuild()
