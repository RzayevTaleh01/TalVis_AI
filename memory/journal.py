"""
memory/journal.py — what was attempted, and how it went.

WHY THIS EXISTS
    long_term.json remembers facts about the person: their name, their city,
    what they are building. Nothing remembered what the assistant *did*.

    So when a delegated task failed — the agent hitting its usage limit is the
    ordinary case — the failure lived only in that one spoken sentence. Ask
    about it an hour later and the assistant had no idea the task had ever been
    attempted, let alone that it was worth retrying. Every failure had to be
    re-explained by the user.

    The journal is the other half of memory: an append-only record of delegated
    work, its outcome, and whether the outcome is worth another go. It is what
    lets "did that finish?" and "try it again" work across sessions.

WHAT GOES IN
    One entry per delegated task. Not conversation, not preferences — those
    already have homes. If nothing was attempted, nothing is written.

RETRYABLE
    A usage limit, a rate limit, a timeout or a dropped connection are all
    "the same request would probably work later". A task the agent understood
    and refused, or one that finished, is not. That single flag is what turns
    a remembered failure into an offer to try again.
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from threading import Lock


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


JOURNAL_PATH = _base_dir() / "memory" / "journal.json"
_lock = Lock()

# Enough to answer "what have we been doing lately" without the file becoming
# something that has to be paged.
MAX_ENTRIES = 500

# Only the last few unfinished items are worth prompt space; the rest are found
# by asking.
PROMPT_MAX_UNFINISHED = 3
PROMPT_MAX_RECENT = 3

# Failures that mean "later, probably fine" rather than "this cannot work".
_RETRYABLE_PATTERNS = (
    r"usage limit", r"rate.?limit", r"quota", r"too many requests",
    r"\b429\b", r"\b503\b", r"overloaded", r"capacity",
    r"timed? ?out", r"timeout", r"deadline",
    r"connection", r"network", r"temporarily unavailable", r"try again",
)

_TERMINAL_OK = {"done"}


def _looks_retryable(status: str, detail: str) -> bool:
    if status in _TERMINAL_OK:
        return False
    if status in ("timeout",):
        return True
    blob = f"{status} {detail}".lower()
    return any(re.search(p, blob) for p in _RETRYABLE_PATTERNS)


def _load() -> list[dict]:
    if not JOURNAL_PATH.exists():
        return []
    try:
        data = json.loads(JOURNAL_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(entries: list[dict]) -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL_PATH.write_text(
        json.dumps(entries[-MAX_ENTRIES:], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def record(task: str, status: str, agent: str = "claude", detail: str = "",
           workspace: str = "", mode: str = "", duration_s: float = 0.0) -> str:
    """Append one attempt. Returns its id. Never raises — a journal that can
    take the assistant down is worse than no journal."""
    try:
        entry = {
            "id": f"j-{uuid.uuid4().hex[:8]}",
            "ts": datetime.now().isoformat(timespec="seconds"),
            "agent": agent,
            "task": (task or "")[:300],
            "workspace": workspace,
            "mode": mode,
            "status": status,
            "detail": (detail or "")[:300],
            "duration_s": round(float(duration_s or 0), 1),
            "retryable": _looks_retryable(status, detail),
            "resolved": status in _TERMINAL_OK,
        }
        with _lock:
            entries = _load()
            entries.append(entry)
            _save(entries)
        print(f"[Journal] {status}: {entry['task'][:60]}")
        return entry["id"]
    except Exception as e:
        print(f"[Journal] could not record: {e}")
        return ""


def resolve(entry_id: str) -> None:
    """Mark an attempt as no longer outstanding — retried, or dropped."""
    try:
        with _lock:
            entries = _load()
            for e in entries:
                if e.get("id") == entry_id:
                    e["resolved"] = True
            _save(entries)
    except Exception:
        pass


def resolve_matching(task: str) -> int:
    """Close out earlier attempts at the same task once it finally succeeds."""
    key = _norm(task)
    if not key:
        return 0
    closed = 0
    try:
        with _lock:
            entries = _load()
            for e in entries:
                if not e.get("resolved") and _norm(e.get("task", "")) == key:
                    e["resolved"] = True
                    closed += 1
            if closed:
                _save(entries)
    except Exception:
        pass
    return closed


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def recent(limit: int = 10) -> list[dict]:
    return list(reversed(_load()))[:limit]


def unfinished(limit: int = 10) -> list[dict]:
    return [e for e in reversed(_load()) if not e.get("resolved")][:limit]


def search(query: str, limit: int = 8) -> list[dict]:
    words = [w for w in _norm(query).split() if len(w) > 2]
    if not words:
        return recent(limit)
    hits = []
    for e in reversed(_load()):
        blob = _norm(f"{e.get('task','')} {e.get('detail','')} "
                     f"{e.get('workspace','')} {e.get('agent','')}")
        score = sum(1 for w in words if w in blob)
        if score:
            hits.append((score, e))
    hits.sort(key=lambda pair: pair[0], reverse=True)
    return [e for _, e in hits[:limit]]


def _describe(e: dict) -> str:
    when = (e.get("ts") or "")[:16].replace("T", " ")
    where = f" in {e['workspace']}" if e.get("workspace") else ""
    detail = f" — {e['detail']}" if e.get("detail") else ""
    retry = " [worth retrying]" if e.get("retryable") else ""
    return (f"{when} · {e.get('agent','agent')} · \"{e.get('task','')[:90]}\""
            f"{where}: {e.get('status','?')}{detail}{retry}")


def format_for_prompt() -> str:
    """A short block for the system prompt: what is still hanging, and what
    was recently attempted. Empty string when there is nothing to say, so a
    fresh install carries no dead weight."""
    try:
        outstanding = unfinished(PROMPT_MAX_UNFINISHED)
        done = [e for e in recent(PROMPT_MAX_RECENT * 3)
                if e.get("resolved")][:PROMPT_MAX_RECENT]
    except Exception:
        return ""

    if not outstanding and not done:
        return ""

    parts = ["[DELEGATED WORK — you already know about these]"]
    if outstanding:
        parts.append("Still open:")
        parts += [f"  - {_describe(e)}" for e in outstanding]
        if any(e.get("retryable") for e in outstanding):
            parts.append(
                "  If the user asks about one of these, you already know it "
                "failed and why. Say so plainly and offer to run it again — "
                "do not ask them to explain it to you."
            )
    if done:
        parts.append("Recently finished:")
        parts += [f"  - {_describe(e)}" for e in done]
    parts.append("")
    return "\n".join(parts)


def format_for_recall(query: str = "") -> str:
    """Answer to 'what did you do about X' — used by the recall tool."""
    entries = search(query) if query else recent(8)
    if not entries:
        return "Nothing has been delegated to an agent yet." if not query else \
               f"No delegated task matches '{query}'."
    return "\n".join(_describe(e) for e in entries)
