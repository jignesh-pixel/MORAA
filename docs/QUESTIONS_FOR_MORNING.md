# Questions for the owner (collected while working unattended)

Started 2026-10-02 (night). Each item says what is blocked, what I did meanwhile, and what I need from you.
Nothing here stops other work; I moved on to the next part every time.

## Open

1. **OpenAI fallback (EXT-7).** The OpenAI image fallback has failed 74+ times for lack of credit.
   - Do you want to (a) add credit so it works as the backup, or (b) remove it from the chain?
   - Meanwhile: nothing changed. A billing error from OpenAI now stops cleanly (no repeated calls). Removing it later is one setting (`FALLBACK_IMAGE_PROVIDER` left empty).
2. **Gemini quota.** To serve 100 paid orders a minute you need about 600 image calls a minute from Google. Do you want to request that increase, or accept a lower ceiling where customers wait with an estimated time?
   - Meanwhile: the code is being built so either choice works.
3. **Hosting (Phase 3).** One server with Docker Compose (enough up to 100 orders/min), or a managed platform (Railway, Fly, Cloud Run)? I will prepare the Docker files either way, but cannot deploy anything for you.
4. **Image storage (Phase 3).** Cloudflare R2 (recommended, no download fees), Amazon S3, or Supabase Storage?
5. **Queue broker (Phase 3).** Redis is the plan. OK to use Redis (a small hosted Redis or one in the Docker Compose)?
6. **Rotate the secrets (SEC-10).** The live keys (Meta, Razorpay, Gemini, OpenAI, Supabase) should be rotated and the old `.env.bak-*` files deleted. Only you can do this; I never touch `backend/.env`.
7. **Supabase Row Level Security (DATA-5).** Please open Supabase and confirm RLS is ON for every table (if it is off, the public key could edit wallet balances). I cannot check this from here without write access.
8. **Production database upgrade.** Phase 2 needs migrations 0009 to 0012 applied to production, with a short pause of the live system (steps in `docs/PHASE_2_DEPLOY_RUNBOOK.md`). Tell me when you want this done; I will not run it myself.
9. **GitHub.** Pushing the branches, opening the pull requests and confirming CI is green (and the final tags) are yours to do. Branches so far: `phase-2-financial-core`, `phase-3-runtime-hardening`.

## Answered
(none yet)

## Added during the Operations phase
- RECHARGE_PAYMENT_URL is not set in the production .env: please set it to your own payment link (then it can become mandatory).
- ADMIN_USERNAMES: which login name(s) should be administrators? Until set, the admin-only endpoints stay open to any logged-in user.
- Please pin GEMINI_IMAGE_MODEL in the production .env (so image quality can't change by surprise).
- Sentry: do you want error reporting? If yes, create a Sentry project and give the DSN to your developer (needs `pip install sentry-sdk`).
- Which WhatsApp number(s) should receive operations alerts (OPS_ALERT_WHATSAPP_NUMBERS)? Alerts to your own number only arrive inside WhatsApp's 24-hour window unless a message template is approved by Meta.
