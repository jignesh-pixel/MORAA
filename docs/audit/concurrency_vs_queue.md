# Concurrency vs Queue Audit: WhatsApp image -> 8-output pack -> delivery -> wallet

Scope: read-only analysis, no app code changed. Numbers are re-derivable from the formulas in section 0. Items marked (ASSUMED) have no measurement in the repo and must be verified against provider dashboards.

## 0. Parameters and formulas

| Symbol | Value | Source |
|---|---|---|
| T | 14 s per Gemini image call | `backend/logs/app_*.log`, 223 `GeminiImageProvider completed` lines: p50 13.8 s, p90 24.2 s, min 10.6, max 142 |
| T_o | ~5.5 s p50 OpenAI (n=299 log lines) | Looks too low for gpt-image-1; treat as unreliable and use T for both |
| N | 8 outputs per input (pack) | Code has 6 styles (`CATALOG_PACK_STYLES`, `MAX_STYLES_PER_PACK=None` = all 6). N=8 is the planned pack size, so N is a variable |
| K | number of inputs (customers or photos) | scenario |
| M | K x N total image calls | hero N=1, pack N=8 |
| Q | provider quota in RPM (IPM) | (ASSUMED) 60 RPM Gemini image. Benchmark report only covers the vision model (60 RPM free, 1,500 RPD), not image generation. Verify in AI Studio. |
| G, O | $0.039 / $0.05 per image (Gemini 2.5 Flash Image, gpt-image-1 medium) | (ASSUMED) list prices |
| D | ~20 s per pack delivery tail | N x (0.8 s `CATALOG_PACK_SEND_THROTTLE_SECONDS` + ~0.7 s send) plus ~1 s Meta upload per image, sequential |
| m | 5.6 MB per in-flight image | ~1.5 MB PNG + 2.0 MB base64 data URL + 1.5 MB `image_data` + decoded copy; 45 MB per 8-image pack |
| base | 0.4 GB per API process | (ASSUMED) FastAPI + SQLAlchemy + genai SDKs |
| c | provider in-flight concurrency | |

- Wall-clock = max( ceil(M/c) x T , M/Q x 60 ) + D. It is the same formula for every model; only c differs.
- Little's law sizing: c_needed = lambda x T. To spend exactly Q per minute: c = ceil(Q x T / 60). Q=60, T=14 gives c=14. With p90 latency, T=24 gives c=24.
- Peak RAM = base + (images held) x m. Current code holds all N results in `asyncio.gather` until the pack finishes.
- CPU% = M x 0.15 vCPU-s / (vCPU x wall). The 0.15 is (ASSUMED): base64 + PNG copies + TLS. Workload is I/O bound.
- Cost = (Gemini successes) x G + (OpenAI fallbacks) x O.

## 1. What the CURRENT model really is (from code)

| Stage | Mechanism | Evidence |
|---|---|---|
| Webhook receive, download, pre-validate | Inline `await` inside the request. `download_media` + Gemini flash pre-validation (`IMAGE_PREVALIDATION_ENABLED`) run before the 200 is returned | `meta_webhook.py` `_ingest_image_for_choice` (l.562-640) |
| Product choice tap -> wallet debit | Inline in the webhook. Atomic guarded `UPDATE ... WHERE wallet_balance >= price` (`wallet_service.charge_customer_balance`). Debit happens BEFORE generation, with refund-on-failure (`_refund_failed_ingestion`) | `meta_webhook.py` l.765, `wallet_service.py` l.177 |
| Job dispatch | FastAPI `BackgroundTasks.add_task(...)` after the response. Runs on the uvicorn event loop, in-process. No queue, no persistence, no ordering, no cap. The docstring says "no Celery/Redis" | `meta_webhook.py` l.11, l.1007, l.1115 |
| Pack fan-out | `asyncio.gather(*[_generate_single_pack_style ...])`. All N styles at once, per customer, unbounded across customers | `meta_whatsapp_service.py` l.1176 |
| Provider call | Gemini: sync SDK via `asyncio.to_thread` into the default executor, capped at min(32, vCPU+4) threads per process (6/8/12 on 2/4/8 vCPU). OpenAI fallback: `AsyncOpenAI`, `max_retries=0`, so truly unbounded. No timeout is set on the Gemini call (max seen 142 s) | `gemini_image_provider.py` l.140, `openai_image_provider.py` l.135 |
| Retry / fallback | `MAX_RETRIES_PER_PROVIDER=0`. One attempt per provider. Chain gemini -> openai. Errors matching "resource exhausted"/quota/billing halt with no fallback. A bare "429"/timeout/5xx falls back to OpenAI (spend on a second vendor) | `image_generation_manager.py` l.32-135, l.140 |
| Fidelity check | NOT in the customer path. `evaluate_fidelity` is a hook that returns "manual QA required" unless `context["product_fidelity"]` is supplied, and nothing supplies it. `processing_service.py` is logging/audit only | `image_generation_manager.py` l.214-236 |
| Delivery | Per-image Meta media upload, sequential (`for data_url ...: await upload_media_to_meta`), then sequential send with 0.8 s sleep | `meta_whatsapp_service.py` l.1201-1226, l.741 |
| DB | `SessionLocal()` opened per pack and held for the whole job. After the first `commit` the following `query` calls autobegin a transaction, so one pooled connection stays checked out during the whole ~15-150 s generate. Engine has no explicit pool args, so SQLAlchemy defaults apply: pool_size 5 + max_overflow 10 = 15 (Postgres), 30 s checkout timeout. `pool_pre_ping=True` | `database.py`, `meta_whatsapp_service.py` l.1091-1132 |
| Spend guard | In-process counter `_spend_count` vs `MAX_GENERATIONS_PER_DAY=100000`. Per process, not shared. Pack reserves N slots up front | `image_generation_manager.py` l.150-190 |
| Celery | Exists (`celery_app.py`, `worker_concurrency=2`, `prefetch_multiplier=1`, eager mode default `CELERY_TASK_ALWAYS_EAGER=True`) but only routes `run_analysis_task`. The WhatsApp generate/deliver path does NOT use it | `celery_app.py`, `tasks/*.py`, `meta_webhook.py` |
| Server | `main.py` runs a single uvicorn process (`settings.WORKERS=4` is not passed to `uvicorn.run`). If launched with `--workers 4`, the pool (15), the executor and the spend counter are all multiplied per process | `main.py` l.194 |
| API rate limit | In-memory, per client IP, 100 req / 60 s, only `/health` exempt. Meta webhook traffic arrives from few IPs, so a burst above 100/min (image + status + button events) returns 429 to Meta and forces its retry cycle. Per-process dict | `middleware/rate_limit.py` |

Summary: **unbounded task-level concurrency (BackgroundTasks + gather), implicitly bounded at Gemini by the thread pool, unbounded at the OpenAI fallback, bounded by accident at 15 packs by the DB pool.** Restart loses all in-flight jobs (charged orders sit in `processing`; only `/webhook/retry` or the 10-minute `STUCK_WHITE_AFTER` white-bg path recovers them).

Concurrency knobs that exist today: `MAX_STYLES_PER_PACK`, `MAX_GENERATIONS_PER_DAY`, `GENERATION_ENABLED`, `DRY_RUN_IMAGE_MODE`, `CELERY_WORKER_CONCURRENCY` (unused for generation), `RATE_LIMIT_*` (wrong layer), `WORKERS` (unused). There is no provider semaphore, no per-customer cap, no queue depth limit.

Side effects of the current model at load:
1. Head-of-line: the `to_thread` executor queue is FIFO across all packs. A hero order that arrives after a 100-photo batch waits behind all of it.
2. The ack text promises "20-30 seconds" (`CATALOG_PACK_ACK_TEMPLATE`), which becomes false under load. There is no ETA and no queue-position message.
3. Paid-then-wait: the customer is debited at tap, so queue delay is billed time. The refund only fires on failure.

## 2. Comparison (T=14 s, Q=60 RPM assumed, D=20 s tail excluded from the wall-clock rows, N=8 pack, N=1 hero)

Models: **Seq** c=1. **Bounded** c=12 (semaphore, below the 14 that saturates Q). **Unbounded** = today's code (all tasks at once; Gemini effectively capped by threads 6/8/12, fallback to OpenAI uncapped). **Queue** = Celery/Redis workers, c=12, plus fair-share and durability.

### 2a. Wall-clock (first input submitted to last image generated), formula max(ceil(M/c)T, M/Q x 60)

| Inputs K | Seq x1 | Seq x8 | Bounded/Queue x1 | Bounded/Queue x8 | Unbounded x1 | Unbounded x8 |
|---|---|---|---|---|---|---|
| 1 | 14 s | 112 s | 14 s | 14 s (8 in 1 wave) | 14 s | ~14-25 s |
| 10 | 140 s | 1,120 s (18.7 min) | 14 s | 98 s (7 waves; RPM floor 80 s) | ~14-30 s, few 429s | ~60-90 s; M=80 exceeds Q=60 in the first minute, so ~20+ images 429 and fall back |
| 100 | 1,400 s (23 min) | 11,200 s (3.1 h) | 126 s (9 waves; RPM floor 100 s) | 938 s (15.6 min; RPM floor 800 s) | ~30-60 s nominal, but ~40 of 100 hit 429 | Fails: M=800. About Q=60 succeed on Gemini in the first minute, ~740 go to 429 then OpenAI (whose limit is much lower, (ASSUMED) ~5 IPM tier-1, verify). Most packs come back partial or failed. DB pool (15) rejects packs 16+ after 30 s and refunds them. RAM about 4.9 GB. |

Add D (~20 s) per pack for delivery (sequential media upload plus throttled sends). Delivery overlaps generation of other customers, so it does not add to the batch total.

### 2b. Other dimensions

| Dimension | Sequential | Bounded concurrent (c=12) | Unbounded (current) | Queue + workers (Celery/Redis) |
|---|---|---|---|---|
| Cost, 800 img (pack x100) | 800G = $31.2. Zero 429 waste | $31.2 (limiter keeps under Q) | 60G + 740O = $39.3 (+26%) if OpenAI accepts; otherwise images are lost, each costs a refund and support. A timed-out Gemini call may still bill after the client gave up (`to_thread` cannot cancel) | $31.2, plus explicit retry budget with jittered backoff (each retry counted). Cost is deterministic |
| Cost as share of revenue | 8 x $0.039 = $0.31 = ~Rs 27 per Rs 500 pack, so provider cost is ~5% of price. The real loss is failed or refunded orders and churn, not API spend | same | same, plus refund and support cost | same |
| Peak RAM = base + held x m | 0.4 + 0.045 (pack held) = 0.45 GB | 0.4 + ~3 packs x 0.045 = 0.54 GB (0.47 GB if results are spilled to disk) | 0.4 + M x 5.6 MB: K=10 x8 -> 0.85 GB; K=100 x1 -> 0.96 GB; K=100 x8 -> 4.9 GB (OOM on 4 GB, 8 GB near limit) | 0.4 (API) + 0.35 per worker process + c x 5.6 MB = ~0.85 GB total; independent of K |
| DB connections | 1 | ceil(c/N)+1 sessions (~3) | 1 per active pack (held through generate) + 1 per webhook. Hard cap 15 (5+10), then QueuePool timeout at 30 s -> `_fail` -> refund. This is an accidental admission control | Workers hold a session only around writes (short transactions): ~workers + API. Fits pool 15, or PgBouncer |
| CPU | negligible: M x 0.15 / wall = 1.2 vCPU-s over 112 s = ~1% of 1 vCPU | 800 x 0.15 = 120 vCPU-s over 938 s = 13% of 1 vCPU, ~6% of 2 | Burst: 120 vCPU-s within ~15-30 s = saturates 2 vCPU. base64 and PNG copies run on the event loop, so webhook p95 goes past Meta's timeout and Meta redelivers (deduped by `external_message_id`, but adds load) | Workers absorb CPU; the API loop stays free; smooth ~13% |
| Provider rate-limit risk | None | Low (in-flight capped, plus RPM limiter) | High. Any K x N > Q per minute sends 429s. Gemini "resource exhausted" -> halt (fine). Plain 429 -> OpenAI fallback (cost and quality jump) | Low. Global semaphore + token bucket + circuit breaker per provider |
| Customer experience | Great for 1 customer, unusable at 10+ (18 min for one x8 batch) | Good: 14 s hero, 98 s for 10 packs. Needs a status message | Fast at K=1, cliff at K>~6 packs/min. Partial packs, refunds, "20-30 s" promise broken | Best if ETA/position messaging and progressive delivery are added. Wait is honest and bounded |
| Failure blast radius | Process crash loses only the current job; head-of-line blocks everyone | Process crash loses all in-flight (BackgroundTasks non-durable), the rest is bounded | One poison burst (a 100-photo customer) starves everyone, can OOM the API (webhooks and wallet down, not just generation), and every in-flight job dies on restart | Worker crash re-delivers (`acks_late`); API stays up; a poison job goes to a dead-letter queue; the queue survives restarts (Redis AOF) |
| Ordering / fairness | Strict FIFO -> a hero order waits behind a 100x8 batch (up to 3.1 h) | FIFO at the semaphore unless prioritised | FIFO in the executor queue: a 100-photo customer monopolises the threads. No fairness | Weighted deficit-round-robin across customers, hero lane priority, aging |

## 3. Recommended architecture

Goal: provider quota is the only global bottleneck, and nobody can monopolise it.

1. **Persist intent before work.** One `generation_job` row per output image (`ingestion_id, style, status queued|running|done|failed, attempts, customer_id, lane, enqueued_at`). It is idempotent per (ingestion, style). On startup, re-drive `queued/running` rows older than the visibility timeout. This also fixes the lost-on-restart problem.
2. **Two lanes, weighted fair share.**
   - Lane H (hero, N=1, Rs 50): weight 4, reserved 25% of slots (3 of 12), can borrow all idle slots.
   - Lane P (pack): weight 1.
   - Within a lane, Deficit-Round-Robin across customers, so a 100-photo customer gets one turn per cycle like everyone else.
   - Aging: any job waiting >60 s gets +1 weight per 30 s, so packs are never starved by a hero flood.
3. **Per-customer cap.** At most 3 images in flight and 2 active packs per customer (3 in flight lets a pack progress while others wait); admission cap of 20 queued packs per customer. Formula: cap_c = max(1, floor(c_global / expected_active_customers)), starting at 3.
4. **Global per-provider semaphore + token bucket.** Semaphore = ceil(0.8 x Q x T/60). With Q=60, T=14 gives 12 (24 if sized to p90). Token bucket = 0.8 x Q per minute (48 RPM). Enforced in Redis (Lua counter with lease TTL 180 s) so it holds across workers. Separate limiters for Gemini image, OpenAI image, and the vision/pre-validate model (a different quota). Circuit breaker: 3 x 429 in 30 s -> pause that provider for `Retry-After`, do not fall back per image. Fallback to OpenAI only through its own limiter and only for pack lane if the customer has not exceeded a fallback-spend cap.
5. **Workers.** Celery on Redis is already in the repo. Add task `generate_style_image(job_id)`, queue `gen`, `acks_late=True`, `prefetch_multiplier=1`, `soft_time_limit=120`, `time_limit=150`, `visibility_timeout=300`. Use `-P gevent -c 12` or threads (I/O bound; prefork at 0.35 GB per process wastes RAM). A dispatcher (single process, DRR) releases jobs to `gen` only when a semaphore token is free, so Celery itself never holds fairness state. Pack completion is a DB counter, not a chord: when done+failed == N, one `deliver_pack` task runs. Set `CELERY_TASK_ALWAYS_EAGER=false`.
6. **Progressive delivery.** Send each image as it completes (spill bytes to disk immediately, drop from RAM) instead of gather-all then upload-all sequentially. It removes the 45 MB hold and cuts perceived latency from ~T+D to ~T for the first image. If the fidelity check is added (planned, one extra vision call of ~3-5 s, T_eff = 14 + 4 = 18 s), give it its own limiter and let it run in the same job so a failed check costs one regeneration, capped at 1 retry per style (worst-case cost x2 on failing styles; budget it).
7. **Backpressure to the customer, and money.** At tap, before debit: ETA = (weighted jobs ahead) / (0.8 x Q / 60 per s). If ETA <= 30 s, proceed silently. If 30 s to 10 min, reply "You're #k in line, about X min. We'll send each image as it's ready." If ETA > 15 min or queue depth > cap, do not debit; reply "We're at capacity. Try again in ~X min" (or offer an opt-in "notify me"). Move the debit to after admission, or auto-refund if a job waits > 20 min. A stale-queue sweeper that refunds is needed anyway because the wallet is debited before generation today.
8. **Fix the accidental limits.** Exempt `/api/meta/webhook` from the IP limiter (or key it on Meta signature, not IP). Release the DB connection before the provider awaits (`db.close()` or `db.commit()` immediately after reads). Set explicit `pool_size/max_overflow`. Add a request timeout to the Gemini call (e.g. 60-90 s, since p90 is 24 s). Share the daily-spend counter through Redis.
9. **Rollout order** (cheapest first): (a) in-process `asyncio.Semaphore(12)` around the provider call plus a per-customer semaphore and hero-first `asyncio.PriorityQueue` in place of `gather`, with the DB re-drive; (b) move to Celery `gen` queue when a second box or restarts-without-loss matters; (c) add DRR dispatcher and ETA messaging.

## 4. Concurrent customers at one moment: 2 vCPU/4 GB vs 4 vCPU/8 GB vs 8 vCPU/16 GB

Formulas:
- RAM-bound packs (current code) = floor((0.75 x RAM - 0.4) / 0.045).
- DB-bound packs (current code) = 15 (pool 5+10); x processes if `--workers`.
- Gemini in-flight (current code) = min(32, vCPU+4); throughput = in-flight / T; packs/min = images/min / N.
- Recommended: c = ceil(0.8 x Q x T / 60), independent of VPS size while Q is the limit; RAM = base + c x 5.6 MB; CPU% = imgs/min x 0.15 / 60 / vCPU (keep <=60%).
- "Served customers" = customers whose pack completes within SLA = 180 s: served = packs/min x 3 (+ those in service). Heroes (N=1) = images/min x 3.

### 4a. Current code (BackgroundTasks + gather, 1 uvicorn process)

| VPS | RAM-bound packs | DB-bound packs (binding) | Gemini threads | Gemini img/min | Pack throughput | Safe concurrent packs before failures | Time for 15 concurrent x8 | Concurrent heroes OK (Q=60) |
|---|---|---|---|---|---|---|---|---|
| 2 vCPU / 4 GB | 57 | 15 | 6 | 26 | 3.2 packs/min | 15 (packs 16+ time out at 30 s and are refunded) | 15 x 8 / 26 per min = 4.6 min | ~15 (DB pool), about 26/min |
| 4 vCPU / 8 GB | 124 | 15 | 8 | 34 | 4.3 packs/min | 15 | 3.5 min | ~15, about 34/min |
| 8 vCPU / 16 GB | 257 | 15 | 12 | 51 | 6.4 packs/min (Q=60 nearly hit) | 15 | 2.4 min | ~15, about 51/min |

The bound is the DB pool, not RAM or CPU. A bigger VPS buys throughput via more threads only until Q; DB pool is 15 regardless. At Q=60, a 100 x8 burst on any size ends in partial packs and refunds.

### 4b. Recommended (semaphore + queue). Two provider scenarios

| VPS | Q | c (in-flight) | Images/min | Packs/min (N=8) | Heroes/min | Served in 180 s: packs / heroes | RAM (API 0.4 + 1 worker 0.35 + c x m) | CPU at full load | Bottleneck |
|---|---|---|---|---|---|---|---|---|---|
| 2 vCPU / 4 GB | 60 | 12 | 48 | 6 | 48 | 18 / 144 | ~0.82 GB | 48 x 0.15 / 60 / 2 = 6% | Provider quota |
| 4 vCPU / 8 GB | 60 | 12 | 48 | 6 | 48 | 18 / 144 | ~0.82 GB | 3% | Provider quota |
| 8 vCPU / 16 GB | 60 | 12 | 48 | 6 | 48 | 18 / 144 | ~0.82 GB | 1.5% | Provider quota |
| 2 vCPU / 4 GB | 300 | 0.8x300x14/60 = 56 | 240 | 30 | 240 | 90 / 720 | 0.75 + 56 x 5.6 MB = ~1.06 GB | 240 x 0.15 / 60 / 2 = 30% | CPU/event loop (keep webhooks on their own process) |
| 4 vCPU / 8 GB | 300 | 56 | 240 | 30 | 240 | 90 / 720 | ~1.06 GB | 15% | Provider quota |
| 8 vCPU / 16 GB | 300 | 56 | 240 | 30 | 240 | 90 / 720 | ~1.06 GB | 8% | Provider quota |
| 8 vCPU / 16 GB | 1000 | 187 | 800 | 100 | 800 | 300 / 2400 | ~1.8 GB | 800 x 0.15 / 60 / 8 = 25% | Meta send limits, DB writes |

Conclusion: at Q=60 the whole product tops out at ~6 packs/min or ~48 heroes/min whatever the VPS is, so the 2 vCPU/4 GB box is enough once concurrency is bounded (RAM ~0.8 GB, CPU <10%). Today's unbounded model needs the 8 vCPU box only to reach the same quota wall, and still breaks at 15 packs because of the DB pool. Buy quota (Q) before buying vCPUs. Also decouple: one API process for webhooks (must answer Meta fast) and separate worker process(es) for generation.

## 5. Verification to do before trusting the numbers

1. Read the real Gemini image RPM/IPM/RPD and OpenAI image IPM in the two consoles and replace Q. Recompute c = ceil(0.8 x Q x T/60).
2. Re-measure T for N=8 concurrent calls (log lines already record `time=`); latency under concurrency can be worse than the solo p50 of 13.8 s. Log p90 = 24 s and max 142 s show a long tail, so add a timeout.
3. Load test with `DRY_RUN_IMAGE_MODE=true` (zero cost) to confirm the DB-pool ceiling (packs 16+ fail at 30 s), the event-loop stall during 800 base64 conversions, and the rate-limit middleware 429s on webhook bursts; then a paid test with `MAX_STYLES_PER_PACK` and a small K.
4. Confirm whether the 0.15 vCPU-s per image and 5.6 MB per image hold (py-spy / tracemalloc on one pack).
