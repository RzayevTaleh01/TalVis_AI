"""
actions/self_check.py — let the assistant look at its own failures.

WHY THIS EXISTS
    TalVis could diagnose any project on the machine except the one it is.
    When something inside it broke, the traceback went to a console nobody was
    reading, and the model's only source of truth was the user saying "it
    stopped working". It would then guess, or ask them to describe a stack
    trace out loud.

    This hands the model its own log. Combined with `delegate` — whose default
    workspace is TalVis's own folder — that closes the loop: read the error,
    understand it, and if it is not obvious, hand the traceback to an agent
    that can open the source and fix it.

WHAT IT DOES NOT DO
    It does not fix anything. Reading and repairing are deliberately separate:
    reading is instant and free, and most questions end there. Repair goes
    through delegate, where the modes and the journal already apply.
"""
from __future__ import annotations

from core import selflog


def _fmt(lines: list[str], empty: str) -> str:
    return "\n".join(lines) if lines else empty


def self_check(parameters=None, player=None, session_memory=None) -> str:
    params = parameters or {}
    what = str(params.get("what", "errors")).lower().strip()
    try:
        limit = max(5, min(120, int(params.get("limit", 40))))
    except (TypeError, ValueError):
        limit = 40

    if player is not None:
        try:
            player.write_log(f"[self_check] {what}")
        except Exception:
            pass

    if what in ("log", "logs", "output", "recent"):
        return _fmt(selflog.tail(limit), "Nothing has been logged yet this run.")

    if what in ("previous", "last_run", "crash", "file"):
        return _fmt(selflog.file_tail(limit),
                    "There is no log file on disk yet.")

    # Default: what went wrong.
    found = selflog.errors(limit)
    if not found:
        return (
            "No errors have been recorded this run — nothing has gone wrong "
            "since TalVis started. If the user is describing a problem that is "
            "not in the log, ask them what they saw."
        )
    return (
        f"{selflog.error_count()} error line(s) recorded this run. Most recent:\n"
        + "\n".join(found)
        + "\n\nRead this yourself and explain the cause in plain language. "
          "If the fix is not obvious from the trace, hand it to the agent with "
          "delegate — the default workspace is TalVis's own source — pasting "
          "the relevant lines into the task."
    )


TOOL = {
    "name": "self_check",
    "description": (
        "Shows YOUR OWN errors and log output — TalVis's internals, not the "
        "computer's health (that is system_status). Call this whenever "
        "something in you misbehaves: the user says you crashed, went quiet, "
        "restarted, lost the connection, or a tool did not do what it should. "
        "Read the result and explain the cause yourself. If the cause is not "
        "clear from it, pass the error text to `delegate` so an agent can open "
        "TalVis's source and investigate. Never ask the user to read a "
        "traceback to you — read your own."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "what": {
                "type": "STRING",
                "description": (
                    "errors (default) = error and traceback lines from this "
                    "run. log = recent output of every kind. previous = the "
                    "log file on disk, which survives a restart, for asking "
                    "what happened before a crash."
                ),
            },
            "limit": {
                "type": "INTEGER",
                "description": "How many lines to return (5-120, default 40).",
            },
        },
        "required": [],
    },
    "handler": self_check,
}
