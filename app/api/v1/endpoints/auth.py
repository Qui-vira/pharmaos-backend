"""
PharmaOS AI - Authentication Endpoints
Registration, login, token refresh, and profile.
"""

import time
import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import (
    hash_password, verify_password,
    create_access_token, create_refresh_token,
    decode_token, get_current_user, TokenData,
)
from app.models.models import Organization, User, OrgType, UserRole
from app.schemas.schemas import (
    RegisterRequest, LoginRequest, TokenResponse,
    RefreshRequest, UserResponse, OrgResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])


# ─── Rate Limiting (in-memory) ─────────────────────────────────────────────

_login_attempts: dict[str, list[float]] = defaultdict(list)
_register_attempts: dict[str, list[float]] = defaultdict(list)

LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300  # 5 minutes

REGISTER_MAX_ATTEMPTS = 3
REGISTER_WINDOW_SECONDS = 3600  # 1 hour


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(
    store: dict[str, list[float]], ip: str, max_attempts: int, window: int
) -> None:
    now = time.time()
    # Clean expired entries
    store[ip] = [t for t in store[ip] if now - t < window]
    if len(store[ip]) >= max_attempts:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(window)},
        )
    store[ip].append(now)


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Register a new organization and its admin user."""
    ip = _get_client_ip(request)
    _check_rate_limit(_register_attempts, ip, REGISTER_MAX_ATTEMPTS, REGISTER_WINDOW_SECONDS)

    # Check if email already exists
    existing = await db.execute(select(User).where(User.email == payload.admin_email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered.")

    # Create organization
    org = Organization(
        name=payload.org_name,
        org_type=OrgType(payload.org_type),
        phone=payload.phone,
        email=payload.admin_email,
        address=payload.address,
        city=payload.city,
        state=payload.state,
        license_number=payload.license_number,
    )
    db.add(org)
    await db.flush()

    # Determine admin role based on org type
    role_map = {
        "pharmacy": UserRole.pharmacy_admin,
        "distributor": UserRole.distributor_admin,
        "wholesaler": UserRole.distributor_admin,
    }
    admin_role = role_map.get(payload.org_type, UserRole.pharmacy_admin)

    # Create admin user
    user = User(
        org_id=org.id,
        email=payload.admin_email,
        password_hash=hash_password(payload.admin_password),
        full_name=payload.admin_full_name,
        role=admin_role,
        phone=payload.phone,
    )
    db.add(user)
    await db.flush()

    # Generate tokens
    access_token = create_access_token(str(user.id), str(org.id), user.role.value)
    refresh_token = create_refresh_token(str(user.id), str(org.id))

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserResponse.model_validate(user),
    )


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Authenticate user and return JWT tokens."""
    ip = _get_client_ip(request)
    _check_rate_limit(_login_attempts, ip, LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS)

    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(payload.password, user.password_hash):
        logger.warning("Failed login attempt for email: %s from IP: %s", payload.email, ip)
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated.")

    # Update last login
    user.last_login = datetime.now(timezone.utc)
    await db.flush()

    access_token = create_access_token(str(user.id), str(user.org_id), user.role.value)
    refresh_token = create_refresh_token(str(user.id), str(user.org_id))

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserResponse.model_validate(user),
    )


@router.post("/refresh", response_model=dict)
async def refresh_token(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    """Exchange a refresh token for a new access token."""

    token_data = decode_token(payload.refresh_token)

    if token_data.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token.")

    user_id = token_data["sub"]
    org_id = token_data["org_id"]

    # Verify user still exists and is active
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive.")

    new_access = create_access_token(str(user.id), str(user.org_id), user.role.value)

    return {"access_token": new_access, "token_type": "bearer"}


@router.get("/me", response_model=UserResponse)
async def get_me(
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current user profile."""
    result = await db.execute(select(User).where(User.id == current_user.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return UserResponse.model_validate(user)
