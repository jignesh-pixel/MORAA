#!/usr/bin/env python3
"""Erase one customer's personal data by hand (the administrator's version of "DELETE MY DATA", PRIV-3).

Usage (from backend/):
    python scripts/erase_customer.py --phone 919812345678            # shows what WOULD be erased
    python scripts/erase_customer.py --phone 919812345678 --yes      # erases

Removes the customer's photos and replaces their name, business, GSTIN, address and phone number with placeholders.
The wallet ledger, payments, refunds and invoices are kept (statutory record keeping). Refuses a customer who still has
money in the wallet and team (ADMIN) accounts. Prints counts only, never the customer's details.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--phone", required=True)
    parser.add_argument("--yes", action="store_true", help="really erase (without it nothing changes)")
    args = parser.parse_args(argv)

    from app.database import SessionLocal
    from app.services import data_lifecycle as dl
    from app.services.wallet_service import find_customer_by_phone

    with SessionLocal() as db:
        customer = find_customer_by_phone(db, args.phone)
        if customer is None:
            print("No customer with that number.")
            return 1
        from app.models.whatsapp_ingestion import WhatsAppIngestion

        orders = db.query(WhatsAppIngestion).filter(WhatsAppIngestion.external_user_id == customer.whatsapp_id).count()
        print(f"Found one customer with {orders} order(s); wallet balance Rs {int(customer.wallet_balance or 0)}.")
        if not args.yes:
            print("Dry run: nothing was changed. Add --yes to erase.")
            return 0
        try:
            result = dl.erase_customer(db, customer)
        except dl.ErasureRefused as refused:
            print(f"Refused: {refused}")
            return 2
        print(f"Erased: {result['photos']} photo(s), {result['orders']} order record(s), {result['audit_rows']} audit row(s) masked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
