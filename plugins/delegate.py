"""
plugins/delegate.py — hand a whole task to a capable AI agent.

WHY THIS EXISTS
    Gemini Live is a single-turn audio model. It answers, and it can call a
    tool, but it cannot open a repository, read ten files, edit three of them,
    run the tests and fix what broke. The two coding actions this replaces
    (dev_agent, code_helper) tried to fake that loop with one-shot
    gemini-flash calls and a lot of glue, and they overlapped each other.

    Claude Code already is that loop. It runs as a local binary, authenticated
    with the user's own Claude account — no API key, no second bill — so the
    integration is a subprocess call, not a client library.

BILLING
    This must stay on the subscription. Claude Code prefers an API key over the
    logged-in account whenever one is present in the environment, and it will
    route to Bedrock or Vertex if told to — each of those is a separate bill
    that would start silently the day something exports one of those variables.
    _subprocess_env() strips them, so a run here always uses the account from
    `claude auth status` and nothing else.

THE DIVISION OF LABOUR
    TalVis (Gemini Live)  — voice, conversation, instant actions
    Claude Code           — multi-step work on the filesystem

    Keeping that line sharp is the whole point. This plugin is deliberately ONE
    tool with a narrow description: if it also answered general questions, the
    model would have two assistants to choose between and would pick wrong.

SAFETY
    Claude Code can edit files and run commands, and here nobody is sitting at
    a keyboard clicking "approve". Two things bound it:

      • Place — each run is pointed at ONE folder: the project named by the
        user, or the default workspace. Any folder on the machine can be
        named, and "Also allow" adds roots visible to every run, so this is
        about focus and speed rather than a fence — an agent handed the
        whole disk searches slowly and edits the wrong copy of a file.
      • Mode  — 'read' answers and reviews with the modifying tools denied,
        'edit' may change files, 'auto' may also run commands. The user picks
        by voice; the default comes from settings.

    --permission-mode bypassPermissions is never used and is not reachable from
    a spoken request, whatever mode is asked for.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from memory import journal

# Spoken mode -> (permission mode, tools denied). bypassPermissions is absent
# on purpose: there is no phrasing a user can say that reaches it.
#
# "read" denies the modifying tools rather than using --permission-mode plan.
# Plan mode is built for a human who will read a proposal and click approve,
# so for a spoken question it answers "no plan needed" and the actual finding
# is left behind in an earlier turn. Denying Bash/Edit/Write gets the same
# guarantee — nothing on disk changes — while still answering the question.
_MODES = {
    "read": ("auto", ["Bash", "Edit", "Write", "NotebookEdit"]),
    "edit": ("acceptEdits", ["Bash"]),
    "auto": ("auto", []),
}
# The model has been known to say "plan"; treat it as the read-only mode.
_MODE_ALIASES = {"plan": "read", "review": "read", "readonly": "read"}
_DEFAULT_MODE = "edit"
_DEFAULT_MODEL = "sonnet"
_DEFAULT_TIMEOUT_MIN = 10

# Claude Code hands back a session id; keeping the last one lets "carry on with
# that" continue the same context instead of starting cold. Process-lifetime
# only — a fresh launch should not silently resume yesterday's work.
_last_session_id: str | None = None
_lock = threading.Lock()


# ── The agents ───────────────────────────────────────────────────────────────
#
# One entry per agent that can be handed a whole task. Claude Code is the only
# one installed today, but the shape is what matters: an agent is a command to
# build and a way to read what came back. Adding a second one — another CLI
# agent, a local model with a runner — is a new entry here and nothing else,
# because the job runner, the journal, the modes and the voice surface are all
# agent-agnostic already.
#
# `speaks_json` marks an agent whose output is parsed as Claude Code's result
# envelope; an agent that just prints text sets it False and the raw output is
# used instead.
_AGENTS: dict[str, dict] = {
    "claude": {
        "label": "Claude",
        "exe": "claude",
        "models": ("sonnet", "opus"),
        "default_model": "sonnet",
        "speaks_json": True,
    },
}
_DEFAULT_AGENT = "claude"
# What a person might call each agent out loud.
_AGENT_ALIASES = {
    "claude code": "claude", "claude-code": "claude", "anthropic": "claude",
    "code agent": "claude", "coder": "claude",
}


def _resolve_agent(name: str) -> tuple[dict | None, str, str]:
    """(spec, key, error). Falls back to the default when nothing is named."""
    key = re.sub(r"\s+", " ", (name or "").strip().lower())
    if not key:
        key = _DEFAULT_AGENT
    key = _AGENT_ALIASES.get(key, key)
    spec = _AGENTS.get(key)
    if spec is None:
        return None, key, (
            f"There is no agent called '{name}'. Available: "
            f"{', '.join(sorted(_AGENTS))}. Ask the user which one they meant."
        )
    if not shutil.which(spec["exe"]):
        return None, key, (
            f"{spec['label']} is not installed on this computer, so it cannot "
            f"be given the task."
        )
    return spec, key, ""


PLUGIN = {
    "name": "delegate",
    "description": (
        "Hands a whole task to an AI agent that works on this computer — it "
        "reads files, edits them, runs commands, checks its own work and "
        "fixes what it broke. Use it for anything that needs several steps "
        "in a folder: building or changing a feature, fixing a bug, "
        "refactoring, investigating why something crashes, reviewing or "
        "summarising a project, reorganising files, running and repairing "
        "tests. "
        "Each run works in one folder: whatever the user names in `project` "
        "(any folder on the computer, by name or full path), or the default "
        "workspace. If they do not say where, ask before guessing. "
        "Do NOT use it to open apps, change computer settings, search the web, "
        "or answer questions you can already answer yourself — it takes minutes "
        "and spends the user's AI plan allowance. "
        "It returns IMMEDIATELY and keeps working in the background — say "
        "one short sentence and move on; the result arrives later as an "
        "[AGENT] message naming the task. SEVERAL TASKS CAN RUN AT ONCE: "
        "start a new one whenever the user asks, even while others are "
        "going — never make them wait. The only pairing refused is two "
        "agents editing the same folder. Just do not call it twice for "
        "the SAME request. action='status' lists everything running; "
        "action='cancel' stops one (name the project or task) or all."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "task": {
                "type": "STRING",
                "description": (
                    "The development task, written in English, with enough "
                    "detail to act on without asking follow-up questions."
                ),
            },
            "mode": {
                "type": "STRING",
                "description": (
                    "read = answers and reviews, cannot change anything. "
                    "edit = may change files. "
                    "auto = may change files and run commands. "
                    "Pass what the user asked for; omit to use their default."
                ),
            },
            "project": {
                "type": "STRING",
                "description": (
                    "Which folder to work in, when the user names one — "
                    "'work on Diaspora', 'in my TripCalc project'. A folder "
                    "name is looked up anywhere under the projects root; a "
                    "full path (C:/laragon/www/site) is used as given, "
                    "anywhere on the machine. Omit to use the default workspace."
                ),
            },
            "model": {
                "type": "STRING",
                "description": "sonnet (default, fast) or opus (slower, stronger).",
            },
            "agent": {
                "type": "STRING",
                "description": (
                    "Which agent to hand it to, when the user names one "
                    "('ask Claude to…'). Omit for the default."
                ),
            },
            "action": {
                "type": "STRING",
                "description": (
                    "run (default) to start a task, status to report how "
                    "far along the running one is, cancel to stop it. "
                    "task is only needed for run."
                ),
            },
            "continue_previous": {
                "type": "BOOLEAN",
                "description": (
                    "true to carry on the previous delegated session instead "
                    "of starting fresh — use when the user says 'keep going', "
                    "'now also…', or refers back to what it just did."
                ),
            },
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "delegate",
    "title": "Delegate (AI agents)",
    "fields": [
        {
            "key": "workspace",
            "type": "text",
            "label": "Workspace folder",
            "placeholder": str(Path(__file__).resolve().parent.parent),
        },
        {
            "key": "projects_root",
            "type": "text",
            "label": "Projects folder (lets you name a project by voice)",
            "placeholder": str(Path.home()),
        },
        {
            "key": "extra_roots",
            "type": "text",
            "label": "Also allow (extra folders, separated by ;)",
            "placeholder": r"C:\laragon\www; D:\work",
        },
        {
            "key": "default_mode",
            "type": "choice",
            "label": "Default mode",
            "options": ["read", "edit", "auto"],
            "default": _DEFAULT_MODE,
        },
        {
            "key": "model",
            "type": "choice",
            "label": "Model",
            "options": ["sonnet", "opus"],
            "default": _DEFAULT_MODEL,
        },
        {
            "key": "timeout_min",
            "type": "choice",
            "label": "Timeout (minutes)",
            "options": ["2", "5", "10", "20", "40"],
            "default": str(_DEFAULT_TIMEOUT_MIN),
        },
    ],
}


# ── Configuration ────────────────────────────────────────────────────────────

def _setting(key: str, fallback):
    try:
        from memory.config_manager import get_plugin_setting
        value = get_plugin_setting("delegate", key)
        if value not in (None, ""):
            return value
    except Exception:
        pass
    return fallback


def _workspace() -> Path:
    """Where Claude is allowed to work. Defaults to this project's own folder."""
    configured = str(_setting("workspace", "")).strip().strip('"')
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent.parent


def _projects_root() -> Path:
    """Where a spoken project name is looked up. Defaults to the home folder,
    so anything the user owns can be reached by name."""
    configured = str(_setting("projects_root", "")).strip().strip('"')
    if configured:
        return Path(configured).expanduser()
    return Path.home()


def _extra_roots() -> list[Path]:
    """Additional folders every run can see, from the 'Also allow' setting.

    A run is pointed at one folder because that is what makes it fast and
    accurate, not because the agent is being fenced in — any absolute path can
    be named directly, and anything listed here is visible from every run.
    """
    raw = str(_setting("extra_roots", "")).strip()
    out = []
    for part in re.split(r"[;\n]+", raw):
        part = part.strip().strip('"')
        if not part:
            continue
        p = Path(part).expanduser()
        if p.is_dir():
            out.append(p)
    return out


# Folders under the projects root that are never a project someone means.
_SKIP_DIRS = {
    "desktop.ini", "$recycle.bin", "node_modules", "__pycache__",
    "appdata", "windows", "program files", "program files (x86)",
    "onedrive", "programdata", "nuget", ".git", ".venv", "venv",
    "dist", "build", "site-packages", "temp", "tmp", "cache",
    "application data", "local settings", "recent", "searches",
    "3d objects", "favorites", "links", "saved games", "contacts",
}

# The projects root is the home folder by default, and a project can be a few
# levels in (Desktop/Diaspora/diaspora-ui). Depth 3 covers that; the cap keeps
# a stray deep tree from turning a spoken name into a filesystem crawl.
_SCAN_DEPTH = 3
_SCAN_CAP = 3000


def _scan_projects() -> list[Path]:
    """Every candidate folder under the projects root, newest first."""
    root = _projects_root()
    if not root.is_dir():
        return []

    found: list[Path] = []
    frontier = [(root, 0)]
    while frontier and len(found) < _SCAN_CAP:
        parent, depth = frontier.pop()
        if depth >= _SCAN_DEPTH:
            continue
        try:
            children = list(parent.iterdir())
        except Exception:
            continue
        for child in children:
            if len(found) >= _SCAN_CAP:
                break
            try:
                if not child.is_dir():
                    continue
            except Exception:
                continue
            if child.name.startswith((".", "$")) or child.name.lower() in _SKIP_DIRS:
                continue
            found.append(child)
            frontier.append((child, depth + 1))

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except Exception:
            return 0.0

    found.sort(key=_mtime, reverse=True)
    return found


def list_projects(limit: int = 40) -> list[str]:
    """Folder names under the projects root, most recently touched first."""
    root = _projects_root()
    out = []
    for p in _scan_projects()[:limit]:
        try:
            out.append(str(p.relative_to(root)))
        except ValueError:
            out.append(str(p))
    return out


def _resolve_project(name: str) -> tuple[Path | None, str]:
    """Map a spoken project name to a folder. Returns (path, error_message).

    Speech gives approximate names, so a name is matched fuzzily against the
    folders under the projects root, and the deepest, most exact match wins.
    A full path is taken literally instead — the user pointing at a location
    is not a guess to be second-guessed.
    """
    root = _projects_root()
    if not root.is_dir():
        return None, (
            f"No projects folder is configured (looked in {root}). "
            f"Tell the user to set it in Settings → Delegate."
        )

    # An absolute path means the user pointed somewhere explicitly — honour it
    # anywhere on the machine, including off the projects root and other drives.
    spoken = (name or "").strip().strip('"')
    if spoken:
        direct = Path(spoken).expanduser()
        if direct.is_absolute():
            if direct.is_dir():
                return direct.resolve(), ""
            return None, f"There is no folder at {direct}."

    wanted = _norm_name(name)
    if not wanted:
        return None, "No project name was given."

    # Scored over EVERY scanned folder, not the recent-first display slice —
    # a project untouched for months still has to be findable by name.
    best, best_score = None, 0
    for folder in _scan_projects():
        score = _name_score(wanted, _norm_name(folder.name))
        if score > best_score:
            best, best_score = folder, score

    if best is None or best_score < 40:
        listing = ", ".join(list_projects(8)) or "none found"
        return None, (
            f"There is no folder called '{name}' under {root}. "
            f"Recently used: {listing}. Ask the user which one they meant, "
            f"or ask for the full path."
        )

    resolved = best.resolve()
    root_resolved = root.resolve()
    if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
        return None, f"'{name}' does not resolve to a folder inside {root}."
    if not resolved.is_dir():
        return None, f"'{best}' is no longer a folder."
    return resolved, ""


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _name_score(wanted: str, folder: str) -> int:
    if not wanted or not folder:
        return 0
    if wanted == folder:
        return 100
    if folder.startswith(wanted) or wanted.startswith(folder):
        return 85
    if wanted in folder or folder in wanted:
        return 65
    w, f = set(wanted.split()), set(folder.split())
    if w and w <= f:
        return 55
    if w & f:
        return 45
    return 0


def _log(player, message: str) -> None:
    print(f"[delegate] {message}")
    if player is not None:
        try:
            player.write_log(f"[delegate] {message}")
        except Exception:
            pass


def _say(player, instruction: str) -> None:
    """Speak mid-task. run() blocks its worker thread, so the tool's return
    value cannot reach the user until the work is finished — this is the only
    channel that can tell them anything before then."""
    if player is None:
        return
    try:
        request_say = getattr(player, "request_say", None)
        if callable(request_say):
            request_say(instruction)
    except Exception:
        pass


# Anything here would move the run off the logged-in account and onto a
# metered bill: an API key or auth token, a redirected base URL, or one of the
# cloud-provider switches. Removed from the child's environment every time.
_BILLING_OVERRIDES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)


def _subprocess_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _BILLING_OVERRIDES}
    env["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] = "1"
    return env


# ── Running Claude Code ──────────────────────────────────────────────────────

def _build_command(spec: dict, task: str, mode: str, model: str,
                   workspace: Path, resume: str | None) -> list[str]:
    exe = shutil.which(spec["exe"])
    if not exe:
        raise FileNotFoundError(
            f"{spec['label']} is not installed, or not on PATH."
        )

    permission, denied = _MODES[mode]
    cmd = [
        exe, "-p", task,
        "--output-format", "json",
        "--model", model,
        "--permission-mode", permission,
        "--add-dir", str(workspace),
    ]
    for extra in _extra_roots():
        if extra.resolve() != workspace.resolve():
            cmd += ["--add-dir", str(extra)]
    if denied:
        cmd += ["--disallowedTools", *denied]
    if resume:
        cmd += ["--resume", resume]
    return cmd


def _summarise(text: str, limit: int = 420) -> str:
    """A spoken answer has to be short. The full text goes to the HUD panel."""
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: stop + 1] if stop > limit * 0.5 else cut) + " …"


# ── The job ──────────────────────────────────────────────────────────────────
#
# WHY THIS RUNS IN THE BACKGROUND
#     The first version blocked inside the tool call until Claude finished, and
#     it took the assistant down. Gemini Live sends a tool_call and waits for a
#     tool_response; a coding task takes minutes, the server gave up waiting,
#     stopped servicing the socket, and the client's keepalive ping then timed
#     out and closed the connection:
#
#         sent 1011 (internal error) keepalive ping timeout
#
#     Measuring ruled out the obvious suspect — that run used 7 CPU-seconds in
#     3.2 minutes, so nothing was starved of CPU. The mistake was structural: a
#     realtime session cannot be held open waiting on a minutes-long tool.
#
#     So the tool returns at once and the work continues in a thread. When it
#     finishes, the result is pushed into whatever session is live at that
#     moment, through the same channel proactive check-ins use — which also
#     means a reconnect in the meantime costs nothing.

# Several tasks at once, keyed by job id.
#
# This started as a single slot that refused a second task. That was wrong for
# the way the work actually arrives: the tasks are independent, they are mostly
# spent waiting on a network round trip rather than on this machine, and making
# the user watch one project finish before starting another wastes their time
# for no benefit.
#
# Two limits remain, and both are about correctness rather than tidiness:
# MAX_CONCURRENT keeps a slip of the tongue from launching a dozen agents on
# one plan, and two writers are never allowed into the same folder at once —
# concurrent agents editing the same files overwrite each other's work.
_jobs: dict[str, dict] = {}
_job_lock = threading.Lock()
MAX_CONCURRENT = 4
_job_seq = 0

# Modes that can change files. Two of these in one folder is the collision.
_WRITING_MODES = {"edit", "auto"}


def _kill_tree(proc) -> None:
    """Kill Claude Code and anything it spawned. A bare proc.kill() can leave
    children behind, still spending the user's plan allowance."""
    try:
        import psutil
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
        parent.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _announce(player, instruction: str) -> None:
    """Hand the finished work back to the live session.

    [AGENT] is the same shape as the [SYSTEM_ALERT] and [PROACTIVE_CHECK]
    tags prompt.txt already defines, so the model speaks the substance in the
    user's own language instead of reading the tag out.
    """
    _say(player, instruction)


def _clear_job(job_id: str) -> bool:
    """Drop one job, reporting whether it had been cancelled."""
    with _job_lock:
        job = _jobs.pop(job_id, None)
    return bool(job and job.get("cancelled"))


def active_jobs() -> list[dict]:
    """What is running right now, for anything that wants to display it.

    Kept plain and copied rather than handing out the live dicts: the HUD polls
    this from the Qt thread while workers mutate the originals, and a reader
    that can see a job half-updated is a crash waiting for a busy moment.
    """
    now = time.monotonic()
    with _job_lock:
        jobs = list(_jobs.values())
    return [
        {
            "id": j["id"],
            "task": j["task"],
            "project": j["workspace"].name,
            "mode": j["mode"],
            "agent": j.get("agent_label") or j.get("agent", ""),
            "elapsed_s": max(0.0, now - j["started"]),
            "cancelled": bool(j.get("cancelled")),
        }
        for j in sorted(jobs, key=lambda x: x["started"])
    ]


def _describe_job(job: dict) -> str:
    mins = (time.monotonic() - job["started"]) / 60
    return (f"{job['id']}: \"{job['task'][:70]}\" in {job['workspace'].name} "
            f"({job['mode']}, {mins:.0f} min)")


def _match_jobs(hint: str) -> list[dict]:
    """Jobs matching a spoken hint — a job id, a project, or task words."""
    with _job_lock:
        jobs = list(_jobs.values())
    key = re.sub(r"\s+", " ", (hint or "").strip().lower())
    if not key:
        return jobs
    exact = [j for j in jobs if j["id"].lower() == key]
    if exact:
        return exact
    hits = [j for j in jobs
            if key in j["workspace"].name.lower() or key in j["task"].lower()]
    if hits:
        return hits
    words = [w for w in key.split() if len(w) > 2]
    return [j for j in jobs
            if any(w in f"{j['task']} {j['workspace'].name}".lower() for w in words)]


def _worker(cmd: list[str], workspace: Path, mode: str, task: str,
            timeout_s: int, player, agent_key: str, agent_label: str,
            job_id: str) -> None:
    global _last_session_id
    started = time.monotonic()
    stdout = stderr = ""
    timed_out = False

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_subprocess_env(),
        )
    except Exception as e:
        _log(player, f"could not start: {e}")
        journal.record(task, "failed", agent=agent_key, detail=str(e)[:200],
                       workspace=workspace.name, mode=mode)
        _clear_job(job_id)
        _announce(player, (
            f"[AGENT] The agent could not start: {e}. "
            f"Tell the user briefly, in their language."
        ))
        return

    with _job_lock:
        if job_id in _jobs:
            _jobs[job_id]["proc"] = proc

    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except Exception:
            pass
    except Exception as e:
        stderr = str(e)

    elapsed = time.monotonic() - started
    cancelled = _clear_job(job_id)

    if cancelled:
        _log(player, f"cancelled after {elapsed:.0f}s")
        journal.record(task, "cancelled", agent=agent_key,
                       detail="stopped by the user", workspace=workspace.name,
                       mode=mode, duration_s=elapsed)
        return

    if timed_out:
        _log(player, f"timed out after {elapsed:.0f}s")
        journal.record(task, "timeout", agent=agent_key,
                       detail=f"hit the {timeout_s // 60}-minute limit",
                       workspace=workspace.name, mode=mode, duration_s=elapsed)
        _announce(player, (
            f"[AGENT] The agent hit its {timeout_s // 60}-minute "
            f"limit and was stopped. Some of the work may already be saved. "
            f"Tell the user, in their language, in one short sentence."
        ))
        return

    payload = None
    try:
        payload = json.loads(stdout)
    except Exception:
        pass

    if payload is None:
        tail = (stderr or stdout or "").strip().splitlines()[-1:] or ["no output"]
        _log(player, f"failed after {elapsed:.0f}s: {tail[0][:120]}")
        journal.record(task, "failed", agent=agent_key, detail=tail[0][:200],
                       workspace=workspace.name, mode=mode, duration_s=elapsed)
        _announce(player, (
            f"[AGENT] The agent failed: {tail[0][:200]}. "
            f"Tell the user briefly, in their language."
        ))
        return

    result = str(payload.get("result") or "").strip()
    if payload.get("session_id"):
        with _lock:
            _last_session_id = payload["session_id"]

    cost, turns = payload.get("total_cost_usd"), payload.get("num_turns")
    _log(player, f"done in {elapsed:.0f}s · {turns} turns · ~${cost:.3f} of plan usage"
                 if isinstance(cost, (int, float))
                 else f"done in {elapsed:.0f}s")

    if player is not None and result:
        try:
            player.show_content(f"{agent_label} — {mode.upper()}", result)
        except Exception:
            pass

    status = "failed" if payload.get("is_error") else "done"
    journal.record(task, status, agent=agent_key,
                   detail=_summarise(result, 160) if status == "failed" else "",
                   workspace=workspace.name, mode=mode, duration_s=elapsed)
    if status == "done":
        journal.resolve_matching(task)

    extra = ""
    denials = payload.get("permission_denials") or []
    if denials and mode != "auto":
        extra = (f" It was blocked from {len(denials)} action(s) by '{mode}' "
                 f"mode; ask whether to re-run it in auto mode.")
    if payload.get("is_error"):
        extra = " It reported an error." + extra

    _announce(player, (
        f"[AGENT] The agent finished '{task[:90]}' after "
        f"{elapsed:.0f} seconds. Its report: {_summarise(result, 700) or '(nothing)'}"
        f"{extra} Tell the user the outcome in their own language, in one or "
        f"two short sentences. The full text is already on their screen."
    ))


def _too_broad(path: Path) -> bool:
    """True for folders that hold every project rather than being one.

    "project: desktop" is what produced the run that took the session down:
    scanning a whole Desktop is slow, and an edit lands in a guessed copy of
    the file. Better to ask which project than to work in all of them.
    """
    p = path.resolve()
    if p.parent == p:                      # a drive root
        return True
    home = Path.home().resolve()
    return p in {
        home, home / "Desktop", home / "Downloads", home / "Documents",
        home / "Pictures", home / "Videos", home / "Music",
    }


def run(parameters: dict, player=None) -> str:
    global _job_seq
    params = parameters or {}
    action = str(params.get("action", "run")).lower().strip() or "run"

    with _job_lock:
        running = list(_jobs.values())

    # ── status ───────────────────────────────────────────────────────────────
    if action in ("status", "check"):
        if not running:
            return "No agents are running right now."
        lines = "; ".join(_describe_job(j) for j in running)
        return (f"{len(running)} task(s) running: {lines}. "
                f"Tell the user what is in flight, briefly.")

    # ── cancel ───────────────────────────────────────────────────────────────
    if action in ("cancel", "stop", "abort"):
        if not running:
            return "Nothing is running, so there was nothing to cancel."
        hint = str(params.get("project") or params.get("task") or "").strip()

        # "all" has to be read before matching, not after: as a search term it
        # matches no project or task, so the lookup would report "no running
        # task matches 'all'" and stop nothing.
        if hint.lower() in ("all", "everything", "every", "both",
                            "hamısı", "hamisi", "hər ikisi", "her ikisi"):
            targets = running
        else:
            targets = _match_jobs(hint)
            if not targets:
                return (f"No running task matches '{hint}'. In flight: "
                        f"{'; '.join(_describe_job(j) for j in running)}.")
            # Several running and nothing said about which: ask rather than
            # guess — cancelling the wrong one throws away real work.
            if len(targets) > 1 and not hint:
                return (f"{len(targets)} tasks are running: "
                        f"{'; '.join(_describe_job(j) for j in targets)}. "
                        f"Ask the user which one to stop, or say 'all' to stop "
                        f"every one.")
        stopped = []
        for job in targets:
            with _job_lock:
                live = _jobs.get(job["id"])
                if live:
                    live["cancelled"] = True
                    proc = live.get("proc")
                else:
                    proc = None
            if proc:
                _kill_tree(proc)
            stopped.append(job["task"][:60])
        return "Stopped: " + "; ".join(f"'{t}'" for t in stopped) + "."

    # ── run ──────────────────────────────────────────────────────────────────
    if len(running) >= MAX_CONCURRENT:
        return (
            f"{len(running)} tasks are already running, which is the limit. "
            f"In flight: {'; '.join(_describe_job(j) for j in running)}. "
            f"Ask the user whether to wait or cancel one."
        )

    task = str(params.get("task", "")).strip()
    if not task:
        return "No task was given. Ask the user what they want built or fixed."

    mode = str(params.get("mode") or _setting("default_mode", _DEFAULT_MODE)).lower().strip()
    mode = _MODE_ALIASES.get(mode, mode)
    if mode not in _MODES:
        mode = _DEFAULT_MODE

    spec, agent_key, agent_problem = _resolve_agent(str(params.get("agent", "")))
    if agent_problem:
        return agent_problem

    model = str(params.get("model") or _setting("model", "")).lower().strip()
    if model not in spec["models"]:
        model = spec["default_model"]

    try:
        timeout_s = int(float(_setting("timeout_min", _DEFAULT_TIMEOUT_MIN))) * 60
    except (TypeError, ValueError):
        timeout_s = _DEFAULT_TIMEOUT_MIN * 60

    project = str(params.get("project", "")).strip()
    if project:
        workspace, problem = _resolve_project(project)
        if problem:
            return problem
    else:
        workspace = _workspace()
        if not workspace.is_dir():
            return (
                f"The configured workspace folder does not exist: {workspace}. "
                f"Tell the user to set it in Settings, Delegate."
            )

    if _too_broad(workspace):
        recent = ", ".join(list_projects(6)) or "none found"
        return (
            f"'{workspace}' holds every project at once, which makes the agent "
            f"slow and risks editing the wrong copy of a file. Ask the user "
            f"which project they mean. Recently used: {recent}."
        )

    # Two agents writing in one folder is the one combination that cannot be
    # allowed to run in parallel: they read the same files, then each writes
    # back over the other's edits. Reading alongside anything is fine.
    if mode in _WRITING_MODES:
        clash = next((j for j in running
                      if j["workspace"] == workspace
                      and j["mode"] in _WRITING_MODES), None)
        if clash:
            mins = (time.monotonic() - clash["started"]) / 60
            return (
                f"An agent is already editing {workspace.name} "
                f"('{clash['task'][:60]}', {mins:.0f} min in). Two agents "
                f"changing the same folder would overwrite each other. Tell "
                f"the user, and offer to queue this after it, run it read-only, "
                f"or point it at a different project."
            )

    resume = None
    if params.get("continue_previous"):
        with _lock:
            resume = _last_session_id

    try:
        cmd = _build_command(spec, task, mode, model, workspace, resume)
    except FileNotFoundError as e:
        return str(e)

    with _job_lock:
        _job_seq += 1
        job_id = f"t{_job_seq}"
    _log(player, f"[{job_id}] {mode}/{model} in {workspace.name}: {task[:70]}")

    thread = threading.Thread(
        target=_worker,
        args=(cmd, workspace, mode, task, timeout_s, player,
              agent_key, spec["label"], job_id),
        daemon=True,
        name=f"DelegatedTask-{job_id}",
    )
    with _job_lock:
        _jobs[job_id] = {
            "id": job_id, "task": task, "workspace": workspace, "mode": mode,
            "agent": agent_key, "agent_label": spec["label"],
            "started": time.monotonic(), "proc": None,
            "cancelled": False, "thread": thread,
        }
        others = len(_jobs) - 1
    thread.start()

    # Returning now is the whole point: the live session gets its tool_response
    # immediately and stays up while the work carries on.
    alongside = (f" It is running alongside {others} other task(s)."
                 if others else "")
    return (
        f"Started {job_id}: '{task[:80]}' in {workspace.name}, {mode} mode."
        f"{alongside} Tell the user it is working and that you will report "
        f"back when it is done — one short sentence, in their language. Do "
        f"not call this tool again for the same request; other tasks can be "
        f"started straight away."
    )
