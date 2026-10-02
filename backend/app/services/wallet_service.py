"""Wallet gating, batch funding and paid-generation accounting.

Implements the paid side of the Moraa Studio journey:

    Scenario 2 — a registered customer with less than one image's worth of
                 balance has their image stored but HELD
                 (``status='pending_payment'``) and receives the balance
                 notice with a recharge CTA. No generation is dispatched.
    Scenario 3 — an N-image batch is funded in arrival order: the first M
                 images (what the balance covers) go through the normal
                 style-selection flow, the remaining N-M are held for payment,
                 and the customer receives the explicit partial breakdown.
    Scenario 4 — every FUNDED image is quality-checked (see
                 ``image_prevalidation_service``) before any credits are spent.

Money rules enforced here:
    * balances are whole Indian Rupees in an INTEGER column — no floats
    * a generation is charged ONCE, at dispatch time, with an atomic guarded
      UPDATE (``wallet_balance >= price``) so concurrent taps can never
      overspend or drive the balance negative
    * a charge is refunded when the generation it paid for fails
    * a rejected or held image is never charged

Every WhatsApp message is sent through the existing
``app.services.meta_whatsapp_service`` helpers — no HTTP logic lives here.
"""

import json
from typing import Optional, Tuple

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.customer import Customer
from app.models.wallet_transaction import (
    KIND_CREDIT_PAYMENT,
    KIND_CREDIT_WHATSAPP_PAY,
    KIND_DEBIT_DISPUTE,
    KIND_DEBIT_ORDER,
    KIND_DEBIT_REFUND,
    KIND_REFUND_ORDER,
    WalletTransaction,
)
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.repositories.base import BaseRepository
from app.utils.logger import logger, mask_phone
from app.utils.phone import INDIA_CODE, normalize_phone
# ─── Ingestion statuses owned by the wallet gate ─────────────────────────

STATUS_PENDING_PAYMENT = "pending_payment"
STATUS_PENDING_REGISTRATION = "pending_registration"
STATUS_REJECTED = "rejected"

# Statuses that may never dispatch a paid generation.
BLOCKED_GENERATION_STATUSES = frozenset(
    {STATUS_PENDING_PAYMENT, STATUS_PENDING_REGISTRATION, STATUS_REJECTED}
)


# ─── Small helpers ───────────────────────────────────────────────────────


def price_per_image() -> int:
    """Configured price of one generated image, in whole Rupees (>= 1)."""
    return max(int(settings.WALLET_IMAGE_PRICE_RUPEES), 1)


def format_rupees(amount: int) -> str:
    """Format a rupee amount for customer-facing text (e.g. '₹1,000')."""
    return f"₹{max(int(amount), 0):,}"


def _customer_repo(db: Session) -> BaseRepository:
    return BaseRepository(Customer, db)


def get_customer(db: Session, whatsapp_id: str) -> Optional[Customer]:
    """Look up a customer by WhatsApp ID (parameterised ORM query)."""
    if not whatsapp_id:
        return None
    try:
        return _customer_repo(db).find_first(whatsapp_id=whatsapp_id)
    except Exception as e:
        db.rollback()
        logger.error(f"Wallet: customer lookup failed: {e}")
        return None


def get_balance(db: Session, whatsapp_id: str) -> int:
    """Current wallet balance in Rupees (0 when the customer is unknown).

    Single source of truth for every balance shown to a customer. A
    column-only query always goes to the database, so it never returns a
    Customer object cached earlier in this session (which could be seconds
    old while a payment or a charge commits in another request).
    """
    if not whatsapp_id:
        return 0
    try:
        value = (
            db.query(Customer.wallet_balance)
            .filter(Customer.whatsapp_id == whatsapp_id)
            .scalar()
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Wallet: balance lookup failed: {e}")
        return 0
    return max(int(value or 0), 0)


def find_customer_by_phone(db: Session, phone: str) -> Optional[Customer]:
    """Exact, index-backed customer lookup across the stored number formats.

    WhatsApp senders arrive as ``919876543210``; Razorpay contacts may arrive as
    ``+919876543210`` or ``9876543210`` (no country code = Indian). A number that
    carries a country code matches only that exact number, never another country's
    number with the same last 10 digits (MON-5).
    """
    raw = (phone or "").strip()
    digits = normalize_phone(raw)
    if not digits:
        return None
    candidates = {raw, digits, "+" + digits}
    if digits.startswith(INDIA_CODE) and len(digits) == 12:
        # Older Indian rows were stored without the country code. Only an Indian number may match them.
        candidates.add(digits[2:])
    try:
        rows = db.query(Customer).filter(Customer.whatsapp_id.in_(candidates)).all()
    except Exception as e:
        db.rollback()
        logger.error(f"Wallet: phone lookup failed: {e}")
        return None
    for preferred in (digits, raw):
        for row in rows:
            if row.whatsapp_id == preferred:
                return row
    return rows[0] if rows else None


def record_ledger(
    db: Session,
    *,
    customer_id: str,
    kind: str,
    amount: int,
    ingestion_id: Optional[str] = None,
    ref: Optional[str] = None,
) -> None:
    """Add the ledger row for a balance change that was JUST applied in this transaction.

    ``balance_after`` is read inside the same transaction, after the UPDATE that took the row lock,
    so it is exactly the balance this change produced. The caller commits (or rolls back) the
    balance change and this row together; a duplicate ``(ingestion_id, kind)`` or ``(kind, ref)``
    fails at flush with IntegrityError, which is how the database refuses a second debit,
    refund or payment credit.
    """
    balance_after = db.query(Customer.wallet_balance).filter(Customer.id == customer_id).scalar()
    db.add(
        WalletTransaction(
            customer_id=customer_id,
            kind=kind,
            amount=int(amount),
            balance_after=int(balance_after or 0),
            ingestion_id=ingestion_id,
            ref=ref,
        )
    )
    db.flush()


def credit_wallet(
    db: Session,
    whatsapp_id: str,
    amount: int,
    commit: bool = True,
    *,
    kind: str = KIND_CREDIT_PAYMENT,
    ingestion_id: Optional[str] = None,
    ref: Optional[str] = None,
) -> int:
    """Add funds (payment captured or refund). Returns the new balance.

    Every credit also writes its ledger row in the same transaction (see ``record_ledger``).
    """
    amount = max(int(amount), 0)
    if amount == 0 or not whatsapp_id:
        # commit=False callers read the return value as "rows updated".
        return 0 if not commit else get_balance(db, whatsapp_id)

    try:
        customer_id = db.query(Customer.id).filter(Customer.whatsapp_id == whatsapp_id).scalar()
        updated = (
            db.query(Customer)
            .filter(Customer.whatsapp_id == whatsapp_id)
            .update(
                {Customer.wallet_balance: Customer.wallet_balance + amount},
                synchronize_session=False,
            )
        )
        if updated == 1 and customer_id:
            record_ledger(db, customer_id=customer_id, kind=kind, amount=amount, ingestion_id=ingestion_id, ref=ref)
        if not commit:
            # Caller commits (atomically with its own writes); report rows hit.
            return updated
        db.commit()
        if updated != 1:
            logger.warning(
                f"Wallet credit skipped — unknown customer whatsapp_id={mask_phone(whatsapp_id)}"
            )
            return 0

        balance = get_balance(db, whatsapp_id)
        logger.info(
            f"Wallet credited: whatsapp_id={mask_phone(whatsapp_id)} amount={amount} "
            f"balance={balance}"
        )
        return balance
    except IntegrityError as e:
        db.rollback()
        logger.warning(f"Wallet credit refused by the ledger (duplicate or invalid row): {getattr(e, 'orig', e)}")
        if not commit:
            raise
        return 0        # nothing was credited; never report the old balance as a success
    except Exception as e:
        db.rollback()
        logger.error(f"Wallet credit failed: {e}")
        if not commit:
            raise  # caller's transaction is gone -- let it handle the failure
        return get_balance(db, whatsapp_id)


# ─── Taking back refunded / charged-back payments ────────────────────────

AUDIT_ACTION_CLAWBACK = "razorpay_clawback"
AUDIT_RESOURCE_TYPE_CLAWBACK = "razorpay_payment"
CLAWBACK_OUTCOME_APPLIED = "applied"
CLAWBACK_OUTCOME_DUPLICATE = "duplicate"
CLAWBACK_OUTCOME_NO_PAYMENT = "no_payment"


def claw_back_payment(
    db: Session,
    *,
    payment_id: str,
    entity_id: str,
    amount: int,
    kind: str,
) -> dict:
    """Take back money Razorpay returned to the payer (a refund, or a chargeback that was lost).

    Policy (decided by the owner): take what is in the wallet and flag the rest. The wallet never
    goes below zero. If the customer already spent part of it, only the available part is taken
    and the missing part is written to an audit row with status ``pending`` (needs manual review).

    Safe against repeats and races: every claw-back for a payment first locks the customer row, then
    looks for the audit row of this refund/dispute (``entity_id``). The audit row, the wallet change
    and the ledger row are committed together, so each refund or dispute is applied exactly once.
    Never takes back more than the payment originally credited, across all refunds and disputes.

    Returns ``{"outcome": applied|duplicate|no_payment, "taken": int, "shortfall": int}``.
    """
    from app.models.audit_log import AuditLog

    amount = max(int(amount), 0)
    credit = (
        db.query(WalletTransaction.customer_id, WalletTransaction.amount)
        .filter(
            WalletTransaction.kind.in_((KIND_CREDIT_PAYMENT, KIND_CREDIT_WHATSAPP_PAY)),
            WalletTransaction.ref == payment_id,
        )
        .first()
    )
    if credit is None or amount == 0:
        return {"outcome": CLAWBACK_OUTCOME_NO_PAYMENT, "taken": 0, "shortfall": 0}
    customer_id, credited = credit[0], int(credit[1])

    try:
        # The lock makes concurrent claw-backs for this wallet run one after another.
        balance = int(
            db.query(Customer.wallet_balance).filter(Customer.id == customer_id).with_for_update().scalar() or 0
        )
        already = (
            db.query(AuditLog.id)
            .filter(AuditLog.action == AUDIT_ACTION_CLAWBACK, AuditLog.resource_id == entity_id)
            .first()
        )
        if already is not None:
            db.rollback()
            return {"outcome": CLAWBACK_OUTCOME_DUPLICATE, "taken": 0, "shortfall": 0}

        prefix = f"{payment_id}:"
        taken_before = -int(
            db.query(func.coalesce(func.sum(WalletTransaction.amount), 0))
            .filter(
                WalletTransaction.kind.in_((KIND_DEBIT_REFUND, KIND_DEBIT_DISPUTE)),
                WalletTransaction.ref.startswith(prefix, autoescape=True),
            )
            .scalar()
        )
        remaining = max(credited - taken_before, 0)
        wanted = min(amount, remaining)
        take = min(wanted, balance)
        shortfall = wanted - take

        if take > 0:
            updated = (
                db.query(Customer)
                .filter(Customer.id == customer_id, Customer.wallet_balance >= take)
                .update({Customer.wallet_balance: Customer.wallet_balance - take}, synchronize_session=False)
            )
            if updated != 1:
                raise RuntimeError("wallet row changed while locked")
            record_ledger(db, customer_id=customer_id, kind=kind, amount=-take, ref=f"{prefix}{entity_id}")
        db.add(
            AuditLog(
                user_id=None,
                action=AUDIT_ACTION_CLAWBACK,
                resource_id=entity_id,
                resource_type=AUDIT_RESOURCE_TYPE_CLAWBACK,
                # "pending" = part of the money could not be taken back; a person must review it.
                status="pending" if shortfall or amount > remaining else "success",
                details=json.dumps(
                    {
                        "payment_id": payment_id,
                        "kind": kind,
                        "requested": amount,
                        "taken": take,
                        "shortfall": shortfall,
                        "over_original_payment": max(amount - remaining, 0),
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
            f"ALERT payment {payment_id}: {kind} of ₹{amount} but only ₹{take} was in the wallet; "
            f"₹{shortfall} could not be taken back and needs manual review (audit row {entity_id})."
        )
    return {"outcome": CLAWBACK_OUTCOME_APPLIED, "taken": take, "shortfall": shortfall}


# ─── Paid generation accounting ──────────────────────────────────────────


def is_generation_allowed(ingestion: Optional[WhatsAppIngestion]) -> bool:
    """True when an ingestion may dispatch a paid generation."""
    if ingestion is None:
        return False
    return (ingestion.status or "") not in BLOCKED_GENERATION_STATUSES


def charge_customer_balance(
    db: Session,
    customer: Customer,
    price: int,
    *,
    ingestion_id: Optional[str] = None,
    commit: bool = True,
) -> Tuple[bool, int]:
    """Atomically debit ``price`` from an already-resolved customer's wallet.

    Returns ``(charged, balance_after)``. The WHERE clause carries the
    affordability check, so a concurrent tap can never overspend or push the
    balance below zero. This is the one place a wallet debit happens —
    ``charge_generation`` and the WhatsApp funded-slot gate
    (``app/api/routes/meta_webhook.py``) both call it, so there is a single
    authoritative deduction path.

    The debit and its ledger row are written in one transaction. With ``commit=False`` the caller
    commits them together with its own writes (the order status), so a crash can never leave money
    taken without the order recording it.
    """
    try:
        updated = (
            db.query(Customer)
            .filter(
                Customer.id == customer.id,
                Customer.wallet_balance >= price,
            )
            .update(
                {Customer.wallet_balance: Customer.wallet_balance - price},
                synchronize_session=False,
            )
        )
        if updated == 1:
            record_ledger(
                db, customer_id=customer.id, kind=KIND_DEBIT_ORDER, amount=-price, ingestion_id=ingestion_id,
            )
        if commit:
            db.commit()
    except IntegrityError as e:
        db.rollback()
        logger.error(
            f"Wallet charge refused by the ledger (this order was already debited?): "
            f"ingestion_id={ingestion_id} {getattr(e, 'orig', e)}"
        )
        return False, customer.balance_rupees
    except Exception as e:
        db.rollback()
        logger.error(f"Wallet charge failed: {e}")
        return False, customer.balance_rupees

    db.refresh(customer)

    if updated != 1:
        logger.warning(
            f"Wallet charge declined: whatsapp_id={mask_phone(customer.whatsapp_id)} "
            f"balance={customer.balance_rupees} price={price}"
        )
        return False, customer.balance_rupees

    logger.info(
        f"Wallet charged: whatsapp_id={mask_phone(customer.whatsapp_id)} amount={price} "
        f"balance={customer.balance_rupees}"
    )
    return True, customer.balance_rupees


def charge_generation(
    db: Session,
    ingestion: WhatsAppIngestion,
) -> Tuple[bool, int]:
    """Atomically debit one image's price for a generation dispatch.

    Returns ``(charged, balance_after)``.
    """
    price = price_per_image()
    whatsapp_id = ingestion.external_user_id or ""

    customer = get_customer(db, whatsapp_id)
    if customer is None:
        logger.warning(
            f"Wallet charge skipped — unknown customer whatsapp_id={mask_phone(whatsapp_id)}"
        )
        return False, 0

    return charge_customer_balance(db, customer, price, ingestion_id=ingestion.id)


def refund_generation_charge(
    db: Session,
    whatsapp_id: str,
    amount: int,
    ingestion_id: Optional[str] = None,
) -> bool:
    """Return a charge to the wallet after a failed generation."""
    amount = max(int(amount), 0)
    if amount == 0:
        return False

    try:
        customer_id = db.query(Customer.id).filter(Customer.whatsapp_id == whatsapp_id).scalar()
        updated = (
            db.query(Customer)
            .filter(Customer.whatsapp_id == whatsapp_id)
            .update(
                {Customer.wallet_balance: Customer.wallet_balance + amount},
                synchronize_session=False,
            )
        )
        if updated == 1 and customer_id:
            record_ledger(db, customer_id=customer_id, kind=KIND_REFUND_ORDER, amount=amount, ingestion_id=ingestion_id)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Wallet refund failed: {e}")
        return False

    if updated != 1:
        logger.warning(
            f"Wallet refund skipped — unknown customer whatsapp_id={mask_phone(whatsapp_id)}"
        )
        return False

    logger.info(
        f"Wallet refunded: whatsapp_id={mask_phone(whatsapp_id)} amount={amount}"
    )
    return True
