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
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings
from app.utils.logger import logger

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


# ─── Media retrieval ─────────────────────────────────────────────────────


async def get_media_url(media_id: str) -> Optional[str]:
    """Retrieve the temporary download URL for a Meta media ID."""
    if not settings.META_WHATSAPP_TOKEN:
        logger.error("META_WHATSAPP_TOKEN not configured — cannot retrieve media")
        return None

    url = META_MEDIA_URL_TEMPLATE.format(media_id=media_id)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}"},
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

    headers = {
        "Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}",
        "User-Agent": "curl/7.68.0",
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            response = await client.get(media_url, headers=headers)

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
    if not settings.META_APP_SECRET:
        logger.info("META_APP_SECRET not configured — skipping webhook signature verification")
        return True

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
        settings.META_APP_SECRET.encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(computed, signature_hash):
        logger.warning("Webhook signature mismatch — possible tampering")
        return False

    return True


# ─── Interactive button message ─────────────────────────────────────────


async def send_prompt_selection_buttons(recipient_id: str, ingestion_id: str) -> bool:
    """Send an interactive button message to the user asking them to select a style."""
    if not settings.META_WHATSAPP_TOKEN or not settings.META_PHONE_NUMBER_ID:
        logger.error("Meta credentials not configured — cannot send button message")
        return False

    url = META_SEND_MESSAGE_URL.format(phone_number_id=settings.META_PHONE_NUMBER_ID)

    prompt_ecommerce_id = f"prompt_ecommerce:{ingestion_id}"
    prompt_close_up_id = f"prompt_close_up:{ingestion_id}"
    prompt_ugc_id = f"prompt_ugc:{ingestion_id}"

    payload = {
        "messaging_product": "whatsapp",
        "to": recipient_id,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    "Please select the style for your earring image:\n"
                    "• Clean E-Commerce — catalog product shot on neutral backdrop\n"
                    "• Close-up on Ear — macro close-up worn on a model's ear\n"
                    "• UGC Lifestyle — authentic customer-style lifestyle photo"
                ),
            },
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": prompt_ecommerce_id,
                            "title": "Clean E-Commerce",
                        },
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": prompt_close_up_id,
                            "title": "Close-up on Ear",
                        },
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": prompt_ugc_id,
                            "title": "UGC Lifestyle",
                        },
                    },
                ],
            },
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            if response.status_code not in (200, 201):
                logger.error(f"Meta send button message failed: status={response.status_code}")
                return False

            data = response.json()
            messages = data.get("messages", [])
            if not messages:
                logger.error(f"Meta send button message response missing 'messages': {data}")
                return False

            logger.info(
                f"Meta button message sent: recipient={recipient_id} "
                f"message_id={messages[0].get('id', '')} "
                f"ingestion_id={ingestion_id}"
            )
            return True

    except httpx.TimeoutException:
        logger.error("Meta send button message timed out")
        return False
    except Exception as e:
        logger.error(f"Meta send button message failed: {e}")
        return False


async def send_feedback_buttons(recipient_id: str, ingestion_id: str) -> bool:
    """Send post-generation interactive feedback buttons."""
    return True


# ─── Plain text + CTA button messages ─────────────────────────────────────


async def _post_message_payload(
    payload: Dict[str, Any],
    label: str,
    reply_to_message_id: Optional[str] = None,
) -> bool:
    """POST a message payload to the Meta Send API with optional context quote."""
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
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            if response.status_code not in (200, 201):
                logger.error(f"Meta send {label} failed: status={response.status_code} body={response.text}")
                return False

            data = response.json()
            messages = data.get("messages", [])
            if not messages:
                logger.error(f"Meta send {label} response missing 'messages': {data}")
                return False

            logger.info(
                f"Meta {label} sent: recipient={payload.get('to', '')} "
                f"message_id={messages[0].get('id', '')}"
            )
            return True

    except httpx.TimeoutException:
        logger.error(f"Meta send {label} timed out")
        return False
    except Exception as e:
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
                f"recipient={recipient_id} media_id={media_id}"
            )

        if index < len(image_urls):
            await asyncio.sleep(CATALOG_PACK_SEND_THROTTLE_SECONDS)

    return sent_count


# Backwards-compatibility alias
send_6_pack_images_to_whatsapp = send_catalog_pack_images_to_whatsapp
send_7_pack_images_to_whatsapp = send_catalog_pack_images_to_whatsapp


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


# ─── Generation trigger ──────────────────────────────────────────────────


async def process_whatsapp_generation(
    ingestion_id: str,
    prompt_type: str = "prompt_ecommerce",
    custom_caption: Optional[str] = None,
    trigger_feedback: bool = True,
) -> bool:
    """Trigger the existing GemVision generation pipeline for a WhatsApp ingestion."""
    from app.database import SessionLocal
    from app.models.image import Image
    from app.models.whatsapp_ingestion import WhatsAppIngestion
    from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
    from app.services.earring_close_up_ears_prompt import build_close_up_ears_prompt
    from app.services.earring_ugc_style_prompt import build_ugc_style_prompt
    from app.ai.image_generation_manager import ImageGenerationManager

    db = SessionLocal()
    try:
        ingestion = db.query(WhatsAppIngestion).filter(
            WhatsAppIngestion.id == ingestion_id
        ).first()

        if not ingestion:
            logger.error(f"WhatsApp generation: ingestion not found: {ingestion_id}")
            return False

        if ingestion.status == "processing":
            logger.info(f"WhatsApp generation: skipping ingestion {ingestion_id} — already in progress")
            return False

        ingestion.status = "processing"
        ingestion.error_message = None
        db.commit()

        image_record = db.query(Image).filter(Image.id == ingestion.image_id).first()
        if not image_record:
            _fail_ingestion(db, ingestion, "Image record not found")
            return False

        from pathlib import Path
        image_path = Path(image_record.file_path)
        if not image_path.exists():
            _fail_ingestion(db, ingestion, f"Image file not found: {image_path}")
            return False

        reference_image_bytes = image_path.read_bytes()
        if len(reference_image_bytes) == 0:
            _fail_ingestion(db, ingestion, "Image file is empty")
            return False

        if prompt_type == "prompt_ecommerce":
            prompt = build_earring_ecommerce_prompt()
        elif prompt_type == "prompt_close_up":
            prompt = build_close_up_ears_prompt()
        elif prompt_type == "prompt_ugc":
            prompt = build_ugc_style_prompt()
        else:
            prompt = build_earring_ecommerce_prompt()

        manager = ImageGenerationManager()
        result = await manager.generate_image(
            prompt=prompt,
            context={"request_id": ingestion.request_id, "aspect_ratio": "4:5"},
            reference_image=reference_image_bytes,
            reference_mime_type=ingestion.mime_type or "image/jpeg",
        )

        if not result.success:
            _fail_ingestion(db, ingestion, f"Generation failed: {result.error}")
            return False

        ingestion.status = "generated"
        db.commit()

        image_data_url = result.image_url
        if not image_data_url:
            _fail_ingestion(db, ingestion, "No image data returned")
            return False

        generated_image_bytes = _data_url_to_bytes(image_data_url)
        if not generated_image_bytes:
            _fail_ingestion(db, ingestion, "Failed to decode image data")
            return False

        media_id = await upload_media_to_meta(generated_image_bytes)
        if not media_id:
            _fail_delivery(db, ingestion, "Meta media upload failed")
            return False

        send_caption = custom_caption if custom_caption is not None else "Here’s your E-commerce Pack 1 📦✨\nRemaining balance: ₹0"

        send_ok = await send_image_to_whatsapp(
            recipient_id=ingestion.external_user_id,
            media_id=media_id,
            caption=send_caption,
            reply_to_message_id=ingestion.external_message_id,
        )

        if not send_ok:
            _fail_delivery(db, ingestion, "Meta message send failed")
            return False

        ingestion.status = "delivered"
        ingestion.error_message = None
        db.commit()

        if trigger_feedback:
            try:
                await asyncio.sleep(1.5)
                await send_feedback_buttons(
                    recipient_id=ingestion.external_user_id,
                    ingestion_id=ingestion_id,
                )
            except Exception as fb_err:
                logger.error(f"Feedback buttons error: {fb_err}")

        return True

    except Exception as e:
        logger.error(f"WhatsApp generation exception: {e}")
        return False
    finally:
        db.close()


REFUND_AUDIT_ACTION = "whatsapp_generation_refund"


def _refund_failed_ingestion(db, ingestion) -> None:
    """Return the slot charge for an ingestion that delivered nothing.

    Idempotent per ingestion (an audit_logs row marks it refunded), so a
    manual retry that fails again never refunds twice. Never raises.
    ponytail: check-then-refund-then-record; a DB error between the refund
    commit and the audit commit could allow one extra refund on a later retry.
    """
    try:
        import json

        from app.models.audit_log import AuditLog
        from app.models.customer import Customer
        from app.services.wallet_service import price_per_image, refund_generation_charge

        already = (
            db.query(AuditLog.id)
            .filter(
                AuditLog.action == REFUND_AUDIT_ACTION,
                AuditLog.resource_id == ingestion.id,
            )
            .first()
        )
        if already:
            return

        from app.services.wallet_service import find_customer_by_phone

        cust = find_customer_by_phone(db, ingestion.external_user_id)
        if cust is None:
            logger.error(f"Refund skipped, customer not found: ingestion_id={ingestion.id}")
            return

        price = price_per_image()
        if not refund_generation_charge(db, cust.whatsapp_id, price):
            return
        db.add(
            AuditLog(
                action=REFUND_AUDIT_ACTION,
                resource_id=ingestion.id,
                resource_type="whatsapp_ingestion",
                status="success",
                details=json.dumps({"whatsapp_id": cust.whatsapp_id, "amount": price}),
            )
        )
        db.commit()
        logger.info(f"Refunded ₹{price} for failed ingestion_id={ingestion.id}")
    except Exception as e:
        logger.error(f"Refund for failed ingestion failed: ingestion_id={ingestion.id} error={e}")
        try:
            db.rollback()
        except Exception:
            pass


def _check_failure_rate(db) -> None:
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


def _data_url_to_bytes(data_url: str) -> Optional[bytes]:
    import base64
    try:
        if not data_url.startswith("data:"):
            return None
        _, payload = data_url.split(",", 1)
        return base64.b64decode(payload)
    except Exception:
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
) -> Optional[str]:
    """Generate one style of the catalog pack and return its image data URL."""
    if DRY_RUN_IMAGE_MODE:
        # ZERO-COST TEST MODE — never call the Gemini / Nano Banana API. Echo the
        # uploaded reference image back so the rest of the pipeline still runs.
        logger.warning(
            f"[TEST MODE - No API Charge] dry-run generation for style='{style_title}' "
            f"ingestion_id={ingestion_id}: returning input image, no provider call made"
        )
        return _bytes_to_data_url(reference_image_bytes, reference_mime_type)

    try:
        from app.ai.image_generation_manager import ImageGenerationManager

        manager = ImageGenerationManager()
        result = await manager.generate_image(
            prompt=prompt,
            context={"request_id": request_id, "aspect_ratio": "4:5"},
            reference_image=reference_image_bytes,
            reference_mime_type=reference_mime_type,
        )

        if not result.success or not result.image_url:
            logger.error(
                f"Catalog pack generation failed for style='{style_title}' "
                f"ingestion_id={ingestion_id}: {result.error}"
            )
            return None

        return result.image_url

    except Exception as e:
        logger.error(
            f"Catalog pack generation exception for style='{style_title}' "
            f"ingestion_id={ingestion_id}: {e}"
        )
        return None


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

    db = SessionLocal()
    try:
        ingestion = db.query(WhatsAppIngestion).filter(
            WhatsAppIngestion.id == ingestion_id
        ).first()

        if not ingestion:
            logger.error(f"Catalog pack generation: ingestion not found: {ingestion_id}")
            return False

        if ingestion.status == "processing":
            logger.info(
                f"Catalog pack generation: skipping ingestion {ingestion_id} — already in progress"
            )
            return False

        ingestion.status = "processing"
        ingestion.error_message = None
        db.commit()

        image_record = db.query(Image).filter(Image.id == ingestion.image_id).first()
        if not image_record:
            _fail_ingestion(db, ingestion, "Image record not found")
            return False

        from pathlib import Path
        image_path = Path(image_record.file_path)
        if not image_path.exists():
            _fail_ingestion(db, ingestion, f"Image file not found: {image_path}")
            return False

        reference_image_bytes = image_path.read_bytes()
        if len(reference_image_bytes) == 0:
            _fail_ingestion(db, ingestion, "Image file is empty")
            return False

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
            _fail_ingestion(db, ingestion, "No valid style prompt builders available")
            return False

        logger.info(
            f"Catalog pack generation started: ingestion_id={ingestion_id} "
            f"styles={len(style_jobs)} dev_throttle={MAX_STYLES_PER_PACK} "
            f"dry_run={DRY_RUN_IMAGE_MODE}"
        )

        gather_results = await asyncio.gather(
            *[
                _generate_single_pack_style(
                    ingestion_id=ingestion_id,
                    style_title=style_title,
                    prompt=prompt,
                    reference_image_bytes=reference_image_bytes,
                    reference_mime_type=ingestion.mime_type or "image/jpeg",
                    request_id=ingestion.request_id,
                )
                for style_title, prompt in style_jobs
            ]
        )

        generated_data_urls: List[str] = [
            data_url for data_url in gather_results if data_url
        ]

        if not generated_data_urls:
            _fail_ingestion(db, ingestion, "All catalog style generations failed")
            return False

        ingestion.status = "generated"
        db.commit()

        media_ids: List[str] = []
        for data_url in generated_data_urls:
            generated_image_bytes = _data_url_to_bytes(data_url)
            if not generated_image_bytes:
                continue

            media_id = await upload_media_to_meta(generated_image_bytes)
            if not media_id:
                continue

            media_ids.append(media_id)

        if not media_ids:
            _fail_delivery(db, ingestion, "All Meta media uploads failed")
            return False

        from app.services.wallet_service import find_customer_by_phone

        cust = find_customer_by_phone(db, ingestion.external_user_id)
        rem_bal = int(cust.wallet_balance or 0) if cust else 0
        balance_text = f"₹{rem_bal:,}"

        sent_count = await send_catalog_pack_images_to_whatsapp(
            recipient_id=ingestion.external_user_id,
            image_urls=media_ids,
            balance_text=balance_text,
            reply_to_message_id=ingestion.external_message_id,
        )
        total_images = len(media_ids)

        if sent_count == 0:
            _fail_delivery(db, ingestion, "Catalog pack Meta message delivery failed")
            return False

        if sent_count < total_images:
            # Partial delivery: the customer already received real value, so
            # this is not refunded -- just kept distinct from full success.
            ingestion.status = "delivered_partial"
            ingestion.error_message = f"Delivered {sent_count}/{total_images} images"
            db.commit()
            logger.warning(
                f"Catalog pack partially delivered: ingestion_id={ingestion_id} "
                f"sent={sent_count}/{total_images} recipient={ingestion.external_user_id}"
            )
            return True

        ingestion.status = "delivered"
        ingestion.error_message = None
        db.commit()

        logger.info(
            f"Catalog pack delivered: ingestion_id={ingestion_id} images={len(media_ids)} "
            f"recipient={ingestion.external_user_id}"
        )
        return True

    except Exception as e:
        logger.error(f"Catalog pack generation exception: ingestion_id={ingestion_id} error={e}")
        return False
    finally:
        db.close()