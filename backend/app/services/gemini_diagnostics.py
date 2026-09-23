"""Google Generative Language / Imagen access diagnostics.

The Generative Language API reports almost every access problem as an opaque
``403 PERMISSION_DENIED``. This module turns that response — plus the far more
useful ``error.status`` and ``error.details[].reason`` fields Google returns —
into a concrete, actionable cause, and can actively probe the configured
image model to prove where access breaks.

Design constraints:

* **Never raises.** Every public entry point is best-effort: a diagnostic
  failure must never take down a request or application startup.
* **Never fabricates.** A probe reports what Google actually answered; it does
  not substitute an optimistic result when the API refuses access.
* **Redacted.** API keys and full key material are never logged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings
from app.utils.logger import logger

GENERATIVE_LANGUAGE_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Console deep-links that resolve each failure class a human has to fix.
CLOUD_CONSOLE_API_LIBRARY = (
    "https://console.cloud.google.com/apis/library/"
    "generativelanguage.googleapis.com"
)
CLOUD_CONSOLE_CREDENTIALS = "https://console.cloud.google.com/apis/credentials"
CLOUD_CONSOLE_BILLING = "https://console.cloud.google.com/billing"
CLOUD_CONSOLE_QUOTAS = (
    "https://console.cloud.google.com/apis/api/"
    "generativelanguage.googleapis.com/quotas"
)
GOOGLE_IMAGEN_DOCS = (
    "https://cloud.google.com/vertex-ai/generative-ai/docs/image/overview"
)

# Benign prompt used by the access probe; never shown to a customer.
_PROBE_PROMPT = (
    "A studio product photograph of a gold ring on a neutral background."
)


@dataclass(frozen=True)
class GoogleApiDiagnosis:
    """A structured, human-readable explanation of a Google API response."""

    ok: bool
    status_code: int
    google_status: str = ""
    reason: str = ""
    message: str = ""
    guidance: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """One-line, log-safe summary of the diagnosis."""
        if self.ok:
            return "Gemini Imagen access verified."
        label = self.reason or self.google_status or "unknown_error"
        detail = self.message.strip().replace("\n", " ")[:300]
        return f"status={self.status_code} reason={label} :: {detail}"

    def log_lines(self) -> List[str]:
        """Multi-line block suitable for ``logger.error`` output."""
        lines = [f"[GeminiDiagnostics] {self.summary}"]
        lines.extend(f"[GeminiDiagnostics]   → {item}" for item in self.guidance)
        return lines


# ─── Error parsing ───────────────────────────────────────────────────────


def _parse_google_error(body: str) -> Tuple[str, str, str]:
    """Extract ``(status, message, reason)`` from a Google error body.

    Falls back to the raw text so a non-JSON proxy/gateway response is still
    surfaced instead of being silently discarded.
    """
    if not body:
        return "", "", ""

    try:
        payload: Any = json.loads(body)
    except (TypeError, ValueError):
        return "", body.strip()[:500], ""

    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return "", body.strip()[:500], ""

    status = str(error.get("status") or "")
    message = str(error.get("message") or "")

    reason = ""
    details = error.get("details")
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            candidate = detail.get("reason")
            if isinstance(candidate, str) and candidate:
                reason = candidate
                break

    return status, message, reason


def interpret_google_error(status_code: int, body: str) -> GoogleApiDiagnosis:
    """Map a Generative Language response to an actionable diagnosis.

    Args:
        status_code: HTTP status returned by Google.
        body: Raw response body (JSON error envelope, or plain text).

    Returns:
        A :class:`GoogleApiDiagnosis`; ``ok`` is True only for a 2xx response.
    """
    if 200 <= status_code < 300:
        return GoogleApiDiagnosis(ok=True, status_code=status_code)

    google_status, message, reason = _parse_google_error(body)
    upper_status = google_status.upper()
    upper_reason = reason.upper()
    # Single normalised blob of everything Google told us, for keyword matching.
    blob = f"{upper_status} {upper_reason} {message.upper()}"

    def diagnosis(
        label: str,
        guidance: List[str],
        *,
        reason_override: Optional[str] = None,
    ) -> GoogleApiDiagnosis:
        return GoogleApiDiagnosis(
            ok=False,
            status_code=status_code,
            google_status=upper_status,
            reason=reason_override or label,
            message=message,
            guidance=guidance,
        )

    # ── API not enabled for the key's project ────────────────────────────
    if (
        upper_reason == "SERVICE_DISABLED"
        or "HAS NOT BEEN USED IN PROJECT" in blob
        or "IT IS DISABLED" in blob
        or "SERVICE_DISABLED" in blob
    ):
        return diagnosis(
            "service_disabled",
            [
                "The Generative Language API is not enabled on the project "
                "that owns GEMINI_API_KEY.",
                f"Enable it: {CLOUD_CONSOLE_API_LIBRARY}",
                "Wait ~1 minute for propagation, then restart the backend.",
            ],
        )

    # ── Key exists but is restricted away from this API ──────────────────
    if (
        upper_reason == "API_KEY_SERVICE_BLOCKED"
        or "API_KEY_SERVICE_BLOCKED" in blob
        or ("REQUESTS TO THIS API" in blob and "ARE BLOCKED" in blob)
    ):
        return diagnosis(
            "api_key_service_blocked",
            [
                "GEMINI_API_KEY is valid but its API restrictions exclude "
                "generativelanguage.googleapis.com.",
                "Either remove the restrictions or add the Generative "
                "Language API to the allowed services.",
                f"Edit the key: {CLOUD_CONSOLE_CREDENTIALS}",
            ],
        )

    # ── Key itself is not usable ─────────────────────────────────────────
    if status_code in (400, 401) and (
        "API_KEY_INVALID" in blob
        or "API KEY NOT VALID" in blob
        or "API KEY MISSING" in blob
        or upper_status == "UNAUTHENTICATED"
    ):
        return diagnosis(
            "api_key_invalid",
            [
                "GEMINI_API_KEY is missing, malformed, or has been revoked "
                "(401/400 API_KEY_INVALID).",
                f"Issue a fresh key: {CLOUD_CONSOLE_CREDENTIALS}",
                "Set it in backend/.env and restart the backend.",
            ],
        )

    # ── Billing disabled ─────────────────────────────────────────────────
    if "BILLING" in blob and status_code in (400, 403):
        return diagnosis(
            "billing_disabled",
            [
                "Billing is not enabled on the project backing "
                "GEMINI_API_KEY; image models require it.",
                f"Enable billing: {CLOUD_CONSOLE_BILLING}",
            ],
        )

    # ── Project-level access denial (region/policy flagged) ──────────────
    if (
        "PROJECT HAS BEEN DENIED" in blob
        or "DENIED ACCESS" in blob
        or "PROJECT DENIED" in blob
    ):
        return diagnosis(
            "project_denied",
            [
                "Google has denied this project access to the model — this is "
                "a project/reputation/region restriction, not a code issue.",
                "Confirm the project is in a supported region and has no "
                "outstanding policy or abuse flag.",
                f"Context: {GOOGLE_IMAGEN_DOCS}",
            ],
        )

    # ── Model not available to this key/project/region ───────────────────
    if status_code == 404 or upper_status == "NOT_FOUND":
        return diagnosis(
            "model_not_found",
            [
                "The configured image model was not found for this key. "
                "Imagen models are not exposed on every project or region.",
                "Verify GEMINI_IMAGE_MODEL and, where required, use Vertex AI "
                "(project + location + service account) instead of the "
                "Generative Language key.",
                f"Model reference: {GOOGLE_IMAGEN_DOCS}",
            ],
        )

    # ── Quota / rate limiting ────────────────────────────────────────────
    if status_code == 429 or upper_status == "RESOURCE_EXHAUSTED" or "QUOTA" in blob:
        return diagnosis(
            "quota_exhausted",
            [
                "The project has exhausted its image-generation quota "
                "(429 / RESOURCE_EXHAUSTED).",
                f"Review quotas: {CLOUD_CONSOLE_QUOTAS}",
                "This is not a permissions problem — capacity must be raised.",
            ],
        )

    # ── Transient upstream failure ───────────────────────────────────────
    if status_code >= 500:
        return diagnosis(
            "upstream_unavailable",
            [
                "Google returned a server-side error; this is transient.",
                "Retry later. If it persists, check Google Cloud status.",
            ],
        )

    # ── Generic 403 / PERMISSION_DENIED ──────────────────────────────────
    if status_code == 403 or upper_status == "PERMISSION_DENIED":
        return diagnosis(
            "permission_denied",
            [
                "Google refused the request for this project/key "
                "(403 PERMISSION_DENIED).",
                "Check, in order: (1) Generative Language API enabled, "
                "(2) key restrictions, (3) billing enabled, (4) the key's "
                "project is granted access to the Imagen model.",
                f"API library: {CLOUD_CONSOLE_API_LIBRARY}",
                f"Credentials: {CLOUD_CONSOLE_CREDENTIALS}",
            ],
        )

    return diagnosis(
        "unclassified_error",
        [
            "Google returned an error this module does not recognise.",
            "Inspect the raw message above; it is the authoritative cause.",
        ],
    )


# ─── Active probe ────────────────────────────────────────────────────────


async def probe_gemini_image_access(
    api_key: Optional[str] = None,
    model_name: Optional[str] = None,
    timeout: Optional[float] = None,
    billable: bool = True,
) -> GoogleApiDiagnosis:
    """Probe whether the configured key can actually generate an Imagen image.

    Runs a deliberate two-stage probe so the failure can be localised:

    1. ``GET /v1beta/models`` — isolates key/project scope. A failure here
       means nothing the key touches will work.
    2. ``POST /v1beta/models/{model}:predict`` — the exact call the provider
       makes. A failure here with a healthy stage 1 means the key is valid but
       the image model specifically is not granted.

    This issues one real (billable) prediction. It is gated by
    ``IMAGE_PROVIDER_STARTUP_DIAGNOSTICS`` at the call site and is intended for
    operator-facing diagnostics, not per-request use.
    """
    key = api_key if api_key is not None else settings.GEMINI_API_KEY
    model = (
        model_name
        or getattr(settings, "GEMINI_IMAGE_MODEL", None)
        or "gemini-3.1-flash-image"
    )
    effective_timeout = (
        timeout
        if timeout is not None
        else float(getattr(settings, "IMAGE_PROVIDER_DIAGNOSTIC_TIMEOUT_SECONDS", 10.0))
    )

    if not key:
        return GoogleApiDiagnosis(
            ok=False,
            status_code=0,
            reason="not_configured",
            message="GEMINI_API_KEY is empty.",
            guidance=[
                "Set GEMINI_API_KEY in backend/.env, or switch "
                "PRIMARY_IMAGE_PROVIDER away from gemini.",
            ],
        )

    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=effective_timeout) as client:
            # ── Stage 1: can the key list models at all? ─────────────────
            try:
                listing = await client.get(
                    f"{GENERATIVE_LANGUAGE_BASE}/models",
                    headers=headers,
                    params={"pageSize": 200},
                )
            except httpx.HTTPError as exc:
                return _network_diagnosis(exc)

            if listing.status_code != 200:
                return interpret_google_error(listing.status_code, listing.text)

            try:
                listed = listing.json().get("models")
            except ValueError:
                listed = None
            available_models = _model_ids(listed)

            if available_models and f"models/{model}" not in available_models:
                # A healthy listing that omits the model is a strong signal
                # that the image model is not exposed to this project.
                return GoogleApiDiagnosis(
                    ok=False,
                    status_code=listing.status_code,
                    reason="model_unavailable",
                    message=(
                        f"'{model}' is not advertised for this key, though "
                        f"{len(available_models)} other model(s) are."
                    ),
                    guidance=[
                        "The key works, but the configured Imagen model is not "
                        "granted to its project/region.",
                        "Confirm GEMINI_IMAGE_MODEL, or enable Imagen for the "
                        "project; Vertex AI is required for some models.",
                        f"Model reference: {GOOGLE_IMAGEN_DOCS}",
                    ],
                )

            if not billable:
                # Free check only (models.list). Stage 2 below is a real, billed
                # image generation -- never run it implicitly at process start.
                return GoogleApiDiagnosis(
                    ok=True,
                    status_code=listing.status_code,
                    message=f"'{model}' is reachable (listing check only, no billable call).",
                )

            # ── Stage 2: the exact generate call the provider makes ──────
            # Gemini image models (Nano Banana) and Imagen expose different
            # REST verbs: :generateContent vs :predict.
            if model.lower().startswith("imagen"):
                endpoint = f"{GENERATIVE_LANGUAGE_BASE}/models/{model}:predict"
                payload: Dict[str, Any] = {
                    "instances": [{"prompt": _PROBE_PROMPT}],
                    "parameters": {
                        "sampleCount": 1,
                        "aspectRatio": "1:1",
                        "outputMimeType": "image/jpeg",
                    },
                }
            else:
                endpoint = (
                    f"{GENERATIVE_LANGUAGE_BASE}/models/{model}:generateContent"
                )
                payload = {
                    "contents": [{"parts": [{"text": _PROBE_PROMPT}]}],
                    "generationConfig": {
                        "responseModalities": ["IMAGE"],
                        "imageConfig": {"aspectRatio": "1:1"},
                    },
                }

            try:
                prediction = await client.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                )
            except httpx.HTTPError as exc:
                return _network_diagnosis(exc)

            if prediction.status_code != 200:
                return interpret_google_error(
                    prediction.status_code, prediction.text
                )

            return GoogleApiDiagnosis(
                ok=True,
                status_code=prediction.status_code,
                message=f"Image generation access verified for '{model}'.",
            )

    except Exception as exc:  # pragma: no cover - defensive, must never raise
        return GoogleApiDiagnosis(
            ok=False,
            status_code=0,
            reason="probe_failed",
            message=f"{type(exc).__name__}: {exc}",
            guidance=["The access probe itself failed; see the message above."],
        )


def _model_ids(listed: Any) -> Optional[List[str]]:
    """Collect ``models/<id>`` names from a models.list payload, if present."""
    if not isinstance(listed, list):
        return None
    ids: List[str] = []
    for entry in listed:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name:
                ids.append(name)
    return ids


def _network_diagnosis(exc: httpx.HTTPError) -> GoogleApiDiagnosis:
    """Represent a transport-level failure (DNS, TLS, timeout) as a diagnosis."""
    return GoogleApiDiagnosis(
        ok=False,
        status_code=0,
        reason="network_error",
        message=f"{type(exc).__name__}: {exc}",
        guidance=[
            "The backend could not reach generativelanguage.googleapis.com.",
            "Check outbound network access, proxy/TLS settings, and firewall "
            "rules for the host running the backend.",
        ],
    )


# ─── Startup integration ─────────────────────────────────────────────────


# Probe at most once per process: the FastAPI lifespan runs on every
# TestClient(app) instantiation and every uvicorn worker, and each probe
# issues one real (billable) prediction.
_STARTUP_PROBE_DONE = False


async def log_startup_image_provider_diagnosis() -> Optional[GoogleApiDiagnosis]:
    """Probe Imagen access at boot and log an actionable cause.

    Best-effort and non-fatal: it logs a warning and returns ``None`` on any
    unexpected condition, so application startup is never blocked by it.

    Returns:
        The diagnosis when a probe ran, otherwise ``None``.
    """
    global _STARTUP_PROBE_DONE
    try:
        if _STARTUP_PROBE_DONE:
            return None
        if not getattr(settings, "IMAGE_PROVIDER_STARTUP_DIAGNOSTICS", False):
            return None
        _STARTUP_PROBE_DONE = True

        chain_uses_gemini = (
            (settings.PRIMARY_IMAGE_PROVIDER or "").strip().lower() == "gemini"
            or (settings.FALLBACK_IMAGE_PROVIDER or "").strip().lower() == "gemini"
        )
        if not chain_uses_gemini:
            return None

        if not settings.GEMINI_API_KEY:
            logger.warning(
                "[GeminiDiagnostics] GEMINI_API_KEY is not configured; the "
                "Gemini image provider cannot run."
            )
            return None

        # billable=False: this runs on every process start (every uvicorn
        # --reload, every worker, every TestClient) -- it must cost nothing.
        diagnosis = await probe_gemini_image_access(billable=False)

        if diagnosis.ok:
            logger.info(
                f"[GeminiDiagnostics] {diagnosis.summary}"
            )
        else:
            for line in diagnosis.log_lines():
                logger.error(line)
            logger.error(
                "[GeminiDiagnostics] Image generation via Gemini will not "
                "produce real output until the cause above is resolved."
            )

        return diagnosis
    except Exception as exc:  # pragma: no cover - startup must never fail
        logger.warning(f"[GeminiDiagnostics] Startup diagnosis skipped: {exc}")
        return None


__all__ = [
    "GoogleApiDiagnosis",
    "interpret_google_error",
    "probe_gemini_image_access",
    "log_startup_image_provider_diagnosis",
]
