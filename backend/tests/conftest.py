"""Suite-wide safety: never let a test create real ERPNext documents.

backend/.env may hold live Frappe Cloud credentials and
ERPNEXT_INVOICE_ENABLED=true; any test that posts a Razorpay webhook would
then bill the live ERPNext company. Tests that exercise ERPNext turn it on
explicitly with mocked HTTP (see test_billing_erpnext.py).
"""

import os

import pytest

# Hermetic settings: never load backend/.env (live secrets, live flags) into
# the test process. Must run before any ``app`` import; conftest.py is
# imported by pytest before the test modules. Set MORAA_ENV_FILE to a path
# beforehand to run the suite against a specific env file on purpose.
os.environ.setdefault("MORAA_ENV_FILE", "")


@pytest.fixture(autouse=True)
def _erpnext_billing_off_by_default(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ERPNEXT_INVOICE_ENABLED", False)
    # Same for live GST verification: .env may switch it on (mock or real
    # vendor); tests that exercise it turn it on explicitly.
    monkeypatch.setattr(settings, "GST_VERIFICATION_ENABLED", False)
    # Most webhook tests post unsigned payloads with no secret configured
    # (local development). Signature tests set this False explicitly.
    monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", True)
    # Starlette's TestClient connects as peer "testclient"; treat it as this
    # machine. Guard tests override this to prove remote peers are blocked.
    monkeypatch.setattr(settings, "LOCAL_PEER_ADDRESSES", "127.0.0.1,::1,testclient")
    yield
