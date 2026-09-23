"""Meta WhatsApp Cloud API webhook routes.

Funded-slot batch gate:
- Every image = one Earring Catalog Pack (7 styles), priced at
  ``wallet_service.price_per_image()`` (configured via
  ``WALLET_IMAGE_PRICE_RUPEES``, default ₹500).
- slots = wallet_balance // price -> exactly that many packs execute.
- Every unfunded image immediately receives the exact recharge hold message.
- Funded packs execute via FastAPI BackgroundTasks (no Celery/Redis dependency).

Deduction and refund both go through ``app.services.wallet_service`` —
the single authoritative wallet-balance-mutation path (atomic guarded
UPDATE for charges, so a concurrent delivery can never overspend or drive
the balance negative).
"""

from typing import Any, Dict, List, Optional, Tuple
import asyncio
import json
import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.api.dependencies import require_auth
from app.config import settings
from app.database import get_db
from app.models.customer import Customer
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.repositories.base import BaseRepository
from app.services.generation_metrics import (
    TARGET_FAILURE_RATE,
    compute_generation_failure_rate,
)
from app.services.image_prevalidation_service import check_image_quality
from app.services.meta_whatsapp_service import (
    CATALOG_PACK_ACK_TEMPLATE,
    download_media,
    get_media_url,
    parse_webhook_entry,
    process_whatsapp_catalog_pack,
    send_whatsapp_cta_url_button,
    send_whatsapp_text,
    validate_image,
    verify_webhook_signature,
)
from app.services.razorpay_service import create_recharge_payment_link
from app.services.upload_service import UploadService
from app.services.wallet_service import (
    charge_customer_balance,
    format_rupees,
    get_customer,
    price_per_image,
    refund_generation_charge,
)
from app.utils.logger import logger

router = APIRouter(prefix="/api/meta", tags=["Meta WhatsApp Webhook"])

DEFAULT_PAYMENT_URL = settings.RECHARGE_PAYMENT_URL


def _hold_message(price: int) -> str:
    """Unfunded-image hold message (sent quoted against the user's photo)."""
    return (
        f"⚠️ Your balance is ₹0 for this image.\n\n"
        f"{format_rupees(price)} required to generate photos for this design.\n"
        f"Tap to recharge: {DEFAULT_PAYMENT_URL}"
    )


def _find_customer_safe(db: Session, sender: str) -> Optional[Customer]:
    """Helper to find customer regardless of leading + or 91 country code differences."""
    clean_sender = sender.lstrip("+").strip()
    c = get_customer(db, clean_sender) or get_customer(db, sender)
    if not c and len(clean_sender) >= 10:
        c = db.query(Customer).filter(Customer.whatsapp_id.contains(clean_sender[-10:])).first()
    return c


def _refund_pack_charge(db: Session, customer: Optional[Customer]) -> None:
    """Refund a single pack charge after a post-deduction failure."""
    if customer is None:
        return
    price = price_per_image()
    refund_generation_charge(db, customer.whatsapp_id, price)
    db.refresh(customer)


# ─── Background generation trigger ──────────────────────────────────────


async def _trigger_generation(ingestion_id: str) -> None:
    """Background task that re-runs the catalog pack for an ingestion.

    Called via BackgroundTasks after successful image ingestion (and by the
    manual retry endpoint). This keeps the webhook response fast (< 5s) while
    generation runs async — no Celery/Redis required.
    """
    try:
        success = await process_whatsapp_catalog_pack(ingestion_id)
        if success:
            logger.info(f"Background generation completed: ingestion_id={ingestion_id}")
        else:
            logger.warning(f"Background generation failed: ingestion_id={ingestion_id}")
    except Exception as e:
        logger.error(f"Background generation exception: ingestion_id={ingestion_id} error={e}")


@router.get("/webhook", summary="Meta webhook verification")
async def verify_webhook(
    hub_mode: Optional[str] = None,
    hub_verify_token: Optional[str] = None,
    hub_challenge: Optional[str] = None,
) -> PlainTextResponse:
    if hub_mode != "subscribe" or not hub_verify_token or hub_verify_token != settings.META_VERIFY_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid verification token or mode",
        )

    if not hub_challenge:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing challenge parameter",
        )

    return PlainTextResponse(content=hub_challenge)


@router.post("/webhook", summary="Receive WhatsApp webhook events")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    try:
        raw_body = await request.body()
        if not raw_body:
            return {"status": "ignored", "message": "Empty body"}
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        logger.error("Failed to parse webhook JSON payload: {}", e)
        return {"status": "error", "message": "Invalid JSON payload"}

    if settings.META_APP_SECRET:
        signature = request.headers.get("X-Hub-Signature-256")
        if not verify_webhook_signature(raw_body, signature):
            return {"status": "error", "message": "Invalid signature"}
    elif not settings.DEBUG:
        # Fail closed outside DEBUG mode: an unconfigured META_APP_SECRET in
        # a non-dev deployment must not silently accept unsigned webhook
        # payloads. DEBUG defaults to True and stays True for local/dev use,
        # so this does not change behavior there -- it only refuses unsigned
        # traffic once DEBUG is turned off for a real deployment.
        logger.error(
            "Webhook rejected: META_APP_SECRET is not configured and DEBUG "
            "is False -- refusing to accept an unsigned payload."
        )
        return {"status": "error", "message": "Webhook not configured"}

    if payload.get("object") == "whatsapp_business_account" and "entry" not in payload:
        return {"status": "ok"}

    if payload.get("object") != "whatsapp_business_account":
        return {"status": "ignored", "message": "Not a WhatsApp business account event"}

    entries = payload.get("entry", [])
    if not entries:
        return {"status": "ignored", "message": "No entries in webhook payload"}

    ingestion_repo = BaseRepository(WhatsAppIngestion, db)
    image_events: List[Dict[str, Any]] = []

    for entry in entries:
        events = parse_webhook_entry(entry)

        for event in events:
            event_type = event.get("type", "")

            if event_type == "status":
                continue

            if event_type == "text":
                sender = event.get("sender", "")
                raw_text = event.get("body", "").strip()
                lower_text = raw_text.lower()
                logger.info("Text message received: sender={} text='{}'", sender, raw_text)

                if "name:" in lower_text and ("business" in lower_text or "gst" in lower_text):
                    user_name = "there"
                    biz_name = "Jewelry Business"
                    gst_val = "N/A"
                    addr_val = "N/A"

                    for line in raw_text.splitlines():
                        line_clean = line.strip()
                        l_low = line_clean.lower()
                        if l_low.startswith("name:"):
                            extracted = line_clean.split(":", 1)[1].strip()
                            if extracted:
                                user_name = extracted.split()[0]
                        elif "business name" in l_low and ":" in line_clean:
                            extracted_biz = line_clean.split(":", 1)[1].strip()
                            if extracted_biz:
                                biz_name = extracted_biz
                        elif "gst" in l_low and ":" in line_clean:
                            extracted_gst = line_clean.split(":", 1)[1].strip()
                            if extracted_gst:
                                # t9: only accept a value that either looks like a
                                # real 15-char GSTIN or is an explicit "no GST"
                                # answer; anything else falls back to N/A instead
                                # of silently storing malformed text.
                                _gst_norm = extracted_gst.replace(" ", "").upper()
                                _gst_none = extracted_gst.strip().lower() in ("na", "n/a", "none", "no", "nil", "-")
                                _gst_valid = bool(re.match(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]$", _gst_norm))
                                if _gst_valid:
                                    gst_val = _gst_norm
                                elif _gst_none:
                                    gst_val = "N/A"
                                # else: leave gst_val at its previous value (default "N/A")
                        elif "address" in l_low and ":" in line_clean:
                            extracted_addr = line_clean.split(":", 1)[1].strip()
                            if extracted_addr:
                                addr_val = extracted_addr

                    clean_sender = sender.lstrip("+").strip()
                    cust = _find_customer_safe(db, sender)

                    if cust is not None:
                        cust.full_name = user_name
                        cust.business_name = biz_name
                        cust.gst_number = gst_val
                        cust.address = addr_val
                        cust.is_registered = True
                        db.commit()
                    else:
                        try:
                            cust = BaseRepository(Customer, db).create(
                                whatsapp_id=clean_sender,
                                full_name=user_name,
                                business_name=biz_name,
                                gst_number=gst_val,
                                address=addr_val,
                                wallet_balance=0,
                                is_registered=True,
                            )
                        except Exception as create_error:
                            db.rollback()
                            logger.error(f"Onboarding customer creation failed: {create_error}")
                            cust = _find_customer_safe(db, sender)

                    if cust is None:
                        continue

                    confirm_msg = (
                        f"Congratulations {user_name}! You’re registered with Moraa Studio 🎉\n"
                        f"You’re all set to start creating stunning product photos.\n\n"
                        f"Need to update your details later? Just send the same form again anytime."
                    )
                    try:
                        pay_url = await create_recharge_payment_link(
                            customer_phone=sender,
                            customer_name=user_name,
                            amount=500,
                        )
                    except Exception as e:
                        logger.error("Failed to generate registration recharge link: {}", e)
                        pay_url = DEFAULT_PAYMENT_URL

                    await send_whatsapp_cta_url_button(
                        recipient_id=sender,
                        body_text=confirm_msg,
                        button_label="Recharge to use",
                        url=pay_url or DEFAULT_PAYMENT_URL,
                    )
                    continue

                if re.search(r"\b(hi|hii|hello|hey|start)\b", lower_text):
                    msg_part_1 = (
                        "Hi there! Welcome to Moraa Studio ✨\n"
                        "We help you turn raw jewelry photos into polished, e-commerce ready images "
                    )
                    msg_part_2 = (
                        "Let’s get you set up, it only takes a minute!\n\n"
                        "Quick registration 📋\n"
                        "Copy this, fill in your details and send it right back:\n\n"
                        "Name:\n"
                        "Business name:\n"
                        "GST number:\n"
                        "Business address:"
                    )
                    await send_whatsapp_text(sender, msg_part_1)
                    await asyncio.sleep(1)
                    await send_whatsapp_text(sender, msg_part_2)
                    continue

                recharge_match = re.search(r"\b(?:recharge|pay|add)\s*(?:rs\.?|inr|₹)?\s*(\d+)\b", lower_text)
                if recharge_match:
                    requested_amount = int(recharge_match.group(1))
                    if requested_amount < 500:
                        await send_whatsapp_text(
                            sender,
                            "Minimum recharge amount is ₹500 ⚠️\nPlease enter an amount of ₹500 or more."
                        )
                        continue

                    cust = _find_customer_safe(db, sender)
                    cust_name = getattr(cust, "full_name", "Customer") if cust else "Customer"
                    try:
                        pay_url = await create_recharge_payment_link(
                            customer_phone=sender,
                            customer_name=cust_name,
                            amount=requested_amount,
                        )
                    except Exception as e:
                        logger.error("Failed to generate custom recharge link: {}", e)
                        pay_url = DEFAULT_PAYMENT_URL

                    await send_whatsapp_cta_url_button(
                        recipient_id=sender,
                        body_text=f"Here is your recharge link for ₹{requested_amount} 💳\nTap below to complete the payment.",
                        button_label=f"Pay ₹{requested_amount}"[:20],
                        url=pay_url or DEFAULT_PAYMENT_URL,
                    )
                    continue

                continue

            if event_type == "interactive" and event.get("subtype") == "button_reply":
                button_reply = event.get("button_reply", {})
                b_id = button_reply.get("id", "")
                sender = event.get("sender", "")

                if b_id.startswith("feedback_"):
                    fb_response = (
                        "Thank you so much for the love! Glad you liked it 🎉 Send your next photo anytime!"
                        if b_id.startswith("feedback_positive")
                        else "Thanks for letting us know! We’re constantly training our model. You can retry with another angle or lighting 📸"
                    )
                    await send_whatsapp_text(recipient_id=sender, message_text=fb_response)
                    continue

                continue

            if event_type == "image":
                image_events.append(event)

    if not image_events:
        return {"status": "ok", "images_processed": 0}

    sender = image_events[0].get("sender", "")
    raw_sender = sender.strip()
    clean_sender = raw_sender.lstrip("+").strip()
    phone_suffix = clean_sender[-10:] if len(clean_sender) >= 10 else clean_sender

    # ── FUNDED SLOT GATE ──
    # Load the customer row ONCE with a clean contains() query (handles
    # "+91…", "91…" and bare numbers alike), then derive paid slots from the
    # real balance. Exactly `slots` images execute a full 7-style pack; every
    # remaining image immediately receives the exact hold message — never a
    # silent drop.
    price = price_per_image()
    customer = db.query(Customer).filter(Customer.whatsapp_id.contains(phone_suffix)).first()
    current_bal = int(customer.wallet_balance or 0) if customer else 0
    slots = current_bal // price

    processed_ingestion_ids: List[str] = []

    for img_ev in image_events:
        message_id = img_ev.get("message_id", "")
        media_id = img_ev.get("media_id", "")
        mime_type = img_ev.get("mime_type", "")
        caption = img_ev.get("caption", "")
        timestamp = img_ev.get("timestamp", "")

        if not media_id:
            logger.warning(f"Image message missing media_id: message_id={message_id[:20]}...")
            ingestion_repo.create(
                external_user_id=sender,
                external_message_id=message_id,
                external_media_id="",
                channel="whatsapp",
                caption=caption,
                mime_type=mime_type,
                timestamp=timestamp,
                status="failed",
                error_message="Missing media_id in webhook payload",
            )
            db.commit()
            continue

        if slots <= 0:
            # Unfunded image — exact hold message, quoted against the photo.
            await send_whatsapp_text(
                recipient_id=sender,
                message_text=_hold_message(price),
                reply_to_message_id=message_id,
            )
            continue

        # Duplicate webhook deliveries must never double-charge or re-queue.
        existing = ingestion_repo.find_first(external_message_id=message_id)
        if existing:
            continue

        # Fetch and validate BEFORE charging: an unreadable or rejected photo
        # costs the customer nothing and never reaches generation.
        media_url = await get_media_url(media_id)
        if not media_url:
            continue

        download_result: Optional[Tuple[bytes, str]] = await download_media(media_url)
        if not download_result:
            continue

        image_bytes, content_type = download_result
        is_valid, _validation_error = validate_image(image_bytes, content_type)
        if not is_valid:
            continue

        if settings.IMAGE_PREVALIDATION_ENABLED:
            quality = await check_image_quality(image_bytes, content_type)
            if not quality.approved:
                await send_whatsapp_text(
                    sender, quality.rejection_message, reply_to_message_id=message_id
                )
                continue

        # Atomic guarded-UPDATE deduction (app.services.wallet_service) — the
        # single authoritative wallet-debit path, shared with charge_generation().
        # Declines safely (no charge) if the balance changed concurrently since
        # `slots` was computed, instead of ever overspending.
        charged, _balance_after = charge_customer_balance(db, customer, price)
        if not charged:
            await send_whatsapp_text(
                recipient_id=sender,
                message_text=_hold_message(price),
                reply_to_message_id=message_id,
            )
            continue
        slots -= 1

        ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
        ext = ext_map.get(content_type, "jpg")
        filename = f"whatsapp_{message_id[:20]}.{ext}"

        upload_service = UploadService(db)
        try:
            upload_result = await upload_service.process_upload(
                file_data=image_bytes,
                filename=filename,
                file_size=len(image_bytes),
                mime_type=content_type,
            )
        except Exception as upload_error:
            logger.error(
                f"WhatsApp upload failed: message_id={message_id[:20]}... error={upload_error}"
            )
            _refund_pack_charge(db, customer)
            continue

        ingestion = ingestion_repo.create(
            external_user_id=sender,
            external_message_id=message_id,
            external_media_id=media_id,
            channel="whatsapp",
            caption=caption,
            mime_type=content_type,
            timestamp=timestamp,
            image_id=upload_result.id,
            file_size=len(image_bytes),
            status="pack_queued",
        )
        db.commit()

        processed_ingestion_ids.append(ingestion.id)

        # Quoted ACK on the funded image.
        await send_whatsapp_text(sender, CATALOG_PACK_ACK_TEMPLATE, reply_to_message_id=message_id)

        # Execute the 7-style catalog pack via FastAPI background task (no Celery/Redis).
        background_tasks.add_task(process_whatsapp_catalog_pack, ingestion.id)

    return {
        "status": "ok",
        "queued": len(processed_ingestion_ids),
    }


@router.post(
    "/webhook/retry/{ingestion_id}",
    summary="Retry delivery for a failed ingestion",
    description=(
        "Manually retry generation/delivery for an ingestion that failed. "
        "Only works for status 'failed' or 'delivery_failed'. "
        "Re-runs the 7-style catalog pack and attempts delivery again."
    ),
)
async def retry_delivery(
    ingestion_id: str,
    background_tasks: BackgroundTasks,
    current_user: Any = Depends(require_auth),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Retry a failed WhatsApp ingestion.

    Requires an authenticated user. This endpoint re-runs a billable
    generation task, so it must never be publicly executable.
    """
    ingestion = db.query(WhatsAppIngestion).filter(
        WhatsAppIngestion.id == ingestion_id
    ).first()

    if not ingestion:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ingestion not found: {ingestion_id}",
        )

    if ingestion.status not in ("failed", "delivery_failed"):
        return {
            "status": "error",
            "message": f"Cannot retry ingestion in status '{ingestion.status}'",
        }

    # Reset status so the catalog pack will be re-processed.
    ingestion.status = "stored"
    ingestion.error_message = None
    db.commit()

    background_tasks.add_task(_trigger_generation, ingestion.id)

    logger.info(
        f"Retry triggered: ingestion_id={ingestion_id} "
        f"user={ingestion.external_user_id}"
    )

    return {
        "status": "queued",
        "message": "Generation retry queued",
        "ingestion_id": ingestion_id,
    }


# ─── GET — Ingestion Status ─────────────────────────────────────────────


@router.get(
    "/webhook/status/{ingestion_id}",
    summary="Check ingestion status",
)
async def get_ingestion_status(
    ingestion_id: str,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Get the current status of a WhatsApp ingestion."""
    ingestion = db.query(WhatsAppIngestion).filter(
        WhatsAppIngestion.id == ingestion_id
    ).first()

    if not ingestion:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ingestion not found: {ingestion_id}",
        )

    return {
        "id": ingestion.id,
        "request_id": ingestion.request_id,
        "status": ingestion.status,
        "channel": ingestion.channel,
        "external_user_id": ingestion.external_user_id,
        "image_id": ingestion.image_id,
        "file_size": ingestion.file_size,
        "error_message": ingestion.error_message,
        "created_at": str(ingestion.created_at),
        "updated_at": str(ingestion.updated_at),
    }


@router.get("/webhook/health", summary="Meta webhook health check")
async def webhook_health() -> Dict[str, Any]:
    return {
        "status": "ready",
        "service": "meta-whatsapp-webhook",
        "verify_token_configured": bool(settings.META_VERIFY_TOKEN),
        "access_token_configured": bool(settings.META_WHATSAPP_TOKEN),
        "phone_number_id_configured": bool(settings.META_PHONE_NUMBER_ID),
        "app_secret_configured": bool(settings.META_APP_SECRET),
        "generation_enabled": bool(settings.OPENAI_API_KEY or settings.GEMINI_API_KEY),
    }


@router.get(
    "/webhook/failure-rate",
    summary="Windowed generation failure rate",
    description=(
        "Failed / total completed WhatsApp catalog-pack generation attempts "
        "over a trailing window, computed from the existing "
        "WhatsAppIngestion status field. No customer-identifying data is "
        "included. Target ceiling: 15%."
    ),
)
async def get_generation_failure_rate(
    window_hours: int = 24,
    current_user: Any = Depends(require_auth),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    report = compute_generation_failure_rate(db, window_hours=window_hours)
    return {
        "window_hours": report.window_hours,
        "total_attempts": report.total_attempts,
        "failed_attempts": report.failed_attempts,
        "failure_rate": report.failure_rate,
        "target_failure_rate": TARGET_FAILURE_RATE,
        "exceeds_target": report.exceeds_target,
    }