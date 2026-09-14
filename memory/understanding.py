"""
memory/understanding.py — what the user is actually trying to do.

WHY THIS EXISTS
    The other two memories answer different questions. long_term.json holds
    facts ("your city is Baku"). journal.json holds events ("that task failed
    on a usage limit"). Neither answers the one that matters most at the start
    of a conversation: *what is this person working towards, and where did we
    leave it?*

    Without that, every session opens cold. The user re-explains the project,
    re-states how they want things done, and re-establishes what was next —
    to an assistant that was present for all of it.

    This file is a single, small, always-current picture: the direction, the
    current focus, what comes next, how they like to work, and what is still
    open. It is rewritten at the end of each session rather than appended to,
    so it stays a page rather than becoming an archive. The archive already
    exists next door; this is the reading of it.

HOW IT IS MAINTAINED
    The session-summary pass at the end of a conversation already sends the
    transcript to a model. That same call now returns this picture too, merged
    with the previous one — so understanding costs no extra round trip, and it
    updates on every reconnect rather than only at a clean shutdown.

WHAT DOES NOT BELONG HERE
    Transcripts, one-off facts, or anything that only mattered for an hour.
    If it would not help open the next conversation, it is not this.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from threading import Lock


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


UNDERSTANDING_PATH = _base_dir() / "memory" / "understanding.json"
_lock = Lock()

# Deliberately tight. A picture that needs scrolling is an archive, and the
# whole value here is that it can be read in one breath at session start.
MAX_DIRECTION = 400
MAX_FOCUS = 300
MAX_ITEMS = 5
MAX_ITEM_CHARS = 180

_FIELDS = ("direction", "focus", "next_steps", "working_style", "open_questions")


def _empty() -> dict:
    return {
        "updated": "",
        "direction": "",        # what they are building, and why
        "focus": "",            # what they are on right now
        "next_steps": [],       # what they said comes next
        "working_style": [],    # how they want work done
        "open_questions": [],   # decisions still hanging
    }


def load() -> dict:
    if not UNDERSTANDING_PATH.exists():
        return _empty()
    try:
        data = json.loads(UNDERSTANDING_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty()
        merged = _empty()
        merged.update({k: v for k, v in data.items() if k in merged})
        return merged
    except Exception:
        return _empty()


def _clean_list(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text[:MAX_ITEM_CHARS])
    return out[:MAX_ITEMS]


def save(update: dict) -> dict:
    """Replace the picture with a cleaned version of `update`.

    Replacement, not merge: the model is given the previous picture and asked
    to produce the current one, so anything it dropped was dropped on purpose.
    Empty strings are the exception — they mean "nothing new to say", and the
    old value is kept rather than blanked by a quiet model.
    """
    previous = load()
    picture = _empty()
    picture["direction"] = (str(update.get("direction", "")).strip()[:MAX_DIRECTION]
                            or previous["direction"])
    picture["focus"] = (str(update.get("focus", "")).strip()[:MAX_FOCUS]
                        or previous["focus"])
    for key in ("next_steps", "working_style", "open_questions"):
        picture[key] = _clean_list(update.get(key)) or previous[key]
    picture["updated"] = datetime.now().isoformat(timespec="seconds")

    try:
        with _lock:
            UNDERSTANDING_PATH.parent.mkdir(parents=True, exist_ok=True)
            UNDERSTANDING_PATH.write_text(
                json.dumps(picture, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        print(f"[Understanding] updated: {picture['focus'][:60]}")
    except Exception as e:
        print(f"[Understanding] could not save: {e}")
    return picture


def has_content(picture: dict | None = None) -> bool:
    p = picture or load()
    return bool(p["direction"] or p["focus"] or p["next_steps"])


def format_for_prompt() -> str:
    """The block that opens every session. Empty until there is something
    real to say, so a fresh install carries no invented context."""
    p = load()
    if not has_content(p):
        return ""

    lines = ["[WHAT THE USER IS WORKING TOWARDS — you already know this]"]
    if p["direction"]:
        lines.append(f"Direction: {p['direction']}")
    if p["focus"]:
        lines.append(f"Currently: {p['focus']}")
    if p["next_steps"]:
        lines.append("Next: " + "; ".join(p["next_steps"]))
    if p["working_style"]:
        lines.append("How they want work done: " + "; ".join(p["working_style"]))
    if p["open_questions"]:
        lines.append("Still undecided: " + "; ".join(p["open_questions"]))
    lines.append(
        "Continue from here. Do not ask them to re-explain the project or "
        "re-state a preference listed above. If they pick up mid-thread, you "
        "are expected to know where it was left."
    )
    lines.append("")
    return "\n".join(lines)


def reflection_prompt(previous: dict, conversation: str, recent_work: str = "") -> str:
    """The instruction that turns a transcript into an updated picture."""
    return (
        "You maintain a personal assistant's understanding of one user.\n\n"
        "Below is the picture you held before this conversation, the "
        "conversation itself, and recent delegated work. Produce the UPDATED "
        "picture.\n\n"
        "Rules:\n"
        "- Keep what is still true, change what moved, drop what is finished.\n"
        "- 'direction' is the durable goal — what they are building and why. "
        "It changes rarely.\n"
        "- 'focus' is what they are on right now.\n"
        "- 'working_style' is how they want work done (things they corrected "
        "you on, standards they insist on). Only things they actually said.\n"
        "- 'open_questions' are decisions still hanging, not tasks.\n"
        "- Never invent. If the conversation says nothing about a field, "
        "return the previous value.\n"
        "- Be specific and short. Each list item one line, at most five items.\n\n"
        "Reply with ONLY a JSON object, no code fence, with keys: "
        "summary (1-2 sentences describing this conversation), direction, "
        "focus, next_steps (list), working_style (list), open_questions (list).\n\n"
        f"PREVIOUS PICTURE:\n{json.dumps(previous, ensure_ascii=False, indent=2)}\n\n"
        f"RECENT DELEGATED WORK:\n{recent_work or '(none)'}\n\n"
        f"CONVERSATION:\n{conversation}"
    )


def parse_reflection(text: str) -> tuple[str, dict]:
    """(summary, picture-update) from the model's reply. Tolerant: a model that
    answers with prose instead of JSON still yields a usable summary."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1] if "```" in raw[3:] else raw.strip("`")
        raw = raw.split("\n", 1)[-1] if raw.lower().startswith("json") else raw
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start:end + 1])
            if isinstance(data, dict):
                summary = str(data.get("summary", "")).strip()
                return summary, {k: data.get(k) for k in _FIELDS}
        except Exception:
            pass
    return (text or "").strip(), {}
