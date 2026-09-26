"""Live GSTIN verification for WhatsApp onboarding (provider-agnostic).

OFF by default (``GST_VERIFICATION_ENABLED=false``): nothing here runs and
onboarding behaves exactly as before.

Pieces:
    normalize_gstin / is_valid_gstin_format  -- Step A pre-check (no API call)
    GstProvider subclasses                   -- one class per lookup vendor
    parse_gst_payload                        -- reads the GST-portal taxpayer
                                                schema (sts / tradeNam / lgnm /
                                                pradr) wherever a vendor nests it
    verify_gstin                             -- Step B, never raises

No real vendor is wired yet. ``GST_PROVIDER``:
    "none" (default) -> every lookup reports ``unavailable`` (retry / skip)
    "mock"           -> local testing only; honoured only while DEBUG=true
To add a vendor: subclass GstProvider, implement ``lookup`` to return the
vendor's JSON, register it in ``_PROVIDERS``. If the vendor's JSON does not
use the GST-portal field names, override ``parse``.
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.config import settings
from app.utils.logger import logger

# Step A: 2-digit state code, 10-char PAN, entity code 1-9/A-Z, literal Z, check char.
GSTIN_FORMAT_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")

INVALID_FORMAT_MESSAGE = (
    "Invalid GST format. A valid GSTIN must be 15 alphanumeric characters "
    "(e.g., 27ABCDE1234F1Z5)."
)

# Result statuses
ACTIVE = "active"            # verified: sts == "Active"
INACTIVE = "inactive"        # registered but cancelled / suspended / etc.
NOT_FOUND = "not_found"      # registry has no such GSTIN
INVALID_FORMAT = "invalid_format"
UNAVAILABLE = "unavailable"  # timeout, HTTP/API error, or no provider configured


@dataclass
class GstVerificationResult:
    status: str
    gstin: str
    trade_name: str = ""
    legal_name: str = ""
    address: str = ""
    registry_status: str = ""   # raw "sts" text, e.g. "Cancelled"
    detail: str = ""

    @property
    def verified(self) -> bool:
        return self.status == ACTIVE

    @property
    def display_name(self) -> str:
        return self.trade_name or self.legal_name


def normalize_gstin(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def is_valid_gstin_format(value: Any) -> bool:
    return bool(GSTIN_FORMAT_RE.match(normalize_gstin(value)))


# ─── Payload parsing (GST-portal taxpayer schema) ─────────────────────────

def _find_taxpayer(payload: Any, depth: int = 0) -> Optional[Dict[str, Any]]:
    """Return the dict that carries ``sts`` (vendors nest it under data/result)."""
    if not isinstance(payload, dict) or depth > 4:
        return None
    if "sts" in payload:
        return payload
    for key in ("data", "result", "taxpayerInfo", "response"):
        found = _find_taxpayer(payload.get(key), depth + 1)
        if found:
            return found
    return None


def _format_address(pradr: Any) -> str:
    addr = (pradr or {}).get("addr") if isinstance(pradr, dict) else None
    if not isinstance(addr, dict):
        return str((pradr or {}).get("adr", "") if isinstance(pradr, dict) else "").strip()
    parts = [addr.get(k) for k in ("bno", "bnm", "flno", "st", "loc", "dst", "stcd", "pncd")]
    return ", ".join(str(p).strip() for p in parts if p and str(p).strip())


def parse_gst_payload(gstin: str, payload: Any) -> GstVerificationResult:
    taxpayer = _find_taxpayer(payload)
    if taxpayer is None:
        return GstVerificationResult(NOT_FOUND, gstin, detail="no taxpayer record in response")
    registry_status = str(taxpayer.get("sts") or "").strip()
    result = GstVerificationResult(
        status=ACTIVE if registry_status.lower() == "active" else INACTIVE,
        gstin=gstin,
        trade_name=str(taxpayer.get("tradeNam") or "").strip(),
        legal_name=str(taxpayer.get("lgnm") or "").strip(),
        address=_format_address(taxpayer.get("pradr")),
        registry_status=registry_status,
    )
    returned = normalize_gstin(taxpayer.get("gstin") or gstin)
    if returned != gstin:
        return GstVerificationResult(NOT_FOUND, gstin, detail=f"registry returned {returned}")
    return result


# ─── Providers ────────────────────────────────────────────────────────────

class GstLookupNotFound(Exception):
    """Vendor says the GSTIN does not exist (e.g. HTTP 404)."""


class GstProvider:
    name = "base"

    async def lookup(self, gstin: str) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def parse(self, gstin: str, payload: Any) -> GstVerificationResult:
        return parse_gst_payload(gstin, payload)


class NoGstProvider(GstProvider):
    name = "none"

    async def lookup(self, gstin: str) -> Any:
        raise RuntimeError("no GST lookup provider configured (GST_PROVIDER)")


class MockGstProvider(GstProvider):
    """Local testing: every well-formed GSTIN is Active, except state code 99."""
    name = "mock"

    async def lookup(self, gstin: str) -> Any:
        if gstin.startswith("99"):
            return {"data": {"gstin": gstin, "sts": "Cancelled", "lgnm": "MOCK CANCELLED CO"}}
        return {"data": {
            "gstin": gstin, "sts": "Active", "tradeNam": "MOCK VERIFIED TRADERS",
            "lgnm": "MOCK VERIFIED TRADERS PVT LTD",
            "pradr": {"addr": {"bno": "12", "st": "Diamond Plaza", "loc": "Surat", "stcd": "Gujarat", "pncd": "395003"}},
        }}


_PROVIDERS = {"none": NoGstProvider, "mock": MockGstProvider}


def get_gst_provider() -> GstProvider:
    name = str(getattr(settings, "GST_PROVIDER", "none") or "none").strip().lower()
    if name == "mock" and not settings.DEBUG:
        logger.warning("GST_PROVIDER=mock ignored because DEBUG is false")
        name = "none"
    return _PROVIDERS.get(name, NoGstProvider)()


async def verify_gstin(raw_gstin: Any, provider: Optional[GstProvider] = None) -> GstVerificationResult:
    """Step A + Step B. Never raises; API trouble becomes UNAVAILABLE."""
    gstin = normalize_gstin(raw_gstin)
    if not GSTIN_FORMAT_RE.match(gstin):
        return GstVerificationResult(INVALID_FORMAT, gstin)
    provider = provider or get_gst_provider()
    timeout = float(getattr(settings, "GST_API_TIMEOUT_SECONDS", 8.0) or 8.0)
    try:
        payload = await asyncio.wait_for(provider.lookup(gstin), timeout=timeout)
    except GstLookupNotFound as e:
        return GstVerificationResult(NOT_FOUND, gstin, detail=str(e))
    except asyncio.TimeoutError:
        logger.warning(f"GST lookup timed out after {timeout}s ({provider.name})")
        return GstVerificationResult(UNAVAILABLE, gstin, detail="timeout")
    except Exception as e:  # noqa: BLE001 -- HTTP errors, bad JSON, no provider
        logger.warning(f"GST lookup failed ({provider.name}): {type(e).__name__}: {e}")
        return GstVerificationResult(UNAVAILABLE, gstin, detail=str(e)[:200])
    try:
        return provider.parse(gstin, payload)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"GST payload could not be parsed ({provider.name}): {e}")
        return GstVerificationResult(UNAVAILABLE, gstin, detail="unparseable response")


# ═══ Onboarding step: verify, then Re-enter / Skip buttons ═══════════════
# State lives in the existing onboarding_sessions table (one row per
# WhatsApp number). Only AWAITING_GSTIN changes routing: the next text
# message is treated as a GSTIN. Every other state keeps today's flow.

STATE_AWAITING_GSTIN = "AWAITING_GSTIN"
STATE_REGISTERED = "REGISTERED"   # profile done -> recharge / send photo

BTN_GST_REENTER = "btn_gst_reenter"
BTN_GST_SKIP = "btn_gst_skip"
GST_BUTTONS = [(BTN_GST_REENTER, "Re-enter GSTIN"), (BTN_GST_SKIP, "Skip for now")]

REENTER_PROMPT = "Please enter your 15-digit GSTIN number:"
SUCCESS_TEMPLATE = "✅ GSTIN Verified Successfully! Trade Name: {trade_name}. Profile complete."
SKIP_MESSAGE = (
    "Understood! You can proceed without GST verification. Note: B2B input tax "
    "credit (ITC) won't be available on invoices without a verified GSTIN.\n\n"
    "Send your jewelry photo whenever you're ready to start!"
)
_CHOICE_SUFFIX = "\n\nWould you like to re-enter it or skip for now?"
_NO_GST_ANSWERS = {"", "na", "n/a", "none", "no", "nil", "-", "skip"}


def verification_enabled() -> bool:
    return bool(getattr(settings, "GST_VERIFICATION_ENABLED", False))


def _session(db, whatsapp_id: str, create: bool = False):
    from app.models.onboarding_session import OnboardingSession

    row = db.query(OnboardingSession).filter(OnboardingSession.whatsapp_id == whatsapp_id).first()
    if row is None and create:
        row = OnboardingSession(whatsapp_id=whatsapp_id, state=STATE_REGISTERED)
        db.add(row)
    return row


def _set_state(db, whatsapp_id: str, state: str) -> None:
    _session(db, whatsapp_id, create=True).state = state


def is_awaiting_gstin(db, sender: str) -> bool:
    if not verification_enabled():
        return False
    from app.services.wallet_service import find_customer_by_phone

    try:
        customer = find_customer_by_phone(db, sender)
        row = _session(db, customer.whatsapp_id) if customer else None
        return bool(row and row.state == STATE_AWAITING_GSTIN)
    except Exception as e:  # noqa: BLE001 -- never break the text router
        db.rollback()
        logger.error(f"GST state lookup failed for {sender}: {e}")
        return False


async def _send_text(sender: str, text: str) -> None:
    from app.services.meta_whatsapp_service import send_whatsapp_text

    await send_whatsapp_text(sender, text)


async def _send_choice(sender: str, body: str) -> None:
    from app.services.meta_whatsapp_service import send_reply_buttons

    await send_reply_buttons(sender, body + _CHOICE_SUFFIX, GST_BUTTONS)


async def process_gstin(db, sender: str, raw_gstin: Any) -> Optional[GstVerificationResult]:
    """Verify one GSTIN for the sender's customer row and message the outcome.

    Never raises. Wallet, balance and images are not touched.
    """
    from app.services.wallet_service import find_customer_by_phone

    try:
        customer = find_customer_by_phone(db, sender)
        if customer is None:
            return None
        result = await verify_gstin(raw_gstin)

        if result.verified:
            customer.gst_number = result.gstin
            if result.display_name:
                customer.business_name = result.display_name[:255]
            if result.address:
                customer.address = result.address
            customer.is_gst_verified = True
            _set_state(db, customer.whatsapp_id, STATE_REGISTERED)
            db.commit()
            await _send_text(sender, SUCCESS_TEMPLATE.format(
                trade_name=result.display_name or customer.business_name))
            return result

        customer.is_gst_verified = False
        if result.status in (INACTIVE, NOT_FOUND):
            customer.gst_number = "N/A"  # never invoice with a rejected GSTIN
        _set_state(db, customer.whatsapp_id, STATE_REGISTERED)
        db.commit()

        if result.status == INVALID_FORMAT:
            body = INVALID_FORMAT_MESSAGE
        elif result.status == INACTIVE:
            body = (f"We couldn't verify GSTIN {result.gstin}: the GST registry shows it as "
                    f"{result.registry_status or 'not active'}.")
        elif result.status == NOT_FOUND:
            body = f"We couldn't find GSTIN {result.gstin} in the GST registry."
        else:  # UNAVAILABLE: timeout / API error -> retry or skip
            body = (f"The GST registry isn't responding right now, so we couldn't verify "
                    f"{result.gstin} yet. You can try again or skip for now.")
        await _send_choice(sender, body)
        return result
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.error(f"GST verification step failed for {sender}: {e}")
        return None


async def verify_after_registration(db, sender: str, raw_gstin: Any) -> None:
    """Called right after a profile is saved (text form or WhatsApp Flow)."""
    if not verification_enabled():
        return
    if str(raw_gstin or "").strip().lower() in _NO_GST_ANSWERS:
        from app.services.wallet_service import find_customer_by_phone

        customer = find_customer_by_phone(db, sender)
        if customer is not None and customer.is_gst_verified:
            customer.is_gst_verified = False
            db.commit()
        return  # no GSTIN given: unchanged flow, nothing to verify
    await process_gstin(db, sender, raw_gstin)


async def handle_gst_button(db, sender: str, button_id: str) -> bool:
    """Re-enter / Skip buttons. Returns True when the button was ours."""
    if button_id not in (BTN_GST_REENTER, BTN_GST_SKIP) or not verification_enabled():
        return False
    from app.services.wallet_service import find_customer_by_phone

    try:
        customer = find_customer_by_phone(db, sender)
        if customer is None:
            return True
        if button_id == BTN_GST_REENTER:
            _set_state(db, customer.whatsapp_id, STATE_AWAITING_GSTIN)
            db.commit()
            await _send_text(sender, REENTER_PROMPT)
        else:
            await skip_gst(db, sender)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.error(f"GST button handling failed for {sender}: {e}")
    return True


async def skip_gst(db, sender: str) -> None:
    from app.services.wallet_service import find_customer_by_phone

    customer = find_customer_by_phone(db, sender)
    if customer is None:
        return
    customer.is_gst_verified = False
    customer.gst_number = "N/A"  # gst_number is NOT NULL; "N/A" is the no-GST convention
    _set_state(db, customer.whatsapp_id, STATE_REGISTERED)
    db.commit()
    await _send_text(sender, SKIP_MESSAGE)


async def handle_gstin_reply(db, sender: str, text: str) -> None:
    """Text received while AWAITING_GSTIN: a GSTIN attempt, or 'skip'."""
    if text.strip().lower() in ("skip", "skip for now"):
        await skip_gst(db, sender)
        return
    await process_gstin(db, sender, text)
