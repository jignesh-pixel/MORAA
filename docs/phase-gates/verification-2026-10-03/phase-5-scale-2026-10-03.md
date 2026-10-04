# Phase 5 scale gate report

- Result: **NOT LOCKED (a step failed)**
- Date: 2026-10-03 12:17 India Standard Time
- Commit: `dd167fa` on `phase-5-scale`
- Machine: Windows 11, Python 3.12.10
- A phase is locked only when this report says LOCKED **and** CI on GitHub is green for the same commit.

| Step | Status | Time | Result |
|---|---|---|---|
| lint | PASS | 0s | ruff check . (E9,F63,F7,F82) |
| tests_sqlite | PASS | 196s | 1039 passed, 21 skipped, 282 warnings in 193.24s (0:03:13) |
| tests_postgres | PASS | 522s | 1060 passed, 172 warnings in 409.95s (0:06:49) |
| prompt_snapshot | PASS | 1s | prompts: 60 / changed: 0 / missing: 0 / added: 0  (image prompts byte-identical to the approved snapshot) |
| dependency_audit | PASS | 6s | No known vulnerabilities found, 49 ignored  (baseline of 27 accepted advisories) |
| load_harness | FAIL | 129s | a=PASS b=PASS c=PASS d=PASS e=PASS f=PASS g=PASS h=FAIL i=PASS k=PASS  WORSE: h: FAIL (locked at PASS) |
| live_probe | PASS | 4s | 18/18 probes as expected |
| boot_probe | PASS | 20s | 5/5 startup rules held |
| live_readonly | SKIP | 0s | not requested (add --live-readonly) |
| pooler_staging | SKIP | 0s | not requested (add --pooler-staging with STAGING_DATABASE_URL set) |

## Output tails

### lint (PASS)

```
All checks passed!
warning: Invalid `# noqa` directive on app\services\earring_close_up_ears_prompt.py:15: expected a comma-separated list of codes (e.g., `# noqa: F401, F841`).
```

### tests_sqlite (PASS)

```

tests/test_message_dedupe.py::WebhookDuplicateTests::test_a_failed_message_can_be_handled_when_meta_retries_it
tests/test_message_dedupe.py::WebhookDuplicateTests::test_a_failure_later_in_the_payload_does_not_repeat_earlier_messages
  C:\Users\justr\AppData\Local\Temp\claude\C--Users-justr-OneDrive-Documents-Studioo-Backend\7198a6f1-f838-4ee5-9dd9-5b40db588a7f\scratchpad\wt\phase-5-scale\backend\app\middleware\error_handler.py:69: DeprecationWarning: datetime.datetime.utcnow() is deprecated and scheduled for removal in a future version. Use timezone-aware objects to represent datetimes in UTC: datetime.datetime.now(datetime.UTC).
    "timestamp": datetime.utcnow().isoformat(),

tests/test_production_safety_fixes.py::PublicHostGuardTests::test_localhost_unaffected
  C:\Users\justr\AppData\Local\Temp\claude\C--Users-justr-OneDrive-Documents-Studioo-Backend\7198a6f1-f838-4ee5-9dd9-5b40db588a7f\scratchpad\wt\phase-5-scale\backend\app\middleware\error_handler.py:54: DeprecationWarning: datetime.datetime.utcnow() is deprecated and scheduled for removal in a future version. Use timezone-aware objects to represent datetimes in UTC: datetime.datetime.now(datetime.UTC).
    "timestamp": datetime.utcnow().isoformat(),

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
1039 passed, 21 skipped, 282 warnings in 193.24s (0:03:13)
```

### tests_postgres (PASS)

```
error:
Command '['C:\\Users\\justr\\OneDrive\\Documents\\Studioo\\Backend\\Moraa Gemvision\\backend\\venv-local\\Lib\\site-packages\\pgserver\\pginstall\\bin\\pg_ctl.exe', '-D', 'C:\\Users\\justr\\AppData\\Local\\Temp\\moraa_pg_z1tlwt28\\data', '-w', 'stop']' returned non-zero exit status 1.
stdout:
waiting for server to shut down............................................................... failed

---
stderr:
pg_ctl: server does not shut down

---

Failed to stop server, killing it instead.
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

### load_harness (FAIL)

```
[h] FAIL in 34.7s
[k] PASS
[i] PASS
results -> C:\Users\justr\AppData\Local\Temp\claude\C--Users-justr-OneDrive-Documents-Studioo-Backend\7198a6f1-f838-4ee5-9dd9-5b40db588a7f\scratchpad\wt\phase-5-scale\backend\tests\load_scenarios\last_results.json
ALLOW_UNSIGNED_WEBHOOKS is on: webhooks without a signature are accepted when their secret is unset. Development only.
Working directory is C:\Users\justr\AppData\Local\Temp\moraa_load_97wngm4f, not C:\Users\justr\AppData\Local\Temp\claude\C--Users-justr-OneDrive-Documents-Studioo-Backend\7198a6f1-f838-4ee5-9dd9-5b40db588a7f\scratchpad\wt\phase-5-scale\backend. Uploads and reports use paths relative to the working directory (C:\Users\justr\AppData\Local\Temp\moraa_load_97wngm4f\uploads, C:\Users\justr\AppData\Local\Temp\moraa_load_97wngm4f\reports); start the server from backend/ so stored file paths keep resolving.
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
PASS development + fresh SQLite migrates itself              -> {'started': True, 'revision': '0017_scheduler_leases', 'head': '0017_scheduler_leases'}
PASS development + same SQLite, second start                 -> {'started': True, 'revision': '0017_scheduler_leases', 'head': '0017_scheduler_leases'}
PASS production + EMPTY PostgreSQL is refused                -> {'started': False, 'error': 'RuntimeError: Database schema is not up to date: database revision none, code expects 0017_scheduler_leases. Run `alembic upgrade head` and restart.'}
PASS alembic upgrade head (command line) succeeds            -> rc=0
PASS production + migrated PostgreSQL starts                 -> {'started': True, 'revision': '0017_scheduler_leases', 'head': '0017_scheduler_leases'}

5/5 startup rules held
```

### live_readonly (SKIP)

```
(no output)
```

### pooler_staging (SKIP)

```
(no output)
```
