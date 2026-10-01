"""Shared Celery retry policy for tasks that make paid AI calls."""

import httpx

# Failures worth retrying: the call may succeed a moment later. Anything else
# (missing record, unreadable image, bad prompt, parse error) fails the same
# way every time, and each retry would re-run a paid AI call for nothing.
TRANSIENT_ERRORS = (ConnectionError, TimeoutError, httpx.TransportError)
