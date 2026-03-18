"""
PharmaOS AI - Authentication Endpoints
Registration, login, token refresh, email verification, Google OAuth,
phone OTP, and two-factor authentication.
"""

import hashlib
import logging
import random
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import (
    hash_password, verify_password,
    create_access_token, create_refresh_token,
    decode_token, get_current_user, TokenData,
    pwd_context,
)
from app.models.models import Organization, User, OrgType, UserRole
from app.schemas.schemas import (
    RegisterRequest, LoginRequest, TokenResponse, LoginResponse,
    RefreshRequest, UserResponse, OrgResponse,
    VerifyEmailRequest, ResendCodeRequest,
    GoogleAuthRequest,
    SendPhoneOtpRequest, VerifyPhoneRequest,
    Enable2FAResponse, Verify2FARequest,
)
from app.middleware.audit import log_auth_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])


# ═══════════════════════════════════════════════════════════════════════════
#  RATE LIMITING (in-memory)
# ═══════════════════════════════════════════════════════════════════════════

_rate_stores: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

RATE_LIMITS = {
    "login": (5, 300),           # 5 per 5 minutes
    "register": (3, 3600),       # 3 per hour
    "resend_code": (3, 3600),    # 3 per hour
    "send_otp": (3, 3600),      # 3 per hour
    "verify": (10, 3600),        # 10 per hour (brute force protection)
    "google": (10, 300),         # 10 per 5 minutes
    "enable_2fa": (5, 3600),     # 5 per hour
}


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(action: str, key: str) -> None:
    max_attempts, window = RATE_LIMITS.get(action, (100, 60))
    store = _rate_stores[action]
    now = time.time()
    store[key] = [t for t in store[key] if now - t < window]
    if len(store[key]) >= max_attempts:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(window)},
        )
    store[key].append(now)


# ═══════════════════════════════════════════════════════════════════════════
#  ACCOUNT LOCKOUT (10 failed verification attempts in 1 hour)
# ═══════════════════════════════════════════════════════════════════════════

LOCKOUT_MAX_FAILURES = 10
LOCKOUT_WINDOW_SECONDS = 3600  # 1 hour
_failed_verify_attempts: dict[str, list[float]] = defaultdict(list)


def _check_account_lockout(identifier: str) -> None:
    """Check if an account is locked due to too many failed verification attempts."""
    now = time.time()
    _failed_verify_attempts[identifier] = [
        t for t in _failed_verify_attempts[identifier] if now - t < LOCKOUT_WINDOW_SECONDS
    ]
    if len(_failed_verify_attempts[identifier]) >= LOCKOUT_MAX_FAILURES:
        raise HTTPException(
            status_code=423,
            detail="Account temporarily locked due to too many failed attempts. Try again later.",
            headers={"Retry-After": str(LOCKOUT_WINDOW_SECONDS)},
        )


def _record_failed_attempt(identifier: str) -> None:
    """Record a failed verification attempt for lockout tracking."""
    _failed_verify_attempts[identifier].append(time.time())


# ═══════════════════════════════════════════════════════════════════════════
#  OTP HELPERS (never log codes, always hash before storing)
# ═══════════════════════════════════════════════════════════════════════════

def _generate_otp() -> str:
    """Generate a cryptographically random 6-digit OTP."""
    return f"{secrets.randbelow(1000000):06d}"


def _hash_otp(code: str) -> str:
    """Hash OTP code with bcrypt for secure storage."""
    return pwd_context.hash(code)


def _verify_otp(code: str, hashed: str) -> bool:
    """Verify an OTP code against its bcrypt hash."""
    try:
        return pwd_context.verify(code, hashed)
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════
#  REGISTER & LOGIN
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/register", response_model=LoginResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Register a new organization and admin user. Sends email verification code."""
    ip = _get_client_ip(request)
    _check_rate_limit("register", ip)

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

    # Generate verification code
    code = _generate_otp()

    # Create admin user (not yet verified)
    user = User(
        org_id=org.id,
        email=payload.admin_email,
        password_hash=hash_password(payload.admin_password),
        full_name=payload.admin_full_name,
        role=admin_role,
        phone=payload.phone,
        is_verified=False,
        verification_hash=_hash_otp(code),
        verification_expires=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(user)
    await db.flush()

    # Send verification email (non-blocking failure)
    from app.services.email import send_verification_email
    send_verification_email(payload.admin_email, code, payload.admin_full_name)

    await log_auth_event(db, "register", payload.admin_email, True, ip, org.id, user.id)

    # Return tokens so user can access the app, but mark as unverified
    access_token = create_access_token(str(user.id), str(org.id), user.role.value)
    refresh_token = create_refresh_token(str(user.id), str(org.id))

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserResponse.model_validate(user),
        requires_verification=True,
        message="Registration successful. Please verify your email.",
        email=payload.admin_email,
    )


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Authenticate user and return JWT tokens."""
    ip = _get_client_ip(request)
    _check_rate_limit("login", ip)

    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(payload.password, user.password_hash):
        logger.warning("Failed login attempt for email: %s from IP: %s", payload.email, ip)
        await log_auth_event(db, "login_failed", payload.email, False, ip)
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated.")

    # Check email verification
    if not user.is_verified:
        return LoginResponse(
            requires_verification=True,
            message="Email not verified. Please check your inbox or request a new code.",
            email=payload.email,
        )

    # Check 2FA
    if user.two_factor_enabled:
        # Issue a short-lived temp token for 2FA step
        temp_token = create_access_token(
            str(user.id), str(user.org_id), user.role.value,
            expires_delta=timedelta(minutes=5),
        )
        return LoginResponse(
            requires_2fa=True,
            temp_token=temp_token,
            message="Two-factor authentication required.",
        )

    # Update last login
    user.last_login = datetime.now(timezone.utc)
    await db.flush()

    access_token = create_access_token(str(user.id), str(user.org_id), user.role.value)
    refresh_token = create_refresh_token(str(user.id), str(user.org_id))

    await log_auth_event(db, "login", payload.email, True, ip, user.org_id, user.id)

    return LoginResponse(
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


# ═══════════════════════════════════════════════════════════════════════════
#  EMAIL VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/verify-email", response_model=LoginResponse)
async def verify_email(payload: VerifyEmailRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Verify email with 6-digit code. Returns tokens on success."""
    ip = _get_client_ip(request)
    _check_rate_limit("verify", ip)
    _check_account_lockout(payload.email)

    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    # Generic message to prevent email enumeration
    generic_fail = "Invalid or expired verification code."

    if not user:
        _record_failed_attempt(payload.email)
        await log_auth_event(db, "verify_email_failed", payload.email, False, ip)
        raise HTTPException(status_code=400, detail=generic_fail)

    if user.is_verified:
        return LoginResponse(
            access_token=create_access_token(str(user.id), str(user.org_id), user.role.value),
            refresh_token=create_refresh_token(str(user.id), str(user.org_id)),
            user=UserResponse.model_validate(user),
            message="Email already verified.",
        )

    if not user.verification_hash or not user.verification_expires:
        raise HTTPException(status_code=400, detail=generic_fail)

    if datetime.now(timezone.utc) > user.verification_expires:
        raise HTTPException(status_code=400, detail="Verification code expired. Please request a new one.")

    if not _verify_otp(payload.code, user.verification_hash):
        _record_failed_attempt(payload.email)
        await log_auth_event(db, "verify_email_failed", payload.email, False, ip)
        raise HTTPException(status_code=400, detail=generic_fail)

    # Mark as verified, clear OTP data
    user.is_verified = True
    user.verification_hash = None
    user.verification_expires = None
    user.last_login = datetime.now(timezone.utc)
    await db.flush()

    await log_auth_event(db, "email_verified", payload.email, True, ip, user.org_id, user.id)

    access_token = create_access_token(str(user.id), str(user.org_id), user.role.value)
    refresh_token = create_refresh_token(str(user.id), str(user.org_id))

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserResponse.model_validate(user),
        message="Email verified successfully.",
    )


@router.post("/resend-code", response_model=dict)
async def resend_code(payload: ResendCodeRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Resend email verification code. Rate limited to 3/hour."""
    ip = _get_client_ip(request)
    _check_rate_limit("resend_code", ip)

    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    # Always return success to prevent email enumeration
    if not user or user.is_verified:
        return {"message": "If an account exists with that email, a verification code has been sent."}

    code = _generate_otp()
    user.verification_hash = _hash_otp(code)
    user.verification_expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    await db.flush()

    from app.services.email import send_verification_email
    send_verification_email(payload.email, code, user.full_name)

    return {"message": "If an account exists with that email, a verification code has been sent."}


# ═══════════════════════════════════════════════════════════════════════════
#  GOOGLE OAUTH
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/google", response_model=LoginResponse)
async def google_auth(payload: GoogleAuthRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Authenticate or register via Google OAuth id_token."""
    ip = _get_client_ip(request)
    _check_rate_limit("google", ip)

    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=501, detail="Google OAuth is not configured.")

    # Verify the id_token with Google
    from app.services.google_oauth import verify_google_token
    google_data = await verify_google_token(payload.id_token)

    if not google_data:
        await log_auth_event(db, "google_auth_failed", "unknown", False, ip)
        raise HTTPException(status_code=401, detail="Invalid Google token.")

    email = google_data["email"]
    google_id = google_data["sub"]

    # Check if user exists by google_id or email
    result = await db.execute(
        select(User).where((User.google_id == google_id) | (User.email == email))
    )
    user = result.scalar_one_or_none()

    if user:
        # Existing user — login
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account is deactivated.")

        # Link Google ID if not already linked
        if not user.google_id:
            user.google_id = google_id
        if google_data.get("picture") and not user.avatar_url:
            user.avatar_url = google_data["picture"]

        user.is_verified = True  # Google already verified email
        user.last_login = datetime.now(timezone.utc)
        await db.flush()

        await log_auth_event(db, "google_login", email, True, ip, user.org_id, user.id)

        # Check 2FA
        if user.two_factor_enabled:
            temp_token = create_access_token(
                str(user.id), str(user.org_id), user.role.value,
                expires_delta=timedelta(minutes=5),
            )
            return LoginResponse(requires_2fa=True, temp_token=temp_token)

        access_token = create_access_token(str(user.id), str(user.org_id), user.role.value)
        refresh_token_str = create_refresh_token(str(user.id), str(user.org_id))

        return LoginResponse(
            access_token=access_token,
            refresh_token=refresh_token_str,
            user=UserResponse.model_validate(user),
        )
    else:
        # New user — auto-register
        org_name = payload.org_name or f"{google_data.get('name', 'User')}'s Organization"
        org = Organization(
            name=org_name,
            org_type=OrgType(payload.org_type),
            email=email,
        )
        db.add(org)
        await db.flush()

        role_map = {
            "pharmacy": UserRole.pharmacy_admin,
            "distributor": UserRole.distributor_admin,
            "wholesaler": UserRole.distributor_admin,
        }
        admin_role = role_map.get(payload.org_type, UserRole.pharmacy_admin)

        # Generate a random password hash (user won't use password login)
        random_pw_hash = hash_password(secrets.token_urlsafe(32))

        user = User(
            org_id=org.id,
            email=email,
            password_hash=random_pw_hash,
            full_name=google_data.get("name", email.split("@")[0]),
            role=admin_role,
            is_verified=True,  # Google already verified
            google_id=google_id,
            avatar_url=google_data.get("picture"),
        )
        db.add(user)
        await db.flush()

        await log_auth_event(db, "google_register", email, True, ip, org.id, user.id)

        access_token = create_access_token(str(user.id), str(org.id), user.role.value)
        refresh_token_str = create_refresh_token(str(user.id), str(org.id))

        return LoginResponse(
            access_token=access_token,
            refresh_token=refresh_token_str,
            user=UserResponse.model_validate(user),
            message="Account created via Google.",
        )


# ═══════════════════════════════════════════════════════════════════════════
#  PHONE OTP VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/send-phone-otp", response_model=dict)
async def send_phone_otp(
    payload: SendPhoneOtpRequest,
    request: Request,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a 6-digit OTP to the user's phone via SMS."""
    ip = _get_client_ip(request)
    _check_rate_limit("send_otp", payload.phone)

    result = await db.execute(select(User).where(User.id == current_user.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    code = _generate_otp()
    user.phone = payload.phone
    user.phone_otp_hash = _hash_otp(code)
    user.phone_otp_expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    await db.flush()

    # Send SMS
    from app.services.sms import send_otp_sms
    sent = await send_otp_sms(payload.phone, code)

    # Always return generic message
    return {"message": "If the phone number is valid, an OTP has been sent."}


@router.post("/verify-phone", response_model=dict)
async def verify_phone(
    payload: VerifyPhoneRequest,
    request: Request,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify phone number with OTP code."""
    ip = _get_client_ip(request)
    _check_rate_limit("verify", ip)
    _check_account_lockout(payload.phone)

    result = await db.execute(select(User).where(User.id == current_user.user_id))
    user = result.scalar_one_or_none()

    generic_fail = "Invalid or expired OTP code."

    if not user:
        raise HTTPException(status_code=400, detail=generic_fail)

    if not user.phone_otp_hash or not user.phone_otp_expires:
        raise HTTPException(status_code=400, detail=generic_fail)

    if datetime.now(timezone.utc) > user.phone_otp_expires:
        raise HTTPException(status_code=400, detail="OTP expired. Please request a new one.")

    if not _verify_otp(payload.code, user.phone_otp_hash):
        _record_failed_attempt(payload.phone)
        await log_auth_event(db, "verify_phone_failed", user.email, False, ip, user.org_id, user.id)
        raise HTTPException(status_code=400, detail=generic_fail)

    user.phone_verified = True
    user.phone_otp_hash = None
    user.phone_otp_expires = None
    await db.flush()

    await log_auth_event(db, "phone_verified", user.email, True, ip, user.org_id, user.id)

    return {"message": "Phone number verified successfully.", "phone_verified": True}


# ═══════════════════════════════════════════════════════════════════════════
#  TWO-FACTOR AUTHENTICATION (TOTP)
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/enable-2fa", response_model=Enable2FAResponse)
async def enable_2fa(
    request: Request,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Enable 2FA. Returns TOTP secret and QR code URL for authenticator apps."""
    ip = _get_client_ip(request)
    _check_rate_limit("enable_2fa", ip)

    result = await db.execute(select(User).where(User.id == current_user.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    if user.two_factor_enabled:
        raise HTTPException(status_code=400, detail="2FA is already enabled.")

    from app.services.totp import generate_totp_secret, encrypt_secret, get_otpauth_uri

    secret = generate_totp_secret()
    encrypted = encrypt_secret(secret)
    user.two_factor_secret_encrypted = encrypted
    await db.flush()

    otpauth_uri = get_otpauth_uri(secret, user.email)

    return Enable2FAResponse(
        secret=secret,
        otpauth_uri=otpauth_uri,
        qr_code_url=f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={otpauth_uri}",
    )


@router.post("/confirm-2fa", response_model=dict)
async def confirm_2fa(
    payload: Verify2FARequest,
    request: Request,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Confirm 2FA setup by verifying a TOTP code. Enables 2FA on the account."""
    ip = _get_client_ip(request)
    _check_rate_limit("verify", ip)

    result = await db.execute(select(User).where(User.id == current_user.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    if not user.two_factor_secret_encrypted:
        raise HTTPException(status_code=400, detail="Call /enable-2fa first.")

    from app.services.totp import decrypt_secret, verify_totp

    _check_account_lockout(str(user.id))

    secret = decrypt_secret(user.two_factor_secret_encrypted)
    if not verify_totp(secret, payload.code):
        _record_failed_attempt(str(user.id))
        await log_auth_event(db, "confirm_2fa_failed", user.email, False, ip, user.org_id, user.id)
        raise HTTPException(status_code=400, detail="Invalid 2FA code.")

    user.two_factor_enabled = True
    await db.flush()

    await log_auth_event(db, "2fa_enabled", user.email, True, ip, user.org_id, user.id)

    return {"message": "Two-factor authentication enabled successfully.", "two_factor_enabled": True}


@router.post("/verify-2fa", response_model=LoginResponse)
async def verify_2fa(payload: Verify2FARequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Verify 2FA TOTP code during login. Requires temp_token from login response."""
    ip = _get_client_ip(request)
    _check_rate_limit("verify", ip)

    if not payload.temp_token:
        raise HTTPException(status_code=400, detail="temp_token is required for 2FA verification.")

    # Decode the temp token
    token_data = decode_token(payload.temp_token)
    user_id = token_data.get("sub")

    _check_account_lockout(str(user_id))

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.two_factor_enabled or not user.two_factor_secret_encrypted:
        raise HTTPException(status_code=400, detail="Invalid 2FA verification request.")

    from app.services.totp import decrypt_secret, verify_totp

    secret = decrypt_secret(user.two_factor_secret_encrypted)
    if not verify_totp(secret, payload.code):
        _record_failed_attempt(str(user_id))
        await log_auth_event(db, "2fa_verify_failed", user.email, False, ip, user.org_id, user.id)
        raise HTTPException(status_code=400, detail="Invalid 2FA code.")

    user.last_login = datetime.now(timezone.utc)
    await db.flush()

    await log_auth_event(db, "2fa_login", user.email, True, ip, user.org_id, user.id)

    access_token = create_access_token(str(user.id), str(user.org_id), user.role.value)
    refresh_token_str = create_refresh_token(str(user.id), str(user.org_id))

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token_str,
        user=UserResponse.model_validate(user),
        message="2FA verification successful.",
    )


@router.post("/disable-2fa", response_model=dict)
async def disable_2fa(
    request: Request,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Disable 2FA on the current account."""
    result = await db.execute(select(User).where(User.id == current_user.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    user.two_factor_enabled = False
    user.two_factor_secret_encrypted = None
    await db.flush()

    ip = _get_client_ip(request)
    await log_auth_event(db, "2fa_disabled", user.email, True, ip, user.org_id, user.id)

    return {"message": "Two-factor authentication disabled.", "two_factor_enabled": False}
