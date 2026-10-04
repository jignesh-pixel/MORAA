#!/usr/bin/env python3
"""Offline fingerprint of every image-generation prompt, exactly as the model receives it.

Assembles each prompt builder (per earring type, per prompt version, per provider) the way
production does -- builder output + the reference-priority block + the provider anchor -- and
hashes it. No network, no provider call, no key: sockets are blocked and generation is stubbed.

  python scripts/prompt_snapshot.py            # compare with tests/snapshots/prompt_hashes.json
  python scripts/prompt_snapshot.py --update   # rewrite the snapshot (only for an intended prompt change)

Image quality is decided by these prompts, so any difference here is an output-quality change
that must be intentional and reviewed. Exit code 0 = identical.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import pkgutil
import re
import socket
import sys
from pathlib import Path
from typing import Dict

BACKEND = Path(__file__).resolve().parent.parent
SNAPSHOT = BACKEND / "tests" / "snapshots" / "prompt_hashes.json"
PRODUCTS = ("generic", "Hoop", "Stud", "Dangle")
VERSIONS = ("v1", "v2")


def _block_network() -> None:
    def refuse(*_a, **_k):
        raise RuntimeError("prompt snapshot: network access is blocked")

    socket.socket.connect = refuse          # type: ignore[assignment]
    socket.create_connection = refuse       # type: ignore[assignment]


def _opt(module: str, name: str) -> str:
    try:
        return getattr(importlib.import_module(module), name)
    except Exception:
        return ""


def build_snapshot(in_process: bool = False) -> Dict[str, str]:
    """Return {prompt label: sha256 of the final prompt text}.

    in_process=True is for use inside another process (the pytest suite): sockets are left alone
    and every setting this function touches is restored afterwards.
    """
    if not in_process:
        _block_network()
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))

    from app.config import settings

    saved = {flag: getattr(settings, flag, None) for flag in ("GENERATION_ENABLED", "DRY_RUN_IMAGE_MODE", "EARRING_PROMPT_VERSION")}
    try:
        return _build(settings)
    finally:
        if in_process:
            for flag, value in saved.items():
                if hasattr(settings, flag):
                    setattr(settings, flag, value)


def _build(settings) -> Dict[str, str]:
    for flag, value in (("GENERATION_ENABLED", False), ("DRY_RUN_IMAGE_MODE", True)):
        if hasattr(settings, flag):
            setattr(settings, flag, value)

    ref_block = _opt("app.ai.product_fidelity", "REFERENCE_PRIORITY_BLOCK")
    gemini_anchor = _opt("app.ai.product_fidelity", "REFERENCE_IMAGE_ANCHOR")
    openai_anchor = _opt("app.ai.providers.openai_image_provider", "OPENAI_IDENTITY_ANCHOR")

    def assemble(raw: str, provider: str) -> str:
        effective = raw if "REFERENCE IMAGE PRIORITY" in raw.upper() else f"{raw}\n\n{ref_block}"
        anchor = openai_anchor if provider == "openai" else gemini_anchor
        return f"{anchor}\n\n{effective}" if anchor else effective

    import app.services as services

    builders = []
    for mod in pkgutil.iter_modules(services.__path__):
        if not mod.name.endswith("_prompt"):
            continue
        module = importlib.import_module(f"app.services.{mod.name}")
        for name, fn in inspect.getmembers(module, inspect.isfunction):
            if fn.__module__ != module.__name__ or not re.match(r"build_\w*prompt\w*$", name):
                continue
            if re.search(r"_v\d+$", name) or name == "build_close_up_ars_prompt":
                continue
            builders.append((name, fn))

    hashes: Dict[str, str] = {}
    has_toggle = hasattr(settings, "EARRING_PROMPT_VERSION")
    for name, fn in sorted(builders, key=lambda b: b[0]):
        takes_type = "earring_type" in inspect.signature(fn).parameters
        versions = VERSIONS if (has_toggle and name == "build_earring_ecommerce_prompt") else ("-",)
        products = PRODUCTS if takes_type else ("n/a",)
        for version in versions:
            if version != "-":
                settings.EARRING_PROMPT_VERSION = version
            for product in products:
                raw = fn(earring_type=None if product == "generic" else product) if takes_type else fn()
                for provider in ("gemini", "openai"):
                    text = assemble(raw, provider)
                    hashes[f"{name}[{version}] {product} -> {provider}"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if has_toggle:
            settings.EARRING_PROMPT_VERSION = "v1"
    return hashes


def compare(current: Dict[str, str], expected: Dict[str, str]) -> Dict[str, list]:
    return {
        "changed": sorted(k for k in expected if k in current and current[k] != expected[k]),
        "missing": sorted(k for k in expected if k not in current),
        "added": sorted(k for k in current if k not in expected),
    }


def main(argv: list[str]) -> int:
    current = build_snapshot()
    if "--update" in argv:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {len(current)} prompt hashes to {SNAPSHOT}")
        return 0
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    diff = compare(current, expected)
    print(f"prompts: {len(current)} | changed: {len(diff['changed'])} | missing: {len(diff['missing'])} | added: {len(diff['added'])}")
    for kind, items in diff.items():
        for item in items:
            print(f"  {kind}: {item}")
    return 0 if not any(diff.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
