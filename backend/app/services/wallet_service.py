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

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import settings
from app.models.customer import Customer
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.repositories.base import BaseRepository
from app.services import onboarding_service
from app.services.image_prevalidation_service import (
    QualityCheckResult,
    check_image_quality,
)
from app.services.meta_whatsapp_service import send_whatsapp_text
from app.utils.logger import logger

# ─── Ingestion statuses owned by the wallet gate ─────────────────────────

STATUS_PENDING_PAYMENT = "pending_payment"
STATUS_PENDING_REGISTRATION = "pending_registration"
STATUS_REJECTED = "rejected"

# Sent when the wallet gate itself failed (internal error). Deliberately does
# NOT mention a balance, because we could not read it reliably.
WALLET_ERROR_MESSAGE = (
    "Sorry, something went wrong on our side. 🙏\n\n"
    "Please resend your photo in a moment."
)

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


async def _safe_send_text(whatsapp_id: str, text: str) -> bool:
    """Send a plain text message; failures are logged, never raised."""
    try:
        return await send_whatsapp_text(whatsapp_id, text)
    except Exception as e:
        logger.error(f"Wallet notice text send failed: {e}")
        return False


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
    """Current wallet balance in Rupees (0 when the customer is unknown)."""
    customer = get_customer(db, whatsapp_id)
    return customer.balance_rupees if customer else 0


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
        return get_balance(db, whatsapp_id)

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


# ─── Batch plan (Scenario 3) ─────────────────────────────────────────────


@dataclass
class BatchPlan:
    """Funding plan for the images one customer sent in a single delivery.

    ``allocate()`` hands out funded slots in arrival order; ``release_slot()``
    returns a slot when the image it was given to is rejected by the quality
    inspector, so a later image in the same batch can still be funded.
    """

    whatsapp_id: str
    total: int
    is_registered: bool
    balance: int
    price_per_image: int
    funded_slots: int
    funded: int = 0
    held: int = 0
    rejected: int = 0
    processed: int = 0
    gate_error: bool = False
    _slots_left: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        self._slots_left = max(int(self.funded_slots), 0)

    # ── accounting ─────────────────────────────────────────────────────

    def allocate(self) -> bool:
        """Reserve a funded slot for the next image (arrival order)."""
        self.processed += 1
        if self._slots_left > 0:
            self._slots_left -= 1
            return True
        self.held += 1
        return False

    def release_slot(self) -> None:
        """Give back a slot (image rejected — no money was taken)."""
        self._slots_left += 1

    def mark_funded(self) -> None:
        """Record that a funded image entered the style-selection flow."""
        self.funded += 1

    def mark_rejected(self) -> None:
        """Record that a funded image failed pre-validation."""
        self.rejected += 1

    # ── derived values ─────────────────────────────────────────────────

    @property
    def needs_notice(self) -> bool:
        """True when the customer must be told something about this batch."""
        return self.held > 0 or not self.is_registered or self.gate_error

    def shortfall_rupees(self) -> int:
        """Rupees still needed to fund every held image."""
        reserved = self.funded * self.price_per_image
        available = max(self.balance - reserved, 0)
        return max(self.held * self.price_per_image - available, 0)

    def recharge_amount(self) -> int:
        """Suggested top-up amount, rounded up to a whole image price."""
        shortfall = self.shortfall_rupees()
        if shortfall <= 0:
            return self.price_per_image
        blocks = (shortfall + self.price_per_image - 1) // self.price_per_image
        return blocks * self.price_per_image

    def remaining_balance(self) -> int:
        """Balance the customer will have once funded images are generated."""
        return max(self.balance - self.funded * self.price_per_image, 0)


def build_batch_plan(db: Session, whatsapp_id: str, total_images: int) -> BatchPlan:
    """Build the funding plan for a customer's image batch.

    Slot maths lives in ``Customer.affordable_image_count`` -- the live
    WhatsApp webhook uses the same helper, so the two cannot drift.
    """
    total = max(int(total_images), 1)
    price = price_per_image()
    customer = get_customer(db, whatsapp_id)
    is_registered = bool(customer and customer.is_registered)
    balance = customer.balance_rupees if customer else 0

    funded_slots = 0
    if is_registered:
        funded_slots = customer.affordable_image_count(total)

    plan = BatchPlan(
        whatsapp_id=whatsapp_id,
        total=total,
        is_registered=is_registered,
        balance=balance,
        price_per_image=price,
        funded_slots=funded_slots,
    )

    logger.info(
        f"Wallet batch plan: whatsapp_id={whatsapp_id} images={total} "
        f"registered={is_registered} balance={balance} funded={funded_slots}"
    )
    return plan


# ─── Image gate (Scenarios 2, 3, 4) ──────────────────────────────────────


@dataclass
class ImageGateDecision:
    """Whether one stored image may enter the style-selection flow."""

    authorized: bool
    reason: str
    plan: Optional[BatchPlan] = None
    quality: Optional[QualityCheckResult] = None


async def authorize_image(
    db: Session,
    ingestion: WhatsAppIngestion,
    image_bytes: bytes,
    mime_type: str,
    plans: Dict[str, BatchPlan],
) -> ImageGateDecision:
    """Decide whether a stored image may proceed to generation.

    Order of checks: registration → wallet funding → quality inspection.
    Never raises: on an unexpected failure the image is held for payment
    (safe default — the customer keeps their money and can retry).
    """
    sender = ingestion.external_user_id or ""
    plan = plans.get(sender)
    if plan is None:
        plan = build_batch_plan(db, sender, 1)
        plans[sender] = plan

    try:
        # ── Scenario 1 gate: images need a registered profile ──────────
        if not plan.is_registered:
            ingestion.status = STATUS_PENDING_REGISTRATION
            ingestion.error_message = "Image held — customer not registered yet"
            db.commit()
            logger.info(
                f"Wallet gate: image held for registration "
                f"ingestion_id={ingestion.id} user={sender}"
            )
            return ImageGateDecision(
                authorized=False, reason="not_registered", plan=plan
            )

        # ── Scenario 2 / 3: is there money for this image? ─────────────
        if not plan.allocate():
            ingestion.status = STATUS_PENDING_PAYMENT
            ingestion.error_message = (
                f"Insufficient wallet balance "
                f"(balance={plan.balance}, price={plan.price_per_image})"
            )
            db.commit()
            logger.info(
                f"Wallet gate: image held for payment ingestion_id={ingestion.id} "
                f"user={sender} balance={plan.balance}"
            )
            return ImageGateDecision(
                authorized=False, reason="pending_payment", plan=plan
            )

        # ── Scenario 4: quality inspection before burning credits ──────
        if settings.IMAGE_PREVALIDATION_ENABLED:
            quality = await check_image_quality(image_bytes, mime_type)
            if not quality.approved:
                plan.release_slot()
                plan.mark_rejected()
                ingestion.status = STATUS_REJECTED
                ingestion.error_message = (
                    f"Pre-validation rejected: {quality.reason or 'unknown'}"
                )
                db.commit()
                logger.info(
                    f"Wallet gate: image rejected by pre-validation "
                    f"ingestion_id={ingestion.id} reason={quality.reason}"
                )
                await _safe_send_text(sender, quality.rejection_message)
                return ImageGateDecision(
                    authorized=False, reason="rejected", plan=plan, quality=quality
                )

        plan.mark_funded()
        logger.info(
            f"Wallet gate: image funded ingestion_id={ingestion.id} "
            f"user={sender} slot={plan.funded}/{plan.funded_slots}"
        )
        return ImageGateDecision(authorized=True, reason="funded", plan=plan)

    except Exception as e:
        db.rollback()
        logger.error(f"Wallet gate failed — image held for payment: {e}")
        plan.gate_error = True
        try:
            ingestion.status = STATUS_PENDING_PAYMENT
            ingestion.error_message = "Image held — wallet check failed"
            db.commit()
        except Exception as inner_e:
            db.rollback()
            logger.error(f"Wallet gate: could not persist hold state: {inner_e}")
        return ImageGateDecision(
            authorized=False, reason="wallet_error", plan=plan
        )


# ─── Customer notices ────────────────────────────────────────────────────


def build_balance_notice(plan: BatchPlan) -> str:
    """Balance / partial-batch notice for a plan with held images.

    The single-image, zero-or-low-balance case returns exactly the documented
    Scenario 2 copy.
    """
    base = (
        f"Your current balance is {format_rupees(plan.balance)} ⚠️\n"
        "Please make a payment to continue."
    )

    if plan.funded == 0 and plan.held == 1:
        return base

    if plan.funded == 0:
        return (
            f"We received {plan.held} images 📸\n\n"
            f"All {plan.held} images are on hold — "
            f"{format_rupees(plan.shortfall_rupees())} needed.\n\n"
            f"{base}"
        )

    return (
        f"We received {plan.total} images 📸\n\n"
        f"✅ Ready to generate: {plan.funded} "
        f"(at {format_rupees(plan.price_per_image)} each)\n"
        f"⏸️ On hold: {plan.held} — "
        f"{format_rupees(plan.shortfall_rupees())} needed\n\n"
        f"{base}"
    )


def build_recharge_cta_body(plan: BatchPlan, amount: int) -> str:
    """Body text shown above the recharge button."""
    if plan.held:
        return (
            f"Top up {format_rupees(amount)} to process "
            f"{plan.held} held image{'s' if plan.held != 1 else ''} ✨"
        )
    return f"Top up {format_rupees(amount)} to keep creating ✨"


async def send_batch_summary(plan: BatchPlan) -> bool:
    """Send the held-image notice + recharge CTA for one customer's batch.

    Called once per customer after the whole webhook delivery has been
    processed, so an N-image batch produces exactly one notice and one button.
    """
    if not plan.needs_notice:
        return False

    if not plan.is_registered:
        # Images from an unknown number: start the registration journey.
        return await onboarding_service.send_registration_prompt(plan.whatsapp_id)

    if plan.gate_error:
        # We could not read the wallet reliably — never quote a balance we do
        # not trust. Ask the customer to resend instead.
        sent = await _safe_send_text(plan.whatsapp_id, WALLET_ERROR_MESSAGE)
        if not plan.held:
            return sent

    amount = plan.recharge_amount()
    sent = await _safe_send_text(plan.whatsapp_id, build_balance_notice(plan))
    await onboarding_service.send_recharge_cta(
        plan.whatsapp_id,
        body_text=build_recharge_cta_body(plan, amount),
        amount_rupees=amount,
    )
    logger.info(
        f"Wallet notice sent: whatsapp_id={plan.whatsapp_id} "
        f"funded={plan.funded} held={plan.held} rejected={plan.rejected} "
        f"recharge={amount}"
    )
    return sent


async def send_batch_summaries(plans: Dict[str, BatchPlan]) -> int:
    """Flush notices for every batch plan that needs one. Never raises."""
    sent = 0
    for plan in plans.values():
        try:
            if await send_batch_summary(plan):
                sent += 1
        except Exception as e:
            logger.error(
                f"Wallet notice failed for whatsapp_id={plan.whatsapp_id}: {e}"
            )
    return sent


async def send_insufficient_balance_notice(
    db: Session,
    whatsapp_id: str,
    needed_images: int = 1,
) -> bool:
    """Send the balance notice + CTA for a single blocked generation."""
    customer = get_customer(db, whatsapp_id)
    balance = customer.balance_rupees if customer else 0
    price = price_per_image()
    needed = max(int(needed_images), 1)

    plan = BatchPlan(
        whatsapp_id=whatsapp_id,
        total=needed,
        is_registered=True,
        balance=balance,
        price_per_image=price,
        funded_slots=0,
    )
    plan.held = needed

    amount = plan.recharge_amount()
    await _safe_send_text(whatsapp_id, build_balance_notice(plan))
    return await onboarding_service.send_recharge_cta(
        whatsapp_id,
        body_text=build_recharge_cta_body(plan, amount),
        amount_rupees=amount,
    )


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
