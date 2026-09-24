"""Meta WhatsApp Cloud API webhook routes.

Photo -> product choice -> charge:
- Every photo is stored (status ``awaiting_choice``) WITHOUT any charge and
  the customer gets two reply buttons: Ecommerce Shot Only
  (``WHITE_BG_PRICE_RUPEES``, default ₹50) or E-Com Pack 1
  (``WALLET_IMAGE_PRICE_RUPEES``, default ₹500).
- The tap is claimed with one guarded UPDATE, then the chosen price is debited
  atomically; if the wallet cannot pay, nothing is queued and the customer
  gets the recharge message quoting their real balance.
- Orders execute via FastAPI BackgroundTasks (no Celery/Redis dependency):
  Ecommerce Shot -> process_whatsapp_white_bg, Pack 1 -> the existing
  process_whatsapp_catalog_pack.

Deduction and refund both go through ``app.services.wallet_service`` —
the single authoritative wallet-balance-mutation path (atomic guarded
UPDATE for charges, so a concurrent delivery can never overspend or drive
the balance negative).
"""

from typing import Any, Dict, List, Optional, Tuple
import asyncio
import json
from datetime import datetime, timedelta, timezone
import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import require_auth
from app.config import settings
from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.whatsapp_ingestion import (
    PRODUCT_PACK_1,
    PRODUCT_WHITE_BG,
    WhatsAppIngestion,
)
from app.repositories.base import BaseRepository
from app.services.generation_metrics import (
    TARGET_FAILURE_RATE,
    compute_generation_failure_rate,
)
from app.services.image_prevalidation_service import check_image_quality
from app.services import meta_whatsapp_service as _mws
from app.services.meta_whatsapp_service import (
    CATALOG_PACK_ACK_TEMPLATE,
    PRODUCT_BUTTON_WHITE,
    parse_product_button_id,
    process_whatsapp_white_bg,
    send_product_selection_buttons,
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
    find_customer_by_phone,
    format_rupees,
    get_balance,
    get_customer,
    price_per_image,
    refund_generation_charge,
)
from app.utils.logger import logger

router = APIRouter(prefix="/api/meta", tags=["Meta WhatsApp Webhook"])

DEFAULT_PAYMENT_URL = settings.RECHARGE_PAYMENT_URL


def _hold_message(price: int, balance: Optional[int] = None, product: str = "this image") -> str:
    """Recharge message when the wallet cannot pay for an order.

    Quotes the REAL balance read from the database. (The old copy always said
    "Your balance is ₹0", even when the customer had money.)
    """
    balance_line = f"Your wallet balance is {format_rupees(balance)}.\n" if balance is not None else ""
    return (
        f"⚠️ {balance_line}"
        f"{format_rupees(price)} is required for {product}.\n"
        f"Tap to recharge: {DEFAULT_PAYMENT_URL}"
    )


def _find_customer_safe(db: Session, sender: str) -> Optional[Customer]:
    """Helper to find customer regardless of leading + or 91 country code differences."""
    return find_customer_by_phone(db, sender)


FEEDBACK_AUDIT_ACTION = "whatsapp_feedback"


def _save_feedback(db: Session, sender: str, button_id: str) -> None:
    """Persist a thumbs up/down against the ingestion it rates. Never raises.

    Button ids are ``feedback_positive[_<ingestion_id>]`` /
    ``feedback_negative[_<ingestion_id>]``; without an id suffix the sender's
    latest ingestion is used.
    """
    try:
        rating = "positive" if button_id.startswith("feedback_positive") else "negative"
        suffix = button_id.split("_", 2)[2] if button_id.count("_") >= 2 else ""
        ingestion = None
        if suffix:
            ingestion = db.query(WhatsAppIngestion).filter(WhatsAppIngestion.id == suffix).first()
        if ingestion is None:
            clean = (sender or "").lstrip("+").strip()
            ingestion = (
                db.query(WhatsAppIngestion)
                .filter(WhatsAppIngestion.external_user_id.in_({sender, clean, "+" + clean}))
                .order_by(WhatsAppIngestion.created_at.desc())
                .first()
            )
        db.add(
            AuditLog(
                action=FEEDBACK_AUDIT_ACTION,
                resource_id=ingestion.id if ingestion else None,
                resource_type="whatsapp_ingestion",
                status=rating,
                details=json.dumps({"whatsapp_id": sender, "button_id": button_id}),
            )
        )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Feedback persistence failed: {e}")


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


# ─── Product choice: Ecommerce Shot Only (₹50) / E-Com Pack 1 (₹500) ────
# A photo is stored WITHOUT any charge and the customer is asked which
# product to create. The reply-button id carries the ingestion id, so the
# choice is tied to that exact stored photo in the database (survives
# restarts, multiple workers and late taps). The wallet is debited only when
# a button is tapped, through wallet_service.charge_customer_balance.

PRODUCT_LABELS = {PRODUCT_WHITE_BG: "Ecommerce Shot Only", PRODUCT_PACK_1: "E-Com Pack 1"}

ALREADY_CHOSEN_MESSAGE = (
    "This photo already has an order, so nothing new was charged. "
    "Send the photo again if you want to place another order."
)
UNKNOWN_CHOICE_MESSAGE = "Sorry, we couldn't find that photo. Please send it again."
UNREADABLE_IMAGE_MESSAGE = (
    "Sorry, we couldn't read this photo. Please send a clear JPG, PNG or WebP photo of the earring."
)

# A paid White order stuck here for this long (crash / restart) may be
# re-queued by the authenticated retry endpoint.
STUCK_WHITE_AFTER = timedelta(minutes=10)


def _white_bg_price() -> int:
    """Ecommerce Shot Only price in whole rupees (independent of Pack 1)."""
    return max(int(settings.WHITE_BG_PRICE_RUPEES), 1)


def _product_price(product_code: str) -> int:
    return _white_bg_price() if product_code == PRODUCT_WHITE_BG else price_per_image()


def _same_sender(stored: str, sender: str) -> bool:
    a = (stored or "").strip().lstrip("+")
    b = (sender or "").strip().lstrip("+")
    return bool(a) and bool(b) and (a == b or a[-10:] == b[-10:])


async def _ingest_image_for_choice(db: Session, event: Dict[str, Any]) -> Optional[str]:
    """Store one incoming photo and send the product-choice buttons.

    No money moves here. Returns the ingestion id when the buttons were sent.
    """
    sender = (event.get("sender") or "").strip()
    message_id = event.get("message_id", "")
    media_id = event.get("media_id", "")
    caption = event.get("caption", "")
    timestamp = event.get("timestamp", "")

    # CLAIM the message first. external_message_id is UNIQUE, so a Meta retry
    # or a concurrent copy of this delivery stops here and never reaches the
    # balance message, the AI pre-check or a second set of buttons.
    ingestion = WhatsAppIngestion(
        external_user_id=sender,
        external_message_id=message_id,
        external_media_id=media_id,
        channel="whatsapp",
        caption=caption,
        mime_type=event.get("mime_type") or None,
        timestamp=timestamp,
        status="received",
    )
    try:
        db.add(ingestion)
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.info(f"Duplicate image delivery ignored: message_id={message_id[:20]}...")
        return None

    def _reject(reason: str) -> None:
        ingestion.status = "rejected"
        ingestion.error_message = reason
        db.commit()

    white_price, pack_price = _white_bg_price(), price_per_image()
    min_price = min(white_price, pack_price)
    customer = find_customer_by_phone(db, sender)
    balance = get_balance(db, customer.whatsapp_id) if customer else 0
    if customer is None or balance < min_price:
        ingestion.status = "unfunded"
        ingestion.error_message = f"Balance {balance} below minimum {min_price} at upload"
        db.commit()
        await send_whatsapp_text(
            recipient_id=sender,
            message_text=_hold_message(
                min_price, get_balance(db, customer.whatsapp_id) if customer else 0, "an order"
            ),
            reply_to_message_id=message_id,
        )
        return None

    # Fetch, validate and AI pre-check once per photo -- before any charge.
    media_url = await get_media_url(media_id)
    download_result: Optional[Tuple[bytes, str]] = await download_media(media_url) if media_url else None
    if not download_result:
        _reject("Media download failed")
        await send_whatsapp_text(sender, UNREADABLE_IMAGE_MESSAGE, reply_to_message_id=message_id)
        return None
    image_bytes, content_type = download_result
    is_valid, validation_error = validate_image(image_bytes, content_type)
    if not is_valid:
        _reject(f"Invalid image: {validation_error}")
        await send_whatsapp_text(sender, UNREADABLE_IMAGE_MESSAGE, reply_to_message_id=message_id)
        return None
    if settings.IMAGE_PREVALIDATION_ENABLED:
        quality = await check_image_quality(image_bytes, content_type)
        if not quality.approved:
            _reject("Rejected by image pre-check")
            await send_whatsapp_text(sender, quality.rejection_message, reply_to_message_id=message_id)
            return None

    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(content_type, "jpg")
    try:
        upload_result = await UploadService(db).process_upload(
            file_data=image_bytes,
            filename=f"whatsapp_{message_id[:20]}.{ext}",
            file_size=len(image_bytes),
            mime_type=content_type,
        )
    except Exception as upload_error:
        logger.error(f"WhatsApp upload failed: message_id={message_id[:20]}... error={upload_error}")
        db.rollback()
        _reject(f"Upload failed: {upload_error}"[:1000])
        await send_whatsapp_text(
            sender, "Sorry, we couldn't save your photo. Please send it again.",
            reply_to_message_id=message_id,
        )
        return None

    ingestion.image_id = upload_result.id
    ingestion.file_size = len(image_bytes)
    ingestion.mime_type = content_type
    ingestion.status = "awaiting_choice"
    db.commit()

    # Re-read the balance at send time: download + AI pre-check can take
    # ~10 s, and a payment or another order may have committed meanwhile.
    sent = await send_product_selection_buttons(
        recipient_id=sender,
        ingestion_id=ingestion.id,
        white_price=white_price,
        pack_price=pack_price,
        balance=get_balance(db, customer.whatsapp_id),
        reply_to_message_id=message_id,
    )
    if not sent:
        logger.error(f"Product selection buttons NOT sent: ingestion_id={ingestion.id}")
    return ingestion.id


async def _handle_product_choice(
    db: Session,
    sender: str,
    button: str,
    ingestion_id: str,
) -> Optional[Tuple[Any, str]]:
    """Charge for the chosen product. Returns (worker, ingestion_id) to queue,
    or None when nothing may run.

    1. The photo must exist and belong to this sender.
    2. One guarded UPDATE moves it awaiting_choice -> choice_claimed, so a
       double tap, a duplicate callback or a concurrent copy can never charge
       or generate twice (database-enforced, not an in-memory lock).
    3. Atomic wallet debit via wallet_service. If it is declined, the claim is
       released (the photo can be chosen again after a recharge) and NOTHING
       is queued.
    """
    product = PRODUCT_WHITE_BG if button == PRODUCT_BUTTON_WHITE else PRODUCT_PACK_1
    label = PRODUCT_LABELS[product]

    ingestion = db.query(WhatsAppIngestion).filter(WhatsAppIngestion.id == ingestion_id).first()
    if ingestion is None or not _same_sender(ingestion.external_user_id, sender):
        logger.warning(f"Product choice for unknown/foreign ingestion: id={ingestion_id} sender={sender}")
        await send_whatsapp_text(recipient_id=sender, message_text=UNKNOWN_CHOICE_MESSAGE)
        return None
    quote_id = ingestion.external_message_id

    claimed = (
        db.query(WhatsAppIngestion)
        .filter(
            WhatsAppIngestion.id == ingestion_id,
            WhatsAppIngestion.status == "awaiting_choice",
        )
        .update(
            {WhatsAppIngestion.status: "choice_claimed", WhatsAppIngestion.product_code: product},
            synchronize_session=False,
        )
    )
    db.commit()
    if claimed != 1:
        logger.info(f"Product choice ignored, ingestion {ingestion_id} not awaiting a choice")
        await send_whatsapp_text(sender, ALREADY_CHOSEN_MESSAGE, reply_to_message_id=quote_id)
        return None

    price = _product_price(product)
    dry_run = product == PRODUCT_WHITE_BG and bool(_mws.DRY_RUN_IMAGE_MODE)
    customer = find_customer_by_phone(db, sender)
    if customer is None:
        charged = False
    elif dry_run:
        # Same balance rule, but no money moves in dry-run.
        charged = get_balance(db, customer.whatsapp_id) >= price
    else:
        charged, _balance_after = charge_customer_balance(db, customer, price)

    if not charged:
        db.query(WhatsAppIngestion).filter(
            WhatsAppIngestion.id == ingestion_id,
            WhatsAppIngestion.status == "choice_claimed",
        ).update(
            {WhatsAppIngestion.status: "awaiting_choice", WhatsAppIngestion.product_code: None},
            synchronize_session=False,
        )
        db.commit()
        balance = get_balance(db, customer.whatsapp_id) if customer else 0
        await send_whatsapp_text(
            recipient_id=sender,
            message_text=_hold_message(price, balance, label),
            reply_to_message_id=quote_id,
        )
        return None

    db.refresh(ingestion)
    ingestion.amount_charged = 0 if dry_run else price
    ingestion.status = "white_queued" if product == PRODUCT_WHITE_BG else "pack_queued"
    db.commit()

    if product == PRODUCT_WHITE_BG:
        await send_whatsapp_text(
            sender,
            "✨ Processing your Ecommerce Shot (1 image"
            + (", test mode - no charge" if dry_run else f", ₹{price}")
            + ")... Please allow 20-30 seconds.",
            reply_to_message_id=quote_id,
        )
        return process_whatsapp_white_bg, ingestion.id
    # Existing Pack 1 acknowledgement + existing Pack 1 worker, unchanged.
    await send_whatsapp_text(sender, CATALOG_PACK_ACK_TEMPLATE, reply_to_message_id=quote_id)
    return process_whatsapp_catalog_pack, ingestion.id


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

                    # Registration only ever writes profile fields. The wallet
                    # balance is never credited or reset here: an existing
                    # customer keeps their exact balance, a new one starts at
                    # ₹0. Money is added only by the signed Razorpay webhook.
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
                        f"You’re all set to start creating stunning product photos.\n"
                        f"Wallet balance: ₹{get_balance(db, cust.whatsapp_id):,}\n\n"
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

                product_choice = parse_product_button_id(b_id)
                if product_choice:
                    job = await _handle_product_choice(db, sender, *product_choice)
                    if job:
                        # Ecommerce Shot -> process_whatsapp_white_bg,
                        # Pack 1 -> existing process_whatsapp_catalog_pack.
                        background_tasks.add_task(*job)
                    continue

                if b_id.startswith("feedback_"):
                    _save_feedback(db, sender, b_id)
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

    # Every photo is stored first and the customer chooses the product with a
    # reply button (see _handle_product_choice). No charge happens here.
    awaiting: List[str] = []
    for img_ev in image_events:
        message_id = img_ev.get("message_id", "")
        # Duplicate Meta deliveries of an already-stored photo stop here; a
        # concurrent copy is stopped by the unique external_message_id claim.
        if message_id and ingestion_repo.find_first(external_message_id=message_id):
            continue
        if not img_ev.get("media_id"):
            logger.warning(f"Image message missing media_id: message_id={message_id[:20]}...")
            ingestion_repo.create(
                external_user_id=img_ev.get("sender", ""),
                external_message_id=message_id,
                external_media_id="",
                channel="whatsapp",
                caption=img_ev.get("caption", ""),
                mime_type=img_ev.get("mime_type", ""),
                timestamp=img_ev.get("timestamp", ""),
                status="failed",
                error_message="Missing media_id in webhook payload",
            )
            db.commit()
            continue
        ingestion_id = await _ingest_image_for_choice(db, img_ev)
        if ingestion_id:
            awaiting.append(ingestion_id)

    return {
        "status": "ok",
        "awaiting_choice": len(awaiting),
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

    updated_at = ingestion.updated_at
    if updated_at is not None and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    stuck_white = (
        ingestion.product_code == PRODUCT_WHITE_BG
        and ingestion.status in ("white_queued", "processing")
        and updated_at is not None
        and datetime.now(timezone.utc) - updated_at > STUCK_WHITE_AFTER
    )
    if ingestion.status not in ("failed", "delivery_failed") and not stuck_white:
        return {
            "status": "error",
            "message": f"Cannot retry ingestion in status '{ingestion.status}'",
        }

    # Reset status so the catalog pack will be re-processed.
    ingestion.status = "stored"
    ingestion.error_message = None
    db.commit()

    # Dispatch by the STORED product chosen with the reply button.
    # NULL / PACK_1 -> Pack 1 catalog worker, exactly as before.
    if ingestion.product_code == PRODUCT_WHITE_BG:
        background_tasks.add_task(process_whatsapp_white_bg, ingestion.id)
    else:
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