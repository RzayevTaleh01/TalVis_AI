# TalVis

TalVis is a voice-first, always-listening desktop AI assistant for Windows (with partial macOS/Linux support). It pairs Google's **Gemini Live** real-time audio model with a PyQt6 "HUD" interface — inspired by J.A.R.V.I.S. — and a large set of local tools that let it actually *do* things on your computer: open apps, control the OS, browse the web, manage files, control games, send messages, and more.

Unlike a simple chatbot, TalVis maintains a persistent voice session, remembers facts about you across restarts, can be woken with a custom wake word ("Hey Jarvis"), and can be controlled remotely from your phone through a built-in encrypted web dashboard.

## Table of Contents

- [Purpose](#purpose)
- [Key Features](#key-features)
- [Architecture Overview](#architecture-overview)
- [Project Structure](#project-structure)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running TalVis](#running-talvis)
- [Key Components](#key-components)
- [Extending TalVis](#extending-talvis)
- [Platform Notes](#platform-notes)

## Purpose

TalVis turns a desktop PC into a spoken-language assistant that:

- Listens continuously (or only after a wake word) and responds with natural speech via Gemini's Live API.
- Executes real actions on the host machine — opening applications, controlling system settings, managing files, searching the web, controlling games (Steam/Epic), sending messages, and more — instead of just describing what it *would* do.
- Remembers durable facts about the user (name, preferences, projects, relationships) in a local JSON store and injects relevant memory into every session.
- Can see the screen or a webcam feed when asked, via on-demand screen/camera capture.
- Recovers gracefully: reconnects sessions transparently, resumes conversations after drops, and can undo its own recent changes.
- Is remotely controllable from a phone browser through a lightweight, encrypted local dashboard.
- Is extensible via a simple plugin system, so new voice-triggered tools can be added without touching the core app.

## Key Features

| Area | Capability |
|---|---|
| **Voice** | Real-time two-way audio conversation via Gemini Live (`gemini-3.1-flash-live-preview`), with live input/output transcription |
| **Wake word** | Optional fully-local, offline "Hey Jarvis" detection (via `openwakeword`) so the mic is not streamed anywhere while asleep |
| **Vision** | On-demand screen capture or webcam capture, described back to you in natural language |
| **Memory** | Long-term personal memory (`memory/long_term.json`) with categories (identity, preferences, projects, relationships, wishes, notes), automatic session summaries, and a `recall_memory` search tool |
| **System control** | Volume, brightness, dark mode, WiFi, window management, keyboard shortcuts, screenshots, lock screen, restart/shutdown (with on-screen confirmation gating) |
| **File management** | List, create, delete, move, copy, rename, read, write, find files, disk usage |
| **Web** | General search, news, deep research, price lookup, and comparison modes via DuckDuckGo/BeautifulSoup |
| **Browser automation** | Playwright-driven browser control |
| **Apps & games** | Launch any installed application; dedicated Steam/Epic Games install/update/status tool |
| **Media** | YouTube playback, video summarization, and trending videos |
| **Communication** | Send messages via WhatsApp/Telegram and similar platforms |
| **Utilities** | Timed reminders (via OS task scheduler), weather reports, flight search, desktop organization/cleanup, live system monitoring (CPU/RAM/GPU/temperature) with voice alerts |
| **Background monitoring** | Track topics (e.g. "AI news") and get a daily spoken alert when something new appears |
| **Undo** | A shared undo stack lets you say "undo" to reverse the assistant's last reversible change |
| **Confirmation gating** | Destructive/irreversible actions (shutdown, restart, WiFi toggle) require an explicit on-screen confirmation the model cannot forge |
| **Remote dashboard** | FastAPI + WebSocket dashboard, AES-256-CBC encrypted, letting you view/control the session and stream your phone's mic from a browser |
| **Plugins** | Drop-in `plugins/*.py` files are auto-discovered at startup — including a bundled plugin that hands multi-step coding tasks off to Claude Code |
| **Theming** | Runtime UI accent-color re-theming (hue-shifted from a single base palette) |

## Architecture Overview

```
 Microphone / Camera
        │
        ▼
 ┌─────────────────┐        Gemini Live API        ┌───────────────────┐
 │   main.py        │ ───── audio + tool calls ───▶ │  Google Gemini     │
 │  TalVisLive      │ ◀──── audio + transcripts ──── │  (Live, real-time) │
 └────────┬─────────┘                                └───────────────────┘
          │ dispatches tool calls
          ▼
 ┌───────────────────────────────────────────────────────────────────┐
 │  core/action_loader.py   →  actions/*.py  (built-in tools)         │
 │  core/plugin_loader.py   →  plugins/*.py  (user/drop-in tools)     │
 └───────────────────────────────────────────────────────────────────┘
          │
          ▼
 ┌─────────────────┐   ┌──────────────────┐   ┌───────────────────────┐
 │ ui.py (PyQt6 HUD)│   │ memory/*         │   │ dashboard/server.py   │
 │ HUD, logs, panels│   │ long-term memory │   │ FastAPI remote control│
 └─────────────────┘   └──────────────────┘   └───────────────────────┘
```

Every spoken exchange flows through `main.py`'s `TalVisLive` class, which owns the Gemini Live session, the audio in/out streams, and tool dispatch. Tools are **not** hardcoded — any file in `actions/` that exports a module-level `TOOL` dict is auto-discovered at startup and registered as a Gemini function the model can call, and any file in `plugins/` with a `PLUGIN` dict works the same way for third-party/optional extensions. This means adding a new voice command is normally a one-file change with no edits to `main.py`.

## Project Structure

```
TalVis/
├── main.py                  # Entry point: Gemini Live session, audio I/O, tool dispatch, briefing/monitor loops
├── ui.py                    # PyQt6 HUD: animated assistant face, logs, settings, file drop zone, camera preview
├── setup.py                 # One-shot installer: pip deps + Playwright browsers, OS-specific notes
├── requirements.txt         # Python dependencies (with platform markers for Windows-only extras)
│
├── actions/                 # Built-in, auto-discovered voice tools (each exposes a `TOOL` dict)
│   ├── open_app.py              # Launch applications/websites
│   ├── computer_settings.py     # Volume, brightness, WiFi, dark mode, window/tab management, etc.
│   ├── computer_control.py      # Low-level input control: type, click, hotkeys, scroll, screenshots
│   ├── file_controller.py       # File/folder CRUD, search, disk usage
│   ├── file_processor.py        # Process an uploaded/dropped file (documents, images, data, etc.)
│   ├── web_search.py            # Search / news / research / price / compare modes
│   ├── browser_control.py       # Playwright-driven browser automation
│   ├── desktop.py               # Wallpaper, desktop organize/clean/stats
│   ├── game_updater.py          # Steam/Epic Games install, update, status
│   ├── youtube_video.py         # Play, summarize, and browse YouTube
│   ├── send_message.py          # WhatsApp/Telegram/etc. messaging
│   ├── reminder.py               # Timed reminders via the OS task scheduler
│   ├── weather_report.py        # Weather lookups
│   ├── flight_finder.py         # Google Flights search
│   ├── system_monitor.py        # CPU/RAM/GPU/temperature status + threshold alerts
│   ├── background_monitor.py    # Daily topic-watching with spoken alerts
│   ├── proactive.py             # Proactive check-in engine
│   ├── screen_processor.py      # Screen/webcam capture for vision tool calls
│   ├── _win_apps.py / _close_app.py  # Windows-specific helpers (not standalone tools; leading `_`)
│   └── ...
│
├── core/                     # Framework internals shared across the app
│   ├── action_loader.py         # Discovers & validates actions/*.py, dispatches tool calls
│   ├── plugin_loader.py         # Discovers & validates plugins/*.py, tracks enable/disable state
│   ├── confirm.py               # UI-issued confirmation gate for irreversible actions
│   ├── undo.py                  # Shared undo stack for reversible actions
│   ├── wake_word.py              # Local, offline "Hey Jarvis" detection (openwakeword)
│   ├── audio_devices.py         # Input/output device resolution
│   ├── installer.py             # First-run dependency auto-installer
│   ├── llm_client.py             # Optional local LLM backend (Ollama / OpenAI-compatible servers)
│   ├── stt.py / tts.py           # Speech-to-text / text-to-speech helpers
│
├── memory/                   # Persistent user memory
│   ├── memory_manager.py        # Load/update/search long-term memory, session summaries
│   ├── config_manager.py        # App settings: voice, devices, plugin config, wake word toggle
│   └── long_term.json           # The actual on-disk memory store (created/updated at runtime)
│
├── plugins/                   # User-extensible, drop-in voice tools
│   ├── _template.py             # Copy-paste starting point for a new plugin
│   └── claude_code.py           # Hands multi-step coding tasks to a local Claude Code CLI session
│
├── dashboard/                 # Remote control web dashboard
│   ├── server.py                 # FastAPI + WebSocket server, AES-256-CBC encrypted session
│   └── static/                   # login.html / app.html / crypto-js.min.js served to the phone browser
│
└── config/
    ├── api_keys.json            # Gemini API key + assistant identity/settings (created on first run)
    └── talvis.ico                # App icon
```

## Installation

### Prerequisites

- Python 3.11+ (project is tested against 3.13)
- A free [Google Gemini API key](https://aistudio.google.com/) (Live API access)
- Windows is the primary target (deepest OS integration); macOS and Linux are supported with reduced native-control features

### Steps

1. **Clone/download the project** and open a terminal in the project root.

2. **Create and activate a virtual environment** (recommended):
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

3. **Run the setup script** — installs Python dependencies (OS-specific extras are filtered automatically via pip markers) and the Playwright browsers used for browser automation:
   ```powershell
   python setup.py
   ```
   This is equivalent to running:
   ```powershell
   pip install -r requirements.txt
   python -m playwright install chromium firefox
   ```

4. **(Windows only)** If `pywin32` does not register correctly, `setup.py` will print a fix-it command. Desktop-shortcut creation and some native integrations depend on it.

5. **(macOS only, optional)** For Safari-based browser automation:
   ```bash
   python -m playwright install webkit
   ```

6. **(Linux only)** A few OS actions (volume, brightness, reminders, opening URLs) shell out to native tools — install what you need:
   - `pulseaudio-utils` (for `pactl`) — volume control
   - `brightnessctl` — brightness control
   - `systemd` (or `at`) — reminders
   - `xdg-utils` (for `xdg-open`) — opening URLs

## Configuration

On first launch, TalVis shows a **setup overlay** asking for your Gemini API key and detected operating system. This is written to `config/api_keys.json`:

```json
{
  "gemini_api_key": "YOUR_GEMINI_API_KEY",
  "os_system": "windows",
  "wake_word_enabled": false,
  "assistant_name": "TalVis"
}
```

You can also create/edit this file manually before the first run to skip the setup screen. Additional runtime settings (voice, input/output audio device, plugin enable/disable state, user's preferred name) are managed through `memory/config_manager.py` and the in-app ⚙ settings panel, not by hand-editing files.

> **Security note:** `config/api_keys.json` holds a live credential. Do not commit it to version control or share it — treat it like any other secret.

Optional: to use a local LLM (Ollama or an OpenAI-compatible server such as LM Studio) instead of/alongside Gemini for certain features, set `"llm_provider"` to `"ollama"` (default, port 11434) or `"openai"` (with `"llm_url"` pointing at your server) in the same config file — see `core/llm_client.py`.

## Running TalVis

```powershell
python main.py
```

On launch, TalVis:

1. Loads your API key, memory, and system prompt.
2. Discovers all valid `actions/*.py` tools and `plugins/*.py` plugins.
3. Opens the PyQt6 HUD window and connects to Gemini Live.
4. Starts listening (or waits for the wake phrase "Hey Jarvis" if wake word is enabled from ⚙ → WAKE WORD).
5. Delivers a short spoken morning briefing (greeting + fetched news) the first time it connects in a session.

Say things like *"open Spotify"*, *"what's on my screen?"*, *"remind me to call mom at 5pm"*, *"search for the best budget laptop"*, or *"remember that my favorite color is blue"* — TalVis will call the matching tool and reply out loud.

Type a command instead of speaking it via the text box in the HUD, or use the **Remote Control** button to get a QR code / link for the phone dashboard.

To close TalVis, either say "close TalVis" / "shut yourself down" or close the window — a brief goodbye and a session summary are saved before exit.

## Key Components

- **`main.py` — `TalVisLive`**: Owns the entire session lifecycle — building the Gemini `LiveConnectConfig` (system prompt, memory, tool declarations, voice, session resumption, context-window compression), streaming mic audio in, playing response audio out, dispatching tool calls, running the morning briefing, background system/topic monitors, and session-summary persistence. A small set of tools (vision capture, memory writes, monitor management, shutdown) are handled inline here because they're tightly coupled to live session state; everything else lives in `actions/`.

- **`ui.py` — `TalVisUI` / `HudCanvas`**: The PyQt6 front end — an animated, audio-reactive "arc reactor" HUD (inspired by J.A.R.V.I.S.), a typed conversation log, a settings/plugin panel, a file drop zone, and a camera preview overlay. Also owns runtime UI re-theming (hue-shifting the whole palette from one accent color with no restart required).

- **`core/action_loader.py`**: Scans `actions/*.py` at startup, validates each file's `TOOL` dict (name, description, JSON-schema parameters, handler), and exposes a registry that `main.py` uses to build Gemini's tool declarations and to run a tool by name. Invalid or colliding tools are logged and skipped — never crash startup.

- **`core/plugin_loader.py`**: The same discovery/validation pattern as `action_loader.py`, but for `plugins/*.py` `PLUGIN` dicts, with per-plugin enable/disable state re-read live from config (no restart needed to toggle a plugin).

- **`core/confirm.py`**: A confirmation gate that only the *UI* can satisfy — not the model — used for irreversible actions like shutdown, restart, and toggling WiFi, so the assistant cannot talk itself into confirming its own destructive action.

- **`core/undo.py`**: A shared undo stack. Any action that changes state can push a `(label, reverse_callable)` pair; saying "undo" reverses the most recent one.

- **`core/wake_word.py`**: Fully local/offline wake-word detection ("Hey Jarvis") via `openwakeword`, with zero cost when disabled and zero added mic latency when enabled (inference runs on a separate thread from the real-time audio path).

- **`memory/memory_manager.py` & `memory/long_term.json`**: The long-term memory store — categorized facts about the user, session summaries popped into the next morning's briefing, and a lightweight local search (`recall_memory`) so old facts don't have to live permanently in the system prompt.

- **`dashboard/server.py`**: A FastAPI + WebSocket server providing a phone-accessible, AES-256-CBC-encrypted control panel for the running session (view conversation, send text, stream phone microphone audio).

- **`plugins/claude_code.py`**: A bundled plugin that hands off multi-step *filesystem* development work (reading/editing multiple files, running tests) to a local Claude Code CLI session, keeping a clean division of labor: Gemini Live handles voice and instant actions, Claude Code handles anything that needs a real coding agent loop.

## Extending TalVis

Adding a new voice-triggered capability normally requires **no changes to `main.py`**:

1. **As a built-in action:** create `actions/my_tool.py` exposing a module-level `TOOL` dict (`name`, `description`, `parameters`, `handler`) — see any existing file in `actions/` for the exact shape expected by `core/action_loader.py`.
2. **As a drop-in plugin:** copy `plugins/_template.py`, rename it, and fill in the `PLUGIN` dict and `run()` function — see `core/plugin_loader.py` for discovery rules.

Both are auto-discovered at startup; naming collisions with existing tools (or with the small set of inline tools declared in `main.py`) are rejected and logged rather than silently overriding anything.

## Platform Notes

- **Windows** is the primary, fully-supported platform — native GPU/temperature reads (NVML/WMI), Task Scheduler-based reminders, and the widest set of OS control actions.
- **macOS** support relies on `osascript` and LaunchAgents for native actions; Safari automation needs an extra Playwright browser install.
- **Linux** support relies on standard CLI tools (`pactl`, `brightnessctl`, `systemd`/`at`, `xdg-open`) which must be installed separately per your distribution.
