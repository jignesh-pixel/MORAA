#!/usr/bin/env python3
"""Phase gate: one command that decides whether a phase is really done.

Runs every verification this project has, records the result in a dated report, and exits non-zero if
anything failed. A phase is "locked" only when this passes AND CI on GitHub is green.

  python scripts/phase_gate.py --phase "Phase 1"                 # everything that needs no secrets
  python scripts/phase_gate.py --phase "Phase 1" --live-readonly # also the READ-ONLY check of the live database
  python scripts/phase_gate.py --skip load_harness --skip live_probe

Steps (in order): lint, tests_sqlite, tests_postgres, prompt_snapshot, dependency_audit, load_harness,
live_probe, boot_probe, live_readonly (--live-readonly), pooler_staging (--pooler-staging).
The report is written to docs/phase-gates/<phase>-<date>.md next to the repository root.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
PY = sys.executable
RANK = {"FAIL": 0, "WARN": 1, "PASS": 2}


@dataclass
class Result:
    name: str
    status: str                      # PASS | FAIL | SKIP
    detail: str = ""
    seconds: float = 0.0
    output_tail: str = ""
    notes: List[str] = field(default_factory=list)


def run(cmd: List[str], env: Optional[dict] = None, timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=BACKEND, env=env or os.environ, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def tail(proc: subprocess.CompletedProcess, lines: int = 25) -> str:
    return "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-lines:])


def summary_line(text: str) -> str:
    matches = re.findall(r"^=*\s*(\d+ (?:passed|failed|skipped|error)[^=]*?)\s*(?:in [\d.]+s.*)?=*$", text, re.M)
    last = [ln for ln in text.splitlines() if re.search(r"\d+ passed", ln)]
    return last[-1].strip(" =") if last else (matches[-1] if matches else "")


# ----------------------------------------------------------------------------- steps
def step_lint() -> Result:
    p = run([PY, "-m", "ruff", "check", "."])
    return Result("lint", "PASS" if p.returncode == 0 else "FAIL", "ruff check . (E9,F63,F7,F82)", output_tail=tail(p, 8))


def _pytest(mode: str) -> Result:
    env = dict(os.environ)
    env.pop("MORAA_TEST_DB", None)            # an exported value must never turn the SQLite run into a PostgreSQL run
    if mode == "postgres":
        env["MORAA_TEST_DB"] = "postgres"
    p = run([PY, "-m", "pytest", "-q", "-rs", "-rf", "-p", "no:cacheprovider"], env=env)
    text = p.stdout + p.stderr
    line = summary_line(text)
    ok = p.returncode == 0
    # Always name the failing tests in the report: an intermittent failure must be traceable afterwards.
    notes = [ln.strip() for ln in text.splitlines() if ln.startswith(("FAILED ", "ERROR "))][:40]
    if mode == "postgres" and "PostgreSQL-only test" in text:
        ok = False
        notes.append("a PostgreSQL-only test was skipped in the PostgreSQL run")
    return Result(f"tests_{mode}", "PASS" if ok else "FAIL", line or "no summary line", output_tail=tail(p, 12), notes=notes)


def step_tests_sqlite() -> Result:
    return _pytest("sqlite")


def step_tests_postgres() -> Result:
    return _pytest("postgres")


def step_prompt_snapshot() -> Result:
    p = run([PY, "scripts/prompt_snapshot.py"], env=dict(os.environ, MORAA_ENV_FILE=""))
    line = [ln for ln in p.stdout.splitlines() if ln.startswith("prompts:")]
    return Result("prompt_snapshot", "PASS" if p.returncode == 0 else "FAIL",
                  (line[-1] if line else "no output") + "  (image prompts byte-identical to the approved snapshot)",
                  output_tail=tail(p, 10))


def step_dependency_audit() -> Result:
    ignore_file = BACKEND / "pip-audit-ignore.txt"
    ids = []
    for raw in ignore_file.read_text(encoding="utf-8").splitlines():
        raw = raw.split("#", 1)[0].strip()
        if raw:
            ids.append(raw)
    cmd = [PY, "-m", "pip_audit", "-r", "constraints.txt", "--no-deps", "--disable-pip", "--strict"]
    for vuln in ids:
        cmd += ["--ignore-vuln", vuln]
    p = run(cmd, timeout=600)
    text = p.stdout + p.stderr
    if p.returncode != 0 and re.search(r"(connection|resolve|timed out|network|HTTPSConnectionPool)", text, re.I):
        return Result("dependency_audit", "SKIP", "advisory service unreachable (network)", output_tail=tail(p, 6))
    last = [ln for ln in text.splitlines() if "vulnerabilit" in ln.lower()]
    return Result("dependency_audit", "PASS" if p.returncode == 0 else "FAIL",
                  f"{last[-1] if last else 'no summary'}  (baseline of {len(ids)} accepted advisories)", output_tail=tail(p, 10))


def step_load_harness() -> Result:
    results_file = BACKEND / "tests" / "load_scenarios" / "last_results.json"
    expected = json.loads((BACKEND / "tests" / "load_scenarios" / "expected_verdicts.json").read_text(encoding="utf-8"))
    backup = results_file.read_bytes() if results_file.exists() else None
    results_file.unlink(missing_ok=True)              # a stale file must never be mistaken for a fresh run
    try:
        p = run([PY, "tests/load_scenarios/run_load_scenarios.py"], timeout=1800)
        if not results_file.exists():
            return Result("load_harness", "FAIL", f"harness produced no results (exit code {p.returncode})", output_tail=tail(p, 12))
        data = json.loads(results_file.read_text(encoding="utf-8"))
    finally:
        if backup is not None:
            results_file.write_bytes(backup)          # the harness overwrites this tracked file; keep the repo clean
        else:
            results_file.unlink(missing_ok=True)
    verdicts = {k: (data.get(k) or {}).get("verdict", "MISSING") for k in expected if not k.startswith("_")}
    worse = [f"{k}: {verdicts[k]} (locked at {expected[k]})" for k in verdicts if RANK.get(verdicts[k], -1) < RANK[expected[k]]]
    better = [f"{k}: {verdicts[k]} (locked at {expected[k]})" for k in verdicts if RANK.get(verdicts[k], -1) > RANK[expected[k]]]
    detail = " ".join(f"{k}={v}" for k, v in sorted(verdicts.items()))
    notes = [f"IMPROVED, tighten expected_verdicts.json: {b}" for b in better]
    return Result("load_harness", "FAIL" if worse else "PASS", detail + ("  WORSE: " + "; ".join(worse) if worse else ""),
                  output_tail=tail(p, 6), notes=notes)


def _script_step(name: str, script: str, timeout: int = 900) -> Result:
    p = run([PY, f"scripts/{script}"], timeout=timeout)
    text = p.stdout + p.stderr
    skips = [ln for ln in text.splitlines() if ln.startswith("SKIP")]
    last = [ln for ln in text.splitlines() if re.search(r"\d+/\d+", ln)]
    return Result(name, "PASS" if p.returncode == 0 else "FAIL", last[-1].strip() if last else f"exit code {p.returncode}",
                  output_tail=tail(p, 30), notes=skips)


def step_live_probe() -> Result:
    return _script_step("live_probe", "live_probe.py")


def step_boot_probe() -> Result:
    return _script_step("boot_probe", "boot_probe.py", timeout=1200)


def step_live_readonly() -> Result:
    p = run([PY, "scripts/supabase_readonly_check.py"], timeout=300)
    text = p.stdout
    last = [ln for ln in text.splitlines() if ln.startswith("RESULT:")]
    return Result("live_readonly", "PASS" if p.returncode == 0 else "FAIL", last[-1] if last else "no result",
                  output_tail=tail(p, 25))


def step_pooler_staging() -> Result:
    if not os.environ.get("STAGING_DATABASE_URL"):
        return Result("pooler_staging", "FAIL", "--pooler-staging was requested but STAGING_DATABASE_URL is not set")
    p = run([PY, "scripts/pooler_write_check.py"], timeout=900)
    last = [ln for ln in p.stdout.splitlines() if ln.startswith("RESULT:")]
    return Result("pooler_staging", "PASS" if p.returncode == 0 else "FAIL", last[-1] if last else f"exit code {p.returncode}",
                  output_tail=tail(p, 25))


# A phase can only be LOCKED when every one of these ran and passed; skipping one makes the report "incomplete".
REQUIRED = {"lint", "tests_sqlite", "tests_postgres", "prompt_snapshot", "dependency_audit", "load_harness",
            "live_probe", "boot_probe"}


def verdict(results: List["Result"]) -> str:
    """The single source of truth for the report label and the exit code."""
    if any(r.status == "FAIL" for r in results):
        return "NOT LOCKED (a step failed)"
    if any(r.name in REQUIRED and r.status != "PASS" for r in results):
        return "NOT LOCKED (a required step was skipped)"
    if not any(r.status == "PASS" for r in results):
        return "NOT LOCKED (nothing ran)"
    return "LOCKED (all steps passed)"


STEPS: List[tuple[str, Callable[[], Result]]] = [
    ("lint", step_lint),
    ("tests_sqlite", step_tests_sqlite),
    ("tests_postgres", step_tests_postgres),
    ("prompt_snapshot", step_prompt_snapshot),
    ("dependency_audit", step_dependency_audit),
    ("load_harness", step_load_harness),
    ("live_probe", step_live_probe),
    ("boot_probe", step_boot_probe),
    ("live_readonly", step_live_readonly),
    ("pooler_staging", step_pooler_staging),
]


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True).stdout.strip()
    except OSError:
        return "?"


def write_report(phase: str, results: List[Result], out: Path) -> None:
    now = dt.datetime.now().astimezone()
    overall = verdict(results)
    lines = [
        f"# {phase} gate report",
        "",
        f"- Result: **{overall}**",
        f"- Date: {now.strftime('%Y-%m-%d %H:%M %Z')}",
        f"- Commit: `{git('rev-parse', '--short', 'HEAD')}` on `{git('branch', '--show-current')}`"
        + ("  (UNCOMMITTED or untracked backend files: the verified code is not all in this commit)"
           if git("status", "--porcelain", "--untracked-files=all", "--", "backend") else ""),
        f"- Machine: {platform.system()} {platform.release()}, Python {platform.python_version()}",
        "- A phase is locked only when this report says LOCKED **and** CI on GitHub is green for the same commit.",
        "",
        "| Step | Status | Time | Result |",
        "|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r.name} | {r.status} | {r.seconds:.0f}s | {r.detail.replace('|', '/')} |")
    for r in results:
        if r.notes:
            lines += ["", f"**{r.name} notes:**"] + [f"- {n}" for n in r.notes]
    lines += ["", "## Output tails", ""]
    for r in results:
        lines += [f"### {r.name} ({r.status})", "", "```", r.output_tail or "(no output)", "```", ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase", default="Phase")
    parser.add_argument("--skip", action="append", default=[], help="step name to skip (repeatable)")
    parser.add_argument("--live-readonly", action="store_true", help="also run the read-only check of the live database")
    parser.add_argument("--pooler-staging", action="store_true",
                        help="also run the write-path pooler check on STAGING (needs STAGING_DATABASE_URL)")
    parser.add_argument("--out", help="report path (default docs/phase-gates/<phase>-<date>.md)")
    args = parser.parse_args()

    names = [n for n, _ in STEPS]
    for s in args.skip:
        if s not in names:
            parser.error(f"unknown step {s!r}; steps: {', '.join(names)}")

    results: List[Result] = []
    for name, fn in STEPS:
        if name in args.skip:
            results.append(Result(name, "SKIP", "skipped by --skip"))
        elif name == "live_readonly" and not args.live_readonly:
            results.append(Result(name, "SKIP", "not requested (add --live-readonly)"))
        elif name == "pooler_staging" and not args.pooler_staging:
            results.append(Result(name, "SKIP", "not requested (add --pooler-staging with STAGING_DATABASE_URL set)"))
        else:
            print(f"==> {name} ...", flush=True)
            started = time.monotonic()
            try:
                result = fn()
            except Exception as exc:                      # a crashing step is a failing step, never a skipped one
                result = Result(name, "FAIL", f"{type(exc).__name__}: {exc}")
            result.seconds = time.monotonic() - started
            results.append(result)
            print(f"    {result.status}: {result.detail}", flush=True)

    slug = re.sub(r"[^a-z0-9]+", "-", args.phase.lower()).strip("-")
    out = Path(args.out) if args.out else REPO / "docs" / "phase-gates" / f"{slug}-{dt.date.today().isoformat()}.md"
    write_report(args.phase, results, out)
    label = verdict(results)
    print()
    print(f"report: {out}")
    print("GATE:", label)
    return 0 if label.startswith("LOCKED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
