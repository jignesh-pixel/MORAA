# Moraa GemVision: pre-deployment audit for Amazon Lightsail

**Date:** 2026-10-04
**Branch audited:** `phase-7-dashboard` @ `729cfa5`
**Method:** Live checks, not documentation. Every claim below comes from a command run today, a file read today, or a **read-only** query against the Supabase database configured in `backend/.env`. All database transactions were opened `READ ONLY`, so PostgreSQL itself refused writes. No code, config or data was changed.

---

## 0. Verdict

**Not ready to switch over yet.** The code itself is in good shape:

- 1,176 backend tests pass.
- ruff, pip-audit, `tsc` and `next build` all pass.
- The live database is at the migration head, the ledger is consistent and RLS is on.

Seven deployment and operations blockers remain. Most are configuration or decisions, not code:

| # | Blocker (P0) | Effort |
|---|---|---|
| 1 | **Next.js 16.2.10 has critical advisories.** One is a Middleware/Proxy bypass for Turbopack apps, which is exactly how the `proxy.ts` password gate is built. Others are RCEs in the image optimiser. | 10 min: bump to 16.3.8+ |
| 2 | **Razorpay keys are test mode** (`rzp_test_…`) | Owner: live keys, live webhook secret, live payment link, live WhatsApp Pay configuration |
| 3 | **78 commits exist only on this laptop.** Branches `phase-2`…`phase-7` and their tags were never pushed, so GitHub CI has never run on them. | 15 min: push, open PR, get CI green |
| 4 | **Secrets not yet rotated** (SEC-10). The two `.env.bak-*` files still hold old secrets. | Owner: about 1 h |
| 5 | **Dashboard and ops channel can't be reached in the planned Docker layout.** The public-host guard 404s `/api/dashboard/*`, the Next.js app isn't in compose, `OPS_INBOUND_URL` is `localhost`, and Apps Script needs a public `/api/ops/notify`. | Decision (section 4.3) |
| 6 | **Which Supabase project is production?** The handover says production is at `0008`. The database in `backend/.env` is already at `0021`, with live traffic today. | Owner: confirm |
| 7 | **The laptop is serving live traffic right now** (last message 2026-10-04 12:08 UTC). It holds 18 in-flight ingestions whose photo paths are Windows-only, so they won't resolve on Linux. | Cutover plan (section 7, phase C) |

If the dashboard and ops channel aren't needed on day one, blocker 5 can be deferred. The WhatsApp bot and payments path can go live with blockers 1–4, 6 and 7 resolved.

---

## 1. Repository and branch state

| Item | Finding |
|---|---|
| Current branch | `phase-7-dashboard`. It contains every other branch: `git branch --no-merged HEAD` is empty. |
| Ahead of `main` | **78 commits**. `main` (2579cb7) is the merge base. |
| On GitHub | Only `main`, `phase-0-hardening`, `phase-1-foundation` and `feature/interactive-prompt-buttons`. Tags: `phase-1-verified`, `gemvision-stable-prompts`. |
| **Local only** | `phase-2-financial-core` … `phase-7-dashboard` and tags `phase-2-gate-passed`, `phase-{3..7}-gate-passed-except-timing`. **This is the only copy of 78 commits.** |
| Fully merged, safe to delete after the push | `backup-run-final`, `backup-today-changes`, `feature/interactive-prompt-buttons` |
| Uncommitted change | `backend/tests/test_money_recovery.py` (+16 lines). It is a **verbatim copy-paste of two existing tests**: `ruff --select F811` reports 2 redefinitions, and the second copy silently replaces the first. Revert it: `git checkout -- backend/tests/test_money_recovery.py`. |
| Untracked | `.claude/work-audit-log/` (skill files), `Claude outputs/*.md`. Harmless. |
| Knowledge graph | `graphify-out/` was built 2026-08-26 from `07227cae`, before phases 2–7, so it is stale. Run `graphify update .` after the merge. |
| Timing gates | Load scenarios `h` and `i` still need a re-run on an unthrottled machine or in CI (handover item 2). |

---

## 2. Database and migrations (Supabase PostgreSQL)

### 2.1 Migration chain

There is a single linear head: `0001 → 0002 → ebdee18538d1 (baseline) → 0003 … → 0021_chat_dashboard`. There are no branches.

The migrations contain no `CREATE INDEX CONCURRENTLY` and no advisory locks, so they are safe through the 6543 transaction pooler. That has been proven in practice: the database got to 0021 through it.

### 2.2 Live database (read-only checks, `scripts/supabase_readonly_check.py` plus ad-hoc queries)

| Check | Result |
|---|---|
| Server | PostgreSQL **17.6**, Supabase transaction pooler `aws-0-ap-south-1:6543`, schema `public` |
| Revision | `0021_chat_dashboard` = code head. **No pending Alembic revisions.** |
| Startup guard `ensure_schema_ready()` | passes |
| `uq_audit_logs_money_once` | present |
| Model vs live drift (`alembic compare_metadata`) | 1 `modify_type` and 4 `modify_nullable`. All are already in the accepted `KNOWN_DRIFT` set in `tests/test_migrations.py`. The other 86 differences are column comments only, which is cosmetic. **No new structural drift.** |
| Row Level Security (DATA-5) | **ON for all 28 tables**, with 0 policies. That means deny-all for `anon` and `authenticated`. The app connects as the owner role, which bypasses RLS. **DATA-5 is resolved.** |
| Wallet invariant `SUM(ledger) = balance` | 0 of 11 customers mismatched. CHECK constraints `ck_customers_wallet_nonneg`, `ck_customers_tier` and `ck_customers_trial_credits_nonneg` are present. |
| Activity | 11 customers, 62 ingestions, 17 chat messages; latest at 2026-10-04 12:08 UTC. `users` = **0**, so no dashboard login exists yet. `order_outputs` and `invoice_records` = 0. |
| In flight now | `awaiting_choice` 10, `unfunded_hold` 7, `processing` 1 |
| Stored photo paths | All 88 `images.file_path` rows have the form `app\uploads\<uuid>\original.jpg`, which is **relative Windows-style** (see 4.4) |

### 2.3 Discrepancy to resolve

`docs/DEVELOPER_HANDOVER.md` §3 says production is at `0008` and that nothing has been applied. The database in `backend/.env` is at `0021`. Either:

- the handover is stale and this database **is** production, already migrated, in which case handover step 4 is done, or
- `backend/.env` points at a staging project.

The test-mode Razorpay keys suggest test usage, but there are 11 real-looking customers. **Confirm the production project ref before cutover.** If production is a different project still at 0008, follow `docs/PHASE_2_DEPLOY_RUNBOOK.md`: freeze writes for 0009, run `wallet_negative_check.py`, then `release.py`.

---

## 3. Feature completeness: WhatsApp webhook, payments, dashboard

| Area | State | What's left |
|---|---|---|
| Meta webhook (`/api/meta/webhook` GET verify and POST receive) | Complete. It covers signature check, dedupe (`processed_messages`), wallet gate, bulk orders, outbox and recovery sweeps. All tests green. | Point Meta at the new URL (section 7, phase C) |
| Registration WhatsApp Flow | Published Flow (`META_REGISTRATION_FLOW_MODE=published`). The Flow JSON has **no data-exchange endpoint**, so no URL needs re-pointing. | — |
| Razorpay webhook (`/api/payments/razorpay/webhook`) | Complete. Handles `payment.captured`, `order.paid`, `payment_link.paid`, `refund.processed`, `payment.dispute.created` and `payment.dispute.lost`. | Live keys and webhook; subscribe exactly these events |
| WhatsApp Pay (native) | On, but `WHATSAPP_PAY_ALLOWLIST` holds **one number** (a pilot). Everyone else gets the Razorpay link. `WHATSAPP_PAY_STRICT` is off. | Owner decision: widen the allowlist or keep the pilot |
| ERPNext invoices | `ERPNEXT_INVOICE_ENABLED=true` with **no local fallback**. Tested only against a fake. | A real end-to-end test against Frappe Cloud before go-live (handover item 7). Also set `ERPNEXT_COMPANY_STATE_CODE` and `ERPNEXT_TAX_TEMPLATE_INTERSTATE` for IGST. |
| GST verification | `GST_VERIFICATION_ENABLED=true` with `GST_PROVIDER=mock`. **The mock is ignored in production** (`gst_service.py:190`), so every lookup reports "unavailable". | Set `GST_VERIFICATION_ENABLED=false` until a vendor is chosen, or set `GST_PROVIDER=http` with vendor keys |
| Consent, retention, erasure | Built and switched off pending owner and lawyer wording. Retention periods are approved (90/30). | `RETENTION_ENABLED=true`; consent once the wording is approved |
| Chat dashboard: backend API (`/api/dashboard/*`, `/api/auth/google`) | Built and tested; admin-only; signed image links | **Not reachable through Caddy** (guard). No user exists. Google client not set. |
| Chat dashboard: frontend (`ChatsPage`, sidebar "WhatsApp Chats") | Built and wired into `page.tsx` and `Sidebar.tsx`. Its files pass ESLint. | Hosting decision (4.3). `NEXT_PUBLIC_API_URL` is set **at build time**. |
| Drive archive | Built, off. Tested with a fake only. | OAuth client, refresh token, folder id |
| Ops team channel (Next.js `/api/ops/*`) | Works locally | `OPS_INBOUND_URL` must be container-reachable. Receipt and screenshot storage needs `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` and a private bucket `ops-media`. **These are unset, so attachments are silently dropped** (`lib/ops.ts:223`). |
| Alerts to owner | Built | `OPS_ALERT_WHATSAPP_NUMBERS` is unset. Messages outside the 24 h window need an approved template. |
| `ClaudeProvider` | Stub (TODO) | Not on any default path. Never set `PRIMARY_AI_PROVIDER=claude`. |
| Graph API version | `v21.0` hardcoded in `meta_whatsapp_service.py:35-37` and `whatsapp_pay_service.py:51`; the frontend defaults to it too. **Meta schedules v21.0 deprecation for 2027-01-21.** | Not a blocker now. Move it into a setting and bump it before January. |

---

## 4. Environment, secrets and configuration

### 4.1 Backend: what the production `docker/.env` must contain

The app **refuses to boot** with `ENVIRONMENT=production` unless the starred (★) variables are valid (`config.py:577-602`).

| Group | Variable | Local `.env` today | Production action |
|---|---|---|---|
| Core | ★`ENVIRONMENT=production` | production | keep |
| | ★`SECRET_KEY` (≥ 32 chars, ≥ 10 distinct) | set (64) | **new value**, identical across workers |
| | ★`DEBUG=false`, ★`ALLOW_UNSIGNED_WEBHOOKS` unset | ok | keep |
| | `LOG_LEVEL` | **DEBUG** | `INFO` (and `LOG_JSON=true` if you add a collector) |
| | `CORS_ORIGINS` | localhost:3000 | the dashboard origin (see 4.3) |
| Supabase | ★`DATABASE_URL` (non-SQLite) | pooler :6543 | keep the transaction pooler for the app. For `pg_dump` backups use the **session pooler :5432** or a direct connection. |
| Meta WhatsApp Cloud API | `META_WHATSAPP_TOKEN` (permanent system-user token) | set | **rotate** |
| | ★`META_APP_SECRET` | set | **rotate** |
| | `META_VERIFY_TOKEN` | set | new random value; enter the same value in Meta |
| | `META_PHONE_NUMBER_ID` | set | keep |
| | `META_REGISTRATION_FLOW_ID`, `_SCREEN`, `_MODE=published` | set | keep |
| Razorpay | `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | **`rzp_test_`** | **live keys** |
| | ★`RAZORPAY_WEBHOOK_SECRET` | test | **live webhook secret** |
| | `RECHARGE_PAYMENT_URL` | **unset** (falls back to the link hardcoded in the source) | your own live link |
| | `WHATSAPP_PAY_CONFIGURATION_NAME` | `moraa_studio` | must be the configuration linked to the **live** Razorpay account |
| | `WHATSAPP_PAY_ALLOWLIST` | 1 number | decide (section 3) |
| AI providers | `GEMINI_API_KEY`, `OPENAI_API_KEY` | set | **rotate** |
| | `GEMINI_IMAGE_MODEL` | `gemini-3.1-flash-image` | keep it pinned |
| | `IMAGE_PROVIDER_RPM`, `MAX_CONCURRENT_PROVIDER_CALLS` | unset (0 = no limit) | size these to the Gemini quota (owner) |
| | `COST_PER_CALL_GEMINI_RUPEES`, `_OPENAI_`, `OPS_ALERT_DAILY_COST_RUPEES` | unset | set these so the spend alert works |
| | `MAX_GENERATIONS_PER_DAY` | 500 | confirm |
| | `IMAGE_PROVIDER_STARTUP_DIAGNOSTICS` | true | fine, but costs one paid probe per worker per restart |
| | `ANTHROPIC_API_KEY` | — | not needed (the provider is a stub) |
| ERPNext | `ERPNEXT_*` (base URL, key, secret, company, accounts, item, template, mode of payment) | set | **rotate** the key and secret. Add `ERPNEXT_COMPANY_STATE_CODE` and `ERPNEXT_TAX_TEMPLATE_INTERSTATE`. Drop the inline comment on `ERPNEXT_COMPANY` and confirm `moraa` is the legal company name. |
| GST | `GST_VERIFICATION_ENABLED`, `GST_PROVIDER`, `GST_API_URL`, `GST_API_KEY` | true / mock | `false`, or `http` plus vendor keys |
| Ops and alerts | `OPS_ENABLED`, `OPS_TEAM`, `OPS_SECRET` | set | keep. `OPS_SECRET` must equal the frontend and Apps Script value. |
| | `OPS_INBOUND_URL` | **`http://localhost:3000/...`** | `http://frontend:3000/api/ops/inbound` (same compose network) |
| | `OPS_ALERT_WHATSAPP_NUMBERS` | unset | the two owner numbers (`docs/ENV_TO_ADD.md`) |
| | `SENTRY_DSN` | unset | set it (`sentry-sdk==2.19.2` is in `requirements.txt`, so the image installs it; the local venv lacks it) |
| Dashboard | `ADMIN_USERNAMES`, `DASHBOARD_ALLOWED_EMAILS` | unset | set them, then run `scripts/create_dashboard_user.py` |
| | `GOOGLE_CLIENT_ID` | unset | optional (Google sign-in) |
| | `LOCAL_PEER_ADDRESSES`, `LOCAL_API_HOSTS` | default | only for the SSH-tunnel option (4.3) |
| | `DRIVE_ENABLED`, `GOOGLE_DRIVE_*`, `DRIVE_FOLDER_ID` | unset | optional |
| Compliance | `RETENTION_ENABLED=true` (90/30) | unset | set it (already approved) |
| | `CONSENT_*` | unset | after the wording is approved |
| Docker only | `PUBLIC_HOSTNAME`, `WEB_CONCURRENCY` | — | required by compose |
| **Must NOT be set** | `UPLOAD_DIR`, `REPORT_DIR`, `LOG_DIR`, `HOST` | `app/uploads`, … | **Do not copy these from `backend/.env`.** `env_file` overrides the image's `ENV UPLOAD_DIR=/data/uploads`, so photos would land outside the `moraa-data` volume and be lost on every redeploy. |
| Dead keys (delete) | `WHATSAPP_BUSINESS_ID`, `WORKERS`, `ENABLE_WALLET_GATE` | present | nothing reads them |

### 4.2 Frontend: production variables

`NEXT_PUBLIC_*` values are **inlined at `next build` time**. Fourteen files fall back to `http://localhost:8000` if `NEXT_PUBLIC_API_URL` is missing during the build.

| Variable | Status | Action |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | set **before `next build`** to whatever origin the browser uses to reach the API (4.3) |
| `NEXT_PUBLIC_GOOGLE_CLIENT_ID` | unset | same value as the backend's `GOOGLE_CLIENT_ID` (build time) |
| `DASHBOARD_PASSWORD`, `DASHBOARD_USER` | **unset, so the gate is OFF** | **Required** if the app is reachable from the internet. Otherwise `/api/gemini/analyze` is an open proxy spending your Gemini key. |
| `GEMINI_API_KEY` | set | rotate |
| `OPS_ENABLED`, `OPS_TEAM`, `OPS_SECRET`, `OPS_SHEET_URL`, `OPS_TEMPLATE_LANG` | set | keep (`OPS_SECRET` must match the backend) |
| `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `GRAPH_VERSION` | set; the token differs from the backend's | rotate. Consider using the same system-user token as the backend. |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | **unset** | needed for ops attachments (bucket `ops-media`). This is a service-role key: server-side only, never `NEXT_PUBLIC_`. |
| `META_APP_SECRET` | unset | not needed (`verifyMetaSignature` is never called) |
| `DASH_KEY` | set | dead (nothing reads it); delete |

### 4.3 Decision needed: how the dashboard and ops routes are reached

**What happens today**

- The public-host guard (`public_host_guard.py`) serves only `/api/meta/webhook` and `/api/payments/razorpay/webhook` to non-local peers.
- Behind Caddy every request is non-local, which is correct for the webhooks.
- But the dashboard's **browser-side** calls to `/api/dashboard/*` would 404.
- Separately, Apps Script must POST to the Next.js `/api/ops/notify` over the internet.

**Option A (recommended for launch day; no code change)**

1. Add a `frontend` service to compose and a second Caddy site for it. `/api/ops/*` stays public because it carries its own secret checks, and everything else sits behind `DASHBOARD_PASSWORD`.
2. Leave the backend dashboard API private.
3. Owners open the dashboard through an SSH tunnel: `ssh -L 8000:127.0.0.1:8000 …`. Publish the api as `127.0.0.1:8000:8000` and build the frontend with `NEXT_PUBLIC_API_URL=http://localhost:8000`.
4. Inside Docker the peer will be the bridge gateway (e.g. `172.18.0.1`), not loopback. Add it to `LOCAL_PEER_ADDRESSES`; find it with `docker network inspect docker_default`. Requests from Caddy carry `X-Forwarded-For`, so they stay public.
5. **Test this on the box.** It has never been run.

**Option B (later, small code change):** add an opt-in setting that lets `/api/dashboard/*` and `/api/auth/*` through the guard and relies on the JWT plus admin check, with Caddy in front.

**Option C:** a VPN (Tailscale or WireGuard) instead of SSH, with the same `LOCAL_PEER_ADDRESSES` caveat.

### 4.4 Hardcoded localhost, local IPs and ngrok

| Location | Finding | Action |
|---|---|---|
| `backend/app/**` | No hardcoded tunnel or host. Only overridable defaults remain (`HOST`, `CORS_ORIGINS`, `LOCAL_*`, `CELERY_*` with eager mode on) and guard comments. | Set via env (4.1) |
| `frontend/src/**` | 14× `process.env.NEXT_PUBLIC_API_URL \|\| "http://localhost:8000"` | Set it at build time. Optionally fail the build if it is missing. |
| `backend/.env` | `OPS_INBOUND_URL=http://localhost:3000/...`, `CORS_ORIGINS=localhost` | Replace in `docker/.env` |
| `HOW_TO_RUN.md:618,658` | ngrok instructions with the static domain `drainpipe-unsoiled-native.ngrok-free.dev`. The Supabase project ref appears in a sample `DATABASE_URL`. | Rewrite for Lightsail and remove the project ref |
| Meta App Dashboard and Razorpay Dashboard | The callback URLs are presumably still the ngrok domain (not visible from code) | Re-point at cutover (section 7, phase C) |
| Tests and `scripts/live_probe.py` | ngrok hostnames used as **test fixtures** | Keep (intentional) |
| **Stored data** | 88 `images.file_path` rows are `app\uploads\<uuid>\original.jpg`. The code opens them directly (`meta_whatsapp_service.py:1725,2068`, `dashboard_service.py:71`, `upload_service.py:217`). On Linux they resolve to nothing. | **Cutover choice:** (a) don't migrate old photos, and let the recovery sweeps close the 18 in-flight rows with a "please resend" message, or (b) rsync `app/uploads/` into the volume and rewrite paths to `/data/uploads/<uuid>/original.jpg` in one transaction after a backup |

### 4.5 Docker artefacts (never built: expect small fixes)

| File | Finding |
|---|---|
| `backend/.dockerignore` | Excludes `.env*`, venvs, `data`, `logs` and tests. Good. **It does not exclude `app/uploads/*` or `app/reports/*`**, so a build from this working tree bakes in **102 customer photos (14 MB)**. A fresh `git clone` on the server is clean, but add both lines anyway. |
| `backend/Dockerfile` | `postgresql-client` comes from Debian. The Supabase server is **17.6**, and `pg_dump` refuses to dump a newer server major version. Check `pg_dump --version` ≥ 17 in the built image. The `python:3.12-slim` trixie base ships 17; bookworm ships 15. Pin `python:3.12-slim-trixie` to make it explicit. **The handover's "client ≥ 15" is wrong for this server.** |
| `docker-compose.yml` | Order is fine (`migrate` → `api` → `caddy`). The frontend is missing (4.3). The api is not published on any host port, which is correct for Option A until you add `127.0.0.1:8000:8000`. |
| `Caddyfile` | Good: it overwrites `X-Forwarded-For` and caps bodies at 12 MB. Create the DNS A record **before** the first `up`, or Let's Encrypt retries will hit rate limits. |
| `docker/README.md` step 5 | `https://<host>/api/meta/webhook/health` will **404**: the guard allowlist is exact-match. Use the verify-token GET in section 7, phase C instead. |
| Sizing | Two uvicorn workers plus Pillow image buffers plus Caddy. Use the 4 GB Lightsail plan in Mumbai (ap-south-1, the same region as the Supabase pooler), as `Claude outputs/vps-choice-explained-simply.md` concludes. |

---

## 5. Security findings

| Sev | Finding | Fix |
|---|---|---|
| **Critical** | `next@16.2.10`: `npm audit --omit=dev` reports 1 critical (Next.js: proxy bypass with Turbopack, Image-Optimizer and `next/og` RCE, SSRF, cache confusion) and 2 high (`postcss`, `sharp`/libvips) | `npm install next@16.3.8 eslint-config-next@16.3.8 --save-exact`, then rebuild |
| High | Test-mode payment keys; secrets not rotated; `.env.bak-before-ops`, `.env.bak-before-paid-test` and `frontend/.env.local.bak-before-ops` hold old secrets | Rotate everything before writing it to the server; delete the backups |
| High | Frontend gate off (`DASHBOARD_PASSWORD` unset) | Set it before any public exposure |
| Medium | `LOG_LEVEL=DEBUG` in a production-mode env | `INFO` |
| Medium | Customer photos not docker-ignored | `.dockerignore` (4.5) |
| Medium | `lib/ops.ts:storeOpsMedia` sends the WhatsApp token to whatever `url` Graph returns, with no host check. The backend has one (SEC-11). | Mirror `META_MEDIA_HOST_SUFFIXES` |
| Low | Supabase project ref in `HOW_TO_RUN.md` | Remove |
| ✅ | RLS on for all 28 tables; webhook signatures fail closed; production boot guard; pip-audit clean (49 documented ignores) | — |

---

## 6. Local check results (run today, 2026-10-04)

| Check | Command | Result |
|---|---|---|
| Backend lint | `ruff check .` | ✅ pass. The ruleset is narrow (E9/F63/F7/F82): `--select F811` catches the 2 duplicate tests. |
| Backend tests (SQLite) | `pytest -q -p no:cacheprovider -W ignore` | ✅ **1176 passed, 21 skipped** (PostgreSQL-only) in 143 s |
| Backend tests (PostgreSQL) | `MORAA_TEST_DB=postgres pytest …` | ⚠️ **Not run.** The tool's permission check blocked it. Run it yourself (section 7, phase A). |
| Dependency audit | `pip-audit -r constraints.txt --no-deps --disable-pip --strict` | ✅ no known vulnerabilities (49 ignored, documented) |
| Live database | `scripts/supabase_readonly_check.py` | ✅ all checks passed (read-only) |
| Frontend types | `npx tsc --noEmit` | ✅ pass |
| Frontend lint | `npx eslint` | ❌ **27 errors, 14 warnings**, all in pre-existing pages (`PhotoFilterPanel`, `HistoryPage`, `ReportsPage`, `NewAnalysisPage`, …): `set-state-in-effect`, `no-explicit-any`, `refs`. None are in the dashboard files. **CI doesn't run ESLint.** |
| Frontend build | `npx next build` | ✅ pass (Turbopack; 4 API routes plus proxy) |
| Frontend dependency audit | `npm audit --omit=dev` | ❌ 1 critical, 2 high (section 5) |

---

## 7. Pre-flight checklist

### Phase A: on this laptop (Git Bash, repo root)

**1. Clean the tree and patch Next.js**

```bash
git checkout -- backend/tests/test_money_recovery.py
cd frontend && npm install next@16.3.8 eslint-config-next@16.3.8 --save-exact && npm audit --omit=dev && cd ..
```

Then add `app/uploads/*`, `app/reports/*` and `!app/uploads/.gitkeep` to `backend/.dockerignore`, and commit.

**2. Backend gates** (from `backend/`)

```bash
venv/Scripts/ruff.exe check .
venv/Scripts/python.exe -m pytest -q -p no:cacheprovider -W ignore
MORAA_TEST_DB=postgres venv/Scripts/python.exe -m pytest -q -rs -p no:cacheprovider -W ignore
venv/Scripts/python.exe tests/load_scenarios/run_load_scenarios.py --only h,i
venv/Scripts/python.exe scripts/phase_gate.py --phase "phase-7-dashboard" --live-readonly
venv/Scripts/python.exe scripts/release.py --check-only
```

Expected results:

- ruff: pass.
- SQLite tests: 1176 passed.
- PostgreSQL tests: no "PostgreSQL-only test" skipped.
- Load scenarios `h` and `i`: pass on a cool, plugged-in machine.
- Phase gate: `prompts: … changed: 0` and live read-only PASS.
- `release.py --check-only`: "ready to start".

**3. Frontend gates** (from `frontend/`)

```bash
npm ci
npx tsc --noEmit
npx eslint
npx next build
```

ESLint errors are pre-existing. Either fix them or accept them for launch, but don't add new ones.

**4. Push and get CI green** (from the repo root)

```bash
git push -u origin phase-2-financial-core phase-3-runtime-hardening phase-4-operations phase-5-scale phase-6-compliance phase-7-dashboard
gh pr create --base main --head phase-7-dashboard --title "Phases 2-7: production hardening and chat dashboard"
```

Merge to `main` only with the owner's approval. Optionally add `npx eslint` and `npx next build` to the CI frontend job.

**5. Secrets**

- Rotate the Meta token and app secret, Razorpay (switch to **live**), Gemini, OpenAI, ERPNext, the Supabase DB password and `SECRET_KEY`.
- Delete the `.env.bak-*` files.
- Put the new values **only** in the server's `docker/.env`.

### Phase B: Lightsail server

1. Create the instance: Mumbai (ap-south-1), Ubuntu LTS, **4 GB**. Attach a static IP. In the firewall open 80 and 443 to everyone, and 22 only to your IP.
2. DNS: an A record for `PUBLIC_HOSTNAME` pointing at the static IP. Wait until it resolves.
3. Supabase: if Network Restrictions are on, allow the static IP.
4. Install Docker Engine and the compose plugin. Then `git clone` and `git checkout phase-7-dashboard` (a fresh clone means no photos or `.env` in the build context).
5. `cd docker && cp env.example .env`. Fill it in from section 4.1. **Do not** add `UPLOAD_DIR`, `REPORT_DIR`, `LOG_DIR` or `HOST`.
6. Build with `docker compose build`, then confirm `docker run --rm moraa-backend pg_dump --version` prints ≥ 17. Don't use `docker compose run … api` for this: it starts the `migrate` dependency.
7. Back up and run a restore drill:
   - `DATABASE_URL=<session-pooler :5432 URL> python scripts/db_backup.py`
   - then `scripts/db_restore_check.py` into a scratch database.

### Phase C: cutover (pick a quiet window)

1. Ask the owner for the go time. Confirm the production project ref (blocker 6).
2. Decide what happens to the 18 in-flight rows and the old photos (4.4).
3. `docker compose up -d --build`. `migrate` should report "OK: database is at 0021_chat_dashboard".
4. Check readiness on the box: `docker compose exec api curl -fsS http://127.0.0.1:8000/health/ready`
5. Public smoke test:
   - `curl "https://$PUBLIC_HOSTNAME/api/meta/webhook?hub.mode=subscribe&hub.verify_token=$META_VERIFY_TOKEN&hub.challenge=42"` should return `42`.
   - `curl -i https://$PUBLIC_HOSTNAME/api/history` should return `404` (the guard working).
6. **Stop the laptop backend and ngrok.**
7. Meta App Dashboard → WhatsApp → Configuration:
   - Callback URL `https://$PUBLIC_HOSTNAME/api/meta/webhook`, with the new verify token.
   - Confirm the `messages` field is subscribed.
8. Razorpay Dashboard (**Live mode**) → Webhooks:
   - URL `https://$PUBLIC_HOSTNAME/api/payments/razorpay/webhook`, with the new secret.
   - Events: `payment.captured`, `order.paid`, `payment_link.paid`, `refund.processed`, `payment.dispute.created`, `payment.dispute.lost`.
9. Live acceptance test:
   - Send a photo from a team number.
   - Do one small real recharge, then one paid order.
   - Check: delivery, the invoice in ERPNext, an `order_outputs` row, ledger = balance, and `docker compose logs api` clean.
10. Run `scripts/supabase_readonly_check.py` and the ledger check again, then watch Sentry and alerts for 24 h.

### Phase D: after launch

- Dashboard hosting (4.3).
- GST vendor.
- Consent wording.
- Drive archive.
- Graph API version bump before 2027-01-21.
- ESLint backlog.
- `graphify update .`.
- Rewrite `HOW_TO_RUN.md`.
- Delete the merged backup branches.

---

## Sources

- Live checks in this audit: git, ruff, pytest, pip-audit, npm audit, tsc, eslint, next build, and read-only Supabase queries (scripts in the session scratchpad).
- Meta Graph API v21.0: [Meta changelog v21.0](https://developers.facebook.com/docs/graph-api/changelog/version21.0/), [ppc.land: v21.0 release and deprecation date](https://ppc.land/meta-releases-graph-api-v21-0-and-marketing-api-v21-0/), [DEV: v20 expiry and auto-upgrade behaviour](https://dev.to/flarecanary/metas-graph-api-v20-expires-september-24-your-calls-wont-fail-theyll-quietly-start-running-v21-2m2a)
- Next.js advisories: GHSA-6gpp-xcg3-4w24 (proxy bypass), GHSA-2xp9-vwfh-vxw4 and GHSA-vcvr-r3jv-pc5j (RCE), via `npm audit`
