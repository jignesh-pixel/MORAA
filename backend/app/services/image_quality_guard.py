"""AI Quality Guard for raw jewelry photos using Gemini Vision."""

import base64
import json
from typing import Tuple
import httpx

from app.config import settings
from app.utils.logger import logger

GUARD_PROMPT = """
Analyze this uploaded raw jewelry photo strictly against 3 rules for Moraa Studio:
1. Subject: Must contain jewelry, specifically earrings.
2. Quantity: Must show ONLY ONE pair of earrings (or a single earring). Reject clusters or multiple distinct pairs.
3. Quality: Must NOT be severely blurry, pixelated, or pitch dark.

Return ONLY a valid JSON object matching this schema:
{
  "is_valid": true,
  "rejection_reason": "clear concise reason if invalid, else empty",
  "tip": "helpful 1-sentence tip for the customer if invalid, else empty"
}
"""


async def validate_jewelry_image_with_gemini(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> Tuple[bool, str]:
    """Validate image clarity and earring constraints via Gemini Vision.
    Fails open on timeout or API issues to prevent blocking the user pipeline.
    """
    if not settings.GEMINI_API_KEY:
        logger.warning("QUALITY_GUARD_SKIPPED: GEMINI_API_KEY not found — passing by default")
        return True, ""

    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={settings.GEMINI_API_KEY}"

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": GUARD_PROMPT},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": b64_image,
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.1,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.error(f"QUALITY_GUARD_SKIPPED: Gemini Guard API failed (status={resp.status_code}): {resp.text[:200]}")
                return True, ""

            data = resp.json()
            raw_json = data["candidates"][0]["content"]["parts"][0]["text"]
            res = json.loads(raw_json)

            is_valid = res.get("is_valid", True)
            tip = res.get("tip") or "Please upload a clear photo of a single pair of earrings."
            return is_valid, tip

    except Exception as e:
        logger.error(f"QUALITY_GUARD_SKIPPED: Gemini Guard exception: {e}")
        return True, ""