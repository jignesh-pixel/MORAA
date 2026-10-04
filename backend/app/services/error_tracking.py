"""Optional error tracking with Sentry (OBS-3).

Off unless ``SENTRY_DSN`` is set. Needs the ``sentry-sdk`` package; if it is not installed the application logs a
warning and carries on (error tracking must never stop the app from starting). Personal data is never sent:
no request bodies, no cookies or auth headers, no customer text, and phone numbers in messages are masked.
"""

import re
from typing import Any, Dict, Optional

from app.config import settings
from app.utils.logger import logger

_PHONE_RE = re.compile(r"(?<![\w.])\+?(\d{2})[\d ()-]{4,14}(\d{4})(?![\w])")
_GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b")
_SECRET_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|rzp_(?:live|test)_[A-Za-z0-9]{6,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})")
_SENSITIVE_KEYS = ("authorization", "cookie", "x-hub-signature", "x-razorpay-signature", "token", "secret", "password",
                   "api_key", "apikey", "phone", "gstin", "name", "address", "email")
_MAX_DEPTH = 8


def _scrub_text(value: str) -> str:
    value = _SECRET_RE.sub("[secret]", value)
    value = _GSTIN_RE.sub("[gstin]", value)
    return _PHONE_RE.sub(r"******", value)


def _scrub_any(value: Any, depth: int = 0) -> Any:
    """Walk dicts/lists/strings (breadcrumbs, extra, contexts, tags): mask text and blank sensitive-looking keys."""
    if depth > _MAX_DEPTH:
        return "[deep]"
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, dict):
        return {k: ("[removed]" if isinstance(k, str) and any(s in k.lower() for s in _SENSITIVE_KEYS)
                    else _scrub_any(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_any(v, depth + 1) for v in value]
    return value


def scrub_event(event: Dict[str, Any], hint: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Remove personal and secret data from an event before it leaves the process."""
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)                  # request bodies carry names, GSTINs, addresses, payment payloads
        request.pop("cookies", None)
        request.pop("query_string", None)
        if isinstance(request.get("url"), str):
            request["url"] = _scrub_text(request["url"].split("?", 1)[0])
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {k: "[removed]" if any(s in k.lower() for s in _SENSITIVE_KEYS) else v
                                  for k, v in headers.items()}
    event.pop("user", None)
    for key in ("message", "logentry", "extra", "contexts", "tags", "breadcrumbs", "transaction"):
        if key in event:
            event[key] = _scrub_any(event[key])
    for exc in (event.get("exception") or {}).get("values", []) or []:
        if isinstance(exc.get("value"), str):
            exc["value"] = _scrub_text(exc["value"])
        for frame in ((exc.get("stacktrace") or {}).get("frames", []) or []):
            frame.pop("vars", None)                # local variables can hold anything
    return event


def init_error_tracking() -> bool:
    """Start Sentry when configured. Returns True when it is active. Never raises."""
    dsn = (getattr(settings, "SENTRY_DSN", "") or "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        logger.bind(category="system").warning(
            "SENTRY_DSN is set but the sentry-sdk package is not installed; error tracking is off. "
            "Install it with: pip install sentry-sdk"
        )
        return False
    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=settings.ENVIRONMENT,
            release=settings.APP_VERSION,
            send_default_pii=False,
            traces_sample_rate=0.0,
            max_request_body_size="never",
            before_send=scrub_event,
        )
        logger.bind(category="system").info("Error tracking (Sentry) is on")
        return True
    except Exception as e:  # noqa: BLE001
        logger.bind(category="system").warning(f"Error tracking could not start: {e}")
        return False
