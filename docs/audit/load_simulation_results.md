# Load and failure simulation results (mock providers, zero cost)

Harness: `backend/tests/load_scenarios/` (`run_load_scenarios.py`, `fakes.py`, `harness_env.py`). Raw numbers: `backend/tests/load_scenarios/last_results.json`.
Run: `backend\venv\Scripts\python.exe backend\tests\load_scenarios\run_load_scenarios.py [--only a,b,c,d,e,f,g,h,i,k]` (about 3 min total). Application code under `backend/app` is imported unmodified.

## 0. Safety and method

- **No paid calls, no secrets.** `harness_env.py` runs before any `app` import: it `chdir`s to a fresh temp dir (so `backend/.env` is never found by pydantic's relative `env_file`), sets every key/token/secret to a dummy string, points `DATABASE_URL` and `UPLOAD_DIR` at a throw-away SQLite file, and asserts none of the real keys leaked into `settings`. Outbound network is blocked (`socket.getaddrinfo`, `socket.connect`, `httpx` send); blocked attempts are counted: **0** in the final run. Gemini/OpenAI/Meta/Razorpay/ERPNext are replaced by in-process fakes; image pre-validation (a Gemini call) is off.
- **What is real.** `ImageGenerationManager` (chain, fallback, quota halt, spend guard), `_handle_product_choice` (claim + wallet debit), `process_whatsapp_catalog_pack` (fan-out, delivery, refund), `wallet_service`, `receive_webhook`, `UploadService`, `RateLimitMiddleware`, `batch_upload` router, PIL code paths, SQLAlchemy sessions on a real (SQLite) DB. Only the network edges are fake.
- **Latency scale 1/100.** Real 15-40 s per image call becomes 0.15-0.40 s. Meta delivery throttle (0.8 s/image) is scaled the same way. `wall_s_x100_naive` multiplies wall time back up, which also inflates CPU/DB overhead that does not scale, so treat it as an upper bound.
- **Provider order** (from settings): primary `gemini`, fallback `openai`. Pack = 6 styles (`CATALOG_PACK_STYLES`); the code cannot build 8 outputs per photo, so "x8" is run at manager level (c1/c2) and the real pack path runs x6 (c3).
- **SQLite limitations.** SQLite has no row locks and serialises writers at DB level, so results for concurrency-sensitive SQL (g1-g3) prove the SQL is correct on SQLite, not that Postgres row-lock semantics behave the same. By reasoning, `UPDATE ... WHERE wallet_balance >= price` is also atomic on Postgres READ COMMITTED (the WHERE is re-checked after the row lock), but that was not run. Check-then-act races (g4, f6) are DB-independent and reproduce on any engine.
- **DB pool.** SQLAlchemy defaults (5+10 connections) deadlock the loop at 16 concurrent orders (scenario k). To avoid confounding every other scenario, the harness lifts the pool to 400+400 for a-h and i; only k uses the app defaults (with `pool_timeout` cut from 30 s to 3 s so it finishes; production stalls are 10x longer).

## 1. Scoreboard

| # | Scenario | Result | Headline |
|---|---|---|---|
| a | 1 image / 1 pack | PASS | delivered, 6 provider calls, wallet exact, 19 sync SQL statements per order |
| b | 10 orders at once | PASS | 10/10 delivered, wallet exact; loop stalls up to ~180 ms |
| c | 100 x 8 / 100 packs | WARN | correct, but Gemini-style threaded calls run 18x slower than unbounded; single stall of 1.9 s on 100 packs |
| d | 5 customers concurrent | FAIL | funding and isolation correct; shared-IP rate limiter returns 429 after 100 webhooks/min |
| e | 10-30% provider faults | FAIL | wallet exact, but real Gemini 429 text disables fallback; partial packs are fully charged; hung provider hangs a paid order |
| f | duplicate webhook | FAIL | image and button dedup hold; text not deduped; duplicate worker start triples provider spend across workers |
| g | concurrent wallet debits | FAIL | debit correct (37 of 37); refund race over-credits the wallet up to 20x |
| h | event-loop blocking | FAIL | stalls of 1-3.8 s; `/api/upload/batch` fails outright (FK error) |
| i | peak memory 100 x 3 MB | FAIL | about 5.1 MB per live output; 100x8 extrapolates to about 4.1 GB |
| k | (extra) DB pool exhaustion | FAIL | 16th concurrent order freezes the event loop; paid orders stranded |

Fail thresholds: h 100 ms single loop stall; i 2 GB above baseline (small container); others are correctness.

## 2. Scenario detail

### a. One image (PASS)
One `generate_image` call: 0.23 s (0.15-0.40 s injected). One paid pack via the real tap handler and worker: 0.56 s wall, 6 provider calls, 1 delivery of 6 images, status `delivered`, wallet 500 -> 0 (charged 500, reconciled). Loop lag max 21 ms. Sync SQL: 19 statements per order.

### b. 10 images at once (PASS with caveats)
10 orders, 60 provider calls all in flight simultaneously (`max_inflight` 60), 0.95 s wall, 10/10 delivered, wallet reconciled. Loop lag max 180 ms (2 stalls over 100 ms) from back-to-back synchronous DB commits inside the tap handlers, which have no `await` between statements.

### c. 100 images x 8 outputs (WARN)
- c1 async provider, 800 concurrent calls via the real manager: 800/800 success, 0.62 s, `max_inflight` 800. There is no semaphore anywhere in the generation path.
- c2 thread-backed provider (mirrors `GeminiImageProvider`, which uses `asyncio.to_thread`): 11.2 s vs 0.62 s unbounded (**18x**). The default executor has 20 threads on this 16-CPU box (`min(32, cpu+4)`), so 800 calls queue behind 20. At 1/100 scale this is about 18.6 minutes for the last customer at real latency (15-40 s x 800 / 20), and the same executor also serves Razorpay link creation and image pre-validation (`asyncio.to_thread`), which starve behind generation.
- c3 real pack path, 100 orders x 6 styles = 600 calls: 100/100 delivered, wallet reconciled, 4.4 s wall. Loop lag max **1.9 s**, 6 stalls over 100 ms (1900 sync SQL statements plus commits on the loop thread).
- c4 spend cap: `MAX_GENERATIONS_PER_DAY=100`, 30 packs at once: 16 packs ran (96 calls), 14 refused before any provider call and refunded (7000 of 15000). All-or-nothing reservation works.

### d. Five customers concurrently (FAIL on rate limit only)
5 customers x 10 photos, balances funding 10, 8, 6, 4, 2 packs (plus a Rs 137 remainder). Result: 30 delivered, 20 held in `awaiting_choice` with recharge prompt, each customer's balance = remainder exactly, per-recipient deliveries match funding, no cross-customer bleed. Loop lag max 467 ms.
Failure: `RateLimitMiddleware` (`app/middleware/rate_limit.py:34`) keys on `request.client.host`. From one Meta egress IP, 150 webhook POSTs in a minute gave **100 x 200 then 50 x 429**. All customers share Meta's IPs, so real traffic above 100 messages per minute (text, statuses and images all count) is rejected, and Meta retries add to the load. `/api/meta/webhook` is not exempt (only `/health` is).

### e. Provider faults at 10/20/30% (FAIL)
e1, per mode, 600 manager calls, both providers failing independently at the given rate (usable = image returned):

| mode | 10% | 20% | 30% | fallback used |
|---|---|---|---|---|
| 429 plain "rate limit" | 99.0% | 94.3% | 91.7% | yes |
| 500 | 99.3% | 96.3% | 92.0% | yes |
| timeout | 99.0% | 95.8% | 92.7% | yes |
| **429 RESOURCE_EXHAUSTED** (real Gemini wording) | 91.0% | 82.2% | 71.0% | **never (0 fallback calls)** |
| empty ("no image data") | 89.8% | 78.5% | 67.7% | never (by design, non-recoverable) |
| blank success (0-byte image) | 91.0% | 82.3% | 70.3% | success reported to caller |

Root cause of the 429 gap: `_QUOTA_EXHAUSTION_PATTERNS` contains `"resource exhausted"/"resource_exhausted"` (`image_generation_manager.py:84-90`), and Gemini's transient per-minute 429 is worded `RESOURCE_EXHAUSTED ... quota exceeded`. `_is_quota_exhaustion` is checked first (`:342`), so a rate-limit blip is treated as billing exhaustion and halts with no fallback, even though `_RECOVERABLE_PATTERNS` lists the same text (`:54-55`, unreachable for these errors because the quota check wins). There is also no retry (`MAX_RETRIES_PER_PROVIDER = 0`), so the image is simply lost.

e2, end-to-end, 60 paid packs, mixed faults on both providers:

| fault rate | delivered (6/6) | delivered_partial | stuck | wallet |
|---|---|---|---|---|
| 10% | 41-43 | 17-19 | 0 | reconciled |
| 20% | 27 | 33 | 0 | reconciled |
| 30% | 11 | 49 | 0 | reconciled |

The wallet always reconciles and nothing is stranded, but a customer whose pack lost any style is charged the full price: `delivered_partial` is never refunded (`meta_whatsapp_service.py:1236-1256`, deliberate per the code comment). At a 10% fault rate about 30% of packs are partial; at 30% about 80%. Refunds only occur when all 6 fail.

e3, hung provider: the primary never returns. After 1.5 s (150 s at real scale) the order is still `processing`, wallet already debited, fallback never tried (0 calls). There is no `asyncio.wait_for` or SDK timeout on the generation path (`grep wait_for` finds only ERPNext and GST). `asyncio.gather` (`meta_whatsapp_service.py:1176`) waits forever; the order is only recovered by `recover_stuck_paid_orders` at the next process restart, and the customer gets no message.

### f. Duplicate webhook delivery (FAIL, partial)
| test | rows / effects | result |
|---|---|---|
| f1 same image message id x20 in one loop | 1 ingestion, 1 button message, 1 Meta media lookup | PASS |
| f2 same id x20 across 20 threads/sessions (multi-worker) | 1 ingestion, 1 button message, 1 media lookup | PASS (unique `external_message_id` claim) |
| f3 sequential retries x5 | 1 / 1 / 1 | PASS |
| f4 same button tap x20 across workers | 1 job enqueued, 1 debit, 19 "already chosen" notices | PASS (guarded status UPDATE) |
| f5 text `hi` x3, `recharge 500` x3 (same message id) | 3 welcome messages, 3 payment links | **FAIL**: no message-id dedup for text (`meta_webhook.py` text branch, l.902-960) |
| f6 duplicate worker start same loop x2 | 6 calls, 1 delivery | PASS (first `await` comes after the status write) |
| f6 duplicate worker start x4 across workers | **18 provider calls**, 3 deliveries (expected 6 and 1) | **FAIL** |

f6 root cause: `if ingestion.status == "processing": return` then `ingestion.status = "processing"` (`meta_whatsapp_service.py:1113-1119`) is check-then-set, not a guarded UPDATE. It is safe within one event loop, unsafe across uvicorn/gunicorn workers or the `/webhook/retry` endpoint plus a live worker. Cost: N x paid provider calls and N deliveries for one payment.

### g. Concurrent wallet debits (FAIL on refund)
- g1/g1b: 100 simultaneous `charge_customer_balance` calls (100 threads and 16 threads, own sessions) on a balance for 37 packs plus Rs 250: **charged exactly 37, declined 63, final 250, never negative, money conserved**, 0 SQLite lock errors.
- g2/g3: real tap handler on 100 photos of one customer, funded for 37: 37 queued, 63 released to `awaiting_choice`, wallet reconciled (100 threads and single loop).
- **g4, refund race:** `_refund_failed_ingestion` (`meta_whatsapp_service.py:798-855`) does check-audit-row, then `refund_generation_charge` (which commits the credit, `wallet_service.py:246-280`), then inserts the audit row. The unique index `uq_audit_logs_money_once` only stops the second audit row; the credit is already committed. 20 concurrent refunds of an order charged Rs 500 credited the wallet **Rs 8,500-10,000** (17-20x), with 1 audit row. Magnitude equals the number of concurrent callers, so real exposure is 2-3x when two workers (f6) or the startup sweeper and a late worker race, but it is unbounded by design.
- Limitation: SQLite, no row locks; see section 0.

### h. Event-loop blocking (FAIL)
Heartbeat every 10 ms; idle baseline max lag 10 ms. Test image 3.01 MB JPEG.

| path | stall (max loop lag) | same work via `asyncio.to_thread` |
|---|---|---|
| `PreprocessingService.preprocess` x8 (`async def` wrapping sync PIL open/resize/JPEG save, `preprocessing_service.py:99-160`) | **3,462 ms** | not measured |
| `check_quality_floor` x20 (sync, called inside async route `image_generation.py:135`) | **3,839 ms** | 60 ms |
| base64 encode+decode of 3 MB x100 (data URL round trip, `_bytes_to_data_url`/`_data_url_to_bytes`, providers) | 139 ms per call, 3.1 s total stalled | not measured |
| `UploadService.process_upload` x20 (sha256, disk write, PIL dimensions, sync DB commits) | **983 ms** | not measured |
| `validate_image` (webhook path) x50 | 0 ms (JPEG `verify()` does not decode; 0.25 ms each) | n/a |
| 100 concurrent packs (c3, sync DB and file reads in async handlers) | 1,886 ms | n/a |

`PreprocessingService` has no callers in `app/` (dead code) so its stall is latent; the others are live. Offloading `check_quality_floor` to a thread cut its stall from 3.8 s to 60 ms.

**Batch upload is broken outright.** `POST /api/upload/batch` calls `proc.log_processing_step(group_id, ...)` (`batch_upload.py:64`) with a fresh UUID that does not exist in `images.request_id`, but `processing_logs.request_id` is a foreign key to it (`models/processing_log.py:31`). All five concurrent 20 x 3 MB requests raised `IntegrityError: FOREIGN KEY constraint failed` (HTTP 500). Observed on SQLite with `PRAGMA foreign_keys=ON` (set by `database.py`); Postgres enforces FKs by default, so it fails there too (static). The cap is 20 files per request (21 files -> 400), so 100 images need 5 requests.

### i. Peak memory (FAIL against 2 GB)
Real pack path, 3 MB reference and 3.0 MB fake outputs (bytes + base64 data URL, like the real providers), fresh process per point, Windows peak working set minus baseline (about 117 MB):

| concurrent packs | outputs | peak delta |
|---|---|---|
| 4 | 24 | 132 MB |
| 8 | 48 | 254 MB |
| 12 | 72 | 376 MB |

Linear fit: 30.5 MB per concurrent pack, **5.1 MB per live output** (3 MB `image_data` released after the style returns, but the 4 MB base64 URL string is held in `gather_results` until the whole pack is delivered, plus the 3 MB reference per pack). `tracemalloc` at 6 packs: 192 MB peak vs 193 MB working set, so it is Python heap, not native. Extrapolation: **100 packs x 6 = about 3.1 GB, 100 images x 8 outputs = about 4.1 GB** above baseline. Real PNG outputs of about 1.5 MB would scale this down by roughly half; the prior audit's 5.6 MB per image agrees. The box had only 1.9 GB free, so 100 was extrapolated, not run.

### k. (extra) DB connection-pool exhaustion (FAIL)
`database.py:19-25` creates the engine without pool arguments (5+10 = 15 connections, 30 s timeout). Each running pack worker keeps its `Session` in an open transaction across the entire provider wait (Image lookup at `meta_whatsapp_service.py:1123`, no commit until `:1198`), so it pins one connection for the full 15-40 s. The 16th order's checkout blocks inside `pool.checkout` **on the event-loop thread**, freezing everything (webhooks, other workers, health) until the timeout, and each further waiter blocks it again.

| concurrent paid orders | loop frozen (max, pool_timeout=3 s) | delivered | stranded paid (`pack_queued`, no refund) |
|---|---|---|---|
| 10 | 0.1 s | 10 | 0 |
| 15 | 0.3 s | 15 | 0 |
| 16 | 3.0 s | 15 | 1 |
| 20 | 12.3 s | 15 | 5 |
| 30 | 39.4 s | 15 | 15 |

With the real 30 s timeout multiply the freezes by 10 (30 orders would freeze the app for about 6.5 minutes). Stranded orders are charged, never generated, never refunded, and get no message until a restart runs `recover_stuck_paid_orders`. The same exhaustion hits the webhook itself (a tap or upload arriving while 15 workers run blocks the loop and returns 500 to Meta).

## 3. Static analysis (paths not exercised)

- **BackgroundTasks execution:** jobs added at `meta_webhook.py:1007` run in-process on the request loop with no queue, cap, persistence or backpressure; a restart drops them (only stuck-order sweep recovers, and only rows with `amount_charged > 0`).
- **Sync DB in async handlers:** every `db.query/commit` in `_ingest_image_for_choice` (`meta_webhook.py:562`), `_handle_product_choice` (`:680`), `receive_webhook` (`:844`) and the pack worker blocks the loop for the DB round trip. Measured 19 statements per order; on Postgres at 5-20 ms RTT that is 0.1-0.4 s of blocked loop per order, so 100 concurrent orders block the loop for 10-40 s in aggregate. Not measurable on local SQLite (0.2 ms per statement); the commit fsync stalls seen above are the only proxy.
- **Real provider internals:** `GeminiImageProvider` (`gemini_image_provider.py:140`) uses `asyncio.to_thread` with no client timeout; `OpenAIImageProvider` similar. Not exercised (would need the SDK). Behaviour derived from fakes that copy its threading model.
- **Meta delivery:** `send_catalog_pack_images_to_whatsapp` sends sequentially with a 0.8 s throttle (`meta_whatsapp_service.py:687-745`, about 5 s per pack tail); Meta's send-rate and 429 handling on delivery were not simulated.
- **Rate limiter memory:** `RateLimitMiddleware._request_counts` (`rate_limit.py:20`) never deletes IP keys, only prunes lists; unbounded key growth under many source IPs.
- **Default executor sharing:** generation, pre-validation (`image_prevalidation_service.py:309`) and Razorpay (`razorpay_service.py:33`) all use the same 20-thread default pool.

## 4. Fix pointers (no code changed)

1. Commit or close the session before long awaits in workers (or use a short-lived session per DB touch) and set `pool_size`/`max_overflow` explicitly; this is the most severe finding (k).
2. Make the refund a single guarded operation: insert the audit row first inside the same transaction as the credit (rely on `uq_audit_logs_money_once`), or credit only if the audit insert succeeded (g4).
3. Claim the order with `UPDATE ... SET status='processing' WHERE id=? AND status IN ('pack_queued','white_queued','stored')` and check rowcount (f6).
4. Split "quota exhausted" from "RESOURCE_EXHAUSTED rate limit"; e.g. treat quota only when the text says quota/billing, not the bare status word (e).
5. Add `asyncio.wait_for` around each provider call and a semaphore (plus a dedicated `ThreadPoolExecutor`) for Gemini calls (c2, e3).
6. Fix `batch_upload.py:64` (log against a real request id or drop the group-level log), and move `check_quality_floor` and other PIL work to `asyncio.to_thread` (h).
7. Dedup text messages by message id (f5); key the rate limiter per sender or exempt the signed Meta webhook (d).
8. Refund partial packs pro rata or state the policy to customers (e2); stream base64 uploads instead of holding all 6 data URLs (i).
