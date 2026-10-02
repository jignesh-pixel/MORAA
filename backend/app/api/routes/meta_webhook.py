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

from typing import Any, Dict, Iterator, List, Optional, Tuple
import asyncio
import hmac
import json
from datetime import datetime, timedelta, timezone
import re
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import require_admin
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
    send_registration_flow,
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
from app.services.gst_service import (
    gst_check_pending,
    handle_gst_button,
    handle_gstin_reply,
    is_awaiting_gstin,
    verify_after_registration,
)
from app.services.upload_service import UploadService
from app.services.ops_forward import divert_ops_messages
from app.services.whatsapp_pay_service import (
    handle_payment_status_event,
    is_native_pay_active,
    send_payment_unavailable,
    try_send_native_recharge,
)
from app.services import entitlement_service as ent
from app.models.wallet_transaction import KIND_DEBIT_ORDER, WalletTransaction
from app.services.wallet_service import (
    charge_customer_balance,
    find_customer_by_phone,
    format_rupees,
    get_balance,
    get_customer,
    price_per_image,
)
from app.services import consent_service, data_lifecycle, eta_service
from app.utils.logger import logger, mask_phone
from app.ai.image_generation_manager import generation_capacity_blocked
from app.utils.executors import run_cpu, run_io
from app.utils.phone import same_phone
from app.services.message_dedupe import claim_message, release_messages

router = APIRouter(prefix="/api/meta", tags=["Meta WhatsApp Webhook"])

DEFAULT_PAYMENT_URL = settings.RECHARGE_PAYMENT_URL


def _hold_body(price: int, balance: Optional[int] = None, product: str = "this image") -> str:
    """_hold_message without the link line, for the in-chat WhatsApp Pay order."""
    balance_line = f"Your wallet balance is {format_rupees(balance)}.\n" if balance is not None else ""
    return f"⚠️ {balance_line}{format_rupees(price)} is required for {product}.\nRecharge your wallet below:"


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


def _ensure_wallet_row(db: Session, sender: str) -> Optional[Customer]:
    """Find the sender's wallet row, creating an UNREGISTERED placeholder if none.

    Same placeholder shape the Razorpay webhook uses for unregistered payers;
    registration later adopts the row by phone. Only called when native pay
    is active, so an in-chat order can be credited to this row.
    """
    cust = find_customer_by_phone(db, sender)
    if cust is not None:
        return cust
    try:
        cust = Customer(
            whatsapp_id=sender,
            full_name="Valued Customer",
            business_name="Jewelry Business",
            gst_number="N/A",
            address="N/A",
            wallet_balance=0,
            is_registered=False,
        )
        db.add(cust)
        db.commit()
        logger.info(f"Created unregistered wallet row for native pay: sender={mask_phone(sender)}")
        return cust
    except IntegrityError:
        db.rollback()  # concurrent create won; read it back
        return find_customer_by_phone(db, sender)
    except Exception as e:
        db.rollback()
        logger.error(f"Wallet row creation failed for {mask_phone(sender)}: {e}")
        return None


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

PRODUCT_LABELS = {PRODUCT_WHITE_BG: "Clean Studio Shot", PRODUCT_PACK_1: "Full Catalog Pack"}

WELCOME_MESSAGE = (
    "Welcome to Moraa Studio ✨\n\n"
    "We transform your raw jewelry photos into studio-grade product visuals in seconds.\n\n"
    "Let’s quickly set up your account!"
)

REGISTRATION_REQUEST_MESSAGE = (
    "Quick Setup 📋\n\n"
    "Please reply with your details:\n\n"
    "• Name:\n"
    "• Brand Name:\n"
    "• City:\n"
    "• GSTIN (Optional):"
)

REGISTRATION_CONFIRMATION_TEMPLATE = (
    "You're all set, {name}! 🎉\n\n"
    "Your account is ready.\n"
    "Wallet Balance: ₹{balance}\n\n"
    "Recharge your wallet below to get started:"
)

# GSTIN rules used by BOTH the text and the Flow registration paths: a valid
# 15-char GSTIN is stored uppercase without spaces; anything else (empty,
# "NA"/"none"/..., malformed) is stored as the "N/A" convention.
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")


def _normalize_gst(value: Any) -> str:
    if not isinstance(value, str):
        return "N/A"
    gst_norm = value.replace(" ", "").upper()
    return gst_norm if _GSTIN_RE.match(gst_norm) else "N/A"


_REG_NAME_KEYS = {"name", "full name", "your name"}
_REG_BUSINESS_KEYS = {
    "business", "business name", "brand", "brand name",
    "shop name", "company", "company name",
}
_REG_GST_KEYS = {"gst", "gstin", "gst number", "gstin number", "gst no", "gst no."}
_REG_ADDRESS_KEYS = {"address", "business address", "shop address"}


def _parse_registration_text(raw_text: str) -> Dict[str, str]:
    """Parse a free-text "Key: value" registration message.

    Keys are matched exactly (case-insensitive, parenthesised parts such as
    "(Optional)" removed). "City" only fills the address when no address key
    was given.
    """
    result = {
        "name": "there",
        "business": "Jewelry Business",
        "gst": "N/A",
        "raw_gst": "",
        "address": "N/A",
    }
    address_val = ""
    city_val = ""
    for line in (raw_text or "").splitlines():
        # Customers often paste the "• Name:" bullets back.
        line_clean = line.strip().lstrip("•*-–· ").strip()
        if ":" not in line_clean:
            continue
        key, value = line_clean.split(":", 1)
        value = value.strip()
        if not value:
            continue
        key = re.sub(r"\([^)]*\)", " ", key.lower())
        key = " ".join(key.split())
        if key in _REG_NAME_KEYS:
            result["name"] = value.split()[0]
        elif key in _REG_BUSINESS_KEYS:
            result["business"] = value
        elif key in _REG_GST_KEYS:
            result["raw_gst"] = value
            # t9: only a real 15-char GSTIN is stored; "NA"/"none"/malformed
            # text becomes N/A.
            result["gst"] = _normalize_gst(value)
        elif key in _REG_ADDRESS_KEYS:
            address_val = value
        elif key == "city":
            city_val = value
    if address_val:
        result["address"] = address_val
    elif city_val:
        result["address"] = city_val
    return result


def _upsert_registered_customer(
    db: Session,
    sender: str,
    full_name: str,
    business_name: str,
    gst_number: str,
    address: str,
) -> Optional[Customer]:
    """Write the registration profile onto the sender's customer row.

    Registration only ever writes profile fields. The wallet balance is never
    credited or reset here: an existing customer (including an unregistered
    placeholder row created by a payment) keeps their exact balance, a new
    one starts at ₹0. Money is added only by the signed Razorpay webhook.
    """
    clean_sender = sender.lstrip("+").strip()
    cust = _find_customer_safe(db, sender)
    if cust is not None:
        cust.full_name = full_name
        cust.business_name = business_name
        cust.gst_number = gst_number
        cust.address = address
        cust.is_registered = True
        db.commit()
        return cust
    try:
        return BaseRepository(Customer, db).create(
            whatsapp_id=clean_sender,
            full_name=full_name,
            business_name=business_name,
            gst_number=gst_number,
            address=address,
            wallet_balance=0,
            is_registered=True,
        )
    except Exception as create_error:
        db.rollback()
        logger.error(f"Onboarding customer creation failed: {create_error}")
        return _find_customer_safe(db, sender)


async def _send_registration_confirmation(
    db: Session, sender: str, cust: Customer, display_name: str
) -> None:
    """"You're all set" + Recharge Wallet CTA (fresh ₹500 Razorpay link).

    Team members (ADMIN) and trial customers with credits get the same
    confirmation as plain text, without any recharge prompt.
    """
    if ent.payment_exempt(cust):
        if ent.is_admin(cust):
            extra = "Team access is active - no recharge needed."
        else:
            remaining = ent.trial_remaining(cust)
            extra = (f"You have {remaining} complimentary trial credit"
                     f"{'' if remaining == 1 else 's'} - no recharge needed.")
        await send_whatsapp_text(
            sender,
            f"You're all set, {display_name}! 🎉\n\nYour account is ready.\n{extra}\n\n"
            "Send your jewelry photo whenever you're ready to start!",
        )
        return
    confirm_msg = REGISTRATION_CONFIRMATION_TEMPLATE.format(
        name=display_name,
        balance=f"{get_balance(db, cust.whatsapp_id):,}",
    )
    if await try_send_native_recharge(db, sender, 500, confirm_msg, site="registration"):
        return
    if await send_payment_unavailable(sender, "registration", body_text=confirm_msg):
        return
    try:
        pay_url = await create_recharge_payment_link(
            customer_phone=sender,
            customer_name=display_name,
            amount=500,
        )
    except Exception as e:
        logger.error("Failed to generate registration recharge link: {}", e)
        pay_url = DEFAULT_PAYMENT_URL

    await send_whatsapp_cta_url_button(
        recipient_id=sender,
        body_text=confirm_msg,
        button_label="Recharge Wallet",
        url=pay_url or DEFAULT_PAYMENT_URL,
    )


def _confirmation_for(db: Session, sender: str):
    """Deferred "You're all set" for a GSTIN resolved after registration
    (Skip button, or a valid re-entered GSTIN)."""
    async def _send() -> None:
        cust = _find_customer_safe(db, sender)
        if cust is not None:
            name = (cust.full_name or "there").split()[0] if (cust.full_name or "").strip() else "there"
            await _send_registration_confirmation(db, sender, cust, name)
    return _send


# ─── WhatsApp Flow registration ──────────────────────────────────────────
# A Flow submission is claimed once per WhatsApp message id with an audit row
# (resource_id = uuid5 of the message id, which fits the 36-char column). A
# Meta retry of the same submission finds that row and does nothing: no
# profile rewrite, no new payment link, no second confirmation.

REGISTRATION_FLOW_AUDIT_ACTION = "whatsapp_registration_flow"
_FLOW_DEDUPE_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "moraa-gemvision/whatsapp-registration-flow")


def _flow_dedupe_key(message_id: str) -> str:
    return str(uuid.uuid5(_FLOW_DEDUPE_NAMESPACE, message_id))


def _flow_already_processed(db: Session, dedupe_key: str) -> bool:
    return (
        db.query(AuditLog.id)
        .filter(
            AuditLog.action == REGISTRATION_FLOW_AUDIT_ACTION,
            AuditLog.resource_id == dedupe_key,
        )
        .first()
        is not None
    )


def _flow_text(value: Any, max_len: Optional[int] = None) -> str:
    """Trim + collapse whitespace; the full value is kept (column cap only)."""
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split())
    return cleaned[:max_len] if max_len else cleaned


def _flow_field(data: Dict[str, Any], *aliases: str) -> Any:
    """Pick a Flow value by key alias. Tolerates Flow Builder keys such as
    ``screen_0_Full_Name_0`` as well as hand-written ones (``full_name``)."""
    for key, value in data.items():
        norm = re.sub(r"^screen_\d+_|_\d+$", "", str(key).strip().lower())
        if norm in aliases:
            return value
    return None


async def _handle_registration_flow(db: Session, event: Dict[str, Any]) -> bool:
    """Create/update the sender's customer from a registration Flow submission.

    Returns True only when this call registered the customer and sent the
    confirmation. Never raises.
    """
    sender = (event.get("sender") or "").strip()
    message_id = (event.get("message_id") or "").strip()
    data = event.get("flow_response") or {}
    if not sender:
        return False

    full_name = _flow_text(_flow_field(data, "full_name", "name", "your_name"), 255)
    # Only the name is required; business_name is NOT NULL, so fall back to it.
    business_name = _flow_text(
        _flow_field(data, "business_name", "brand_name", "business", "brand"), 255
    ) or full_name
    address = _flow_text(_flow_field(data, "address", "city")) or "N/A"
    # Raw GSTIN exactly as the Flow sent it, under any Flow Builder key
    # (screen_0_GSTIN_3, gstin, gst_number...). It is what the GST step
    # validates; the stored value stays "N/A" until that step resolves it.
    raw_gst = _flow_field(data, "gst_number", "gstin", "gst", "gst_no", "gstin_number")
    if raw_gst is None:
        # Flow Builder labels like "GSTIN (Optional)" become keys such as
        # screen_0_GSTIN_Optional_3: accept any remaining key naming GST.
        raw_gst = next((v for k, v in data.items() if "gst" in str(k).lower()), None)
    gst_number = _normalize_gst(raw_gst)
    # A non-empty GSTIN must pass the GST step before registration completes:
    # an invalid one leaves the profile saved as a draft (is_registered=False)
    # and sends the Re-enter / Skip buttons.
    registered_now = not gst_check_pending(raw_gst)

    dedupe_key = _flow_dedupe_key(message_id) if message_id else None
    if dedupe_key and _flow_already_processed(db, dedupe_key):
        logger.info(f"Duplicate registration Flow ignored: message_id={message_id[:40]}")
        return False

    if not full_name:
        logger.warning(
            f"Registration Flow missing name: sender={mask_phone(sender)} keys={sorted(data)}"
        )
        await send_whatsapp_text(sender, REGISTRATION_REQUEST_MESSAGE)
        return False

    cust: Optional[Customer] = None
    for _attempt in range(2):
        try:
            existing = _find_customer_safe(db, sender)
            if existing is not None:
                # Row lock (PostgreSQL) serialises concurrent copies of the
                # same submission; the loser then sees the winner's claim.
                cust = (
                    db.query(Customer)
                    .filter(Customer.id == existing.id)
                    .with_for_update()
                    .one()
                )
                if dedupe_key and _flow_already_processed(db, dedupe_key):
                    db.rollback()
                    logger.info(f"Duplicate registration Flow ignored: message_id={message_id[:40]}")
                    return False
                cust.full_name = full_name
                cust.business_name = business_name
                cust.gst_number = gst_number
                cust.address = address
                cust.is_registered = registered_now
            else:
                cust = Customer(
                    whatsapp_id=sender.lstrip("+").strip(),
                    full_name=full_name,
                    business_name=business_name,
                    gst_number=gst_number,
                    address=address,
                    wallet_balance=0,
                    is_registered=registered_now,
                )
                db.add(cust)
            if dedupe_key:
                db.add(
                    AuditLog(
                        action=REGISTRATION_FLOW_AUDIT_ACTION,
                        resource_id=dedupe_key,
                        resource_type="whatsapp_message",
                        status="success",
                        details=json.dumps({
                            "whatsapp_id": sender,
                            "message_id": message_id,
                            "flow_token": data.get("flow_token"),
                        }),
                    )
                )
            db.commit()
            break
        except IntegrityError:
            # A concurrent request created this customer first (unique
            # whatsapp_id): retry once against the now-existing row.
            db.rollback()
            cust = None
        except Exception as e:
            db.rollback()
            logger.error(f"Registration Flow save failed for {mask_phone(sender)}: {e}")
            return False
    if cust is None:
        logger.error(f"Registration Flow could not be saved for {mask_phone(sender)}")
        return False

    # GST first: "You're all set" is sent only once the GSTIN is resolved
    # (verified, accepted, skipped, or none given / check disabled).
    await verify_after_registration(
        db, sender, raw_gst,
        on_complete=lambda: _send_registration_confirmation(db, sender, cust, full_name),
    )
    return True


ALREADY_CHOSEN_MESSAGE = (
    "This photo already has an order, so nothing new was charged. "
    "Send the photo again if you want to place another order."
)
UNKNOWN_CHOICE_MESSAGE = "Sorry, we couldn't find that photo. Please send it again."
# Largest wallet recharge a customer can ask for in one message (MON-12).
MAX_RECHARGE_RUPEES = 50_000
CAPACITY_MESSAGE = (
    "We can't generate new images right now, so nothing was charged. "
    "Your photo is saved, so you can tap your choice again once generation is available 🙏"
)
INFLIGHT_MESSAGE = (
    "You already have {n} orders being prepared, so nothing was charged. "
    "Please wait for one to arrive, then tap your choice on this photo again 🙏"
)
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
    return same_phone(stored, sender)


async def _ingest_image_for_choice(db: Session, event: Dict[str, Any]) -> Optional[str]:
    """Store one incoming photo and send the product-choice buttons.

    No money moves here. Returns the ingestion id when the buttons were sent.
    """
    sender = (event.get("sender") or "").strip()
    message_id = event.get("message_id", "")
    media_id = event.get("media_id", "")
    caption = event.get("caption", "")
    timestamp = event.get("timestamp", "")

    # Nothing about this customer or photo is stored until they have agreed to the data notice (PRIV-2).
    if await _consent_gate(db, sender, message_id):
        return None

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
    # Team members and trial customers with credits are never held for funds.
    if customer is None or (balance < min_price and not ent.payment_exempt(customer)):
        ingestion.status = "unfunded"
        ingestion.error_message = f"Balance {balance} below minimum {min_price} at upload"
        db.commit()
        if customer is None and is_native_pay_active(sender):
            customer = _ensure_wallet_row(db, sender)
        hold_balance = get_balance(db, customer.whatsapp_id) if customer else 0
        if customer is not None and await try_send_native_recharge(
            db, sender, max(min_price, 500), _hold_body(min_price, hold_balance, "an order"),
            reply_to_message_id=message_id, site="photo_low_balance",
        ):
            return None
        if await send_payment_unavailable(
            sender, "photo_low_balance",
            body_text=_hold_body(min_price, hold_balance, "an order"), reply_to_message_id=message_id,
        ):
            return None
        await send_whatsapp_text(
            recipient_id=sender,
            message_text=_hold_message(min_price, hold_balance, "an order"),
            reply_to_message_id=message_id,
        )
        return None

    # Fetch, validate and AI pre-check once per photo -- before any charge. Take what the rest of this
    # function needs out of the ORM objects and end the transaction first: the download and AI pre-check
    # below take seconds, and a connection held that long is what exhausts the pool under photo bursts (PERF-1).
    new_ingestion_id = ingestion.id
    customer_wallet_id = customer.whatsapp_id
    db.commit()

    media_url = await get_media_url(media_id)
    download_result: Optional[Tuple[bytes, str]] = await download_media(media_url) if media_url else None
    if not download_result:
        _reject("Media download failed")
        await send_whatsapp_text(sender, UNREADABLE_IMAGE_MESSAGE, reply_to_message_id=message_id)
        return None
    image_bytes, content_type = download_result
    is_valid, validation_error = await run_cpu(validate_image, image_bytes, content_type)   # PIL decode: off the loop
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
    current_balance = get_balance(db, customer_wallet_id)
    db.commit()           # the Meta send below must not hold a connection
    sent = await send_product_selection_buttons(
        recipient_id=sender,
        ingestion_id=new_ingestion_id,
        white_price=white_price,
        pack_price=pack_price,
        balance=current_balance,
        reply_to_message_id=message_id,
    )
    if not sent:
        logger.error(f"Product selection buttons NOT sent: ingestion_id={new_ingestion_id}")
    return new_ingestion_id


# ingestion id -> outbox job id, for orders recorded in the outbox but not yet handed to a background task.
_recorded_runs: Dict[str, int] = {}


async def _consent_gate(db: Session, sender: str, reply_to_message_id: Optional[str] = None) -> bool:
    """True when this number has not yet agreed to the data notice: the notice was (re)sent and the caller must stop
    here, collecting nothing (PRIV-2). Always False while the consent step is switched off."""
    if not consent_service.is_active():
        return False
    if consent_service.has_consented(db, sender):
        return False
    if not await consent_service.ask_for_consent(sender, reply_to_message_id):
        logger.warning(f"Consent notice could not be sent to {mask_phone(sender)}")
    return True


async def _handle_erasure_command(db: Session, sender: str, raw_text: str) -> None:
    """"DELETE MY DATA" asks for confirmation; "CONFIRM DELETE" within 15 minutes erases (PRIV-3)."""
    customer = find_customer_by_phone(db, sender)
    if customer is None:
        await send_whatsapp_text(sender, data_lifecycle.ERASURE_NOTHING_MESSAGE)
        return
    if data_lifecycle.is_erasure_request(raw_text):
        data_lifecycle.request_erasure(db, customer)
        await send_whatsapp_text(sender, data_lifecycle.ERASURE_ASK_MESSAGE)
        return
    if not data_lifecycle.erasure_requested_recently(db, customer):
        await send_whatsapp_text(sender, data_lifecycle.ERASURE_EXPIRED_MESSAGE)
        return
    customer_id = customer.id
    db.commit()                          # end this transaction: the erasure below uses its own connection
    try:
        await run_io(data_lifecycle.erase_customer_by_id, customer_id)
    except data_lifecycle.ErasureRefused as refused:
        await send_whatsapp_text(sender, str(refused))
        return
    await send_whatsapp_text(sender, data_lifecycle.ERASURE_DONE_MESSAGE)


def _queue_order_run(background_tasks: BackgroundTasks, job: Tuple[Any, str]) -> None:
    """Start a paid order. With the outbox on, the order is first recorded in the database (so a crash or deploy
    before it starts does not strand it: the sweep starts it) and then run right here as before."""
    worker, ingestion_id = job
    if settings.OUTBOX_ENABLED:
        from app.services import outbox

        job_id = _recorded_runs.pop(ingestion_id, None)       # recorded when the order was queued (see above)
        if job_id is None:
            kind = "white" if worker is process_whatsapp_white_bg else "pack"
            job_id = outbox.enqueue_order_run(kind, ingestion_id)
        if job_id is not None:
            background_tasks.add_task(outbox.run_job_now, job_id)
            return
    background_tasks.add_task(worker, ingestion_id)


def _orders_ahead(ingestion_id: str) -> int:
    """Paid orders currently in progress, from every customer, other than this one (sync; own session)."""
    from app.database import SessionLocal
    from app.services.meta_whatsapp_service import STUCK_PAID_STATUSES

    try:
        with SessionLocal() as session:
            return int(session.query(func.count(WhatsAppIngestion.id)).filter(
                WhatsAppIngestion.status.in_(STUCK_PAID_STATUSES), WhatsAppIngestion.id != ingestion_id
            ).scalar() or 0)
    except Exception:  # noqa: BLE001 -- an estimate must never block an order
        return 0


async def _orders_ahead_released(db: Session, ingestion_id: str) -> int:
    """``_orders_ahead`` after ending this request's transaction: the count uses its own connection, and holding two per
    order exhausts the pool under bursts (PERF-1)."""
    try:
        db.commit()
    except Exception:  # noqa: BLE001 -- an estimate must never fail an order that is already paid and queued
        db.rollback()
    return await run_io(_orders_ahead, ingestion_id)


def _parallel_orders(calls_per_order: int) -> Optional[int]:
    """How many orders the server works on at once (None = no limit configured, so nobody waits in line)."""
    limit = int(getattr(settings, "MAX_CONCURRENT_PROVIDER_CALLS", 0) or 0)
    return max(limit // max(calls_per_order, 1), 1) if limit > 0 else None


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
        logger.warning(f"Product choice for unknown/foreign ingestion: id={ingestion_id} sender={mask_phone(sender)}")
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

    # Capacity BEFORE any money moves (UX-4): if today's generation limit (or the kill switch) leaves no room
    # for this order, decline it now with nothing charged and keep the photo choosable. Team (ADMIN) orders
    # are not counted against the daily cap, and dry-run makes no provider calls.
    if not _mws.DRY_RUN_IMAGE_MODE and not (customer is not None and ent.is_admin(customer)):
        needed = 1 if product == PRODUCT_WHITE_BG else _mws.pack_generation_count()
        db.commit()           # end this transaction first: the counter lookup below needs its own connection
        no_capacity = await run_io(generation_capacity_blocked, needed)
        if no_capacity:
            logger.warning(f"Order {ingestion_id} declined before charging: {no_capacity}")
            db.query(WhatsAppIngestion).filter(
                WhatsAppIngestion.id == ingestion_id,
                WhatsAppIngestion.status == "choice_claimed",
            ).update(
                {WhatsAppIngestion.status: "awaiting_choice", WhatsAppIngestion.product_code: None},
                synchronize_session=False,
            )
            db.commit()
            await send_whatsapp_text(sender, CAPACITY_MESSAGE, reply_to_message_id=quote_id)
            return None

    # At most N paid orders per customer in progress at once (Q-6), decided BEFORE any money moves. Team (ADMIN)
    # orders are exempt.
    cap = int(getattr(settings, "MAX_INFLIGHT_ORDERS_PER_CUSTOMER", 0) or 0)
    if cap > 0 and not (customer is not None and ent.is_admin(customer)):
        from app.services.meta_whatsapp_service import STUCK_PAID_STATUSES

        in_flight = db.query(func.count(WhatsAppIngestion.id)).filter(
            WhatsAppIngestion.external_user_id == ingestion.external_user_id,
            WhatsAppIngestion.status.in_(STUCK_PAID_STATUSES),
            WhatsAppIngestion.id != ingestion_id,
        ).scalar() or 0
        if in_flight >= cap:
            logger.info(f"Order {ingestion_id} declined before charging: {in_flight} orders already in progress")
            db.query(WhatsAppIngestion).filter(
                WhatsAppIngestion.id == ingestion_id,
                WhatsAppIngestion.status == "choice_claimed",
            ).update(
                {WhatsAppIngestion.status: "awaiting_choice", WhatsAppIngestion.product_code: None},
                synchronize_session=False,
            )
            db.commit()
            await send_whatsapp_text(sender, INFLIGHT_MESSAGE.format(n=in_flight), reply_to_message_id=quote_id)
            return None

    # Tiered access (entitlement_service): ADMIN is free; a TRIAL customer
    # with credits for this product is free (a credit is used only when the
    # order is delivered). Everything else is the normal wallet path.
    free_access = None
    if customer is not None and ent.is_admin(customer):
        free_access = "admin"
    elif customer is not None and ent.has_trial_credits(customer):
        if not ent.trial_shot_allowed(customer, product):
            if get_balance(db, customer.whatsapp_id) < price:
                db.query(WhatsAppIngestion).filter(
                    WhatsAppIngestion.id == ingestion_id,
                    WhatsAppIngestion.status == "choice_claimed",
                ).update(
                    {WhatsAppIngestion.status: "awaiting_choice", WhatsAppIngestion.product_code: None},
                    synchronize_session=False,
                )
                db.commit()
                permitted = " / ".join(ent.allowed_product_labels(customer)) or "no products"
                await send_whatsapp_text(
                    sender,
                    f"Your complimentary trial covers: {permitted}. Tap that option on your photo "
                    f"to use a trial credit, or recharge your wallet to order the {label}.",
                    reply_to_message_id=quote_id,
                )
                return None
            # Wallet can pay for the product outside the trial: normal charge below.
        elif ent.trial_credits_available(db, customer, product, exclude_ingestion_id=ingestion_id):
            free_access = "trial"

    if free_access:
        charged = True
    elif customer is None:
        charged = False
    elif dry_run:
        # Same balance rule, but no money moves in dry-run.
        charged = get_balance(db, customer.whatsapp_id) >= price
    else:
        # commit=False: the debit and its ledger row are committed TOGETHER with the order status
        # below, so a crash can never leave money taken with the order unrecorded.
        charged, _balance_after = charge_customer_balance(
            db, customer, price, ingestion_id=ingestion_id, commit=False,
        )

    if not charged:
        db.rollback()                    # discard any half-applied debit before releasing the claim
        db.query(WhatsAppIngestion).filter(
            WhatsAppIngestion.id == ingestion_id,
            WhatsAppIngestion.status == "choice_claimed",
        ).update(
            {WhatsAppIngestion.status: "awaiting_choice", WhatsAppIngestion.product_code: None},
            synchronize_session=False,
        )
        db.commit()
        balance = get_balance(db, customer.whatsapp_id) if customer else 0
        if await try_send_native_recharge(
            db, sender, max(price, 500), _hold_body(price, balance, label), reply_to_message_id=quote_id,
            site="product_choice",
        ):
            return None
        if await send_payment_unavailable(
            sender, "product_choice", body_text=_hold_body(price, balance, label), reply_to_message_id=quote_id,
        ):
            return None
        await send_whatsapp_text(
            recipient_id=sender,
            message_text=_hold_message(price, balance, label),
            reply_to_message_id=quote_id,
        )
        return None

    try:
        db.refresh(ingestion)
        ingestion.amount_charged = 0 if (dry_run or free_access) else price
        ingestion.status = "white_queued" if product == PRODUCT_WHITE_BG else "pack_queued"
        db.commit()                      # debit + ledger row + status in ONE transaction
    except Exception:
        db.rollback()
        debited = (
            not (dry_run or free_access)
            and db.query(WalletTransaction.id)
            .filter(WalletTransaction.ingestion_id == ingestion_id, WalletTransaction.kind == KIND_DEBIT_ORDER)
            .first()
            is not None
        )
        if debited:
            # The commit reached the server before the error: the money IS taken. Finish the order.
            db.query(WhatsAppIngestion).filter(
                WhatsAppIngestion.id == ingestion_id,
                WhatsAppIngestion.status == "choice_claimed",
            ).update(
                {
                    WhatsAppIngestion.amount_charged: price,
                    WhatsAppIngestion.status: "white_queued" if product == PRODUCT_WHITE_BG else "pack_queued",
                },
                synchronize_session=False,
            )
            db.commit()
            logger.error(f"Order {ingestion_id}: commit error after the debit was stored; order completed from the ledger")
        else:
            # Nothing was taken; release the claim so the customer can tap again.
            db.query(WhatsAppIngestion).filter(
                WhatsAppIngestion.id == ingestion_id,
                WhatsAppIngestion.status == "choice_claimed",
            ).update(
                {WhatsAppIngestion.status: "awaiting_choice", WhatsAppIngestion.product_code: None},
                synchronize_session=False,
            )
            db.commit()
            raise

    # Ties the webhook request (the id on every line of this request AND of the background job it starts) to the
    # order, so a paid order can be followed from the customer's tap to its delivery (OBS-6).
    logger.info(f"Order queued: ingestion_id={ingestion_id} product={product} free_access={free_access or 'no'}")

    # Record the paid order in the durable outbox NOW, before anything else can fail (the acknowledgement message, the
    # queue-depth lookup): from here on a crash or deploy cannot strand it, the outbox sweep starts it (Q-1).
    if settings.OUTBOX_ENABLED:
        from app.services import outbox

        db.commit()                      # end this transaction: the outbox write below uses its own connection
        run_id = await run_io(outbox.enqueue_order_run, "white" if product == PRODUCT_WHITE_BG else "pack", ingestion_id)
        if run_id is not None:
            if len(_recorded_runs) > 1000:
                _recorded_runs.clear()
            _recorded_runs[ingestion_id] = run_id

    if product == PRODUCT_WHITE_BG:
        if free_access == "trial":
            cost_note = ", complimentary trial credit"
        elif free_access == "admin":
            cost_note = ", team access - no charge"
        elif dry_run:
            cost_note = ", test mode - no charge"
        else:
            cost_note = f", ₹{price}"
        ahead = await _orders_ahead_released(db, ingestion_id)
        suffix = eta_service.ack_suffix("white_bg", ahead, _parallel_orders(1), "Please allow 20-30 seconds.")
        await send_whatsapp_text(
            sender,
            "✨ Processing your Clean Studio Shot (1 image"
            + cost_note
            + ")... "
            + suffix,
            reply_to_message_id=quote_id,
        )
        return process_whatsapp_white_bg, ingestion_id
    # Pack 1 acknowledgement + worker. The time estimate is measured from recent orders (UX-2); until some have
    # been measured the original wording is sent unchanged.
    ahead = await _orders_ahead_released(db, ingestion_id)
    default_tail = "Please allow 20-30 seconds."
    suffix = eta_service.ack_suffix("pack", ahead, _parallel_orders(_mws.pack_generation_count()), default_tail)
    ack = CATALOG_PACK_ACK_TEMPLATE if suffix == default_tail else CATALOG_PACK_ACK_TEMPLATE.replace(default_tail, suffix)
    await send_whatsapp_text(sender, ack, reply_to_message_id=quote_id)
    return process_whatsapp_catalog_pack, ingestion_id


@router.get("/webhook", summary="Meta webhook verification")
async def verify_webhook(
    request: Request,
    hub_mode: Optional[str] = None,
    hub_verify_token: Optional[str] = None,
    hub_challenge: Optional[str] = None,
) -> PlainTextResponse:
    # Meta sends dotted names (hub.mode, hub.verify_token, hub.challenge);
    # FastAPI only binds the underscore names, so read the dotted ones too.
    query = request.query_params
    hub_mode = query.get("hub.mode") or hub_mode
    hub_verify_token = query.get("hub.verify_token") or hub_verify_token
    hub_challenge = query.get("hub.challenge") or hub_challenge
    if (
        hub_mode != "subscribe"
        or not hub_verify_token
        or not hmac.compare_digest(
            hub_verify_token.encode("utf-8"), (settings.META_VERIFY_TOKEN or "").encode("utf-8")
        )
    ):
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


def _message_claims(db: Session = Depends(get_db)) -> Iterator[List[str]]:
    """The message ids this request claimed. If handling fails part-way, the claims are given back so
    Meta's retry of the message is handled instead of skipped."""
    claimed: List[str] = []
    try:
        yield claimed
    except BaseException:
        release_messages(db, claimed)
        raise


@router.post("/webhook", summary="Receive WhatsApp webhook events")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    claimed: List[str] = Depends(_message_claims),
) -> Dict[str, Any]:
    if not isinstance(claimed, list):
        # Called directly (not through FastAPI, e.g. the load harness): nothing tracks the claims, so a
        # failure part-way cannot give them back. Duplicate protection itself still works.
        claimed = []
    try:
        raw_body = await request.body()
    except Exception as e:
        logger.error("Failed to read webhook body: {}", e)
        return {"status": "error", "message": "Invalid JSON payload"}
    if not raw_body:
        return {"status": "ignored", "message": "Empty body"}

    # Verify the signature on the raw bytes BEFORE parsing: an unsigned sender
    # must not be able to make the server parse arbitrary JSON.
    if (settings.META_APP_SECRET or "").strip():
        signature = request.headers.get("X-Hub-Signature-256")
        if not verify_webhook_signature(raw_body, signature):
            return {"status": "error", "message": "Invalid signature"}
    elif not settings.ALLOW_UNSIGNED_WEBHOOKS:
        # Fail closed: with no META_APP_SECRET an unsigned payload is only
        # accepted under the explicit development flag (refused at boot in
        # production). DEBUG no longer relaxes this check.
        logger.error(
            "Webhook rejected: META_APP_SECRET is not configured and "
            "ALLOW_UNSIGNED_WEBHOOKS is off -- refusing an unsigned payload."
        )
        return {"status": "error", "message": "Webhook not configured"}

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        logger.error("Failed to parse webhook JSON payload: {}", e)
        return {"status": "error", "message": "Invalid JSON payload"}
    if not isinstance(payload, dict):
        logger.warning("Webhook JSON body is not an object: {}", type(payload).__name__)
        return {"status": "ignored", "message": "Unexpected payload shape"}

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
        # Studioo Ops: team ops messages -> Next.js in background; no-op for customers.
        entry = divert_ops_messages(entry, background_tasks)
        events = parse_webhook_entry(entry)

        for event in events:
            # Only the message being handled right now may be given back if handling fails: messages
            # finished earlier in this payload already had their effect and must not be repeated on retry.
            claimed.clear()
            event_type = event.get("type", "")

            if event_type == "payment_status":
                await handle_payment_status_event(db, event)
                continue

            if event_type == "status":
                continue

            # Meta re-delivers a message it did not get a fast answer for. Text, button and form replies
            # are handled once per message id (photos have their own unique-id guard further down).
            if event_type in ("text", "interactive"):
                message_id = event.get("message_id", "")
                if not claim_message(db, message_id):
                    logger.info("Duplicate delivery of a {} message ignored", event_type)
                    continue
                if message_id:
                    claimed.append(message_id)

            if event_type == "text":
                sender = event.get("sender", "")
                raw_text = event.get("body", "").strip()
                lower_text = raw_text.lower()
                # Never log the body: registration replies carry name, GSTIN and address.
                logger.info("Text message received: sender={} chars={}", mask_phone(sender), len(raw_text or ""))

                # After "Re-enter GSTIN" the next text is the GSTIN itself
                # (always False unless GST_VERIFICATION_ENABLED).
                if is_awaiting_gstin(db, sender):
                    await handle_gstin_reply(db, sender, raw_text, on_complete=_confirmation_for(db, sender))
                    continue

                if "name:" in lower_text and any(k in lower_text for k in ("business", "brand", "gst", "city", "address")):
                    if await _consent_gate(db, sender):
                        continue
                    parsed = _parse_registration_text(raw_text)
                    user_name = parsed["name"]
                    biz_name = parsed["business"]
                    gst_val = parsed["gst"]
                    raw_gst = parsed["raw_gst"]
                    addr_val = parsed["address"]

                    cust = _upsert_registered_customer(
                        db, sender, user_name, biz_name, gst_val, addr_val
                    )
                    if cust is None:
                        continue

                    # GST first; the confirmation waits for GST resolution.
                    await verify_after_registration(
                        db, sender, raw_gst,
                        on_complete=lambda: _send_registration_confirmation(db, sender, cust, user_name),
                    )
                    continue

                if settings.ERASURE_COMMAND_ENABLED and (
                    data_lifecycle.is_erasure_request(raw_text) or data_lifecycle.is_erasure_confirmation(raw_text)
                ):
                    await _handle_erasure_command(db, sender, raw_text)
                    continue

                if re.search(r"\b(hi|hii|hello|hey|start)\b", lower_text):
                    if await _consent_gate(db, sender):
                        continue
                    # Two separate messages: welcome first, then the form.
                    # New / unregistered senders get the registration Flow;
                    # when it is not configured or Meta rejects it (and for
                    # registered customers, as before) the text form is sent.
                    await send_whatsapp_text(sender, WELCOME_MESSAGE)
                    greet_cust = _find_customer_safe(db, sender)
                    if greet_cust is None or not greet_cust.is_registered:
                        if await send_registration_flow(sender):
                            continue
                        logger.warning(
                            "Registration Flow not sent (unconfigured or rejected by Meta) — "
                            "falling back to text registration: sender={}", mask_phone(sender)
                        )
                    await send_whatsapp_text(sender, REGISTRATION_REQUEST_MESSAGE)
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
                    if requested_amount > MAX_RECHARGE_RUPEES:
                        await send_whatsapp_text(
                            sender,
                            f"The maximum recharge amount is ₹{MAX_RECHARGE_RUPEES:,} ⚠️\n"
                            f"Please enter an amount of ₹{MAX_RECHARGE_RUPEES:,} or less."
                        )
                        continue

                    if await try_send_native_recharge(
                        db, sender, requested_amount,
                        f"Recharge your Moraa Studio wallet with ₹{requested_amount} 💳",
                        site="recharge_command",
                    ):
                        continue
                    if await send_payment_unavailable(sender, "recharge_command"):
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

            if event_type == "interactive" and event.get("subtype") == "nfm_reply":
                await _handle_registration_flow(db, event)
                continue

            if event_type == "interactive" and event.get("subtype") == "button_reply":
                button_reply = event.get("button_reply", {})
                b_id = button_reply.get("id", "")
                sender = event.get("sender", "")

                if await handle_gst_button(db, sender, b_id, on_complete=_confirmation_for(db, sender)):
                    continue

                if b_id in (consent_service.CONSENT_YES, consent_service.CONSENT_NO):
                    if b_id == consent_service.CONSENT_YES:
                        consent_service.record_consent(db, sender)
                        await send_whatsapp_text(sender, consent_service.AGREED_MESSAGE)
                    else:
                        await send_whatsapp_text(sender, consent_service.DECLINED_MESSAGE)
                    continue

                product_choice = parse_product_button_id(b_id)
                if product_choice:
                    job = await _handle_product_choice(db, sender, *product_choice)
                    if job:
                        # Ecommerce Shot -> process_whatsapp_white_bg,
                        # Pack 1 -> existing process_whatsapp_catalog_pack.
                        _queue_order_run(background_tasks, job)
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

    claimed.clear()     # every text / button message above is finished
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
def retry_delivery(
    ingestion_id: str,
    background_tasks: BackgroundTasks,
    current_user: Any = Depends(require_admin),
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

    # A retry re-runs a billable generation WITHOUT charging again, so it is only allowed while the
    # customer's payment is still held by this order: never after the money went back to the wallet,
    # and never for an order that no product was chosen (and so nothing charged) for.
    from app.models.wallet_transaction import KIND_REFUND_ORDER

    refunded = (
        db.query(AuditLog.id)
        .filter(AuditLog.action == _mws.REFUND_AUDIT_ACTION, AuditLog.resource_id == ingestion.id)
        .first()
        is not None
        or db.query(WalletTransaction.id)
        .filter(WalletTransaction.ingestion_id == ingestion.id, WalletTransaction.kind == KIND_REFUND_ORDER)
        .first()
        is not None
    )
    if refunded:
        return {
            "status": "error",
            "message": "This order was already refunded to the customer's wallet; ask them to send the photo again.",
        }
    if not ingestion.product_code and not ingestion.amount_charged:
        return {"status": "error", "message": "No product was chosen and nothing was charged for this photo, so there is nothing to retry."}

    # Reset status atomically so two simultaneous retries cannot both dispatch a worker.
    reset = (
        db.query(WhatsAppIngestion)
        .filter(
            WhatsAppIngestion.id == ingestion.id,
            WhatsAppIngestion.status == ingestion.status,
        )
        .update({WhatsAppIngestion.status: "stored", WhatsAppIngestion.error_message: None}, synchronize_session=False)
    )
    db.commit()
    if reset != 1:
        return {"status": "error", "message": "The order changed while retrying; check its status and try again."}
    # A refund may have been committed between the check above and the reset: if so, undo the reset.
    if (
        db.query(AuditLog.id)
        .filter(AuditLog.action == _mws.REFUND_AUDIT_ACTION, AuditLog.resource_id == ingestion_id)
        .first()
        is not None
    ):
        db.query(WhatsAppIngestion).filter(WhatsAppIngestion.id == ingestion_id).update(
            {WhatsAppIngestion.status: "failed"}, synchronize_session=False)
        db.commit()
        return {"status": "error", "message": "This order was refunded while retrying; ask the customer to resend the photo."}
    db.refresh(ingestion)

    # Dispatch by the STORED product chosen with the reply button.
    # NULL / PACK_1 -> Pack 1 catalog worker, exactly as before.
    if ingestion.product_code == PRODUCT_WHITE_BG:
        background_tasks.add_task(process_whatsapp_white_bg, ingestion.id)
    else:
        background_tasks.add_task(_trigger_generation, ingestion.id)

    logger.info(
        f"Retry triggered: ingestion_id={ingestion_id} "
        f"user={mask_phone(ingestion.external_user_id)}"
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
def get_ingestion_status(
    ingestion_id: str,
    current_user: Any = Depends(require_admin),
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
        "external_user_id": mask_phone(ingestion.external_user_id),
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
def get_generation_failure_rate(
    window_hours: int = 24,
    current_user: Any = Depends(require_admin),
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