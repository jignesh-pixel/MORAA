# WhatsApp Native Pay Audit: Why Users Get the Browser Link (30 Sep 2026)

Read-only audit by three parallel agents: routing, payload schema, and webhook/env. No files were changed and no Meta or Razorpay calls were made.

## TL;DR

The `order_details` payload is **spec-compliant**, so the payload is not the reason users see the browser link. The native path **never runs**, for two reasons:

1. **Config:** `backend/.env` has **none** of the `WHATSAPP_PAY_*` keys. `WHATSAPP_PAY_ENABLED` defaults to `False` (`config.py:196`), so `try_send_native_recharge` returns `False` at its first gate (`whatsapp_pay_service.py:90`), **without logging anything**. Every recharge prompt then falls through to the Razorpay URL.
2. **Meta side (the hard blocker):** business verification was rejected (error 141010, see the 25 Sep scaffold doc) and no Payment Configuration exists in WhatsApp Manager yet. With the env keys set, Meta would still reject the send and the code would fall back to the link again (gate 6 below).

---

## 1. Where users are sent to the browser (all paths are under `backend/app/`)

| # | File:line | Trigger | Tries native? | Fallback |
|---|---|---|---|---|
| 1 | `api/routes/meta_webhook.py:355` | Registration confirmation | Yes (₹500) | Personal Razorpay link `:358` → static URL `:365` → "Recharge Wallet" button `:367` |
| 2 | `api/routes/meta_webhook.py:609` | Photo sent with low balance | **Only if a customer row exists** | Plain text with a **static** `rzp.io` link (`_hold_message` `:112`, sent `:614`) |
| 3 | `api/routes/meta_webhook.py:777` | Product tap when the charge fails | Yes | Same static link as plain text `:781-783` |
| 4 | `api/routes/meta_webhook.py:962` | User types "recharge N" | Yes | Personal link `:970` → static `:977` → button `:979` |
| 5 | `services/entitlement_service.py:190` | Trial exhausted | Yes | Personal link `:196` → `RECHARGE_PAYMENT_URL` `:203` → button `:204` |
| 6 | `services/whatsapp_pay_service.py:295` → `_send_fallback_link_once` `:493` | Native payment **failed** (webhook) | After native | Personal link + button `:502-507` |
| 7 | `services/whatsapp_pay_service.py:359` | Native order **expired** | — | Nothing is sent (the user is stranded) |

Other link sources:
- `config.py:307`: hard-coded `RECHARGE_PAYMENT_URL = "https://rzp.io/rzp/FbuLh9je"`.
- `razorpay_service.py:52-56`: the same static URL is returned whenever `payment_link.create` fails.

### Gates inside `try_send_native_recharge` (`whatsapp_pay_service.py:185-233`)

| # | Line | Condition that makes it return `False` | Logged? | In prod today |
|---|---|---|---|---|
| 1 | `:90` | `WHATSAPP_PAY_ENABLED` is False | **No** | **FIRES** |
| 2 | `:92` | `WHATSAPP_PAY_CONFIGURATION_NAME` is empty | warning | would fire |
| 3 | `:95` | Importer line1, city, zone, postal or country is empty | warning (every call) | would fire |
| 4 | `:98` | Phone not in a non-empty `WHATSAPP_PAY_ALLOWLIST` | **No** | passes (empty) |
| 5 | `:202` | No customer row for the phone | **No** | per user |
| 6 | `:222-229` | Meta rejected the send (non-2xx, or no `messages` in the response) | error with body; order `dispatch_failed` | would fire until Meta enables payments |
| 7 | `:230` | Any other exception | error | — |

---

## 2. Env: required vs actual (`backend/.env`)

| Key | Default | In .env | Needed |
|---|---|---|---|
| `WHATSAPP_PAY_ENABLED` | False | **absent** | **Yes** |
| `WHATSAPP_PAY_CONFIGURATION_NAME` | "" | **absent** | **Yes**, must match WhatsApp Manager exactly |
| `WHATSAPP_PAY_IMPORTER_ADDRESS_LINE1` | "" | **absent** | **Yes** |
| `WHATSAPP_PAY_IMPORTER_CITY` | "" | **absent** | **Yes** |
| `WHATSAPP_PAY_IMPORTER_ZONE_CODE` | "" | **absent** | **Yes**, state code e.g. `MH` |
| `WHATSAPP_PAY_IMPORTER_POSTAL_CODE` | "" | **absent** | **Yes** |
| `WHATSAPP_PAY_IMPORTER_ADDRESS_LINE2` | "" | absent | Recommended (360dialog marks it required) |
| `WHATSAPP_PAY_ALLOWLIST` | "" (= everyone) | absent | Set to test numbers for staged rollout |
| `WHATSAPP_PAY_TAX_PERCENT` | 0 | absent | Confirm GST treatment |
| `META_WHATSAPP_TOKEN` / `META_PHONE_NUMBER_ID` / `META_APP_SECRET` | "" | set | Yes (all OK) |
| `DEBUG` | True | **appears twice** (`true` on line 8, `false` on line 81) | Remove the duplicate |

---

## 3. Payload schema compliance (India payment gateway spec)

Built at `whatsapp_pay_service.py:119-182`, sent via `meta_whatsapp_service.py:364-415`.

```json
{
  "messaging_product": "whatsapp", "recipient_type": "individual", "to": "91XXXXXXXXXX",
  "type": "interactive",
  "interactive": {
    "type": "order_details",
    "body": {"text": "..."},
    "action": {
      "name": "review_and_pay",
      "parameters": {
        "reference_id": "mgv_<24hex>",
        "type": "digital-goods",
        "payment_settings": [{
          "type": "payment_gateway",
          "payment_gateway": {
            "type": "razorpay",
            "configuration_name": "<WHATSAPP_PAY_CONFIGURATION_NAME>",
            "razorpay": {"receipt": "mgv_...", "notes": {"whatsapp_id": "...", "reference_id": "..."}}
          }
        }],
        "currency": "INR",
        "total_amount": {"value": 50000, "offset": 100},
        "order": {
          "status": "pending",
          "expiration": {"timestamp": "<now+900>", "description": "This recharge order has expired."},
          "items": [{
            "retailer_id": "wallet_recharge_500", "name": "Moraa Studio Wallet Recharge",
            "amount": {"value": 50000, "offset": 100}, "quantity": 1,
            "country_of_origin": "IN", "importer_name": "MORAA STUDIO",
            "importer_address": {"address_line1": "...", "city": "...", "zone_code": "MH", "postal_code": "...", "country_code": "IN"}
          }],
          "subtotal": {"value": 50000, "offset": 100},
          "tax": {"value": 0, "offset": 100, "description": "Inclusive of taxes"}
        }
      }
    }
  }
}
```

**All fields pass:**
- `interactive.type` is `order_details` and `action.name` is `review_and_pay`.
- `payment_settings[].payment_gateway` has the current shape (no deprecated top-level `payment_type`/`payment_configuration`).
- Currency is INR with offset 100 (paise).
- `reference_id` is 28 chars from the allowed charset.
- Items: 50000 × 1 = subtotal 50000. Subtotal + tax 0 = total 50000. **They match.**
- The importer fields are correct: Meta requires them whenever there is no `catalog_id`, even for digital goods.

### Wrong vs right (for reference)

These are the shapes that would open a browser. The code uses **none** of them:

```diff
- "interactive": {"type": "cta_url", "action": {"name": "cta_url", "parameters": {"url": "https://rzp.io/..."}}}
- "interactive": {"type": "button", ...}                       # with a link in the body
- "parameters": {"payment_type": "upi", "payment_configuration": "..."}   # old shape
+ "interactive": {"type": "order_details",
+   "action": {"name": "review_and_pay",
+     "parameters": {"payment_settings": [{"type": "payment_gateway",
+       "payment_gateway": {"type": "razorpay", "configuration_name": "<exact name>"}}], ...}}}
```

The current fallback sends a button or plain text with a URL (sites 1-5). That URL is what opens Chrome or Safari.

---

## 4. Code gotchas that cause silent fallbacks

1. **Gates 1, 4 and 5 log nothing.** The bypass is invisible in the logs.
2. **Meta error codes are never parsed.** A permanent error ("payments not enabled", a wrong config name, 141010) is treated like a transient one. Every prompt retries, writes a `dispatch_failed` row, and falls back to the link.
3. **Double payment on timeout.** A timeout at `meta_whatsapp_service.py:410` returns False, but Meta may still have delivered the order. The user then gets both the in-chat order and a link.
4. **Failed payment sends the link while the in-chat order is still payable** (`:291-296`). This also risks a double payment.
5. **Expired orders:** nothing is sent. There is no `canceled` order_status and no retry.
6. **The reconciliation sweep is never scheduled** (`reconcile_pending_orders` `:419` is called only from tests). A missed webhook means the payment is never credited.
7. **`pg_transaction_id` assumption** (`:332`). A credit requires this field in the payment lookup response, and only the test fixtures confirm it exists. If Meta omits it, **every real payment becomes `amount_mismatch` and is never credited**. Verify it with one real lookup before go-live.
8. **Razorpay webhook race** (`payment_routes.py:85-118`). If Razorpay's `payment.captured` arrives first, it credits by `notes`/`contact` and never closes the in-chat order card. If `contact` differs from the WhatsApp number, the wrong wallet could be credited.
9. **Low-balance photo path (site 2)** never tries native when `customer is None`.
10. `meta_whatsapp_service.py:106`: a webhook change that has `statuses` skips any `messages` in the same change.

---

## 5. Action plan

### Phase 0: Meta prerequisites (blocking; nothing in code can bypass them)
1. **Business verification:** resubmit it in Business Manager → Security Center and get it approved (it was rejected with 141010).
2. **Razorpay:** the account must be live and KYC-complete. The account owner must be available to authorise the OAuth link.
3. **WhatsApp Manager → Account tools → Payment configurations → Create:**
   - Provider: **Razorpay**
   - Name: e.g. `moraa_studio_razorpay` (60 characters max; this is what goes in `.env`)
   - Complete the Razorpay authorisation until the status shows **Active**.
4. **Eligibility:**
   - The WABA and phone number are India (+91) and the currency is INR.
   - The number is on the Cloud API.
   - The Meta app is subscribed to the `messages` webhook field. Payment statuses arrive as `statuses[].type == "payment"`.
5. Payments work only inside the 24-hour customer-service window, and the customer needs a WhatsApp client version that supports payments. Older clients show "update WhatsApp", not a browser.

### Phase 1: Config (no code)
```env
WHATSAPP_PAY_ENABLED=true
WHATSAPP_PAY_CONFIGURATION_NAME=moraa_studio_razorpay   # exact match
WHATSAPP_PAY_IMPORTER_ADDRESS_LINE1=...
WHATSAPP_PAY_IMPORTER_ADDRESS_LINE2=...
WHATSAPP_PAY_IMPORTER_CITY=...
WHATSAPP_PAY_IMPORTER_ZONE_CODE=MH
WHATSAPP_PAY_IMPORTER_POSTAL_CODE=...
WHATSAPP_PAY_ALLOWLIST=91XXXXXXXXXX        # your test number(s) only, at first
```
Also remove the duplicate `DEBUG` line.

### Phase 2: Minimal code changes (for "never leave WhatsApp")
1. **Log every gate** (`:90`, `:98`, `:202`) with a reason, so a bypass is visible.
2. **Add a `WHATSAPP_PAY_STRICT` flag.** When it is true, sites 1-6 never send a URL: a native failure sends a short "payment is temporarily unavailable, please try again in a few minutes" text and an admin alert. **Turn it on only after Phase 0 is done and the allowlisted tests pass.** Before that, strict mode means nobody can pay.
3. **Parse Meta error codes** in `_post_message_payload`. Store the code in `last_error`. On a permanent error, trip a circuit breaker (for example, disable native for 1 hour and alert) instead of retrying on every prompt.
4. **Failed or expired order:** send `order_status: canceled` for the old reference and a **fresh `order_details`**, never a link.
5. **Site 2:** create or find the customer before the native attempt, or route it through the same helper as the other sites.
6. **Schedule `reconcile_pending_orders`** every 5 minutes (a FastAPI startup background loop is enough, since there is no Celery beat).
7. **Razorpay webhook:** if `notes.reference_id` starts with `mgv_`, defer to the native reconcile path (or close the order card) instead of crediting by contact.
8. **Remove the static `rzp.io` URL** as a last resort once strict mode is on.

### Phase 3: Testing
1. Set the allowlist to one number, `ENABLED=true`, and restart. Send "recharge 500".
2. Expected: an in-chat "Review and pay" card. The log shows no "enabled but … empty" warning and no "Meta send whatsapp pay order failed".
3. If Meta rejects the send, the error body in the log names the reason (config not linked, not eligible, and so on). Fix it on the Meta side.
4. Pay ₹500 with UPI inside the sheet, then check:
   - the webhook `statuses[].type=payment`
   - that the lookup returns `captured` with `pg_transaction_id` (**gotcha #7**)
   - the wallet was credited once
   - the `order_status completed` card
5. Replay the same webhook and confirm there is no second credit (money-once claim).
6. Let an order expire and cancel a payment. Confirm the behaviour matches Phase 2.4.
7. Widen the allowlist to a few users, then clear it (everyone), then turn on `STRICT`.
