"""AI pre-validation of a funded jewellery image (Scenario 4).

Before any generation credits are spent, a funded image is inspected by a
fast Gemini model:

    * it must be jewellery (earrings, studs, jhumkas, rings, …)
    * it must contain exactly ONE pair (two pieces) or ONE single piece
    * it must not be blurry / unusable

The model is asked for strict JSON only, and the APPROVE/REJECT decision is
then derived deterministically in Python from those numbers — the model is
never trusted to return a boolean verdict on its own.

Failure policy: when the inspector cannot run (no API key, ``google-genai``
missing, API outage, unusable JSON) the result follows
``settings.IMAGE_PREVALIDATION_FAIL_OPEN`` — by default the image is allowed
through, because wrongly blocking a paying customer is worse than one wasted
generation. Set ``IMAGE_PREVALIDATION_FAIL_OPEN=false`` to reject instead.

No customer-facing message ever contains raw model output: rejections use the
fixed templates in this module.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.config import settings
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

VALIDATION_PROMPT = (
    "You are a strict quality inspector for a jewellery photo studio. "
    "Inspect the attached image and return STRICT JSON only — no markdown, "
    "no code fences, no explanation.\n\n"
    "Return exactly this object:\n"
    '{"is_jewellery": true, "item_count": 1, "pair_count": 1, '
    '"is_blurry": false, "confidence": 0.0}\n\n'
    "Field rules:\n"
    "- is_jewellery: true only when the image clearly shows a piece of "
    "jewellery (earrings, studs, jhumkas, ring, necklace, bracelet, pendant).\n"
    "- item_count: number of separate jewellery pieces visible "
    "(one pair of earrings = 2 pieces, one single stud = 1).\n"
    "- pair_count: number of complete pairs visible (0 for a single piece).\n"
    "- is_blurry: true when the image is too blurry, dark or cropped to judge "
    "the product.\n"
    "- confidence: your confidence in this assessment, 0.0 to 1.0.\n\n"
    "Do not guess: count only what is clearly visible."
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


def _parse_json_object(raw: Any) -> Optional[Dict[str, Any]]:
    """Extract a JSON object from an LLM response (code fences tolerated)."""
    if not isinstance(raw, str) or not raw.strip():
        return None

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        parsed = json.loads(cleaned[start : end + 1])
    except Exception:
        return None

    return parsed if isinstance(parsed, dict) else None


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


def _unavailable_result() -> QualityCheckResult:
    """Result used when the inspector could not run at all."""
    fail_open = bool(settings.IMAGE_PREVALIDATION_FAIL_OPEN)
    if fail_open:
        logger.warning(
            "Image pre-validation unavailable — allowing the image through "
            "(IMAGE_PREVALIDATION_FAIL_OPEN=true)"
        )
    else:
        logger.warning(
            "Image pre-validation unavailable — rejecting the image "
            "(IMAGE_PREVALIDATION_FAIL_OPEN=false)"
        )
    return QualityCheckResult(
        approved=fail_open,
        checked=False,
        reason=REASON_UNAVAILABLE,
    )


# ─── Public API ──────────────────────────────────────────────────────────


async def check_image_quality(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> QualityCheckResult:
    """Run the fast Gemini quality inspection on a funded image.

    Never raises: any failure resolves through the fail-open/fail-closed
    setting. Returns a ``QualityCheckResult`` whose ``approved`` flag tells the
    caller whether the image may enter the paid generation pipeline.
    """
    if not image_bytes:
        return QualityCheckResult(
            approved=False, checked=False, reason=REASON_UNAVAILABLE
        )

    if not settings.GEMINI_API_KEY:
        return _unavailable_result()

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        logger.warning(
            "Image pre-validation: google-genai not installed — using fallback policy"
        )
        return _unavailable_result()

    model_name = settings.IMAGE_PREVALIDATION_MODEL or "gemini-3.6-flash"

    try:
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        image_part = types.Part.from_bytes(
            data=image_bytes,
            mime_type=mime_type or "image/jpeg",
        )
        config = types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
        )

        # Mirror GeminiImageProvider: run the sync SDK call in a thread.
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model_name,
            contents=[VALIDATION_PROMPT, image_part],
            config=config,
        )

        payload = _parse_json_object(getattr(response, "text", None))
        if not payload:
            logger.warning("Image pre-validation: model returned no usable JSON")
            return _unavailable_result()

        result = evaluate_inspection_payload(payload)
        logger.info(
            f"Image pre-validation: approved={result.approved} "
            f"reason={result.reason or 'ok'} items={result.item_count} "
            f"pairs={result.pair_count} blurry={result.is_blurry} "
            f"confidence={result.confidence:.2f}"
        )
        return result

    except Exception as e:
        # Error summary only — no secrets, no image data.
        logger.error(f"Image pre-validation failed: {e}")
        return _unavailable_result()
