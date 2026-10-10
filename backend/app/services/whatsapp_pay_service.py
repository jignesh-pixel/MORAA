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
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import httpx
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.whatsapp_payment_order import WhatsAppPaymentOrder
from app.services import pricing
from app.services.meta_whatsapp_service import _post_message_payload, send_whatsapp_text
from app.models.wallet_transaction import KIND_CREDIT_WHATSAPP_PAY
from app.services.wallet_service import credit_wallet, find_customer_by_phone, get_balance
from app.services.pending_payment_service import mark_pending_credited
from app.utils.executors import run_io
from app.utils.logger import logger, mask_phone
from app.utils.phone import normalize_phone
_plog = logger.bind(category="payments")

GRAPH_BASE = "https://graph.facebook.com/v21.0"
INR_OFFSET = 100

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
PACK_ORDER_FAILED_STRICT_MESSAGE = "Your in-chat payment didn't go through. Reply *packs* to choose your pack again."


# ─── Activation ──────────────────────────────────────────────────────────


def _digits(phone: str) -> str:
    return (phone or "").strip().lstrip("+")


def _allowlisted(phone: str) -> bool:
    raw = (settings.WHATSAPP_PAY_ALLOWLIST or "").strip()
    if not raw:
        return True
    wanted = {normalize_phone(p) for p in raw.split(",") if p.strip()}
    return normalize_phone(phone) in wanted


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
    """A fresh id per checkout: mgv_<unix seconds>_<12 hex>, 27 chars. Meta: max 35, [A-Za-z0-9_-.];
    it is also the Razorpay receipt (max 40), so a completed order is never reloaded."""
    return f"mgv_{int(time.time())}_{uuid.uuid4().hex[:12]}"


def _money(paise: int) -> Dict[str, int]:
    return {"value": int(paise), "offset": INR_OFFSET}


def order_totals(amount_rupees: int) -> Dict[str, int]:
    subtotal = int(amount_rupees) * INR_OFFSET
    tax = (subtotal * max(int(settings.WHATSAPP_PAY_TAX_PERCENT), 0)) // 100
    return {"subtotal": subtotal, "tax": tax, "total": subtotal + tax}


def _order_item(retailer_id: str, name: str, paise: int) -> Dict[str, Any]:
    """One order line (quantity 1). Without a catalog_id Meta requires the origin and importer fields."""
    return {
        "retailer_id": retailer_id,
        "name": name[:60],
        "amount": _money(paise),
        "quantity": 1,
        "country_of_origin": settings.WHATSAPP_PAY_COUNTRY_OF_ORIGIN,
        "importer_name": settings.WHATSAPP_PAY_IMPORTER_NAME,
        "importer_address": _importer_address(),
    }


def pack_order_items(white_units: int, creative_packs: int, total_rupees: int) -> List[Dict[str, Any]]:
    """Order lines for a SKU pack cart whose amounts add up exactly to ``total_rupees`` (GST-inclusive prices): the
    Catalog Pack SKUs at today's creative pack price, the white-background SKUs at the rest."""
    from app.services import pricing

    white_units, creative_packs, total = int(white_units), int(creative_packs), int(total_rupees)
    creative_total = creative_packs * pricing.creative_pack_price() if creative_packs else 0
    white_total = total - creative_total
    white_name = f"{pricing.STUDIO_TITLE} {pricing.pack_title(white_units)}" if white_units else ""
    creative_name = f"{pricing.CREATIVE_TITLE} {pricing.pack_title(creative_packs)}" if creative_packs else ""
    if white_units and creative_packs and white_total > 0:
        return [_order_item(pricing.pack_retailer_id(white_units), white_name, white_total * INR_OFFSET),
                _order_item(pricing.catalog_retailer_id(creative_packs), creative_name, creative_total * INR_OFFSET)]
    if white_units and creative_packs:          # cannot happen for a priced cart: one line, never a negative one
        return [_order_item(f"sku_cart_{white_units}_{creative_packs}", f"{white_name} + {creative_name}",
                            total * INR_OFFSET)]
    if creative_packs:
        return [_order_item(pricing.catalog_retailer_id(creative_packs), creative_name, total * INR_OFFSET)]
    return [_order_item(pricing.pack_retailer_id(white_units), white_name, total * INR_OFFSET)]


def build_order_details_payload(
    recipient_id: str,
    whatsapp_id: str,
    reference_id: str,
    amount_rupees: int,
    body_text: str,
    expires_at: datetime,
    items: Optional[List[Dict[str, Any]]] = None,
    expiry_text: str = "This recharge order has expired.",
) -> Dict[str, Any]:
    """Pure builder for the interactive order_details message (no I/O). ``items`` (a SKU pack cart) must add up to
    ``amount_rupees``; without them the order is one wallet-recharge line."""
    totals = order_totals(amount_rupees)
    order_items = items or [_order_item(
        f"wallet_recharge_{int(amount_rupees)}", settings.WHATSAPP_PAY_ITEM_NAME or "Wallet Recharge", totals["subtotal"],
    )]
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
                            "description": expiry_text[:120],
                        },
                        "items": order_items,
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
    return await _send_native_order(db, recipient_id, amount_rupees, body_text, reply_to_message_id, site)


async def try_send_native_pack(
    db: Session,
    recipient_id: str,
    white_units: int,
    creative_packs: int,
    total_rupees: int,
    body_text: str,
    reply_to_message_id: Optional[str] = None,
    site: str = "pack_checkout",
) -> bool:
    """Send a SKU pack cart as a native order_details order (Review and pay). Never raises.

    The amount is the cart total priced by the server (pricing.py), never a recharge minimum. When Meta confirms the
    payment, reconcile_order grants the SKU credits instead of crediting the wallet. False = not active / no
    customer / rejected: the caller sends the strict "unavailable" notice or its payment link."""
    from app.services.sku_packs import MAX_PACK_UNITS, PURPOSE_SKU_PACK

    counts = (white_units, creative_packs)
    if any(isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= MAX_PACK_UNITS for n in counts) \
            or white_units + creative_packs < 1 or int(total_rupees) <= 0:
        _plog.warning(f"WhatsApp Pay pack refused: units={white_units!r} creative={creative_packs!r} "
                      f"total={total_rupees!r} site={site}")
        return False
    return await _send_native_order(
        db, recipient_id, int(total_rupees), body_text, reply_to_message_id, site,
        purpose=PURPOSE_SKU_PACK, white_units=white_units, creative_packs=creative_packs,
    )


async def _send_native_order(
    db: Session,
    recipient_id: str,
    amount_rupees: int,
    body_text: str,
    reply_to_message_id: Optional[str],
    site: str,
    purpose: Optional[str] = None,
    white_units: int = 0,
    creative_packs: int = 0,
) -> bool:
    """Record one order and send its order_details message. ``purpose`` None = wallet recharge (the amount is raised
    to the recharge minimum), "sku_pack" = the exact cart total. Never raises."""
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
        amount = int(amount_rupees) if purpose else max(int(amount_rupees), pricing.min_recharge())
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
            purpose=purpose,
            white_units=white_units if purpose else None,
            creative_packs=creative_packs if purpose else None,
        )
        db.add(order)
        db.commit()

        pack_items = pack_order_items(white_units, creative_packs, amount) if purpose else None
        payload = build_order_details_payload(
            recipient_id, customer.whatsapp_id, order.reference_id, amount, body_text, expires_at,
            items=pack_items, expiry_text="This order has expired." if purpose else "This recharge order has expired.",
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


async def send_order_status(recipient_id: str, reference_id: str, status: str, description: str = "",
                            body_text: str = "Wallet recharge update") -> bool:
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
            "body": {"text": body_text},
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
    configuration_name, reference_id = order.configuration_name, order.reference_id
    # Release the database connection before the slow Meta call (the order reloads on next use).
    db.commit()
    payment = await lookup_payment(configuration_name, reference_id)
    if payment is None:
        return "lookup_failed"

    now = datetime.now(timezone.utc)
    if payment.get("status") != "captured":
        # Only a live order moves between sent / pending / expired. An order that already ended as
        # failed / dispatch_failed / expired keeps that status: it is re-checked only in case the
        # customer paid anyway, and a not-yet-paid answer must not rewrite its history.
        if order.status in ("sent", "pending", "created"):
            expires_at = order.expires_at
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            past_expiry = bool(expires_at and now > expires_at)
            if past_expiry:
                order.status = "expired"
            elif order.status != "created":
                order.status = "pending"          # a "created" order stays created: Meta may never have shown it
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
            **({"purpose": order.purpose, "units": int(order.white_units or 0),
                "creative_packs": int(order.creative_packs or 0)} if _is_pack(order) else {}),
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
        if _is_pack(order):
            _grant_pack_order(db, order, pay_id)
        elif credit_wallet(
            db, order.whatsapp_id, order.amount_rupees, commit=False,
            kind=KIND_CREDIT_WHATSAPP_PAY, ref=pay_id,
        ) != 1:
            raise RuntimeError("customer row not updated")
        order.status, order.credited = "captured", True
        # If the Razorpay webhook parked this payment for review earlier (the order was in a state that
        # would not be credited), close those rows in the same transaction so nobody credits it again by hand.
        mark_pending_credited(db, pay_id, order.whatsapp_id)
        db.query(AuditLog).filter(
            AuditLog.action == "razorpay_payment_unmatched", AuditLog.resource_id == pay_id, AuditLog.status == "pending"
        ).update({AuditLog.status: "resolved"}, synchronize_session=False)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"WhatsApp Pay credit failed for {order.reference_id}: {e}")
        return "credit_failed"

    what = (f"SKU pack ({int(order.white_units or 0)} white + {int(order.creative_packs or 0)} catalog) for "
            if _is_pack(order) else "")
    logger.info(
        f"WhatsApp Pay credited {what}₹{order.amount_rupees} to {mask_phone(order.whatsapp_id)} "
        f"(ref={order.reference_id}, payment={pay_id})"
    )
    await _send_receipt(db, order)
    return "credited"


def _is_pack(order: WhatsAppPaymentOrder) -> bool:
    from app.services.sku_packs import PURPOSE_SKU_PACK

    return order.purpose == PURPOSE_SKU_PACK


def _grant_pack_order(db: Session, order: WhatsAppPaymentOrder, pay_id: str) -> None:
    """Grant the SKUs a captured pack order bought, in the caller's transaction, keyed on the Razorpay payment id
    (as a pack link payment is, so refunds claw it back the same way). Raises when nothing can be granted."""
    from app.models.sku_credit import SKU_CREATIVE
    from app.services import sku_packs

    customer = find_customer_by_phone(db, order.whatsapp_id)
    if customer is None:
        raise RuntimeError("no customer row for this pack order")
    white, creative = int(order.white_units or 0), int(order.creative_packs or 0)
    if white + creative < 1:
        raise RuntimeError("pack order without SKUs")
    if white:
        sku_packs.grant_pack_credits(db, customer, white, pay_id)
    if creative:
        sku_packs.grant_pack_credits(db, customer, creative, pay_id, sku=SKU_CREATIVE)


# Orders that ended without a credit (Meta rejected the message, the payment failed, or it expired) are still
# re-checked for this long, in case the customer paid anyway and the webhook was lost.
RECHECK_WINDOW = timedelta(hours=24)
# "created" is included: an order whose send crashed before its status was updated may still have reached the customer.
_LIVE_STATUSES = ("created", "sent", "pending")
_ENDED_STATUSES = ("dispatch_failed", "failed", "expired")
# A check that finds nothing schedules the next one later and later (2 min, 4, 8 ... up to 1 hour), so old
# orders stop crowding out new ones and Meta is not asked about the same order every few minutes.
RECHECK_BACKOFF_BASE_SECONDS = 120
RECHECK_BACKOFF_CAP_SECONDS = 3600
_FINISHED_OUTCOMES = frozenset({"credited", "already_credited", "amount_mismatch", "captured"})


def next_check_delay(attempts: int) -> timedelta:
    """Wait before the next look at an order that has been checked ``attempts`` times without result."""
    return timedelta(seconds=min(RECHECK_BACKOFF_BASE_SECONDS * (2 ** min(max(attempts, 0), 12)), RECHECK_BACKOFF_CAP_SECONDS))


async def _reconcile_one(db: Session, order_id: str, results: Dict[str, int]) -> None:
    """Check one order and schedule its next check. Never raises."""
    try:
        order = db.get(WhatsAppPaymentOrder, order_id)
        if order is None or order.credited:
            return
        outcome = await reconcile_order(db, order)
        results[outcome] = results.get(outcome, 0) + 1
        order = db.get(WhatsAppPaymentOrder, order_id)
        if order is not None and not order.credited and outcome not in _FINISHED_OUTCOMES:
            order.check_attempts = int(order.check_attempts or 0) + 1
            order.next_check_at = datetime.now(timezone.utc) + next_check_delay(order.check_attempts)
            db.commit()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"WhatsApp Pay reconcile of order {order_id} failed: {e}")


async def reconcile_pending_orders(
    db: Session,
    older_than_seconds: int = 120,
    limit: int = 50,
    session_factory: Optional[Callable[[], Session]] = None,
) -> Dict[str, int]:
    """Sweep orders whose payment webhook may have been missed.

    Picks live (sent / pending) orders and, for ``RECHECK_WINDOW``, ended ones (dispatch_failed / failed /
    expired) that are due (``next_check_at`` empty or passed), never-checked orders first. With a
    ``session_factory`` every order is handled in its own short session, so one slow Meta answer never holds a
    database connection across the other orders.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=older_than_seconds)
    due = or_(WhatsAppPaymentOrder.next_check_at.is_(None), WhatsAppPaymentOrder.next_check_at <= now)
    ids = [
        row[0]
        for row in db.query(WhatsAppPaymentOrder.id)
        .filter(
            WhatsAppPaymentOrder.credited.is_(False),
            WhatsAppPaymentOrder.created_at <= cutoff,
            due,
            or_(
                WhatsAppPaymentOrder.status.in_(_LIVE_STATUSES),
                and_(
                    WhatsAppPaymentOrder.status.in_(_ENDED_STATUSES),
                    WhatsAppPaymentOrder.created_at >= now - RECHECK_WINDOW,
                ),
            ),
        )
        .order_by(WhatsAppPaymentOrder.next_check_at.asc().nulls_first(), WhatsAppPaymentOrder.created_at)
        .limit(limit)
        .all()
    ]
    db.commit()          # the listing is done: free the connection before the slow lookups
    results: Dict[str, int] = {}
    for order_id in ids:
        if session_factory is not None:
            with session_factory() as order_db:
                await _reconcile_one(order_db, order_id, results)
        else:
            await _reconcile_one(db, order_id, results)
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
            from app.services.scheduler_lease import holds_lease

            if not await holds_lease("whatsapp_pay_reconcile", interval * 2 + 30):
                continue                          # another process owns this job right now (ARC-2)
            with SessionLocal() as db:
                results = await reconcile_pending_orders(db, session_factory=SessionLocal)
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

        customer = find_customer_by_phone(db, order.whatsapp_id)
        if _is_pack(order) and customer is not None:
            from app.api.routes.payment_routes import _pack_receipt

            await send_order_status(order.whatsapp_id, order.reference_id, "completed", "Pack purchased",
                                    body_text="Your order update")
            await send_whatsapp_text(
                recipient_id=order.whatsapp_id,
                message_text=_pack_receipt(db, customer, int(order.white_units or 0), int(order.creative_packs or 0)),
            )
        else:
            await send_order_status(order.whatsapp_id, order.reference_id, "completed", "Wallet recharged")
            await send_whatsapp_text(
                recipient_id=order.whatsapp_id,
                message_text=PAYMENT_TIPS_MESSAGE.format(
                    paid=f"{order.amount_rupees:,}",
                    balance=f"{get_balance(db, order.whatsapp_id):,}",
                ),
            )
        job = dispatch_payment_invoice(
            recipient_id=order.whatsapp_id,
            payment_id=order.pg_payment_id or order.reference_id,
            amount=order.amount_rupees,
            customer_name=getattr(customer, "full_name", None) or "Valued Customer",
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
        if settings.ERPNEXT_INVOICE_ENABLED:
            queued = False
            if settings.OUTBOX_ENABLED:
                from app.services import outbox

                outcome = await run_io(
                    outbox.enqueue_status,
                    "payment_invoice",
                    {
                        "recipient_id": order.whatsapp_id,
                        "payment_id": order.pg_payment_id or order.reference_id,
                        "amount": order.amount_rupees,
                        "customer_name": getattr(customer, "full_name", None) or "Valued Customer",
                        "customer_snapshot": {
                            "full_name": getattr(customer, "full_name", None),
                            "business_name": getattr(customer, "business_name", None),
                            "gst_number": getattr(customer, "gst_number", None),
                "is_gst_verified": bool(getattr(customer, "is_gst_verified", False)),
                            "address": getattr(customer, "address", None),
                        },
                    },
                    f"inv:{order.pg_payment_id or order.reference_id}",
                )
                queued = outcome in (outbox.QUEUED, outbox.DUPLICATE)   # a duplicate is already queued or sent
            if queued:                       # the durable job replaces the in-memory one
                job.close()
                outbox.ensure_default_handlers()
                task = asyncio.get_running_loop().create_task(outbox.drain_once())
            else:
                task = asyncio.get_running_loop().create_task(job)
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_TASKS.discard)
        else:
            await job  # local PDF only: same inline behaviour as before
    except Exception as e:
        logger.error(f"WhatsApp Pay receipt dispatch failed for {order.reference_id}: {e}")
    if _is_pack(order):
        await _pack_followups(db, order)


async def _pack_followups(db: Session, order: WhatsAppPaymentOrder) -> None:
    """After a granted pack: the Usage Log sheet row and Drive share, then the photos sent before paying. Never
    raises: the SKUs are already granted."""
    try:
        from app.services import sku_packs

        customer = find_customer_by_phone(db, order.whatsapp_id)
        if customer is None:
            return
        await run_io(sku_packs.queue_followups, customer.id)
        if int(order.white_units or 0):
            from app.api.routes.meta_webhook import start_held_photos

            await start_held_photos(db, order.whatsapp_id)
    except Exception as e:  # noqa: BLE001
        logger.error(f"WhatsApp Pay pack follow-ups failed for {order.reference_id}: {type(e).__name__}: {e}")


async def _send_fallback_link_once(db: Session, order: WhatsAppPaymentOrder) -> None:
    """After a failed in-chat payment, offer the Razorpay link once.

    Strict mode: no link; an in-chat "reply recharge N to retry" text instead.
    """
    if order.fallback_sent:
        return
    if _is_pack(order):
        await _pack_payment_failed_once(db, order)
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


async def _pack_payment_failed_once(db: Session, order: WhatsAppPaymentOrder) -> None:
    """A failed in-chat pack payment. Strict mode: an in-chat "reply packs to try again" text (no URL); otherwise
    one pack payment link for the same cart. Sent once per order."""
    try:
        if settings.WHATSAPP_PAY_STRICT:
            sent = await send_whatsapp_text(order.whatsapp_id, PACK_ORDER_FAILED_STRICT_MESSAGE)
        else:
            from app.services import sku_messages
            from app.services.meta_whatsapp_service import send_whatsapp_cta_url_button
            from app.services.razorpay_service import create_pack_payment_link

            customer = find_customer_by_phone(db, order.whatsapp_id)
            url = await create_pack_payment_link(
                customer_phone=order.whatsapp_id,
                customer_name=getattr(customer, "full_name", None) or "Customer",
                units=int(order.white_units or 0),
                creative_packs=int(order.creative_packs or 0),
            )
            sent = bool(url) and await send_whatsapp_cta_url_button(
                recipient_id=order.whatsapp_id,
                body_text=ORDER_FAILED_MESSAGE,
                button_label=sku_messages.PAY_BUTTON,
                url=url,
            )
        if sent:
            order.fallback_sent = True
            db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"WhatsApp Pay pack retry notice failed for {order.reference_id}: {e}")
