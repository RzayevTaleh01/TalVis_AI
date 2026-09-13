import re
import time
import subprocess
import platform
import shutil

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

_SYSTEM = platform.system()

# Windows-only: the index of what is actually installed (Start Menu + registry).
# Imported lazily-but-eagerly so the 0.65 s enumeration happens at startup in a
# background thread, not on the first voice command.
_win_apps = None
if _SYSTEM == "Windows":
    try:
        from . import _win_apps as _win_apps_mod
        _win_apps = _win_apps_mod
        _win_apps.warm_async()
    except Exception as _e:              # pragma: no cover - defensive
        print(f"[open_app] Windows app index unavailable: {_e}")

# Typing the app name into the Start Menu search box and pressing Enter used to
# be the fallback for everything. It is off by default now: it cannot tell
# success from failure, so it returned True unconditionally and made TalVis
# report "Opened X" after Enter landed on whatever the search had highlighted.
# Set to True to restore the old behaviour.
ALLOW_START_MENU_TYPING = False

_APP_ALIASES: dict[str, dict[str, str]] = {

    "chrome":             {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "google chrome":      {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "firefox":            {"Windows": "firefox",                 "Darwin": "Firefox",              "Linux": "firefox"},
    "edge":               {"Windows": "msedge",                  "Darwin": "Microsoft Edge",       "Linux": "microsoft-edge"},
    "brave":              {"Windows": "brave",                   "Darwin": "Brave Browser",        "Linux": "brave-browser"},
    "safari":             {"Windows": "msedge",                  "Darwin": "Safari",               "Linux": "firefox"},
    "opera":              {"Windows": "opera",                   "Darwin": "Opera",                "Linux": "opera"},
    "whatsapp":           {"Windows": "WhatsApp",                "Darwin": "WhatsApp",             "Linux": "whatsapp"},
    "telegram":           {"Windows": "Telegram",                "Darwin": "Telegram",             "Linux": "telegram"},
    "discord":            {"Windows": "Discord",                 "Darwin": "Discord",              "Linux": "discord"},
    "slack":              {"Windows": "Slack",                   "Darwin": "Slack",                "Linux": "slack"},
    "zoom":               {"Windows": "Zoom",                    "Darwin": "zoom.us",              "Linux": "zoom"},
    "teams":              {"Windows": "msteams",                 "Darwin": "Microsoft Teams",      "Linux": "teams"},
    "skype":              {"Windows": "skype",                   "Darwin": "Skype",                "Linux": "skype"},
    "signal":             {"Windows": "signal",                  "Darwin": "Signal",               "Linux": "signal"},
    "spotify":            {"Windows": "Spotify",                 "Darwin": "Spotify",              "Linux": "spotify"},
    "vlc":                {"Windows": "vlc",                     "Darwin": "VLC",                  "Linux": "vlc"},
    "netflix":            {"Windows": "Netflix",                 "Darwin": "Netflix",              "Linux": "firefox"},
    "vscode":             {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "visual studio code": {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "code":               {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "terminal":           {"Windows": "wt",                      "Darwin": "Terminal",             "Linux": "x-terminal-emulator"},
    "cmd":                {"Windows": "cmd.exe",                 "Darwin": "Terminal",             "Linux": "bash"},
    "powershell":         {"Windows": "powershell.exe",          "Darwin": "Terminal",             "Linux": "bash"},
    "postman":            {"Windows": "Postman",                 "Darwin": "Postman",              "Linux": "postman"},
    "git":                {"Windows": "git-bash",                "Darwin": "Terminal",             "Linux": "bash"},
    "figma":              {"Windows": "Figma",                   "Darwin": "Figma",                "Linux": "figma"},
    "blender":            {"Windows": "blender",                 "Darwin": "Blender",              "Linux": "blender"},
    "word":               {"Windows": "winword",                 "Darwin": "Microsoft Word",       "Linux": "libreoffice --writer"},
    "excel":              {"Windows": "excel",                   "Darwin": "Microsoft Excel",      "Linux": "libreoffice --calc"},
    "powerpoint":         {"Windows": "powerpnt",                "Darwin": "Microsoft PowerPoint", "Linux": "libreoffice --impress"},
    "libreoffice":        {"Windows": "soffice",                 "Darwin": "LibreOffice",          "Linux": "libreoffice"},
    "notepad":            {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "textedit":           {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "explorer":           {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "file explorer":      {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "finder":             {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "task manager":       {"Windows": "taskmgr.exe",             "Darwin": "Activity Monitor",     "Linux": "gnome-system-monitor"},
    "settings":           {"Windows": "ms-settings:",            "Darwin": "System Preferences",   "Linux": "gnome-control-center"},
    "calculator":         {"Windows": "calc.exe",                "Darwin": "Calculator",           "Linux": "gnome-calculator"},
    "paint":              {"Windows": "mspaint.exe",             "Darwin": "Preview",              "Linux": "gimp"},
    "instagram":          {"Windows": "Instagram",               "Darwin": "Instagram",            "Linux": "firefox"},
    "tiktok":             {"Windows": "TikTok",                  "Darwin": "TikTok",               "Linux": "firefox"},
    "notion":             {"Windows": "Notion",                  "Darwin": "Notion",               "Linux": "notion"},
    "obsidian":           {"Windows": "Obsidian",                "Darwin": "Obsidian",             "Linux": "obsidian"},
    "capcut":             {"Windows": "CapCut",                  "Darwin": "CapCut",               "Linux": "capcut"},
    "steam":              {"Windows": "steam",                   "Darwin": "Steam",                "Linux": "steam"},
    "epic":               {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
    "epic games":         {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
}


def _normalize(raw: str) -> str:
    key = raw.lower().strip()

    if key in _APP_ALIASES:
        return _APP_ALIASES[key].get(_SYSTEM, raw)

    for alias_key, os_map in _APP_ALIASES.items():
        if alias_key in key or key in alias_key:
            return os_map.get(_SYSTEM, raw)

    return raw  

def _is_uri(value: str) -> bool:
    """'ms-settings:' is a shell URI; 'C:\\Program Files\\x.exe' is not."""
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]+:", value))


def _popen(target: str) -> bool:
    try:
        subprocess.Popen(
            [target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.0)
        return True
    except Exception as e:
        print(f"[open_app] launch failed ({target}): {e}")
        return False


def _launch_windows(app_name: str, original: str = "") -> bool:
    """Resolve the name against what Windows knows is installed, cheapest first.

    Every tier is a lookup rather than a guess, so a False here means the app
    genuinely could not be found — which is what lets open_app report honestly
    instead of claiming success after a blind keystroke.
    """
    def _start_menu(candidates, minimum: int) -> bool:
        if not _win_apps:
            return False
        seen = set()
        for candidate in candidates:
            key = (candidate or "").lower().strip()
            if not key or key in seen:
                continue
            seen.add(key)
            hit = _win_apps.find(candidate, minimum=minimum)
            if not hit:
                continue
            name, aumid = hit
            print(f"[open_app] Start Menu: '{candidate}' → '{name}' [{aumid}]")
            if _win_apps.launch_aumid(aumid):
                time.sleep(1.0)
                return True
        return False

    # 1 — a shell URI ('ms-settings:'), unambiguous and instant
    if _is_uri(app_name):
        try:
            subprocess.Popen(f'start "" "{app_name}"', shell=True)
            time.sleep(0.8)
            return True
        except Exception as e:
            print(f"[open_app] URI launch failed ({app_name}): {e}")

    # 2 — the Start Menu's own list, but only on a confident match: an exact
    #     name, a prefix, or a whole word. This outranks PATH deliberately —
    #     PATH is full of shims that answer to the wrong name (Cursor installs
    #     a 'code' command, so "open Visual Studio Code" via PATH opens Cursor).
    #     The ORIGINAL spoken name goes first: the alias map rewrites
    #     "powerpoint" to "powerpnt", which matches no display name anywhere.
    if _start_menu((original, app_name), minimum=70):
        return True

    # 3 — a real executable on PATH ('cmd.exe', 'wt', 'soffice')
    exe = shutil.which(app_name) or shutil.which(app_name.split(".")[0])
    if exe and _popen(exe):
        return True

    # 4 — registered in App Paths but absent from PATH: Office, Chrome, Firefox
    if _win_apps:
        resolved = _win_apps.resolve_exe(app_name)
        if resolved:
            print(f"[open_app] App Paths: '{app_name}' → {resolved}")
            if _popen(resolved):
                return True

    # 5 — Start Menu again, now accepting a loose match
    if _start_menu((original, app_name), minimum=40):
        return True

    # 6 — the old keyboard hack, off unless explicitly re-enabled
    if ALLOW_START_MENU_TYPING:
        try:
            import pyautogui
            pyautogui.PAUSE = 0.1
            pyautogui.press("win")
            time.sleep(0.7)
            pyautogui.write(app_name, interval=0.05)
            time.sleep(0.9)
            pyautogui.press("enter")
            time.sleep(2.5)
            return True
        except Exception as e:
            print(f"[open_app] Start Menu search failed: {e}")

    return False


def _launch_macos(app_name: str, original: str = "") -> bool:

    try:
        result = subprocess.run(
            ["open", "-a", app_name],
            capture_output=True, timeout=8
        )
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["open", "-a", f"{app_name}.app"],
            capture_output=True, timeout=8
        )
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    binary = shutil.which(app_name) or shutil.which(app_name.lower())
    if binary:
        try:
            subprocess.Popen(
                [binary],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.0)
            return True
        except Exception:
            pass

    try:
        import pyautogui
        pyautogui.hotkey("command", "space")
        time.sleep(0.6)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.8)
        pyautogui.press("enter")
        time.sleep(1.5)
        return True
    except Exception as e:
        print(f"[open_app] Spotlight failed: {e}")

    return False


_LINUX_TERMINAL_FALLBACKS = [
    "x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal",
    "xterm", "lxterminal", "mate-terminal", "tilix", "alacritty", "kitty",
]

def _launch_linux(app_name: str, original: str = "") -> bool:

    # terminal emulators: try common ones in order
    if app_name in ("x-terminal-emulator", "gnome-terminal", "terminal"):
        for term in _LINUX_TERMINAL_FALLBACKS:
            if shutil.which(term):
                try:
                    subprocess.Popen([term], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    time.sleep(1.0)
                    return True
                except Exception:
                    continue

    binary = (
        shutil.which(app_name) or
        shutil.which(app_name.lower()) or
        shutil.which(app_name.lower().replace(" ", "-")) or
        shutil.which(app_name.lower().replace(" ", "_"))
    )
    if binary:
        try:
            subprocess.Popen(
                [binary],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.0)
            return True
        except Exception:
            pass

    try:
        subprocess.run(
            ["xdg-open", app_name],
            capture_output=True, timeout=5
        )
        return True
    except Exception:
        pass

    for desktop_name in [
        app_name.lower(),
        app_name.lower().replace(" ", "-"),
        app_name.lower().replace(" ", ""),
    ]:
        try:
            result = subprocess.run(
                ["gtk-launch", desktop_name],
                capture_output=True, timeout=5
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass

    return False


_OS_LAUNCHERS = {
    "Windows": _launch_windows,
    "Darwin":  _launch_macos,
    "Linux":   _launch_linux,
}

def open_app(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    app_name = (parameters or {}).get("app_name", "").strip()

    if not app_name:
        return "No application name provided."

    launcher = _OS_LAUNCHERS.get(_SYSTEM)
    if launcher is None:
        return f"Unsupported operating system: {_SYSTEM}"

    normalized = _normalize(app_name)
    print(f"[open_app] Launching: '{app_name}' → '{normalized}' ({_SYSTEM})")

    if player:
        player.write_log(f"[open_app] {app_name}")

    try:
        if launcher(normalized, app_name):
            return f"Opened {app_name}."
        if normalized.lower() != app_name.lower():
            if launcher(app_name, app_name):
                return f"Opened {app_name}."

        # Nothing matched. Every tier above was a real lookup, so this is a
        # genuine "not installed" — say so, and offer the closest names the
        # Start Menu does have rather than leaving the model to guess.
        if _win_apps:
            close = _win_apps.suggestions(app_name)
            if close:
                return (
                    f"I could not find '{app_name}' installed. "
                    f"The closest installed apps are: {', '.join(close)}. "
                    f"Ask the user which one they meant."
                )
        return (
            f"'{app_name}' does not appear to be installed on this computer. "
            f"Tell the user it is not installed — do not claim it was opened."
        )
    except Exception as e:
        print(f"[open_app] Error: {e}")
        return f"Failed to open {app_name}: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "open_app",
    "description": "Opens any application on the computer. Use this whenever the user asks to open, launch, or start any app, website, or program. Always call this tool — never just say you opened it.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "app_name": {
                "type": "STRING",
                "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')"
            }
        },
        "required": [
            "app_name"
        ]
    },
    "handler": open_app,
}
