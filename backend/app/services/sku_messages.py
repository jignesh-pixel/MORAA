"""Customer-facing WhatsApp copy for SKU packs and Drive delivery (Phase 8).

One place for the words; every amount is formatted by ``pricing.format_rupees`` (no rupee figure is written here).
Interactive bodies stay well under Meta's 1024-character limit, list buttons under 20 characters.
"""

from app.services import pricing

PACK_MENU_BUTTON = "View packs"          # list-message button, 20 characters at most


def _skus(count: int) -> str:
    """"1 SKU" / "13 SKUs" (never negative)."""
    count = max(int(count), 0)
    return f"{count} SKU" + ("" if count == 1 else "s")


def pack_menu_body() -> str:
    return (
        "Choose a pack 👇\n\n"
        "1 SKU = 1 white-background product photo. Pay once, then send your photos here."
    )


PAY_BUTTON = "Place Order"               # cart URL button (opens the Razorpay payment), 20 characters at most


def pack_link_body(units: int, total: int, creative_packs: int = 0) -> str:
    """The cart summary sent with the single payment button. A cart with Catalog Pack SKUs lists both collections."""
    units, creative_packs = max(int(units), 0), max(int(creative_packs), 0)
    lines = ["Your Cart is ready!"]
    if creative_packs:
        if units:
            lines.append(f"{pricing.STUDIO_TITLE}: {_skus(units)}")
        lines.append(f"{pricing.CREATIVE_TITLE}: {_skus(creative_packs)}")
    lines += [f"Total SKUs: {units + creative_packs}",
              f"Total Amount: {pricing.format_rupees(total)} (incl. GST)", "Click below to complete your payment:"]
    return "\n".join(lines)


STUDIO_COLLECTION_TITLE = pricing.STUDIO_TITLE
CATALOG_COLLECTION_TITLE = pricing.CREATIVE_TITLE


def collections_body() -> str:
    """The "View Collections" list body: the two collections and their prices."""
    return (
        "Choose a collection 👇\n\n"
        f"1. {STUDIO_COLLECTION_TITLE}: white-background studio shots, "
        f"{pricing.format_rupees(pricing.sku_price())} per SKU\n"
        f"2. {CATALOG_COLLECTION_TITLE} (Ecomm Pack 1): 7 jewellery photoshoot styles per photo, "
        f"{pricing.format_rupees(pricing.creative_pack_price())} per SKU\n\n"
        "Tap View Collections, pick one and tap Send."
    )


def studio_collection_row(db=None) -> str:
    """List row description (72 characters at most)."""
    return f"White-background shots · {pricing.format_rupees(pricing.sku_price(db))} per SKU"


def catalog_collection_row() -> str:
    return f"Ecomm Pack 1 · 7 styles · {pricing.format_rupees(pricing.creative_pack_price())} per SKU"


def catalog_tier_description(units: int) -> str:
    """List row description of one Catalog Pack tier (72 characters at most)."""
    return f"{_skus(units)} of 7 photoshoot styles · {pricing.format_rupees(units * pricing.creative_pack_price())}"


def _tiers(unit_price: int) -> str:
    return "\n".join(
        f"• {pricing.pack_title(n)}: {pricing.format_rupees(n * unit_price)}" for n in pricing.menu_pack_sizes()
    )


PRODUCT_LIST_BODY = "Tap View items below to select your packs and add them to your cart."


def studio_tiers_body() -> str:
    """Body of the Studio Shot product list: the SKU tiers and their prices."""
    return (
        f"{_tiers(pricing.sku_price())}\n\n"
        "1 SKU = 1 white-background studio photo. Tap View items, set quantities with + / −, "
        "then send your cart."
    )


def catalog_pack_body() -> str:
    """Body of the Catalog Pack product list: the SKU tiers and their prices."""
    return (
        f"{_tiers(pricing.creative_pack_price())}\n\n"
        "1 SKU = 1 photo made in 7 jewellery photoshoot styles. Tap View items, set quantities with + / −, "
        "then send your cart."
    )


def creative_received(balance_left: int) -> str:
    return f"Photo received ✅ 1 {pricing.CREATIVE_TITLE} used, {max(int(balance_left), 0)} left."


def pack_link_unavailable() -> str:
    return "Sorry, we couldn't create your payment link right now. Please try again in a few minutes 🙏"


def photo_received(balance_left: int) -> str:
    return f"Photo received ✅ 1 SKU used, {max(int(balance_left), 0)} left."


def ready_message(link: str, balance_left: int = 0, name: str = "", email: str = "") -> str:
    """The batch-ready message: the customer's Drive folder link (also shared with their email address)."""
    customer = (name or "").strip() or "Customer"
    return (
        f"Dear {customer},\n\n"
        "Your batch photos are ready!\n"
        f"You can download them from this drive link: {link} which is also sent on your email id.\n\n"
        "Thanks for choosing Moraa Studio ✨"
    )


def held_photos_started(started: int, waiting: int, balance_left: int) -> str:
    """After a pack payment: the photos sent before paying are being made now."""
    text = f"✨ Processing your {started} photo{'' if started == 1 else 's'} now. {_skus(balance_left)} left."
    if waiting:
        text += (f"\n\n{waiting} more photo{'' if waiting == 1 else 's'} "
                 f"{'is' if waiting == 1 else 'are'} waiting for SKUs. Choose a pack to add more 👇")
    return text


def photos_held() -> str:
    """A photo from a customer without SKUs: kept, and made as soon as a pack is paid."""
    return "Photo received ✅ Choose a collection below; your photos are made as soon as your payment is complete."


def ask_email() -> str:
    return "Please send your email address (a Gmail address works best) so we can share your image folder with you 📁"


def buy_more(balance_left: int) -> str:
    if int(balance_left) <= 0:
        return "You've used all your SKUs. Choose a pack to keep going 👇"
    return f"You have {_skus(balance_left)} left. Choose a pack to add more 👇"


def order_unreadable() -> str:
    return "Sorry, we couldn't read that order. Please choose a pack from the list 👇"


def skus_left(count: int) -> str:
    return f"{_skus(count)} left"
