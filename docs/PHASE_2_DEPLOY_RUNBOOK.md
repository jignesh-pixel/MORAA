# Phase 2 deployment runbook (financial core)

Written 2026-10-02. For whoever puts Phase 2 on the live system. **Nothing in Phase 2 has been applied to production yet.**
Production is still at database version `0008_customer_access_tiers`; the code now expects `0012_payment_reconcile`.
That is why the gate's "live database" step fails today: it is a deployment step, not a code defect.

## What the new database versions do

| Version | What it adds | Safe to repeat? |
|---|---|---|
| 0009 `wallet_ledger` | A record of every wallet change (`wallet_transactions`); one opening entry per customer with a balance; a rule that a balance can never be negative | Yes. Refuses to run (with a message) if any balance is negative |
| 0010 `processed_messages` | Remembers handled WhatsApp message ids so a repeat delivery is ignored | Yes |
| 0011 `pending_payments` | A parking list for Razorpay payments with no usable phone number | Yes |
| 0012 `payment_reconcile` | "Next check" columns on WhatsApp Pay orders, and a table of Razorpay links we sent | Yes |

Rolling back past 0001 or 0002 no longer deletes customers or balances (those downgrades now keep the data).

## Order of steps (the part that matters)

1. **Before anything:** run `python scripts/wallet_negative_check.py` against production (read-only). On 2026-10-02 it reported
   11 customers, 0 negative balances, so 0009 can pass its own safety check. Run it again right before deploying.
2. **Take a database backup** (Supabase backup or a dump). Phase 4 will formalise this; do it by hand now.
3. **Short write freeze** (a few minutes): stop the OLD app so it cannot change wallet balances without writing ledger rows.
   The old code writes no ledger rows. If it runs after 0009 has made its opening entries, the ledger and the balances
   drift apart (the rule "ledger total = balance" breaks) and nothing repairs that automatically.
4. Run `alembic upgrade head` (applies 0009 to 0012). Do not start the old code again after this.
5. Start the NEW app. At startup it refuses to run if the database is behind, so a forgotten step is caught.
6. Check, read-only: `python scripts/supabase_readonly_check.py` should report "up to date" at `0012_payment_reconcile`.
7. If you must go back: restore the backup, then run the old code. Do not run the old code against the upgraded database.

If a freeze is impossible, the alternative is a one-time correcting entry per customer after the new code is live
(sum of ledger versus balance). That needs a developer; the freeze is simpler and safer.

## New settings (all optional, safe defaults)

- `RAZORPAY_LINK_RECONCILE_INTERVAL_SECONDS` (default 300; 0 turns the Razorpay link check off). Needs the Razorpay API keys already in use.
- No new secrets are required.

## Things to know after deploying

- Payments with no usable phone now land in the `pending_payments` table with status `pending`. Someone should look at that
  table (and the audit rows `razorpay_payment_review` / `razorpay_clawback` with status `pending`) at least daily until a dashboard exists.
  A developer credits a parked payment with `pending_payment_service.credit_pending_payment(db, payment_id, phone)`; it is safe to run twice.
- Razorpay refunds and lost chargebacks now take money back out of the wallet. If the customer already spent it, the wallet goes to zero
  and the shortfall is flagged (`razorpay_clawback`, status `pending`). Owner decision recorded 2026-10-02.
- No refund is ever given for images the customer is unhappy with (owner decision). Failed orders (nothing generated) are still refunded automatically.
- The recovery and reconcile sweeps run inside every app process. With several processes they repeat harmless work. A single scheduler is Phase 3.
- The `processed_messages` table is cleaned of ids older than 14 days by the existing sweep.
