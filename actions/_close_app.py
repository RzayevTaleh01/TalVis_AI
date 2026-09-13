"""
actions/_close_app.py — close a named application, and nothing else.

WHY THIS EXISTS
    computer_settings' close_app took no arguments at all:

        def close_app():
            if _OS == "Darwin": pyautogui.hotkey("command", "q")
            else:               pyautogui.hotkey("alt", "f4")

    The model passes the app name in `value`, and the dispatcher calls `func()`
    with no arguments, so the name was discarded every single time. What it
    actually did was press Alt+F4 — closing whichever window happened to hold
    focus. That is usually TalVis's own HUD, which is why asking it to close an
    app repeatedly killed the assistant instead.

    The fix is the same shape as the one open_app needed: resolve the spoken
    name against what is really running, act on that specific process, and say
    honestly when there is nothing to act on.

WHAT A MATCH MEANS
    Only processes owning a visible top-level window are considered. That is
    what a person means by "an app" — it excludes services, background helpers
    and the dozens of svchost instances a name could otherwise collide with.

SAFETY
    TalVis never closes itself. Its own PID, its ancestors, and every process
    running the same Python interpreter are excluded before matching, as are
    the shell and session-critical system processes. A close request that
    resolves to one of those matches nothing rather than being obeyed.
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
import time

_OS = platform.system()

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

# Closing any of these logs the user out, kills the desktop, or takes Windows
# down with it. No spoken request should ever reach them.
_NEVER_CLOSE = {
    "explorer.exe", "csrss.exe", "winlogon.exe", "wininit.exe", "services.exe",
    "lsass.exe", "smss.exe", "dwm.exe", "svchost.exe", "system", "registry",
    "fontdrvhost.exe", "sihost.exe", "ctfmon.exe", "runtimebroker.exe",
    "systemsettings.exe", "textinputhost.exe", "searchhost.exe",
    "shellexperiencehost.exe", "startmenuexperiencehost.exe",
}

# Store/UWP apps do not own top-level windows; these processes host them. A
# match on one of these is a match on one window, never on the process.
_WINDOW_HOSTS = {"applicationframehost.exe"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())).strip()


# ── Who must never be touched ────────────────────────────────────────────────

def _path_key(p: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(p))
    except Exception:
        return os.path.normcase(p or "")


# The app's own entry point, used to recognise sibling instances of TalVis.
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SCRIPT = _path_key(os.path.join(_BASE_DIR, "main.py"))
_OWN_PYTHON = _path_key(sys.executable)


def _protected_pids() -> set[int]:
    """This process and its ancestors — the cheap, always-available part."""
    pids: set[int] = {os.getpid()}
    if not _PSUTIL:
        return pids
    try:
        for parent in psutil.Process().parents():
            pids.add(parent.pid)
    except Exception:
        pass
    return pids


def _is_own_process(proc) -> bool:
    """True if `proc` is TalVis — this instance or another one.

    Matching on exe() is not enough and the reason is easy to miss: a venv's
    python.exe is a launcher stub, so psutil reports the *base* interpreter
    (…\\Python313\\python.exe) as the running executable while sys.executable
    inside that same process reports the venv path. The two never compare
    equal, and the HUD process sails straight through the guard.

    The invocation is what identifies us, so match on cmdline: either it was
    started with our interpreter, or it names our main.py.
    """
    try:
        cmdline = proc.cmdline()
    except Exception:
        return False
    if not cmdline:
        return False

    if _path_key(cmdline[0]) == _OWN_PYTHON:
        return True

    cwd = None
    for arg in cmdline[1:]:
        if not arg or arg.startswith("-"):
            continue
        if _path_key(arg) == _MAIN_SCRIPT:
            return True
        # A relative "main.py" only resolves against the process's own cwd.
        if os.path.basename(arg).lower() == "main.py":
            if cwd is None:
                try:
                    cwd = proc.cwd()
                except Exception:
                    cwd = ""
            if cwd and _path_key(os.path.join(cwd, arg)) == _MAIN_SCRIPT:
                return True
    return False


# ── What is actually on screen ───────────────────────────────────────────────

def _visible_windows() -> list[tuple[int, int, str]]:
    """[(hwnd, pid, title)] for visible top-level windows that have a title."""
    if _OS != "Windows":
        return []
    try:
        import win32gui
        import win32process
    except ImportError:
        return []

    found: list[tuple[int, int, str]] = []

    def _collect(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            if win32gui.GetParent(hwnd):
                return                      # child window, not an app window
            title = win32gui.GetWindowText(hwnd) or ""
            if not title.strip():
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            found.append((hwnd, pid, title))
        except Exception:
            pass

    try:
        win32gui.EnumWindows(_collect, None)
    except Exception as e:
        print(f"[close_app] window enumeration failed: {e}")
    return found


def running_apps() -> list[dict]:
    """One entry per closable app: its pid, process name, windows and titles."""
    protected = _protected_pids()
    by_pid: dict[int, dict] = {}

    rejected: set[int] = set()

    for hwnd, pid, title in _visible_windows():
        if pid in protected or pid in rejected:
            continue
        entry = by_pid.get(pid)
        if entry is None:
            name = ""
            if _PSUTIL:
                try:
                    proc = psutil.Process(pid)
                    name = proc.name()
                except Exception:
                    rejected.add(pid)
                    continue
                if _is_own_process(proc):
                    rejected.add(pid)      # TalVis never appears as closable
                    continue
            if name.lower() in _NEVER_CLOSE:
                rejected.add(pid)
                continue
            entry = {"pid": pid, "name": name, "hwnds": [], "titles": []}
            by_pid[pid] = entry
        entry["hwnds"].append(hwnd)
        entry["titles"].append(title)

    return list(by_pid.values())


# ── Matching ─────────────────────────────────────────────────────────────────

def _score(query: str, app: dict) -> int:
    q = _norm(query)
    if not q:
        return 0

    stem = _norm(os.path.splitext(app.get("name") or "")[0])
    if stem:
        if stem == q:
            return 100
        if stem.startswith(q) or q.startswith(stem):
            return 90

    best = 0
    for title in app.get("titles", []):
        t = _norm(title)
        if not t:
            continue
        if t == q:
            best = max(best, 95)
        elif t.endswith(q):
            # Window titles read "<document> - <App Name>", so a match on the
            # trailing group is a match on the application itself.
            best = max(best, 80)
        elif " - " not in title and t.startswith(q):
            # A title with no " - " separator carries no document name, so it
            # is the app announcing itself: "Realtek Audio Console", "Settings".
            best = max(best, 75)
        elif re.search(rf"\b{re.escape(q)}\b", t) or q in t:
            # A hit in the middle of a "<document> - <App>" title is the
            # document, not the app — "Sign in - Claude - Brave" must not make
            # "close Claude" mean "close Brave". Scored below the confident
            # threshold on purpose: it can surface as a suggestion, never act.
            best = max(best, 50)
    return best


def find(query: str, minimum: int = 55) -> dict | None:
    ranked = rank(query, minimum=minimum, limit=1)
    return ranked[0] if ranked else None


def rank(query: str, minimum: int = 55, limit: int = 5) -> list[dict]:
    scored = []
    for app in running_apps():
        s = _score(query, app)
        if s >= minimum:
            scored.append((s, app))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [app for _, app in scored[:limit]]


def open_app_names(limit: int = 8) -> list[str]:
    """Human-readable names of what is currently open, for an honest failure."""
    names = []
    for app in running_apps():
        label = os.path.splitext(app.get("name") or "")[0]
        if label and label not in names:
            names.append(label)
    return names[:limit]


# ── Closing ──────────────────────────────────────────────────────────────────

def _post_close(hwnds: list[int]) -> None:
    try:
        import win32con
        import win32gui
    except ImportError:
        return
    for hwnd in hwnds:
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            continue


def close_match(app: dict, query: str = "", grace: float = 8.0) -> tuple[bool, str]:
    """Ask the app's windows to close, then escalate if it ignores that.

    WM_CLOSE is what clicking the X does, so an app with unsaved work still
    gets its chance to prompt. Only a process that is still alive after the
    grace period is terminated.
    """
    label = os.path.splitext(app.get("name") or "")[0] or f"pid {app['pid']}"
    pid = app["pid"]

    # Checked again here, not just during matching: this is the last point
    # before a window is told to close, and the cost of being wrong is the
    # assistant shutting itself down mid-sentence.
    if pid in _protected_pids():
        return False, f"Refusing to close {label} — that is TalVis itself."
    if _PSUTIL:
        try:
            if _is_own_process(psutil.Process(pid)):
                return False, f"Refusing to close {label} — that is TalVis itself."
        except Exception:
            pass

    # Store apps do not own their own windows: ApplicationFrameHost hosts them
    # all, so one process can be the frame for several unrelated apps. Close
    # only the window that matched, and never terminate the host — doing so
    # would take every other Store app down with it.
    hwnds = app.get("hwnds", [])
    host = (app.get("name") or "").lower() in _WINDOW_HOSTS
    if host and query:
        q = _norm(query)
        targeted = [h for h, t in zip(hwnds, app.get("titles", [])) if q in _norm(t)]
        if targeted:
            hwnds = targeted
        _post_close(hwnds)
        return True, f"Closed {query}."

    _post_close(hwnds)

    if not _PSUTIL:
        return True, f"Asked {label} to close."

    try:
        proc = psutil.Process(pid)
    except Exception:
        return True, f"Closed {label}."

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not proc.is_running():
            return True, f"Closed {label}."
        time.sleep(0.2)

    try:
        proc.terminate()
        proc.wait(timeout=5)
        return True, f"Closed {label}."
    except Exception:
        pass

    # terminate() raising is not proof the app survived: a browser tearing down
    # a dozen child processes routinely outlives the wait and then exits a
    # moment later. Ask the OS what is true instead of trusting the timeout.
    time.sleep(1.0)
    try:
        if not psutil.pid_exists(pid) or not proc.is_running():
            return True, f"Closed {label}."
    except Exception:
        return True, f"Closed {label}."

    return False, (
        f"{label} is still open — it may be showing a dialog asking you to "
        f"save. Deal with that window and ask me again."
    )


def close_by_name(query: str) -> str:
    """Entry point: close the named app. Never closes TalVis. Never guesses."""
    query = (query or "").strip()
    if not query:
        return (
            "Which app should I close? Tell the user you need the app's name — "
            "do not close anything."
        )

    if _OS == "Darwin":
        try:
            result = subprocess.run(
                ["osascript", "-e", f'tell application "{query}" to quit'],
                capture_output=True, timeout=8,
            )
            if result.returncode == 0:
                return f"Closed {query}."
        except Exception:
            pass
        return f"Could not close '{query}' — it may not be running."

    if _OS == "Linux":
        try:
            result = subprocess.run(
                ["wmctrl", "-c", query], capture_output=True, timeout=5
            )
            if result.returncode == 0:
                return f"Closed {query}."
        except Exception:
            pass
        return f"Could not close '{query}' — it may not be running."

    if not _PSUTIL:
        return "psutil is not installed, so I cannot tell which apps are running."

    app = find(query)
    if app:
        ok, message = close_match(app, query=query)
        print(f"[close_app] '{query}' → {app.get('name')} pid={app['pid']} → {message}")
        return message

    loose = rank(query, minimum=40, limit=3)
    if loose:
        names = ", ".join(
            os.path.splitext(a.get("name") or "")[0] for a in loose
        )
        return (
            f"Nothing open matches '{query}'. The closest open apps are: {names}. "
            f"Ask the user which one they meant."
        )

    open_now = open_app_names()
    if open_now:
        return (
            f"'{query}' does not appear to be open, so there was nothing to close. "
            f"Currently open: {', '.join(open_now)}. "
            f"Tell the user it was not running — do not claim you closed it."
        )
    return (
        f"'{query}' is not running, so there was nothing to close. "
        f"Tell the user it was not open — do not claim you closed it."
    )
