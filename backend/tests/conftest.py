"""Suite-wide safety: never let a test create real ERPNext documents.

backend/.env may hold live Frappe Cloud credentials and
ERPNEXT_INVOICE_ENABLED=true; any test that posts a Razorpay webhook would
then bill the live ERPNext company. Tests that exercise ERPNext turn it on
explicitly with mocked HTTP (see test_billing_erpnext.py).
"""

import os
import tempfile
from pathlib import Path

import pytest

# Hermetic settings: never load backend/.env (live secrets, live flags) into
# the test process. Must run before any ``app`` import; conftest.py is
# imported by pytest before the test modules. Set MORAA_ENV_FILE to a path
# beforehand to run the suite against a specific env file on purpose.
os.environ.setdefault("MORAA_ENV_FILE", "")

# Throw-away storage: tests must not write into backend/data (the local dev
# SQLite), backend/app/uploads, app/reports or backend/logs.
_TEST_STORAGE = tempfile.mkdtemp(prefix="moraa_tests_")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{Path(_TEST_STORAGE, 'test.db').as_posix()}")
os.environ.setdefault("UPLOAD_DIR", str(Path(_TEST_STORAGE, "uploads")))
os.environ.setdefault("REPORT_DIR", str(Path(_TEST_STORAGE, "reports")))
os.environ.setdefault("LOG_DIR", str(Path(_TEST_STORAGE, "logs")))


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
    # Existing tests exercise the plain background-task path; outbox tests switch the durable queue on themselves.
    monkeypatch.setattr(settings, "OUTBOX_ENABLED", False)
    # Legacy tests download from made-up hosts; the host check has its own tests.
    monkeypatch.setattr(settings, "META_MEDIA_HOST_CHECK", False)
    # Provider health memory is process-wide: one test's simulated outage must not open a circuit for the next.
    from app.ai.provider_protection import breaker, bucket

    from app.services import eta_service

    breaker.reset()
    bucket.reset()
    eta_service.reset()
    yield
    breaker.reset()
    bucket.reset()
    eta_service.reset()
