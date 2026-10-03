"""Meta WhatsApp Cloud API service — ingestion + generation + delivery.

Responsibilities:
    - Parse incoming Meta webhook payloads
    - Retrieve media from Meta's authenticated media API
    - Validate downloaded images
    - Store images using existing GemVision UploadService
    - Create WhatsAppIngestion records for traceability
    - Trigger existing GemVision generation pipeline
    - Upload generated images to Meta media API
    - Send generated images back to WhatsApp users
    - Deliver PDF invoices and multi-pack batch completions
"""

import asyncio
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings
from app.services import metrics
from app.utils.executors import run_io
from app.utils.logger import logger, mask_phone
# ─── Constants ────────────────────────────────────────────────────────────

META_MEDIA_URL_TEMPLATE = "https://graph.facebook.com/v21.0/{media_id}"
META_SEND_MESSAGE_URL = "https://graph.facebook.com/v21.0/{phone_number_id}/messages"
META_MEDIA_UPLOAD_URL = "https://graph.facebook.com/v21.0/{phone_number_id}/media"

SUPPORTED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}

# ─── 6-Style Earring Catalog Pack (ordered delivery 1..6) ─────────────────
CATALOG_PACK_STYLES: List[Tuple[str, str]] = [
    ("Clean E-Commerce", "prompt_ecommerce"),
    ("Close-up on Ear", "prompt_close_up"),
    ("Scale Reference", "prompt_scale_reference"),
    ("Professional Studio", "prompt_professional"),
    ("Lifestyle Shot", "prompt_complementary"),
    ("UGC Style", "prompt_ugc"),
]

# ─── Styles per pack (settings.MAX_STYLES_PER_PACK, default 1) ─────────────
# Set MAX_STYLES_PER_PACK in .env (or leave it empty for all styles).
MAX_STYLES_PER_PACK: Optional[int] = settings.MAX_STYLES_PER_PACK


def _pack_style_count_label() -> str:
    """Human-readable style count for user-facing copy, kept in sync with the throttle."""
    if MAX_STYLES_PER_PACK is None:
        return f"all {len(CATALOG_PACK_STYLES)} styles"
    count = max(MAX_STYLES_PER_PACK, 1)
    return "1 test style" if count == 1 else f"{count} test styles"


def pack_generation_count() -> int:
    """How many images one Catalog Pack generates (all styles, or fewer under the development throttle)."""
    if MAX_STYLES_PER_PACK is None:
        return len(CATALOG_PACK_STYLES)
    return min(len(CATALOG_PACK_STYLES), max(MAX_STYLES_PER_PACK, 1))


CATALOG_PACK_ACK_TEMPLATE = (
    f"✨ Processing your Earring Catalog Pack (generating {_pack_style_count_label()})... "
    "Please allow 20-30 seconds."
)

CATALOG_PACK_SEND_THROTTLE_SECONDS = 0.8

# ─── ZERO-COST TEST MODE (dry-run) ────────────────────────────────────────
# When DRY_RUN_IMAGE_MODE=true the Gemini / Nano Banana image-generation API is
# NEVER called. The uploaded reference image is echoed back as the "generated"
# style, so the whole WhatsApp pipeline (reference image -> Meta media upload ->
# delivery) can be exercised end-to-end at ZERO image-generation cost.
# Set to false (the default) to restore real generation.
DRY_RUN_IMAGE_MODE: bool = bool(settings.DRY_RUN_IMAGE_MODE) or (
    os.getenv("DRY_RUN_IMAGE_MODE", "false").lower() == "true"
)

# Fixed user-facing confirmation sent while in zero-cost test mode.
DRY_RUN_DELIVERY_MESSAGE = (
    "[TEST MODE - No API Charge] 1/1 Style processed successfully."
)


# ─── Webhook payload parsing ─────────────────────────────────────────────


def parse_webhook_entry(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse a single Meta webhook entry into a list of normalised change events."""
    events: List[Dict[str, Any]] = []

    changes = entry.get("changes", [])
    for change in changes:
        value = change.get("value", {})

        statuses = value.get("statuses", [])
        if statuses:
            for status in statuses:
                if isinstance(status, dict) and status.get("type") == "payment":
                    # WhatsApp Pay (India) payment status update.
                    from app.services.whatsapp_pay_service import parse_payment_status
                    events.append(parse_payment_status(status))
                    continue
                events.append({
                    "type": "status",
                    "message_id": status.get("id", ""),
                    "status": status.get("status", ""),
                    "timestamp": status.get("timestamp", ""),
                })
            continue

        messages = value.get("messages", [])
        for message in messages:
            msg_type = message.get("type", "")
            msg_id = message.get("id", "")
            sender = message.get("from", "")
            timestamp = message.get("timestamp", "")

            if msg_type == "image":
                image_data = message.get("image", {})
                events.append({
                    "type": "image",
                    "message_id": msg_id,
                    "sender": sender,
                    "timestamp": timestamp,
                    "media_id": image_data.get("id", ""),
                    "mime_type": image_data.get("mime_type", ""),
                    "caption": image_data.get("caption", ""),
                })
            elif msg_type == "text":
                text_data = message.get("text", {})
                events.append({
                    "type": "text",
                    "message_id": msg_id,
                    "sender": sender,
                    "timestamp": timestamp,
                    "body": text_data.get("body", ""),
                })
            elif msg_type == "interactive":
                interactive_data = message.get("interactive", {})
                interactive_type = interactive_data.get("type", "")
                button_reply = interactive_data.get("button_reply", {})

                if interactive_type == "button_reply":
                    events.append({
                        "type": "interactive",
                        "subtype": "button_reply",
                        "message_id": msg_id,
                        "sender": sender,
                        "timestamp": timestamp,
                        "button_reply": {
                            "id": button_reply.get("id", ""),
                            "title": button_reply.get("title", ""),
                        },
                    })
                elif interactive_type == "nfm_reply":
                    # WhatsApp Flow submission. Meta sends the submitted
                    # fields as a JSON *string* in nfm_reply.response_json.
                    nfm_reply = interactive_data.get("nfm_reply") or {}
                    events.append({
                        "type": "interactive",
                        "subtype": "nfm_reply",
                        "message_id": msg_id,
                        "sender": sender,
                        "timestamp": timestamp,
                        "flow_name": nfm_reply.get("name", ""),
                        "flow_response": parse_flow_response_json(nfm_reply.get("response_json")),
                    })
                else:
                    events.append({
                        "type": "unsupported",
                        "message_id": msg_id,
                        "sender": sender,
                        "timestamp": timestamp,
                        "raw_type": f"interactive:{interactive_type}",
                    })
            else:
                events.append({
                    "type": "unsupported",
                    "message_id": msg_id,
                    "sender": sender,
                    "timestamp": timestamp,
                    "raw_type": msg_type,
                })

    return events


def parse_flow_response_json(raw: Any) -> Dict[str, Any]:
    """Decode a Flow ``response_json`` safely. Never raises; {} on bad input."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, (str, bytes)) or not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("Flow response_json is not valid JSON — ignored")
        return {}
    return data if isinstance(data, dict) else {}


# ─── Meta request helper (retry on 429 / 5xx) ────────────────────────────

# Answers worth retrying for calls that can be repeated safely (lookups, downloads).
META_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
# A SEND may be retried only when Meta certainly did NOT process it. 429 means "rejected before processing".
# A 500/502/503/504 can come from a gateway AFTER Meta accepted the message, so retrying could send the
# customer the same image or text twice; those are left to the caller, as before.
META_SEND_RETRYABLE_STATUSES = frozenset({429})


def _meta_retry_wait(response: Optional["httpx.Response"], attempt: int) -> Optional[float]:
    """Seconds to wait before retry number ``attempt`` (0-based); None = do not retry.

    Exponential backoff with +-50% jitter. When Meta sends ``Retry-After`` and it is longer than the cap,
    waiting is not worth it (the caller gets the failure now, as before)."""
    base = max(float(settings.META_RETRY_BACKOFF_BASE_SECONDS), 0.0)
    cap = max(float(settings.META_RETRY_BACKOFF_CAP_SECONDS), 0.0)
    delay = min(base * (2 ** attempt), cap) * random.uniform(0.5, 1.5)
    if response is not None:
        hint = response.headers.get("retry-after")
        try:
            asked = float(hint) if hint else None
        except ValueError:
            asked = None
        if asked is not None:
            if asked > cap:
                return None
            delay = max(delay, asked)
    return delay


async def _meta_request(call: Any, label: str, *, idempotent: bool) -> "httpx.Response":
    """Run one Meta HTTP call (``call`` is a zero-argument coroutine factory), retrying what is safe to retry.

    Retried (up to META_REQUEST_RETRIES times, with jittered waits): "could not connect" errors (the request
    never left, so nothing can be duplicated) and HTTP 429. For ``idempotent`` calls (lookups, downloads) also
    5xx answers and read timeouts. A SEND is never retried after a 5xx or a read timeout: it may already have
    been delivered, and retrying would send the customer the same message twice, so that is left to the caller.
    The last response (or exception) is returned/raised unchanged when retries run out (EXT-4).
    """
    retries = max(int(settings.META_REQUEST_RETRIES or 0), 0)
    retryable = META_RETRYABLE_STATUSES if idempotent else META_SEND_RETRYABLE_STATUSES
    for attempt in range(retries + 1):
        last = attempt >= retries
        try:
            response = await call()
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # No connection was ever established, so nothing was sent: always safe to retry.
            if last:
                raise
            wait = _meta_retry_wait(None, attempt)
        except httpx.TimeoutException:
            if last or not idempotent:
                raise
            wait = _meta_retry_wait(None, attempt)
        else:
            if response.status_code not in retryable or last:
                return response
            wait = _meta_retry_wait(response, attempt)
            if wait is None:
                return response
        logger.warning(f"Meta {label}: transient failure, retry {attempt + 1}/{retries} in {wait:.1f}s")
        await asyncio.sleep(wait)
    raise RuntimeError("unreachable")  # pragma: no cover


# ─── Media retrieval ─────────────────────────────────────────────────────


_MEDIA_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,100}$")


def is_valid_media_id(media_id: object) -> bool:
    """A WhatsApp media id is a short token of digits/letters. Anything else (slashes, dots, query strings) could change
    which address the lookup is sent to, so it is refused before any request is made."""
    return isinstance(media_id, str) and bool(_MEDIA_ID_RE.match(media_id))


def is_meta_media_host(url: object) -> bool:
    """True when ``url`` is https and its host is one of Meta's (the access token is sent to it)."""
    if not settings.META_MEDIA_HOST_CHECK:
        return True
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(url))
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return False
    suffixes = [s.strip().lower() for s in (settings.META_MEDIA_HOST_SUFFIXES or "").split(",") if s.strip()]
    return any(host.endswith(s) or host == s.lstrip(".") for s in suffixes)


async def get_media_url(media_id: str) -> Optional[str]:
    """Retrieve the temporary download URL for a Meta media ID."""
    if not settings.META_WHATSAPP_TOKEN:
        logger.error("META_WHATSAPP_TOKEN not configured — cannot retrieve media")
        return None
    if not is_valid_media_id(media_id):
        logger.error("Refusing media lookup: the media id is not a plain token")
        return None

    url = META_MEDIA_URL_TEMPLATE.format(media_id=media_id)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await _meta_request(
                lambda: client.get(url, headers={"Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}"}),
                "media lookup", idempotent=True,
            )

            if response.status_code != 200:
                logger.error(
                    f"Meta media metadata request failed: status={response.status_code} "
                    f"media_id={media_id[:20]}..."
                )
                return None

            data = response.json()
            media_url = data.get("url")
            if not media_url:
                logger.error(f"Meta media metadata response missing 'url' field: media_id={media_id[:20]}...")
                return None
            if not is_meta_media_host(media_url):
                logger.error(f"Meta returned a media link on an unexpected host; not following it: media_id={media_id[:20]}...")
                return None

            return media_url

    except httpx.TimeoutException:
        logger.error(f"Meta media metadata request timed out: media_id={media_id[:20]}...")
        return None
    except Exception as e:
        logger.error(f"Meta media metadata request failed: {e}")
        return None


async def download_media(media_url: str) -> Optional[Tuple[bytes, str]]:
    """Download media from Meta CDN using Bearer auth and redirect handling."""
    if not media_url:
        return None
    if not is_meta_media_host(media_url):
        logger.error("Refusing media download: the link is not on a Meta host (the access token is not sent)")
        return None

    headers = {
        "Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}",
        "User-Agent": "curl/7.68.0",
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            response = await _meta_request(
                lambda: client.get(media_url, headers=headers), "media download", idempotent=True
            )

            if response.status_code != 200:
                logger.error(
                    f"Media download failed: status={response.status_code} "
                    f"body={response.text[:200]}"
                )
                return None

            content_type = response.headers.get("content-type", "application/octet-stream")
            if ";" in content_type:
                content_type = content_type.split(";")[0].strip()

            image_bytes = response.content
            if len(image_bytes) == 0:
                logger.error("Downloaded media is empty (0 bytes)")
                return None

            return image_bytes, content_type

    except httpx.TimeoutException:
        logger.error("Media download timed out")
        return None
    except Exception as e:
        logger.error(f"Media download failed: {e}")
        return None


# ─── Image validation ────────────────────────────────────────────────────


def validate_image(
    image_bytes: bytes,
    content_type: str,
) -> Tuple[bool, Optional[str]]:
    """Validate downloaded image bytes before storage."""
    if len(image_bytes) == 0:
        return False, "Downloaded image is empty (0 bytes)"

    if len(image_bytes) > settings.META_MAX_MEDIA_BYTES:
        max_mb = settings.META_MAX_MEDIA_BYTES / (1024 * 1024)
        return False, f"Image too large: {len(image_bytes)} bytes (max {max_mb:.0f} MB)"

    normalized_ct = content_type.lower().split(";")[0].strip()
    if normalized_ct not in SUPPORTED_IMAGE_MIMES:
        return False, f"Unsupported image format: {content_type} (supported: {', '.join(sorted(SUPPORTED_IMAGE_MIMES))})"

    try:
        from PIL import Image as PILImage

        img = PILImage.open(io.BytesIO(image_bytes))
        img.verify()
        img = PILImage.open(io.BytesIO(image_bytes))
        w, h = img.size
        if w < 16 or h < 16:
            return False, f"Image too small: {w}x{h} (minimum 16x16)"
    except Exception as e:
        return False, f"Invalid image data: {e}"

    return True, None


# ─── Webhook signature verification ──────────────────────────────────────


def verify_webhook_signature(
    payload_body: bytes,
    signature_header: Optional[str],
) -> bool:
    """Verify Meta's X-Hub-Signature-256 HMAC-SHA256 signature."""
    secret = (settings.META_APP_SECRET or "").strip()
    if not secret:
        # Fail closed: without the app secret no signature can be verified.
        logger.warning("META_APP_SECRET not configured — webhook signature cannot be verified")
        return False

    if not signature_header:
        logger.warning("Webhook request missing X-Hub-Signature-256 header")
        return False

    import hmac

    expected_prefix = "sha256="
    if not signature_header.startswith(expected_prefix):
        logger.warning(f"Invalid signature format: {signature_header[:20]}...")
        return False

    signature_hash = signature_header[len(expected_prefix):]

    computed = hmac.new(
        secret.encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()

    # Bytes, not str: compare_digest raises TypeError on non-ASCII text, which
    # turned a malformed header into a 500.
    if not hmac.compare_digest(computed.encode("ascii"), signature_hash.encode("utf-8")):
        logger.warning("Webhook signature mismatch — possible tampering")
        return False

    return True


# ─── Interactive button message ─────────────────────────────────────────


async def send_feedback_buttons(recipient_id: str, ingestion_id: str) -> bool:
    """Send post-generation interactive feedback buttons."""
    return True


# ─── Plain text + CTA button messages ─────────────────────────────────────


def _meta_error_details(response: Any) -> Dict[str, Any]:
    """Pull code / error_subcode / message / details out of a Graph API error body."""
    try:
        err = (response.json() or {}).get("error") or {}
    except Exception:
        return {"status": getattr(response, "status_code", None), "message": (getattr(response, "text", "") or "")[:300]}
    return {
        "status": getattr(response, "status_code", None),
        "code": err.get("code"),
        "subcode": err.get("error_subcode"),
        "type": err.get("type"),
        "message": err.get("message"),
        "details": (err.get("error_data") or {}).get("details"),
        "fbtrace_id": err.get("fbtrace_id"),
    }


async def _post_message_payload(
    payload: Dict[str, Any],
    label: str,
    reply_to_message_id: Optional[str] = None,
    error_out: Optional[Dict[str, Any]] = None,
) -> bool:
    """POST a message payload to the Meta Send API with optional context quote.

    When ``error_out`` is a dict it is filled with the parsed Meta error
    (code, subcode, message, details) or {"timeout": True} on failure.
    """
    if not settings.META_WHATSAPP_TOKEN:
        logger.error(f"META_WHATSAPP_TOKEN not configured — cannot send {label}")
        return False

    if not settings.META_PHONE_NUMBER_ID:
        logger.error(f"META_PHONE_NUMBER_ID not configured — cannot send {label}")
        return False

    if reply_to_message_id:
        payload["context"] = {"message_id": reply_to_message_id}

    url = META_SEND_MESSAGE_URL.format(phone_number_id=settings.META_PHONE_NUMBER_ID)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await _meta_request(
                lambda: client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                ),
                label,
                idempotent=False,     # a send that timed out may have been delivered: never resent blindly
            )

            if response.status_code not in (200, 201):
                err = _meta_error_details(response)
                if error_out is not None:
                    error_out.update(err)
                metrics.registry.inc("moraa_meta_send_total", {"outcome": f"http_{response.status_code // 100}xx"})
                logger.error(
                    f"Meta send {label} failed: status={err.get('status')} code={err.get('code')} "
                    f"subcode={err.get('subcode')} message={err.get('message')!r} "
                    f"details={err.get('details')!r} fbtrace_id={err.get('fbtrace_id')}"
                )
                return False

            data = response.json()
            messages = data.get("messages", [])
            if not messages:
                logger.error(f"Meta send {label} response missing 'messages': {data}")
                return False

            logger.info(
                f"Meta {label} sent: recipient={mask_phone(payload.get('to', ''))} "
                f"message_id={messages[0].get('id', '')}"
            )
            metrics.registry.inc("moraa_meta_send_total", {"outcome": "ok"})
            return True

    except httpx.TimeoutException:
        if error_out is not None:
            error_out["timeout"] = True
        metrics.registry.inc("moraa_meta_send_total", {"outcome": "timeout"})
        logger.error(f"Meta send {label} timed out")
        return False
    except Exception as e:
        if error_out is not None:
            error_out["message"] = str(e)
        metrics.registry.inc("moraa_meta_send_total", {"outcome": "error"})
        logger.error(f"Meta send {label} failed: {e}")
        return False


async def send_whatsapp_text(
    recipient_id: str,
    message_text: str,
    reply_to_message_id: Optional[str] = None,
) -> bool:
    """Send a plain text WhatsApp message."""
    return await send_text_message(recipient_id, message_text, reply_to_message_id=reply_to_message_id)


async def send_text_message(
    recipient_id: str,
    text: str,
    reply_to_message_id: Optional[str] = None,
) -> bool:
    """Send a plain text message to a WhatsApp user with optional reply quoting."""
    if not recipient_id or not text:
        return False

    payload = {
        "messaging_product": "whatsapp",
        "to": recipient_id,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }

    return await _post_message_payload(payload, "text message", reply_to_message_id=reply_to_message_id)


async def send_whatsapp_cta_url_button(
    recipient_id: str,
    body_text: str,
    button_label: str,
    url: str,
    reply_to_message_id: Optional[str] = None,
) -> bool:
    """Send an interactive CTA-URL button with optional context quoting."""
    if not recipient_id or not body_text:
        logger.warning("send_whatsapp_cta_url_button called without recipient/body — skipped")
        return False

    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        logger.error("send_whatsapp_cta_url_button: payment URL missing or invalid")
        return False

    display_text = (button_label or "").strip()
    if not display_text:
        return False
    if len(display_text) > 20:
        display_text = display_text[:20].rstrip()

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_id,
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": body_text},
            "action": {
                "name": "cta_url",
                "parameters": {
                    "display_text": display_text,
                    "url": url,
                },
            },
        },
    }

    return await _post_message_payload(payload, "interactive CTA URL button", reply_to_message_id=reply_to_message_id)


async def send_reply_buttons(
    recipient_id: str,
    body_text: str,
    buttons: List[Tuple[str, str]],
    reply_to_message_id: Optional[str] = None,
) -> bool:
    """Send an interactive message with up to 3 reply buttons [(id, title)].

    Meta limits a reply-button title to 20 characters.
    """
    if not recipient_id or not body_text or not buttons:
        return False
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient_id,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": bid, "title": title[:20]}}
                    for bid, title in buttons[:3]
                ],
            },
        },
    }
    return await _post_message_payload(payload, "reply buttons", reply_to_message_id=reply_to_message_id)


async def send_interactive_cta_button(
    recipient_id: str,
    body_text: str,
    button_id: str,
    button_title: str,
    reply_to_message_id: Optional[str] = None,
) -> bool:
    """Send an interactive single-button (CTA) message."""
    if not recipient_id or not button_id or not button_title:
        return False

    payload = {
        "messaging_product": "whatsapp",
        "to": recipient_id,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": button_id,
                            "title": button_title,
                        },
                    }
                ],
            },
        },
    }

    return await _post_message_payload(payload, "interactive CTA button", reply_to_message_id=reply_to_message_id)


# ─── WhatsApp Flow (registration form) ──────────────────────────────────

REGISTRATION_FLOW_TOKEN_PREFIX = "moraa_reg_"
REGISTRATION_FLOW_CTA = "Setup Account"
REGISTRATION_FLOW_BODY = (
    "Quick Setup 📋\n\n"
    "Tap below to share your name, brand name, address and GSTIN (optional)."
)


async def send_registration_flow(
    recipient_id: str,
    reply_to_message_id: Optional[str] = None,
) -> bool:
    """Send the registration WhatsApp Flow. Returns False (never raises) when
    the Flow is not configured or Meta rejects the message, so the caller can
    fall back to the plain-text registration request.

    Payload per Meta "Sending a Flow" docs: interactive.type "flow",
    action.name "flow", parameters flow_message_version "3", flow_id,
    flow_cta, flow_token, flow_action "navigate" + flow_action_payload.screen.
    """
    flow_id = (settings.META_REGISTRATION_FLOW_ID or "").strip()
    screen = (settings.META_REGISTRATION_FLOW_SCREEN or "").strip()
    if not recipient_id or not flow_id or not screen:
        logger.info("Registration Flow not configured — using text registration")
        return False

    mode = (settings.META_REGISTRATION_FLOW_MODE or "").strip().lower() or "draft"
    parameters: Dict[str, Any] = {
        "flow_message_version": "3",
        "flow_token": f"{REGISTRATION_FLOW_TOKEN_PREFIX}{recipient_id.lstrip('+')}",
        "flow_id": flow_id,
        "flow_cta": REGISTRATION_FLOW_CTA,
        "flow_action": "navigate",
        "flow_action_payload": {"screen": screen},
        "mode": mode,
    }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_id,
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "body": {"text": REGISTRATION_FLOW_BODY},
            "action": {"name": "flow", "parameters": parameters},
        },
    }
    try:
        return await _post_message_payload(
            payload, "registration flow", reply_to_message_id=reply_to_message_id
        )
    except Exception as e:  # _post_message_payload already never raises
        logger.error(f"Registration flow send failed: {e}")
        return False


# ─── WhatsApp message send ───────────────────────────────────────────────


async def send_image_to_whatsapp(
    recipient_id: str,
    media_id: str,
    caption: str = "Your e-commerce image is ready.",
    reply_to_message_id: Optional[str] = None,
) -> bool:
    """Send an image message to a WhatsApp user via Meta Send API with contextual quote."""
    if not settings.META_WHATSAPP_TOKEN or not settings.META_PHONE_NUMBER_ID:
        return False

    payload = {
        "messaging_product": "whatsapp",
        "to": recipient_id,
        "type": "image",
        "image": {
            "id": media_id,
            "caption": caption,
        },
    }

    return await _post_message_payload(payload, "image", reply_to_message_id=reply_to_message_id)


async def send_document_to_whatsapp(
    recipient_id: str,
    document_bytes: bytes,
    filename: str = "invoice.pdf",
    caption: str = "",
    reply_to_message_id: Optional[str] = None,
) -> bool:
    """Upload and deliver a PDF document to WhatsApp via Meta Cloud API."""
    if not settings.META_WHATSAPP_TOKEN or not settings.META_PHONE_NUMBER_ID:
        logger.error("Meta credentials not configured for document upload")
        return False

    upload_url = META_MEDIA_UPLOAD_URL.format(phone_number_id=settings.META_PHONE_NUMBER_ID)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            files = {"file": (filename, document_bytes, "application/pdf")}
            data = {"messaging_product": "whatsapp", "type": "application/pdf"}
            headers = {"Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}"}

            upload_resp = await client.post(upload_url, headers=headers, files=files, data=data)
            if upload_resp.status_code not in (200, 201):
                logger.error(f"Failed to upload invoice document: {upload_resp.text}")
                return False

            media_id = upload_resp.json().get("id")

            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient_id,
                "type": "document",
                "document": {
                    "id": media_id,
                    "filename": filename,
                    "caption": caption,
                },
            }
            return await _post_message_payload(payload, "document", reply_to_message_id=reply_to_message_id)

    except Exception as e:
        logger.error(f"Exception sending document to WhatsApp: {e}")
        return False


# ─── 6-Pack catalog delivery ─────────────────────────────────────────────


async def send_catalog_pack_images_to_whatsapp(
    recipient_id: str,
    image_urls: list,
    balance_text: str,
    reply_to_message_id: Optional[str] = None,
) -> bool:
    """Deliver the generated Earring Catalog Pack styles to WhatsApp with
    contextual quote. Returns the number of images actually sent (0..total)
    so the caller can tell a partial delivery apart from a total failure."""
    if not recipient_id or not image_urls:
        return 0

    # Caption counts reflect what is actually delivered, so the throttled
    # single-style test pack does not claim to be a complete 6-style pack.
    total = len(image_urls)
    known_styles = len(CATALOG_PACK_STYLES)
    sent_count = 0

    for index, media_id in enumerate(image_urls, start=1):
        style_title = (
            CATALOG_PACK_STYLES[index - 1][0]
            if index <= known_styles
            else f"Style {index}"
        )

        if DRY_RUN_IMAGE_MODE:
            # ZERO-COST TEST MODE — fixed confirmation copy, no API charge made.
            caption = DRY_RUN_DELIVERY_MESSAGE
        elif index == total:
            caption = (
                f"{index}/{total} {style_title} ✨\n"
                "Here's your Earring Catalog Pack 📦\n"
                f"Remaining balance: {balance_text}"
            )
        else:
            caption = f"{index}/{total} {style_title}"

        quote_id = reply_to_message_id if index == 1 else None

        send_ok = await send_image_to_whatsapp(
            recipient_id=recipient_id,
            media_id=media_id,
            caption=caption,
            reply_to_message_id=quote_id,
        )
        if send_ok:
            sent_count += 1
        else:
            logger.error(
                f"Catalog pack delivery: image {index}/{total} failed — "
                f"recipient={mask_phone(recipient_id)} media_id={media_id}"
            )

        if index < len(image_urls):
            await asyncio.sleep(CATALOG_PACK_SEND_THROTTLE_SECONDS)

    return sent_count


# ─── Meta media upload ───────────────────────────────────────────────────


async def upload_media_to_meta(
    image_bytes: bytes,
    mime_type: str = "image/png",
) -> Optional[str]:
    """Upload an image to Meta's WhatsApp media API."""
    if not settings.META_WHATSAPP_TOKEN or not settings.META_PHONE_NUMBER_ID:
        logger.error("Meta credentials missing for media upload")
        return None

    url = META_MEDIA_UPLOAD_URL.format(phone_number_id=settings.META_PHONE_NUMBER_ID)
    ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
    ext = ext_map.get(mime_type, ".png")

    # g8: bounded retry for transient network/5xx failures -- a single
    # blip previously dropped the image permanently with no recovery.
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}"},
                    files={"file": (f"generated{ext}", image_bytes, mime_type)},
                    data={"messaging_product": "whatsapp", "type": mime_type},
                )

                if response.status_code not in (200, 201):
                    logger.error(
                        f"Meta media upload failed (attempt {attempt}/{max_attempts}): "
                        f"status={response.status_code}"
                    )
                    if response.status_code < 500 and response.status_code != 429:
                        return None  # non-retryable client error
                else:
                    data = response.json()
                    return data.get("id")

        except Exception as e:
            logger.error(f"Meta media upload exception (attempt {attempt}/{max_attempts}): {e}")

        if attempt < max_attempts:
            await asyncio.sleep(1.5 * attempt)

    return None


REFUND_AUDIT_ACTION = "whatsapp_generation_refund"


def _refund_failed_ingestion(db, ingestion) -> None:
    """Return the slot charge for an ingestion that delivered nothing.

    Money moves at most once per ingestion. The refund audit row is the
    claim: it is flushed BEFORE the wallet credit, and the partial unique
    index uq_audit_logs_money_once rejects a concurrent second claim, so a
    racing duplicate (retry endpoint, failure path, startup recovery) rolls
    back without crediting. Claim, credit and commit are one transaction, the
    same pattern as the Razorpay and WhatsApp Pay credits. Callers commit
    their own changes before calling. Never raises.
    """
    # Read once: after a rollback the ORM object is expired, and reloading it
    # on a broken connection would raise from the error path.
    ingestion_id = getattr(ingestion, "id", None)
    try:
        import json

        from sqlalchemy.exc import IntegrityError

        from app.models.audit_log import AuditLog
        from app.models.customer import Customer
        from app.models.wallet_transaction import KIND_DEBIT_ORDER, KIND_REFUND_ORDER, WalletTransaction
        from app.services.wallet_service import credit_wallet

        already = (
            db.query(AuditLog.id)
            .filter(
                AuditLog.action == REFUND_AUDIT_ACTION,
                AuditLog.resource_id == ingestion_id,
            )
            .first()
        )
        if already:
            return

        from app.services.wallet_service import find_customer_by_phone

        # Refund exactly what the ledger says was debited, to the wallet it was debited from. Orders
        # charged before the ledger existed have no debit row: they fall back to amount_charged and
        # the phone lookup. An order with NO recorded charge is never refunded (nothing was taken).
        debit = (
            db.query(WalletTransaction.customer_id, WalletTransaction.amount)
            .filter(WalletTransaction.ingestion_id == ingestion_id, WalletTransaction.kind == KIND_DEBIT_ORDER)
            .first()
        )
        if debit is not None:
            price = -int(debit.amount)
            cust = db.get(Customer, debit.customer_id)
        else:
            charged = getattr(ingestion, "amount_charged", None)
            price = int(charged) if charged else 0
            cust = find_customer_by_phone(db, ingestion.external_user_id) if price > 0 else None
        if price <= 0:
            logger.info(f"Refund not applicable, no recorded charge: ingestion_id={ingestion_id}")
            return
        if cust is None:
            logger.error(f"Refund skipped, customer not found: ingestion_id={ingestion_id}")
            return

        # 1. Claim: a concurrent duplicate fails here (on PostgreSQL it waits
        #    for the winner's commit, then fails) before any money moves.
        db.add(
            AuditLog(
                action=REFUND_AUDIT_ACTION,
                resource_id=ingestion_id,
                resource_type="whatsapp_ingestion",
                status="success",
                details=json.dumps({"whatsapp_id": cust.whatsapp_id, "amount": price}),
            )
        )
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            logger.info(f"Refund already claimed concurrently: ingestion_id={ingestion_id}")
            return

        # 2. Credit inside the same transaction; 3. commit claim + credit together.
        if credit_wallet(
            db, cust.whatsapp_id, price, commit=False,
            kind=KIND_REFUND_ORDER, ingestion_id=ingestion_id,
        ) != 1:
            db.rollback()
            logger.error(
                f"Refund rolled back, wallet row not updated: ingestion_id={ingestion_id}"
            )
            return
        db.commit()
        logger.info(f"Refunded ₹{price} for failed ingestion_id={ingestion_id}")
    except Exception as e:
        logger.error(f"Refund for failed ingestion failed: ingestion_id={ingestion_id} error={e}")
        try:
            db.rollback()
        except Exception:
            pass


_FAILURE_RATE_CHECK_INTERVAL_SECONDS = 60.0
_last_failure_rate_check = 0.0


def _check_failure_rate(db) -> None:
    # The check is a 24-hour GROUP BY over the orders table: at most once a minute per process, not once per
    # failed order (the alert sweep in alert_service also checks it on a schedule).
    global _last_failure_rate_check
    import time as _time

    now = _time.monotonic()
    if now - _last_failure_rate_check < _FAILURE_RATE_CHECK_INTERVAL_SECONDS:
        return
    _last_failure_rate_check = now
    try:
        from app.services.generation_metrics import alert_if_failure_rate_exceeded

        alert_if_failure_rate_exceeded(db)
    except Exception as e:
        logger.error(f"Failure-rate check failed: {e}")


def _fail_ingestion(db, ingestion, error_message: str) -> None:
    ingestion.status = "failed"
    ingestion.error_message = error_message
    db.commit()
    logger.error(f"WhatsApp generation failed: ingestion_id={ingestion.id} error={error_message}")
    _refund_failed_ingestion(db, ingestion)
    _check_failure_rate(db)


def _fail_delivery(db, ingestion, error_message: str) -> None:
    ingestion.status = "delivery_failed"
    ingestion.error_message = error_message
    db.commit()
    logger.error(f"WhatsApp delivery failed: ingestion_id={ingestion.id} error={error_message}")
    _refund_failed_ingestion(db, ingestion)
    _check_failure_rate(db)


def _refunded_amount(db, ingestion) -> int:
    """Rupees refunded for this ingestion (0 when no refund row exists)."""
    try:
        from app.models.audit_log import AuditLog

        row = (
            db.query(AuditLog.details)
            .filter(
                AuditLog.action == REFUND_AUDIT_ACTION,
                AuditLog.resource_id == ingestion.id,
            )
            .first()
        )
        if row is None:
            return 0
        return int((json.loads(row[0] or "{}") or {}).get("amount") or 0)
    except Exception as e:
        logger.error(f"Refund lookup failed: ingestion_id={ingestion.id} error={e}")
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def _release_db(db) -> None:
    """End the open transaction so its connection goes back to the pool BEFORE a long network wait (PERF-1).

    A Session keeps its connection from its first query until commit/rollback; a worker that then waits
    15-40 s for a provider or Meta would hold it for the whole wait, and about 15 such orders exhaust the pool
    and freeze the event loop. Committing here is safe: everything read so far is already in local variables or
    reloads on next use, and nothing half-written is pending at these call sites.
    """
    try:
        db.commit()
    except Exception as e:  # noqa: BLE001 -- releasing must never break an order
        logger.error(f"Could not end the transaction before a network wait: {e}")
        try:
            db.rollback()
        except Exception:
            pass


async def _notify_failed_order(db, ingestion, product_label: str) -> None:
    """Tell the customer a paid order failed, using the Clean Studio Shot
    failure wording. States a refund only when a refund row exists. Never raises."""
    refunded = _refunded_amount(db, ingestion)
    text = f"Sorry, we couldn't create your {product_label} this time. "
    if refunded:
        text += f"₹{refunded} has been refunded to your wallet."
    elif not ingestion.amount_charged:
        text += "You were not charged."
    ingestion_id, recipient_id, quote_id = ingestion.id, ingestion.external_user_id, ingestion.external_message_id
    _release_db(db)
    try:
        await send_whatsapp_text(recipient_id, text.strip(), reply_to_message_id=quote_id)
    except Exception as notify_error:
        logger.error(f"Failed-order notice not sent: ingestion_id={ingestion_id} error={notify_error}")


def _advance_status(db, ingestion_id: str, expected: str, new: str) -> bool:
    """Move an order forward only if nobody else (the recovery sweep) changed it meanwhile.

    A False result means the order was already failed and refunded by recovery; the caller must stop
    and not deliver. Never raises."""
    from app.models.whatsapp_ingestion import WhatsAppIngestion

    try:
        moved = (
            db.query(WhatsAppIngestion)
            .filter(WhatsAppIngestion.id == ingestion_id, WhatsAppIngestion.status == expected)
            .update({WhatsAppIngestion.status: new}, synchronize_session=False)
        )
        db.commit()
        db.expire_all()
    except Exception as e:
        db.rollback()
        logger.error(f"Status change {expected}->{new} failed: ingestion_id={ingestion_id} error={e}")
        return False
    if moved != 1:
        logger.error(
            f"Order {ingestion_id} was changed by recovery while its worker ran "
            f"(expected '{expected}', wanted '{new}'); worker stops."
        )
        return False
    return True


# Paid orders whose worker can no longer be running once they are this old
# (queued/processing rows left behind by a process restart).
STUCK_PAID_STATUSES = ("white_queued", "pack_queued", "processing", "generated", "stored")
# 'generated' means images may already be on their way to the customer: give delivery three times as long.
GENERATED_PATIENCE = 3


async def recover_unrefunded_failed_orders(older_than) -> int:
    """Refund orders that were marked failed/delivery_failed but whose refund never completed.

    ``_fail_ingestion`` commits the terminal status and THEN refunds; a crash or a database error in
    between used to leave the money held forever. The refund is idempotent (audit claim + ledger
    uniqueness), so re-running it for every such order is safe. Never raises."""
    from datetime import datetime, timezone

    from sqlalchemy import exists

    from app.database import SessionLocal
    from app.models.audit_log import AuditLog
    from app.models.whatsapp_ingestion import PRODUCT_WHITE_BG, WhatsAppIngestion

    recovered = 0
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - older_than
        rows = (
            db.query(WhatsAppIngestion)
            .filter(
                WhatsAppIngestion.status.in_(("failed", "delivery_failed")),
                WhatsAppIngestion.amount_charged > 0,
                WhatsAppIngestion.updated_at < cutoff,
                ~exists().where(
                    AuditLog.action == REFUND_AUDIT_ACTION, AuditLog.resource_id == WhatsAppIngestion.id
                ),
            )
            .order_by(WhatsAppIngestion.updated_at)
            .limit(50)
            .all()
        )
        for row in rows:
            row_id, product_code = row.id, row.product_code
            # Re-check right before paying out: the rows were loaded earlier, and a retry may have restarted
            # this order since. A guarded no-op UPDATE only matches while the order is still failed.
            still_failed = (
                db.query(WhatsAppIngestion)
                .filter(
                    WhatsAppIngestion.id == row_id,
                    WhatsAppIngestion.status.in_(("failed", "delivery_failed")),
                )
                .update({WhatsAppIngestion.status: WhatsAppIngestion.status}, synchronize_session=False)
            )
            db.commit()
            if still_failed != 1:
                continue
            logger.error(f"Failed order still holds the customer's money, refunding: ingestion_id={row_id}")
            refunded_before = _refunded_amount(db, row)
            _refund_failed_ingestion(db, row)
            refunded_after = _refunded_amount(db, row)
            if refunded_after > 0 and refunded_before == 0:       # only the process that moved the money tells the customer
                label = "Clean Studio Shot" if product_code == PRODUCT_WHITE_BG else "Full Catalog Pack"
                await _notify_failed_order(db, row, label)
                recovered += 1
            elif refunded_after == 0:
                # Could not refund (e.g. wallet row missing): move it to the back of the queue, stay quiet.
                db.query(WhatsAppIngestion).filter(WhatsAppIngestion.id == row_id).update(
                    {WhatsAppIngestion.updated_at: datetime.now(timezone.utc)}, synchronize_session=False)
                db.commit()
                logger.error(f"Refund still not possible: ingestion_id={row_id}; needs manual review")
    except Exception as e:
        logger.error(f"Unrefunded failed order recovery failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()
    return recovered


async def release_abandoned_choice_claims(older_than) -> int:
    """Put orders left in ``choice_claimed`` (process died right after the tap) back to
    ``awaiting_choice`` so the customer can tap again. Only when NO debit exists for the order; the
    debit and the status change commit together, so a debited order is never in this state."""
    from datetime import datetime, timezone

    from sqlalchemy import exists

    from app.database import SessionLocal
    from app.models.wallet_transaction import KIND_DEBIT_ORDER, WalletTransaction
    from app.models.whatsapp_ingestion import WhatsAppIngestion

    released = 0
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - older_than
        released = (
            db.query(WhatsAppIngestion)
            .filter(
                WhatsAppIngestion.status == "choice_claimed",
                WhatsAppIngestion.updated_at < cutoff,
                ~exists().where(
                    WalletTransaction.ingestion_id == WhatsAppIngestion.id,
                    WalletTransaction.kind == KIND_DEBIT_ORDER,
                ),
            )
            .update(
                {WhatsAppIngestion.status: "awaiting_choice", WhatsAppIngestion.product_code: None},
                synchronize_session=False,
            )
        )
        db.commit()
        if released:
            logger.warning(f"Released {released} abandoned product-choice claim(s)")
    except Exception as e:
        logger.error(f"Abandoned choice release failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()
    return int(released or 0)


INTERRUPTED_PHOTO_MESSAGE = (
    "Sorry, we couldn't finish processing your photo and nothing was charged. Please send it again."
)


async def recover_interrupted_photos(older_than) -> int:
    """A photo whose processing was cut off (the server restarted while it was downloading or being checked) stays
    in ``received`` forever and the customer never gets the choice buttons (Q-2). No money moves at that stage, so the
    row is closed and the customer is asked to send the photo again. Returns how many were closed."""
    from datetime import datetime, timezone

    from app.database import SessionLocal
    from app.models.whatsapp_ingestion import WhatsAppIngestion

    closed: list = []
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - older_than
        rows = (
            db.query(WhatsAppIngestion)
            .filter(WhatsAppIngestion.status == "received", WhatsAppIngestion.updated_at < cutoff)
            .limit(50)
            .all()
        )
        for row in rows:
            claimed = (
                db.query(WhatsAppIngestion)
                .filter(WhatsAppIngestion.id == row.id, WhatsAppIngestion.status == "received")
                .update(
                    {WhatsAppIngestion.status: "rejected", WhatsAppIngestion.error_message: "Photo processing interrupted"},
                    synchronize_session=False,
                )
            )
            if claimed == 1:
                closed.append((row.external_user_id, row.external_message_id))
        db.commit()
    except Exception as e:
        logger.error(f"Interrupted photo recovery failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()
    for sender, message_id in closed:
        try:
            await send_whatsapp_text(sender, INTERRUPTED_PHOTO_MESSAGE, reply_to_message_id=message_id)
        except Exception as e:
            logger.error(f"Interrupted photo notice not sent: {e}")
    if closed:
        logger.warning(f"Closed {len(closed)} interrupted photo(s); customers asked to resend")
    return len(closed)


RECOVERY_SWEEP_INTERVAL_SECONDS = 180
FAILED_REFUND_GRACE = timedelta(minutes=2)       # let the live failure path finish its own refund first
CHOICE_CLAIM_GRACE = timedelta(minutes=5)
INTERRUPTED_PHOTO_GRACE = timedelta(minutes=10)    # a photo normally takes seconds; far longer means it was cut off


async def run_recovery_sweep_forever(stuck_after) -> None:
    """Periodic recovery (started from the app lifespan). Until the scheduler of the scale phase
    exists this runs in the single API process; every step is idempotent, so a second instance is
    harmless. Never raises except on cancel."""
    import asyncio

    while True:
        await asyncio.sleep(RECOVERY_SWEEP_INTERVAL_SECONDS)
        try:
            from app.services.scheduler_lease import holds_lease

            if not await holds_lease("order_recovery", RECOVERY_SWEEP_INTERVAL_SECONDS * 2 + 30):
                continue                          # another process owns recovery right now (ARC-2)
            stuck = await recover_stuck_paid_orders(stuck_after)
            unrefunded = await recover_unrefunded_failed_orders(FAILED_REFUND_GRACE)
            released = await release_abandoned_choice_claims(CHOICE_CLAIM_GRACE)
            await recover_interrupted_photos(INTERRUPTED_PHOTO_GRACE)
            from app.services.message_dedupe import purge_old_processed_messages

            await asyncio.to_thread(purge_old_processed_messages)
            for action, count in (("stuck_refunded", stuck), ("unrefunded_refunded", unrefunded), ("claims_released", released)):
                if count:
                    metrics.registry.inc("moraa_recovery_sweep_total", {"action": action}, count)
            if stuck or unrefunded or released:
                logger.warning(f"Recovery sweep: stuck={stuck} unrefunded={unrefunded} released={released}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Recovery sweep iteration failed: {e}")


async def recover_stuck_paid_orders(older_than) -> int:
    """Startup recovery: fail + refund (exactly once) paid orders stuck in a
    queued/processing state for longer than ``older_than``.

    Only rows with a recorded charge (``amount_charged > 0``) are touched;
    recent rows, completed rows and unpaid rows are left alone. Each row is
    claimed with a guarded UPDATE so two sweepers can never both refund it.
    Returns the number of orders recovered. Never raises.
    """
    from datetime import datetime, timezone

    from sqlalchemy import or_

    from app.database import SessionLocal
    from app.models.whatsapp_ingestion import PRODUCT_WHITE_BG, WhatsAppIngestion

    recovered = 0
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - older_than
        generated_cutoff = datetime.now(timezone.utc) - older_than * GENERATED_PATIENCE
        # Photos of a bulk order are worked on a few at a time, so the last ones legitimately wait a long time before
        # they start: they get three times the patience of a single order.
        bulk_cutoff = datetime.now(timezone.utc) - older_than * 3
        rows = (
            db.query(WhatsAppIngestion)
            .filter(
                WhatsAppIngestion.status.in_(STUCK_PAID_STATUSES),
                WhatsAppIngestion.amount_charged > 0,
                WhatsAppIngestion.updated_at < cutoff,
                or_(WhatsAppIngestion.group_id.is_(None), WhatsAppIngestion.updated_at < bulk_cutoff),
                or_(
                    WhatsAppIngestion.status != "generated",
                    WhatsAppIngestion.updated_at < generated_cutoff,
                ),
            )
            .all()
        )
        for row in rows:
            message = f"Recovered by the sweep: order stuck in '{row.status}'"
            claimed = (
                db.query(WhatsAppIngestion)
                .filter(
                    WhatsAppIngestion.id == row.id,
                    WhatsAppIngestion.status == row.status,
                )
                .update(
                    {WhatsAppIngestion.status: "failed", WhatsAppIngestion.error_message: message},
                    synchronize_session=False,
                )
            )
            db.commit()
            if claimed != 1:
                continue
            db.refresh(row)
            logger.error(f"Stuck paid order recovered: ingestion_id={row.id} {message}")
            _refund_failed_ingestion(db, row)
            label = "Clean Studio Shot" if row.product_code == PRODUCT_WHITE_BG else "Full Catalog Pack"
            await _notify_failed_order(db, row, label)
            recovered += 1
    except Exception as e:
        logger.error(f"Stuck paid order recovery failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()
    return recovered


def _data_url_to_bytes(data_url: str) -> Optional[bytes]:
    import base64
    try:
        if not data_url.startswith("data:"):
            return None
        _, payload = data_url.split(",", 1)
        return base64.b64decode(payload)
    except Exception:
        return None


def _result_bytes(result: Any) -> Optional[bytes]:
    """Raw image bytes of a generation result: its ``image_data``, else decoded from a data URL (older callers)."""
    data = getattr(result, "image_data", None)
    if isinstance(data, (bytes, bytearray)) and data:
        return bytes(data)
    url = getattr(result, "image_url", None)
    if isinstance(url, str) and url:
        return _data_url_to_bytes(url)
    return None


def _bytes_to_data_url(
    image_bytes: bytes, mime_type: Optional[str] = None
) -> str:
    """Encode raw image bytes as a base64 data URL (inverse of _data_url_to_bytes)."""
    import base64
    resolved_mime = mime_type or "image/jpeg"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{resolved_mime};base64,{encoded}"


# ─── 6-Style Catalog Pack generation orchestrator ────────────────────────


async def _generate_single_pack_style(
    ingestion_id: str,
    style_title: str,
    prompt: str,
    reference_image_bytes: bytes,
    reference_mime_type: str,
    request_id: str,
    spend_reserved: bool = False,
) -> Optional[bytes]:
    """Generate one style of the catalog pack and return the raw image bytes (None when it failed)."""
    if DRY_RUN_IMAGE_MODE:
        # ZERO-COST TEST MODE — never call the Gemini / Nano Banana API. Echo the
        # uploaded reference image back so the rest of the pipeline still runs.
        logger.warning(
            f"[TEST MODE - No API Charge] dry-run generation for style='{style_title}' "
            f"ingestion_id={ingestion_id}: returning input image, no provider call made"
        )
        return reference_image_bytes

    try:
        from app.ai.image_generation_manager import ImageGenerationManager

        manager = ImageGenerationManager()
        result = await manager.generate_image(
            prompt=prompt,
            context={"request_id": request_id, "aspect_ratio": "4:5"},
            reference_image=reference_image_bytes,
            reference_mime_type=reference_mime_type,
            spend_reserved=spend_reserved,
        )

        image_bytes = _result_bytes(result) if result.success else None
        if not image_bytes:
            logger.error(
                f"Catalog pack generation failed for style='{style_title}' "
                f"ingestion_id={ingestion_id}: {result.error}"
            )
            return None

        return image_bytes

    except Exception as e:
        logger.error(
            f"Catalog pack generation exception for style='{style_title}' "
            f"ingestion_id={ingestion_id}: {e}"
        )
        return None


async def _generate_and_upload_style(
    generate_deadline_seconds: float = 0.0, **kwargs: Any
) -> Tuple[bool, Optional[str]]:
    """Generate one style, then upload it to Meta AT ONCE and let go of the bytes (PERF-5).

    Holding all six finished images (each several MB) until the last style completes is what made memory grow
    with every concurrent Pack; now each image lives only from "generated" to "uploaded". Returns
    ``(generated, media_id)``: ``(False, None)`` = the style failed, ``(True, None)`` = generated but the
    Meta upload failed, so the worker can still tell those two failures apart.

    ``generate_deadline_seconds`` bounds the GENERATION only (EXT-1): a style that finished in time is always
    uploaded and delivered, never cancelled half-way through its upload by the pack deadline.
    """
    try:
        if generate_deadline_seconds and generate_deadline_seconds > 0:
            image_bytes = await asyncio.wait_for(_generate_single_pack_style(**kwargs), timeout=generate_deadline_seconds)
        else:
            image_bytes = await _generate_single_pack_style(**kwargs)
    except asyncio.TimeoutError:
        logger.error(
            f"Catalog pack style '{kwargs.get('style_title')}' still generating after "
            f"{generate_deadline_seconds:.0f}s; dropped ingestion_id={kwargs.get('ingestion_id')}"
        )
        return False, None
    if not image_bytes:
        return False, None
    media_id = await upload_media_to_meta(image_bytes)
    del image_bytes
    return True, (media_id or None)


async def _gather_styles_with_deadline(
    coros: List[Any], deadline_seconds: float, ingestion_id: str
) -> List[Optional[str]]:
    """Run every style at once and wait at most ``deadline_seconds`` for the whole pack (EXT-1).

    Like ``asyncio.gather`` (same order, a failed style is ``None``), but a style still running at the
    deadline is cancelled and counted as failed, so one stuck style can no longer hold the whole paid pack:
    the styles that finished are delivered. A deadline of 0 or less means no deadline.
    """
    tasks = [asyncio.ensure_future(c) for c in coros]
    if not tasks:
        return []
    try:
        _, pending = await asyncio.wait(tasks, timeout=deadline_seconds if deadline_seconds > 0 else None)
    except BaseException:
        # The worker itself was cancelled (e.g. shutdown): like asyncio.gather, take the styles down with it
        # instead of leaving paid provider calls running with nobody to collect them.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    if pending:
        logger.error(
            f"Catalog pack deadline of {deadline_seconds:.0f}s reached: {len(pending)} of {len(tasks)} "
            f"style(s) dropped ingestion_id={ingestion_id}"
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    results: List[Optional[str]] = []
    for task in tasks:
        if task.cancelled() or task.exception() is not None:
            results.append(None)
        else:
            results.append(task.result())
    return results


# Statuses from which the Pack worker may start: queued by the product tap, or
# reset to "stored" by the retry endpoint. Same shape as WHITE_BG_RUNNABLE_STATUSES.
PACK_RUNNABLE_STATUSES = ("pack_queued", "stored")


async def process_whatsapp_catalog_pack(ingestion_id: str) -> bool:
    """Generate the catalog styles in parallel and deliver them to WhatsApp.

    The number of styles is capped by ``MAX_STYLES_PER_PACK`` (1 during the
    development throttle) instead of always fanning out to all 6 styles.
    """
    from app.database import SessionLocal
    from app.models.image import Image
    from app.models.whatsapp_ingestion import WhatsAppIngestion
    from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
    from app.services.earring_close_up_ears_prompt import build_close_up_ears_prompt
    from app.services.earring_scale_reference_prompt import build_scale_reference_prompt
    from app.services.earring_professional_shot_prompt import build_professional_shot_prompt
    from app.services.earring_complementary_shot_prompt import build_complementary_shot_prompt
    from app.services.earring_ugc_style_prompt import build_ugc_style_prompt
    from app.models.customer import Customer

    style_prompt_builders = {
        "prompt_ecommerce": build_earring_ecommerce_prompt,
        "prompt_close_up": build_close_up_ears_prompt,
        "prompt_scale_reference": build_scale_reference_prompt,
        "prompt_professional": build_professional_shot_prompt,
        "prompt_complementary": build_complementary_shot_prompt,
        "prompt_ugc": build_ugc_style_prompt,
    }

    started_at = time.monotonic()
    db = SessionLocal()
    ingestion = None
    # Slots this order took from the shared daily counter and has not yet accounted for (COST-1). Whatever is
    # left here when the worker ends (an exception, a cancellation) is given back in the `finally` below.
    held_slots = 0
    held_day: Optional[str] = None

    async def _fail(message: str, delivery: bool = False) -> bool:
        # Same path as before (status + refund exactly once via the audit
        # row), then tell the customer -- mirrors the Clean Studio Shot flow.
        if delivery:
            _fail_delivery(db, ingestion, message)
        else:
            _fail_ingestion(db, ingestion, message)
        await _notify_failed_order(db, ingestion, "Full Catalog Pack")
        return False

    try:
        # Atomic claim, like the Clean Studio Shot worker: only a runnable
        # status can move to processing, and only one UPDATE can match. A
        # read-then-write let two concurrent starts both claim the order and
        # pay for every style twice; an open-ended "not processing" rule would
        # also let a late duplicate re-run a delivered or refunded order.
        claimed = (
            db.query(WhatsAppIngestion)
            .filter(
                WhatsAppIngestion.id == ingestion_id,
                WhatsAppIngestion.status.in_(PACK_RUNNABLE_STATUSES),
            )
            .update(
                {WhatsAppIngestion.status: "processing", WhatsAppIngestion.error_message: None},
                synchronize_session=False,
            )
        )
        db.commit()
        if claimed != 1:
            exists = db.query(WhatsAppIngestion.id).filter(
                WhatsAppIngestion.id == ingestion_id
            ).first()
            if exists is None:
                logger.error(f"Catalog pack generation: ingestion not found: {ingestion_id}")
            else:
                logger.info(
                    f"Catalog pack generation: skipping ingestion {ingestion_id} — already in progress"
                )
            return False

        ingestion = db.query(WhatsAppIngestion).filter(
            WhatsAppIngestion.id == ingestion_id
        ).first()
        db.refresh(ingestion)

        image_record = db.query(Image).filter(Image.id == ingestion.image_id).first()
        if not image_record:
            return await _fail("Image record not found")

        from pathlib import Path
        image_path = Path(image_record.file_path)
        if not image_path.exists():
            return await _fail(f"Image file not found: {image_path}")

        reference_image_bytes = image_path.read_bytes()
        if len(reference_image_bytes) == 0:
            return await _fail("Image file is empty")

        style_jobs: List[Tuple[str, str]] = []
        for style_title, prompt_type in CATALOG_PACK_STYLES:
            builder = style_prompt_builders.get(prompt_type)
            if builder is None:
                continue
            style_jobs.append((style_title, builder()))

        # Development throttle — cap the pack to MAX_STYLES_PER_PACK styles
        # (1 during development) so we do not burn 6 parallel generations.
        if MAX_STYLES_PER_PACK is not None:
            style_jobs = style_jobs[: max(MAX_STYLES_PER_PACK, 1)]

        if not style_jobs:
            return await _fail("No valid style prompt builders available")

        # Daily spend cap: reserve every style of this paid pack up front. The
        # pack either fits under MAX_GENERATIONS_PER_DAY as a whole or fails
        # (and is refunded) before any provider call -- never a 1/6 pack.
        from app.services import entitlement_service as ent
        from app.services.wallet_service import find_customer_by_phone as _find_cust

        spend_reserved = False
        if not DRY_RUN_IMAGE_MODE and ent.is_admin(_find_cust(db, ingestion.external_user_id)):
            # Team (ADMIN) order: not part of the customers' daily ceiling, but counted against the team ceiling
            # (COST-2). The GENERATION_ENABLED kill switch still applies per call.
            from app.ai.image_generation_manager import admin_spend_key, reserve_admin_slots

            _release_db(db)
            blocked = await run_io(reserve_admin_slots, len(style_jobs))
            if blocked:
                return await _fail(f"Generation blocked: {blocked}")
            spend_reserved = True
            held_slots, held_day = len(style_jobs), admin_spend_key()
        elif not DRY_RUN_IMAGE_MODE:
            from app.ai.image_generation_manager import current_spend_day, reserve_generation_slots

            _release_db(db)           # the shared counter is a database call; hold no connection meanwhile
            reserve_day = current_spend_day()
            blocked = await run_io(reserve_generation_slots, len(style_jobs))
            if blocked:
                return await _fail(f"Generation blocked: {blocked}")
            spend_reserved = True
            held_slots, held_day = len(style_jobs), reserve_day

        logger.info(
            f"Catalog pack generation started: ingestion_id={ingestion_id} "
            f"styles={len(style_jobs)} dev_throttle={MAX_STYLES_PER_PACK} "
            f"dry_run={DRY_RUN_IMAGE_MODE}"
        )

        # Everything the long waits below need, read once; then the connection goes back to the pool
        # for the whole 15-40 s of generation (PERF-1).
        reference_mime = ingestion.mime_type or "image/jpeg"
        order_request_id = ingestion.request_id
        recipient_id = ingestion.external_user_id
        quote_id = ingestion.external_message_id
        _release_db(db)

        gather_results = await _gather_styles_with_deadline(
            [
                _generate_and_upload_style(
                    generate_deadline_seconds=float(settings.PACK_GENERATION_DEADLINE_SECONDS or 0),
                    ingestion_id=ingestion_id,
                    style_title=style_title,
                    prompt=prompt,
                    reference_image_bytes=reference_image_bytes,
                    reference_mime_type=reference_mime,
                    request_id=order_request_id,
                    spend_reserved=spend_reserved,
                )
                for style_title, prompt in style_jobs
            ],
            0,      # no overall cut-off here: each style bounds its own generation, and uploads are never cut
            ingestion_id,
        )

        # Each style was uploaded to Meta the moment it finished; a style that failed (or was dropped at the
        # deadline) is None. Same order as the styles, failures skipped, exactly as before.
        generated_any = any(r and r[0] for r in gather_results)
        media_ids: List[str] = [r[1] for r in gather_results if r and r[1]]

        # Styles that produced nothing (failed, dropped at the deadline) cost nothing: give their reserved
        # slots back to the shared daily counter (COST-1).
        if held_slots:
            failed_styles = sum(1 for r in gather_results if not (r and r[0]))
            held_slots = 0              # accounted for: the `finally` must not release these a second time
            if failed_styles:
                from app.ai.image_generation_manager import release_generation_slots

                await run_io(release_generation_slots, failed_styles, held_day)

        if not generated_any:
            return await _fail("All catalog style generations failed")

        if not _advance_status(db, ingestion_id, "processing", "generated"):
            return False

        if not media_ids:
            return await _fail("All Meta media uploads failed", delivery=True)

        from app.services.wallet_service import find_customer_by_phone, get_balance

        cust = find_customer_by_phone(db, recipient_id)
        rem_bal = get_balance(db, cust.whatsapp_id) if cust else 0
        balance_text = f"₹{rem_bal:,}"
        _release_db(db)

        sent_count = await send_catalog_pack_images_to_whatsapp(
            recipient_id=recipient_id,
            image_urls=media_ids,
            balance_text=balance_text,
            reply_to_message_id=quote_id,
        )
        # Completion is measured against the styles this pack was meant to
        # produce, not just the images that happened to be generated.
        total_images = len(style_jobs)

        if sent_count == 0:
            return await _fail("Catalog pack Meta message delivery failed", delivery=True)

        if sent_count < total_images:
            # Partial delivery: the customer already received real value, so
            # this is not refunded -- just kept distinct from full success.
            if not _advance_status(db, ingestion_id, "generated", "delivered_partial"):
                return True       # recovery changed the order; the images are already with the customer
            ingestion.error_message = f"Delivered {sent_count}/{total_images} images"
            db.commit()
            logger.warning(
                f"Catalog pack partially delivered: ingestion_id={ingestion_id} "
                f"sent={sent_count}/{total_images} recipient={mask_phone(recipient_id)}"
            )
            try:
                await send_whatsapp_text(
                    recipient_id,
                    f"Note: {sent_count} of {total_images} images in your Full Catalog Pack "
                    "could be created this time.",
                    reply_to_message_id=quote_id,
                )
            except Exception as notify_error:
                logger.error(f"Partial-pack notice not sent: ingestion_id={ingestion_id} error={notify_error}")
            await ent.record_trial_success(db, ingestion)  # images were delivered
            return True

        if not _advance_status(db, ingestion_id, "generated", "delivered"):
            return True       # images are already with the customer; do not overwrite a swept status
        ingestion.error_message = None
        db.commit()

        logger.info(
            f"Catalog pack delivered: ingestion_id={ingestion_id} images={len(media_ids)} "
            f"recipient={mask_phone(ingestion.external_user_id)}"
        )
        from app.services import eta_service

        eta_service.record_duration("pack", time.monotonic() - started_at)
        await ent.record_trial_success(db, ingestion)  # trial orders only; never raises
        return True

    except Exception as e:
        logger.error(f"Catalog pack generation exception: ingestion_id={ingestion_id} error={e}")
        # A paid order must never be left in processing/generated with no
        # output and no refund: record the failure through the same path.
        try:
            db.rollback()
            if ingestion is not None:
                db.refresh(ingestion)
                if ingestion.status not in ("failed", "delivery_failed", "delivered", "delivered_partial"):
                    return await _fail(f"Unexpected error: {e}"[:1000])
        except Exception as fail_error:
            logger.error(
                f"Catalog pack could not record failure: ingestion_id={ingestion_id} "
                f"error={fail_error}"
            )
        return False
    finally:
        if held_slots:
            # The worker ended (error / cancellation) before it could account for the slots it took: they paid
            # for nothing delivered, so they go back to the daily counter.
            try:
                from app.ai.image_generation_manager import release_generation_slots

                await run_io(release_generation_slots, held_slots, held_day)
            except BaseException:  # noqa: BLE001 -- must never mask the real outcome
                pass
        db.close()


# ─── White Background E-Commerce Image (₹50, 1 image) ────────────────────
# The customer picks the product with a WhatsApp reply button AFTER the photo
# is stored (never from the caption). Button ids carry the ingestion id so the
# choice is tied to the exact stored photo in the database.
PRODUCT_BUTTON_WHITE = "gv_white"
PRODUCT_BUTTON_PACK_1 = "gv_pack1"

# Statuses a paid White order may be (re)generated from: freshly queued by
# the product-choice handler, or reset to "stored" by the authenticated retry
# endpoint.
WHITE_BG_RUNNABLE_STATUSES = ("white_queued", "stored")

WHITE_BG_DRY_RUN_CAPTION = (
    "[TEST MODE - No API Charge] Clean Studio Shot: your photo is echoed back "
    "unchanged. No image was generated and your wallet was not charged."
)

WHITE_BG_AUDIT_ACTION = "whatsapp_white_generated"


def product_button_id(button: str, ingestion_id: str) -> str:
    return f"{button}:{ingestion_id}"


def parse_product_button_id(button_id: str) -> Optional[Tuple[str, str]]:
    """Return (button, ingestion_id) for a product-choice reply id, else None."""
    button, sep, ingestion_id = (button_id or "").partition(":")
    if not sep or not ingestion_id or button not in (PRODUCT_BUTTON_WHITE, PRODUCT_BUTTON_PACK_1):
        return None
    return button, ingestion_id


async def send_product_selection_buttons(
    recipient_id: str,
    ingestion_id: str,
    white_price: int,
    pack_price: int,
    balance: int,
    reply_to_message_id: Optional[str] = None,
) -> bool:
    """Ask which product to create for one stored photo (2 reply buttons).

    Meta limits a reply-button title to 20 characters, so the titles are the
    short forms; the body carries the full product names.
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient_id,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    "Photo received 📸\n\n"
                    "What would you like to create for this design?\n\n"
                    f"• Clean Studio Shot (₹{white_price}) — 1 polished product image on pure white with natural soft shadows.\n"
                    f"• Full Catalog Pack (₹{pack_price}) — Multi-angle commercial set with lifestyle staging.\n\n"
                    f"Wallet Balance: ₹{balance:,}"
                ),
            },
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": product_button_id(PRODUCT_BUTTON_WHITE, ingestion_id),
                            "title": f"Studio Shot — ₹{white_price}"[:20],
                        },
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": product_button_id(PRODUCT_BUTTON_PACK_1, ingestion_id),
                            "title": f"Catalog Pack — ₹{pack_price}"[:20],
                        },
                    },
                ],
            },
        },
    }
    return await _post_message_payload(
        payload, "product selection buttons", reply_to_message_id=reply_to_message_id
    )


async def process_whatsapp_white_bg(ingestion_id: str) -> bool:
    """Generate and deliver exactly ONE Ecommerce Shot (pure white) image.

    Dedicated worker for product WHITE_BG -- never routes through the Pack 1
    catalog worker. The order is claimed with one guarded UPDATE, so a second
    trigger for the same ingestion can never run a second generation. Reuses
    the frozen Prompt 1 builder and the shared ImageGenerationManager (spend
    guard + Gemini -> OpenAI fallback). Every failure goes through
    _fail_ingestion / _fail_delivery (refund of amount_charged exactly once)
    and the customer is told; an unexpected exception is recorded the same
    way, so a paid order is never left silently stuck in "processing".
    """
    import json
    from pathlib import Path

    from app.database import SessionLocal
    from app.models.audit_log import AuditLog
    from app.models.image import Image
    from app.models.whatsapp_ingestion import PRODUCT_WHITE_BG, WhatsAppIngestion
    from app.services.ecommerce_shot_prompt import build_ecommerce_shot_prompt

    from app.ai.concurrency_gate import PRIORITY_SINGLE, generation_priority

    generation_priority.set(PRIORITY_SINGLE)       # a single shot is served ahead of Pack calls when providers are busy
    started_at = time.monotonic()
    db = SessionLocal()
    ingestion = None

    async def _fail(message: str, delivery: bool = False) -> bool:
        if delivery:
            _fail_delivery(db, ingestion, message)
        else:
            _fail_ingestion(db, ingestion, message)
        # States a refund only when the refund row exists (never a refund
        # that was skipped), same wording as before.
        await _notify_failed_order(db, ingestion, "Clean Studio Shot")
        return False

    try:
        # Atomic claim: only one caller can move a runnable order to processing.
        claimed = (
            db.query(WhatsAppIngestion)
            .filter(
                WhatsAppIngestion.id == ingestion_id,
                WhatsAppIngestion.product_code == PRODUCT_WHITE_BG,
                WhatsAppIngestion.status.in_(WHITE_BG_RUNNABLE_STATUSES),
            )
            .update(
                {WhatsAppIngestion.status: "processing", WhatsAppIngestion.error_message: None},
                synchronize_session=False,
            )
        )
        db.commit()
        if claimed != 1:
            logger.info(f"White BG: ingestion {ingestion_id} not runnable (missing, other product, or already claimed)")
            return False

        ingestion = db.query(WhatsAppIngestion).filter(WhatsAppIngestion.id == ingestion_id).first()
        db.refresh(ingestion)

        image_record = db.query(Image).filter(Image.id == ingestion.image_id).first()
        if not image_record:
            return await _fail("Image record not found")
        image_path = Path(image_record.file_path)
        if not image_path.exists():
            return await _fail(f"Image file not found: {image_path}")
        reference_image_bytes = image_path.read_bytes()
        if not reference_image_bytes:
            return await _fail("Image file is empty")
        reference_mime_type = ingestion.mime_type or "image/jpeg"
        order_request_id = ingestion.request_id
        recipient_id = ingestion.external_user_id
        quote_id = ingestion.external_message_id

        provider_name, model_used, fallback_used = "dry_run", "none", False
        if DRY_RUN_IMAGE_MODE:
            logger.warning(
                f"[TEST MODE - No API Charge] White BG dry-run ingestion_id={ingestion_id}: "
                "echoing input image, no provider call made"
            )
            generated_bytes: Optional[bytes] = reference_image_bytes
        else:
            from app.ai.image_generation_manager import ImageGenerationManager

            # The customer's stored photo goes to the model as the reference
            # image; the prompt tells it to re-photograph THAT earring on white.
            # Team (ADMIN) orders are not counted against the daily spend
            # cap; the GENERATION_ENABLED kill switch still applies.
            from app.services import entitlement_service as ent
            from app.services.wallet_service import find_customer_by_phone as _find_cust

            admin_order = ent.is_admin(_find_cust(db, recipient_id))
            _release_db(db)           # no connection held during the 15-40 s provider wait (PERF-1)
            admin_key = None
            if admin_order:
                # Counted against the team ceiling (COST-2) instead of the customers' one.
                from app.ai.image_generation_manager import admin_spend_key, reserve_admin_slots

                blocked = await run_io(reserve_admin_slots, 1)
                if blocked:
                    return await _fail(f"Generation blocked: {blocked}")
                admin_key = admin_spend_key()
            result = await ImageGenerationManager().generate_image(
                prompt=build_ecommerce_shot_prompt(),
                context={"request_id": order_request_id, "aspect_ratio": "1:1"},
                reference_image=reference_image_bytes,
                reference_mime_type=reference_mime_type,
                **({"spend_reserved": True} if admin_order else {}),
            )
            if admin_key and not result.success:
                from app.ai.image_generation_manager import release_admin_slots

                await run_io(release_admin_slots, 1, admin_key)         # a failed team call does not use up the ceiling
            generated_bytes = _result_bytes(result) if result.success else None
            if not result.success or (not generated_bytes and not getattr(result, "image_url", None)
                                      and not getattr(result, "image_data", None)):
                return await _fail(f"Generation failed: {result.error}")
            provider_name = result.provider_name or ""
            model_used = result.model_used or ""
            fallback_used = bool(result.fallback_used)

        if not generated_bytes:
            return await _fail("Failed to decode generated image data")

        # Keep the delivered image next to the stored original, and record
        # which provider/model produced it (non-fatal: delivery is what the
        # customer paid for).
        output_path = image_path.parent / f"white_bg_{ingestion.id}.png"
        try:
            output_path.write_bytes(generated_bytes)
        except Exception as store_error:
            logger.error(f"White BG: could not store output for {ingestion_id}: {store_error}")
            output_path = None
        db.add(
            AuditLog(
                action=WHITE_BG_AUDIT_ACTION,
                resource_id=ingestion.id,
                resource_type="whatsapp_ingestion",
                status="success",
                details=json.dumps({
                    "provider": provider_name,
                    "model": model_used,
                    "fallback_used": fallback_used,
                    "dry_run": DRY_RUN_IMAGE_MODE,
                    "output_path": str(output_path) if output_path else None,
                }),
            )
        )
        if not _advance_status(db, ingestion_id, "processing", "generated"):
            return False

        media_id = await upload_media_to_meta(generated_bytes)
        if not media_id:
            return await _fail("Meta media upload failed", delivery=True)

        if DRY_RUN_IMAGE_MODE:
            caption = WHITE_BG_DRY_RUN_CAPTION
        else:
            from app.services.wallet_service import find_customer_by_phone, get_balance

            cust = find_customer_by_phone(db, recipient_id)
            rem_bal = get_balance(db, cust.whatsapp_id) if cust else 0
            caption = (
                "Here's your Clean Studio Shot ✨\n"
                f"Remaining balance: ₹{rem_bal:,}"
            )
        _release_db(db)

        sent = await send_image_to_whatsapp(
            recipient_id=recipient_id,
            media_id=media_id,
            caption=caption,
            reply_to_message_id=quote_id,
        )
        if not sent:
            return await _fail("Meta message send failed", delivery=True)

        if not _advance_status(db, ingestion_id, "generated", "delivered"):
            return True
        ingestion.error_message = None
        db.commit()
        logger.info(
            f"White BG delivered: ingestion_id={ingestion_id} provider={provider_name} model={model_used}"
        )
        from app.services import entitlement_service as ent
        from app.services import eta_service

        eta_service.record_duration("white_bg", time.monotonic() - started_at)
        await ent.record_trial_success(db, ingestion)  # trial orders only; never raises
        return True

    except Exception as e:
        logger.error(f"White BG unexpected error: ingestion_id={ingestion_id} error={e}")
        try:
            db.rollback()
            if ingestion is not None:
                db.refresh(ingestion)
                if ingestion.status not in ("failed", "delivery_failed", "delivered"):
                    return await _fail(f"Unexpected error: {e}"[:1000])
        except Exception as fail_error:
            logger.error(
                f"White BG could not record failure: ingestion_id={ingestion_id} "
                f"error={fail_error}"
            )
        return False
    finally:
        db.close()
