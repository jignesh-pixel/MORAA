#!/usr/bin/env python3
# Usage (from backend/):  python scripts/gst_onboarding_e2e.py .
"""Offline end-to-end run of the GSTIN onboarding flow through the REAL webhook
route and the REAL mock GST provider. Uses an in-memory SQLite DB and captures
every outgoing WhatsApp payload instead of sending it. No network calls."""
import json, os, sys, socket
BACKEND = sys.argv[1] if len(sys.argv) > 1 else "."
sys.path.insert(0, BACKEND); os.chdir(BACKEND)
def _blocked(*a, **k): raise RuntimeError("network blocked in e2e runner")
socket.socket.connect = _blocked; socket.create_connection = _blocked

from unittest.mock import AsyncMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
import app.models  # noqa
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.customer import Customer
from app.models.onboarding_session import OnboardingSession
from app.services import meta_whatsapp_service as mws
from app.api.routes import meta_webhook as wh
from app.services import gst_service as gst

SENDER = "919323438262"
engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(engine); db = sessionmaker(bind=engine)()
app.dependency_overrides[get_db] = lambda: (yield db)

sent = []  # every outgoing WhatsApp payload
async def capture(payload, label, reply_to_message_id=None):
    sent.append(payload); return True

settings.GST_VERIFICATION_ENABLED = True; settings.GST_PROVIDER = "mock"; settings.DEBUG = True
settings.RATE_LIMIT_ENABLED = False; settings.META_APP_SECRET = ""
settings.META_WHATSAPP_TOKEN = "x"; settings.META_PHONE_NUMBER_ID = "x"
client = TestClient(app)
results = []

def post(message):
    body = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {"messages": [message]}}]}]}
    r = client.post("/api/meta/webhook", json=body); assert r.status_code == 200, r.text

def text(body, mid): post({"type": "text", "id": mid, "from": SENDER, "timestamp": "1", "text": {"body": body}})
def button(bid, mid): post({"type": "interactive", "id": mid, "from": SENDER, "timestamp": "1",
                            "interactive": {"type": "button_reply", "button_reply": {"id": bid, "title": "x"}}})
def cust():
    db.expire_all(); return db.query(Customer).filter_by(whatsapp_id=SENDER).one()
def state():
    db.expire_all(); r = db.query(OnboardingSession).filter_by(whatsapp_id=SENDER).first(); return r.state if r else None
def last_buttons():
    for p in reversed(sent):
        if p.get("type") == "interactive" and p["interactive"]["type"] == "button":
            return p["interactive"]["body"]["text"], [b["reply"]["id"] for b in p["interactive"]["action"]["buttons"]]
def last_text():
    for p in reversed(sent):
        if p.get("type") == "text": return p["text"]["body"]
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail)); print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))

with patch.object(mws, "_post_message_payload", new=AsyncMock(side_effect=capture)), \
     patch.object(wh, "create_recharge_payment_link", new=AsyncMock(return_value="https://rzp.io/test")), \
     patch.object(wh, "send_registration_flow", new=AsyncMock(return_value=False)):
    db.add(Customer(whatsapp_id=SENDER, full_name="Anurag", business_name="Jewelry Business", gst_number="N/A",
                    address="JOGESHWARI-E TEST", wallet_balance=1000, is_registered=False)); db.commit()

    print("\n== Case 1: invalid GSTIN in the registration form ==")
    text("Hi", "e2e.1"); n = len(sent)
    text("• Name: Anurag Mehta\n• Brand Name: Moraa Jewels\n• City: Mumbai\n• GSTIN (Optional): INVALID123", "e2e.2")
    body, ids = last_buttons() or ("", [])
    check("format error message sent", body.startswith(gst.INVALID_FORMAT_MESSAGE), body[:80])
    check("buttons are btn_gst_reenter + btn_gst_skip", ids == ["btn_gst_reenter", "btn_gst_skip"], ids)
    c = cust(); check("not verified, no GSTIN stored", (c.is_gst_verified, c.gst_number) == (False, "N/A"), (c.is_gst_verified, c.gst_number))

    print("\n== Case 2: Re-enter -> valid GSTIN -> mock Active ==")
    button("btn_gst_reenter", "e2e.3")
    check("state AWAITING_GSTIN", state() == gst.STATE_AWAITING_GSTIN, state())
    check("re-enter prompt sent", last_text() == gst.REENTER_PROMPT, last_text())
    text("27ABCDE1234F1Z5", "e2e.4")
    c = cust()
    check("is_gst_verified=True", c.is_gst_verified is True)
    check("gst_number saved", c.gst_number == "27ABCDE1234F1Z5", c.gst_number)
    check("trade name overwrote business name", c.business_name == "MOCK VERIFIED TRADERS", c.business_name)
    check("success reply sent", last_text() == gst.SUCCESS_TEMPLATE.format(trade_name="MOCK VERIFIED TRADERS"), last_text())
    check("state back to REGISTERED", state() == gst.STATE_REGISTERED, state())

    print("\n== Case 3: Skip ==")
    button("btn_gst_skip", "e2e.5")
    c = cust()
    check("is_gst_verified=False", c.is_gst_verified is False)
    check('gst_number="N/A"', c.gst_number == "N/A", c.gst_number)
    check("ITC disclaimer delivered", last_text() == gst.SKIP_MESSAGE, (last_text() or "")[:60])
    check("wallet untouched (₹1000)", c.wallet_balance == 1000, c.wallet_balance)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; {len(sent)} WhatsApp payloads captured, 0 sent.")
sys.exit(1 if failed else 0)
