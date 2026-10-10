"""Razorpay payment webhook routes — wallet recharge lifecycle.

Handles both standard payment links and Razorpay Payment Pages.
"""

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.services.billing_service import dispatch_payment_invoice
from app.services.invoice_service import generate_invoice_pdf
from app.services.meta_whatsapp_service import (
    send_document_to_whatsapp,
    send_whatsapp_text,
)
from app.models.whatsapp_payment_order import WhatsAppPaymentOrder
from app.models.wallet_transaction import KIND_CREDIT_PAYMENT, KIND_DEBIT_DISPUTE, KIND_DEBIT_REFUND
from app.models.sku_credit import SKU_CREATIVE
from app.services import pricing, sku_packs
from app.services.pending_payment_service import mark_pending_credited, record_pending_payment
from app.services.wallet_service import (
    CLAWBACK_OUTCOME_DUPLICATE,
    CLAWBACK_OUTCOME_NO_PAYMENT,
    claw_back_payment,
    credit_wallet,
    find_customer_by_phone,
    get_balance,
    record_ledger,
)
from app.utils.executors import run_io
from app.utils.logger import logger, mask_phone
from app.utils.phone import is_plausible_phone, normalize_phone
router = APIRouter(prefix="/api/payments", tags=["Payments"])

SIGNATURE_HEADER = "X-Razorpay-Signature"
PAISE_PER_RUPEE = pricing.PAISE_PER_RUPEE

AUDIT_ACTION_PAYMENT_CAPTURED = "razorpay_payment_captured"
# Captured payment the webhook could not match to any payer (no phone).
AUDIT_ACTION_PAYMENT_UNMATCHED = "razorpay_payment_unmatched"
# One row per transient credit failure (each answered 503 so Razorpay retries).
AUDIT_ACTION_PAYMENT_CREDIT_FAILED = "razorpay_payment_credit_failed"
# After this many failed credit attempts for one payment, stop asking Razorpay
# to retry (it would eventually disable the webhook) and leave the pending rows
# plus an ALERT for a manual credit.
MAX_CREDIT_ATTEMPTS = 8
AUDIT_RESOURCE_TYPE = "razorpay_payment"
# A payment, refund or dispute that needs a person's attention (wrong currency, unknown payment, dispute opened).
AUDIT_ACTION_PAYMENT_REVIEW = "razorpay_payment_review"

# WhatsApp Pay order states in which the WhatsApp Pay path credits (its sweep re-checks "sent" and "pending"
# orders) or already credited the money. "created" / "dispatch_failed" orders are never swept, so they are not here.
WHATSAPP_PAY_ACTIVE_STATES = frozenset({"sent", "pending", "captured"})

# A refund that finished, or a dispute that was lost, takes the money back out of the wallet.
MONEY_BACK_EVENTS = ("refund.processed", "payment.dispute.lost")
EVENT_DISPUTE_CREATED = "payment.dispute.created"
# A refund whose payment we have not credited yet is retried by Razorpay for this long, then left for review.
NO_PAYMENT_RETRY_WINDOW_SECONDS = 6 * 3600

PAYMENT_TIPS_MESSAGE = (
    "Payment Received 💳\n\n"
    "₹{paid} has been added to your wallet.\n"
    "Current Balance: ₹{balance}\n\n"
    "Send your earring photo whenever you're ready! 📸\n"
    "(Tip: Good lighting and sharp focus produce the best studio results)"
)

# Receipt for a SKU pack payment (notes.purpose "sku_pack"): credits, not rupees, were added.
PACK_RECEIPT_MESSAGE = (
    "Payment Received 💳\n\n"
    "{pack} added to your account.\n"
    "SKUs available: {credits}\n"
    "Valid till: {valid_till}\n\n"
    "Send your earring photos whenever you're ready! 📸\n"
    "(Each photo uses one SKU and makes one white-background studio image)"
)
# Added to the pack receipt when the customer has no email yet: their Drive image folder is shared to it.
PACK_EMAIL_REQUEST_LINE = "\n\nPlease send us your email address so we can share your image folder with you. 📁"


def verify_razorpay_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    secret = (settings.RAZORPAY_WEBHOOK_SECRET or "").strip()
    if not secret:
        # Fail closed: nothing can be verified without the secret. The route
        # decides separately whether the dev-only unsigned mode applies.
        return False
    if not signature_header:
        return False

    computed = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed.encode("ascii"), signature_header.strip().encode("utf-8"))


def _nested_get(payload: Dict[str, Any], *path: str) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _paise_to_rupees(amount_paise: int) -> int:
    return (amount_paise + PAISE_PER_RUPEE // 2) // PAISE_PER_RUPEE


def _as_id(value: Any) -> str:
    """Coerce a JSON scalar to a clean id string; reject non-scalars."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def _extract_sender_id(payment_entity: Dict[str, Any], payment_link_entity: Dict[str, Any]) -> str:
    """
    Safely resolve the payer's phone (or id) without ANY KeyError.

    Priority: notes.sender_id -> notes.phone/whatsapp_id/mobile ->
    contact (Payment Pages) -> customer_id (last resort, e.g. "cust_XXXX").
    Every access is a guarded ``.get()`` so an unexpected payload shape can
    never raise.
    """
    entities = [e for e in (payment_entity, payment_link_entity) if isinstance(e, dict)]

    # 1. Notes set at link-creation time are the most reliable.
    for ent in entities:
        notes = ent.get("notes")
        if not isinstance(notes, dict):
            continue
        for key in ("sender_id", "phone", "whatsapp_id", "mobile"):
            candidate = str(notes.get(key) or "").strip()
            if candidate:
                return candidate

    # 2. Direct contact captured by Payment Pages at checkout.
    for ent in entities:
        candidate = str(ent.get("contact") or "").strip()
        if candidate:
            return candidate

    # 3. Last resort: customer_id still enables wallet credit + dedupe.
    for ent in entities:
        candidate = str(ent.get("customer_id") or "").strip()
        if candidate:
            return candidate

    return ""


def extract_payment_data(payload: Dict[str, Any]) -> Optional[Tuple[str, int, str]]:
    """
    Safely extract (sender_id, amount_in_rupees, payment_id) without ANY KeyError,
    supporting 'payment.captured' (Payment Pages), 'payment_link.paid' (Dynamic
    Links) and 'order.paid' events.

    Returns ``None`` when the payload carries no usable payment reference;
    ``sender_id`` may be "" when the payer's phone is absent (the route then
    reports ``missing_phone``). Never raises — malformed payloads are logged
    and rejected.
    """
    try:
        if not isinstance(payload, dict):
            logger.warning("Razorpay webhook payload is not a JSON object")
            return None

        payment_entity = _nested_get(payload, "payload", "payment", "entity")
        payment_link_entity = _nested_get(payload, "payload", "payment_link", "entity")

        if not isinstance(payment_entity, dict):
            payment_entity = {}
        if not isinstance(payment_link_entity, dict):
            payment_link_entity = {}

        if not payment_entity and not payment_link_entity:
            return None

        # Safe extraction of Payment ID (required, must be a scalar).
        payment_id = _as_id(
            payment_entity.get("id") or payment_link_entity.get("id")
        )
        if not payment_id:
            logger.warning("Razorpay webhook payload missing payment entity id")
            return None

        # Safe extraction of Amount (required, in paise).
        amount_paise_raw = payment_entity.get("amount")
        if amount_paise_raw is None:
            amount_paise_raw = payment_link_entity.get("amount")

        try:
            amount_paise = int(amount_paise_raw)
        except (TypeError, ValueError):
            logger.warning("Razorpay webhook payload has a non-numeric amount")
            return None

        if amount_paise <= 0:
            return None

        # Safe extraction of Customer Phone / Sender ID (optional).
        sender_id = _extract_sender_id(payment_entity, payment_link_entity)

        return sender_id, _paise_to_rupees(amount_paise), payment_id

    except Exception as e:
        # Absolute safety net: payload parsing must never bubble a KeyError
        # (or anything else) into the middleware as a 500.
        logger.error(f"Razorpay webhook payload extraction failed unexpectedly: {e}")
        return None


def extract_currency(payload: Dict[str, Any]) -> str:
    """Upper-cased currency code of the payment in a payment event ("" when absent)."""
    for path in (("payload", "payment", "entity"), ("payload", "payment_link", "entity")):
        entity = _nested_get(payload, *path)
        if isinstance(entity, dict):
            code = entity.get("currency")
            if isinstance(code, str) and code.strip():
                return code.strip().upper()
    return ""


def _record_payment_review(db: Session, resource_id: str, reason: str, details: Dict[str, Any]) -> None:
    """Leave a durable "a person must look at this" row (once per ``resource_id`` and reason). Never raises."""
    try:
        if _review_reason_exists(db, resource_id, reason):
            return
        db.add(
            AuditLog(
                user_id=None,
                action=AUDIT_ACTION_PAYMENT_REVIEW,
                resource_id=resource_id,
                resource_type=AUDIT_RESOURCE_TYPE,
                status="pending",
                details=json.dumps({"reason": reason, **details}),
            )
        )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Could not record payment review row for {resource_id}: {e}")


def _review_reason_exists(db: Session, resource_id: str, reason: str) -> bool:
    rows = (
        db.query(AuditLog.details)
        .filter(AuditLog.action == AUDIT_ACTION_PAYMENT_REVIEW, AuditLog.resource_id == resource_id)
        .all()
    )
    return any(f'"reason": "{reason}"' in (row[0] or "") for row in rows)


def _is_recent(created_at: Any) -> bool:
    """True when a Razorpay epoch timestamp is under NO_PAYMENT_RETRY_WINDOW_SECONDS old (or missing/invalid)."""
    try:
        age = time.time() - float(created_at)
    except (TypeError, ValueError):
        return True
    return age < NO_PAYMENT_RETRY_WINDOW_SECONDS


def _handle_money_back_event(db: Session, payload: Dict[str, Any], event: str) -> Dict[str, str]:
    """Refund processed / dispute lost: take the money back out of the wallet. Dispute opened: flag it."""
    is_refund = event.startswith("refund.")
    entity = _nested_get(payload, "payload", "refund" if is_refund else "dispute", "entity")
    if not isinstance(entity, dict):
        return {"status": "unparseable"}
    entity_id = _as_id(entity.get("id"))
    payment_id = _as_id(entity.get("payment_id"))
    try:
        amount_paise = int(entity.get("amount"))
    except (TypeError, ValueError):
        amount_paise = 0
    if not entity_id or not payment_id or amount_paise <= 0:
        logger.warning(f"Razorpay {event} payload is missing id, payment id or amount")
        return {"status": "unparseable"}
    amount = _paise_to_rupees(amount_paise)
    if amount == 0:
        logger.warning(f"Razorpay {event} {entity_id}: {amount_paise} paise is under half a rupee; wallet not changed.")
        return {"status": "ignored_sub_rupee"}

    currency = str(entity.get("currency") or "").strip().upper()
    # A refund/dispute record that omits its currency is judged by the original payment, which the
    # credit path already verified as INR; one that names another currency is never applied.
    if currency and currency != "INR":
        logger.error(f"ALERT Razorpay {event} {entity_id}: currency '{currency}' is not INR; not applied.")
        _record_payment_review(db, entity_id, "currency_not_inr", {"event": event, "payment_id": payment_id})
        return {"status": "unsupported_currency"}

    if event == EVENT_DISPUTE_CREATED:
        # Razorpay only holds the money while a dispute is open; it is taken back if the dispute is lost.
        logger.error(
            f"ALERT Razorpay dispute {entity_id} opened on payment {payment_id} (₹{amount}). "
            "The wallet is not changed unless the dispute is lost; respond to it in the Razorpay dashboard."
        )
        _record_payment_review(db, entity_id, "dispute_opened", {"payment_id": payment_id, "amount": amount})
        return {"status": "dispute_flagged"}

    kind = KIND_DEBIT_REFUND if is_refund else KIND_DEBIT_DISPUTE
    try:
        # A SKU pack payment gives back credits; anything else is a wallet payment, exactly as before.
        result = sku_packs.claw_back_pack(db, payment_id=payment_id, entity_id=entity_id, amount_rupees=amount, kind=kind)
        if result is None:
            result = claw_back_payment(db, payment_id=payment_id, entity_id=entity_id, amount=amount, kind=kind)
    except Exception as e:
        logger.error(f"Razorpay {event} {entity_id}: claw-back failed, asking Razorpay to retry: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not apply the refund yet, retry later",
        )
    if result["outcome"] == CLAWBACK_OUTCOME_NO_PAYMENT:
        logger.error(
            f"ALERT Razorpay {event} {entity_id} refers to payment {payment_id}, which this system never credited. "
            "Nothing was taken from any wallet; review it."
        )
        _record_payment_review(db, entity_id, "no_matching_credit", {"event": event, "payment_id": payment_id, "amount": amount})
        if _is_recent(entity.get("created_at")):
            # The payment event may simply not have been credited yet (still in flight, or waiting for
            # Razorpay's own retry). Answer 503 so Razorpay delivers this refund again later; answering
            # 200 would drop it for good and the customer would keep the refunded money.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Payment not credited yet, retry later",
            )
        return {"status": "no_matching_credit"}
    if result["outcome"] == CLAWBACK_OUTCOME_DUPLICATE:
        return {"status": "already_processed"}
    if not is_refund:
        # The "dispute opened" flag is closed now that the dispute has been decided.
        try:
            db.query(AuditLog).filter(
                AuditLog.action == AUDIT_ACTION_PAYMENT_REVIEW,
                AuditLog.resource_id == entity_id,
                AuditLog.status == "pending",
                AuditLog.details.contains('"dispute_opened"'),
            ).update({AuditLog.status: "resolved"}, synchronize_session=False)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning(f"Could not close the dispute flag for {entity_id}: {e}")
    return {"status": "clawed_back" if not result["shortfall"] else "clawed_back_partial"}


def _whatsapp_pay_will_credit() -> bool:
    """True while the WhatsApp Pay path (and its reconcile sweep) is on, so it can credit its own payments."""
    return bool(settings.WHATSAPP_PAY_ENABLED) and int(settings.WHATSAPP_PAY_RECONCILE_INTERVAL_SECONDS or 0) > 0


def _whatsapp_pay_order_state(db: Session, payment_id: str, payload: Dict[str, Any]) -> Optional[str]:
    """Status of the WhatsApp Pay order this Razorpay payment belongs to (None = not a WhatsApp Pay payment).

    Matched exactly, by the Razorpay payment id or order id recorded on the order. Never raises.
    """
    try:
        order_id = _as_id(_nested_get(payload, "payload", "payment", "entity", "order_id"))
        match = WhatsAppPaymentOrder.pg_payment_id == payment_id
        if order_id:
            match = or_(match, WhatsAppPaymentOrder.pg_order_id == order_id)
        row = db.query(WhatsAppPaymentOrder.status, WhatsAppPaymentOrder.credited).filter(match).first()
    except Exception as e:
        db.rollback()
        logger.error(f"WhatsApp Pay order lookup failed for payment {payment_id}: {e}")
        return None
    if row is None:
        return None
    return "captured" if row[1] else (row[0] or "created")


def _whole(raw: Any) -> int:
    """A note value as a whole number (-1 when it is not one). Never raises."""
    text = str(raw).strip() if isinstance(raw, (str, int)) and not isinstance(raw, bool) else ""
    try:
        return int(text) if text.isascii() and text.isdigit() else -1
    except ValueError:                          # more digits than int() accepts
        return -1


def _pack_counts(payload: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """(white-background SKUs, Catalog Pack SKUs) of a SKU pack payment (notes.purpose "sku_pack"; payment notes
    first, then the payment link's notes).

    None = not a pack (a wallet recharge, as before). (0, 0) = a pack whose counts are missing or not whole numbers
    from 0 to MAX_PACK_UNITS (at least one item): such a payment is parked for review, never credited."""
    for path in (("payload", "payment", "entity", "notes"), ("payload", "payment_link", "entity", "notes")):
        notes = _nested_get(payload, *path)
        if not isinstance(notes, dict) or str(notes.get("purpose") or "").strip().lower() != sku_packs.PURPOSE_SKU_PACK:
            continue
        units = _whole(notes.get("units", notes.get("total_skus")))
        creative = _whole(notes.get("creative_packs", 0))
        limit = sku_packs.MAX_PACK_UNITS
        if 0 <= units <= limit and 0 <= creative <= limit and units + creative >= 1:
            return units, creative
        return 0, 0
    return None


def _pack_matches_link(db: Session, payload: Dict[str, Any], units: int, amount_paid: int) -> bool:
    """A pack is granted only for a payment of a pack link WE created, for that link's units and amount: notes on a
    payment from anywhere else (a payment page, a checkout) are never trusted."""
    from app.models.razorpay_payment_link import RazorpayPaymentLink

    link_id = _as_id(_nested_get(payload, "payload", "payment_link", "entity", "id"))
    if not link_id:
        return False
    row = db.query(RazorpayPaymentLink).filter(RazorpayPaymentLink.link_id == link_id).first()
    return (row is not None and row.purpose == sku_packs.PURPOSE_SKU_PACK and int(row.units or 0) == int(units)
            and int(row.amount_rupees) == int(amount_paid))


def _grant_cart(db: Session, customer: Customer, white: int, creative: int, payment_id: str) -> None:
    """Grant every SKU a cart payment bought, in the caller's transaction (each once: a repeat raises IntegrityError)."""
    if white:
        sku_packs.grant_pack_credits(db, customer, white, payment_id)
    if creative:
        sku_packs.grant_pack_credits(db, customer, creative, payment_id, sku=SKU_CREATIVE)


def _resolve_customer(db: Session, sender_id: str) -> Optional[Customer]:
    if not sender_id:
        return None

    return find_customer_by_phone(db, sender_id)


def _pack_receipt(db: Session, customer: Customer, units: int, creative: int = 0) -> str:
    """The pack receipt with the credits and validity read from the ledger at send time (includes this grant)."""
    until = sku_packs.valid_until(db, customer.id) if units else sku_packs.valid_until(db, customer.id, SKU_CREATIVE)
    bought = [f"{pricing.STUDIO_TITLE} {pricing.pack_title(units)}"] if units else []
    if creative:
        bought.append(f"{pricing.CREATIVE_TITLE} {pricing.pack_title(creative)}")
    text = PACK_RECEIPT_MESSAGE.format(
        pack=" + ".join(bought),
        credits=sku_packs.balance(db, customer.id),
        valid_till=sku_packs.ist_date(until) if until is not None else "-",
    )
    if creative:
        text += f"\n{pricing.CREATIVE_TITLE} SKUs available: {sku_packs.balance(db, customer.id, SKU_CREATIVE)}"
    return text if getattr(customer, "email", None) else text + PACK_EMAIL_REQUEST_LINE


async def _queue_invoice(background_tasks: BackgroundTasks, args: Dict[str, Any]) -> None:
    """Send the invoice through the durable outbox (retried, survives a restart); plain background task if the outbox
    is off or the database could not record it."""
    if settings.OUTBOX_ENABLED:
        from app.services import outbox

        payload = {k: args[k] for k in ("recipient_id", "payment_id", "amount", "customer_name", "customer_snapshot")}
        outcome = await run_io(outbox.enqueue_status, "payment_invoice", payload, f"inv:{args['payment_id']}")
        if outcome == outbox.QUEUED:
            outbox.ensure_default_handlers()
            background_tasks.add_task(outbox.drain_once)
            return
        if outcome == outbox.DUPLICATE:
            return                                # this payment's invoice is already queued or sent
    background_tasks.add_task(dispatch_payment_invoice, **args)


def _already_processed(db: Session, payment_reference: str) -> bool:
    if not payment_reference:
        return False
    try:
        existing = (
            db.query(AuditLog.id)
            .filter(
                AuditLog.action == AUDIT_ACTION_PAYMENT_CAPTURED,
                AuditLog.resource_id == payment_reference,
            )
            .first()
        )
        return existing is not None
    except Exception as e:
        logger.error(f"Audit lookup error: {e}")
        db.rollback()
        return False


def _record_unmatched_payment(
    db: Session, payment_reference: str, amount_paid: int, event: str
) -> None:
    """Keep a durable record of a captured payment with no payer phone.

    Retrying cannot add a phone to the payload, so the webhook still answers
    200 (Razorpay disables a webhook that keeps failing for 24 hours). This
    pending audit row plus the ALERT log line let an operator find the
    payment and credit the payer by hand. Recorded once per payment. Never
    raises.
    """
    logger.error(
        f"ALERT Razorpay payment {payment_reference} (₹{amount_paid}, event={event}) "
        "has no payer phone and was NOT credited. If another event for this payment "
        "credits it, the audit row is marked resolved; otherwise identify the payer "
        "and credit manually."
    )
    try:
        exists = (
            db.query(AuditLog.id)
            .filter(
                AuditLog.action == AUDIT_ACTION_PAYMENT_UNMATCHED,
                AuditLog.resource_id == payment_reference,
            )
            .first()
        )
        if exists:
            return
        db.add(
            AuditLog(
                user_id=None,
                action=AUDIT_ACTION_PAYMENT_UNMATCHED,
                resource_id=payment_reference,
                resource_type=AUDIT_RESOURCE_TYPE,
                status="pending",
                details=json.dumps(
                    {"amount_paid": amount_paid, "currency": "INR", "event": event}
                ),
            )
        )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Could not record unmatched payment {payment_reference}: {e}")


def _record_credit_failure(
    db: Session, payment_reference: str, amount_paid: int, reason: str
) -> int:
    """Record one failed credit attempt; return how many have been recorded.

    Written in its own transaction after the failed one was rolled back, so
    the money claim stays free for Razorpay's retry. Never raises (returns 0
    when even the record cannot be written, so the caller keeps retrying).
    """
    try:
        db.add(
            AuditLog(
                user_id=None,
                action=AUDIT_ACTION_PAYMENT_CREDIT_FAILED,
                resource_id=payment_reference,
                resource_type=AUDIT_RESOURCE_TYPE,
                status="pending",
                details=json.dumps(
                    {"amount_paid": amount_paid, "currency": "INR", "reason": reason[:300]}
                ),
            )
        )
        db.commit()
        return (
            db.query(AuditLog.id)
            .filter(
                AuditLog.action == AUDIT_ACTION_PAYMENT_CREDIT_FAILED,
                AuditLog.resource_id == payment_reference,
            )
            .count()
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Could not record credit failure for {payment_reference}: {e}")
        return 0


def _retry_or_give_up(
    db: Session, payment_reference: str, amount_paid: int, reason: str
) -> Dict[str, str]:
    """Answer 503 so Razorpay retries, until MAX_CREDIT_ATTEMPTS is reached."""
    attempts = _record_credit_failure(db, payment_reference, amount_paid, reason)
    if attempts >= MAX_CREDIT_ATTEMPTS:
        logger.error(
            f"ALERT Razorpay payment {payment_reference} (₹{amount_paid}) failed to credit "
            f"{attempts} times ({reason}); NOT credited and no longer retried. "
            "Credit manually after fixing the cause."
        )
        return {"status": "credit_failed"}
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Payment not credited yet, retry later",
    )


def _payment_audit_row(
    payment_reference: str,
    sender_id: str,
    amount_paid: int,
    auto_provisioned: bool = False,
    pack_units: Optional[int] = None,
    creative_packs: int = 0,
) -> AuditLog:
    details: Dict[str, Any] = {
        "sender_id": sender_id,
        "amount_paid": amount_paid,
        "currency": "INR",
        # Payer had no matching customer; money is held on an
        # unregistered wallet row until they register.
        "auto_provisioned": auto_provisioned,
    }
    if pack_units or creative_packs:
        details.update({"purpose": sku_packs.PURPOSE_SKU_PACK, "units": pack_units, "creative_packs": creative_packs})
    return AuditLog(
        user_id=None,
        action=AUDIT_ACTION_PAYMENT_CAPTURED,
        resource_id=payment_reference,
        resource_type=AUDIT_RESOURCE_TYPE,
        status="success",
        details=json.dumps(details),
    )


@router.post(
    "/razorpay/webhook",
    summary="Razorpay payment webhook",
    status_code=status.HTTP_200_OK,
)
async def razorpay_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Dict[str, str]:
    raw_body = await request.body()
    signature_header = request.headers.get(SIGNATURE_HEADER)

    if settings.RAZORPAY_WEBHOOK_SECRET:
        if not verify_razorpay_signature(raw_body, signature_header):
            logger.error("Razorpay webhook signature verification failed.")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Razorpay webhook signature",
            )
    elif not settings.ALLOW_UNSIGNED_WEBHOOKS:
        logger.error(
            "Razorpay webhook rejected: RAZORPAY_WEBHOOK_SECRET not configured "
            "and ALLOW_UNSIGNED_WEBHOOKS is off."
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook not configured",
        )

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        logger.error(f"Malformed JSON in webhook: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    if not isinstance(payload, dict):
        logger.error("Razorpay webhook JSON body is not an object")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    return await process_razorpay_event(db, payload, background_tasks)


async def process_razorpay_event(
    db: Session, payload: Dict[str, Any], background_tasks: BackgroundTasks
) -> Dict[str, str]:
    """Apply one (already verified) Razorpay event: credit a payment, take back a refund, or flag it.

    The webhook calls this after checking the signature; the payment-link reconcile sweep calls it with an
    event rebuilt from Razorpay's own answer. Both therefore credit through exactly the same once-only claim.
    """
    event = payload.get("event", "")
    logger.info(f"Received Razorpay webhook event: '{event}'")

    if event in MONEY_BACK_EVENTS or event == EVENT_DISPUTE_CREATED:
        return _handle_money_back_event(db, payload, event)

    if event not in ("payment_link.paid", "payment.captured", "order.paid"):
        return {"status": "ignored", "event": event}

    extracted = extract_payment_data(payload)
    if extracted is None:
        logger.warning(f"Unable to extract required payment data from payload for event: {event}")
        return {"status": "unparseable"}

    sender_id, amount_paid, payment_reference = extracted
    currency = extract_currency(payload)
    if currency != "INR":
        # The wallet is in rupees: a payment in any other (or no) currency is never credited as rupees.
        logger.error(
            f"ALERT Razorpay payment {payment_reference}: currency '{currency or 'missing'}' is not INR; "
            "NOT credited. Review it in the Razorpay dashboard."
        )
        _record_payment_review(db, payment_reference, "currency_not_inr", {"currency": currency, "event": event})
        return {"status": "unsupported_currency"}
    wa_pay_state = _whatsapp_pay_order_state(db, payment_reference, payload)
    if wa_pay_state is not None and (wa_pay_state == "captured" or _whatsapp_pay_will_credit()):
        # This payment belongs to a WhatsApp Pay order: that path credits the amount that was ORDERED,
        # once Meta confirms it (its sweep re-checks "sent"/"pending" orders). Crediting it here too would use
        # a different amount for the same money. When that path is off, or the order is in a state it will
        # not credit, this path credits (or parks) the payment itself, so the payer is never left unpaid.
        if wa_pay_state in WHATSAPP_PAY_ACTIVE_STATES:
            logger.info(f"Payment {payment_reference} belongs to a WhatsApp Pay order; left to that path.")
            return {"status": "whatsapp_pay_order"}
        if _already_processed(db, payment_reference):
            return {"status": "already_processed"}
        logger.error(
            f"ALERT Razorpay payment {payment_reference} belongs to a WhatsApp Pay order in state '{wa_pay_state}' "
            "that will not be credited; parked for manual review."
        )
        record_pending_payment(
            db, payment_id=payment_reference, amount_rupees=amount_paid, currency=currency, event=event,
            payer_hint=sender_id, reason="whatsapp_pay_not_credited",
        )
        return {"status": "whatsapp_pay_order_review"}
    if not sender_id or not is_plausible_phone(sender_id):
        # Razorpay sends several events per payment; another event that did
        # carry the phone may already have credited it.
        if _already_processed(db, payment_reference):
            return {"status": "already_processed"}
        # No phone, or only a Razorpay id like "cust_...": never credit a wallet nobody can use.
        _record_unmatched_payment(db, payment_reference, amount_paid, event)
        record_pending_payment(
            db, payment_id=payment_reference, amount_rupees=amount_paid, currency=currency, event=event,
            payer_hint=sender_id, reason="missing_phone" if not sender_id else "not_a_phone_number",
        )
        return {"status": "missing_phone"}

    if _already_processed(db, payment_reference):
        logger.info(f"Payment {payment_reference} already processed, skipping duplicate (event={event}, no credit).")
        return {"status": "already_processed"}

    # A SKU pack payment grants credits instead of rupees; its units come from the notes we set on the link.
    pack = _pack_counts(payload)
    pack_white, pack_creative = pack if pack else (0, 0)
    pack_units = (pack_white + pack_creative) if pack else None     # truthy = this payment buys SKU credits
    if pack_units == 0:
        logger.error(
            f"ALERT Razorpay payment {payment_reference} (₹{amount_paid}) is a SKU pack with unusable units; "
            "NOT credited and parked for manual review."
        )
        record_pending_payment(
            db, payment_id=payment_reference, amount_rupees=amount_paid, currency=currency, event=event,
            payer_hint=sender_id, reason="bad_pack_units",
        )
        return {"status": "pack_units_invalid"}
    if pack_units and not _pack_matches_link(db, payload, pack_white, amount_paid):
        logger.error(
            f"ALERT Razorpay payment {payment_reference} (₹{amount_paid}) says it is a SKU pack but does not match a "
            "pack link we created; NOT credited and parked for manual review."
        )
        record_pending_payment(
            db, payment_id=payment_reference, amount_rupees=amount_paid, currency=currency, event=event,
            payer_hint=sender_id, reason="pack_unverified",
        )
        return {"status": "pack_unverified"}

    # Digits only: spaces/hyphens/brackets in the payer's number must not
    # create a second wallet row for the same phone.
    clean_sender = normalize_phone(sender_id)
    customer = _resolve_customer(db, clean_sender)
    customer_name = "Valued Customer"

    customer_was_created = customer is None
    # The audit row is the idempotency claim: it is written in the SAME
    # transaction as the wallet change, and uq_audit_logs_money_once makes a
    # concurrent duplicate fail -- so a payment credits at most once.
    db.add(_payment_audit_row(payment_reference, clean_sender, amount_paid, customer_was_created, pack_white,
                              pack_creative))
    try:
        # Claim first: a concurrent duplicate fails here (on PostgreSQL it
        # waits for the winner's commit, then fails) before any credit.
        db.flush()
    except IntegrityError:
        db.rollback()
        logger.info(f"Payment {payment_reference} already processed concurrently, skipping duplicate.")
        return {"status": "already_processed"}

    if customer is None:
        # Payer has no WhatsApp registration yet. Hold the money on an
        # UNREGISTERED wallet row (placeholder NOT NULL values, as before); the
        # registration flow adopts this row by phone and fills in real details.
        customer = Customer(
            whatsapp_id=clean_sender,
            full_name="Valued Customer",
            business_name="Jewelry Business",
            gst_number="N/A",
            address="N/A",
            wallet_balance=0 if pack_units else amount_paid,      # a pack buys credits, never wallet rupees
            is_registered=False,
        )
        db.add(customer)
    try:
        if customer_was_created:
            db.flush()      # assigns customer.id; a concurrent first payment conflicts here (IntegrityError)
            if pack_units:
                _grant_cart(db, customer, pack_white, pack_creative, payment_reference)
            else:
                record_ledger(db, customer_id=customer.id, kind=KIND_CREDIT_PAYMENT, amount=amount_paid, ref=payment_reference)
        else:
            if pack_units:
                _grant_cart(db, customer, pack_white, pack_creative, payment_reference)
            elif credit_wallet(db, customer.whatsapp_id, amount_paid, commit=False, ref=payment_reference) != 1:
                raise RuntimeError("customer row not updated")
            if getattr(customer, "full_name", None):
                customer_name = customer.full_name
        # Close any earlier "not credited" alerts for this payment in the same
        # transaction, so nobody credits it a second time by hand.
        mark_pending_credited(db, payment_reference, clean_sender)
        db.query(AuditLog).filter(
            AuditLog.action.in_((AUDIT_ACTION_PAYMENT_UNMATCHED, AUDIT_ACTION_PAYMENT_CREDIT_FAILED)),
            AuditLog.resource_id == payment_reference,
            AuditLog.status == "pending",
        ).update({AuditLog.status: "resolved"}, synchronize_session=False)
        db.commit()
    except IntegrityError as integrity_error:
        db.rollback()
        logger.warning(f"Payment {payment_reference}: integrity error: {getattr(integrity_error, 'orig', integrity_error)}")
        if _already_processed(db, payment_reference):
            logger.info(f"Payment {payment_reference} already processed concurrently, skipping duplicate.")
            return {"status": "already_processed"}
        # Usually a concurrent first payment created the same customer row.
        # The claim was rolled back too, so Razorpay's retry credits normally
        # (and then finds the existing customer). A 200 here lost the money.
        logger.error(
            f"Payment webhook: customer provisioning conflict for {mask_phone(clean_sender)}; "
            f"payment {payment_reference} not credited yet, asking Razorpay to retry."
        )
        return _retry_or_give_up(db, payment_reference, amount_paid, "customer provisioning conflict")
    except Exception as e:
        db.rollback()
        logger.error(
            f"Payment webhook: wallet credit failed for {mask_phone(clean_sender)}: {e}; "
            f"payment {payment_reference} not credited yet, asking Razorpay to retry."
        )
        return _retry_or_give_up(db, payment_reference, amount_paid, f"wallet credit failed: {e}")

    if pack_units:
        logger.info(
            f"SKU pack of {pack_units} granted for ₹{amount_paid} to {mask_phone(clean_sender)}: event={event} "
            f"payment_id={payment_reference}. Now sending WhatsApp confirmation."
        )
    else:
        logger.info(
            f"Wallet credited ₹{amount_paid} for {mask_phone(clean_sender)}: event={event} "
            f"payment_id={payment_reference} balance_after={get_balance(db, customer.whatsapp_id)}. "
            "Now sending WhatsApp confirmation."
        )

    # WhatsApp Notifications Dispatch
    try:
        # 1. Payment receipt with the balance read from the DB at send time
        # (includes this credit; never echoes the payment amount as balance).
        if pack_units:
            await send_whatsapp_text(recipient_id=clean_sender, message_text=_pack_receipt(db, customer, pack_white, pack_creative))
        else:
            await send_whatsapp_text(
                recipient_id=clean_sender,
                message_text=PAYMENT_TIPS_MESSAGE.format(
                    paid=f"{amount_paid:,}",
                    balance=f"{get_balance(db, customer.whatsapp_id):,}",
                ),
            )

        # 2. PDF Invoice Dispatch -- in the background after the response:
        # ERPNext Sales Invoice PDF when ERPNEXT_INVOICE_ENABLED, otherwise
        # (or on any ERPNext failure) the same local ReportLab receipt.
        invoice_args = dict(
            recipient_id=clean_sender,
            payment_id=payment_reference,
            amount=amount_paid,
            customer_name=customer_name,
            customer_snapshot={
                "full_name": getattr(customer, "full_name", None),
                "business_name": getattr(customer, "business_name", None),
                "gst_number": getattr(customer, "gst_number", None),
                "is_gst_verified": bool(getattr(customer, "is_gst_verified", False)),
                "address": getattr(customer, "address", None),
            },
            local_pdf_fn=generate_invoice_pdf,
            send_document_fn=send_document_to_whatsapp,
        )
        await _queue_invoice(background_tasks, invoice_args)

        logger.info(f"Sent confirmation and tips to {mask_phone(clean_sender)}; invoice queued")
    except Exception as e:
        logger.error(f"Post-payment WhatsApp dispatch failed: {e}")

    if pack_units:
        # Usage Logs sheet row and Drive folder share (outbox jobs; no-ops unless Drive delivery is on).
        await run_io(sku_packs.queue_followups, customer.id)
        if pack_white:
            # Photos sent before paying start now with the new SKUs; their images go to the customer's Drive folder.
            from app.api.routes.meta_webhook import start_held_photos

            await start_held_photos(db, clean_sender, background_tasks)

    return {"status": "ok"}