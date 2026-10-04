# Phase 4 operations gate report

- Result: **NOT LOCKED (a step failed)**
- Date: 2026-10-02 21:44 India Standard Time
- Commit: `37f7db4` on `phase-4-operations`  (UNCOMMITTED or untracked backend files: the verified code is not all in this commit)
- Machine: Windows 11, Python 3.12.10
- A phase is locked only when this report says LOCKED **and** CI on GitHub is green for the same commit.

| Step | Status | Time | Result |
|---|---|---|---|
| lint | PASS | 0s | ruff check . (E9,F63,F7,F82) |
| tests_sqlite | PASS | 95s | 968 passed, 21 skipped, 168 warnings in 92.81s (0:01:32) |
| tests_postgres | PASS | 351s | 989 passed, 85 warnings in 288.77s (0:04:48) |
| prompt_snapshot | PASS | 1s | prompts: 60 / changed: 0 / missing: 0 / added: 0  (image prompts byte-identical to the approved snapshot) |
| dependency_audit | PASS | 9s | No known vulnerabilities found, 49 ignored  (baseline of 27 accepted advisories) |
| load_harness | PASS | 104s | a=PASS b=PASS c=PASS d=PASS e=PASS f=PASS g=PASS h=PASS i=PASS k=PASS |
| live_probe | PASS | 4s | 18/18 probes as expected |
| boot_probe | PASS | 22s | 5/5 startup rules held |
| live_readonly | FAIL | 3s | RESULT: 2 CHECK(S) FAILED: ['database up to date', 'ensure_schema_ready() (the startup guard)'] |
| pooler_staging | SKIP | 0s | not requested (add --pooler-staging with STAGING_DATABASE_URL set) |

## Output tails

### lint (PASS)

```
All checks passed!
```

### tests_sqlite (PASS)

```
tests/test_migrations.py: 3 warnings
tests/test_operations.py: 4 warnings
tests/test_shared_spend_counter.py: 78 warnings
  C:\Users\justr\OneDrive\Documents\Studioo\Backend\Moraa Gemvision\backend\venv-local\Lib\site-packages\sqlalchemy\engine\default.py:941: DeprecationWarning: The default datetime adapter is deprecated as of Python 3.12; see the sqlite3 documentation for suggested replacement recipes
    cursor.execute(statement, parameters)

tests/test_production_safety_fixes.py::PublicHostGuardTests::test_localhost_unaffected
  C:\Users\justr\OneDrive\Documents\Studioo\Backend\Moraa Gemvision\backend\app\middleware\error_handler.py:54: DeprecationWarning: datetime.datetime.utcnow() is deprecated and scheduled for removal in a future version. Use timezone-aware objects to represent datetimes in UTC: datetime.datetime.now(datetime.UTC).
    "timestamp": datetime.utcnow().isoformat(),

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
968 passed, 21 skipped, 168 warnings in 92.81s (0:01:32)
```

### tests_postgres (PASS)

```
    "timestamp": datetime.utcnow().isoformat(),

tests/test_migrations.py::EnsureSchemaReadyTests::test_development_sqlite_migrates_a_legacy_create_all_database_in_place
  C:\Users\justr\OneDrive\Documents\Studioo\Backend\Moraa Gemvision\backend\venv-local\Lib\site-packages\sqlalchemy\engine\default.py:941: DeprecationWarning: The default datetime adapter is deprecated as of Python 3.12; see the sqlite3 documentation for suggested replacement recipes
    cursor.execute(statement, parameters)

tests/test_production_safety_fixes.py::PublicHostGuardTests::test_localhost_unaffected
  C:\Users\justr\OneDrive\Documents\Studioo\Backend\Moraa Gemvision\backend\app\middleware\error_handler.py:54: DeprecationWarning: datetime.datetime.utcnow() is deprecated and scheduled for removal in a future version. Use timezone-aware objects to represent datetimes in UTC: datetime.datetime.now(datetime.UTC).
    "timestamp": datetime.utcnow().isoformat(),

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
989 passed, 85 warnings in 288.77s (0:04:48)
```

### prompt_snapshot (PASS)

```
prompts: 60 | changed: 0 | missing: 0 | added: 0
SECRET_KEY is not set � generated a random per-process key. Set SECRET_KEY in the environment for production so JWTs remain valid across restarts and multiple workers.
```

### dependency_audit (PASS)

```
WARNING:pip_audit._cli:--no-deps is supported, but users are encouraged to fully hash their pinned dependencies
WARNING:pip_audit._cli:Consider using a tool like `pip-compile`: https://pip-tools.readthedocs.io/en/latest/#using-hashes
No known vulnerabilities found, 49 ignored
```

### load_harness (PASS)

```
[h] PASS in 13.9s
[k] PASS
[i] PASS
results -> C:\Users\justr\OneDrive\Documents\Studioo\Backend\Moraa Gemvision\backend\tests\load_scenarios\last_results.json
ALLOW_UNSIGNED_WEBHOOKS is on: webhooks without a signature are accepted when their secret is unset. Development only.
Working directory is C:\Users\justr\AppData\Local\Temp\moraa_load_u68cp55o, not C:\Users\justr\OneDrive\Documents\Studioo\Backend\Moraa Gemvision\backend. Uploads and reports use paths relative to the working directory (C:\Users\justr\AppData\Local\Temp\moraa_load_u68cp55o\uploads, C:\Users\justr\AppData\Local\Temp\moraa_load_u68cp55o\reports); start the server from backend/ so stored file paths keep resolving.
```

### live_probe (PASS)

```
PASS local browser, /api/history                                    -> HTTP/1.1 200 OK  (expected 200)
PASS remote peer, Host: localhost, /api/history                     -> HTTP/1.1 404 Not Found  (expected 404)
PASS remote peer, Host: localhost, /docs                            -> HTTP/1.1 404 Not Found  (expected 404)
PASS remote peer, Host path-injection '#'                           -> HTTP/1.1 400 Bad Request  (expected 400)
PASS remote peer, Host path-injection '?'                           -> HTTP/1.1 400 Bad Request  (expected 400)
PASS tunnel style (loopback + X-Forwarded-For)                      -> HTTP/1.1 404 Not Found  (expected 404)
PASS empty X-Forwarded-For header                                   -> HTTP/1.1 404 Not Found  (expected 404)
PASS X-Real-IP header                                               -> HTTP/1.1 404 Not Found  (expected 404)
PASS webhook GET with a bad verify token                            -> HTTP/1.1 403 Forbidden  (expected 403)
PASS webhook POST, unsigned, no secret configured                   -> HTTP/1.1 200 OK  (expected 200)
PASS razorpay POST, unsigned, no secret configured                  -> HTTP/1.1 400 Bad Request  (expected 400)
PASS '//api' path trick                                             -> HTTP/1.1 404 Not Found  (expected 404)
PASS /uploads from a remote host name                               -> HTTP/1.1 404 Not Found  (expected 404)
PASS chunked 3.9 MB body to a public webhook                        -> HTTP/1.1 413 Request Entity Too Large  (expected 413)
PASS static file, local, Range request                              -> HTTP/1.1 206 Partial Content  (expected 206)
PASS static file, local, plain                                      -> HTTP/1.1 200 OK  (expected 200)
PASS static path traversal                                          -> HTTP/1.1 404 Not Found  (expected 404)
PASS local CORS preflight from the dashboard origin                 -> HTTP/1.1 200 OK  (expected 200)

18/18 probes as expected
```

### boot_probe (PASS)

```
PASS development + fresh SQLite migrates itself              -> {'started': True, 'revision': '0015_auth_hardening', 'head': '0015_auth_hardening'}
PASS development + same SQLite, second start                 -> {'started': True, 'revision': '0015_auth_hardening', 'head': '0015_auth_hardening'}
PASS production + EMPTY PostgreSQL is refused                -> {'started': False, 'error': 'RuntimeError: Database schema is not up to date: database revision none, code expects 0015_auth_hardening. Run `alembic upgrade head` and restart.'}
PASS alembic upgrade head (command line) succeeds            -> rc=0
PASS production + migrated PostgreSQL starts                 -> {'started': True, 'revision': '0015_auth_hardening', 'head': '0015_auth_hardening'}

5/5 startup rules held
```

### live_readonly (FAIL)

```
[OK  ] env file loaded: yes
[OK  ] ENVIRONMENT: production
[OK  ] database dialect: postgresql
[OK  ] database port: 6543
[OK  ] transaction_read_only: on
[OK  ] server_version: 17.6
[OK  ] current_schema: public
[OK  ] search_path: "\$user", public, extensions
[OK  ] 6 separate read-only transactions: ok
[OK  ] connection path: Supabase transaction-mode pooler port (6543)
[OK  ] schema revision in database: 0008_customer_access_tiers
[OK  ] schema head expected by code: 0015_auth_hardening
[OK  ] revision known to this build: True
[FAIL] database up to date: False
[OK  ] uq_audit_logs_money_once present in current schema: True
[FAIL] ensure_schema_ready() (the startup guard): RuntimeError: Database schema is not up to date: database revision 0008_customer_access_tiers, code expects 0015_auth_hardening. Run `alembic upgrade head` and restart.

RESULT: 2 CHECK(S) FAILED: ['database up to date', 'ensure_schema_ready() (the startup guard)']
RECHARGE_PAYMENT_URL is not set: the built-in static payment link from the source code is used as the fallback. Set RECHARGE_PAYMENT_URL to your own link in the environment.
CELERY_TASK_ALWAYS_EAGER is true in production: analysis tasks run inside the web process. Set it to false and run a real worker with a broker.
```

### pooler_staging (SKIP)

```
(no output)
```
