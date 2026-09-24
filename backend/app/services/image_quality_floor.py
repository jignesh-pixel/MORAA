"""Deterministic post-generation image quality floor (P1·04).

A lightweight, Pillow-only sanity check run on a generated image before it
is treated as deliverable. This is NOT an aesthetic quality/CV model — it
only catches generations that are unreadable or badly undersized (e.g. a
corrupt or truncated image from a provider glitch). ``MIN_DIMENSION_PX`` is
set far below any realistic provider output size (OpenAI/Gemini image
generation both produce 1024px-class images), so it only ever flags
obviously broken output, not real aesthetic variation.

Sharpness is measured and reported but deliberately NOT used as a pass/fail
gate: there is no existing corpus of generated macro-shot images to derive
a defensible threshold from, and guessing one would violate the "no
arbitrary thresholds" rule this fix operates under. The score is exposed so
a real threshold can be set later once genuine output has been examined.
"""

import io
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageFilter, ImageStat

from app.utils.logger import logger

# Real provider output is 1024px-class; this floor only catches images that
# are obviously truncated/corrupt/placeholder-sized, never a real variation.
MIN_DIMENSION_PX = 256


@dataclass
class QualityFloorResult:
    passed: bool
    reason: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    sharpness_score: Optional[float] = None


def check_quality_floor(image_bytes: bytes) -> QualityFloorResult:
    """Run the deterministic quality floor on a generated image.

    Checks, in order: the bytes decode as an image, and the image meets a
    minimum dimension. Never raises — an internal error in the check itself
    is reported as a failure with reason ``"check_error"`` rather than
    silently passing a generation nothing actually verified.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception as e:
        logger.warning(f"Quality floor: image failed to decode: {e}")
        return QualityFloorResult(passed=False, reason="unreadable_image")

    width, height = img.size
    if width < MIN_DIMENSION_PX or height < MIN_DIMENSION_PX:
        return QualityFloorResult(
            passed=False,
            reason="below_minimum_dimensions",
            width=width,
            height=height,
        )

    sharpness_score: Optional[float] = None
    try:
        grayscale = img.convert("L")
        edges = grayscale.filter(ImageFilter.FIND_EDGES)
        sharpness_score = ImageStat.Stat(edges).stddev[0]
    except Exception as e:
        # Sharpness is best-effort telemetry, not a gate — never block a
        # decodable, correctly-sized image over a measurement failure.
        logger.warning(f"Quality floor: sharpness measurement failed: {e}")

    return QualityFloorResult(
        passed=True, width=width, height=height, sharpness_score=sharpness_score
    )
