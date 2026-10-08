#!/usr/bin/env python3
"""Change the price of one SKU (white-background pack shot, GST included), with no code change or restart.

Usage (from backend/):   python scripts/set_price.py --sku-price 25 [--by "Owner name"] [--dry-run]

The new price is stored in the database (price_settings) with who changed it and when, and an audit row keeps the old
and new price. New payment links use it from that moment; packs already paid keep the credits they bought. When
META_CATALOG_ID and META_WHATSAPP_TOKEN are set, the pack prices in the WhatsApp catalogue are updated too; otherwise
the one manual step is printed.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sku-price", type=int, required=True, help="whole rupees per SKU, GST included")
    parser.add_argument("--by", default=None, help="who is changing it (default: this computer's user name)")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    args = parser.parse_args(argv)
    if args.sku_price < 1:
        print("The price must be at least 1 rupee.")
        return 2

    from app.database import SessionLocal
    from app.services import pricing

    by = (args.by or getpass.getuser() or "script").strip()
    with SessionLocal() as db:
        current = pricing.sku_price(db)
        print(f"Price per SKU: {current} -> {args.sku_price} rupees (GST included)")
        for size in pricing.pack_sizes():
            print(f"  {pricing.pack_title(size)}: {size * args.sku_price} rupees")
        if args.dry_run:
            print("Dry run: nothing changed.")
            return 0
        pricing.set_sku_price(db, args.sku_price, by)
    print("Saved. New payment links use this price from now on.")
    if pricing.push_catalogue_prices(args.sku_price):
        print("WhatsApp catalogue updated.")
    else:
        print("WhatsApp catalogue NOT updated automatically: set the pack prices above in Commerce Manager "
              "(or set META_CATALOG_ID and META_WHATSAPP_TOKEN and run this again).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
