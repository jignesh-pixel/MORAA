# Operations runbook

Written 2026-10-02. For whoever runs the live system. Plain steps; a developer can follow them without reading the code.

## 1. What tells you the system is healthy

| Address (answers only to requests made on the server itself or inside your private network; a request that arrives through the public internet / reverse proxy with forwarding headers gets 404 on purpose, so check these from the server or by your monitoring tool on the private address) | What it answers |
|---|---|
| `/health/live` (also `/health`) | The program is running. Never touches the database. |
| `/health/ready` | The program can really work: the database answers, the database is at the newest version, and the upload folder can be written. Answers 503 with a reason when any of those fail. Use this for the deploy "is it up yet?" check and for any load balancer. |
| `/metrics` | Numbers for a dashboard (see section 4). |

## 2. Alerts to your WhatsApp

Set `OPS_ALERT_WHATSAPP_NUMBERS` to your number (international format, no `+`, several separated by commas). Every 5 minutes the system checks
a few things and sends ONE short message per problem, then stays quiet about that problem for an hour (`OPS_ALERT_COOLDOWN_MINUTES`).

| Message starts with | What it means | What to do |
|---|---|---|
| "X% of the last N finished image orders failed" | More than 15% of orders in the last 24 h failed (needs at least 5 orders) | Check the image providers (Gemini / OpenAI) and `/metrics` provider numbers. Failed orders are refunded automatically. |
| "N of today's M image calls are used" | 80% of the daily ceiling is used | Raise `MAX_GENERATIONS_PER_DAY` if you want to keep serving, or expect new orders to be declined at the limit (customers are told nothing was charged). |
| "N paid Razorpay payment(s) ... waiting for you to credit them" | A payment arrived with no usable phone number | Find the payer in the Razorpay dashboard, then a developer runs `credit_pending_payment(db, payment_id, phone)` (safe to run twice). |
| "N payment item(s) ... need a look" | A refund you could not fully take back, a chargeback opened, or an unusual payment | Look at the audit rows `razorpay_clawback` / `razorpay_payment_review` with status `pending`. |
| "N paid order(s) have been stuck for over 20 minutes" | Orders took money but did not finish | The recovery job refunds them; look for a slow provider or Meta problem. |

If no number is configured the alerts are only written to the log (search for `OPS_ALERT`).

## 3. Backups (do this before the first production deploy, then every night)

The wallet balances live ONLY in the Supabase database. Turn on Supabase's own point-in-time recovery if your plan has it, AND keep your own dumps:

```
python scripts/db_backup.py --out-dir backups --keep 14
```
It needs the PostgreSQL client tools (`pg_dump`, version 15 or newer). It never prints the database address or password. Schedule it nightly
(Windows Task Scheduler or cron) and copy the `backups` folder somewhere else (another disk or cloud storage).

**Restore drill, every quarter** (a backup nobody has restored is not yet a backup):
1. Create an empty scratch PostgreSQL database on your own machine (NOT Supabase).
2. `set RESTORE_TEST_DATABASE_URL=postgresql://user:password@localhost:5432/restore_scratch`
3. `python scripts/db_restore_check.py backups/moraa-<date>.dump`
4. It must end with `RESULT: restore verified` (customer and ledger counts look right, no balance differs from its ledger).
Write the date of each successful drill here: ____

## 4. Numbers worth watching (`/metrics`)

- `moraa_orders{status=...}`: orders in the last 24 h by status (a growing `failed` / `processing` is the early warning).
- `moraa_generation_slots_in_use` against `moraa_generation_slots_cap`: how much of today's image ceiling is used.
- `moraa_provider_calls_total{provider,outcome}` and `moraa_provider_fallbacks_total`: how often Gemini fails and OpenAI takes over.
- `moraa_pending_payments`: payments waiting for you.
- `moraa_http_requests_total{status="5xx"}` and `moraa_http_request_seconds`: errors and slowness.
- `moraa_meta_send_total{outcome}`: whether WhatsApp messages are going out.
- `moraa_db_up`: 1 when the database answers.

## 5. Releasing a new version

1. Take a backup (section 3).
2. `python scripts/release.py` runs the database upgrade and checks it. It must print `RESULT: ready to start the new version`. If it does not, do NOT start the new version.
   (`--check-only` checks without changing anything.)
3. Start the new version. In production it refuses to start on an out-of-date database, so a forgotten step cannot slip through.
4. Open `/health/ready`: it must say `ready`.
5. To go back: restore the backup, then run the old version (never run the old version on an upgraded database).
The one-off Phase 2 pause (the first upgrade past version 0008) is described in `docs/PHASE_2_DEPLOY_RUNBOOK.md`.

## 6. Error reports (optional)

Set `SENTRY_DSN` (and install `sentry-sdk`) to get crash reports in Sentry. Customer names, phone numbers, request bodies, cookies and
login headers are removed before anything is sent.

## 7. Logs

Each request has a short id that appears on every log line it causes, including the background job that makes the images, so one
customer's order can be followed from their tap to the delivery (search the id, or the `ingestion_id`). `LOG_JSON=true` writes one JSON
line per log entry for a log collector. Logs never contain message text or full phone numbers.

## 8. Administrators and logins

Set `ADMIN_USERNAMES` (comma-separated usernames) so only they can use the retry-delivery and failure-rate endpoints. Until at least one
administrator is configured those endpoints stay open to any logged-in user (as before) and the log says so. A deactivated account stops
working immediately. A refresh token can be used once; using one twice logs that account out everywhere.

## 9. Behaviour changes to know about

- `DEV_RELOAD` (auto-restart on code change) is now off unless you set it to true; the old always-on reload is gone.
- The analyses list returns at most 100 items per call by default (use `limit` and `offset` to page).
- Set `GEMINI_IMAGE_MODEL` explicitly in the production environment so a future default change can never alter image quality unannounced.
- A refresh token used twice within 20 seconds is just refused (a double tap); used again later it logs that account out everywhere.

## 10. Background jobs that survive a restart (outbox) and who runs the periodic jobs

- Paid orders, invoice sends and ops-team forwards are first written to the `outbox_jobs` table. If the server is restarted or
  crashes before one starts, the sweep (every 20 seconds, 90 seconds grace for orders) starts it. A job that fails is retried
  with growing waits (30 s, 2 min, 10 min, 30 min, 1 h) and, after 6 tries, parked as `dead` and you get a WhatsApp alert
  ("background job(s) gave up"). `/metrics` shows `moraa_outbox_jobs{status=...}`.
- Several server processes may run at once: each periodic job (payment checks, stuck-order refunds, alerts, retention) has a
  one-row lease in `scheduler_leases`, so only one process runs it. A process that shuts down gracefully gives its leases back;
  after a crash the others take over within a few minutes.

## 11. Privacy: consent, retention, erasure

- Consent notice: `CONSENT_REQUIRED=true` with your wording in `CONSENT_NOTICE_TEXT` (see `docs/QUESTIONS_FOR_MORNING.md`). Who agreed
  to which version, and when, is in `consent_records`. Raise `CONSENT_VERSION` when the wording changes.
- Retention: `RETENTION_ENABLED=true` (only after the periods are approved) deletes customer photos 90 days after a finished order
  and hides phone numbers in audit notes after 30 days, once a day. It never touches money records.
- Erasure: a customer sends DELETE MY DATA and then CONFIRM DELETE. By hand: `python scripts/erase_customer.py --phone <number>` shows
  what would be erased; add `--yes` to do it. Photos and personal details are removed; the wallet ledger, payments, refunds and invoices
  stay for the legal period. A customer with money in the wallet or a team account is refused.

## 12. Costs

Every AI image call is recorded in `provider_calls`. Set `COST_PER_CALL_GEMINI_RUPEES` / `COST_PER_CALL_OPENAI_RUPEES` to see rupees,
and `OPS_ALERT_DAILY_COST_RUPEES` to be warned on WhatsApp when a day passes your line. `python scripts/cost_report.py --days 7` lists
the last week. Team orders have their own daily limit (`MAX_ADMIN_GENERATIONS_PER_DAY`, default 200).

## 13. Provider protection and fairness (settings)

`CIRCUIT_BREAKER_FAILURES` (5) and `CIRCUIT_BREAKER_COOLDOWN_SECONDS` (60): after 5 failures in a row a provider is skipped for a
minute so orders fail fast and are refunded instead of waiting for timeouts. `IMAGE_PROVIDER_RPM` and `MAX_CONCURRENT_PROVIDER_CALLS`
(both 0 = no limit) smooth bursts to the provider's quota; a single Studio Shot is served ahead of Pack images when the limit is
reached. `MAX_INFLIGHT_ORDERS_PER_CUSTOMER` (3) limits paid orders one customer can have in progress at once.
