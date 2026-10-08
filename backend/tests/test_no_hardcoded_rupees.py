"""Phase 8 guard: rupee amounts live in app/services/pricing.py only.

Fails the build when a file under app/ (other than pricing.py, and config.py whose defaults are the first seeds of
those prices) writes a rupee amount into code: a "₹500" / "Rs 500" text, a number assigned to a PRICE/RUPEES/AMOUNT/
RECHARGE name, an ``amount=500`` argument or default, ``max(price, 500)`` / ``min(...)`` with a money name, or a
comparison such as ``requested_amount < 500``. Docstrings and comments are not code and are ignored.
"""

import ast
import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
# prompt_generation_service compares a piece's ESTIMATED market value to pick marketing words; it charges
# nothing (and its prompts are frozen), so it is not a price.
ALLOWED = {APP / "services" / "pricing.py", APP / "config.py", APP / "services" / "prompt_generation_service.py"}
MONEY_NAME = re.compile(r"(price|rupee|amount|recharge|total|balance)", re.I)
RUPEE_TEXT = re.compile(r"(₹\s*\d|\bRs(\.\s*|\s+)\d|\bINR\s+\d)", re.I)
MIN_AMOUNT = 10            # small numbers (0, 1, retries, lengths) are not prices


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _number(node: ast.AST) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool) and node.value >= MIN_AMOUNT)


def _docstrings(tree: ast.AST) -> set:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def violations(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstrings(tree)
    found = []

    def hit(node: ast.AST, why: str) -> None:
        found.append(f"{path.relative_to(APP.parent)}:{node.lineno}: {why}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if RUPEE_TEXT.search(node.value):
                hit(node, f"rupee amount in text {node.value[:40]!r}")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None and _number(node.value) and any(MONEY_NAME.search(_name(t)) for t in targets):
                hit(node, f"rupee amount assigned to {_name(targets[0])}")
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg and MONEY_NAME.search(keyword.arg) and _number(keyword.value):
                    hit(node, f"rupee amount passed as {keyword.arg}=")
            if _name(node.func) in ("max", "min") and any(_number(a) for a in node.args) and any(
                    MONEY_NAME.search(_name(n)) for a in node.args for n in ast.walk(a)):
                hit(node, f"rupee floor/ceiling in {_name(node.func)}()")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.args + node.args.kwonlyargs
            defaults = [None] * (len(node.args.args) - len(node.args.defaults)) + node.args.defaults + node.args.kw_defaults
            for arg, default in zip(args, defaults):
                if default is not None and MONEY_NAME.search(arg.arg) and _number(default):
                    hit(node, f"rupee default for {arg.arg}")
        elif isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            if any(_number(s) for s in sides) and any(MONEY_NAME.search(_name(s)) for s in sides):
                hit(node, "rupee amount in a comparison")
    return found


class NoHardcodedRupeesTests(unittest.TestCase):
    def test_no_rupee_amount_outside_pricing(self):
        found = []
        for path in sorted(APP.rglob("*.py")):
            if path not in ALLOWED:
                found += violations(path)
        self.assertEqual(found, [], "Move these amounts into app/services/pricing.py:\n" + "\n".join(found))

    def test_the_guard_catches_the_usual_forms(self):
        sample = APP.parent / "tests" / "_rupee_guard_sample.py"
        sample.write_text(
            'MIN_RECHARGE_RUPEES = 500\n'
            'def f(amount: int = 500):\n'
            '    """₹500 in a docstring is fine."""\n'
            '    text = "Minimum recharge is ₹500"\n'
            '    if requested_amount < 500:\n'
            '        pass\n'
            '    return g(amount=500), max(price, 500)\n', encoding="utf-8")
        self.addCleanup(sample.unlink)
        found = violations(sample)
        self.assertEqual(len(found), 6, found)


if __name__ == "__main__":
    unittest.main()
