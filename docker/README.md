# Moraa GemVision: Docker (DEP-1)

Written 2026-10-02. **Not yet run on a real server**: nobody has built these files on a machine with Docker, so the first
`docker compose up` is the real test. Expect to fix small things.

## What is here

| File | What it does |
|---|---|
| `../backend/Dockerfile` | Builds the backend image (Python 3.12, runs as a non-root user, health check, graceful shutdown). |
| `docker-compose.yml` | `migrate` (database upgrade, runs once) -> `api` (the web server) behind `caddy` (automatic HTTPS). |
| `Caddyfile` | HTTPS reverse proxy with a 12 MB body limit. |
| `.env.example` | The settings the stack needs. Copy to `.env` and fill in. Never commit `.env`. |

## First start

1. Point a DNS name at the server and put it in `.env` as `PUBLIC_HOSTNAME`.
2. `cd docker && cp .env.example .env`, fill it in (database address, secrets, provider keys).
3. Take a database backup (`docs/OPERATIONS_RUNBOOK.md`, section 3).
4. `docker compose up -d --build`. The `migrate` step upgrades the database and refuses to continue if the result is wrong.
5. Check `https://<your name>/api/meta/webhook/health` and the logs (`docker compose logs -f api`).

## Things to know

- Only the two provider webhooks are reachable from the internet; the dashboard and admin pages are not served through
  this proxy on purpose (the application's public-host guard). The dashboard needs its own private route.
- On a deploy the old container stops taking new work and has up to 150 s to finish orders already running; anything
  unfinished is refunded by the recovery sweep.
- Customer photos live in the `moraa-data` volume. That ties the app to one server; moving them to object storage
  (S3 / R2 / Supabase Storage) is a later step and needs an owner decision (`docs/QUESTIONS_FOR_MORNING.md`).
- Redis, Celery workers and a separate generation queue are NOT in this file yet: generation still runs inside the api
  process (made safe for several processes by the database leases and the outbox).
- The frontend (Next.js) is not part of this stack yet.
