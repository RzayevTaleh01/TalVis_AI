"""
actions/_win_apps.py — what Windows itself knows is installed.

WHY THIS EXISTS
    open_app's Windows path only ever asked `shutil.which()`, which searches
    PATH. Almost nothing a person actually opens by voice is on PATH — Chrome,
    WhatsApp, Telegram, PowerPoint, Spotify, Discord are all missing from it.
    So virtually every request fell through to the last resort:

        pyautogui.press("win"); pyautogui.write(name); pyautogui.press("enter")

    That types the name into the Start Menu search box. It is slow (~4 s of
    sleeps), it seizes the keyboard, it breaks if any window steals focus
    mid-type, and — worst — it returns True unconditionally, so TalVis reports
    "Opened X" after pressing Enter on whatever the search happened to
    highlight. The log shows exactly that: "Opened Cloud." for an app that was
    never opened.

    This module replaces the guess with a lookup. Two sources, both authoritative:

      • shell:AppsFolder — every entry the Start Menu shows, Win32 *and*
        Store/UWP apps, each with its AppUserModelID. Enumerated over COM via
        pywin32 (already a dependency). ~0.65 s, cached for the process.

      • App Paths registry — full executable paths for programs that register
        themselves but are not on PATH (Office, Chrome, Firefox). Instant.

    Launching an AppUserModelID through `explorer.exe shell:AppsFolder\\<id>` is
    exactly what clicking the Start Menu tile does. No keyboard, no search box,
    no ambiguity about what got opened.

Nothing here raises: every entry point returns None / False / [] on failure so
open_app can fall through to its next strategy.
"""
from __future__ import annotations

import re
import subprocess
import threading
import winreg

# ── Index of installed apps (display name → AppUserModelID) ──────────────────

_index_lock = threading.Lock()
_index_cache: list[tuple[str, str]] | None = None

# Start Menu folders are full of things that are not the app: its uninstaller,
# its release notes, a shortcut to the vendor's website. A query like "chrome"
# must never land on "Uninstall Chrome", so these are pushed to the bottom
# rather than removed — occasionally one of them is genuinely what was asked for.
_JUNK_MARKERS = (
    "uninstall", "readme", "read me", "documentation", "release notes",
    "changelog", "manual", "web site", "website", "help", "support",
    "license", "licence", "repair", "remove ",
)


def _enumerate_appsfolder() -> list[tuple[str, str]]:
    """(display_name, AppUserModelID) for every Start Menu app. [] on failure."""
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return []

    # open_app runs inside a thread-pool executor, so COM must be initialised on
    # THIS thread. Balanced in the finally block — leaking an apartment per call
    # would eventually wedge the process.
    try:
        pythoncom.CoInitialize()
    except Exception:
        pass
    try:
        shell = win32com.client.Dispatch("Shell.Application")
        folder = shell.NameSpace("shell:AppsFolder")
        if folder is None:
            return []
        out: list[tuple[str, str]] = []
        for item in folder.Items():
            try:
                name = str(item.Name).strip()
                aumid = str(item.Path).strip()
                if name and aumid:
                    out.append((name, aumid))
            except Exception:
                continue
        return out
    except Exception as e:
        print(f"[win_apps] AppsFolder enumeration failed: {e}")
        return []
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def index(refresh: bool = False) -> list[tuple[str, str]]:
    """Cached app index. First call costs ~0.65 s; warm_async() hides that."""
    global _index_cache
    with _index_lock:
        if _index_cache is None or refresh:
            _index_cache = _enumerate_appsfolder()
        return _index_cache


def warm_async() -> None:
    """Build the index in the background so the first voice command is instant."""
    if _index_cache is not None:
        return
    threading.Thread(target=index, daemon=True, name="WinAppIndexWarm").start()


# ── Matching ─────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s.lower())).strip()


def _score(query: str, name: str) -> int:
    """0 = no match. Higher is better. Speech gives us loose names ('powerpoint'
    for 'PowerPoint', 'chrome' for 'Google Chrome'), so partial credit matters."""
    q, n = _norm(query), _norm(name)
    if not q or not n:
        return 0

    if q == n:
        s = 100
    elif n.startswith(q):
        s = 85
    elif re.search(rf"\b{re.escape(q)}\b", n):
        s = 70
    elif q in n:
        s = 55
    else:
        q_tokens, n_tokens = q.split(), n.split()
        if all(t in n_tokens for t in q_tokens):
            s = 60
        elif all(any(w.startswith(t) for w in n_tokens) for t in q_tokens):
            s = 45
        else:
            return 0

    if any(j in n for j in _JUNK_MARKERS):
        s -= 45
    return s


def find(query: str, minimum: int = 40) -> tuple[str, str] | None:
    """Best (name, aumid) for a spoken app name, or None."""
    ranked = rank(query, minimum=minimum, limit=1)
    return ranked[0] if ranked else None


def rank(query: str, minimum: int = 40, limit: int = 5) -> list[tuple[str, str]]:
    """Best matches, strongest first. Used for 'did you mean' when nothing hits."""
    if not query or not query.strip():
        return []
    scored = []
    for name, aumid in index():
        s = _score(query, name)
        if s >= minimum:
            # Shorter name wins a tie: "Chrome" beats "Chrome Beta" for "chrome".
            scored.append((s, -len(name), name, aumid))
    scored.sort(reverse=True)
    return [(name, aumid) for _, _, name, aumid in scored[:limit]]


def suggestions(query: str, limit: int = 3) -> list[str]:
    """Loosely-matching installed app names, for an honest failure message."""
    return [name for name, _ in rank(query, minimum=30, limit=limit)]


# ── Launching ────────────────────────────────────────────────────────────────

def launch_aumid(aumid: str) -> bool:
    """Open an app by AppUserModelID — the Start Menu tile's own mechanism."""
    try:
        subprocess.Popen(
            ["explorer.exe", f"shell:AppsFolder\\{aumid}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        print(f"[win_apps] AUMID launch failed ({aumid}): {e}")
        return False


# ── App Paths registry ───────────────────────────────────────────────────────

_paths_lock = threading.Lock()
_paths_cache: dict[str, str] | None = None


def _read_app_paths() -> dict[str, str]:
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    out: dict[str, str] = {}
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            root_key = winreg.OpenKey(root, key_path)
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(root_key)[0]
            for i in range(count):
                try:
                    sub = winreg.EnumKey(root_key, i)
                    with winreg.OpenKey(root_key, sub) as sub_key:
                        value = winreg.QueryValue(sub_key, None)
                    if value:
                        # HKLM is scanned first; setdefault keeps it winning so a
                        # per-user stub cannot shadow a machine-wide install.
                        out.setdefault(sub.lower(), value.strip('"'))
                except OSError:
                    continue
        finally:
            root_key.Close()
    return out


def app_paths() -> dict[str, str]:
    global _paths_cache
    with _paths_lock:
        if _paths_cache is None:
            try:
                _paths_cache = _read_app_paths()
            except Exception as e:
                print(f"[win_apps] App Paths read failed: {e}")
                _paths_cache = {}
        return _paths_cache


def resolve_exe(name: str) -> str | None:
    """Full path for a registered executable not on PATH ('winword' → Office)."""
    if not name:
        return None
    table = app_paths()
    key = name.lower().strip().strip('"')
    return table.get(key) or table.get(f"{key}.exe")
