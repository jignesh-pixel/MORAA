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

## Scale architecture - IN PROGRESS (branch `phase-5-scale`) (some parts need owner decisions: hosting, Redis, storage)
## Compliance and cleanup - NOT STARTED
