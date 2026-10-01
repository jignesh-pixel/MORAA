"""Simple in-memory rate limiting middleware."""

import time
from collections import defaultdict
from typing import Dict, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.middleware.public_host_guard import PUBLIC_ALLOWED_PATHS
from app.utils.logger import logger

# Never rate limited:
# - the provider webhooks: Meta and Razorpay call from a handful of IPs (one
#   bucket would 429 real customers' messages and payments at ~100/min) and
#   every payload is signature-verified by its route;
# - the health probe;
# - static customer images under /uploads (one page view is many hits).
_EXEMPT_PATHS = frozenset(PUBLIC_ALLOWED_PATHS | {"/health"})
_EXEMPT_PREFIXES = ("/uploads/",)


def is_rate_limit_exempt(path: str) -> bool:
    """True for paths the limiter never counts."""
    normalized = path.rstrip("/") or "/"
    return normalized in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory rate limiter based on client IP."""

    def __init__(self, app):
        super().__init__(app)
        self._request_counts: Dict[str, list] = defaultdict(list)
        self.max_requests = settings.RATE_LIMIT_REQUESTS
        self.window_seconds = settings.RATE_LIMIT_WINDOW_SECONDS
        self._last_prune = 0.0

    async def dispatch(self, request: Request, call_next):
        """Check rate limit before processing request."""
        if not settings.RATE_LIMIT_ENABLED:
            return await call_next(request)

        if is_rate_limit_exempt(request.url.path):
            return await call_next(request)

        # Get client IP
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        # Drop timestamps outside the window, and whole idle IPs, so the
        # table cannot grow without bound.
        self._prune(now)
        self._request_counts[client_ip] = [
            ts for ts in self._request_counts[client_ip]
            if now - ts < self.window_seconds
        ]

        # Check limit
        if len(self._request_counts[client_ip]) >= self.max_requests:
            logger.warning(
                f"Rate limit exceeded for {client_ip}",
                extra={"category": "api"},
            )
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Too many requests. Please try again later.",
                    "retry_after": self.window_seconds,
                },
                headers={"Retry-After": str(self.window_seconds)},
            )

        # Add current request
        self._request_counts[client_ip].append(now)
        return await call_next(request)

    def _prune(self, now: float) -> None:
        """Forget IPs whose newest request is outside the window (at most once per window)."""
        if now - self._last_prune < self.window_seconds:
            return
        self._last_prune = now
        stale = [
            ip for ip, stamps in self._request_counts.items()
            if not stamps or now - stamps[-1] >= self.window_seconds
        ]
        for ip in stale:
            del self._request_counts[ip]


def setup_rate_limit(app: FastAPI) -> None:
    """Add rate limiting middleware."""
    app.add_middleware(RateLimitMiddleware)
