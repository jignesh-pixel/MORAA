"""Phase 3 / EXT-4, EXT-5: Meta calls retry what is safe to retry; Razorpay link creation has a hard timeout."""

import asyncio
import unittest
from unittest.mock import patch

import httpx

from app.config import settings
from app.services import meta_whatsapp_service as mws
from app.services import razorpay_service as rzp

OK_SEND = {"messages": [{"id": "wamid.sent"}]}


class FakeClient:
    """Scripted httpx client: each call takes the next step (a Response or an exception to raise)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.last_kwargs = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _next(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    async def post(self, url, **kwargs):
        return self._next(**kwargs)

    async def get(self, url, **kwargs):
        return self._next(**kwargs)


def resp(status, json=None, headers=None):
    return httpx.Response(status, json=json if json is not None else {}, headers=headers or {})


class MetaRetryTests(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(settings, "META_WHATSAPP_TOKEN", "token"),
                  patch.object(settings, "META_PHONE_NUMBER_ID", "1"),
                  patch.object(settings, "META_REQUEST_RETRIES", 2),
                  patch.object(settings, "META_RETRY_BACKOFF_BASE_SECONDS", 0.001),
                  patch.object(settings, "META_RETRY_BACKOFF_CAP_SECONDS", 0.01)):
            p.start()
            self.addCleanup(p.stop)

    def send(self, script):
        client = FakeClient(script)
        errors = {}
        with patch.object(mws.httpx, "AsyncClient", lambda *a, **k: client):
            ok = asyncio.run(mws._post_message_payload({"to": "919812345678"}, "text", error_out=errors))
        return ok, client.calls, errors

    def test_a_429_is_retried_then_the_message_goes_out_once(self):
        ok, calls, _ = self.send([resp(429), resp(200, OK_SEND)])
        self.assertEqual((ok, calls), (True, 2))

    def test_429_is_retried_a_bounded_number_of_times(self):
        ok, calls, errors = self.send([resp(429), resp(429), resp(429), resp(200, OK_SEND)])
        self.assertEqual((ok, calls), (False, 3))                      # 1 call + 2 retries, then the failure stands
        self.assertEqual(errors.get("status"), 429)

    def test_a_send_is_never_retried_after_a_5xx(self):
        """A gateway can answer 502/503/504 AFTER Meta accepted the message: a retry would duplicate it."""
        for status in (500, 502, 503, 504):
            ok, calls, _ = self.send([resp(status), resp(200, OK_SEND)])
            self.assertEqual((ok, calls), (False, 1), status)

    def test_a_connect_timeout_is_retried_because_nothing_was_sent(self):
        ok, calls, _ = self.send([httpx.ConnectTimeout("no route"), resp(200, OK_SEND)])
        self.assertEqual((ok, calls), (True, 2))

    def test_a_client_error_is_not_retried(self):
        ok, calls, _ = self.send([resp(400), resp(200, OK_SEND)])
        self.assertEqual((ok, calls), (False, 1))

    def test_a_send_that_timed_out_is_never_resent(self):
        """It may already have been delivered: a retry would send the customer the same message twice."""
        ok, calls, errors = self.send([httpx.ReadTimeout("slow"), resp(200, OK_SEND)])
        self.assertEqual((ok, calls), (False, 1))
        self.assertTrue(errors.get("timeout"))

    def test_a_failure_to_connect_is_retried_because_nothing_was_sent(self):
        ok, calls, _ = self.send([httpx.ConnectError("refused"), resp(200, OK_SEND)])
        self.assertEqual((ok, calls), (True, 2))

    def test_a_long_retry_after_means_do_not_wait(self):
        ok, calls, _ = self.send([resp(429, headers={"retry-after": "120"}), resp(200, OK_SEND)])
        self.assertEqual((ok, calls), (False, 1))

    def test_a_short_retry_after_is_honoured_not_undercut(self):
        wait = mws._meta_retry_wait(resp(429, headers={"retry-after": "0.005"}), 0)
        self.assertGreaterEqual(wait, 0.005)

    def test_downloads_and_lookups_are_retried_even_after_a_timeout(self):
        client = FakeClient([httpx.ReadTimeout("slow"), resp(200, {"url": "http://cdn/x"})])
        with patch.object(mws.httpx, "AsyncClient", lambda *a, **k: client):
            self.assertEqual(asyncio.run(mws.get_media_url("media-1")), "http://cdn/x")
        self.assertEqual(client.calls, 2)

    def test_download_retries_a_503(self):
        client = FakeClient([resp(503), httpx.Response(200, content=b"imagebytes", headers={"content-type": "image/jpeg"})])
        with patch.object(mws.httpx, "AsyncClient", lambda *a, **k: client):
            self.assertEqual(asyncio.run(mws.download_media("http://cdn/x")), (b"imagebytes", "image/jpeg"))
        self.assertEqual(client.calls, 2)


class RazorpayLinkTests(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(settings, "RAZORPAY_KEY_ID", "rzp_test_key"),
                  patch.object(settings, "RAZORPAY_KEY_SECRET", "rzp_test_secret"),
                  patch.object(settings, "RAZORPAY_API_TIMEOUT_SECONDS", 10.0)):
            p.start()
            self.addCleanup(p.stop)

    def post(self, script):
        client = FakeClient(script)
        with patch.object(rzp.httpx, "AsyncClient", lambda *a, **k: client):
            return asyncio.run(rzp._post_payment_link({"amount": 50000})), client

    def test_success_returns_the_link(self):
        link, client = self.post([resp(200, {"id": "plink_1", "short_url": "https://rzp.io/x"})])
        self.assertEqual(link["short_url"], "https://rzp.io/x")
        self.assertEqual(client.calls, 1)

    def test_a_timeout_gives_none_and_is_not_retried(self):
        link, client = self.post([httpx.ReadTimeout("slow"), resp(200, {"id": "x"})])
        self.assertIsNone(link)
        self.assertEqual(client.calls, 1)          # a retry could create a second link

    def test_an_error_status_gives_none(self):
        self.assertIsNone(self.post([resp(500)])[0])
        self.assertIsNone(self.post([resp(401)])[0])

    def test_missing_keys_give_none_without_calling_razorpay(self):
        with patch.object(settings, "RAZORPAY_KEY_ID", ""):
            link, client = self.post([resp(200, {"id": "x"})])
        self.assertIsNone(link)
        self.assertEqual(client.calls, 0)

    def test_create_link_falls_back_to_the_static_link_on_failure(self):
        with patch.object(rzp, "_post_payment_link", return_value=None) as m:
            m.side_effect = None
            async def none(payload):
                return None
            with patch.object(rzp, "_post_payment_link", new=none):
                url = asyncio.run(rzp.create_recharge_payment_link("919812345678", "T", 500))
        self.assertEqual(url, rzp.ACTIVE_PAYMENT_URL)

    def test_create_link_returns_the_personal_link_and_records_it(self):
        recorded = []

        async def ok(payload):
            self.assertEqual(payload["amount"], 50000)
            self.assertEqual(payload["notes"], {"whatsapp_id": "919812345678"})
            return {"id": "plink_9", "short_url": "https://rzp.io/personal"}

        with patch.object(rzp, "_post_payment_link", new=ok), \
             patch.object(rzp, "record_payment_link", side_effect=lambda *a: recorded.append(a)):
            url = asyncio.run(rzp.create_recharge_payment_link("919812345678", "T", 500))
        self.assertEqual(url, "https://rzp.io/personal")
        self.assertEqual(recorded, [("plink_9", "919812345678", 500)])

    def test_the_secret_never_appears_in_the_request_body(self):
        link, client = self.post([resp(200, {"id": "plink_1", "short_url": "u"})])
        self.assertNotIn("rzp_test_secret", str(client.last_kwargs))


if __name__ == "__main__":
    unittest.main()
