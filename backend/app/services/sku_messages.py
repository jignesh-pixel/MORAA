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


PAY_BUTTON = "💳 Pay via Razorpay"       # cart URL button, 20 characters at most


def pack_link_body(units: int, total: int, creative_packs: int = 0) -> str:
    """The cart summary sent with the single payment button."""
    lines = ["Your Cart is ready!", f"Total SKUs: {max(int(units), 0)}"]
    if creative_packs:
        lines.append(f"{pricing.CREATIVE_TITLE}: {int(creative_packs)}")
    lines += [f"Total Amount: {pricing.format_rupees(total)} (incl. GST)", "Click below to complete your payment:"]
    return "\n".join(lines)


def collections_body() -> str:
    """Shown after registration: the two collections and their prices."""
    return (
        "Choose a collection 👇\n\n"
        f"• E-commerce Only: plain white-background shots, {pricing.format_rupees(pricing.sku_price())} per SKU\n"
        f"• {pricing.CREATIVE_TITLE}: 7 jewellery photoshoot styles, "
        f"{pricing.format_rupees(pricing.creative_pack_price())}\n\n"
        "Tap View Collections, add packs to your cart, send the cart, and pay once."
    )


def creative_received(balance_left: int) -> str:
    return f"Photo received ✅ 1 {pricing.CREATIVE_TITLE} used, {max(int(balance_left), 0)} left."


def pack_link_unavailable() -> str:
    return "Sorry, we couldn't create your payment link right now. Please try again in a few minutes 🙏"


def photo_received(balance_left: int) -> str:
    return f"Photo received ✅ 1 SKU used, {max(int(balance_left), 0)} left."


def ready_message(link: str, balance_left: int) -> str:
    return f"Your images are ready! 📁 Download from your Drive: {link}. You have {_skus(balance_left)} left."


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
