"""
PharmaOS AI - Rate Limiting Middleware
IP-based in-memory rate limiter for API abuse prevention.
"""

import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Simple IP-based rate limiter using in-memory storage.
    Configurable limits for different route prefixes.
    """

    def __init__(self, app):
        super().__init__(app)
        self._stores: dict[str, dict[str, list[float]]] = {
            "auth": defaultdict(list),
            "upload": defaultdict(list),
            "api": defaultdict(list),
        }
        # (max_requests, window_seconds)
        self._limits = {
            "auth": (10, 300),       # 10 requests per 5 minutes
            "upload": (5, 3600),     # 5 uploads per hour
            "api": (100, 60),        # 100 requests per minute
        }

    def _get_client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _get_bucket(self, path: str) -> str:
        if "/auth/" in path:
            return "auth"
        if "/import/" in path or "/upload" in path:
            return "upload"
        return "api"

    async def dispatch(self, request: Request, call_next):
        ip = self._get_client_ip(request)
        bucket = self._get_bucket(request.url.path)
        store = self._stores[bucket]
        max_requests, window = self._limits[bucket]

        now = time.time()
        store[ip] = [t for t in store[ip] if now - t < window]

        if len(store[ip]) >= max_requests:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please try again later."},
                headers={"Retry-After": str(window)},
            )

        store[ip].append(now)
        return await call_next(request)
