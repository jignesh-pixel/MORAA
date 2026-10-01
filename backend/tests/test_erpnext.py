import os
import sys
import asyncio
from pathlib import Path
from dotenv import load_dotenv

backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import httpx

async def main():
    base_url = os.getenv("ERPNEXT_BASE_URL", "").rstrip("/")
    api_key = os.getenv("ERPNEXT_API_KEY", "")
    api_secret = os.getenv("ERPNEXT_API_SECRET", "")
    company = os.getenv("ERPNEXT_COMPANY", "moraa")

    headers = {
        "Authorization": f"token {api_key}:{api_secret}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    item_code = "GEMVISION-WALLET-RECHARGE"

    async with httpx.AsyncClient() as client:
        # 1. Ensure Item exists
        item_res = await client.get(f"{base_url}/api/resource/Item/{item_code}", headers=headers)
        if item_res.status_code == 404:
            print(f"Creating Item '{item_code}' in ERPNext...")
            new_item_payload = {
                "item_code": item_code,
                "item_name": "GemVision Wallet Recharge",
                "item_group": "Services",
                "is_stock_item": 0
            }
            create_item_res = await client.post(f"{base_url}/api/resource/Item", headers=headers, json=new_item_payload)
            print(f"Item creation status: {create_item_res.status_code}")

        # 2. Create Sales Invoice
        print("\nCreating Sales Invoice...")
        invoice_payload = {
            "company": company,
            "customer": "Test User",
            "items": [
                {
                    "item_code": item_code,
                    "qty": 1,
                    "rate": 10.0
                }
            ]
        }
        res = await client.post(f"{base_url}/api/resource/Sales Invoice", headers=headers, json=invoice_payload)
        print("\n================ ERPNEXT RESPONSE ================")
        print(f"Status Code: {res.status_code}")
        print("Raw Response Text:")
        print(res.text)
        print("==================================================\n")

if __name__ == "__main__":
    # Manual script only: load live credentials when run directly. Loading
    # them at import time leaked backend/.env into every pytest session.
    load_dotenv(backend_dir / ".env")
    load_dotenv(backend_dir.parent / ".env")
    asyncio.run(main())