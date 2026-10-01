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

from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.config import settings
from app.models.customer import Customer
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.repositories.base import BaseRepository
from app.utils.logger import logger

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
    ``+919876543210`` or ``9876543210``. Equality on the known variants replaces
    the old leading-wildcard ``.contains()`` scan with the same matches.
    """
    raw = (phone or "").strip()
    digits = raw.lstrip("+")
    if not digits:
        return None
    last10 = digits[-10:]
    candidates = {raw, digits, "+" + digits, last10, "91" + last10, "+91" + last10}
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


def credit_wallet(db: Session, whatsapp_id: str, amount: int, commit: bool = True) -> int:
    """Add funds (payment captured or refund). Returns the new balance."""
    amount = max(int(amount), 0)
    if amount == 0 or not whatsapp_id:
        # commit=False callers read the return value as "rows updated".
        return 0 if not commit else get_balance(db, whatsapp_id)

    try:
        updated = (
            db.query(Customer)
            .filter(Customer.whatsapp_id == whatsapp_id)
            .update(
                {Customer.wallet_balance: Customer.wallet_balance + amount},
                synchronize_session=False,
            )
        )
        if not commit:
            # Caller commits (atomically with its own writes); report rows hit.
            return updated
        db.commit()
        if updated != 1:
            logger.warning(
                f"Wallet credit skipped — unknown customer whatsapp_id={whatsapp_id}"
            )
            return 0

        balance = get_balance(db, whatsapp_id)
        logger.info(
            f"Wallet credited: whatsapp_id={whatsapp_id} amount={amount} "
            f"balance={balance}"
        )
        return balance
    except Exception as e:
        db.rollback()
        logger.error(f"Wallet credit failed: {e}")
        if not commit:
            raise  # caller's transaction is gone -- let it handle the failure
        return get_balance(db, whatsapp_id)


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
) -> Tuple[bool, int]:
    """Atomically debit ``price`` from an already-resolved customer's wallet.

    Returns ``(charged, balance_after)``. The WHERE clause carries the
    affordability check, so a concurrent tap can never overspend or push the
    balance below zero. This is the one place a wallet debit happens —
    ``charge_generation`` and the WhatsApp funded-slot gate
    (``app/api/routes/meta_webhook.py``) both call it, so there is a single
    authoritative deduction path.
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
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Wallet charge failed: {e}")
        return False, customer.balance_rupees

    db.refresh(customer)

    if updated != 1:
        logger.warning(
            f"Wallet charge declined: whatsapp_id={customer.whatsapp_id} "
            f"balance={customer.balance_rupees} price={price}"
        )
        return False, customer.balance_rupees

    logger.info(
        f"Wallet charged: whatsapp_id={customer.whatsapp_id} amount={price} "
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
            f"Wallet charge skipped — unknown customer whatsapp_id={whatsapp_id}"
        )
        return False, 0

    return charge_customer_balance(db, customer, price)


def refund_generation_charge(
    db: Session,
    whatsapp_id: str,
    amount: int,
) -> bool:
    """Return a charge to the wallet after a failed generation."""
    amount = max(int(amount), 0)
    if amount == 0:
        return False

    try:
        updated = (
            db.query(Customer)
            .filter(Customer.whatsapp_id == whatsapp_id)
            .update(
                {Customer.wallet_balance: Customer.wallet_balance + amount},
                synchronize_session=False,
            )
        )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Wallet refund failed: {e}")
        return False

    if updated != 1:
        logger.warning(
            f"Wallet refund skipped — unknown customer whatsapp_id={whatsapp_id}"
        )
        return False

    logger.info(
        f"Wallet refunded: whatsapp_id={whatsapp_id} amount={amount}"
    )
    return True
