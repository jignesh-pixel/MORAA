# MORAA GemVision Backend Audit — Load, Cost, Failure Modes

Date: 2026-09-29. Method: static code reading by three parallel agents, plus log analysis (223 real Gemini image calls, 18–21 Sept). Load-test results are in `load_simulation_results.md`; the queue model is in `concurrency_vs_queue.md`. Numbers marked *(assumed)* need verifying.

## 0. Ground truth (corrects some assumptions)

| Assumption | Reality |
|---|---|
| Pack = 8 outputs per input | **Pack 1 = 6 outputs** (`CATALOG_PACK_STYLES`, `meta_whatsapp_service.py:36-43`). Studio Shot (white background) = 1 output, ₹50. Pack = ₹500 |
| Celery runs the image jobs | **No.** WhatsApp jobs run as FastAPI `BackgroundTasks` on one event loop. Celery only serves dashboard analysis, and `.env` sets `CELERY_TASK_ALWAYS_EAGER=true` |
| Images are batched | **No.** Each photo gets its own row, its own button prompt and its own tap. No album or debounce logic |
| Fidelity QA runs | It is a stub returning "manual QA required" |
| 4 workers | `WORKERS=4` is never passed to uvicorn. It runs as one process |

Live path per order: 1 Gemini pre-check (`gemini-3.6-flash`, before any charge), then 1 image call (Studio Shot) or 6 parallel image calls (Pack). No analysis or prompt-generation model calls. Zero retries. Fallback to OpenAI `gpt-image-1` exists but has no credits (74+ failures in logs).

## 1. Cost, tokens and time per scenario (Gemini primary, no failures)

Assumptions: 4 chars per token, ~1.5k tokens per reference image, ~1.2k tokens per output image, ₹88 per USD, Gemini 3.1 Flash Image at about $0.067 per image. Prompts measured: pack prompts total 72.4k chars (~18k tokens); Studio Shot prompt 6.6k chars.

| Scenario | API calls | Tokens in / out | USD | INR |
|---|---|---|---|---|
| 1 photo, Studio Shot | 2 | 6k / 1.6k | 0.08 | ₹7 |
| 10 photos, Studio Shot | 20 | 60k / 16k | 0.76 | ₹67 |
| 100 photos, Studio Shot | 200 | 600k / 160k | 7.6 | ₹669 |
| 1 photo, Pack (6 outputs) | 7 | 34k / 8.6k | 0.44 | ₹39 |
| 10 photos, Pack | 70 | 340k / 86k | 4.4 | ₹388 |
| **100 photos, Pack (600 outputs)** | **700** | **3.4M / 860k** | **44** | **₹3,881** |

Revenue for the same 100 packs is ₹50,000, so API cost is about 7.8%. If OpenAI served everything, a pack costs about $1.6–1.9 (₹140–170), still profitable. **Failures, refunds and partial-pack credits matter more than API spend.**

Measured latency (logs): mean 17.4 s per image call, p50 13.8 s, p95 26.9 s, max 142 s. Delivery adds about 10–20 s (sequential Meta upload plus a 0.8 s throttle per image).

Wall clock for 100 photos (17.4 s per call, tail ignored):

| Mode | Studio Shot ×100 | Pack ×100 (600 calls) |
|---|---|---|
| Sequential | 29 min | 2.9 h |
| Concurrency 5 | 5.8 min | 35 min |
| Concurrency 10 | 2.9 min | 17 min |
| Concurrency 20 | 1.5 min | 8.7 min |

Actual behavior today (unbounded fan-out): a 100-pack burst does not run at "concurrency 600". It fails, per the queue model. Only a fraction get through Gemini's quota, the DB pool is exhausted at about 15 in-flight orders, and about 5 GB of RAM is needed. The customer-side flow also needs 100 separate button taps.

## 2. Customer scenarios

| # | Scenario | What happens now | Verdict |
|---|---|---|---|
| S1 | One photo at a time | Ingest inline (5–15 s), 2 buttons, atomic debit on tap, background generation, ~45 s end to end for a pack | **Works** |
| S2 | 10 photos at once | 10 rows, 10 prompts, 10 taps. If Meta bundles them in one payload they are ingested sequentially, 1–2 min, beyond Meta's ~20 s timeout, so Meta retries (dedup stops double work) | Works but slow, poor UX |
| S3 | 100 photos (WhatsApp limit) | Same as S2 ×10, rate limiter can 429 Meta, thread pool starves, later ingestion waits behind generation | **Degrades badly** |
| S4 | Wallet covers 6 of 10 | Per-photo atomic debit: 6 succeed, 4 declined and reset to `awaiting_choice`. Correct | **Works** |
| S5 | Recharge mid-job | Razorpay credit is idempotent. Failure cases in F5 | Works, edge cases |
| S6 | Provider 429 | Gemini per-minute 429 is classed as quota exhaustion and does not fall back | **Fails** |
| S7 | Backend restart mid-job | Jobs are lost. Recovery runs only at startup, refunds orders with `amount_charged > 0` | Partial |
| S8 | Meta re-delivers webhook | Image: UNIQUE dedup works. Text and buttons: no dedup (duplicate recharge links) | Partial |
| S9 | 5 customers at once | Contend for 6–12 threads and 15 DB connections | Degrades |
| S10 | Half the pack fails | Charged ₹500, delivered N of 6, no refund, styles may be mislabeled | **Weak** |

## 3. Failure points, ranked

### Critical / High

| ID | Where | Why it fails | Fix |
|---|---|---|---|
| F1 | `database.py:16-21`, `meta_whatsapp_service.py:1158-1176`, `meta_webhook.py:601-635` | Sync SQLAlchemy inside `async def`. A transaction stays open across download, pre-check and generation, pinning one connection for 30–90 s. Pool is 5+10. The 16th checkout blocks the event loop for 30 s, freezing every webhook | Commit/close session before every network await, use short-lived sessions in background jobs, set pool params explicitly, run DB work in `to_thread` or use async SQLAlchemy |
| F2 | `middleware/rate_limit.py` | Per-IP 100 requests/min, in-memory, applies to Meta and Razorpay webhooks (small IP set, plus ~3 status webhooks per outbound message). ~3–4 orders/min exhaust it, then Meta retries with backoff | Exempt signature-verified webhook paths, use Redis per-key limits for the rest |
| F3 | `gemini_image_provider.py:101,140` | No timeout, default thread pool `min(32, cpu+4)` (6 threads on 2 vCPU) shared with pre-check. One pack fills it. Max seen 142 s | `HttpOptions(timeout≈60 s)`, dedicated executor plus semaphore, overall pack deadline, deliver what finished |
| F4 | `meta_webhook.py:1031-1054` | Image ingestion is synchronous in the webhook. A crash leaves a row in `received` that Meta's retry skips, so the customer never gets buttons. No sweeper | Verify signature, insert row, enqueue, return 200. Add sweeper for stale `received` and `awaiting_choice` |
| F5 | `wallet_service`, `meta_webhook.py:788-791` | Debit commits alone, `amount_charged` and status are set later. A crash between them leaves money taken and no refund path | Single transaction, or a `wallet_transactions` ledger with unique `(ingestion_id, kind)` |
| F6 | `meta_whatsapp_service.py:813-847` | Refund is check, credit and commit, then audit insert. Two racing failure paths (retry endpoint and startup sweeper) can both refund | Insert audit claim and flush first, then credit, then commit once |
| F7 | `payment_routes.py:288-345` | Returns HTTP 200 on `missing_phone` or `IntegrityError` for a new phone, so Razorpay never retries and money is captured but not credited | 5xx on retryable errors, `ON CONFLICT DO NOTHING` for the customer, a `pending_payments` table |
| F8 | `payment_routes.py:253`, `config.py:29` | If the webhook secret is unset and `DEBUG` is on, anyone can forge `payment.captured`. `.env` has duplicate `DEBUG` keys (true, then false) | Refuse to boot in production without secrets, remove duplicate key |
| F9 | `image_generation_manager.py:84-90,342` | Gemini `RESOURCE_EXHAUSTED` (also plain per-minute limits) is treated as billing exhaustion, so no fallback and no retry. Retries set to 0 | Classify on status codes, backoff plus retry on rate limits, shared token bucket and circuit breaker |
| F10 | `.env:99` | `MAX_GENERATIONS_PER_DAY=1`. Logs show real orders blocked on 28 Sept. Counter is per process | Set a real cap, warn at startup if below 6, move counter to Redis or DB |
| F11 | `analysis_service.py:99`, `.env` | Eager Celery runs a dashboard analysis on the event loop thread, freezing all webhooks. In real mode `.delay()` fires before the commit | Real broker in production, commit before dispatch |
| F12 | `image_generation.py:49`, `batch_upload.py:73` | `/api/generate-image`, `/api/upload*` are unauthenticated, spend credits, read up to 20 files into memory, and share threads and the daily counter with paid orders | Require auth, cap body size at the proxy |

### Medium / Low

- **Pack charged in full when partial** (`meta_whatsapp_service.py:1235-1255`): no refund or retry of failed styles, and style captions are matched by list index.
- **Retry endpoint** (`meta_webhook.py:1107`) re-runs generation with no charge after a refund, so it gives free packs.
- **Pack claim is read-then-write** (`meta_whatsapp_service.py:1113`), so two triggers can double-deliver.
- **Trial credits** (`entitlement_service.py:111`) are racy, so two taps can take the last credit.
- **Recovery and reconciliation** run only at startup. `reconcile_pending_orders` is never scheduled, so a missed WhatsApp Pay webhook is never credited.
- **Memory:** each image is held as raw bytes plus base64 plus a data-URL (~3.7× size). ~60–80 MB per active pack.
- **Meta Send** has no 429 retry, so an image is dropped.
- **Missing indexes:** `whatsapp_ingestions.status`, `created_at`, `(external_user_id, status)`, `audit_logs (action, resource_id)`.
- **Startup sweep** deletes upload dirs with no `Image` row, which can race another worker mid-upload.
- **Logs:** `LOG_LEVEL=DEBUG`, `diagnose=True`, full phone numbers logged, no `enqueue=True`.
- **Disk:** no retention for uploads and outputs, ~1–3 GB per 1,000 orders.
- **Ops prefix routing** (`ops_forward.py`, off by default): words like `start`, `fix`, `help` from team numbers are diverted, with no retry if Next.js is down. Use an explicit `ops ` prefix.
- **Stale models:** `GEMINI_MODEL=gemini-2.5-flash` returns 404, `ONBOARDING_PARSER_MODEL=gemini-1.5-flash` is retired, and the `GEMINI_IMAGE_MODEL` code default differs from `.env`.
- **Hosting:** the host guard treats any `X-Forwarded-For` as public, so a normal nginx proxy would 404 the dashboard API unless `LOCAL_API_HOSTS` is set. No Dockerfile exists yet.
- **Secrets:** `backend/.env` and `.env.bak-before-ops` hold live-looking credentials. They are untracked, but rotate them if either file was ever shared.

### Strengths (keep)

- Wallet debit is a guarded atomic `UPDATE ... WHERE balance >= price`, so no overdraw or double debit.
- Product choice claim (`awaiting_choice → choice_claimed`) makes double taps safe.
- Image ingestion has a UNIQUE `external_message_id` claim, so Meta retries do not double-process.
- Razorpay credit is idempotent through an audit claim with a partial unique index, in the same transaction as the credit.
- Pricing is per photo, so partial wallet balance is handled correctly.
- Signature verification fails closed when `DEBUG` is false.
- ERPNext and GST cannot block money: they run after commit with timeouts and a local PDF fallback.
- Honest failure: no fake outputs, and refund on total failure is idempotent.
- API cost is only ~5–8% of revenue, so there is large headroom to add retries and QA.

## 4. Module interference

1. **One process, one loop, one thread pool** is shared by webhooks, dashboard, generation, static `/uploads` and sync DB (F1, F3, F11).
2. **Dashboard vs. paid orders:** unauthenticated `/api/generate-image` and eager Celery analysis compete with customers for threads, loop time and the daily spend counter.
3. **Rate limiter vs. webhooks** (F2): the protection layer throttles the revenue path.
4. **Ingestion vs. generation:** they share the same thread pool and DB pool, so a large batch starves new customers of button prompts.
5. **Retry endpoint vs. startup sweeper vs. failure path:** three writers can refund or re-run the same order without an atomic claim (F6).
6. **Import coupling:** services import route modules lazily (`whatsapp_pay_service` → `payment_routes`, `main` → `STUCK_WHITE_AFTER`). Move shared constants to `services/constants.py`.
7. **Duplicate generation paths** (WhatsApp workers, `/api/generate-image`, `earring_*` routes): only the WhatsApp workers bill.

Solution shape: separate three lanes: (a) a fast webhook receiver that only verifies, records and enqueues, (b) a durable job queue with bounded, fair generation workers, (c) the dashboard API on its own process with its own DB pool.

## 5. Concurrent vs. queued

Full model in `concurrency_vs_queue.md`. Summary (T = 14 s per image, *assumed* Gemini quota of 60 requests/min):

| Case | Sequential | Bounded, 12 in flight | Unbounded (current) |
|---|---|---|---|
| 100 single-image orders | 23 min | 126 s | about 40 hit 429s |
| 100 photos × 6 outputs | ~2.9 h | ~15 min | fails |

Sequential wastes no money on 429 retries but is slow. Unbounded wastes money and fails. **Bounded concurrency with a fair queue is the answer.** Once bounded, the provider quota is the ceiling, not VPS size.

Recommended: a durable job per output image, two lanes (single-image orders get priority and reserved slots, packs get aging), at most 3 images in flight per customer, a global semaphore sized from the quota, a token bucket with a circuit breaker, send each image as it finishes, tell the customer their queue position and ETA.

## 6. VPS capacity (model, not benchmark)

| VPS | Current code, safe concurrent generating orders | Notes |
|---|---|---|
| 2 vCPU / 4 GB | ~15 (one process, DB-pool bound) | Threads: 6. One pack fills them. 1–2 packs truly parallel |
| 4 vCPU / 8 GB | ~15 in one process. ~60 if four workers were actually launched | Threads 8 per process |
| 8 vCPU / 16 GB | Same shape, ~120 with 8 workers | Memory is not the limit |

Order of bottlenecks: (1) rate limiter, (2) DB pool and sync DB on the loop, (3) thread pool without timeouts, (4) Gemini image quota, (5) memory, (6) disk. After the fixes below, customers "browsing" (photo sent, no job running) are nearly free, and the generation ceiling is the Gemini quota (about 6–10 packs/min at 60 requests/min).

## 7. Fix roadmap

**Phase 1: stop the bleeding (small edits, high value)**
1. Fix `MAX_GENERATIONS_PER_DAY`, remove duplicate `DEBUG`, refuse production boot without webhook secrets (F8, F10).
2. Exempt webhooks from the rate limiter (F2).
3. Add Gemini timeout, dedicated executor and semaphore (F3).
4. Release DB sessions before network awaits, set pool params (F1).
5. Distinguish Gemini 429 rate limits from billing exhaustion, add backoff (F9).

**Phase 2: money safety**
6. Atomic debit plus status, a wallet ledger, refund claim-first (F5, F6).
7. Razorpay webhook: 5xx on retryable errors, `pending_payments` (F7).
8. Block free retries, atomic pack claim, partial-pack refund policy.

**Phase 3: scale**
9. Webhook only enqueues; durable per-image job queue with fair scheduling (Redis or Celery `gen` queue, `acks_late`).
10. Sweepers for stuck `received`, `awaiting_choice` and unreconciled payments, run on a schedule.
11. Debounce multi-photo bursts into one "N photos, ₹X, confirm?" prompt (100 taps become 1).
12. Dockerfile, gunicorn or uvicorn workers, Redis, proxy header config, upload retention, log config.

**Phase 4: quality**
13. Real fidelity QA (`gemini-3.6-flash` compare, about $0.002 per image) with one retry.
14. Update stale models, add indexes, auth on dashboard routes.

## 8. Measured results: mock-provider load simulation

Run: `backend\venv\Scripts\python.exe backend\tests\load_scenarios\run_load_scenarios.py` (about 3 min). Full detail in `load_simulation_results.md`. Safety: fake providers and Meta, dummy keys, temp SQLite DB, outbound network guard (0 blocked attempts), latency scaled 1/100. SQLite has no row locks, so wallet results show the SQL logic only. The 100 × 3 MB memory figure is extrapolated from 4/8/12-pack runs.

| Scenario | Result | Finding |
|---|---|---|
| a. 1 pack | PASS | Delivered, 6 provider calls, wallet exact |
| b. 10 packs | PASS | 10/10 delivered, loop stalls up to 180 ms |
| c. 100 photos, 100 packs | WARN | 800 calls succeed with no cap. `to_thread` Gemini calls are 18× slower than unbounded (20-thread executor). Loop stalls 1.9 s. Spend cap is all-or-nothing |
| d. 5 customers, 150 webhooks from one IP | FAIL | 100 × 200 then 50 × 429 from the rate limiter (F2 confirmed) |
| e. 10–30% provider faults | FAIL | Real Gemini `429 RESOURCE_EXHAUSTED` text disables fallback (usable 82% vs 94% for plain 429 at 20%). Partial packs charged in full: ~30% of packs at 10% faults, ~80% at 30%. A hung provider leaves the order stuck in `processing`, already debited (F3, F9 confirmed) |
| f. Duplicate webhooks | FAIL | Image, button and same-loop dedup hold. Duplicate texts produce 3 welcomes and 3 payment links. Duplicate worker start across workers made 18 provider calls instead of 6 (check-then-set at `meta_whatsapp_service.py:1113`) |
| g. Wallet under concurrency | FAIL | Guarded debit is correct: 37 charged of 37, never negative. **Refund over-credits:** 20 concurrent refunds of ₹500 credited ₹8,500–10,000 (F6 confirmed, `meta_whatsapp_service.py:798-855`) |
| h. Event-loop blocking | FAIL | `check_quality_floor` 3.8 s (60 ms via `to_thread`), `PreprocessingService.preprocess` 3.5 s (no callers in `app/`), `process_upload` 1 s, base64 139 ms per call |
| i. Memory | FAIL | ~5.1 MB per live output. 100 × 8 extrapolates to ~4.1 GB, 100 × 6 to ~3.1 GB |
| k. DB pool exhaustion | FAIL | Most severe. The 16th concurrent order blocks the loop: 3 s at 16 orders and 39 s at 30 (with a 3 s test timeout; production's 30 s would be ~10× longer). Stranded paid orders never refunded: 1 at 16, 5 at 20, 15 at 30 (F1 confirmed) |

### New findings from the simulation

- **F13 (High): `POST /api/upload/batch` always fails with a foreign-key error.** `batch_upload.py:64` logs a processing step against a freshly generated UUID that has no `Image` row. Observed on SQLite. Postgres enforces foreign keys too, so it should fail there as well. This is not on the WhatsApp path, but the dashboard batch upload appears broken.
- **F14 (Medium): the refund over-credit is worse than the code comment suggests.** The unique index only blocks the second audit row after the money has already moved. A failure storm (for example a provider outage) can pay out several times the order value.
- **F15 (Medium): a DB pool timeout strands paid orders.** Nothing refunds an order whose worker crashed at checkout time until the next restart, and then only if `amount_charged > 0`.

### Priority after measurement

The three findings that can lose money or freeze everything are F1 (pool held across provider waits), F6 (refund over-credit) and F2 (webhook rate limiter). Do these first. The rest of Phase 1 follows in the roadmap above.
