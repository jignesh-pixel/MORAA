# Developer handover: Moraa GemVision backend (written 2026-10-03)

Read this first. It says what was built, where it is, how it was checked, and what is still yours to do. Every claim here was
checked against the code and the gate reports in `docs/phase-gates/`. Nothing described below has been applied to production.

## 1. Where the code is

Everything is in this repository, on local branches that build on each other (the owner pushes nothing; you push and open the PRs):

| Branch (tag) | What it adds |
|---|---|
| `phase-2-financial-core` (`phase-2-gate-passed`) | Wallet ledger, once-only payments/refunds/chargebacks, capacity check before charging |
| `phase-3-runtime-hardening` | Event-loop protection, provider timeouts and retries, shared daily AI spend counter |
| `phase-4-operations` | Health/metrics, WhatsApp alerts, request ids, indexes, auth hardening, backup/restore/release scripts |
| `phase-5-scale` | Durable outbox, scheduler leases, circuit breaker, per-customer limit, honest ETA, Docker files |
| `phase-6-compliance` | Consent, retention, erasure, receipts/IGST, cost log, team limit, bulk photo orders, GST vendor hook, ERPNext-only invoices |
| `phase-7-dashboard` (`phase-7-gate-passed-except-timing`) | WhatsApp chat dashboard (backend + Next.js page), Drive archive, Google sign-in |

`phase-7-dashboard` contains all of the above, so it is the branch to review and deploy. Phase 0 and 1 are already on GitHub (`phase-1-verified`).
Tags with `-except-timing` mean: all tests, prompts, audit and probes pass; two speed-sensitive load checks need a re-run (section 5).

## 2. What was built (by area)

**Money (phase 2).** `wallet_transactions` ledger written in the same transaction as every balance change; invariant `SUM(ledger) == balance`; DB CHECK balance >= 0.
Razorpay payments credited once (unique audit claim); refund/dispute events take money back (over-balance: take what is there, flag the rest `pending` for a person).
Payments without a phone go to `pending_payments`. WhatsApp Pay and payment-link reconcile sweeps. Phone numbers normalised (`app/utils/phone.py`).
Orders are declined before charging when the daily generation limit/kill switch leaves no room. Duplicate messages deduped (`processed_messages`).
Owner rule: no refund because a customer dislikes an output; refunds only when we failed to deliver.

**Runtime (phase 3).** Dedicated thread pools (`app/utils/executors.py`: cpu, io, net, disk), DB connections released before long waits, every outside call has a timeout,
rate-limit vs quota-exhaustion classification, shared `generation_spend` counter with release on failure, pack deadline bounds generation only, native async Gemini.

**Operations (phase 4).** `/health/live`, `/health/ready`, `/metrics`; WhatsApp alerts with DB cooldown (`alert_service`); Sentry scrubber (optional);
request-id in logs; migration 0014 indexes; auth: `is_admin`, token revocation, single-use refresh tokens (0015); scripts `db_backup.py`, `db_restore_check.py`, `release.py`.

**Scale (phase 5).** `outbox_jobs` (0016): paid orders, invoices, ops forwards and Drive jobs survive restarts (claim, retry with backoff, dead-letter + alert);
`scheduler_leases` (0017): one process runs each periodic job; provider circuit breaker + token bucket + priority gate; `MAX_INFLIGHT_ORDERS_PER_CUSTOMER` (3);
`eta_service` (measured delivery time); pure-ASGI middleware; streamed upload caps; Dockerfile/compose/Caddy (never built, see section 4).

**Compliance and features (phase 6).** `consent_records` (0018, switched off until wording is supplied); retention + "DELETE MY DATA" (`data_lifecycle.py`, off until approved);
`provider_calls` cost log (0019); team daily ceiling (`MAX_ADMIN_GENERATIONS_PER_DAY`); Meta media id/host validation; optional "ops " prefix;
IGST template/place of supply (`gst_places.py`, inactive until two settings are filled); GST vendor adapter (`GST_PROVIDER=http`); invoices are ERPNext-only when
`ERPNEXT_INVOICE_ENABLED=true` (no local fallback series; failures retry through the outbox); bulk photo orders (`bulk_orders.py`, migration 0020 `group_id`):
photos within 45 s of each other are held, one "N photos, Rs X, confirm?" message is sent, confirm charges every photo (one ledger row each) in one transaction and
runs 4 at a time; "choose one by one" falls back to the normal per-photo buttons.

**Dashboard (phase 7).** Tables `chat_messages`, `order_outputs`, `invoice_records` (0021). Writers in `app/services/chat_log.py` are best-effort and never block a message or order:
inbound events (`meta_webhook` receive loop), outbound sends (`_post_message_payload`), produced images (`_upload_and_keep`, written to `uploads/outputs/` after upload),
invoices (`billing_service`). Read-only API `app/api/routes/dashboard.py` (admin only; signed 10-15 minute image links); Google sign-in `POST /api/auth/google`;
Drive archive `app/services/drive_archive.py` via outbox; frontend `ChatsPage.tsx` + `components/chats/*` + `services/chat-dashboard.service.ts`.
Chats and produced images are kept 90 days (`RETENTION_CHAT_DAYS`); erasure removes a customer's chat record and Drive copies.
A bug found on the way: the start-up orphan clean-up would have deleted `uploads/outputs/`; it now keeps it.

## 3. Database migrations (apply in order; production is at 0008)

0009 wallet ledger (refuses to run if any negative balance exists) -> 0010 processed_messages -> 0011 pending_payments -> 0012 payment reconcile columns/links -> 0013 generation_spend ->
0014 hot-path indexes -> 0015 auth hardening -> 0016 outbox_jobs -> 0017 scheduler_leases -> 0018 consent_records -> 0019 provider_calls -> 0020 whatsapp_ingestions.group_id ->
0021 chat dashboard tables. All are idempotent; downgrades are deliberate no-ops (some tables hold audit/money history). Never run `alembic downgrade base` on a live database.
Deploy order matters: **upgrade the database first, then start the new code** (in production the app refuses to start on an old schema; in development the new column
`group_id` would make ingestion queries fail on a database that has not been migrated). Procedure: `docs/PHASE_2_DEPLOY_RUNBOOK.md` (write freeze for 0009) and
`docs/OPERATIONS_RUNBOOK.md` sections 3 and 5 (backup, `scripts/release.py`).

## 4. What is still yours to do (in this order)

1. **Push and review.** Push the branches, open PRs (do not merge to main without the owner), get GitHub CI green (`.github/workflows/ci.yml`). Rename the tags to `phase-N-verified` once CI is green.
2. **Re-run the two timing checks** (`backend/tests/load_scenarios/run_load_scenarios.py --only h,i`) on a cool, plugged-in machine or in CI. They pass at full CPU speed
   (h about 14 s, i about 1.1 MB per image) and fail when the CPU is throttled (the dev laptop dropped to about 62% performance). Older code behaves identically.
3. **Backups first.** Install PostgreSQL client tools (>= 15), run `python scripts/db_backup.py`, then do a restore drill with `scripts/db_restore_check.py` into a scratch database. Neither script has been run end to end (no pg_dump on the dev machine).
4. **Production upgrade** at a time the owner chooses: freeze writes, backup, `python scripts/release.py`, check `/health/ready`, verify wallet invariant with `scripts/wallet_negative_check.py` (read-only). The owner has agreed in principle; ask for the go time.
5. **Secrets:** rotate the Meta, Razorpay, Gemini, OpenAI and Supabase keys flagged in the audit (SEC-10); delete `.env.bak-*` files; confirm Row Level Security is on for every Supabase table (DATA-5).
   Set `SECRET_KEY` explicitly in production (signed dashboard links and JWTs use it; with several workers it must be the same everywhere).
6. **Settings for production** (full list with examples: `docs/ENV_TO_ADD.md`): `OPS_ALERT_WHATSAPP_NUMBERS=919699899825,919820666332`, `RETENTION_ENABLED=true` (90/30 days approved),
   `CONSENT_*` once the owner supplies the notice, `SENTRY_DSN` (then `pip install -r requirements.txt`: `sentry-sdk==2.19.2` was added to requirements but never installed here),
   `ADMIN_USERNAMES`, `RECHARGE_PAYMENT_URL`, pin `GEMINI_IMAGE_MODEL`, `DASHBOARD_ALLOWED_EMAILS`, `GOOGLE_CLIENT_ID`. Create the first dashboard login with `scripts/create_dashboard_user.py`.
   Alerts go to personal numbers: WhatsApp only allows that within 24 h of the person last messaging the business number; consider an approved template.
7. **ERPNext:** with `ERPNEXT_INVOICE_ENABLED=true` and the keys (`docs/ENV_TO_ADD.md`) test against the real ERPNext company: invoice creation, payment entry, PDF fetch, retry after ERPNext downtime, and IGST (`ERPNEXT_COMPANY_STATE_CODE`, `ERPNEXT_TAX_TEMPLATE_INTERSTATE`; the accountant provides both). All tested only against a fake.
8. **GST verification:** choose a vendor (Razorpay has no public GST API; e.g. Cashfree, Appyflow, Surepass, gstinapi.in), then set `GST_PROVIDER=http`, `GST_API_URL` (contains `{gstin}`), `GST_API_KEY`, `GST_VERIFICATION_ENABLED=true`. The adapter expects the GST-portal taxpayer JSON (`sts`, `tradeNam`, `lgnm`, `pradr`); override `HttpGstProvider.parse` if the vendor differs. With verification on, only verified GSTINs print on invoices.
9. **Google Drive archive:** create a Google OAuth client, obtain a refresh token for the Drive owner's account, set `DRIVE_*`/`GOOGLE_DRIVE_*`. Tested only with a fake; verify upload, folder, links, deletion on retention/erasure. The owner uses a personal account now (limits can grow later; a Workspace shared drive is the cleaner long-term choice). Drive file names contain the customer's phone number.
10. **Hosting (AWS Lightsail):** `backend/Dockerfile`, `docker/docker-compose.yml`, `docker/Caddyfile`, `docker/env.example` exist but were never built or run (no Docker on the dev machine); expect small fixes. Uploads live in a volume on that one server. No Redis: not needed (outbox + leases cover durability and single-runner jobs). The public-host guard returns 404 for everything except the two webhooks when requests arrive via the proxy, **including the dashboard API**: decide how the dashboard is reached (same machine/VPN, or add a private route) before exposing it.
11. **Frontend:** the dashboard page is in the existing Next.js app. Set `NEXT_PUBLIC_API_URL` (and `NEXT_PUBLIC_GOOGLE_CLIENT_ID` for Google sign-in). Decide how the frontend is hosted and protected (the app already has a password gate in `src/proxy.ts`).
12. **Review the owner-facing wording** in WhatsApp messages added here (bulk prompt, per-customer limit decline, interrupted photo, erasure messages, consent) and Meta-approve a template if you want alerts or follow-ups outside the 24 h window.

## 5. Known limits and decisions to keep in mind

- Orders per customer in progress: 3 (`MAX_INFLIGHT_ORDERS_PER_CUSTOMER`); bulk orders are exempt (up to 50 photos per confirmation, `BULK_MAX_PHOTOS`).
- Team (ADMIN) orders have their own ceiling, default 200 images a day (`MAX_ADMIN_GENERATIONS_PER_DAY`); the owner has not confirmed the number. Gemini quota (`IMAGE_PROVIDER_RPM`, `MAX_CONCURRENT_PROVIDER_CALLS`) and prices per call (`COST_PER_CALL_*_RUPEES`) are unset (0 = off): ask the owner.
- Wallet balances are never refunded (owner decision): "DELETE MY DATA" is refused while a balance remains.
- Invoices: if ERPNext is down the customer still gets the "payment received" message and the invoice retries (6 attempts over about 2 hours, then an alert). After a dead job a person must act.
- The dashboard only has data from the day it goes live. Chats older than that cannot be rebuilt (Meta does not provide history).
- Not done on purpose: splitting `meta_whatsapp_service.py`/`meta_webhook.py` and merging duplicate generation code (touches every money path and hundreds of test patches; do it after launch, in its own verified step);
  merging the seven earring prompt routes (five files are frozen by the prompt freeze guard); signed image links / object storage for customer photos (`/uploads` is still served locally; the guard keeps it private); a Redis/Celery queue (outbox covers it).
- Image prompt builders are locked: `python scripts/phase_gate.py` must keep reporting `prompts: ... changed: 0`.
- The load harness (`tests/load_scenarios/`) runs provider time 100x faster than real life. Scenario `l` measures the dashboard's image-copy cost separately; scenario `i` disables the chat record on purpose (see comments in `run_load_scenarios.py`).

## 6. How to check the work

From `backend/`: `ruff check .`, `pytest -q -p no:cacheprovider -W ignore`, `MORAA_TEST_DB=postgres pytest ...` (needs the `pgserver` package, already in the dev venv),
`python scripts/phase_gate.py --phase "<name>" --live-readonly` (the live step fails until production is upgraded). Gate reports: `docs/phase-gates/verification-2026-10-03/`.
Reference documents: `docs/MORNING_SUMMARY.md` (owner summary), `docs/ROADMAP_STATUS.md`, `docs/OPERATIONS_RUNBOOK.md` (sections 1-15), `docs/QUESTIONS_FOR_MORNING.md` (open owner questions), `docs/DASHBOARD_PLAN.md`, `docs/ENV_TO_ADD.md`, `worklog/*.pdf` (printable per-phase audit logs).

## 7. Open owner decisions (do not guess these)

Production upgrade time; key rotation/Supabase check; privacy notice wording (owner does not want the AI-providers-abroad line or the how-to-delete line; a lawyer should still review before enabling consent);
GST vendor choice; Gemini quota and costs; team daily limit; whether team commands need the "ops " prefix (parked); Google Drive account long-term; how the dashboard is exposed.
