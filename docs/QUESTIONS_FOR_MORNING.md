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

## Added during the Scale phase
- **Provider limits:** what is your real Gemini image quota (calls per minute)? Once known, set `IMAGE_PROVIDER_RPM` (smooths bursts) and `MAX_CONCURRENT_PROVIDER_CALLS` (how many image calls run at once). Both are 0 = "no limit" until you tell us.
- **Orders per customer:** a customer can now have at most 3 paid orders in progress at once (`MAX_INFLIGHT_ORDERS_PER_CUSTOMER`); the 4th tap is declined with nothing charged. Is 3 right?
- **Photo bursts (UX-1):** today every photo gets its own "choose Studio Shot or Pack" message. Grouping a burst (e.g. "12 photos, ₹600, confirm?") changes the customer flow and how you charge. How should a batch be priced and confirmed? (Not built yet; needs your decision.)
- **Delivery time message (UX-2):** the message still says "Please allow 20-30 seconds" until the system has measured 3 real orders, then it says "Usually ready in about N minutes". Do you want different wording?
- **Customer photos on disk / signed links (DEP-3, SEC-7):** photos are stored on the server's disk and the images folder is only reachable from the server itself (the public-host guard). To run on more than one server they must move to object storage (Cloudflare R2, S3 or Supabase Storage). Which one? (Not built; needs your choice and an account.)
- **Redis / Celery queue (Q-1):** orders are now recorded in the database before they start and are picked up again if the server restarts (no Redis needed). A separate worker queue (Redis + Celery) is only needed if one server cannot cope with the load. Do you want it?
- **Hosting (DEP-1):** `docker/` now has a Dockerfile, a compose file and an HTTPS proxy config, but it has not been run on a real server yet. Which server / provider will it run on?

## Added during the Compliance and cleanup phase
- **Privacy notice and consent (PRIV-2):** the mechanism is built and switched OFF. Please give us the exact wording of the notice customers must agree to (what you keep, why, that photos go to AI providers abroad, how to delete). Your lawyer or adviser should read it. When you give it, set `CONSENT_REQUIRED=true` and `CONSENT_NOTICE_TEXT=...` (max 1,024 characters because it is a WhatsApp message). Customers then see it with an "I agree" button before the welcome form, registration or any photo.
- **Retention periods (DATA-7):** built and switched OFF (`RETENTION_ENABLED=false`) because it deletes customer photos. Proposed: photos 90 days after a finished order; phone numbers hidden in audit notes after 30 days; money records kept 8 years and never touched by this job. Do you approve these periods?
- **"DELETE MY DATA" (PRIV-3):** a customer can send DELETE MY DATA, then CONFIRM DELETE within 15 minutes. It removes their photos and personal details but keeps the money trail. It refuses while there is money in their wallet (support settles that first). Is this wording and behaviour acceptable? Please also decide how you want to settle a leaving customer's remaining wallet balance.
- **Tax receipts (PRIV-4):** the PDF is now titled "Payment receipt" with a note that it is not a GST tax invoice. For inter-state buyers to be charged IGST on the ERPNext invoice, tell your accountant to give us (a) your state's two-digit GST code and (b) the name of the ERPNext tax template that carries IGST; they go in `ERPNEXT_COMPANY_STATE_CODE` and `ERPNEXT_TAX_TEMPLATE_INTERSTATE`. Until then every invoice uses the current template.
- **GST number checking (EXT-8):** the live GSTIN check always answers "unavailable" in production. Do you want a paid GST verification provider (we would integrate it), or should the step be removed? Until then a GSTIN is only checked for format, and invoices print any well-formed GSTIN.
- **Ops team commands (EXT-8):** a team member typing "start" or "help" is treated as an ops command, not a customer message. We built an option (`OPS_EXPLICIT_PREFIX=true`) where ops commands must start with "ops " (for example "ops start"). Do you want it on? It changes how your team types.
- **AI spend (COST-2/COST-4):** every AI image call is now recorded. Tell us the price per call for Gemini and OpenAI (`COST_PER_CALL_GEMINI_RUPEES`, `COST_PER_CALL_OPENAI_RUPEES`) and the daily rupee amount that should trigger a WhatsApp warning (`OPS_ALERT_DAILY_COST_RUPEES`). Team (admin) orders now have their own daily limit of 200 images (`MAX_ADMIN_GENERATIONS_PER_DAY`); is that right? Run `python scripts/cost_report.py` to see the last 7 days.
- **Not done on purpose (needs a decision from you, not a code change):** (1) splitting the two very large files and merging the duplicate generation code (MAINT-1, ARC-4, ARC-5): this touches every payment and order path and every test; we recommend doing it only after launch, in a separate verified step. (2) Merging the seven earring prompt routes into one (MAINT-2): five of those files are frozen by the prompt freeze guard (a deliberate lock), so changing them would need you to unlock them first.

## Added during Phase 8 (SKU packs + Drive delivery), 7 Oct 2026
Defaults taken so the build could continue; each is easy to change once you decide.
- **Credit validity:** a new pack extends every unused credit to 90 days from that purchase (one "valid till" date per customer). Per-pack expiry (older credits expire on their own date) is possible but more complex. Which do you want?
- **Pack 1 (1 SKU):** can always be bought by its catalogue id `pack_1`, but is only shown in the WhatsApp menu when `ECOM_PACK1_ENABLED=true`. Correct reading of "Pack 1 hidden"?
- **Customers with both a wallet balance and SKU credits:** a photo uses an SKU credit first; with no credits the old Studio Shot / Catalog Pack buttons (paid from the wallet) appear as today. With no credits and not enough wallet money, the pack menu is shown instead of the recharge link (only when `SKU_PACKS_ENABLED=true`). OK?
- **The ₹500 Full Catalog Pack (7 styles)** still delivers on WhatsApp, also when Drive delivery is on. Should it move to Drive too, or be retired?
- **Unknown text:** with `SKU_PACKS_ENABLED=true`, any message the bot does not understand gets the pack menu. Fine, or should it stay silent?
- **Registration Flow email** is required in the form (the text registration and "send your email in chat" still work without it). Required or optional?
- **Invoices for packs** are raised at purchase for the whole pack (quantity = SKUs, rate = price per SKU incl. GST). Accountant to confirm timing, item code (`SKU_ERPNEXT_ITEM_CODE`) and HSN/SAC.
- **Out-of-window "ready" message:** needs a Meta-approved utility template (`BATCH_NOTIFY_TEMPLATE_NAME`, two parameters: folder link, SKUs left). Until then such a batch only raises an alert. Please submit the template.
- **Proposed consent wording (not switched on)** now also mentions Drive storage: "...We keep your finished images in a private Google Drive folder shared only with your email, for 90 days." Your lawyer should approve it with the rest of the notice.
