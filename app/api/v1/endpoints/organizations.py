"""
PharmaOS AI - Organization & User Management Endpoints
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_roles, hash_password, TokenData
from app.models.models import Organization, User, UserRole
from app.schemas.schemas import (
    OrgResponse, OrgUpdateRequest,
    UserResponse, UserCreateRequest, UserUpdateRequest,
)
from app.middleware.audit import log_audit

router = APIRouter(prefix="/orgs", tags=["Organizations"])


@router.get("/me", response_model=OrgResponse)
async def get_my_org(
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current organization details."""
    result = await db.execute(select(Organization).where(Organization.id == current_user.org_id))
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return OrgResponse.model_validate(org)


@router.put("/me", response_model=OrgResponse)
async def update_my_org(
    payload: OrgUpdateRequest,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "distributor_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update current organization settings."""
    result = await db.execute(select(Organization).where(Organization.id == current_user.org_id))
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(org, field, value)

    await log_audit(db, current_user.org_id, current_user.user_id, "update", "organization", org.id, update_data)
    await db.flush()
    return OrgResponse.model_validate(org)


@router.get("/me/users", response_model=list[UserResponse])
async def list_org_users(
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "distributor_admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all users in the current organization."""
    result = await db.execute(
        select(User).where(User.org_id == current_user.org_id).order_by(User.created_at.desc())
    )
    users = result.scalars().all()
    return [UserResponse.model_validate(u) for u in users]


@router.post("/me/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_org_user(
    payload: UserCreateRequest,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "distributor_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Invite a new user to the organization."""

    # Validate role is appropriate for org type
    result = await db.execute(select(Organization).where(Organization.id == current_user.org_id))
    org = result.scalar_one_or_none()

    pharmacy_roles = {"pharmacy_admin", "pharmacist", "cashier"}
    distributor_roles = {"distributor_admin", "warehouse_staff", "sales_rep"}

    if org.org_type.value in ("pharmacy",) and payload.role not in pharmacy_roles:
        raise HTTPException(status_code=400, detail=f"Role '{payload.role}' not valid for pharmacy organizations.")
    if org.org_type.value in ("distributor", "wholesaler") and payload.role not in distributor_roles:
        raise HTTPException(status_code=400, detail=f"Role '{payload.role}' not valid for distributor organizations.")

    # Check email uniqueness
    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered.")

    user = User(
        org_id=current_user.org_id,
        email=payload.email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        role=UserRole(payload.role),
        phone=payload.phone,
    )
    db.add(user)
    await db.flush()

    await log_audit(db, current_user.org_id, current_user.user_id, "create", "user", user.id)
    return UserResponse.model_validate(user)


@router.put("/me/users/{user_id}", response_model=UserResponse)
async def update_org_user(
    user_id: str,
    payload: UserUpdateRequest,
    current_user: TokenData = Depends(require_roles("pharmacy_admin", "distributor_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update a user in the organization."""
    result = await db.execute(
        select(User).where(User.id == user_id, User.org_id == current_user.org_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found in your organization.")

    update_data = payload.model_dump(exclude_unset=True)

    if "role" in update_data:
        update_data["role"] = UserRole(update_data["role"])

    for field, value in update_data.items():
        setattr(user, field, value)

    await log_audit(db, current_user.org_id, current_user.user_id, "update", "user", user.id, update_data)
    await db.flush()
    return UserResponse.model_validate(user)
