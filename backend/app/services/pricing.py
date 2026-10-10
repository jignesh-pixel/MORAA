"""The one place that knows prices (Phase 8).

Every rupee amount the backend charges, quotes or invoices comes from here: the per-SKU pack price (database row in
``price_settings``, seeded from ``SKU_PRICE_RUPEES``), pack totals, the GST split for invoices, the wallet product
prices and recharge limits (settings), and the text that shows them. ``tests/test_no_hardcoded_rupees.py`` fails the
build if another file hard-codes a rupee amount.

Prices are whole rupees, GST included, everywhere except ``gst_split`` (paise precision for invoices).
"""

import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.models.sku_credit import PRICE_KEY_SKU, SKU_CREATIVE, SKU_WHITE_BG, PriceSetting
from app.utils.logger import logger

# Retailer ids in the Meta catalogue (Commerce Manager). Studio Shot tiers ("studio_sku_20") buy white-background
# SKUs; Catalog Pack tiers ("sku_pack_20") buy Catalog Pack SKUs. Each tier is a pack size from SKU_PACK_SIZES.
STUDIO_RETAILER_PREFIX = "studio_sku_"
CATALOG_RETAILER_PREFIX = "sku_pack_"
# The first catalogue's Studio Shot ids ("pack_20"): still read in carts and list replies sent before the switch.
PACK_RETAILER_PREFIX = "pack_"
# Studio Shot tiers are catalogue items whose retailer id is the Meta "content id": pack size -> id. Lower case,
# because parse_retailer_id compares lower-cased ids. Catalog Pack ids (sku_pack_N) are NOT mapped.
STUDIO_CONTENT_IDS = {
    1: "cuye50mhk4",
    5: "jqlyripyx9",
    20: "dnv3cpdm89",
    50: "weime1udtl",
    100: "ufd9yt7y6f",
}
_STUDIO_UNITS_BY_CONTENT_ID = {content_id: units for units, content_id in STUDIO_CONTENT_IDS.items()}
PAISE_PER_RUPEE = 100
PRICE_CHANGED_ACTION = "price_changed"


def _stored_price(key: str, db: Any = None) -> Optional[int]:
    """The price saved in price_settings, or None (no row, or the database cannot answer)."""
    try:
        if db is not None:
            value = db.query(PriceSetting.price_rupees).filter(PriceSetting.sku == key).scalar()
        else:
            from app.database import SessionLocal

            with SessionLocal() as session:
                value = session.query(PriceSetting.price_rupees).filter(PriceSetting.sku == key).scalar()
    except Exception as e:  # noqa: BLE001 -- a price lookup must never break a chat; the configured price is used
        logger.warning(f"Stored price lookup failed ({type(e).__name__}); using the configured price")
        if db is not None:
            db.rollback()
        return None
    return int(value) if value is not None else None


# ── SKU packs ─────────────────────────────────────────────────────────────────────────────────────────────

def sku_price(db: Any = None) -> int:
    """Price of one SKU in whole rupees, GST included: the stored price, else SKU_PRICE_RUPEES."""
    stored = _stored_price(PRICE_KEY_SKU, db)
    return stored if stored is not None and stored > 0 else max(int(settings.SKU_PRICE_RUPEES), 1)


def pack_sizes() -> List[int]:
    """Pack sizes that can be bought (SKU_PACK_SIZES), smallest first."""
    sizes = set()
    for part in str(settings.SKU_PACK_SIZES or "").split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            sizes.add(int(part))
    return sorted(sizes)


def menu_pack_sizes() -> List[int]:
    """Pack sizes shown in menus: every size, except the 1-SKU pack unless ECOM_PACK1_ENABLED."""
    return [s for s in pack_sizes() if s != 1 or settings.ECOM_PACK1_ENABLED]


def is_pack_size(units: Any) -> bool:
    return isinstance(units, int) and not isinstance(units, bool) and units in pack_sizes()


def pack_total(units: int, db: Any = None) -> int:
    """Rupees for ``units`` SKUs at today's price (GST included)."""
    return max(int(units), 0) * sku_price(db)


def pack_retailer_id(units: int) -> str:
    """Studio Shot tier id in the catalogue: its Meta content id (``dnv3cpdm89`` for 20), or ``studio_sku_N`` for a
    pack size without a content id."""
    return STUDIO_CONTENT_IDS.get(int(units)) or f"{STUDIO_RETAILER_PREFIX}{int(units)}"


def catalog_retailer_id(units: int) -> str:
    """Catalog Pack tier id in the catalogue: ``sku_pack_20``."""
    return f"{CATALOG_RETAILER_PREFIX}{int(units)}"


def _tier_units(text: str, prefix: str) -> Optional[int]:
    number = text[len(prefix):] if text.startswith(prefix) else ""
    return int(number) if number.isdigit() and is_pack_size(int(number)) else None


def parse_retailer_id(retailer_id: Any) -> Optional[Tuple[str, int]]:
    """A Studio Shot content id (``dnv3cpdm89``) or ``studio_sku_20`` -> (white_bg, 20), ``sku_pack_5`` -> (creative_pack, 5); None for anything else (or a size
    that is not a pack size). Prices are never taken from Meta: only the id is used."""
    text = str(retailer_id or "").strip().lower()
    content_units = _STUDIO_UNITS_BY_CONTENT_ID.get(text)
    if content_units and is_pack_size(content_units):
        return SKU_WHITE_BG, content_units
    for prefix, sku in ((STUDIO_RETAILER_PREFIX, SKU_WHITE_BG), (CATALOG_RETAILER_PREFIX, SKU_CREATIVE),
                        (PACK_RETAILER_PREFIX, SKU_WHITE_BG)):
        units = _tier_units(text, prefix)
        if units:
            return sku, units
    return None


def units_for_retailer_id(retailer_id: Any) -> Optional[int]:
    """White-background SKUs of a Studio Shot tier id (``studio_sku_20`` -> 20), else None."""
    parsed = parse_retailer_id(retailer_id)
    return parsed[1] if parsed and parsed[0] == SKU_WHITE_BG else None


# Catalog Pack (Ecomm Pack 1): one Catalog Pack SKU = one photo's full-style catalogue pack, sold in the same tiers.
STUDIO_TITLE = "Studio Shot"
CREATIVE_TITLE = "Catalog Pack"
MAX_CART_UNITS = 10000


def creative_pack_price() -> int:
    """One Catalog Pack SKU (sku_pack_N tiers cost N x this), GST included: CATALOG_PACK_SKU_PRICE."""
    return max(int(settings.CATALOG_PACK_SKU_PRICE), 1)


def quote_cart(items: Any, db: Any = None) -> Dict[str, Any]:
    """Price a WhatsApp catalogue cart on the server (Meta's prices are never used).

    ``items`` = [{"retailer_id", "quantity"}]. studio_sku_N items add N white-background SKUs per quantity,
    sku_pack_N items add N Catalog Pack SKUs; unknown ids and quantities that are not positive whole numbers are
    skipped, and each total is capped at MAX_CART_UNITS. Returns {"white_units", "creative_packs", "white_total", "creative_total",
    "total", "lines": [{"retailer_id", "quantity"}]}."""
    white = creative = 0
    lines: List[Dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        quantity = item.get("quantity")
        if isinstance(quantity, str) and quantity.strip().isdecimal():
            quantity = int(quantity)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            continue
        retailer_id = str(item.get("retailer_id") or "").strip().lower()
        parsed = parse_retailer_id(retailer_id)
        if parsed is None:
            continue
        sku, size = parsed
        if sku == SKU_WHITE_BG:
            white += size * quantity
        else:
            creative += size * quantity
        lines.append({"retailer_id": retailer_id, "quantity": quantity})
    white, creative = min(white, MAX_CART_UNITS), min(creative, MAX_CART_UNITS)
    white_total = pack_total(white, db) if white else 0
    creative_total = creative * creative_pack_price() if creative else 0
    return {"white_units": white, "creative_packs": creative, "white_total": white_total,
            "creative_total": creative_total, "total": white_total + creative_total, "lines": lines}


def pack_title(units: int) -> str:
    """"20 SKUs" (fits a WhatsApp list row title, 24 characters)."""
    return f"{int(units)} SKU" + ("" if int(units) == 1 else "s")


def pack_description(units: int, db: Any = None) -> str:
    """"20 white-background shots · ₹400" (a WhatsApp list row description, 72 characters)."""
    shots = "shot" if int(units) == 1 else "shots"
    return f"{int(units)} white-background {shots} · {format_rupees(pack_total(units, db))}"


def credit_value(db: Any = None) -> int:
    """What one credit is worth in rupees (for the dashboard and reconciliation)."""
    return sku_price(db)


# ── GST and invoices ──────────────────────────────────────────────────────────────────────────────────────

def gst_split(total_rupees: Any) -> Tuple[Decimal, Decimal]:
    """Split a GST-inclusive amount into (net, tax), in rupees with paise. net + tax == total exactly."""
    total = Decimal(str(total_rupees)).quantize(Decimal("0.01"))
    rate = Decimal(max(int(settings.SKU_GST_PERCENT), 0))
    net = (total * Decimal(100) / (Decimal(100) + rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return net, total - net


def erpnext_pack_line(units: int, unit_price: int) -> Dict[str, Any]:
    """ERPNext invoice line for a pack: quantity = SKUs, rate = the price per SKU that was paid.

    With ERPNEXT_PRICES_INCLUDE_TAX the GST-inclusive rate is sent and ERPNext splits the tax; otherwise the net rate
    is sent and ERPNext adds the tax lines from the template."""
    rate = Decimal(int(unit_price))
    if not settings.ERPNEXT_PRICES_INCLUDE_TAX:
        rate = gst_split(rate)[0]
    item_code = (settings.SKU_ERPNEXT_ITEM_CODE or settings.ERPNEXT_RECHARGE_ITEM_CODE or "").strip()
    return {"item_code": item_code, "qty": int(units), "rate": float(rate)}


# ── Wallet products and recharges (unchanged amounts, now read from one place) ────────────────────────────

def white_bg_price() -> int:
    """Clean Studio Shot paid from the wallet."""
    return max(int(settings.WHITE_BG_PRICE_RUPEES), 1)


def catalog_pack_price() -> int:
    """Full Catalog Pack paid from the wallet."""
    return max(int(settings.WALLET_IMAGE_PRICE_RUPEES), 1)


def min_recharge() -> int:
    return max(int(settings.MIN_RECHARGE_RUPEES), 1)


def max_recharge() -> int:
    return max(int(settings.MAX_RECHARGE_RUPEES), min_recharge())


def format_rupees(amount: Any) -> str:
    """₹1,000 (whole rupees, never negative)."""
    return f"₹{max(int(amount), 0):,}"


# ── Changing the price ────────────────────────────────────────────────────────────────────────────────────

def set_sku_price(db: Any, price: int, changed_by: str) -> Tuple[int, int]:
    """Store a new per-SKU price (from this moment) and an audit row with the old and new price. Commits.

    Returns (old price, new price). Packs already paid keep the credits they bought."""
    from app.models.audit_log import AuditLog

    price = int(price)
    if price < 1:
        raise ValueError("The SKU price must be at least 1 rupee")
    old = sku_price(db)
    now = datetime.now(timezone.utc)
    row = db.get(PriceSetting, PRICE_KEY_SKU)
    if row is None:
        db.add(PriceSetting(sku=PRICE_KEY_SKU, price_rupees=price, effective_from=now, changed_by=changed_by[:100]))
    else:
        row.price_rupees, row.effective_from, row.changed_by = price, now, changed_by[:100]
    db.add(AuditLog(
        action=PRICE_CHANGED_ACTION, resource_type="price_setting", resource_id=PRICE_KEY_SKU, status="success",
        details=json.dumps({"key": PRICE_KEY_SKU, "old": old, "new": price, "by": changed_by[:100],
                            "effective_from": now.isoformat()}),
    ))
    db.commit()
    return old, price


def catalogue_requests(unit_price: int, catalog_unit_price: Optional[int] = None) -> List[Dict[str, Any]]:
    """Graph API batch requests that set every tier's catalogue price (paise, INR) by its retailer id: Studio Shot
    tiers at ``unit_price`` per SKU, Catalog Pack tiers at ``catalog_unit_price`` (default CATALOG_PACK_SKU_PRICE)."""
    catalog_unit = int(catalog_unit_price) if catalog_unit_price is not None else creative_pack_price()
    return [
        {"method": "UPDATE", "retailer_id": retailer_id(size),
         "data": {"price": size * int(price) * PAISE_PER_RUPEE, "currency": "INR",
                  "name": f"{title} {pack_title(size)}"}}
        for title, retailer_id, price in ((STUDIO_TITLE, pack_retailer_id, unit_price),
                                          (CREATIVE_TITLE, catalog_retailer_id, catalog_unit))
        for size in pack_sizes()
    ]


def push_catalogue_prices(unit_price: int, client: Any = None) -> bool:
    """Update the pack prices in the WhatsApp catalogue (META_CATALOG_ID). True when Meta accepted them; False when
    the catalogue is not configured or Meta refused (the caller tells the operator to change them by hand). Blocking."""
    import httpx

    catalog_id, token = settings.META_CATALOG_ID.strip(), settings.META_WHATSAPP_TOKEN.strip()
    if not catalog_id or not token:
        return False
    owns_client = client is None
    client = client or httpx.Client(timeout=20.0)
    try:
        response = client.post(
            f"https://graph.facebook.com/v21.0/{catalog_id}/batch",
            headers={"Authorization": f"Bearer {token}"},
            json={"requests": catalogue_requests(unit_price)},
        )
    except httpx.HTTPError as e:
        logger.error(f"Catalogue price update failed ({type(e).__name__})")
        return False
    finally:
        if owns_client:
            client.close()
    if response.status_code != 200:
        logger.error(f"Catalogue price update refused (HTTP {response.status_code})")
        return False
    return True


def price_summary(db: Any = None) -> Dict[str, Any]:
    """Prices for the dashboard and the catalogue sync."""
    unit, catalog_unit = sku_price(db), creative_pack_price()
    return {
        "sku_price": unit,
        "catalog_pack_sku_price": catalog_unit,
        "gst_percent": int(settings.SKU_GST_PERCENT),
        "packs": {size: size * unit for size in pack_sizes()},
        "catalog_packs": {size: size * catalog_unit for size in pack_sizes()},
        "menu_sizes": menu_pack_sizes(),
        "white_bg_price": white_bg_price(),
        "catalog_pack_price": catalog_pack_price(),
        "min_recharge": min_recharge(),
        "max_recharge": max_recharge(),
    }
