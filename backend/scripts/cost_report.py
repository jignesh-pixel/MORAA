#!/usr/bin/env python3
"""Show the last days of AI image provider calls and their estimated cost (COST-4).

Usage (from backend/):   python scripts/cost_report.py [--days 7]

Reads the ``provider_calls`` table (needs migration 0019). The cost is an ESTIMATE: calls multiplied by the price per
call set in COST_PER_CALL_GEMINI_RUPEES / COST_PER_CALL_OPENAI_RUPEES (0 until you set them). Read-only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)

    from app.database import SessionLocal
    from app.services import provider_call_log

    try:
        with SessionLocal() as db:
            rows = provider_call_log.days_summary(db, args.days)
    except Exception as e:  # noqa: BLE001
        print(f"Could not read the call log ({type(e).__name__}). Has `alembic upgrade head` been run?")
        return 1
    if not rows:
        print("No provider calls recorded in that period.")
        return 0
    print(f"{'day':<12}{'provider':<10}{'ok':>6}{'failed':>8}{'est. Rs':>10}")
    for r in rows:
        print(f"{r['day']:<12}{r['provider']:<10}{r['success']:>6}{r['failure']:>8}{r['rupees']:>10}")
    print(f"Total estimated: Rs {sum(r['rupees'] for r in rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
