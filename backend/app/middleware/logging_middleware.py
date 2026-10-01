"""Request logging middleware for API calls."""

import time
import uuid

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.utils.logger import logger, safe_log


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log all incoming requests and their response times."""

    async def dispatch(self, request: Request, call_next):
        """Process request, log it, and return response."""
        # Generate request ID
        request_id = str(uuid.uuid4())[:8]
        request.state.request_id = request_id

        # Capture start time
        start_time = time.time()

        # Log request
        # scope["path"] is the routed path (request.url is rebuilt from the
        # Host header). Values are passed as arguments, never formatted into
        # the template, so a path containing "{x}" cannot break logging.
        path = safe_log(request.scope.get("path", ""))
        api_log = logger.bind(category="api")
        api_log.info("→ [{}] {} {}", request_id, request.method, path)

        try:
            response = await call_next(request)

            # Calculate duration
            duration_ms = (time.time() - start_time) * 1000

            # Log response
            api_log.info(
                "← [{}] {} {} → {} ({:.1f}ms)",
                request_id, request.method, path, response.status_code, duration_ms,
            )

            # Add request ID to response headers
            response.headers["X-Request-ID"] = request_id
            return response

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            api_log.error(
                "✗ [{}] {} {} → ERROR ({:.1f}ms): {}",
                request_id, request.method, path, duration_ms, str(e),
            )
            raise


def setup_logging_middleware(app: FastAPI) -> None:
    """Add request logging middleware."""
    app.add_middleware(RequestLoggingMiddleware)
