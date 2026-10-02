# Morning summary (written 2026-10-02, night)

Plain language, for you to read first. Your developer can use the technical documents it points to.

## Where things stand

All six development phases are now **built and tested**. Nothing has been applied to your live database or your live server, and nothing has been pushed to GitHub. Verification and locking of each phase is the next step, and it needs you (see "What I need from you").

| Phase | What it gives you | Branch | Built? |
|---|---|---|---|
| 1 Foundation | Tests on a real database, one command that re-checks everything | (already done earlier) | yes |
| 2 Financial core | Every rupee has a ledger row; payments, refunds and chargebacks move money at most once; orders are declined before charging when the daily limit is full | `phase-2-financial-core` | yes |
| 3 Runtime hardening | The bot stays responsive under load; failed image calls do not eat the daily limit | `phase-3-runtime-hardening` | yes |
| 4 Operations | Health pages, metrics, WhatsApp alerts to you, logs you can follow, safer logins, backup and restore scripts | `phase-4-operations` | yes |
| 5 Scale | Paid orders survive a restart; only one server runs each periodic job; a failing provider is skipped; a customer can have at most 3 paid orders in progress; honest delivery times; Docker files | `phase-5-scale` | yes (some parts wait for your decisions) |
| 6 Compliance and cleanup | Consent step, "DELETE MY DATA", retention job, receipt wording, IGST option, AI cost log and alert, a separate limit for team orders | `phase-6-compliance` (includes phase 5 and its review fixes) | yes (privacy parts are switched off until you supply wording) |

Each phase was reviewed by independent read-only reviewers (one looking for regressions, one trying to break it). Their findings for phases 4 and 5 were fixed. Phase 6 has had its own tests and a full gate run (see `docs/phase-gates/`) but not yet an independent review.

## What changed for your customers (only these things)

1. The "Processing your order" message says "Usually ready in about N minutes" once the system has measured three real orders. Until then it still says 20-30 seconds.
2. A fourth paid order from the same customer while three are still in progress is declined with "nothing was charged".
3. A photo that was cut off by a server restart gets "please send it again" (nothing charged).
4. The PDF after a recharge is titled "Payment receipt" and says it is not a GST tax invoice.
5. If you switch consent on: a new number sees your privacy notice and an "I agree" button first. Customers can send DELETE MY DATA / CONFIRM DELETE.

Image quality and the image prompts are untouched (the prompt check still reports "changed 0").

## What I need from you

Open `docs/QUESTIONS_FOR_MORNING.md`. The ones that block the most:

1. **Production database upgrade:** when may we apply migrations 0009 to 0019 (a short write freeze; steps in `docs/PHASE_2_DEPLOY_RUNBOOK.md` and `docs/OPERATIONS_RUNBOOK.md`)? Nothing above takes effect on the live system until this is done.
2. **Secrets and Supabase:** rotate the keys the audit flagged, and confirm Row Level Security is on for every Supabase table.
3. **Hosting, storage and Redis:** where will this run, where do customer photos live, do you want Redis?
4. **Privacy wording** and the **retention periods** (both are ready but off).
5. **GitHub:** may we push the branches and open pull requests so CI runs? (I cannot push or merge on my own.)
6. Smaller settings: `ADMIN_USERNAMES`, `RECHARGE_PAYMENT_URL`, pin `GEMINI_IMAGE_MODEL`, Sentry, the number that receives alerts, Gemini quota, price per AI call.

## Printable audit logs

`worklog/` holds one PDF per phase (phases 1 to 6): print two copies, your developer confirms each task, you check by hand. Tasks marked blocked or dropped say why.

## What was not done, and why

- Photo-burst grouping, object storage, the Redis/Celery queue: need your decisions.
- Splitting the very large files, merging duplicate generation code, merging the seven earring routes: left for after launch (they touch every payment path, or files you froze on purpose).
- A real GST verification provider: needs your decision.
- Backups and restore drill: scripts are written but this computer has no PostgreSQL tools, so they have not been run end to end. Docker files have never been built (no Docker here).
