"""One rule for comparing phone numbers (MON-5).

Numbers are compared in international format without the "+" (E.164 digits), e.g. ``919876543210``,
which is how WhatsApp sends them and how customers are stored. A number WITHOUT a country code
(10 digits, optionally with a leading 0) is taken to be Indian. A number WITH a country code must
match exactly: a foreign number that merely ends in the same 10 digits is a different person.
"""

import re

INDIA_CODE = "91"


def normalize_phone(raw: object) -> str:
    """Return the international digits of a phone number ("" when there are none).

    Values that are not phone numbers (they contain letters, e.g. a Razorpay ``cust_...`` id) are
    returned stripped and unchanged, so they are never turned into a lookalike number.
    """
    text = str(raw or "").strip()
    if re.search(r"[A-Za-z]", text):
        return text
    digits = re.sub(r"\D", "", text)
    if digits.startswith("00"):          # international dialling prefix: 0091...
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):   # national trunk prefix: 09876543210
        digits = digits[1:]
    if len(digits) == 13 and digits.startswith(INDIA_CODE + "0"):   # "+91 0 98765 43210"
        digits = INDIA_CODE + digits[3:]
    if len(digits) == 10:
        return INDIA_CODE + digits
    return digits


def is_plausible_phone(raw: object) -> bool:
    """True when the value looks like a real phone number (8 to 15 digits, no letters), per E.164."""
    text = str(raw or "").strip()
    if not text or re.search(r"[A-Za-z]", text):
        return False
    return 8 <= len(normalize_phone(text)) <= 15


def same_phone(a: object, b: object) -> bool:
    """True when two values are the same number after normalisation."""
    na, nb = normalize_phone(a), normalize_phone(b)
    return bool(na) and na == nb
