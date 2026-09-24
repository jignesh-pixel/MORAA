"""WhatsApp new-customer onboarding service (Scenario 1 ONLY).

This module is the single home of onboarding logic:
    * registration state handling
      (NOT_REGISTERED / AWAITING_REGISTRATION / REGISTERED)
    * free-form registration parsing — strict JSON via a Gemini text parser
      whose values are ground-checked against the customer's own message,
      with a deliberately CONSERVATIVE label-based fallback so onboarding
      still works when no LLM is reachable (an uncertain field stays empty)
    * idempotent customer creation
    * the WhatsApp messages / recharge CTA sent during onboarding

Design rules honoured here:
    * ALL WhatsApp HTTP sending is delegated to the existing
      ``app.services.meta_whatsapp_service`` — no HTTP logic lives here.
    * The webhook is a THIN delegator: it calls ``handle_text`` and moves on.
    * ``handle_text`` NEVER raises: a parser/DB/API failure is logged safely
      and results in either a friendly retry message or ``handled=False``
      (so the existing WhatsApp behaviour continues untouched).
    * Nothing is ever invented: missing fields stay missing,
      ``is_complete`` stays False, ungrounded LLM values are dropped, and the
      fallback parser never turns conversational prose into fields.
    * A new user's FIRST ordinary text message always starts the documented
      welcome journey; registration data is only parsed from follow-up
      messages once the user is AWAITING_REGISTRATION.
    * Tokens, API secrets and authorization headers are never logged, and
      internal exceptions are never exposed to the customer.

Not implemented here (deliberately): wallet, payments, Razorpay, WhatsApp
Pay, image pre-validation, Celery/generation changes, Scenarios 2-4.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models.customer import Customer
from app.models.onboarding_session import OnboardingSession
from app.repositories.base import BaseRepository
from app.services.meta_whatsapp_service import (
    send_interactive_cta_button,
    send_text_message,
    send_whatsapp_cta_url_button,
)
from app.utils.logger import logger


# ─── Message templates (exact customer-facing copy) ──────────────────────

WELCOME_MESSAGE = (
    "Hi there! Welcome to Moraa Studio ✨\n\n"
    "We help you turn raw jewelry photos into polished, e-commerce ready "
    "images — powered by AI 💎\n\n"
    "Let’s get you set up, it only takes a minute!"
)

REGISTRATION_INSTRUCTIONS_MESSAGE = (
    "Quick registration 📋\n\n"
    "Copy this, fill in your details and send it right back:\n\n"
    "Name:\n"
    "Business name:\n"
    "GST number:\n"
    "Business address:"
)

PARSER_FAILURE_MESSAGE = (
    "Sorry, I couldn’t read those details properly. 🙏\n\n"
    "Please send your Name, Business name, GST number and Business address "
    "again."
)

INCOMPLETE_REGISTRATION_HEADER = "Almost there! 📋"

CONGRATULATIONS_TEMPLATE = (
    "Congratulations {first_name}! You’re registered with Moraa Studio 🎉\n\n"
    "You’re all set to start creating stunning product photos."
)

# Interactive CTA shown after successful registration.
# PLACEHOLDER ONLY — no wallet, payment gateway or deduction logic exists.
RECHARGE_BUTTON_ID = "recharge_500"
RECHARGE_BUTTON_TITLE = "💳 Recharge to use"
RECHARGE_CTA_BODY = (
    "Tap below and recharge when you’re ready to start creating ✨"
)

# Greetings that (re)start the onboarding journey for a NON-registered user.
# Anchored to the whole message so "hi, my name is Ravi and I run a shop" is
# NOT treated as a greeting.
_GREETING_REGEX = re.compile(
    r"^\s*(?:"
    r"hi+|hey+|hello+|helo|hlo|yo|start|begin|sign\s*up|register|"
    r"namaste|namaskar|namaskaram|vanakkam|hola|"
    r"good\s*(?:morning|afternoon|evening)|"
    r"jai\s*jinendra|kem\s*cho"
    r")\s*(?:there|dost|ji|sir|madam)?[\s!.,।]*$",
    re.IGNORECASE,
)

# Shown when the details were read but could not be saved (internal fault).
# Deliberately vague — internal errors are never exposed to the customer.
RETRY_MESSAGE = (
    "Sorry, something went wrong on our side. 🙏\n\n"
    "Please send your Name, Business name, GST number and Business address "
    "again."
)

# ─── Limits / validation ─────────────────────────────────────────────────

# Maximum inbound text length handed to the parser (defensive bound).
MAX_REGISTRATION_TEXT_LENGTH = 2000

# Required registration fields, in the order customers are asked for them.
REQUIRED_FIELDS: tuple = ("full_name", "business_name", "gst_number", "address")

FIELD_LABELS: Dict[str, str] = {
    "full_name": "Name",
    "business_name": "Business name",
    "gst_number": "GST number",
    "address": "Business address",
}

# Values a customer may send instead of a real GST number. These are treated
# as "no GST supplied" (never invented, never counted as complete).
_GST_PLACEHOLDER_VALUES = {
    "no", "none", "nil", "na", "n/a", "not applicable", "not-applicable",
    "no gst", "no gstin", "nahi", "nai", "unknown", "not registered",
}


class OnboardingState(str, Enum):
    """Registration states for a WhatsApp user."""

    NOT_REGISTERED = "NOT_REGISTERED"
    AWAITING_REGISTRATION = "AWAITING_REGISTRATION"
    REGISTERED = "REGISTERED"


class RegistrationParserError(Exception):
    """Raised when registration text cannot be parsed at all.

    Callers (the onboarding flow) catch this and send a friendly retry
    message — the exception never reaches the WhatsApp customer.
    """


@dataclass
class RegistrationData:
    """Structured registration fields extracted from a WhatsApp message.

    ``is_complete`` is always DERIVED from the four required fields so a
    missing field can never be reported as complete.
    """

    full_name: str = ""
    business_name: str = ""
    gst_number: str = ""
    address: str = ""
    is_complete: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Return the strict parser JSON shape."""
        return {
            "full_name": self.full_name,
            "business_name": self.business_name,
            "gst_number": self.gst_number,
            "address": self.address,
            "is_complete": self.is_complete,
        }

    @property
    def missing_fields(self) -> List[str]:
        """Required fields that are still empty, in ask-order."""
        return [name for name in REQUIRED_FIELDS if not getattr(self, name)]

    def recompute_completeness(self) -> "RegistrationData":
        """Recompute ``is_complete`` from the fields themselves."""
        self.is_complete = not self.missing_fields
        return self


@dataclass
class OnboardingResult:
    """Outcome of the onboarding interceptor for one inbound text message."""

    handled: bool = False
    response: Optional[Dict[str, Any]] = None


# ─── Text sanitisation ───────────────────────────────────────────────────


def sanitize_text(text: Optional[str]) -> str:
    """Sanitise inbound WhatsApp text before parsing or logging.

    Removes control characters, trims whitespace, and bounds the length so
    an oversized message cannot be forwarded to the LLM.
    """
    if not text or not isinstance(text, str):
        return ""

    # Drop control characters (keep normal whitespace).
    cleaned = "".join(
        ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32
    )
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()

    if len(cleaned) > MAX_REGISTRATION_TEXT_LENGTH:
        logger.warning(
            f"Onboarding: inbound text truncated "
            f"({len(cleaned)} -> {MAX_REGISTRATION_TEXT_LENGTH} chars)"
        )
        cleaned = cleaned[:MAX_REGISTRATION_TEXT_LENGTH]

    return cleaned


def _clean_value(value: Any) -> str:
    """Normalise a single parsed field value (string-safe, trimmed)."""
    if value is None or not isinstance(value, str):
        return ""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    # Strip surrounding separators/quotes left over from "Label: value" input.
    value = value.strip().strip(",;|").strip().strip('"').strip("'").strip()
    return value


def normalize_gst_number(value: Any) -> str:
    """Normalise a GST number without ever inventing one.

    Uppercases, removes internal spaces/hyphens, and treats placeholder
    answers ("no GST", "not registered", ...) as *missing*.
    Returns "" when nothing usable was supplied.
    """
    cleaned = _clean_value(value).upper()
    if not cleaned:
        return ""

    if cleaned.lower() in _GST_PLACEHOLDER_VALUES:
        return ""

    # Drop separators inside the number, then validate plausibility:
    # a real GSTIN mixes digits and letters. Anything else stays missing
    # rather than being counted as a GST number.
    compact = re.sub(r"[\s\-_.]", "", cleaned)
    digits = sum(1 for ch in compact if ch.isdigit())
    letters = sum(1 for ch in compact if ch.isalpha())

    if len(compact) < 10 or digits < 5 or letters < 2:
        logger.info(
            "Onboarding: supplied GST value is not a plausible GSTIN — "
            "treated as missing"
        )
        return ""

    if not re.fullmatch(r"[0-9A-Z]+", compact):
        return ""

    return compact


# Phrasing that explicitly states "I do not have a GST number". Detection is
# anchored on the GST token so an unrelated "nahi hai" can never blank a
# genuinely supplied GST number.
_NO_GST_PATTERNS = (
    re.compile(
        r"\b(?:no|not|don'?t|do\s+not|without|nahi|bina)\b[^\n]{0,12}?"
        r"\b(?:have\s+)?(?:an?\s+)?(?:(?:gst|gstin)\s*(?:number|no\.?|in)?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bgst(?:in)?\s*(?:number|no\.?|in)?\s*(?:is\s*)?"
        r"(?:not\s+(?:available|registered|applicable)|nahi|nai|nahin)\b",
        re.IGNORECASE,
    ),
)


def _mentions_no_gst(text: str) -> bool:
    """Detect an explicit 'I do not have a GST number' statement."""
    return any(pattern.search(text) for pattern in _NO_GST_PATTERNS)


# ─── Registration parsing ────────────────────────────────────────────────

PARSER_SYSTEM_PROMPT = (
    "You are a precise data-extraction engine for MORAA Studio customer "
    "registration. Extract ONLY the registration fields explicitly present "
    "in the customer's WhatsApp message and return STRICT JSON.\n\n"
    "Return exactly this JSON object and nothing else:\n"
    '{"full_name": "", "business_name": "", "gst_number": "", '
    '"address": "", "is_complete": false}\n\n'
    "Rules:\n"
    "- Never invent, guess, autocomplete or correct any value. If a field is "
    "not explicitly present, return an empty string for it.\n"
    "- The message may be free-form, may use different labels, Hinglish, "
    "typos, extra spaces, punctuation, or list fields in any order.\n"
    "- full_name: the person's name (never the business name).\n"
    "- business_name: the business / shop / firm / company / brand name "
    "exactly as supplied.\n"
    "- gst_number: the GST / GSTIN value only, uppercased with spaces "
    "removed. If the customer says they do not have a GST number (e.g. "
    '"no GST", "GST nahi hai"), return an empty string — never invent one.\n'
    "- address: the business address exactly as supplied.\n"
    "- is_complete: true ONLY when all four fields are non-empty.\n"
    "- Output raw JSON only: no markdown, no code fences, no explanation, "
    "no conversational reply."
)

# Label markers used by the deterministic fallback parser. Longer phrases are
# listed before their short forms so they win at the same start position.
_LABEL_PATTERNS: List[tuple] = [
    # full_name
    (r"full\s*name", "full_name"),
    (r"customer\s*name", "full_name"),
    (r"your\s*name", "full_name"),
    (r"naam", "full_name"),
    (r"name", "full_name"),
    # business_name
    (r"business\s*name", "business_name"),
    (r"shop\s*name", "business_name"),
    (r"firm\s*name", "business_name"),
    (r"company\s*name", "business_name"),
    (r"brand\s*name", "business_name"),
    (r"store\s*name", "business_name"),
    (r"business", "business_name"),
    (r"shop", "business_name"),
    (r"firm", "business_name"),
    (r"company", "business_name"),
    (r"brand", "business_name"),
    (r"store", "business_name"),
    # gst_number
    (r"gstin", "gst_number"),
    (r"gst\s*(?:number|no\.?|in\b)?", "gst_number"),
    # address
    (r"business\s*address", "address"),
    (r"billing\s*address", "address"),
    (r"shop\s*address", "address"),
    (r"full\s*address", "address"),
    (r"address", "address"),
    (r"addr", "address"),
    (r"pata", "address"),
]

# A fallback label is only honoured when it is CLEARLY a field label:
#   * it starts a segment (message start, a new line, or follows , ; |), and
#   * it is immediately followed by an explicit separator (: ： or =).
# "my name is Ravi and I have a jewellery shop" is prose, not a field, so it is
# deliberately NOT parsed — an uncertain field stays empty.
_SEGMENT_BOUNDARY = r"(?:^|[\n,;|])"
_LABEL_SEPARATOR = r"[:：=]"

_MARKER_REGEX = re.compile(
    _SEGMENT_BOUNDARY
    + r"[ \t]*(?P<label>"
    + "|".join(f"(?:{pattern})" for pattern, _ in _LABEL_PATTERNS)
    + r")[ \t]*"
    + _LABEL_SEPARATOR
    + r"[ \t]*",
    re.IGNORECASE | re.MULTILINE,
)

# Fields whose value must stay on the label's own line. Only the address may
# legitimately run across lines, so only it keeps a multi-line value.
_SINGLE_LINE_FIELDS = ("full_name", "business_name", "gst_number")

# Source-grounding tokens: alphanumeric runs of 2+ characters.
_TOKEN_REGEX = re.compile(r"[0-9A-Za-z]{2,}")


def _canonical_field(label: str) -> Optional[str]:
    """Map a matched label string to its canonical field name."""
    lowered = re.sub(r"\s+", " ", label.strip().lower())
    for pattern, field_name in _LABEL_PATTERNS:
        if re.fullmatch(pattern, lowered):
            return field_name
    # Fall back to a prefix check (e.g. "gst number:" matched loosely).
    for pattern, field_name in _LABEL_PATTERNS:
        if re.match(pattern, lowered):
            return field_name
    return None


def _heuristic_extract(text: str) -> RegistrationData:
    """Conservative, deterministic extraction from CLEARLY LABELLED input.

    Only labels that start a segment and carry an explicit separator are read
    as fields; the value is the text up to the next such label. Conversational
    prose ("Hi, my name is Ravi and I have a jewellery shop") therefore yields
    NO fields at all, and a field that cannot be read with confidence stays
    empty rather than being guessed. An address may span multiple lines;
    every other field stops at the end of its line.
    """
    data = RegistrationData()
    matches = list(_MARKER_REGEX.finditer(text))
    if not matches:
        return data.recompute_completeness()

    for index, match in enumerate(matches):
        field_name = _canonical_field(match.group("label"))
        if not field_name or getattr(data, field_name):
            continue  # never overwrite a captured value

        value_start = match.end()
        value_end = (
            matches[index + 1].start() if index + 1 < len(matches) else len(text)
        )
        raw_value = text[value_start:value_end]

        if field_name in _SINGLE_LINE_FIELDS:
            # Trailing prose on the following line is not part of this field.
            raw_value = raw_value.split("\n", 1)[0]

        # Drop leftover separator/filler characters in front of the value.
        raw_value = re.sub(r"^\s*[:：=\-–—]+\s*", "", raw_value)
        raw_value = re.sub(
            r"^\s*(?:is|are|was|hai|hain)\s+",
            "",
            raw_value,
            flags=re.IGNORECASE,
        )
        value = _clean_value(raw_value)

        if not value:
            continue

        if field_name == "gst_number":
            value = normalize_gst_number(value)
            if not value:
                continue

        setattr(data, field_name, value)

    if _mentions_no_gst(text):
        data.gst_number = ""

    return data.recompute_completeness()


def is_value_grounded(value: str, source_text: str) -> bool:
    """Return True when every token of ``value`` occurs in ``source_text``.

    Keeps LLM extraction honest: case, punctuation and whitespace differences
    are tolerated (the model may reformat "ananya shah" as "Ananya Shah"),
    but a value containing words the customer never sent — an invented GST
    number or an embellished address — is rejected.
    """
    value_tokens = _TOKEN_REGEX.findall(value or "")
    if not value_tokens:
        return False

    source_tokens = {token.lower() for token in _TOKEN_REGEX.findall(source_text or "")}
    return all(token.lower() in source_tokens for token in value_tokens)


def _safe_json_loads(raw: Any) -> Optional[Dict[str, Any]]:
    """Parse a JSON object out of an LLM response (code fences tolerated)."""
    if not isinstance(raw, str) or not raw.strip():
        return None

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        parsed = json.loads(cleaned[start : end + 1])
    except Exception:
        return None

    return parsed if isinstance(parsed, dict) else None


def _coerce_llm_payload(payload: Dict[str, Any], text: str) -> RegistrationData:
    """Map an LLM payload onto strict, validated, source-grounded data.

    A value the customer never wrote is discarded (field name only is logged,
    never the value), so a hallucinating model cannot create customer data.
    """
    data = RegistrationData()

    for name in REQUIRED_FIELDS:
        raw_value = payload.get(name)
        value = (
            normalize_gst_number(raw_value)
            if name == "gst_number"
            else _clean_value(raw_value)
        )

        if value and not is_value_grounded(value, text):
            logger.warning(
                f"Onboarding: dropped LLM value for '{name}' — not present in "
                f"the customer's message"
            )
            value = ""

        setattr(data, name, value)

    # The customer explicitly stated they have no GST number.
    if _mentions_no_gst(text):
        data.gst_number = ""

    # is_complete from the LLM is ignored — completeness is always derived
    # from the four required fields, so a missing field can never pass.
    return data.recompute_completeness()


# --- Cost safety net (audit: unbounded paid Gemini calls if this gate is
# ever enabled). Lightweight and in-process on purpose: this whole path is
# disabled by default (ENABLE_ONBOARDING_GATE=False) and unused today, so a
# new DB table isn't warranted -- this only caps spend once someone flips
# the gate on. Mirrors the quota-exhaustion pattern already used in
# app/ai/image_generation_manager.py.
_QUOTA_EXHAUSTION_PATTERNS = (
    "quota exceeded",
    "resource exhausted",
    "resource_exhausted",
    "credit_balance_exhausted",
    "billing",
    "rate limit",
    "429",
)
_onboarding_quota_cooldown_until: float = 0.0
_onboarding_daily_call_count: int = 0
_onboarding_daily_call_day: Optional[str] = None


def _is_onboarding_quota_exhaustion(error_text: str) -> bool:
    lowered = error_text.lower()
    return any(pattern in lowered for pattern in _QUOTA_EXHAUSTION_PATTERNS)


def _onboarding_budget_ok() -> bool:
    """False when the paid Gemini parser call should be skipped this time:
    a quota-exhaustion cooldown is active, or today's call budget is spent.
    Callers fall back to the deterministic heuristic parser either way."""
    global _onboarding_daily_call_count, _onboarding_daily_call_day

    now = time.time()
    if now < _onboarding_quota_cooldown_until:
        return False

    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    if _onboarding_daily_call_day != today:
        _onboarding_daily_call_day = today
        _onboarding_daily_call_count = 0

    return _onboarding_daily_call_count < settings.ONBOARDING_PARSER_MAX_CALLS_PER_DAY


async def _extract_via_gemini(text: str) -> Optional[RegistrationData]:
    """Call the Gemini text model and return parsed fields, or None on failure.

    Uses the same ``google-genai`` client convention as the existing
    ``GeminiImageProvider``. Returns None whenever Gemini is unavailable so
    the caller can fall back to deterministic extraction.
    """
    if not settings.GEMINI_API_KEY:
        logger.info("Onboarding: GEMINI_API_KEY not configured — using fallback parser")
        return None

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        logger.warning(
            "Onboarding: google-genai package not installed — using fallback parser"
        )
        return None

    if not _onboarding_budget_ok():
        logger.warning(
            "Onboarding: Gemini parser skipped (daily budget spent or quota "
            "cooldown active) -- using fallback parser"
        )
        return None

    model_name = settings.ONBOARDING_PARSER_MODEL or settings.GEMINI_MODEL

    global _onboarding_daily_call_count
    _onboarding_daily_call_count += 1

    try:
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        config = types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
        )

        # Mirror GeminiImageProvider: run the sync SDK call in a thread.
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model_name,
            contents=(
                f"{PARSER_SYSTEM_PROMPT}\n\n"
                f"Customer registration message:\n---\n{text}\n---\n"
                "Return the strict JSON object now."
            ),
            config=config,
        )

        payload = _safe_json_loads(getattr(response, "text", None))
        if not payload:
            logger.warning("Onboarding: Gemini parser returned no usable JSON")
            return None

        parsed = _coerce_llm_payload(payload, text)
        logger.info(
            f"Onboarding: Gemini parser extracted fields "
            f"missing={parsed.missing_fields} complete={parsed.is_complete}"
        )
        return parsed

    except Exception as e:
        # Never log tokens/secrets — only the error summary.
        logger.error(f"Onboarding: Gemini parser failed: {e}")
        if _is_onboarding_quota_exhaustion(str(e)):
            global _onboarding_quota_cooldown_until
            _onboarding_quota_cooldown_until = time.time() + settings.ONBOARDING_PARSER_QUOTA_COOLDOWN_SECONDS
            logger.error(
                "Onboarding: Gemini quota/rate exhaustion detected — pausing "
                f"parser calls for {settings.ONBOARDING_PARSER_QUOTA_COOLDOWN_SECONDS}s"
            )
        return None


async def parse_registration_text(text: str) -> RegistrationData:
    """Extract strict registration fields from free-form WhatsApp text.

    Returns a ``RegistrationData`` whose ``is_complete`` is derived from the
    four required fields. Raises ``RegistrationParserError`` only when the
    message cannot be parsed at all (empty text or an unexpected fault).
    """
    cleaned = sanitize_text(text)
    if not cleaned:
        raise RegistrationParserError("empty registration text")

    parsed = await _extract_via_gemini(cleaned)
    if parsed is not None:
        return parsed

    return _heuristic_extract(cleaned)


# ─── Registration data merging ───────────────────────────────────────────


def merge_registration_data(
    pending: RegistrationData,
    incoming: RegistrationData,
) -> RegistrationData:
    """Merge newly parsed fields with the fields already captured.

    Already-supplied values are preserved; newly supplied values win (so a
    customer can also correct a detail). Nothing is ever invented or
    carried over from another field.
    """
    merged = RegistrationData()
    for name in REQUIRED_FIELDS:
        new_value = getattr(incoming, name, "")
        old_value = getattr(pending, name, "")
        setattr(merged, name, new_value or old_value)
    return merged.recompute_completeness()


# ─── Message builders ────────────────────────────────────────────────────


def build_missing_fields_message(data: RegistrationData) -> str:
    """Ask ONLY for the fields that are still missing."""
    missing = data.missing_fields
    bullets = "\n".join(f"• {FIELD_LABELS[name]}" for name in missing)
    return (
        f"{INCOMPLETE_REGISTRATION_HEADER}\n\n"
        f"I still need:\n{bullets}\n\n"
        "Please send those details and I’ll complete your registration."
    )


def build_congratulations_message(full_name: str) -> str:
    """Build the exact congratulations message using the customer's first name."""
    first_name = (full_name or "").strip().split(" ")[0] or "there"
    return CONGRATULATIONS_TEMPLATE.format(first_name=first_name)


def is_greeting(text: str) -> bool:
    """True when the whole message is a greeting / start command.

    Deliberately anchored: "Hi, my name is Ravi and I have a jewellery shop" is
    a message with content, not a bare greeting.
    """
    cleaned = sanitize_text(text)
    if not cleaned:
        return False
    return bool(_GREETING_REGEX.match(cleaned))


# ─── Customer identification ─────────────────────────────────────────────


def _customer_repo(db: Session) -> BaseRepository:
    return BaseRepository(Customer, db)


def is_registered(whatsapp_id: str, db: Optional[Session] = None) -> bool:
    """Return True when a customer record exists for this WhatsApp ID.

    Never creates or mutates records. ``db`` is optional so the function can
    be used standalone; the session is closed when the caller did not supply
    one.
    """
    if not whatsapp_id:
        return False

    if db is not None:
        return _lookup_customer(db, whatsapp_id) is not None

    session = SessionLocal()
    try:
        return _lookup_customer(session, whatsapp_id) is not None
    finally:
        session.close()


def _lookup_customer(db: Session, whatsapp_id: str) -> Optional[Customer]:
    """Look up a customer by WhatsApp ID (parameterised ORM query)."""
    return _customer_repo(db).find_first(whatsapp_id=whatsapp_id)


def create_or_update_customer(
    db: Session,
    whatsapp_id: str,
    data: RegistrationData,
) -> Optional[Customer]:
    """Idempotently create (or refresh) the customer for this WhatsApp ID.

    A duplicate registration can never create a second row: the existing
    record is updated instead. The unique constraint on ``whatsapp_id`` is
    the final guard if two webhook deliveries race.
    """
    if not data.is_complete:
        return None

    existing = _lookup_customer(db, whatsapp_id)

    if existing is not None:
        try:
            existing.full_name = data.full_name
            existing.business_name = data.business_name
            existing.gst_number = data.gst_number
            existing.address = data.address
            db.commit()
            db.refresh(existing)
            logger.info(
                f"Onboarding: existing customer updated (no duplicate created) "
                f"whatsapp_id={whatsapp_id}"
            )
            return existing
        except Exception as e:
            db.rollback()
            logger.error(f"Onboarding: customer update failed: {e}")
            return None

    try:
        customer = _customer_repo(db).create(
            whatsapp_id=whatsapp_id,
            full_name=data.full_name,
            business_name=data.business_name,
            gst_number=data.gst_number,
            address=data.address,
            # New customers start with an empty wallet (Scenario 1). The
            # balance is never reset when an existing profile is updated.
            wallet_balance=0,
            is_registered=True,
        )
        logger.info(f"Onboarding: customer created whatsapp_id={whatsapp_id}")
        return customer
    except IntegrityError:
        # Concurrent duplicate delivery — return the existing record.
        db.rollback()
        existing = _lookup_customer(db, whatsapp_id)
        logger.info(
            f"Onboarding: concurrent duplicate registration resolved to existing "
            f"customer whatsapp_id={whatsapp_id}"
        )
        return existing
    except Exception as e:
        db.rollback()
        logger.error(f"Onboarding: customer creation failed: {e}")
        return None


# ─── Onboarding session state ────────────────────────────────────────────


def _session_repo(db: Session) -> BaseRepository:
    return BaseRepository(OnboardingSession, db)


def get_or_create_session(db: Session, whatsapp_id: str) -> Optional[OnboardingSession]:
    """Fetch (or create) the onboarding session row for a WhatsApp ID."""
    try:
        session_row = _session_repo(db).find_first(whatsapp_id=whatsapp_id)
        if session_row is not None:
            return session_row

        return _session_repo(db).create(
            whatsapp_id=whatsapp_id,
            state=OnboardingState.NOT_REGISTERED.value,
        )
    except IntegrityError:
        db.rollback()
        return _session_repo(db).find_first(whatsapp_id=whatsapp_id)
    except Exception as e:
        db.rollback()
        logger.error(f"Onboarding: session lookup failed: {e}")
        return None


def set_onboarding_state(db: Session, whatsapp_id: str, state: OnboardingState) -> None:
    """Persist the onboarding state for a WhatsApp ID (best effort)."""
    session_row = get_or_create_session(db, whatsapp_id)
    if session_row is None:
        return

    try:
        session_row.state = state.value
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Onboarding: failed to persist state '{state.value}': {e}")


def get_onboarding_state(db: Session, whatsapp_id: str) -> OnboardingState:
    """Read the stored onboarding state (defaults to NOT_REGISTERED)."""
    if is_registered(whatsapp_id, db):
        return OnboardingState.REGISTERED

    session_row = get_or_create_session(db, whatsapp_id)
    if session_row is None:
        return OnboardingState.NOT_REGISTERED

    try:
        return OnboardingState(session_row.state)
    except ValueError:
        logger.warning(
            f"Onboarding: unknown stored state '{session_row.state}' — "
            f"defaulting to NOT_REGISTERED"
        )
        return OnboardingState.NOT_REGISTERED


def _load_pending(db: Session, whatsapp_id: str) -> RegistrationData:
    """Load partially captured registration fields (nothing is lost)."""
    session_row = _session_repo(db).find_first(whatsapp_id=whatsapp_id)
    if not session_row or not session_row.pending_data:
        return RegistrationData()

    try:
        payload = json.loads(session_row.pending_data)
    except Exception as e:
        logger.error(f"Onboarding: pending registration data unreadable: {e}")
        return RegistrationData()

    if not isinstance(payload, dict):
        return RegistrationData()

    return RegistrationData(
        full_name=_clean_value(payload.get("full_name")),
        business_name=_clean_value(payload.get("business_name")),
        gst_number=normalize_gst_number(payload.get("gst_number")),
        address=_clean_value(payload.get("address")),
    ).recompute_completeness()


def _save_pending(db: Session, whatsapp_id: str, data: RegistrationData) -> None:
    """Persist partially captured fields so the customer is not asked twice."""
    session_row = get_or_create_session(db, whatsapp_id)
    if session_row is None:
        return

    try:
        session_row.pending_data = json.dumps(
            {name: getattr(data, name) for name in REQUIRED_FIELDS},
            ensure_ascii=False,
        )
        session_row.state = OnboardingState.AWAITING_REGISTRATION.value
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Onboarding: failed to persist pending registration data: {e}")


# ─── Outbound message helpers (never raise) ──────────────────────────────


async def _safe_send_text(whatsapp_id: str, text: str) -> bool:
    """Send a plain text message; failures are logged, never raised."""
    try:
        return await send_text_message(whatsapp_id, text)
    except Exception as e:
        logger.error(f"Onboarding: text message send failed: {e}")
        return False


async def _safe_send_cta(
    whatsapp_id: str,
    body_text: str,
    button_id: str,
    button_title: str,
) -> bool:
    """Send the interactive CTA button; failures are logged, never raised."""
    try:
        return await send_interactive_cta_button(
            whatsapp_id,
            body_text,
            button_id,
            button_title,
        )
    except Exception as e:
        logger.error(f"Onboarding: CTA button send failed: {e}")
        return False


async def _safe_send_cta_url(
    whatsapp_id: str,
    body_text: str,
    button_label: str,
    url: str,
) -> bool:
    """Send the CTA-URL button; failures are logged, never raised."""
    try:
        return await send_whatsapp_cta_url_button(
            whatsapp_id,
            body_text,
            button_label,
            url,
        )
    except Exception as e:
        logger.error(f"Onboarding: CTA URL button send failed: {e}")
        return False


async def send_whatsapp_message(whatsapp_id: str, text: str) -> bool:
    """Public, never-raising plain-text send used across the journey."""
    return await _safe_send_text(whatsapp_id, text)


async def send_recharge_cta(
    whatsapp_id: str,
    body_text: Optional[str] = None,
    amount_rupees: Optional[int] = None,
) -> bool:
    """Send the recharge call-to-action used in Scenarios 1, 2 and 3.

    When ``RECHARGE_PAYMENT_URL`` is configured (a PSP-hosted payment page such
    as a Razorpay link) a CTA-URL button labelled "Pay ₹<amount>" is sent.
    Otherwise the journey falls back to the interactive reply button
    (``recharge_500`` / "💳 Recharge to use") that Scenario 1 shipped with —
    no payment gateway credential or SDK is required either way.
    """
    body = body_text or RECHARGE_CTA_BODY
    amount = (
        int(amount_rupees)
        if amount_rupees is not None
        else int(settings.WALLET_IMAGE_PRICE_RUPEES)
    )
    payment_url = (settings.RECHARGE_PAYMENT_URL or "").strip()

    if payment_url:
        sent = await _safe_send_cta_url(
            whatsapp_id, body, f"Pay ₹{max(amount, 0):,}", payment_url
        )
        if sent:
            return True
        logger.warning(
            "Onboarding: payment CTA URL button failed — falling back to the "
            "recharge reply button"
        )

    return await _safe_send_cta(
        whatsapp_id, body, RECHARGE_BUTTON_ID, RECHARGE_BUTTON_TITLE
    )


async def send_registration_prompt(
    whatsapp_id: str,
    db: Optional[Session] = None,
) -> bool:
    """Send the welcome + registration instructions and set AWAITING state.

    Reused by the wallet gate when an unregistered number sends an image, so a
    customer always sees one consistent onboarding entry point.
    """
    if not whatsapp_id:
        return False

    own_session = db is None
    session = db or SessionLocal()
    try:
        result = await _start_onboarding(session, whatsapp_id)
        return bool(result.handled)
    except Exception as e:
        logger.error(f"Onboarding: could not send registration prompt: {e}")
        return False
    finally:
        if own_session:
            session.close()


# ─── Flow handlers ───────────────────────────────────────────────────────


async def _start_onboarding(db: Session, whatsapp_id: str) -> OnboardingResult:
    """Send the welcome + registration instructions and await details."""
    set_onboarding_state(db, whatsapp_id, OnboardingState.AWAITING_REGISTRATION)

    await _safe_send_text(whatsapp_id, WELCOME_MESSAGE)
    await _safe_send_text(whatsapp_id, REGISTRATION_INSTRUCTIONS_MESSAGE)

    logger.info(f"Onboarding: welcome + instructions sent to {whatsapp_id}")
    return OnboardingResult(handled=True, response={"status": "onboarding_started"})


async def _handle_registration_input(
    db: Session,
    whatsapp_id: str,
    text: str,
) -> OnboardingResult:
    """Parse registration input, ask for gaps, or complete registration."""
    try:
        parsed = await parse_registration_text(text)
    except RegistrationParserError as e:
        logger.warning(f"Onboarding: registration parser rejected input: {e}")
        await _safe_send_text(whatsapp_id, PARSER_FAILURE_MESSAGE)
        return OnboardingResult(
            handled=True, response={"status": "onboarding_parse_failed"}
        )
    except Exception as e:
        # Unexpected parser fault — safe log, friendly retry, no internals.
        logger.error(f"Onboarding: registration parsing failed: {e}")
        await _safe_send_text(whatsapp_id, PARSER_FAILURE_MESSAGE)
        return OnboardingResult(
            handled=True, response={"status": "onboarding_parse_failed"}
        )

    pending = _load_pending(db, whatsapp_id)
    merged = merge_registration_data(pending, parsed)

    if not merged.is_complete:
        _save_pending(db, whatsapp_id, merged)
        await _safe_send_text(whatsapp_id, build_missing_fields_message(merged))
        logger.info(
            f"Onboarding: awaiting registration fields "
            f"missing={merged.missing_fields} whatsapp_id={whatsapp_id}"
        )
        return OnboardingResult(
            handled=True,
            response={"status": "onboarding_incomplete", "missing": merged.missing_fields},
        )

    customer = create_or_update_customer(db, whatsapp_id, merged)
    if customer is None:
        # Persistence failed — do NOT congratulate; ask the customer to retry
        # without exposing any internal detail.
        logger.error(
            f"Onboarding: could not persist customer for whatsapp_id={whatsapp_id}"
        )
        await _safe_send_text(whatsapp_id, RETRY_MESSAGE)
        return OnboardingResult(
            handled=True, response={"status": "onboarding_persist_failed"}
        )

    set_onboarding_state(db, whatsapp_id, OnboardingState.REGISTERED)

    text_ok = await _safe_send_text(
        whatsapp_id, build_congratulations_message(customer.full_name)
    )
    button_ok = await send_recharge_cta(whatsapp_id)

    if not text_ok or not button_ok:
        logger.warning(
            f"Onboarding: registration completed but a message failed to send "
            f"whatsapp_id={whatsapp_id} text_ok={text_ok} button_ok={button_ok}"
        )

    logger.info(f"Onboarding: registration complete whatsapp_id={whatsapp_id}")
    return OnboardingResult(
        handled=True,
        response={
            "status": "onboarding_completed",
            "customer_id": customer.id,
        },
    )


# ─── Public entry point (called by the webhook) ──────────────────────────


async def handle_text(
    whatsapp_id: str,
    text: str,
    db: Optional[Session] = None,
) -> OnboardingResult:
    """Handle one inbound WhatsApp text message for onboarding.

    Returns an ``OnboardingResult``:
        handled=True  — onboarding consumed the message; the caller must NOT
                        pass it to any other handler
        handled=False — the message is not ours: the existing WhatsApp
                        behaviour must continue unchanged

    This function NEVER raises, even when the feature flag is on.
    """
    if not settings.ENABLE_ONBOARDING_GATE:
        # Feature disabled: existing behaviour is untouched.
        return OnboardingResult(handled=False)

    if not whatsapp_id:
        logger.warning("Onboarding: text message without sender — skipped")
        return OnboardingResult(handled=False)

    cleaned = sanitize_text(text)
    if not cleaned:
        return OnboardingResult(handled=False)

    own_session = db is None
    session = db or SessionLocal()

    try:
        # ── Existing registered users always bypass onboarding ─────────
        if is_registered(whatsapp_id, session):
            logger.info(
                f"Onboarding: registered user {whatsapp_id} bypassed onboarding"
            )
            return OnboardingResult(handled=False)

        state = get_onboarding_state(session, whatsapp_id)

        if state is OnboardingState.REGISTERED:
            # Session mirror says registered but no customer row exists —
            # recover by treating the user as new rather than blocking them.
            logger.warning(
                f"Onboarding: state REGISTERED without customer row "
                f"whatsapp_id={whatsapp_id} — recovering"
            )
            state = OnboardingState.NOT_REGISTERED

        if is_greeting(cleaned):
            # Greeting / "start": (re)send the welcome journey even if the
            # user is mid-registration, so the instructions are always at hand.
            return await _start_onboarding(session, whatsapp_id)

        if state is OnboardingState.AWAITING_REGISTRATION:
            # The user has already been asked for their details, so this
            # message is registration input (parsed conservatively, merged
            # with anything already captured).
            return await _handle_registration_input(session, whatsapp_id, cleaned)

        # New / unknown user: the FIRST ordinary text message ALWAYS starts the
        # documented onboarding journey (welcome → instructions →
        # AWAITING_REGISTRATION). Registration data is never parsed from a
        # first message, so casual prose can never become customer data.
        return await _start_onboarding(session, whatsapp_id)

    except Exception as e:
        # Onboarding must never break the WhatsApp webhook.
        logger.error(f"Onboarding: unexpected error for whatsapp_id={whatsapp_id}: {e}")
        try:
            session.rollback()
        except Exception:
            pass
        return OnboardingResult(handled=False)
    finally:
        if own_session:
            session.close()
