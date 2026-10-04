#!/usr/bin/env python3
"""Build a printable PDF audit report from a phase log (JSON).

Usage:
    python build_report.py worklog/<phase>.json
    python build_report.py worklog/<phase>.json --out-dir reports
    python build_report.py worklog/<phase>.json --check      # validate only
    python build_report.py --doctor                          # can this machine build the PDF?

One log file = one phase of development. The reasons, key decisions and errors are written ONCE
for the whole phase; the checklist underneath stays short (one line per task plus how to check it).

The JSON file is the single source of truth. The PDF is regenerated from it every time and must
never be edited by hand. Numbers (tasks 1.1, 1.2 ...; decisions D1, D2 ...; errors E1, E2 ...) are
derived from order, so the log is append-only: never reorder or delete entries. The PDF is meant
to be printed: the task checkboxes and the status boxes are left empty on purpose, for the reader
to fill in by hand.
"""
import argparse
import json
import os
import re
import sys
from datetime import date
from xml.sax.saxutils import escape

STATUS_LABEL = {
    "done": "DONE",
    "in_progress": "IN PROGRESS",
    "blocked": "BLOCKED",
    "todo": "NOT STARTED",
    "dropped": "DROPPED",
}

LEGEND = (
    "How to use this page: tick the box [ ] at the start of a task once you have checked that it is "
    "done and done well. Write the status (done, in progress, blocked, not started) by hand in the "
    "empty box at the right of each task, and fill in the summary table below. Nothing on this page "
    "is pre-filled with a status. The reasons, decisions (D1, D2...) and errors (E1, E2...) are "
    "explained once for the whole phase, after the checklist; tasks refer to them by number."
)


# --------------------------------------------------------------------------- load / validate
def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f"ERROR: file not found: {path}")
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: {path} is not valid JSON (line {e.lineno}, col {e.colno}): {e.msg}")


def _is_text(v):
    return isinstance(v, str) and v.strip() != ""


def _items(container, key):
    v = container.get(key)
    return v if isinstance(v, list) else []


def _check_alts(alts, path, errors):
    for i, a in enumerate(alts, 1):
        if not isinstance(a, dict) or not _is_text(a.get("option")) or not _is_text(a.get("why_not")):
            errors.append(f"{path}: alternatives[{i}] needs \"option\" and \"why_not\".")


def validate(data):
    errors, warnings = [], []
    if not isinstance(data, dict):
        return ["Top level must be a JSON object."], warnings
    if not _is_text(data.get("project")):
        errors.append('"project" is missing or empty.')
    if not _is_text(data.get("why")):
        errors.append('"why" is missing: say, once, why this phase of work is being done.')
    if "phase" in data and not _is_text(data.get("phase")):
        errors.append('"phase" must be a non-empty text when present.')

    for key in ("decisions", "errors"):
        if key in data and not isinstance(data[key], list):
            errors.append(f'"{key}" must be a list.')

    for i, d in enumerate(_items(data, "decisions"), 1):
        path = f"decision D{i}"
        if not isinstance(d, dict) or not _is_text(d.get("decision")) or not _is_text(d.get("why")):
            errors.append(f"{path}: needs a \"decision\" and a \"why\".")
            continue
        path = f'decision D{i} ("{d["decision"]}")'
        if "alternatives" in d and not isinstance(d["alternatives"], list):
            errors.append(f"{path}: \"alternatives\" must be a list.")
        else:
            _check_alts(_items(d, "alternatives"), path, errors)
        if not _items(d, "alternatives"):
            warnings.append(f"{path}: no alternatives listed (fine only if there truly was one viable option; say so in \"why\").")

    for i, e in enumerate(_items(data, "errors"), 1):
        path = f"error E{i}"
        if not isinstance(e, dict) or not _is_text(e.get("error")):
            errors.append(f"{path}: needs an \"error\" field.")
            continue
        if not _is_text(e.get("cause")):
            warnings.append(f"{path}: has no \"cause\".")
        if not _is_text(e.get("fix")):
            warnings.append(f"{path}: has no \"fix\".")

    domains = data.get("domains")
    if not isinstance(domains, list) or not domains:
        errors.append('"domains" must be a non-empty list.')
        return errors, warnings

    for di, dom in enumerate(domains, 1):
        dpath = f"domain {di}"
        if not isinstance(dom, dict):
            errors.append(f"{dpath}: must be an object.")
            continue
        if not _is_text(dom.get("name")):
            errors.append(f"{dpath}: missing \"name\".")
        else:
            dpath = f'domain {di} ("{dom["name"]}")'
        tasks = dom.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            errors.append(f"{dpath}: \"tasks\" must be a non-empty list.")
            continue
        for ti, t in enumerate(tasks, 1):
            tpath = f"task {di}.{ti}"
            if not isinstance(t, dict):
                errors.append(f"{tpath}: must be an object.")
                continue
            if not _is_text(t.get("title")):
                errors.append(f"{tpath}: missing \"title\".")
            else:
                tpath = f'task {di}.{ti} ("{t["title"]}")'
            if t.get("status") not in STATUS_LABEL:
                errors.append(
                    f"{tpath}: \"status\" must be one of {', '.join(STATUS_LABEL)} "
                    f"(got {t.get('status')!r})."
                )
            # Optional per-task detail (use sparingly; the phase-level sections are the default home).
            for key in ("errors", "alternatives", "files"):
                if key in t and not isinstance(t[key], list):
                    errors.append(f"{tpath}: \"{key}\" must be a list.")
            for i, e in enumerate(_items(t, "errors"), 1):
                if not isinstance(e, dict) or not _is_text(e.get("error")):
                    errors.append(f"{tpath}: errors[{i}] needs an \"error\" field.")
            _check_alts(_items(t, "alternatives"), tpath, errors)

            if t.get("status") == "done" and not _is_text(t.get("verify")):
                warnings.append(f"{tpath}: marked done but has no \"verify\" (how to check it).")
            if t.get("status") == "blocked" and not _is_text(t.get("blocked_by")):
                warnings.append(f"{tpath}: blocked but has no \"blocked_by\".")
    return errors, warnings


# --------------------------------------------------------------------------- shared helpers
def all_tasks(data):
    return [t for d in data["domains"] for t in d["tasks"]]


def slugify(s):
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "project"


# --------------------------------------------------------------------------- PDF
FONT_SETS = [
    # (regular, bold, italic) - first set whose regular file exists wins.
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"),
    ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
     "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
     "/usr/share/fonts/truetype/noto/NotoSans-Italic.ttf"),
    ("/mnt/c/Windows/Fonts/segoeui.ttf", "/mnt/c/Windows/Fonts/segoeuib.ttf", "/mnt/c/Windows/Fonts/segoeuii.ttf"),
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/segoeuii.ttf"),
    ("/mnt/c/Windows/Fonts/arial.ttf", "/mnt/c/Windows/Fonts/arialbd.ttf", "/mnt/c/Windows/Fonts/ariali.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/ariali.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
     "/System/Library/Fonts/Supplemental/Arial Italic.ttf"),
]


def build_pdf(data, gen_date, out_path):
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (CondPageBreak, Flowable, HRFlowable, Paragraph,
                                        SimpleDocTemplate, Spacer, Table, TableStyle)
    except ImportError:
        return False

    # ---- fonts (a Unicode font is needed for the rupee sign)
    unicode_ok = False
    reg_name, bold_name, ital_name = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"
    for reg, bold, ital in FONT_SETS:
        if os.path.exists(reg):
            pdfmetrics.registerFont(TTFont("WL", reg))
            pdfmetrics.registerFont(TTFont("WL-B", bold if os.path.exists(bold) else reg))
            pdfmetrics.registerFont(TTFont("WL-I", ital if os.path.exists(ital) else reg))
            pdfmetrics.registerFontFamily("WL", normal="WL", bold="WL-B", italic="WL-I", boldItalic="WL-B")
            reg_name, bold_name, ital_name = "WL", "WL-B", "WL-I"
            unicode_ok = True
            break

    def clean(s):
        s = str(s)
        if not unicode_ok:  # built-in Helvetica cannot draw these
            s = s.replace("\u20b9", "Rs.").replace("\u2192", "->").replace("\u2713", "ok")
        return s

    def esc(s):
        return escape(clean(s)).replace("\n", "<br/>")

    ink = colors.HexColor("#111111")
    grey = colors.HexColor("#555555")
    rule = colors.HexColor("#999999")

    base = ParagraphStyle("base", fontName=reg_name, fontSize=9.5, leading=13, textColor=ink)
    title_s = ParagraphStyle("title", parent=base, fontName=bold_name, fontSize=20, leading=24, spaceAfter=2)
    sub_s = ParagraphStyle("sub", parent=base, fontSize=9.5, textColor=grey, spaceAfter=6)
    legend_s = ParagraphStyle("legend", parent=base, fontSize=9, leading=12.5, borderColor=rule,
                              borderWidth=0.8, borderPadding=6, spaceBefore=8, spaceAfter=14)
    sec_s = ParagraphStyle("sec", parent=base, fontName=bold_name, fontSize=15, leading=19, spaceBefore=8)
    dom_s = ParagraphStyle("dom", parent=base, fontName=bold_name, fontSize=12, leading=16, spaceBefore=6)
    goal_s = ParagraphStyle("goal", parent=base, fontName=ital_name, fontSize=9, textColor=grey, spaceAfter=4)
    task_s = ParagraphStyle("task", parent=base, fontName=bold_name, fontSize=10.5, leading=13.5)
    item_head_s = ParagraphStyle("itemhead", parent=base, fontName=bold_name, fontSize=10.5, leading=13.5,
                                 spaceBefore=4, spaceAfter=1, keepWithNext=1)
    cell_s = ParagraphStyle("cell", parent=base, fontSize=9, leading=11)
    cellb_s = ParagraphStyle("cellb", parent=cell_s, fontName=bold_name)

    def mk_styles(tag, indent):
        label = ParagraphStyle(tag + "label", parent=base, fontName=bold_name, fontSize=7.5, leading=9.5,
                               textColor=grey, leftIndent=indent, spaceBefore=4, spaceAfter=1, keepWithNext=1)
        body = ParagraphStyle(tag + "body", parent=base, leftIndent=indent, spaceAfter=2)
        item = ParagraphStyle(tag + "item", parent=base, leftIndent=indent + 4 * mm,
                              firstLineIndent=-3.5 * mm, spaceAfter=3)
        meta = ParagraphStyle(tag + "meta", parent=base, fontSize=8, textColor=grey, leftIndent=indent)
        return label, body, item, meta

    P = mk_styles("p", 0)            # phase-level sections (decisions, errors, why)
    T = mk_styles("t", 9 * mm)       # under a task heading

    class CheckBox(Flowable):
        def __init__(self, size=11):
            super().__init__()
            self.size = size

        def wrap(self, aw, ah):
            return self.size, self.size

        def draw(self):
            self.canv.setStrokeColor(ink)
            self.canv.setLineWidth(1.2)
            self.canv.rect(0, 0, self.size, self.size)

    class WriteBox(Flowable):
        """Empty outlined box where the reader writes the task's status by hand."""

        def __init__(self, width=32 * mm, height=22):
            super().__init__()
            self.width, self.height = width, height

        def wrap(self, aw, ah):
            return self.width, self.height

        def draw(self):
            self.canv.setStrokeColor(ink)
            self.canv.setLineWidth(0.8)
            self.canv.rect(0, 0, self.width, self.height)

    # ---- small builders shared by phase-level and task-level text
    def stacked(label, text, S):
        return [Paragraph(label, S[0]), Paragraph(esc(text), S[1])]

    def inline(label, text, S):
        return Paragraph(f'<font size="7" color="#555555"><b>{label}</b></font>&nbsp;&nbsp;{esc(text)}', S[1])

    def alts_flow(alts, S):
        out = [Paragraph("OTHER OPTIONS CONSIDERED (AND WHY NOT)", S[0])]
        if not alts:
            out.append(Paragraph("None logged.", S[1]))
        for a in alts:
            out.append(Paragraph(
                f"•&nbsp;&nbsp;<b>{esc(a['option'])}</b> — not chosen because: {esc(a['why_not'])}", S[2]))
        return out

    def section(title):
        return [CondPageBreak(50 * mm), Paragraph(esc(title), sec_s),
                HRFlowable(width="100%", thickness=1.6, color=ink, spaceBefore=2, spaceAfter=5)]

    tasks = all_tasks(data)
    phase = (data.get("phase") or "").strip()
    sub = "Work Audit Log"
    if phase:
        sub += f" &nbsp;·&nbsp; {esc(phase)}"
    sub += f" &nbsp;·&nbsp; generated {esc(gen_date)}"
    story = [
        Paragraph(esc(data["project"]), title_s),
        Paragraph(sub, sub_s),
        Paragraph(esc(LEGEND).replace("[ ]", "☐" if unicode_ok else "[ ]"), legend_s),
    ]

    # ---- summary: task counts are facts; the status columns are left blank to hand-fill
    rows = [[Paragraph(h, cellb_s) for h in ("Domain", "Tasks", "Done", "In progress", "Blocked", "Not started")]]
    for di, dom in enumerate(data["domains"], 1):
        rows.append([Paragraph(f"{di}. {esc(dom['name'])}", cell_s), str(len(dom["tasks"])), "", "", "", ""])
    rows.append([Paragraph("Total", cellb_s), str(len(tasks)), "", "", "", ""])
    summary = Table(rows, colWidths=[None, 16 * mm, 20 * mm, 26 * mm, 22 * mm, 25 * mm], repeatRows=1)
    summary.setStyle(TableStyle([
        ("FONTNAME", (1, 0), (-1, -1), reg_name), ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, -1), (-1, -1), bold_name),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E6E6E6")),
        ("GRID", (0, 0), (-1, -1), 0.6, rule),
        ("TOPPADDING", (0, 0), (-1, 0), 4), ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("TOPPADDING", (0, 1), (-1, -1), 8), ("BOTTOMPADDING", (0, 1), (-1, -1), 8),
    ]))
    story += [summary, Spacer(1, 6)]

    # ---- 1. why this phase
    story += section("Why this phase")
    story.append(Paragraph(esc(data["why"]), P[1]))
    if data.get("approach"):
        story += stacked("WHAT WAS DONE, IN SHORT", data["approach"], P)

    # ---- 2. checklist (short: one line per task + how to check it)
    story += section("Checklist")
    for di, dom in enumerate(data["domains"], 1):
        story.append(CondPageBreak(40 * mm))
        story.append(Paragraph(f"{di}. {esc(dom['name'])}", dom_s))
        story.append(HRFlowable(width="100%", thickness=0.8, color=ink, spaceBefore=1, spaceAfter=3))
        if dom.get("goal"):
            story.append(Paragraph(esc(dom["goal"]), goal_s))
        for ti, t in enumerate(dom["tasks"], 1):
            story.append(CondPageBreak(30 * mm))
            head = Table(
                [[CheckBox(), Paragraph(f"{di}.{ti}&nbsp;&nbsp;{esc(t['title'])}", task_s), WriteBox()]],
                colWidths=[9 * mm, None, 32 * mm])
            head.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]))
            story.append(head)
            if t["status"] == "dropped":
                story.append(Paragraph("<b>DROPPED</b> - this task is no longer being done.", T[3]))
            if t.get("date"):
                story.append(Paragraph(f"Logged: {esc(t['date'])}", T[3]))
            if t.get("blocked_by"):
                story.append(inline("BLOCKED BY", t["blocked_by"], T))
            # Optional per-task detail: printed only when present.
            if t.get("why"):
                story += stacked("WHY THIS TASK", t["why"], T)
            if t.get("solution"):
                story += stacked("SOLUTION", t["solution"], T)
            if t.get("why_this"):
                story += stacked("WHY THIS SOLUTION", t["why_this"], T)
            if t.get("alternatives"):
                story += alts_flow(t["alternatives"], T)
            if t.get("errors"):
                story.append(Paragraph("ERRORS FACED", T[0]))
                for i, e in enumerate(t["errors"], 1):
                    parts = [f"<b>{i}. Error:</b> {esc(e['error'])}"]
                    if e.get("cause"):
                        parts.append(f"<b>Cause:</b> {esc(e['cause'])}")
                    if e.get("fix"):
                        parts.append(f"<b>Fix:</b> {esc(e['fix'])}")
                    story.append(Paragraph("<br/>".join(parts), T[2]))
            if t.get("files"):
                story.append(inline("FILES", ", ".join(str(f) for f in t["files"]), T))
            if t.get("verify"):
                story.append(inline("HOW TO CHECK", t["verify"], T))
            story.append(Spacer(1, 3))
            story.append(HRFlowable(width="100%", thickness=0.4, color=rule, spaceBefore=1, spaceAfter=4))

    # ---- 3. key decisions
    story += section("Key decisions and why")
    decisions = data.get("decisions") or []
    if not decisions:
        story.append(Paragraph("None logged.", P[1]))
    for i, d in enumerate(decisions, 1):
        story.append(CondPageBreak(35 * mm))
        story.append(Paragraph(f"D{i}&nbsp;&nbsp;{esc(d['decision'])}", item_head_s))
        story += stacked("WHY THIS CHOICE", d["why"], P)
        story += alts_flow(d.get("alternatives") or [], P)
        if d.get("tasks"):
            story.append(Paragraph(f"Relates to task(s): {esc(d['tasks'])}", P[3]))
        story.append(Spacer(1, 3))
        story.append(HRFlowable(width="100%", thickness=0.4, color=rule, spaceBefore=1, spaceAfter=4))

    # ---- 4. errors faced
    story += section("Errors faced")
    errs = data.get("errors") or []
    if not errs:
        story.append(Paragraph("None logged.", P[1]))
    err_s = ParagraphStyle("err", parent=base, spaceAfter=7)
    for i, e in enumerate(errs, 1):
        parts = [f"<b>E{i}</b>&nbsp;&nbsp;<b>{esc(e['error'])}</b>"]
        if e.get("cause"):
            parts.append(f"<b>Cause:</b> {esc(e['cause'])}")
        if e.get("fix"):
            parts.append(f"<b>Fix:</b> {esc(e['fix'])}")
        if e.get("task"):
            parts.append(f"<b>Task:</b> {esc(e['task'])}")
        story.append(Paragraph("<br/>".join(parts), err_s))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(reg_name, 8)
        canvas.setFillColor(grey)
        canvas.drawString(18 * mm, 10 * mm, clean(f"{data['project']}  ·  Work Audit Log  ·  {gen_date}"))
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    title = f"{data['project']} - Work Audit Log" + (f" - {phase}" if phase else "")
    doc = SimpleDocTemplate(out_path, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=18 * mm, title=clean(title), author="Claude")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return True


# --------------------------------------------------------------------------- doctor
def doctor():
    """Report whether this machine can build the PDF. Returns a process exit code."""
    import platform
    print(f"Python {platform.python_version()}  ({sys.executable})")
    ok = True
    try:
        import reportlab
        print(f"reportlab {reportlab.Version}: OK")
    except ImportError:
        ok = False
        print("reportlab: MISSING")
        print("  Install:  python3 -m pip install --user reportlab")
        print("  If pip says 'externally-managed-environment', add --break-system-packages")
        print("  (still a user-level install) or use a virtual environment.")
    font = next((r for r, _, _ in FONT_SETS if os.path.exists(r)), None)
    if font:
        print(f"Unicode font for the rupee sign: {font}")
    else:
        print("Unicode font: none found. The PDF still builds but prints 'Rs.' instead of the rupee sign.")
    print("READY to build PDFs." if ok else "NOT READY: install reportlab, then run --doctor again.")
    return 0 if ok else 1


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Build the printable PDF audit report from a phase log (JSON)")
    ap.add_argument("worklog", nargs="?", help="path to the phase log, e.g. worklog/payments.json")
    ap.add_argument("--out-dir", help="where to write the PDF (default: next to the JSON file)")
    ap.add_argument("--date", help="override the generated date (YYYY-MM-DD)")
    ap.add_argument("--check", action="store_true", help="validate the JSON and exit")
    ap.add_argument("--doctor", action="store_true", help="check that this machine can build the PDF, then exit")
    args = ap.parse_args()

    if args.doctor:
        sys.exit(doctor())
    if not args.worklog:
        ap.error("the path to the phase log is required (or use --doctor)")

    data = load(args.worklog)
    errors, warnings = validate(data)
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    if errors:
        print("The log has problems that must be fixed before a report can be built:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(2)

    if args.check:
        print(f"OK: {len(data['domains'])} domains, {len(all_tasks(data))} tasks, "
              f"{len(data.get('decisions') or [])} decisions, {len(data.get('errors') or [])} errors, "
              f"{len(warnings)} warnings.")
        return

    gen_date = args.date or date.today().isoformat()
    out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.worklog))
    os.makedirs(out_dir, exist_ok=True)
    name = slugify(data["project"])
    if data.get("phase"):
        name += "-" + slugify(data["phase"])
    out_path = os.path.join(out_dir, f"{name}-worklog-{gen_date}.pdf")
    if not build_pdf(data, gen_date, out_path):
        sys.exit("ERROR: the Python package 'reportlab' is required to build the PDF.\n"
                 "  Install it with:  python3 -m pip install --user reportlab\n"
                 "  If pip says 'externally-managed-environment', add --break-system-packages "
                 "(still a user-level install) or use a virtual environment.\n"
                 "  Check this machine with:  python3 build_report.py --doctor")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
