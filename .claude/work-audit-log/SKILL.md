---
name: work-audit-log
description: Keep a printable, auditable log of one phase of development work, as a short numbered checklist (domains and tasks with empty checkboxes) plus the reasons, key decisions with the options rejected, and errors faced, each written once for the whole phase instead of repeated on every task, then export it as a PDF so the user can print two copies, have the developer confirm each task, and verify it by hand. Use this whenever Claude is doing multi-step work on a project in Claude Code or chat (building, debugging, refactoring, automating, setting up pipelines or workflows), whenever the user asks for a to-do list, progress report, audit trail, changelog, "what did you do and why", "document this" or "export what we've done", and at the end of any substantial work session, even if the user never says "log" or "audit".
---

# Work Audit Log

## Why this exists

The user cannot read every line of code Claude writes, but still needs to know three things: what was done, how far the project has really got, and why each important decision was made instead of the alternatives. This log is how they check Claude's work.

They print the PDF twice. One copy goes to the developer, who confirms each task is done. The other stays with the user (the business owner), who then checks, task by task, that it was really done and done to a good standard, and writes down the result by hand. So the document serves two readers: a developer who needs to know what to look at, and an owner who is smart and busy, is not reading the code, and will use the page to question your decisions.

## The core idea: explain once per phase, not once per task

One log covers **one phase of development** (one run of this skill: for example "WhatsApp intake", "Payments", "Image pipeline v2"). The reasons, the key decisions and the errors are written **once for the whole phase**. The checklist underneath stays short: one line per task plus a line on how to check it. Repeating the same explanation on every task makes the printout long and hard to verify, so don't.

The printed PDF always has these parts, in this order:

1. **Page 1:** project, phase and date, a short "how to use" box, and a summary table (task count per domain; the Done / In progress / Blocked / Not started columns are blank for the user to fill in).
2. **Why this phase:** the reason this phase of work exists, plus an optional short "what was done".
3. **Checklist:** numbered domains (1, 2, 3), tasks under them (1.1, 1.2, 2.1...). Each task starts with an empty checkbox, has an empty box at the right of its heading for the user to write the status by hand, and carries a "how to check" line.
4. **Key decisions and why:** numbered D1, D2... Each has the choice made, why, and the other options considered with the specific reason each lost. Each says which tasks it relates to.
5. **Errors faced:** numbered E1, E2... Each has the error, its root cause, the fix, and the task it came up in.

Every page has a footer with the project name, "Work Audit Log", the date and the page number.

## How it works

- One JSON file per phase is the single source of truth: Claude edits only this file.
- `scripts/build_report.py` turns it into the PDF. The PDF is the only export: do not produce TXT, Markdown or other formats. It is regenerated each time and never edited by hand.
- Full field list and a worked example: `references/schema.md`. Read it before writing the first entry in a new log.

Where the log lives:
- **Claude Code:** `worklog/<phase-slug>.json` in the project root (for example `worklog/payments.json`). The PDF goes in `worklog/` too.
- **Chat:** look for an existing phase log (attached, in the project files, or in the working directory) and continue it. If none exists, create one in the working directory. Always hand the finished PDF to the user at the end, because files in the sandbox are invisible to them until sent.

A new phase of development gets a new file, so each printout stays about one phase long. If the user is continuing the same phase, continue the same file.

## Workflow

1. **Start.** Look for a log for this phase. If there is one, load it and continue. If not, create a new file and write the phase `why` first: the problem or goal that makes this phase necessary, and what is wrong or missing without it.
2. **Plan up front.** Add each planned task with `status: "todo"` and a title. Group tasks under domains (e.g. "WhatsApp intake", "Payments", "Database", "Deployment"). Reuse a domain when the work fits it.
3. **Log as you go, not afterwards.**
   - Set `in_progress` when you start a task.
   - Add a **decision** at the moment you choose between real options. Reconstructing it later produces tidy fiction.
   - Add an **error** at the moment it happens, while the exact message and cause are fresh.
4. **Close out.** Set the final statuses, add a `verify` line to each finished task, then run the builder:
   ```
   python3 <skill-dir>/scripts/build_report.py worklog/<phase-slug>.json
   ```
   `<skill-dir>` is the "Base directory for this skill" shown when this skill loads (in Claude Code, `~/.claude/skills/work-audit-log` or `.claude/skills/work-audit-log`). Use `python` or `py -3` if `python3` does not exist (common on Windows). Use `--check` to validate without building. Fix any ERROR it prints and re-run. On a machine where the builder has not run before, see "First run on a machine" below.
5. **Tell the user** in one or two lines: how many tasks are done, in progress or blocked, and where the PDF is. Do not paste the report into the chat.

Rebuild the report at the end of every work session, and whenever the user asks for status.

## First run on a machine

The PDF needs the Python package `reportlab`. Before the first build on a machine (typical in Claude Code on the user's own computer), run:

```
python3 <skill-dir>/scripts/build_report.py --doctor
```

It prints the Python version, whether `reportlab` is installed, and which font will be used. If `reportlab` is missing:

1. Run `python3 -m pip install --user reportlab`.
2. If pip refuses with "externally-managed-environment" (common on Ubuntu and WSL), re-run it with `--break-system-packages` (still a user-level install), or create a virtual environment and run the builder with that environment's Python.

Never let a missing package stop the work. Keep updating the log file as normal, and tell the user the PDF will be built as soon as `reportlab` is installed. If the doctor says no rupee-capable font was found, the PDF still builds and prints "Rs." instead of the rupee sign; mention it in one line and carry on.

## Quality bar

These rules are what make the printout useful rather than decorative.

- **Phase `why`:** reason from first principles. What problem or constraint forced this phase, and what breaks if it isn't done? A few sentences, not a page.
- **Task `title`:** say what will be true when it's finished ("Reject blurry photos before spending a generation credit"), not an activity ("Work on image check").
- **Task `verify`:** one or two lines a non-programmer can follow in a couple of minutes ("Send a photo to the test number; expect 8 images back within 2 minutes"). Cover two things: proof that it works, and what *good quality* looks like, because the owner is checking both ("the white-background image has no shadow or colour cast; edges are clean"). If you couldn't test something, say what is untested.
- **Decisions:** log only choices where a reasonable developer could have gone another way and the user may want to question it (a tool, a library, a data structure, a cost trade-off, something dropped). Do not log routine steps. Name the real alternatives and the specific reason each lost (cost, latency, complexity, failure risk, fit with the stack). If there truly was one viable path, say so in `why` and explain what ruled the others out. Never invent weak alternatives to fill the field. Say which tasks the decision relates to, so the owner can find the code that follows from it.
- **Errors:** log errors that cost real time, changed the approach, or reveal a fragile spot. Give the key line of the error message (verbatim where possible), the root cause rather than only the symptom, and the fix. Say so when the fix is a workaround. Skip typos fixed in seconds.
- **Length:** aim for a phase to print in roughly two to five pages. If the log is growing past that, you are probably repeating yourself in tasks or logging routine steps as decisions.
- **Status honesty:** the `status` field is your own bookkeeping and is not printed (the user fills status in by hand), but it still has to be truthful because it drives your chat summary and the validation. `done` means you ran it and saw it work. Code that is written but unrun is `in_progress`, and `verify` says what remains unchecked. Use `blocked` plus `blocked_by` when waiting on the user or an outside service.

**Per-task detail is the exception.** A task can carry its own `why`, `solution`, `alternatives` or `errors`, and they will print under it. Use that only when one task is complex enough that explaining it inside the phase-level sections would be confusing. Normally leave these out.

## Rules that keep the printouts trustworthy

- **Append-only.** Task numbers (1.1), decision numbers (D1) and error numbers (E1) come from order, and the user will refer to them on paper ("task 2.3 looks wrong", "D2 is not what I wanted"). Never reorder, renumber or delete entries. If a task is abandoned, set `status: "dropped"` (it prints a "DROPPED" note) and record the reason as a decision.
- **The boxes belong to the user.** The task checkbox and the status box are always printed empty, and nothing on the page pre-fills a status. Never add a status label, "done" mark or count that the user is meant to write themselves, and never present a task as verified because you marked it done.
- **Record failures plainly.** A log that only shows success is worthless for auditing. If something was tried and rolled back, record it as a decision (what was tried, why it was abandoned) or an error.
- **No secrets.** Never write API keys, tokens or passwords into the log. Name the variable instead (`WHATSAPP_TOKEN`), since the PDF gets printed and shared.
