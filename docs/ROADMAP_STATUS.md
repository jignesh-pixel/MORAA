# Production-readiness roadmap: where every finding stands

Source: `GemVision_Production_Readiness_1.html` (audit of 1 Oct 2026). Updated as work proceeds.
"Done" means built, tested on SQLite and PostgreSQL, reviewed by independent reviewers and committed. Nothing is applied to production yet.

Branch order (each branch starts from the previous): `phase-2-financial-core` -> `phase-3-runtime-hardening` -> `phase-4-operations` -> ...
Gate reports: `docs/phase-gates/`. Questions that need the owner: `docs/QUESTIONS_FOR_MORNING.md`.

## Phases 0-1 (emergency fixes, foundation) - DONE earlier (locked, tag `phase-1-verified`)
SEC-1, SEC-2, SEC-3, SEC-5, SEC-6, SEC-10 (code part), PRIV-1, OBS-1, OBS-5, CFG-1, CFG-2, CFG-7, ARC-3, DATA-1, DATA-5 (needs owner check), MON-1, MON-3, DATA-6, TEST-1/2/3 (CI, test env, PostgreSQL tests), SEC-9 (framework upgrade).

## Phase 2 financial core - DONE (branch `phase-2-financial-core`)
MON-2, MON-4, MON-5, MON-7, MON-8, MON-9, MON-10, MON-11, MON-12, Q-3, UX-4, migration 0002 safety. Dropped by owner decision: MON-6 / UX-3 (no refunds for output quality).

## Runtime hardening - DONE (branch `phase-3-runtime-hardening`, gate: every load scenario passes)
PERF-1, PERF-2 (database-only routes now threaded; webhook handler's own database calls remain, see Phase 3 scale), PERF-3, PERF-4, PERF-5, PERF-6,
EXT-1, EXT-2, EXT-3, EXT-4, EXT-5, COST-1, DATA-8, Q-4.
Open inside this area: EXT-7 (OpenAI fallback: fund or remove) - owner decision.

## Operations - DEVELOPED, awaiting verification (branch `phase-4-operations`, commit 37f7db4)
Built: health live/ready, metrics, WhatsApp ops alerts, Sentry (optional, scrubbed), request-id logs, hot-path indexes (0014),
auth hardening (0015: admin role, token revocation, single-use refresh), config cleanup, backup/restore-drill/release scripts,
`docs/OPERATIONS_RUNBOOK.md`. Gate report: `docs/phase-gates/phase-4-operations-2026-10-02.md` - every step passes except the
live database check, which fails until production is upgraded past revision 0008 (by design).
Owner questions: see `docs/QUESTIONS_FOR_MORNING.md` (Operations section).

## Scale architecture - DEVELOPED in part (branch `phase-5-scale`); awaiting verification
Built (all behind settings, nothing needs Redis): pure-ASGI middleware (PERF-7; the rate-limit table was already pruned, PERF-8);
streamed upload size checks and length caps on base64 fields (SEC-8); API docs off in production and an admin-only ingestion
status endpoint (SEC-7, part); per-provider circuit breaker and token bucket (EXT-6); a durable outbox in the database (Q-5)
that now carries ops-team forwards, invoice sends and the start of every paid order (Q-1, part: a crash or deploy before an order
starts no longer strands it); scheduler leases so periodic jobs run in one process only (ARC-2); a priority gate for provider
calls and a per-customer limit on orders in progress (Q-6); interrupted-photo recovery (Q-2, part); honest delivery estimates
(UX-2); Dockerfile, compose and HTTPS proxy files plus graceful shutdown (DEP-1, DEP-2, not yet run on a server).
New migrations: 0016 (outbox_jobs), 0017 (scheduler_leases) - applied by `scripts/release.py`.
NOT built, and why:
- UX-1 photo-burst grouping: BUILT on 2026-10-03 after the owner's answer (see OPERATIONS_RUNBOOK section 14).
- DEP-3 object storage and signed image links (SEC-7 rest): needs the owner's choice of storage provider.
- Q-1 full Celery/Redis generation queue and Q-2 "webhook only records and answers": larger changes whose benefit depends on real load; the
  database outbox covers the lost-order risk for now.
- ARC-4 / ARC-5 structural refactors (move shared constants out of route files, one generation job for all products): they touch every
  test patch point and the money paths; scheduled last, together with MAINT-1, in the cleanup phase.

## Compliance and cleanup - DEVELOPED in part (branch `phase-6-compliance`, includes the Scale review fixes); awaiting verification
Built: consent before any personal data is collected (PRIV-2, OFF until the owner supplies the wording; migration 0018); data retention
(DATA-7, OFF until approved) and customer erasure by "DELETE MY DATA" or `scripts/erase_customer.py` (PRIV-3); payment-receipt wording and
IGST template/place of supply for inter-state buyers (PRIV-4, inactive until two settings are filled); Meta media id/host validation
(SEC-11); optional explicit "ops " prefix (EXT-8); separate daily limit for team orders (COST-2); per-call cost log with daily spend alert
and `scripts/cost_report.py` (COST-4, migration 0019); empty placeholder folders removed and the generated results file untracked (MAINT-3/4).
Already satisfied when checked: DEP-6 (the Celery command in HOW_TO_RUN is valid), DEP-7 (every frontend service reads NEXT_PUBLIC_API_URL),
MAINT-3 (the experiment scripts and outputs were already untracked and ignored; they stay on the owner's disk, and the Docker build ignores them).
NOT done: MAINT-1/ARC-4/ARC-5 (large refactors of money paths: recommended after launch), MAINT-2 (frozen prompt route files), a real GST
verification provider (EXT-8, owner decision), "only verified GSTINs on invoices" (needs the GST provider).


## Owner decisions received 2026-10-03
Approved: production upgrade in principle (timing still to be agreed, after verification), push to GitHub (blocked by the tool permission, see below),
privacy notice (without the AI-abroad and how-to-delete lines), 90-day retention, 3 orders per customer, bulk photo orders, Sentry, alert numbers
919699899825 and 919820666332, paid GST verification (vendor still to be chosen). Built since: bulk orders (migration 0020), configurable GST vendor,
verified-only GSTIN on invoices, `docs/ENV_TO_ADD.md`. Still open: items 3, 4, 7, 10, 13 and parts of 11/12 (see the owner message in the chat).

## Phase 7 - WhatsApp chat dashboard (branch `phase-7-dashboard`, tag `phase-7-gate-passed-except-timing`)
Built 2026-10-03: record of every message, image we produced and invoice (migration 0021), read-only API with signed image links, Google sign-in, Drive archive
(off until configured), Next.js "WhatsApp Chats" page (chats, profile, weekly audit, zoom viewer), retention and erasure of the chat record. Independent review done and fixed.
Tests: SQLite 1176, PostgreSQL 1197 pass; prompts unchanged. Load checks h (loop stalls) and i (memory per image) pass when the laptop runs at full speed (13 s / 1.1 MB) and fail
when its CPU is throttled (about 62% performance: 27-34 s / 2.6-3.1 MB); the older phase 6 code behaves identically in that state, so it is the machine. Re-run on a cool machine or in CI.
New load scenario l measures the dashboard record's own cost (3 MB write = 7.5 ms, worst loop stall 22 ms, nothing left in memory).
Verification gate reports: docs/phase-gates/verification-2026-10-03/. Still needed before any phase counts as locked: production upgrade (migrations 0009-0021), CI green, h/i re-run.
