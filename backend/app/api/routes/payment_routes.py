"""Razorpay payment webhook routes — wallet recharge lifecycle.

Handles both standard payment links and Razorpay Payment Pages.
"""

import hashlib
import hmac
import json
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.services.invoice_service import generate_invoice_pdf
from app.services.meta_whatsapp_service import (
    send_document_to_whatsapp,
    send_whatsapp_text,
)
from app.services.wallet_service import credit_wallet, find_customer_by_phone, get_balance
from app.utils.logger import logger

router = APIRouter(prefix="/api/payments", tags=["Payments"])

SIGNATURE_HEADER = "X-Razorpay-Signature"
PAISE_PER_RUPEE = 100

AUDIT_ACTION_PAYMENT_CAPTURED = "razorpay_payment_captured"
AUDIT_RESOURCE_TYPE = "razorpay_payment"

PAYMENT_TIPS_MESSAGE = (
    "Payment Received 💳\n\n"
    "₹{paid} has been added to your wallet.\n"
    "Current Balance: ₹{balance}\n\n"
    "Send your earring photo whenever you're ready! 📸\n"
    "(Tip: Good lighting and sharp focus produce the best studio results)"
)


def verify_razorpay_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    secret = (settings.RAZORPAY_WEBHOOK_SECRET or "").strip()
    if not secret:
        return True
    if not signature_header:
        return False

    computed = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed, signature_header.strip())


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


def _resolve_customer(db: Session, sender_id: str) -> Optional[Customer]:
    if not sender_id:
        return None

    return find_customer_by_phone(db, sender_id)


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


def _payment_audit_row(
    payment_reference: str,
    sender_id: str,
    amount_paid: int,
    auto_provisioned: bool = False,
) -> AuditLog:
    return AuditLog(
        user_id=None,
        action=AUDIT_ACTION_PAYMENT_CAPTURED,
        resource_id=payment_reference,
        resource_type=AUDIT_RESOURCE_TYPE,
        status="success",
        details=json.dumps(
            {
                "sender_id": sender_id,
                "amount_paid": amount_paid,
                "currency": "INR",
                # Payer had no matching customer; money is held on an
                # unregistered wallet row until they register.
                "auto_provisioned": auto_provisioned,
            }
        ),
    )


@router.post(
    "/razorpay/webhook",
    summary="Razorpay payment webhook",
    status_code=status.HTTP_200_OK,
)
async def razorpay_webhook(
    request: Request,
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
    elif not settings.DEBUG:
        logger.error("Razorpay webhook rejected: RAZORPAY_WEBHOOK_SECRET not configured and DEBUG is False.")
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

    event = payload.get("event", "")
    logger.info(f"Received Razorpay webhook event: '{event}'")

    if event not in ("payment_link.paid", "payment.captured", "order.paid"):
        return {"status": "ignored", "event": event}

    extracted = extract_payment_data(payload)
    if extracted is None:
        logger.warning(f"Unable to extract required payment data from payload for event: {event}")
        return {"status": "unparseable"}

    sender_id, amount_paid, payment_reference = extracted
    if not sender_id:
        logger.warning(f"Payment {payment_reference} processed but sender phone number not found.")
        return {"status": "missing_phone"}

    if _already_processed(db, payment_reference):
        logger.info(f"Payment {payment_reference} already processed, skipping duplicate (event={event}, no credit).")
        return {"status": "already_processed"}

    clean_sender = sender_id.lstrip("+").strip()
    customer = _resolve_customer(db, sender_id)
    customer_name = "Valued Customer"

    customer_was_created = customer is None
    # The audit row is the idempotency claim: it is written in the SAME
    # transaction as the wallet change, and uq_audit_logs_money_once makes a
    # concurrent duplicate fail -- so a payment credits at most once.
    db.add(_payment_audit_row(payment_reference, clean_sender, amount_paid, customer_was_created))
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
            wallet_balance=amount_paid,
            is_registered=False,
        )
        db.add(customer)
    try:
        if not customer_was_created:
            if credit_wallet(db, customer.whatsapp_id, amount_paid, commit=False) != 1:
                raise RuntimeError("customer row not updated")
            if getattr(customer, "full_name", None):
                customer_name = customer.full_name
        db.commit()
    except IntegrityError:
        db.rollback()
        if _already_processed(db, payment_reference):
            logger.info(f"Payment {payment_reference} already processed concurrently, skipping duplicate.")
            return {"status": "already_processed"}
        logger.error(f"Payment webhook: customer provisioning conflict for {clean_sender}")
        return {"status": "error", "message": "Customer provisioning failed"}
    except Exception as e:
        db.rollback()
        logger.error(f"Payment webhook: wallet credit failed for {clean_sender}: {e}")
        return {"status": "error", "message": "Wallet credit failed"}

    logger.info(
        f"Wallet credited ₹{amount_paid} for {clean_sender}: event={event} "
        f"payment_id={payment_reference} balance_after={get_balance(db, customer.whatsapp_id)}. "
        "Now sending WhatsApp confirmation."
    )

    # WhatsApp Notifications Dispatch
    try:
        # 1. Payment receipt with the balance read from the DB at send time
        # (includes this credit; never echoes the payment amount as balance).
        await send_whatsapp_text(
            recipient_id=clean_sender,
            message_text=PAYMENT_TIPS_MESSAGE.format(
                paid=f"{amount_paid:,}",
                balance=f"{get_balance(db, customer.whatsapp_id):,}",
            ),
        )

        # 2. PDF Invoice Dispatch
        inv_suffix = payment_reference[-4:] if len(payment_reference) >= 4 else "1042"
        inv_number = f"Invoice_MoraaStudio_{inv_suffix}"
        pdf_bytes = generate_invoice_pdf(
            customer_name=customer_name,
            invoice_number=inv_number,
            amount=amount_paid,
        )

        await send_document_to_whatsapp(
            recipient_id=clean_sender,
            document_bytes=pdf_bytes,
            filename=f"{inv_number}.pdf",
            caption="",
        )

        logger.info(f"Successfully sent confirmation, invoice and tips to {clean_sender}")
    except Exception as e:
        logger.error(f"Post-payment WhatsApp dispatch failed: {e}")

    return {"status": "ok"}