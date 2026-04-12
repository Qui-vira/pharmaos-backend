"""
PharmaOS AI - Main Application Entry Point
FastAPI application with middleware, CORS, and route mounting.
"""

import asyncio
import logging
import sys
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# ─── LOGGING SETUP — MUST be before any other app imports ─────────────────────
# Force root logger to INFO so all logger.info() calls are visible in Railway.
# Without this, Python defaults to WARNING and drops all INFO/DEBUG lines.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,  # Railway captures stdout
    force=True,  # Override any existing root logger config
)
# Also force uvicorn's loggers to INFO
for _uv_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    logging.getLogger(_uv_logger_name).setLevel(logging.INFO)

from app.core.config import settings
from app.core.database import engine, Base
from app.api.v1.router import api_router
from app.middleware.rate_limit import RateLimitMiddleware

logger = logging.getLogger(__name__)
logger.info("APP_STARTUP: logging configured at INFO level, output to stdout")

# Background reminder scheduler task handle
_reminder_task: asyncio.Task = None


async def _reminder_scheduler_loop():
    """
    Background task that runs the reminder engine every 15 minutes.
    Fallback for when Celery beat is not available (e.g. Railway).
    """
    from app.core.database import async_session_factory
    from app.services.reminder_engine import run_reminder_cycle

    logger.info("Reminder scheduler started (15-min interval)")
    while True:
        try:
            await asyncio.sleep(900)  # 15 minutes
            async with async_session_factory() as db:
                stats = await run_reminder_cycle(db)
                await db.commit()
            logger.info("Reminder scheduler cycle: %s", stats)
        except asyncio.CancelledError:
            logger.info("Reminder scheduler stopped")
            break
        except Exception:
            logger.exception("Reminder scheduler error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown events."""
    global _reminder_task

    # Startup: Create tables if they don't exist (dev only — use Alembic in production)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Re-emit WhatsApp diagnostic now that logging is properly configured.
    # The module-level WA_INIT in whatsapp.py fires at import time, which may be
    # before basicConfig runs. This ensures it shows up in Railway logs.
    from app.services.whatsapp import whatsapp_service, WHATSAPP_API_BASE
    _phone_id = whatsapp_service.phone_number_id or ""
    _token = whatsapp_service.access_token or ""
    logger.info(
        "WA_INIT (startup): configured=%s phone_number_id=%s token_len=%d token_prefix=%s api=%s",
        whatsapp_service.is_configured,
        _phone_id if _phone_id else "MISSING",
        len(_token),
        _token[:6] + "..." if _token else "NONE",
        WHATSAPP_API_BASE,
    )

    # Start background reminder scheduler as a fallback.
    # Set DISABLE_REMINDER_SCHEDULER=true if using Celery beat instead.
    if not getattr(settings, "DISABLE_REMINDER_SCHEDULER", False):
        _reminder_task = asyncio.create_task(_reminder_scheduler_loop())

    yield

    # Shutdown
    if _reminder_task and not _reminder_task.done():
        _reminder_task.cancel()
        try:
            await _reminder_task
        except asyncio.CancelledError:
            pass
    await engine.dispose()


# Disable API docs in production
docs_url = "/docs" if settings.DEBUG else None
redoc_url = "/redoc" if settings.DEBUG else None

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "Multi-tenant SaaS platform for pharmacies, distributors, and wholesalers. "
        "AI-powered inventory management, consultation system, and smart ordering."
    ),
    docs_url=docs_url,
    redoc_url=redoc_url,
    lifespan=lifespan,
)


# ─── Security Headers Middleware ─────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


# ─── Middleware (order matters: last added = first executed) ─────────────────

app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

# CORS must be added LAST so it executes FIRST (FastAPI reverses middleware order).
# This ensures OPTIONS preflight requests get CORS headers before any other middleware.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)


# ─── Global Exception Handler ───────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Hide stack traces in production. Show details only when DEBUG=True."""
    if settings.DEBUG:
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc), "traceback": traceback.format_exc()},
        )
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred. Please try again later."},
    )


# ─── Routes ─────────────────────────────────────────────────────────────────

app.include_router(api_router, prefix=settings.API_PREFIX)


@app.get("/", tags=["Health"])
async def root():
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "operational",
    }


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "healthy"}
