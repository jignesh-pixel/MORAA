HANDOFF: MORAA GemVision, Phase 2 (financial core). Written 2026-10-02. Plain text for pasting into a new Claude Code session.

== 0. WHO/WHAT ==
Repo: C:\PROJECTS\Moraa Gemvision (Windows, Git Bash/PowerShell). Backend: backend\ (FastAPI, sync SQLAlchemy, Alembic, Supabase Postgres via pooler :6543). Python venv: backend\venv\Scripts\python.exe.
Goal: production-grade backend for 100 req/min. Phases 0-5 are in the separate roadmap report (PRODUCTION_READINESS_ASSESSMENT.md). Phase 0 (emergency hardening) and Phase 1 (foundation) are DONE, VERIFIED, LOCKED: tag phase-1-verified, CI green on f14d0ca, gate report docs/phase-gates/phase-1-2026-10-02.md.
Read first: AGENTS.md (project rules), this file, the roadmap report. Standing user rules:
 - After each unit: run review agents on the diff AND on how it interacts with dependent code. Output quality (image prompts, behaviour) must never go down.
 - Never print/store secrets. Never run tests against production DB. Production checks must be READ ONLY.
 - Never touch ports 8000/3000. Do not kill the pre-existing postgres.exe processes.
 - Commit ONLY by explicit pathspec (git add <files>; git commit -m ... -- <files>). Never sweep in user files: "Claude outputs/*", docs/live_prompts_dump.json, .claude/work-audit-log/, worklog/ (worklog is mine, ok to leave untracked or ask).
 - No pushes/merges to main by the agent. User opens PRs (gh CLI not installed).
 - Commit message trailer: Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com> (use whatever the new session's attribution rule says).
 - Do not edit backend/.env. Do not start Phase 3 without user go-ahead.
 - Large python edits: write a patch .py file with the Write tool and run it (bash heredocs with quotes break). 

== 1. BRANCH / GIT STATE ==
Branch: phase-2-financial-core (off phase-1-foundation).
Commits on it: fd4612f "feat(money): wallet ledger, atomic debit+status, non-negative balance CHECK (Phase 2 U1-U2)".
UNCOMMITTED (tracked, modified): backend/app/api/routes/meta_webhook.py, backend/app/main.py, backend/app/services/meta_whatsapp_service.py, backend/tests/test_refund_and_prevalidation.py, backend/tests/test_white_bg_product.py.
UNCOMMITTED (new): backend/tests/test_money_recovery.py.
Check: git status --short ; git diff --stat

== 2. DONE AND COMMITTED (fd4612f) ==
U1 Ledger: new table wallet_transactions (model backend/app/models/wallet_transaction.py; kinds opening_balance, debit_order, refund_order, credit_payment, credit_whatsapp_pay). Signed amount; balance_after. Unique partial indexes: (ingestion_id, kind) and (kind, ref). Invariant: SUM(ledger.amount) == customers.wallet_balance.
 Migration backend/alembic/versions/0009_wallet_ledger.py: creates table, backfills one opening_balance row per customer with non-zero balance, adds CHECK ck_customers_wallet_nonneg (NOT VALID then VALIDATE, PG only), refuses to run if any negative balance exists. Customer model also has the CHECK.
 wallet_service.py: record_ledger(); credit_wallet(..., kind, ingestion_id, ref) writes ledger row in same txn; charge_customer_balance(db, customer, price, *, ingestion_id, commit=True); refund_generation_charge(..., ingestion_id). IntegrityError from ledger is caught and logged; credit_wallet(commit=True) returns 0 on duplicate.
 Call sites updated: payment_routes.py (Razorpay, incl. new-customer branch writes ledger), whatsapp_pay_service.py (kind credit_whatsapp_pay, ref=pay_id), meta_whatsapp_service._refund_failed_ingestion (kind refund_order).
U2 Atomic debit: meta_webhook._handle_product_choice now charges with commit=False, then sets amount_charged + status and commits ONCE (debit+ledger+status atomic). On commit error: rollback; if a debit ledger row exists (commit reached server) the order is completed from the ledger, else claim released to awaiting_choice and error re-raised.
Tests added: backend/tests/test_wallet_ledger.py (invariant helpers assert_ledger_matches_balances, LedgerTestBase; crash injection; 100 concurrent debits and CHECK on PG), backend/tests/test_migrations.py (backfill test).
Review agents ran on U1/U2; findings fixed (ledger IntegrityError handling, NOT VALID check, commit-after-server-commit case).

== 3. DONE BUT UNCOMMITTED (U3: refund integrity + recovery) ==
In meta_whatsapp_service.py:
 - _refund_failed_ingestion: amount and wallet come from the debit ledger row. Fallback only for pre-ledger orders: amount_charged > 0 + phone lookup. NULL/0 amount_charged and no debit row -> NO refund (removed old "NULL -> Pack 1 price" fallback, which created money).
 - _advance_status(db, ingestion_id, expected, new): guarded UPDATE; workers use it for processing->generated, generated->delivered, generated->delivered_partial so a swept (failed+refunded) order is never delivered; worker returns False/True without notifying.
 - recover_unrefunded_failed_orders(older_than): failed/delivery_failed orders with amount_charged>0 and no refund audit row get refunded; re-checks status per row, ordered by updated_at, notifies only the process that moved the money.
 - release_abandoned_choice_claims(older_than): choice_claimed older than 5 min with NO debit ledger row -> awaiting_choice.
 - run_recovery_sweep_forever(stuck_after) every 180s (constants RECOVERY_SWEEP_INTERVAL_SECONDS, FAILED_REFUND_GRACE=2min, CHOICE_CLAIM_GRACE=5min). Started in app/main.py lifespan (task cancelled on shutdown); also run once at startup.
 - recover_stuck_paid_orders: STUCK_PAID_STATUSES now includes "stored"; "generated" waits GENERATED_PATIENCE=3x longer; error text says "Recovered by the sweep".
In meta_webhook.py retry_delivery: refuses if refund exists (audit or ledger) or if no product_code AND no amount_charged; status reset is an atomic guarded UPDATE; after reset re-checks for a refund and undoes the reset.
Tests: new backend/tests/test_money_recovery.py (12 tests); edited test_refund_and_prevalidation.py (ingestion fixture now sets amount_charged=PRICE) and test_white_bg_product.py (legacy-refund test replaced by "no recorded charge -> no refund" and "pre-ledger with amount_charged -> refunded"; legacy retry test row gets amount_charged=PACK). These test edits are DELIBERATE behaviour changes, keep them.
STATUS OF LAST STEP: a third review agent's fixes ("patch9", applied to meta_whatsapp_service.py and meta_webhook.py just before the user interrupted) were applied but NOT YET TESTED. Before it, the full suites were green: SQLite 718 passed, PostgreSQL 730 passed (after fixing test_retry_legacy_null_goes_to_pack_1). Verify the current state yourself with the commands in section 5.

== 4. NEXT STEPS (in order) ==
 1. Run section 5 checks; fix anything red. Possible issue areas: the new "stored" status in STUCK_PAID_STATUSES, the or_()/generated_cutoff query, datetime import changes at top of meta_whatsapp_service.py.
 2. Commit U3 by pathspec (files in section 1 lists + tests/test_money_recovery.py).
 3. Production READ-ONLY check (user approved): count customers with negative wallet_balance and total customers in production Supabase, in a READ ONLY transaction, no writes, never print the URL. Reuse the style of backend/scripts/supabase_readonly_check.py (add a check there, or a tiny new script). Migration 0009 itself refuses to run if negatives exist.
 4. Remaining Phase 2 units (roadmap items MON-4, 5, 9, 10, 11, Q-3, UX-4, DATA-6 done):
    a. MON-4: Razorpay webhook requires currency == INR; handle refund.* and payment.dispute.* events as negative ledger entries (claimed once; may leave a balance that needs a clear policy -- CHECK balance>=0 means a refund/dispute larger than balance needs a decision: ask user).
    b. MON-5: normalise phone to E.164 everywhere; 10-digit last-digits fallback only if the input has no country code (wallet_service.find_customer_by_phone). Also _same_sender in meta_webhook.
    c. MON-9: atomic trial credit: UPDATE customers SET trial_credits_used = trial_credits_used + 1 WHERE trial_credits_used < trial_credits_total (entitlement_service.py).
    d. MON-10: processed_messages table (message_id PK) claim at top of webhook handler for text/button messages (today only image messages are deduplicated by unique external_message_id).
    e. MON-11: pending_payments table for Razorpay payments with no phone (today they credit "cust_..." as whatsapp_id); Razorpay handler skips payments linked to a whatsapp_payment_orders row.
    f. Q-3: WhatsApp Pay reconcile fixes (next_check_at backoff, re-check dispatch_failed/failed/expired for 24h, one short session per order) + Razorpay payment-link reconcile.
    g. UX-4: check capacity (daily cap/provider quota) BEFORE the debit.
    h. Make migration 0002 downgrade non-destructive (known hazard: "alembic downgrade base" drops customers; pinned in tests/test_migrations.py).
    i. Optional: pg_try_advisory_lock around the recovery sweep for multi-process runs (full fix belongs to Phase 3 scheduler, ARC-2).
 5. Each unit: write PG-capable tests (invariant: SUM(ledger)==balance; refunds <= debits per ingestion; crash-injection at each step), run review agents (regression reviewer + adversarial reviewer, read-only, told not to run pytest/servers), fix confirmed findings, commit by pathspec.
 6. END OF PHASE: run the gate and write the report, tell user to confirm GitHub CI green on the same commit, then tag phase-2-verified (user pushes). Then produce the Phase 2 work-audit log (worklog\phase-2-financial-core.json + build PDF, see section 6).

== 5. COMMANDS (run from C:\PROJECTS\Moraa Gemvision\backend unless noted) ==
Lint:            venv\Scripts\python.exe -m ruff check .
SQLite tests:    venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -W ignore -rf
PG tests (local throw-away server via pgserver, ~3 min):
   PowerShell:   $env:MORAA_TEST_DB="postgres"; venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -W ignore -rf
   Git Bash:     MORAA_TEST_DB=postgres venv/Scripts/python.exe -m pytest -q -p no:cacheprovider -W ignore -rf
Only new money tests:  venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -W ignore tests/test_wallet_ledger.py tests/test_money_recovery.py tests/test_migrations.py
Show uncommitted work: git status --short ; git diff --stat
Commit example (U3):
   git add backend/app/api/routes/meta_webhook.py backend/app/main.py backend/app/services/meta_whatsapp_service.py backend/tests/test_refund_and_prevalidation.py backend/tests/test_white_bg_product.py backend/tests/test_money_recovery.py
   git commit -m "feat(money): ledger-driven refunds, guarded transitions, recovery sweep, safer retry (Phase 2 U3)" -- <same files>
Phase gate (full, ~7 min; do not run other heavy things meanwhile; the load harness is CPU sensitive):
   venv\Scripts\python.exe scripts\phase_gate.py --phase "Phase 2" --live-readonly
   Optional staging pooler write test: set STAGING_DATABASE_URL in the shell environment ONLY (never in files/chat), then add --pooler-staging. The staging password was reset by the user; ask for the new URL if needed; never print it.
Work audit log: use the work-audit-log skill; JSON schema in its references/schema.md; build: python <skill-dir>\scripts\build_report.py worklog\phase-2-financial-core.json (use --check first). Phase 1 log exists at worklog\phase-1-foundation.json as a style example.

== 6. IMPORTANT CONTEXT AND DECISIONS ==
 - USER DECISION (partial packs): NO partial-pack refund/regeneration. Policy is "garbage in, garbage out": deliver what is generated; no automatic refund for a pack that delivers only some styles. The Full Catalog Pack will grow to 8 styles (a 8th style is in development; do not hard-code 6). Roadmap item MON-6/UX-3 is DROPPED.
 - USER DECISION: do a read-only production check for negative balances before migration 0009 is ever run there (step 4.3).
 - Production migration 0009 is NOT applied anywhere yet. Deploy order: take care that old app code does not run against the new schema between backfill and new code (old code writes no ledger rows and would break SUM==balance). Plan a short write freeze or a reconciliation row. Put this in the runbook.
 - Test DB mode: MORAA_TEST_DB=postgres gives each test its own schema in a throw-away local PostgreSQL (pgserver). Default is SQLite. Tests never touch Supabase (guard in tests/db_support.py).
 - Flake hardening already done in Phase 1 (pool_timeout 120, barriers 120s, error collection in thread tests). If a PG test flakes, the gate report names it.
 - Known open load-harness failures owned by later phases: e (provider faults), f (text dedup, Phase 2 item MON-10 should improve it: then update tests/load_scenarios/expected_verdicts.json to tighten), h (event-loop stalls), i (memory), k (DB pool exhaustion).
 - Known accepted risks: 27 pip-audit advisories in backend/pip-audit-ignore.txt (mostly Pillow 11; upgrade to 12 awaits user decision and an image-output regression check).
 - Known weak spots found by reviewers and NOT fixed yet: the sweeps run in every uvicorn worker (duplicate work, idempotent but noisy) -> Phase 3 scheduler; a 'generated' order whose delivery takes >30 min could be refunded though images were sent (very unlikely, narrowed by 3x patience); retry endpoint has only require_auth (no admin role) -> SEC-4 in Phase 4.
 - Money invariants to keep true forever: (1) SUM(wallet_transactions.amount) per customer == customers.wallet_balance; (2) one debit_order and at most one refund_order per ingestion (DB unique index); (3) a payment id credits once (audit claim uq_audit_logs_money_once plus ledger (kind, ref)); (4) refund <= debit; (5) debit+status commit atomically.
 - Image prompt quality is locked by backend/tests/snapshots/prompt_hashes.json (60 hashes). Do not change prompt builders in Phase 2.
 - Windows quirks: use Write tool for big patch scripts; avoid sleep loops (use run_in_background / Monitor); line-ending warnings (LF->CRLF) from git are harmless.
 - Process agents: review agents are general-purpose subagents, read-only; give them the diff, the callers, and ask for ranked, file:line defects.

== 7. OPEN QUESTIONS FOR THE USER (ask when reached) ==
 - Dispute/refund events (MON-4) that exceed the wallet balance: allow negative balance (conflicts with new CHECK), or clamp and flag for manual review?
 - Phase 3 choices later: hosting (VPS vs managed), object storage (R2/S3), queue broker (Redis), Gemini quota request, fund or drop the OpenAI fallback.
