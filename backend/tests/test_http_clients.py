"""PERF-6: the shared outbound HTTP clients (app/utils/http_clients.py).

One TLS context per process, one connection pool per event loop, one cached client per (timeout, redirects, auth)
setting; warm connections are reused, cookies never persist, shutdown closes everything, a closed client is rebuilt.
Connection reuse and cookie isolation are checked against a real local HTTP server (no network beyond 127.0.0.1).
Every call site is checked to keep its own timeout / redirect / auth setting.
"""

import asyncio
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.config import settings
from app.utils import http_clients


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"          # keep-alive, like Meta / Razorpay

    def do_GET(self):  # noqa: N802 -- http.server naming
        self.server.ports.add(self.client_address[1])           # one source port = one TCP connection
        self.server.cookie_headers.append(self.headers.get("Cookie"))
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "session=abc; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):          # keep the test output quiet
        pass


class LocalServer:
    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.ports, self.server.cookie_headers = set(), []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/x"
        return self.server

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class _Base(unittest.TestCase):
    def run_async(self, coro_factory):
        async def go():
            try:
                return await coro_factory()
            finally:
                await http_clients.close_shared_clients()
        return asyncio.run(go())


# ── caching and lifecycle ────────────────────────────────────────────────────────────────────────────────────

class CachingTests(_Base):
    def test_the_same_setting_returns_the_same_client_and_builds_it_once(self):
        real = httpx.AsyncClient
        built = []

        def counting(*a, **k):
            built.append(k)
            return real(*a, **k)

        async def go():
            with patch.object(httpx, "AsyncClient", side_effect=counting):
                first = await http_clients.shared_client(timeout=30.0)
                again = [await http_clients.shared_client(timeout=30.0) for _ in range(5)]
            return first, again

        first, again = self.run_async(go)
        self.assertTrue(all(c is first for c in again))
        self.assertEqual(len(built), 1)

    def test_different_settings_get_their_own_client_on_one_shared_pool(self):
        async def go():
            a = await http_clients.shared_client(timeout=30.0)
            b = await http_clients.shared_client(timeout=60.0)
            c = await http_clients.shared_client(timeout=60.0, follow_redirects=True)
            d = await http_clients.shared_client(timeout=10.0, basic_auth=("key", "secret"))
            e = await http_clients.shared_client(timeout=10.0, basic_auth=("key2", "secret"))
            return a, b, c, d, e

        clients = self.run_async(go)
        self.assertEqual(len({id(c) for c in clients}), 5)
        self.assertEqual(len({id(c._transport) for c in clients}), 1)        # every client shares one pool
        a, b, c, d, e = clients
        self.assertEqual((a.timeout.read, b.timeout.read), (30.0, 60.0))
        self.assertEqual((b.follow_redirects, c.follow_redirects), (False, True))
        self.assertIsInstance(d.auth, httpx.BasicAuth)
        self.assertIsNone(a.auth)

    def test_concurrent_first_calls_build_one_client(self):
        real = httpx.AsyncClient
        built = []

        def counting(*a, **k):
            built.append(1)
            return real(*a, **k)

        async def go():
            with patch.object(httpx, "AsyncClient", side_effect=counting):
                return await asyncio.gather(*(http_clients.shared_client(timeout=30.0) for _ in range(20)))

        clients = self.run_async(go)
        self.assertEqual(len({id(c) for c in clients}), 1)
        self.assertEqual(len(built), 1)

    def test_each_event_loop_has_its_own_pool_but_the_tls_context_is_built_once_per_process(self):
        calls = []
        real = httpx.create_ssl_context

        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)

        saved = http_clients._SSL_CONTEXT
        http_clients._SSL_CONTEXT = None
        try:
            with patch.object(httpx, "create_ssl_context", side_effect=counting):
                t1 = self.run_async(lambda: self._transport_of(30.0))
                t2 = self.run_async(lambda: self._transport_of(30.0))
        finally:
            http_clients._SSL_CONTEXT = saved
        self.assertIsNot(t1, t2)
        self.assertEqual(len(calls), 1)

    async def _transport_of(self, timeout):
        return (await http_clients.shared_client(timeout=timeout))._transport

    def test_keepalive_and_pool_limits_come_from_the_settings(self):
        with patch.object(settings, "HTTP_KEEPALIVE_SECONDS", 42.0), \
                patch.object(settings, "HTTP_MAX_CONNECTIONS", 7), \
                patch.object(settings, "HTTP_MAX_KEEPALIVE_CONNECTIONS", 3):
            pool = self.run_async(lambda: self._transport_of(30.0))._pool
        self.assertEqual((pool._keepalive_expiry, pool._max_connections, pool._max_keepalive_connections),
                         (42.0, 7, 3))
        self.assertEqual(settings.HTTP_KEEPALIVE_SECONDS, 60.0)                  # the default outlives httpx's 5 s

    def test_close_shuts_the_clients_and_the_pool_and_the_next_call_builds_fresh_ones(self):
        async def go():
            first = await http_clients.shared_client(timeout=30.0)
            transport = first._transport
            await http_clients.close_shared_clients()
            closed = first.is_closed
            second = await http_clients.shared_client(timeout=30.0)
            return first, closed, transport, second

        first, closed, transport, second = self.run_async(go)
        self.assertTrue(closed)
        self.assertIsNot(second, first)
        self.assertIsNot(second._transport, transport)

    def test_a_client_closed_by_someone_else_is_rebuilt_on_a_new_pool(self):
        with LocalServer() as server:
            url = f"http://127.0.0.1:{server.server_address[1]}/x"

            async def go():
                first = await http_clients.shared_client(timeout=30.0)
                other = await http_clients.shared_client(timeout=60.0)
                await first.aclose()                                   # also closes the pool `other` shares
                second = await http_clients.shared_client(timeout=30.0)
                other_again = await http_clients.shared_client(timeout=60.0)
                status = ((await second.get(url)).status_code, (await other_again.get(url)).status_code)
                return first, second, other, other_again, second.is_closed, status

            first, second, other, other_again, closed, status = self.run_async(go)
        self.assertIsNot(second, first)
        self.assertIsNot(second._transport, first._transport)
        self.assertIsNot(other_again, other)                          # rebuilt too: its old pool was closed
        self.assertFalse(closed)
        self.assertEqual(status, (200, 200))

    def test_close_without_clients_and_outside_a_loop_is_harmless(self):
        self.run_async(http_clients.close_shared_clients)
        asyncio.run(http_clients.close_shared_clients())

    def test_warm_up_builds_the_default_client_and_never_raises(self):
        async def go():
            await http_clients.warm_up()
            return list(http_clients._STATES[asyncio.get_running_loop()].clients)

        keys = self.run_async(go)
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0][0][1], 30.0)                        # the default Meta send timeout
        with patch.object(http_clients, "shared_client", AsyncMock(side_effect=RuntimeError("boom"))):
            asyncio.run(http_clients.warm_up())

    def test_the_client_is_entered_once_like_the_async_with_it_replaces(self):
        """Tests and fakes that configure ``AsyncClient().__aenter__`` keep working."""
        inner = MagicMock(name="entered")
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=inner)

        async def go():
            with patch.object(httpx, "AsyncClient", factory):
                async with http_clients.shared_http(timeout=30.0) as client:
                    return client

        self.assertIs(self.run_async(go), inner)
        factory.return_value.__aenter__.assert_awaited_once()
        factory.return_value.__aexit__.assert_awaited_once()          # by close_shared_clients, not by the with

    def test_leaving_shared_http_does_not_close_the_client(self):
        async def go():
            async with http_clients.shared_http(timeout=30.0) as client:
                pass
            return client, client.is_closed

        client, closed = self.run_async(go)
        self.assertFalse(closed)


# ── real sockets ─────────────────────────────────────────────────────────────────────────────────────────────

class RealConnectionTests(_Base):
    def test_ten_calls_reuse_one_connection(self):
        with LocalServer() as server:
            async def go():
                for _ in range(10):
                    async with http_clients.shared_http(timeout=5.0) as client:
                        self.assertEqual((await client.get(self.url)).status_code, 200)

            self.url = f"http://127.0.0.1:{server.server_address[1]}/x"
            self.run_async(go)
        self.assertEqual(len(server.ports), 1)

    def test_clients_with_different_timeouts_share_the_warm_connection(self):
        with LocalServer() as server:
            url = f"http://127.0.0.1:{server.server_address[1]}/x"

            async def go():
                for timeout in (30.0, 60.0, 20.0, 30.0):
                    await (await http_clients.shared_client(timeout=timeout)).get(url)

            self.run_async(go)
        self.assertEqual(len(server.ports), 1)

    def test_the_old_per_call_client_opened_a_connection_every_time(self):
        """The baseline this replaces: a new client per call = a new TCP (and TLS) connection per call."""
        with LocalServer() as server:
            url = f"http://127.0.0.1:{server.server_address[1]}/x"

            async def go():
                for _ in range(5):
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        await client.get(url)

            asyncio.run(go())
        self.assertEqual(len(server.ports), 5)

    def test_a_cookie_set_by_one_call_is_never_sent_on_the_next(self):
        with LocalServer() as server:
            url = f"http://127.0.0.1:{server.server_address[1]}/x"

            async def go():
                client = await http_clients.shared_client(timeout=5.0)
                await client.get(url)
                await client.get(url)
                return dict(client.cookies)

            stored = self.run_async(go)
        self.assertEqual(server.cookie_headers, [None, None])
        self.assertEqual(stored, {})


# ── every call site keeps its own setting and goes through the shared client ─────────────────────────────────

class _Recorder:
    """Stands in for httpx.AsyncClient: records how each shared client was built and answers every request."""

    def __init__(self, response):
        self.built, self.requests, self.response = [], [], response

    def __call__(self, *a, **k):
        self.built.append(k)
        recorder = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, **kw):
                recorder.requests.append(("GET", url, kw))
                return recorder.response

            async def post(self, url, **kw):
                recorder.requests.append(("POST", url, kw))
                return recorder.response

        return _Client()


def _response(status=200, json=None, content=b"x", content_type="image/jpeg"):
    return httpx.Response(status, json=json, headers={"content-type": content_type}) if json is not None else \
        httpx.Response(status, content=content, headers={"content-type": content_type})


class CallSiteTests(_Base):
    def setUp(self):
        for p in (patch.object(settings, "META_WHATSAPP_TOKEN", "t"), patch.object(settings, "META_PHONE_NUMBER_ID", "1"),
                  patch.object(settings, "META_MEDIA_HOST_CHECK", False), patch.object(settings, "META_REQUEST_RETRIES", 0),
                  patch.object(settings, "RAZORPAY_KEY_ID", "rzp_key"), patch.object(settings, "RAZORPAY_KEY_SECRET", "rzp_s"),
                  patch.object(settings, "RAZORPAY_API_TIMEOUT_SECONDS", 10.0)):
            p.start()
            self.addCleanup(p.stop)

    def built_with(self, coro_factory, response):
        recorder = _Recorder(response)
        with patch.object(httpx, "AsyncClient", recorder):
            result = self.run_async(coro_factory)
        return result, recorder

    def timeout_of(self, kwargs):
        t = kwargs["timeout"]
        return t if isinstance(t, (int, float)) else (t.connect, t.read)

    def test_meta_sends_reuse_one_client_across_messages(self):
        from app.services import meta_whatsapp_service as mws

        async def go():
            return [await mws.send_whatsapp_text("919800000000", f"hi {i}") for i in range(4)]

        sent, recorder = self.built_with(go, _response(json={"messages": [{"id": "wamid.1"}]}))
        self.assertEqual(sent, [True] * 4)
        self.assertEqual(len(recorder.built), 1)                   # was 4: one client per message
        self.assertEqual(len(recorder.requests), 4)
        self.assertEqual(self.timeout_of(recorder.built[0]), 30.0)

    def test_media_lookup_download_and_upload_keep_their_settings(self):
        from app.services import meta_whatsapp_service as mws

        async def go():
            url = await mws.get_media_url("12345")
            image = await mws.download_media("https://lookaside.fbsbx.com/x")
            media_id = await mws.upload_media_to_meta(b"img")
            return url, image, media_id

        (url, image, media_id), recorder = self.built_with(
            go, _response(json={"url": "https://lookaside.fbsbx.com/x", "id": "media-9"}, content_type="image/jpeg"))
        self.assertEqual((url, media_id), ("https://lookaside.fbsbx.com/x", "media-9"))
        self.assertIsNotNone(image)
        settings_seen = sorted((self.timeout_of(k), bool(k.get("follow_redirects"))) for k in recorder.built)
        self.assertEqual(settings_seen, [(30.0, False), (60.0, False), (60.0, True)])   # 3 settings, 3 clients
        self.assertTrue(all("transport" in k for k in recorder.built))

    def test_razorpay_link_creation_and_lookup_use_basic_auth_and_their_timeouts(self):
        from app.services import razorpay_link_reconcile as rlr
        from app.services import razorpay_service as rzp

        async def go():
            return await rzp._post_payment_link({"amount": 100}), await rlr.fetch_payment_link("plink_1")

        (link, fetched), recorder = self.built_with(go, _response(json={"id": "plink_1", "short_url": "u"}))
        self.assertEqual((link["id"], fetched["id"]), ("plink_1", "plink_1"))
        auths = [k["auth"] for k in recorder.built]
        self.assertTrue(all(isinstance(a, httpx.BasicAuth) for a in auths))
        self.assertEqual(sorted(self.timeout_of(k) for k in recorder.built if not isinstance(k["timeout"], float)),
                         [(5.0, 10.0)])                            # creation: connect 5 s, read 10 s, as before
        self.assertIn(10.0, [k["timeout"] for k in recorder.built])  # lookup: 10 s, as before

    def test_whatsapp_pay_lookup_and_gst_lookup_keep_their_timeouts(self):
        from app.services import gst_service, whatsapp_pay_service

        async def go():
            pay = await whatsapp_pay_service.lookup_payment("cfg", "ref-1")
            with patch.multiple(settings, GST_API_URL="https://vendor.test/{gstin}", GST_API_TIMEOUT_SECONDS=8.0):
                gst = await gst_service.HttpGstProvider().lookup("27AAAPS1234C1Z5")
            return pay, gst

        (pay, gst), recorder = self.built_with(go, _response(json={"id": "p", "status": "ok"}))
        self.assertEqual((pay["id"], gst["id"]), ("p", "p"))
        self.assertEqual(sorted(k["timeout"] for k in recorder.built), [8.0, 20.0])
        self.assertTrue(all("auth" not in k for k in recorder.built))     # Meta / GST never get the Razorpay key


if __name__ == "__main__":
    unittest.main()
