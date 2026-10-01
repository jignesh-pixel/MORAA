"""Native WhatsApp Pay (India) wallet recharge via ``order_details``.

Primary recharge path when ``WHATSAPP_PAY_ENABLED`` is True; OFF by default.

    try_send_native_recharge()  -> builds + sends an order_details message
                                   (Razorpay payment_gateway configuration).
                                   Returns False on ANY problem so the caller
                                   runs its existing Razorpay-link code — the
                                   "silent fallback".
    handle_payment_status_event() <- Meta webhook statuses[] with type "payment"
    reconcile_order()           -> confirms with the Meta payment lookup API
                                   (never trusts the webhook alone) and credits
                                   the wallet exactly once.
    reconcile_pending_orders()  -> sweep for orders whose webhook never arrived.

Money safety: the credit uses the SAME idempotency claim as the Razorpay
webhook (audit_logs action ``razorpay_payment_captured`` keyed on the
Razorpay payment id, protected by the ``uq_audit_logs_money_once`` unique
index) and the same ``wallet_service.credit_wallet``. If Razorpay's own
webhook also reports the payment, whichever arrives second is a no-op.
Wallet deduction, refunds and generation are not touched by this module.

Payload shapes follow Meta's "Payments API — India, payment gateway" docs:
order_details / review_and_pay, payment statuses webhook, payment lookup
``GET /<PHONE_NUMBER_ID>/payments/<PAYMENT_CONFIGURATION>/<REFERENCE_ID>``.
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.whatsapp_payment_order import WhatsAppPaymentOrder
from app.services.meta_whatsapp_service import _post_message_payload, send_whatsapp_text
from app.services.wallet_service import credit_wallet, find_customer_by_phone, get_balance
from app.utils.logger import logger, mask_phone
_plog = logger.bind(category="payments")

GRAPH_BASE = "https://graph.facebook.com/v21.0"
INR_OFFSET = 100
MIN_RECHARGE_RUPEES = 500

# Must equal payment_routes.AUDIT_ACTION_PAYMENT_CAPTURED / AUDIT_RESOURCE_TYPE:
# sharing the claim is what makes a double credit impossible.
MONEY_ONCE_ACTION = "razorpay_payment_captured"
MONEY_ONCE_RESOURCE_TYPE = "razorpay_payment"

ORDER_FAILED_MESSAGE = "Your in-chat payment didn't go through. You can pay with the link below instead."
# Strict mode (WHATSAPP_PAY_STRICT): in-chat texts that never carry a URL.
PAYMENT_UNAVAILABLE_MESSAGE = "Payment system is temporarily unavailable. Please try again shortly."
ORDER_FAILED_STRICT_MESSAGE = (
    "Your in-chat payment didn't go through. "
    "Reply *recharge {amount}* to try again."
)


# ─── Activation ──────────────────────────────────────────────────────────


def _digits(phone: str) -> str:
    return (phone or "").strip().lstrip("+")


def _allowlisted(phone: str) -> bool:
    raw = (settings.WHATSAPP_PAY_ALLOWLIST or "").strip()
    if not raw:
        return True
    wanted = {_digits(p)[-10:] for p in raw.split(",") if p.strip()}
    return _digits(phone)[-10:] in wanted


def _importer_address() -> Optional[Dict[str, str]]:
    address = {
        "address_line1": (settings.WHATSAPP_PAY_IMPORTER_ADDRESS_LINE1 or "").strip(),
        "city": (settings.WHATSAPP_PAY_IMPORTER_CITY or "").strip(),
        "zone_code": (settings.WHATSAPP_PAY_IMPORTER_ZONE_CODE or "").strip(),
        "postal_code": (settings.WHATSAPP_PAY_IMPORTER_POSTAL_CODE or "").strip(),
        "country_code": (settings.WHATSAPP_PAY_COUNTRY_OF_ORIGIN or "IN").strip(),
    }
    if not all(address.values()):
        return None
    line2 = (settings.WHATSAPP_PAY_IMPORTER_ADDRESS_LINE2 or "").strip()
    if line2:
        address["address_line2"] = line2
    return address


def native_pay_block_reason(phone: str) -> Optional[str]:
    """None when native pay may be attempted for phone, else the gate that blocks it."""
    if not settings.WHATSAPP_PAY_ENABLED:
        return "gate1_disabled: WHATSAPP_PAY_ENABLED is false"
    if not (settings.WHATSAPP_PAY_CONFIGURATION_NAME or "").strip():
        return "gate2_no_config_name: WHATSAPP_PAY_CONFIGURATION_NAME is empty"
    if _importer_address() is None:
        return ("gate3_importer_incomplete: WHATSAPP_PAY_IMPORTER_ADDRESS_LINE1/CITY/"
                "ZONE_CODE/POSTAL_CODE or COUNTRY_OF_ORIGIN is empty")
    if not _allowlisted(phone):
        return "gate4_not_allowlisted: number not in WHATSAPP_PAY_ALLOWLIST"
    return None


def is_native_pay_active(phone: str) -> bool:
    """True only when the feature is on, fully configured and allowed for phone."""
    return native_pay_block_reason(phone) is None


def _log_native_skip(recipient_id: str, site: str, reason: str) -> None:
    msg = f"WhatsApp Pay native skipped: site={site} recipient={mask_phone(recipient_id)} reason={reason}"
    # Gate 1 is the normal "feature off" state: INFO. Everything else means
    # the feature is on but this prompt will not be native: WARNING.
    if reason.startswith("gate1_"):
        _plog.info(msg)
    else:
        _plog.warning(msg)


# ─── Outbound: order_details ─────────────────────────────────────────────


def new_reference_id() -> str:
    """Max 35 chars, [A-Za-z0-9_-.] (Meta constraint)."""
    return f"mgv_{uuid.uuid4().hex[:24]}"


def _money(paise: int) -> Dict[str, int]:
    return {"value": int(paise), "offset": INR_OFFSET}


def order_totals(amount_rupees: int) -> Dict[str, int]:
    subtotal = int(amount_rupees) * INR_OFFSET
    tax = (subtotal * max(int(settings.WHATSAPP_PAY_TAX_PERCENT), 0)) // 100
    return {"subtotal": subtotal, "tax": tax, "total": subtotal + tax}


def build_order_details_payload(
    recipient_id: str,
    whatsapp_id: str,
    reference_id: str,
    amount_rupees: int,
    body_text: str,
    expires_at: datetime,
) -> Dict[str, Any]:
    """Pure builder for the interactive order_details message (no I/O)."""
    totals = order_totals(amount_rupees)
    item = {
        "retailer_id": f"wallet_recharge_{int(amount_rupees)}",
        "name": (settings.WHATSAPP_PAY_ITEM_NAME or "Wallet Recharge")[:60],
        "amount": _money(totals["subtotal"]),
        "quantity": 1,
        "country_of_origin": settings.WHATSAPP_PAY_COUNTRY_OF_ORIGIN,
        "importer_name": settings.WHATSAPP_PAY_IMPORTER_NAME,
        "importer_address": _importer_address(),
    }
    tax = _money(totals["tax"])
    if settings.WHATSAPP_PAY_TAX_DESCRIPTION:
        tax["description"] = settings.WHATSAPP_PAY_TAX_DESCRIPTION[:60]
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_id,
        "type": "interactive",
        "interactive": {
            "type": "order_details",
            "body": {"text": body_text},
            "action": {
                "name": "review_and_pay",
                "parameters": {
                    "reference_id": reference_id,
                    "type": settings.WHATSAPP_PAY_GOODS_TYPE,
                    "payment_settings": [{
                        "type": "payment_gateway",
                        "payment_gateway": {
                            "type": "razorpay",
                            "configuration_name": settings.WHATSAPP_PAY_CONFIGURATION_NAME.strip(),
                            "razorpay": {
                                "receipt": reference_id,
                                # Lets the existing Razorpay webhook resolve the
                                # customer if Razorpay also reports this payment.
                                "notes": {"whatsapp_id": whatsapp_id, "reference_id": reference_id},
                            },
                        },
                    }],
                    "currency": "INR",
                    "total_amount": _money(totals["total"]),
                    "order": {
                        "status": "pending",
                        "expiration": {
                            "timestamp": str(int(expires_at.timestamp())),
                            "description": "This recharge order has expired.",
                        },
                        "items": [item],
                        "subtotal": _money(totals["subtotal"]),
                        "tax": tax,
                    },
                },
            },
        },
    }


async def try_send_native_recharge(
    db: Session,
    recipient_id: str,
    amount_rupees: int,
    body_text: str,
    reply_to_message_id: Optional[str] = None,
    site: str = "unknown",
) -> bool:
    """Send a native WhatsApp Pay recharge order. Never raises.

    Returns True only when Meta accepted the order_details message. False
    means "not active / no customer / rejected"; the caller then calls
    send_payment_unavailable (strict mode) or its Razorpay-link code.
    Every exit is logged with the gate / Meta error that caused it.
    """
    try:
        reason = native_pay_block_reason(recipient_id)
        if reason:
            _log_native_skip(recipient_id, site, reason)
            return False
        customer = find_customer_by_phone(db, recipient_id)
        if customer is None:
            # wallet row required for crediting; link path handles placeholders
            _log_native_skip(recipient_id, site, "gate5_no_customer: no wallet row for this number")
            return False
        amount = max(int(amount_rupees), MIN_RECHARGE_RUPEES)
        expiry = max(int(settings.WHATSAPP_PAY_ORDER_EXPIRY_SECONDS), 300)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expiry)
        order = WhatsAppPaymentOrder(
            reference_id=new_reference_id(),
            whatsapp_id=customer.whatsapp_id,
            amount_rupees=amount,
            total_paise=order_totals(amount)["total"],
            status="created",
            configuration_name=settings.WHATSAPP_PAY_CONFIGURATION_NAME.strip(),
            expires_at=expires_at,
        )
        db.add(order)
        db.commit()

        payload = build_order_details_payload(
            recipient_id, customer.whatsapp_id, order.reference_id, amount, body_text, expires_at
        )
        _plog.info(
            f"WhatsApp Pay native attempt: site={site} recipient={mask_phone(recipient_id)} "
            f"ref={order.reference_id} amount_rupees={amount} config={order.configuration_name}")
        meta_error: Dict[str, Any] = {}
        sent = await _post_message_payload(
            payload, "whatsapp pay order", reply_to_message_id=reply_to_message_id, error_out=meta_error,
        )
        order.status = "sent" if sent else "dispatch_failed"
        if not sent:
            if meta_error.get("timeout"):
                order.last_error = "timeout: Meta may still have delivered the order"
            else:
                order.last_error = (
                    f"meta status={meta_error.get('status')} code={meta_error.get('code')} "
                    f"subcode={meta_error.get('subcode')}: {meta_error.get('message') or ''} "
                    f"{meta_error.get('details') or ''}"
                ).strip()[:500]
        db.commit()
        if sent:
            logger.info(f"WhatsApp Pay native sent: site={site} ref={order.reference_id}")
        else:
            _plog.warning(
                f"WhatsApp Pay native dispatch failed: site={site} recipient={mask_phone(recipient_id)} "
                f"ref={order.reference_id} code={meta_error.get('code')} subcode={meta_error.get('subcode')} "
                f"message={meta_error.get('message')!r} details={meta_error.get('details')!r} "
                f"timeout={bool(meta_error.get('timeout'))}")
        return bool(sent)
    except Exception as e:
        db.rollback()
        logger.error(f"WhatsApp Pay dispatch error: site={site} recipient={mask_phone(recipient_id)}: {e}")
        return False


async def send_payment_unavailable(
    recipient_id: str,
    site: str,
    body_text: Optional[str] = None,
    reply_to_message_id: Optional[str] = None,
) -> bool:
    """Strict-mode replacement for every Razorpay-link fallback. Never raises.

    Returns False when WHATSAPP_PAY_STRICT is off (caller keeps its link
    code). Returns True when strict mode handled the prompt: an in-chat
    notice was sent (no URL) and an ALERT logged; the caller must stop.
    """
    if not settings.WHATSAPP_PAY_STRICT:
        return False
    _plog.error(
        f"ALERT WhatsApp Pay strict: native recharge not sent, no link emitted: "
        f"site={site} recipient={mask_phone(recipient_id)}")
    text = f"{body_text}\n\n{PAYMENT_UNAVAILABLE_MESSAGE}" if body_text else PAYMENT_UNAVAILABLE_MESSAGE
    try:
        await send_whatsapp_text(recipient_id, text, reply_to_message_id=reply_to_message_id)
    except Exception as e:
        logger.error(f"WhatsApp Pay strict notice failed for {mask_phone(recipient_id)}: {e}")
    return True


async def send_order_status(recipient_id: str, reference_id: str, status: str, description: str = "") -> bool:
    order: Dict[str, Any] = {"status": status}
    if description:
        order["description"] = description[:120]
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_id,
        "type": "interactive",
        "interactive": {
            "type": "order_status",
            "body": {"text": "Wallet recharge update"},
            "action": {"name": "review_order", "parameters": {"reference_id": reference_id, "order": order}},
        },
    }
    return await _post_message_payload(payload, "whatsapp pay order status")


# ─── Inbound: payment status webhook ─────────────────────────────────────


def parse_payment_status(status: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise one webhook ``statuses[]`` entry whose ``type == "payment"``."""
    payment = status.get("payment") if isinstance(status.get("payment"), dict) else {}
    transaction = payment.get("transaction") if isinstance(payment.get("transaction"), dict) else {}
    return {
        "type": "payment_status",
        "status_id": status.get("id", ""),
        "recipient_id": status.get("recipient_id", ""),
        "payment_status": status.get("status", ""),
        "reference_id": payment.get("reference_id", ""),
        "amount": payment.get("amount") or {},
        "currency": payment.get("currency", ""),
        "transaction_status": transaction.get("status", ""),
        "pg_order_id": transaction.get("id", ""),
        "pg_payment_id": transaction.get("pg_transaction_id", ""),
        "timestamp": status.get("timestamp", ""),
    }


async def handle_payment_status_event(db: Session, event: Dict[str, Any]) -> str:
    """Webhook entry point. The webhook is only a trigger: money moves only
    after reconcile_order() confirms with the payment lookup API. Never raises."""
    try:
        reference_id = (event.get("reference_id") or "").strip()
        order = (
            db.query(WhatsAppPaymentOrder).filter(WhatsAppPaymentOrder.reference_id == reference_id).first()
            if reference_id else None
        )
        if order is None:
            logger.warning(f"WhatsApp Pay status for unknown reference_id={reference_id!r} ignored")
            return "unknown_order"
        if event.get("pg_order_id") and not order.pg_order_id:
            order.pg_order_id = str(event["pg_order_id"])[:64]

        if event.get("transaction_status") == "failed" and event.get("payment_status") != "captured":
            if order.status not in ("captured",):
                order.status = "failed"
            db.commit()
            await _send_fallback_link_once(db, order)
            return "failed"

        db.commit()
        return await reconcile_order(db, order)
    except Exception as e:
        db.rollback()
        logger.error(f"WhatsApp Pay status handling failed: {e}")
        return "error"


# ─── Reconciliation ──────────────────────────────────────────────────────


async def lookup_payment(configuration_name: str, reference_id: str) -> Optional[Dict[str, Any]]:
    """GET /<PHONE_NUMBER_ID>/payments/<PAYMENT_CONFIGURATION>/<REFERENCE_ID>."""
    if not settings.META_WHATSAPP_TOKEN or not settings.META_PHONE_NUMBER_ID:
        return None
    url = f"{GRAPH_BASE}/{settings.META_PHONE_NUMBER_ID}/payments/{configuration_name}/{reference_id}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}"})
        if resp.status_code != 200:
            logger.error(f"WhatsApp Pay lookup failed: status={resp.status_code} body={resp.text[:500]}")
            return None
        data = resp.json()
    except Exception as e:
        logger.error(f"WhatsApp Pay lookup error: {e}")
        return None
    # Accept either a bare payment object or a {"payments": [...]} wrapper.
    if isinstance(data, dict) and isinstance(data.get("payments"), list):
        data = data["payments"][0] if data["payments"] else None
    return data if isinstance(data, dict) else None


def _successful_transaction(payment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for tx in payment.get("transactions") or []:
        if isinstance(tx, dict) and tx.get("status") == "success" and tx.get("pg_transaction_id"):
            return tx
    return None


def _paid_paise(payment: Dict[str, Any]) -> Optional[int]:
    amount = payment.get("amount") or {}
    try:
        value, offset = int(amount.get("value")), int(amount.get("offset", INR_OFFSET))
    except (TypeError, ValueError):
        return None
    return value * INR_OFFSET // offset if offset else None


async def reconcile_order(db: Session, order: WhatsAppPaymentOrder) -> str:
    """Confirm one order with Meta and credit the wallet at most once."""
    if order.credited:
        return "already_credited"
    payment = await lookup_payment(order.configuration_name, order.reference_id)
    if payment is None:
        return "lookup_failed"

    now = datetime.now(timezone.utc)
    if payment.get("status") != "captured":
        expires_at = order.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        order.status = "expired" if expires_at and now > expires_at else "pending"
        db.commit()
        return order.status

    tx = _successful_transaction(payment)
    paid = _paid_paise(payment)
    if tx is None or payment.get("currency") != "INR" or paid != order.total_paise:
        order.status = "amount_mismatch"
        order.last_error = f"lookup currency={payment.get('currency')} paid={paid} expected={order.total_paise}"
        db.commit()
        logger.error(f"WhatsApp Pay NOT credited ({order.reference_id}): {order.last_error}")
        return "amount_mismatch"

    pay_id = str(tx.get("pg_transaction_id"))[:36]
    order.pg_payment_id = pay_id
    if tx.get("id") and not order.pg_order_id:
        order.pg_order_id = str(tx["id"])[:64]

    db.add(AuditLog(
        action=MONEY_ONCE_ACTION,
        resource_id=pay_id,
        resource_type=MONEY_ONCE_RESOURCE_TYPE,
        status="success",
        details=json.dumps({
            "sender_id": order.whatsapp_id,
            "amount_paid": order.amount_rupees,
            "currency": "INR",
            "source": "whatsapp_pay",
            "reference_id": order.reference_id,
        }),
    ))
    try:
        db.flush()  # the money-once claim
    except IntegrityError:
        db.rollback()
        # Already credited by the Razorpay webhook (or a concurrent reconcile).
        order = db.query(WhatsAppPaymentOrder).filter(WhatsAppPaymentOrder.id == order.id).one()
        order.status, order.credited, order.pg_payment_id = "captured", True, pay_id
        db.commit()
        logger.info(f"WhatsApp Pay {order.reference_id}: payment {pay_id} already credited elsewhere")
        return "already_credited"

    try:
        if credit_wallet(db, order.whatsapp_id, order.amount_rupees, commit=False) != 1:
            raise RuntimeError("customer row not updated")
        order.status, order.credited = "captured", True
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"WhatsApp Pay credit failed for {order.reference_id}: {e}")
        return "credit_failed"

    logger.info(
        f"WhatsApp Pay credited ₹{order.amount_rupees} to {mask_phone(order.whatsapp_id)} "
        f"(ref={order.reference_id}, payment={pay_id})"
    )
    await _send_receipt(db, order)
    return "credited"


async def reconcile_pending_orders(db: Session, older_than_seconds: int = 120, limit: int = 50) -> Dict[str, int]:
    """Sweep orders whose payment webhook may have been missed."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
    rows: List[WhatsAppPaymentOrder] = (
        db.query(WhatsAppPaymentOrder)
        .filter(
            WhatsAppPaymentOrder.status.in_(("sent", "pending")),
            WhatsAppPaymentOrder.credited.is_(False),
            WhatsAppPaymentOrder.created_at <= cutoff,
        )
        .order_by(WhatsAppPaymentOrder.created_at)
        .limit(limit)
        .all()
    )
    results: Dict[str, int] = {}
    for order in rows:
        outcome = await reconcile_order(db, order)
        results[outcome] = results.get(outcome, 0) + 1
    return results


async def run_reconcile_sweep_forever() -> None:
    """Background loop started from app lifespan: reconcile missed webhooks.

    Safe to run in every worker: crediting is guarded by the money-once
    audit claim (uq_audit_logs_money_once). Never raises except on cancel.
    """
    interval = int(settings.WHATSAPP_PAY_RECONCILE_INTERVAL_SECONDS or 0)
    if interval <= 0:
        logger.info("WhatsApp Pay reconcile sweep disabled (interval <= 0)")
        return
    interval = max(interval, 60)
    from app.database import SessionLocal

    while True:
        await asyncio.sleep(interval)
        if not settings.WHATSAPP_PAY_ENABLED:
            continue
        try:
            with SessionLocal() as db:
                results = await reconcile_pending_orders(db)
            if results:
                logger.info(f"WhatsApp Pay reconcile sweep: {results}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"WhatsApp Pay reconcile sweep failed: {e}")


# ─── Customer messages ───────────────────────────────────────────────────


# Background invoice jobs started by _send_receipt (strong refs until done).
_BACKGROUND_TASKS: "set[asyncio.Task]" = set()


async def _send_receipt(db: Session, order: WhatsAppPaymentOrder) -> None:
    """Same receipt text + invoice PDF as the Razorpay webhook. Never raises.

    The PDF goes through billing_service.dispatch_payment_invoice (ERPNext
    Sales Invoice, local ReportLab fallback). With ERPNext enabled it runs as
    a detached task so the Meta webhook is not held up by ERPNext calls.
    """
    try:
        from app.api.routes.payment_routes import PAYMENT_TIPS_MESSAGE
        from app.services.billing_service import dispatch_payment_invoice
        from app.services.invoice_service import generate_invoice_pdf
        from app.services.meta_whatsapp_service import send_document_to_whatsapp

        await send_order_status(order.whatsapp_id, order.reference_id, "completed", "Wallet recharged")
        await send_whatsapp_text(
            recipient_id=order.whatsapp_id,
            message_text=PAYMENT_TIPS_MESSAGE.format(
                paid=f"{order.amount_rupees:,}",
                balance=f"{get_balance(db, order.whatsapp_id):,}",
            ),
        )
        customer = find_customer_by_phone(db, order.whatsapp_id)
        job = dispatch_payment_invoice(
            recipient_id=order.whatsapp_id,
            payment_id=order.pg_payment_id or order.reference_id,
            amount=order.amount_rupees,
            customer_name=getattr(customer, "full_name", None) or "Valued Customer",
            customer_snapshot={
                "full_name": getattr(customer, "full_name", None),
                "business_name": getattr(customer, "business_name", None),
                "gst_number": getattr(customer, "gst_number", None),
                "address": getattr(customer, "address", None),
            },
            local_pdf_fn=generate_invoice_pdf,
            send_document_fn=send_document_to_whatsapp,
        )
        if settings.ERPNEXT_INVOICE_ENABLED:
            task = asyncio.get_running_loop().create_task(job)
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_TASKS.discard)
        else:
            await job  # local PDF only: same inline behaviour as before
    except Exception as e:
        logger.error(f"WhatsApp Pay receipt dispatch failed for {order.reference_id}: {e}")


async def _send_fallback_link_once(db: Session, order: WhatsAppPaymentOrder) -> None:
    """After a failed in-chat payment, offer the Razorpay link once.

    Strict mode: no link; an in-chat "reply recharge N to retry" text instead.
    """
    if order.fallback_sent:
        return
    if settings.WHATSAPP_PAY_STRICT:
        try:
            if await send_whatsapp_text(
                order.whatsapp_id, ORDER_FAILED_STRICT_MESSAGE.format(amount=order.amount_rupees)
            ):
                order.fallback_sent = True
                db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"WhatsApp Pay strict retry notice failed for {order.reference_id}: {e}")
        return
    try:
        from app.services.meta_whatsapp_service import send_whatsapp_cta_url_button
        from app.services.razorpay_service import create_recharge_payment_link

        customer = find_customer_by_phone(db, order.whatsapp_id)
        url = await create_recharge_payment_link(
            customer_phone=order.whatsapp_id,
            customer_name=getattr(customer, "full_name", None) or "Customer",
            amount=order.amount_rupees,
        )
        if await send_whatsapp_cta_url_button(
            recipient_id=order.whatsapp_id,
            body_text=ORDER_FAILED_MESSAGE,
            button_label=f"Pay ₹{order.amount_rupees}"[:20],
            url=url or settings.RECHARGE_PAYMENT_URL,
        ):
            order.fallback_sent = True
            db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"WhatsApp Pay fallback link failed for {order.reference_id}: {e}")
