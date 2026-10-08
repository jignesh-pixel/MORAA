"""SKU pack credits (Phase 8): the only code that writes ``customer_sku_credits``.

A paid pack adds credits, each white-background order uses one, a failed order gives its credit back once, unused
credits expire SKU_CREDIT_VALIDITY_DAYS after the newest purchase (rolling: a new pack extends every unused credit),
and a refunded or charged-back pack payment takes its credits back. Credits never touch ``wallet_balance``.

Every change first locks the customer row, then reads the balance (SUM of quantity) and writes ONE row with its
``balance_after``, so two changes for one customer run one after another. The database refuses a negative balance
and a second row for the same ``(reference_id, action)``: a payment grants once, an order consumes once and is
refunded once.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.customer import Customer
from app.models.sku_credit import (
    ACTION_CLAWBACK,
    ACTION_CONSUME,
    ACTION_EXPIRE,
    ACTION_PURCHASE,
    ACTION_REFUND,
    SKU_CREATIVE,
    SKU_WHITE_BG,
    CustomerSkuCredit,
)
from app.services import pricing
from app.services.wallet_service import (
    AUDIT_ACTION_CLAWBACK,
    AUDIT_RESOURCE_TYPE_CLAWBACK,
    CLAWBACK_OUTCOME_APPLIED,
    CLAWBACK_OUTCOME_DUPLICATE,
)
from app.utils.logger import logger

# Razorpay notes.purpose (and razorpay_payment_links.purpose) of a pack payment; NULL / absent = wallet recharge.
PURPOSE_SKU_PACK = "sku_pack"
# Largest pack one payment link may carry (links outside 1..this are refused, payments outside it are parked).
MAX_PACK_UNITS = 10000
# Must equal payment_routes.AUDIT_ACTION_PAYMENT_CAPTURED: its details hold what the payer paid ("amount_paid").
PAYMENT_CAPTURED_ACTION = "razorpay_payment_captured"

IST = timezone(timedelta(hours=5, minutes=30))


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """In UTC. SQLite hands back times without a timezone; they are UTC."""
    if value is None:
        return None
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def ist_date(value: datetime) -> str:
    """"6 Jan 2027": a customer-facing date in India time."""
    local = _aware(value).astimezone(IST)
    return f"{local.day} {local:%b %Y}"


def _lock_customer(db: Session, customer_id: str) -> Optional[Customer]:
    """Lock the customer row until the transaction ends: credit changes for one customer run one at a time."""
    return db.query(Customer).filter(Customer.id == customer_id).with_for_update().first()


def balance(db: Session, customer_id: str, sku: str = SKU_WHITE_BG) -> int:
    """The customer's credits of one SKU: SUM(quantity) of their ledger rows for it (0 when none)."""
    value = (
        db.query(func.coalesce(func.sum(CustomerSkuCredit.quantity), 0))
        .filter(CustomerSkuCredit.customer_id == customer_id, CustomerSkuCredit.sku == sku)
        .scalar()
    )
    return max(int(value or 0), 0)


def valid_until(db: Session, customer_id: str, sku: str = SKU_WHITE_BG) -> Optional[datetime]:
    """When the customer's unused credits of one SKU expire: the newest purchase's expiry (None: never bought)."""
    value = (
        db.query(func.max(CustomerSkuCredit.expires_at))
        .filter(CustomerSkuCredit.customer_id == customer_id, CustomerSkuCredit.action == ACTION_PURCHASE,
                CustomerSkuCredit.sku == sku)
        .scalar()
    )
    return _aware(value)


def _add_row(
    db: Session,
    customer_id: str,
    action: str,
    quantity: int,
    reference_id: str,
    current: int,
    expires_at: Optional[datetime] = None,
    sku: str = SKU_WHITE_BG,
) -> int:
    """Write one ledger row on top of ``current`` (read under the customer lock). Returns the new balance.

    Flushed at once, so a duplicate ``(reference_id, action)`` or a negative balance fails here (IntegrityError)."""
    after = current + quantity
    db.add(
        CustomerSkuCredit(
            customer_id=customer_id,
            sku=sku,
            action=action,
            quantity=quantity,
            balance_after=after,
            reference_id=str(reference_id),
            expires_at=expires_at,
        )
    )
    db.flush()
    return after


def purchase_reference(payment_id: str, sku: str = SKU_WHITE_BG) -> str:
    """Ledger reference of a purchase: the payment id for white-background SKUs, "<payment id>:<sku>" for other
    SKUs, so one cart payment can grant several SKUs, each once."""
    return str(payment_id) if sku == SKU_WHITE_BG else f"{payment_id}:{sku}"


def grant_pack_credits(
    db: Session, customer: Customer, units: int, payment_id: str, now: Optional[datetime] = None,
    sku: str = SKU_WHITE_BG,
) -> int:
    """Add a paid pack's credits in the CALLER's transaction (no commit). Returns the new balance.

    Every unused credit is then valid for SKU_CREDIT_VALIDITY_DAYS from ``now``. A second grant for the same payment
    raises IntegrityError (the caller rolls back); ``units`` below 1 or an unknown customer raises ValueError."""
    units = int(units)
    if units < 1:
        raise ValueError(f"A pack must add at least one credit, got {units}")
    if _lock_customer(db, customer.id) is None:
        raise ValueError("Unknown customer")
    now = now or datetime.now(timezone.utc)
    expires_at = now + timedelta(days=max(int(settings.SKU_CREDIT_VALIDITY_DAYS), 1))
    after = _add_row(db, customer.id, ACTION_PURCHASE, units, purchase_reference(payment_id, sku),
                     balance(db, customer.id, sku), expires_at, sku)
    logger.info(f"SKU credits granted: customer_id={customer.id} sku={sku} units={units} payment_id={payment_id} "
                f"balance={after}")
    return after


def consume_credit(db: Session, customer: Customer, ingestion_id: str, sku: str = SKU_WHITE_BG) -> bool:
    """Use one credit for an order in the CALLER's transaction (no commit), so the order status and the credit are
    committed together. False (nothing written) when no credit is left or the credits have expired. A second
    consume for the same order raises IntegrityError."""
    _lock_customer(db, customer.id)
    current = balance(db, customer.id, sku)
    until = valid_until(db, customer.id, sku)
    if current < 1 or until is None or until < datetime.now(timezone.utc):
        return False
    _add_row(db, customer.id, ACTION_CONSUME, -1, ingestion_id, current, sku=sku)
    return True


def refund_credit(db: Session, ingestion_id: str) -> bool:
    """Give back the credit a failed order used. Commits. Never raises.

    Only an order that consumed a credit and was not refunded yet; True only if THIS call gave the credit back.
    Callers commit their own changes before calling."""
    try:
        consumed = (
            db.query(CustomerSkuCredit.customer_id, CustomerSkuCredit.sku)
            .filter(CustomerSkuCredit.action == ACTION_CONSUME, CustomerSkuCredit.reference_id == ingestion_id)
            .first()
        )
        if consumed is None:
            return False
        customer_id, sku = consumed[0], consumed[1]
        _lock_customer(db, customer_id)
        already = (
            db.query(CustomerSkuCredit.id)
            .filter(CustomerSkuCredit.action == ACTION_REFUND, CustomerSkuCredit.reference_id == ingestion_id)
            .first()
        )
        if already is not None:
            db.rollback()
            return False
        after = _add_row(db, customer_id, ACTION_REFUND, 1, ingestion_id, balance(db, customer_id, sku), sku=sku)
        db.commit()
    except IntegrityError:
        db.rollback()                      # a concurrent refund of the same order won
        return False
    except Exception as e:  # noqa: BLE001 -- a failed refund must never break the failure path that calls it
        db.rollback()
        logger.error(f"SKU credit refund failed: ingestion_id={ingestion_id} error={type(e).__name__}: {e}")
        return False
    logger.info(f"SKU credit refunded: customer_id={customer_id} ingestion_id={ingestion_id} balance={after}")
    return True


def _expire_customer(db: Session, customer_id: str, now: datetime, sku: str = SKU_WHITE_BG) -> bool:
    """Expire one customer's credits of one SKU when past their validity. Commits. True when a row was written."""
    _lock_customer(db, customer_id)
    current = balance(db, customer_id, sku)
    until = valid_until(db, customer_id, sku)
    if current < 1 or until is None or until >= now:
        db.rollback()
        return False
    reference = f"exp:{customer_id}:{until:%Y%m%d}" + ("" if sku == SKU_WHITE_BG else ":c")
    expired_before = (
        db.query(CustomerSkuCredit.reference_id)
        .filter(CustomerSkuCredit.customer_id == customer_id, CustomerSkuCredit.action == ACTION_EXPIRE,
                CustomerSkuCredit.sku == sku)
        .all()
    )
    if any(row[0] == reference for row in expired_before):
        # A credit given back after these credits had already expired: expire it under its own reference.
        reference = f"{reference}:{len(expired_before)}"
    _add_row(db, customer_id, ACTION_EXPIRE, -current, reference, current, sku=sku)
    db.commit()
    logger.info(f"SKU credits expired: customer_id={customer_id} credits={current} valid_until={until.isoformat()}")
    return True


def expire_due_credits(now: Optional[datetime] = None) -> int:
    """Expire the unused credits of every customer whose validity has passed (own session). Returns how many
    customers. Never raises."""
    from app.database import SessionLocal

    now = now or datetime.now(timezone.utc)
    expired = 0
    try:
        with SessionLocal() as db:
            holders = [
                (row[0], row[1])
                for row in db.query(CustomerSkuCredit.customer_id, CustomerSkuCredit.sku)
                .group_by(CustomerSkuCredit.customer_id, CustomerSkuCredit.sku)
                .having(func.sum(CustomerSkuCredit.quantity) > 0)
                .all()
            ]
            db.rollback()
            for customer_id, sku in holders:
                try:
                    if _expire_customer(db, customer_id, now, sku):
                        expired += 1
                except Exception as e:  # noqa: BLE001 -- one customer's failure must not stop the others
                    db.rollback()
                    logger.error(f"SKU credit expiry failed: customer_id={customer_id} error={type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"SKU credit expiry sweep failed: {type(e).__name__}: {e}")
    return expired


async def run_expiry_forever() -> None:
    """Background loop started from the application lifespan: one expiry pass a day, in one process only."""
    from app.services.scheduler_lease import holds_lease
    from app.utils.executors import run_io

    interval = 24 * 3600
    while True:
        await asyncio.sleep(3600)                       # wake hourly; the lease keeps it to one pass a day
        try:
            if not await holds_lease("sku_expiry", interval):
                continue
            count = await run_io(expire_due_credits)
            if count:
                logger.bind(category="system").info(f"SKU credit expiry: {count} customer(s) expired")
            await asyncio.sleep(interval - 3600)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"SKU credit expiry pass failed: {type(e).__name__}: {e}")


def claw_back_pack(
    db: Session,
    *,
    payment_id: str,
    entity_id: str,
    amount_rupees: int,
    kind: str,
) -> Optional[dict]:
    """Take back the credits of a pack payment Razorpay returned to the payer (refund, or a chargeback that was lost).

    None when ``payment_id`` is not a pack purchase (the caller then tries the wallet). Same policy as
    ``wallet_service.claw_back_payment``: take what is left, never below zero, and flag the rest on the audit row
    (status ``pending``). Credits to take = the refunded share of the pack, rounded up, never more than the pack
    minus earlier claw-backs. Each refund/dispute (``entity_id``) is applied once: the customer row is locked, then
    the audit row is looked for; audit row and ledger row are committed together.

    Returns ``{"outcome": applied|duplicate, "taken": int, "shortfall": int}`` (credits, not rupees).
    """
    from app.models.audit_log import AuditLog

    purchases = [
        (row[0], row[1], int(row[2]))
        for row in db.query(CustomerSkuCredit.customer_id, CustomerSkuCredit.sku, CustomerSkuCredit.quantity)
        .filter(
            CustomerSkuCredit.action == ACTION_PURCHASE,
            CustomerSkuCredit.reference_id.in_([purchase_reference(payment_id, s) for s in (SKU_WHITE_BG, SKU_CREATIVE)]),
        )
        .all()
    ]
    if not purchases:
        return None
    customer_id = purchases[0][0]
    amount = max(int(amount_rupees), 0)

    try:
        _lock_customer(db, customer_id)
        already = (
            db.query(AuditLog.id)
            .filter(AuditLog.action == AUDIT_ACTION_CLAWBACK, AuditLog.resource_id == entity_id)
            .first()
        )
        if already is not None:
            db.rollback()
            return {"outcome": CLAWBACK_OUTCOME_DUPLICATE, "taken": 0, "shortfall": 0}

        captured = (
            db.query(AuditLog.details)
            .filter(AuditLog.action == PAYMENT_CAPTURED_ACTION, AuditLog.resource_id == payment_id)
            .first()
        )
        try:
            paid = int(json.loads((captured[0] if captured else None) or "{}").get("amount_paid") or 0)
        except (TypeError, ValueError, AttributeError):
            paid = 0
        if paid <= 0:   # no record of what was paid: value it at today's prices
            paid = sum(pricing.pack_total(units, db) if sku == SKU_WHITE_BG else units * pricing.catalog_pack_price()
                       for _cid, sku, units in purchases)
        earlier_refunds = [
            row[0]
            for row in db.query(AuditLog.resource_id)
            .filter(
                AuditLog.action == AUDIT_ACTION_CLAWBACK,
                AuditLog.details.contains(f'"payment_id": "{payment_id}"', autoescape=True),
            )
            .all()
        ]
        # The refunded share of the payment applies to every SKU it bought, each rounded up, never more than what
        # is left of that purchase and never below a zero balance.
        wanted = take = shortfall = over = 0
        units_purchased = 0
        for _cid, sku, units in purchases:
            units_purchased += units
            references = [r if sku == SKU_WHITE_BG else f"{r}:{sku}" for r in earlier_refunds]
            taken_before = -int(
                db.query(func.coalesce(func.sum(CustomerSkuCredit.quantity), 0))
                .filter(CustomerSkuCredit.action == ACTION_CLAWBACK, CustomerSkuCredit.sku == sku,
                        CustomerSkuCredit.reference_id.in_(references or ["-"]))
                .scalar()
            )
            remaining = max(units - taken_before, 0)
            asked = -(-amount * units // paid)
            sku_wanted = min(asked, remaining)
            current = balance(db, customer_id, sku)
            sku_take = min(sku_wanted, current)
            if sku_take > 0:
                reference = entity_id if sku == SKU_WHITE_BG else f"{entity_id}:{sku}"
                _add_row(db, customer_id, ACTION_CLAWBACK, -sku_take, reference, current, sku=sku)
            wanted, take = wanted + sku_wanted, take + sku_take
            shortfall += sku_wanted - sku_take
            over += max(asked - remaining, 0)

        db.add(
            AuditLog(
                user_id=None,
                action=AUDIT_ACTION_CLAWBACK,
                resource_id=entity_id,
                resource_type=AUDIT_RESOURCE_TYPE_CLAWBACK,
                # "pending" = part of the credits could not be taken back; a person must review it.
                status="pending" if shortfall or over else "success",
                details=json.dumps(
                    {
                        "payment_id": payment_id,
                        "kind": kind,
                        "purpose": PURPOSE_SKU_PACK,
                        "requested": amount,
                        "amount_paid": paid,
                        "units_purchased": units_purchased,
                        "credits_wanted": wanted,
                        "taken": take,
                        "shortfall": shortfall,
                        "over_original_payment": over,
                    }
                ),
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        # Only a repeat of this same refund/dispute is a duplicate; any other constraint failure must surface.
        if db.query(AuditLog.id).filter(
            AuditLog.action == AUDIT_ACTION_CLAWBACK, AuditLog.resource_id == entity_id
        ).first() is None:
            raise
        return {"outcome": CLAWBACK_OUTCOME_DUPLICATE, "taken": 0, "shortfall": 0}
    except Exception:
        db.rollback()
        raise

    if shortfall:
        logger.error(
            f"ALERT pack payment {payment_id}: {kind} of ₹{amount} means {wanted} SKU credit(s) back but only {take} "
            f"were left; {shortfall} could not be taken back and need manual review (audit row {entity_id})."
        )
    return {"outcome": CLAWBACK_OUTCOME_APPLIED, "taken": take, "shortfall": shortfall}


def queue_followups(customer_id: str) -> None:
    """After a committed grant: refresh the customer's Usage Logs sheet and share their Drive folder (outbox jobs
    that do nothing unless Drive delivery is on). Blocking. Never raises."""
    try:
        from app.services import usage_log

        usage_log.queue_sync(customer_id)
    except Exception as e:  # noqa: BLE001 -- a follow-up must never undo or block a grant
        logger.warning(f"Usage log sync not queued for customer_id={customer_id}: {type(e).__name__}: {e}")
    try:
        from app.services import drive_layout

        drive_layout.queue_share(customer_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Drive folder share not queued for customer_id={customer_id}: {type(e).__name__}: {e}")


def customer_for_order(db: Session, ingestion_id: str) -> Optional[str]:
    """The customer whose credit paid for this order (None when no credit did)."""
    row = (
        db.query(CustomerSkuCredit.customer_id)
        .filter(CustomerSkuCredit.action == ACTION_CONSUME, CustomerSkuCredit.reference_id == ingestion_id)
        .first()
    )
    return row[0] if row else None


def credit_returned(db: Session, ingestion_id: str) -> bool:
    """True when the credit this order used has been given back."""
    return (
        db.query(CustomerSkuCredit.id)
        .filter(CustomerSkuCredit.action == ACTION_REFUND, CustomerSkuCredit.reference_id == ingestion_id)
        .first()
        is not None
    )
