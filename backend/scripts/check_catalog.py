"""Read-only: compare the catalogue items the bot sends in its multi-product message with what the Meta catalogue holds.

    python scripts/check_catalog.py [--token ACCESS_TOKEN]

Needs a token with the ``catalog_management`` permission (a system user token of the Business that owns
META_CATALOG_ID); the WhatsApp-only META_WHATSAPP_TOKEN usually cannot read a catalogue. Prints, for every retailer id
the bot sends (``studio_sku_N`` and ``sku_pack_N``), whether it exists and whether WhatsApp can show it with the
"+" button: it needs a price, "in stock" availability, visibility "published" and an image. Writes nothing.
"""

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import pricing  # noqa: E402

GRAPH = "https://graph.facebook.com/v21.0"
FIELDS = "retailer_id,name,price,availability,visibility,review_status,image_url"


def fetch_products(catalog_id: str, token: str) -> List[Dict[str, Any]]:
    """Every product of the catalogue (follows paging). Raises RuntimeError with Meta's message on refusal."""
    products: List[Dict[str, Any]] = []
    url, params = f"{GRAPH}/{catalog_id}/products", {"fields": FIELDS, "limit": 200}
    with httpx.Client(timeout=30.0, headers={"Authorization": f"Bearer {token}"}) as client:
        while url:
            response = client.get(url, params=params)
            body = response.json()
            if response.status_code != 200:
                raise RuntimeError((body.get("error") or {}).get("message") or f"HTTP {response.status_code}")
            products += body.get("data", [])
            url, params = (body.get("paging") or {}).get("next"), None
    return products


def problems(product: Dict[str, Any]) -> List[str]:
    found = []
    if not product.get("price"):
        found.append("no price")
    if product.get("availability") != "in stock":
        found.append(f"availability={product.get('availability')}")
    if product.get("visibility") != "published":
        found.append(f"visibility={product.get('visibility')}")
    if not product.get("image_url"):
        found.append("no image")
    if product.get("review_status") not in (None, "approved"):
        found.append(f"review={product.get('review_status')}")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--token", default="", help="catalogue access token (default: META_WHATSAPP_TOKEN)")
    args = parser.parse_args()
    catalog_id = (settings.META_CATALOG_ID or "").strip()
    token = (args.token or settings.META_WHATSAPP_TOKEN or "").strip()
    if not catalog_id or not token:
        print("META_CATALOG_ID and a token are required")
        return 2
    try:
        by_id = {str(p.get("retailer_id")): p for p in fetch_products(catalog_id, token)}
    except RuntimeError as e:
        print(f"Could not read catalogue {catalog_id}: {e}\nUse a token with catalog_management (--token).")
        return 2
    print(f"Catalogue {catalog_id}: {len(by_id)} products")
    bad = 0
    for title, make in ((pricing.STUDIO_TITLE, pricing.pack_retailer_id),
                        (pricing.CREATIVE_TITLE, pricing.catalog_retailer_id)):
        for size in pricing.menu_pack_sizes():
            rid = make(size)
            product = by_id.get(rid)
            issue = ["MISSING from catalogue"] if product is None else problems(product)
            bad += bool(issue)
            print(f"  {title:<13} {rid:<16} {'OK' if not issue else ', '.join(issue)}")
    extras = sorted(rid for rid in by_id if not rid.startswith((pricing.STUDIO_RETAILER_PREFIX,
                                                                pricing.CATALOG_RETAILER_PREFIX)))
    if extras:
        print(f"Other retailer ids in the catalogue (not sent by the bot): {', '.join(extras[:20])}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
