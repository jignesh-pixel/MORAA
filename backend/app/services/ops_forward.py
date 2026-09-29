"""Studioo Ops · webhook guard (Python side of the hybrid integration).

Team numbers (``OPS_TEAM``) sending ops messages are diverted away from the
customer pipeline and forwarded to the Next.js route ``/api/ops/inbound`` as a
FastAPI background task. Everything else is returned untouched.

Default-deny and fail-open for customers:
- ``OPS_ENABLED`` false / ``OPS_TEAM`` empty or invalid  -> nothing is diverted.
- Any unexpected error in the guard                      -> the original entry is
  returned unchanged, so customer processing is never blocked.
"""

from typing import Any, Dict, List, Optional
import copy
import json
import re

import httpx
from fastapi import BackgroundTasks

from app.config import settings
from app.utils.logger import logger

# Only messages starting with one of these are ops. Anything else from a team
# number ("Hi", design prompts, ...) goes to the normal customer pipeline.
OPS_PREFIXES = (
    "💸", "💡", "✅", "✔", "☑", "task", "done",
    "start", "block", "todo", "idea", "err", "fix",
    "kb", "undo", "help", "exp",
)


def has_ops_prefix(text: str) -> bool:
    """Stripped, case-insensitive prefix match. Word prefixes must end at a
    non-letter so "expensive ring" / "helpful" / "fixed" are NOT ops."""
    t = (text or "").strip().lower()
    for p in OPS_PREFIXES:
        if t.startswith(p):
            if not p.isalpha() or len(t) == len(p) or not t[len(p)].isalpha():
                return True
    return False


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _team_numbers() -> set:
    if not settings.OPS_ENABLED or not settings.OPS_TEAM:
        return set()
    try:
        team = json.loads(settings.OPS_TEAM)
        return {_digits(n) for n in team.values() if _digits(n)}
    except Exception:
        logger.error("[ops] OPS_TEAM is not valid JSON; ops guard disabled")
        return set()


def is_ops_message(msg: Dict[str, Any]) -> bool:
    """Text or image/document caption from a team number that starts with an ops prefix."""
    msg_type = msg.get("type")
    if msg_type == "text":
        return has_ops_prefix((msg.get("text") or {}).get("body") or "")
    if msg_type in ("image", "document"):
        return has_ops_prefix((msg.get(msg_type) or {}).get("caption") or "")
    return False


async def forward_to_ops(message: Dict[str, Any]) -> None:
    """Background task: hand one raw WhatsApp message to Next.js. Never raises."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                settings.OPS_INBOUND_URL,
                json={"message": message},
                headers={"x-ops-secret": settings.OPS_SECRET},
            )
        if r.status_code >= 300:
            logger.error("[ops] inbound forward failed: {} {}", r.status_code, r.text[:300])
    except Exception as e:
        logger.error("[ops] inbound forward error: {}", e)


def divert_ops_messages(entry: Dict[str, Any], background_tasks: BackgroundTasks) -> Dict[str, Any]:
    """Queue team ops messages for Next.js and return the entry without them.

    Returns the SAME object when nothing is diverted (all customer traffic).
    """
    try:
        team = _team_numbers()
        if not team or not settings.OPS_INBOUND_URL or not settings.OPS_SECRET:
            return entry

        diverted: List[str] = []
        for change in entry.get("changes") or []:
            for msg in (change.get("value") or {}).get("messages") or []:
                if _digits(msg.get("from")) in team and is_ops_message(msg):
                    diverted.append(msg.get("id", ""))
        if not diverted:
            return entry

        stripped = copy.deepcopy(entry)
        for change in stripped.get("changes") or []:
            value = change.get("value") or {}
            msgs = value.get("messages")
            if not msgs:
                continue
            keep = []
            for msg in msgs:
                if msg.get("id", "") in diverted and _digits(msg.get("from")) in team and is_ops_message(msg):
                    background_tasks.add_task(forward_to_ops, msg)
                    logger.info("[ops] diverted team message {} to ops", msg.get("id", ""))
                else:
                    keep.append(msg)
            value["messages"] = keep
        return stripped
    except Exception as e:
        logger.error("[ops] guard error, falling back to customer flow: {}", e)
        return entry
