"""Suite-wide safety: never let a test create real ERPNext documents.

backend/.env may hold live Frappe Cloud credentials and
ERPNEXT_INVOICE_ENABLED=true; any test that posts a Razorpay webhook would
then bill the live ERPNext company. Tests that exercise ERPNext turn it on
explicitly with mocked HTTP (see test_billing_erpnext.py).
"""

import pytest


@pytest.fixture(autouse=True)
def _erpnext_billing_off_by_default(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ERPNEXT_INVOICE_ENABLED", False)
    # Same for live GST verification: .env may switch it on (mock or real
    # vendor); tests that exercise it turn it on explicitly.
    monkeypatch.setattr(settings, "GST_VERIFICATION_ENABLED", False)
    yield
