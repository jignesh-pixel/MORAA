"""Pre-validation of a funded jewellery image (Scenario 4) -- BYPASSED.

Strict single-call policy: one customer action makes exactly ONE paid Gemini call, the image generation itself.
``check_image_quality`` therefore makes no network request and no Gemini call: every non-empty photo is approved
immediately. Photos are still decoded and validated locally (``meta_whatsapp_service.validate_image``) before
this runs.

The deterministic policy (``evaluate_inspection_payload``) and the fixed customer-facing rejection copy are kept,
so an inspector can be wired back in without changing callers.
"""

import re
from dataclasses import dataclass
from typing import Any, Dict

from app.utils.logger import logger

# ─── Reason codes ────────────────────────────────────────────────────────

REASON_MULTIPLE_PAIRS = "multiple_pairs"
REASON_MULTIPLE_ITEMS = "multiple_items"
REASON_NOT_JEWELLERY = "not_jewellery"
REASON_BLURRY = "blurry"
REASON_UNAVAILABLE = "inspector_unavailable"

REASON_LABELS: Dict[str, str] = {
    REASON_MULTIPLE_PAIRS: "more than one pair in the photo",
    REASON_MULTIPLE_ITEMS: "more than one piece of jewellery in the photo",
    REASON_NOT_JEWELLERY: "no jewellery item was detected",
    REASON_BLURRY: "the photo is too blurry to use",
    REASON_UNAVAILABLE: "the photo could not be checked right now",
}

# Fixed customer-facing copy (never built from model output).
REJECTION_MESSAGES: Dict[str, str] = {
    REASON_MULTIPLE_PAIRS: (
        "This image doesn’t meet our guidelines ❌\n\n"
        "We noticed more than one pair of earrings in the photo.\n"
        "Please resend a photo with just one pair clearly visible 📸"
    ),
    REASON_MULTIPLE_ITEMS: (
        "This image doesn’t meet our guidelines ❌\n\n"
        "We noticed more than one piece of jewellery in the photo.\n"
        "Please resend a photo with just one piece clearly visible 📸"
    ),
    REASON_NOT_JEWELLERY: (
        "This image doesn’t meet our guidelines ❌\n\n"
        "We couldn’t find a piece of jewellery in this photo.\n"
        "Please resend a clear photo of your jewellery 📸"
    ),
    REASON_BLURRY: (
        "This image doesn’t meet our guidelines ❌\n\n"
        "The photo is too blurry to work with.\n"
        "Please resend a sharp, well-lit photo 📸"
    ),
    REASON_UNAVAILABLE: (
        "We couldn’t check this photo right now ❌\n\n"
        "Please resend it in a moment and we’ll get started 📸"
    ),
}

DEFAULT_REJECTION_MESSAGE = (
    "This image doesn’t meet our guidelines ❌\n\n"
    "Please resend a clear photo of a single pair of jewellery 📸"
)

# ─── Result model ────────────────────────────────────────────────────────


@dataclass
class QualityCheckResult:
    """Outcome of the pre-generation quality inspection."""

    approved: bool
    reason: str = ""
    checked: bool = True
    is_jewellery: bool = False
    item_count: int = 0
    pair_count: int = 0
    is_blurry: bool = False
    confidence: float = 0.0

    @property
    def rejection_message(self) -> str:
        """Customer-facing rejection copy for this result's reason."""
        return build_rejection_message(self)


def build_rejection_message(result: QualityCheckResult) -> str:
    """Return the fixed customer-facing message for a failed inspection."""
    return REJECTION_MESSAGES.get(result.reason, DEFAULT_REJECTION_MESSAGE)


# ─── Defensive parsing helpers ───────────────────────────────────────────


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce an LLM field to a non-negative int without raising."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    if isinstance(value, str):
        match = re.search(r"-?\d+", value)
        if match:
            return max(int(match.group(0)), 0)
    return default


def _as_bool(value: Any, default: bool = False) -> bool:
    """Coerce an LLM field to a bool without raising."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "y", "1"}:
            return True
        if lowered in {"false", "no", "n", "0"}:
            return False
    return default


def _as_float(value: Any, default: float = 0.0) -> float:
    """Coerce an LLM field to a float in [0, 1] without raising."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, 0.0), 1.0)


def evaluate_inspection_payload(payload: Dict[str, Any]) -> QualityCheckResult:
    """Derive the approve/reject decision deterministically from the fields.

    Kept pure and separate from the API call so the policy is unit-testable
    and cannot drift with model behaviour.
    """
    is_jewellery = _as_bool(payload.get("is_jewellery"), default=False)
    item_count = _as_int(payload.get("item_count"))
    pair_count = _as_int(payload.get("pair_count"))
    is_blurry = _as_bool(payload.get("is_blurry"), default=False)
    confidence = _as_float(payload.get("confidence"))

    result = QualityCheckResult(
        approved=True,
        checked=True,
        is_jewellery=is_jewellery,
        item_count=item_count,
        pair_count=pair_count,
        is_blurry=is_blurry,
        confidence=confidence,
    )

    if not is_jewellery:
        result.approved = False
        result.reason = REASON_NOT_JEWELLERY
    elif pair_count > 1:
        result.approved = False
        result.reason = REASON_MULTIPLE_PAIRS
    elif item_count > 2:
        result.approved = False
        result.reason = REASON_MULTIPLE_ITEMS
    elif is_blurry:
        result.approved = False
        result.reason = REASON_BLURRY

    return result


# ─── Public API ──────────────────────────────────────────────────────────


async def check_image_quality(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> QualityCheckResult:
    """Approve a funded image immediately, with no network request and no Gemini call (strict single-call policy).

    Never raises. Only an empty payload is refused: there is no photo to generate from.
    """
    if not image_bytes:
        return QualityCheckResult(
            approved=False, checked=False, reason=REASON_UNAVAILABLE
        )

    logger.info(
        f"[API-AUDIT] Image pre-validation bypassed: 0 Gemini calls "
        f"(bytes={len(image_bytes)} mime={mime_type or 'unknown'})"
    )
    return QualityCheckResult(approved=True, checked=False)
