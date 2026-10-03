"""EXT-8: a configurable HTTP vendor for GSTIN verification."""

import asyncio
import unittest
from unittest.mock import patch

import httpx

from app.config import settings
from app.services import gst_service

GSTIN = "27AAAPS1234C1Z5"
OK = {"data": {"gstin": GSTIN, "sts": "Active", "tradeNam": "ACME", "lgnm": "ACME PVT LTD"}}


def _run(handler, **cfg):
    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    base = dict(GST_PROVIDER="http", GST_API_URL="https://vendor.test/gstin/{gstin}", GST_API_KEY="k-secret",
                GST_API_KEY_HEADER="x-api-key", DEBUG=False)
    base.update(cfg)
    with patch.multiple(settings, **base), patch.object(httpx, "AsyncClient", side_effect=factory):
        return asyncio.run(gst_service.verify_gstin(GSTIN))


class HttpProviderTests(unittest.TestCase):
    def test_an_active_taxpayer_is_verified_and_the_key_goes_in_the_header(self):
        seen = {}

        def handler(request):
            seen["url"], seen["key"] = str(request.url), request.headers.get("x-api-key")
            return httpx.Response(200, json=OK)

        result = _run(handler)
        self.assertEqual((result.status, result.display_name), (gst_service.ACTIVE, "ACME"))
        self.assertEqual((seen["url"], seen["key"]), (f"https://vendor.test/gstin/{GSTIN}", "k-secret"))

    def test_404_is_not_found(self):
        self.assertEqual(_run(lambda r: httpx.Response(404)).status, gst_service.NOT_FOUND)

    def test_a_vendor_error_is_unavailable_not_a_crash_and_never_leaks_the_key(self):
        result = _run(lambda r: httpx.Response(500, text="k-secret"))
        self.assertEqual(result.status, gst_service.UNAVAILABLE)
        self.assertNotIn("k-secret", result.detail)

    def test_a_cancelled_registration_is_inactive(self):
        body = {"data": {"gstin": GSTIN, "sts": "Cancelled", "lgnm": "X"}}
        self.assertEqual(_run(lambda r: httpx.Response(200, json=body)).status, gst_service.INACTIVE)

    def test_a_non_https_or_malformed_url_is_refused(self):
        for url in ("http://vendor.test/{gstin}", "https://vendor.test/fixed"):
            self.assertEqual(_run(lambda r: httpx.Response(200, json=OK), GST_API_URL=url).status, gst_service.UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()


class VerifiedOnlyOnInvoiceTests(unittest.TestCase):
    def test_with_verification_on_only_a_verified_gstin_is_printed(self):
        from app.services.billing_service import billing_gstin

        snap = {"gst_number": GSTIN, "is_gst_verified": False}
        with patch.object(settings, "GST_VERIFICATION_ENABLED", True):
            self.assertIsNone(billing_gstin(snap))
            self.assertEqual(billing_gstin({**snap, "is_gst_verified": True}), GSTIN)
        with patch.object(settings, "GST_VERIFICATION_ENABLED", False):
            self.assertEqual(billing_gstin(snap), GSTIN)               # unchanged while verification is off
