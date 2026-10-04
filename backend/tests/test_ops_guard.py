"""Studioo Ops webhook guard: team ops messages are diverted, customers untouched."""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import ops_forward

NGROK = "drainpipe-unsoiled-native.ngrok-free.dev"
TEAM = json.dumps({"harvil": "919699899825", "anurag": "+91 93234 38262"})
OPS_ON = dict(OPS_ENABLED=True, OPS_TEAM=TEAM, OPS_SECRET="s3cret",
              OPS_INBOUND_URL="http://next.local/api/ops/inbound")


def _entry(*msgs):
    return {"id": "waba", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "contacts": [], "messages": list(msgs)}}]}


def text(frm, body, mid="m1"):
    return {"from": frm, "id": mid, "type": "text", "text": {"body": body}}


def image(frm, caption=None, mid="i1"):
    img = {"id": "media1", "mime_type": "image/jpeg"}
    if caption is not None:
        img["caption"] = caption
    return {"from": frm, "id": mid, "type": "image", "image": img}


class _Patched(unittest.TestCase):
    cfg = OPS_ON

    def setUp(self):
        self.patches = [patch.object(settings, k, v) for k, v in self.cfg.items()]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()


class DivertTests(_Patched):
    def test_customer_entry_returned_identical(self):
        bt, e = MagicMock(), _entry(text("919000000001", "hi"))
        self.assertIs(ops_forward.divert_ops_messages(e, bt), e)
        bt.add_task.assert_not_called()

    def test_team_text_diverted(self):
        bt = MagicMock()
        out = ops_forward.divert_ops_messages(_entry(text("919699899825", "💸 450 chai")), bt)
        self.assertEqual(out["changes"][0]["value"]["messages"], [])
        bt.add_task.assert_called_once()
        self.assertEqual(bt.add_task.call_args.args[1]["id"], "m1")

    def test_team_number_formatting_normalised(self):
        bt = MagicMock()
        ops_forward.divert_ops_messages(_entry(text("919323438262", "todo polish ring")), bt)
        bt.add_task.assert_called_once()

    def test_team_image_without_ops_caption_goes_to_customer_flow(self):
        bt = MagicMock()
        for msg in (image("919699899825"), image("919699899825", "nice ring")):
            e = _entry(msg)
            self.assertIs(ops_forward.divert_ops_messages(e, bt), e)
        bt.add_task.assert_not_called()

    def test_team_image_with_ops_caption_diverted(self):
        bt = MagicMock()
        out = ops_forward.divert_ops_messages(_entry(image("919699899825", "exp 450 chai")), bt)
        self.assertEqual(out["changes"][0]["value"]["messages"], [])
        bt.add_task.assert_called_once()

    def test_mixed_batch_only_ops_removed(self):
        bt = MagicMock()
        cust = text("919000000001", "hello", "c1")
        e = _entry(text("919699899825", "idea x", "t1"), cust)
        out = ops_forward.divert_ops_messages(e, bt)
        self.assertEqual(out["changes"][0]["value"]["messages"], [cust])
        self.assertEqual(len(e["changes"][0]["value"]["messages"]), 2)  # original not mutated

    def test_prefix_rules(self):
        yes = ["EXP 20 auto", "  task call karigar", "Done", "✅ shipped", "💸450", "fix: login", "KB gst rules"]
        no = ["Hi", "Hiii", "hello", "expensive ring", "helpful", "fixed?", "Make a gold jhumka on white bg", ""]
        for t in yes:
            self.assertTrue(ops_forward.has_ops_prefix(t), t)
        for t in no:
            self.assertFalse(ops_forward.has_ops_prefix(t), t)

    def test_team_non_ops_text_goes_to_customer_flow(self):
        bt = MagicMock()
        for body in ("Hi", "Hiii", "Hello", "gold jhumka on white background"):
            e = _entry(text("919699899825", body))
            self.assertIs(ops_forward.divert_ops_messages(e, bt), e)
        bt.add_task.assert_not_called()


class DisabledTests(unittest.TestCase):
    def _check(self, **cfg):
        with patch.multiple(settings, **{**OPS_ON, **cfg}):
            bt, e = MagicMock(), _entry(text("919699899825", "task x"))
            self.assertIs(ops_forward.divert_ops_messages(e, bt), e)
            bt.add_task.assert_not_called()

    def test_kill_switch(self):
        self._check(OPS_ENABLED=False)

    def test_bad_team_json(self):
        self._check(OPS_TEAM="{not json")

    def test_missing_url_or_secret(self):
        self._check(OPS_INBOUND_URL="")
        self._check(OPS_SECRET="")

    def test_defaults_are_off(self):
        from app.config import Settings
        s = Settings(_env_file=None)
        self.assertFalse(s.OPS_ENABLED)
        self.assertEqual(s.OPS_TEAM, "")


class WebhookEndToEnd(_Patched):
    def _post(self, msg):
        body = {"object": "whatsapp_business_account", "entry": [_entry(msg)]}
        with patch.object(settings, "META_APP_SECRET", ""), patch.object(settings, "ALLOW_UNSIGNED_WEBHOOKS", True):
            return TestClient(app).post("/api/meta/webhook", json=body, headers={"host": NGROK})

    def test_team_text_forwarded_and_customer_logic_skipped(self):
        with patch.object(ops_forward, "forward_to_ops", new=AsyncMock()) as fwd, \
             patch("app.api.routes.meta_webhook.send_whatsapp_text", new=AsyncMock()) as send, \
             patch("app.api.routes.meta_webhook.is_awaiting_gstin") as gst:
            r = self._post(text("919699899825", "task call karigar"))
        self.assertEqual((r.status_code, r.json()["status"]), (200, "ok"))
        fwd.assert_awaited_once()
        self.assertEqual(fwd.await_args.args[0]["id"], "m1")
        send.assert_not_awaited()
        gst.assert_not_called()

    def test_customer_text_reaches_customer_logic(self):
        with patch.object(ops_forward, "forward_to_ops", new=AsyncMock()) as fwd, \
             patch("app.api.routes.meta_webhook.is_awaiting_gstin", return_value=False) as gst, \
             patch("app.api.routes.meta_webhook.send_whatsapp_text", new=AsyncMock()), \
             patch("app.api.routes.meta_webhook.send_registration_flow", new=AsyncMock(), create=True):
            r = self._post(text("919000000001", "hi"))
        self.assertEqual(r.status_code, 200)
        fwd.assert_not_awaited()
        gst.assert_called_once()


    def test_team_hi_reaches_customer_logic(self):
        with patch.object(ops_forward, "forward_to_ops", new=AsyncMock()) as fwd, \
             patch("app.api.routes.meta_webhook.is_awaiting_gstin", return_value=False) as gst, \
             patch("app.api.routes.meta_webhook.send_whatsapp_text", new=AsyncMock()), \
             patch("app.api.routes.meta_webhook.send_registration_flow", new=AsyncMock(), create=True):
            r = self._post(text("919699899825", "Hiii"))
        self.assertEqual(r.status_code, 200)
        fwd.assert_not_awaited()
        gst.assert_called_once()


class ForwardTests(_Patched):
    def test_forward_posts_with_secret_and_never_raises(self):
        import asyncio
        import httpx

        seen = {}

        def handler(req):
            seen["hdr"] = req.headers.get("x-ops-secret")
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"ok": True})

        real = httpx.AsyncClient
        with patch.object(ops_forward.httpx, "AsyncClient",
                          lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
            asyncio.run(ops_forward.forward_to_ops({"id": "m1"}))
        self.assertEqual(seen, {"hdr": "s3cret", "body": {"message": {"id": "m1"}}})

        def boom(req):
            raise httpx.ConnectError("down")
        with patch.object(ops_forward.httpx, "AsyncClient",
                          lambda **kw: real(transport=httpx.MockTransport(boom), **kw)):
            asyncio.run(ops_forward.forward_to_ops({"id": "m1"}))  # must not raise


class ExplicitPrefixTests(_Patched):
    cfg = {**OPS_ON, "OPS_EXPLICIT_PREFIX": True}

    def test_a_plain_ops_word_from_the_team_is_a_customer_message(self):
        bt, e = MagicMock(), _entry(text("919699899825", "start"))
        self.assertIs(ops_forward.divert_ops_messages(e, bt), e)
        bt.add_task.assert_not_called()

    def test_ops_start_is_diverted_and_the_word_ops_is_removed(self):
        bt = MagicMock()
        out = ops_forward.divert_ops_messages(_entry(text("919699899825", "Ops start day")), bt)
        self.assertEqual(out["changes"][0]["value"]["messages"], [])
        forwarded = bt.add_task.call_args.args[1]
        self.assertEqual(forwarded["text"]["body"], "start day")

    def test_ops_word_before_something_that_is_not_a_command_is_not_diverted(self):
        bt, e = MagicMock(), _entry(text("919699899825", "ops hello there"))
        self.assertIs(ops_forward.divert_ops_messages(e, bt), e)

    def test_a_caption_works_the_same_way(self):
        bt = MagicMock()
        ops_forward.divert_ops_messages(_entry(image("919699899825", "ops 💸 450 chai")), bt)
        self.assertEqual(bt.add_task.call_args.args[1]["image"]["caption"], "💸 450 chai")
