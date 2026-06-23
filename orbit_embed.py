"""
ORBIT EMBED — Lightweight offline HTML app shell
Built with PyQt6 + QtWebEngine (Chromium). Single file, no build step.

Designed to be bundled with apps that render their UI in HTML and need a
consistent, private, secure, offline-only host window — not a general browser.

WHAT IT IS:
  · A chromium shell that serves your HTML app from a local directory via
    the custom app:// scheme (no file:// path leakage, proper CORS, MIME types)
  · A two-way JS↔Python bridge so your HTML can call Python and vice-versa
  · Always-offline: all http/https requests are hard-blocked by default;
    only app://, file://, data:, blob:, about: and qrc: pass through
  · Always private: off-the-record profile — nothing written to disk ever
  · Security-hardened WebEngine settings

WHAT IT IS NOT:
  · A general-purpose browser (no ad-block, no bookmarks, no search, no VPN)
  · A sandboxed renderer (the app:// root can read the full directory tree)

USAGE (CLI):
    python orbit_embed.py [options]

    --root DIR          Root directory served as app:// (default: dir of this script)
    --entry FILE        Entry HTML relative to root   (default: index.html)
    --title TEXT        Window title                  (default: App)
    --width N           Window width                  (default: 1280)
    --height N          Window height                 (default: 820)
    --frameless         Frameless window
    --no-tabs           Single-tab mode (no tab bar shown)
    --no-nav            Hide navigation bar (address bar + back/fwd/reload)
    --allow-host HOST   Allow http/https to this host:port (e.g. localhost:3000)
                        Repeat for multiple: --allow-host a --allow-host b
    --kiosk             Shorthand for --frameless --no-nav --no-tabs
    --dev               Dev mode: F12 DevTools, standard context menu, hot-reload
    --address-bar-mode  url (default) | title | hidden
    --log PATH          Log file path. Pass "none" to disable. (default: ~/orbit_embed.log)
    --quiet             Suppress all stdout output
    --verbose           Log debug-level messages (file-system events, etc.)

USAGE (embed as library):
    from orbit_embed import OrbitEmbed, OrbitBridge
    from PyQt6.QtWidgets import QApplication

    class MyBridge(OrbitBridge):
        def handle_invoke(self, method: str, args: list):
            if method == "greet":
                return f"Hello, {args[0]}!"

    app = QApplication(sys.argv)
    win = OrbitEmbed(
        root="/path/to/my/app",
        entry="index.html",
        bridge=MyBridge(),
        title="My App",
        show_tabs=True,
        show_nav=True,
    )
    win.show()
    app.exec()

JS BRIDGE API (inside your HTML):
    <!-- Injected automatically — do not include manually -->
    window.__orbitReady(function(orbit) {
        // Call Python:
        orbit.invoke("myMethod", ["arg1", 42], function(result) {
            console.log("Python returned:", result);
        });

        // Listen for Python→JS events:
        orbit.onEvent(function(name, data) {
            if (name === "update") { ... }
        });
    });

    // Or use the promise API:
    const result = await window.orbitInvoke("myMethod", ["arg1", 42]);

PYTHON→JS events:
    # From any Python code after window is built:
    win.emit_event("update", {"count": 42})
    # Or via the bridge reference:
    bridge.emit_event("update", {"count": 42})

    # Built-in events fired automatically:
    #   networkLocked     {reason: "idle"|"timeout"|"manual"}
    #   fileDrop          {files: [{path, name, size, modified, isDir}]}
    #   contextMenu       {x, y, href?, selectedText?, mediaType?}
    #   downloadStarted   {file}
    #   downloadFinished  {file, status, activeDownloads}
    #   rendererCrash     {}
    #   minimizedToTray   {}
    #   instanceRaised    {}   (second instance launched without --url)
    #   hotReload         {path}  (dev mode only)
    #   protocolActivated {url}   (launched via OS protocol handler or --url)

OS PROTOCOL HANDLER:
    # Register myapp:// so browsers/OS can launch this script:
    from orbit_embed import register_protocol_handler
    register_protocol_handler("myapp", title="My App")

    # Or from the command line:
    python orbit_embed.py --register-protocol myapp
    python orbit_embed.py --unregister-protocol myapp

    # In your HTML app, listen for the activation:
    orbit.onEvent(function(name, data) {
        if (name === "protocolActivated") {
            console.log("Opened via:", data.url);  // "myapp://open?doc=123"
        }
    });

DEPENDENCIES:
    PyQt6 >= 6.11.1  (pip install PyQt6 PyQt6-WebEngine PyQt6-Qt6)
    PyQt6-WebEngine must be installed separately on some platforms.
    No other runtime dependencies.
"""

from __future__ import annotations

import sys
import os
import json
import mimetypes
import argparse
import traceback
import threading
import time as _time
import base64
import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote

# ── Logging ───────────────────────────────────────────────────────────────────

_LOG_PATH:    Path = Path.home() / "orbit_embed.log"
_LOG_QUIET:   bool = False   # set by --quiet
_LOG_VERBOSE: bool = False   # set by --verbose


def _log(msg: str, level: str = "info"):
    """
    Write a log line to the log file and optionally to stdout.

    level — "info" (default) | "debug" (only printed when --verbose is set)
    """
    if level == "debug" and not _LOG_VERBOSE:
        return
    line = msg if msg.endswith("\n") else msg + "\n"
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
    except Exception:
        pass
    if not _LOG_QUIET:
        print(msg, flush=True)

def _excepthook(exc_type, exc_value, exc_tb):
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    _log("\n" + "═" * 60 + "\nORBIT EMBED CRASH\n" + "═" * 60 + "\n" + msg)
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = _excepthook
# NOTE: Python version is logged in main() after _LOG_PATH is configured.
# A small amount of early output (dependency check, Qt path warnings) may go
# to the default ~/orbit_embed.log even when --log is overridden.  This is
# acceptable — it ensures import-time errors are never silently lost.

# ── Dynamic Qt path resolution (no hardcoded venv/install paths) ──────────────
#
# Finds PyQt6's own Qt binaries from wherever PyQt6 is installed — works with
# pip installs, conda, venvs, system Python, bundled distributions.
# This replaces the hardcoded C:\Qt\6.7.3\ block in the full Orbit browser.

def _setup_qt_paths():
    """
    Locate the Qt runtime that PyQt6 was built against and set the environment
    variables that QtWebEngineProcess needs to find its resources/locales/plugins.
    Must be called BEFORE any PyQt6 import.
    """
    try:
        import importlib.util
        spec = importlib.util.find_spec("PyQt6")
        if spec is None:
            return
        pkg_dir = Path(spec.submodule_search_locations[0])

        if sys.platform == "win32":
            qt_bin = pkg_dir / "Qt6" / "bin"
            if qt_bin.is_dir():
                os.add_dll_directory(str(qt_bin))
                os.environ["PATH"] = str(qt_bin) + os.pathsep + os.environ.get("PATH", "")
            qt_plugins = pkg_dir / "Qt6" / "plugins"
            if qt_plugins.is_dir():
                os.environ.setdefault("QT_PLUGIN_PATH", str(qt_plugins))
            wep = qt_bin / "QtWebEngineProcess.exe"
            if wep.is_file():
                os.environ.setdefault("QTWEBENGINEPROCESS_PATH", str(wep))
            res = pkg_dir / "Qt6" / "resources"
            if res.is_dir():
                os.environ.setdefault("QTWEBENGINE_RESOURCES_PATH", str(res))
            loc = pkg_dir / "Qt6" / "translations" / "qtwebengine_locales"
            if loc.is_dir():
                os.environ.setdefault("QTWEBENGINE_LOCALES_PATH", str(loc))
            os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
                "--enable-features=MediaFoundationH264Decoding,"
                "MediaFoundationH264Encoding,"
                "MediaFoundationClearPlayback"
            )
        else:
            # macOS / Linux: Qt ships inside the PyQt6 package
            qt_lib = pkg_dir / "Qt6" / "lib"
            if qt_lib.is_dir():
                existing = os.environ.get("LD_LIBRARY_PATH", "")
                os.environ["LD_LIBRARY_PATH"] = str(qt_lib) + (":" + existing if existing else "")
    except Exception as e:
        _log(f"[orbit_embed] Qt path setup warning: {e}")

_setup_qt_paths()

# ── Pre-flight dependency check ───────────────────────────────────────────────
#
# Gives a clear, actionable error if PyQt6 or its sub-packages are missing,
# rather than a cryptic ModuleNotFoundError deep inside the import block.

def _check_dependencies():
    missing = []
    partial = []

    # Check base PyQt6
    import importlib.util
    if importlib.util.find_spec("PyQt6") is None:
        missing.append("PyQt6")
    else:
        # PyQt6 present — check required sub-modules
        for sub in ("PyQt6.QtCore", "PyQt6.QtWidgets", "PyQt6.QtGui",
                    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets"):
            if importlib.util.find_spec(sub) is None:
                partial.append(sub)

    if missing or partial:
        lines = [
            "",
            "═" * 60,
            "  ORBIT EMBED — Missing dependencies",
            "═" * 60,
        ]
        if missing:
            lines += [
                "",
                "  PyQt6 is not installed in this Python environment.",
                f"  Python: {sys.executable}",
                "",
                "  Install with:",
                "    pip install \"PyQt6>=6.11.1\" \"PyQt6-WebEngine>=6.11.1\" \"PyQt6-Qt6>=6.11.1\"",
                "",
                "  If you use a virtual environment, activate it first.",
            ]
        elif partial:
            lines += [
                "",
                "  PyQt6 is installed but some sub-packages are missing:",
            ]
            for p in partial:
                lines.append(f"    missing: {p}")
            lines += [
                "",
                "  Fix with:",
                "    pip install --upgrade \"PyQt6>=6.11.1\" \"PyQt6-WebEngine>=6.11.1\" \"PyQt6-Qt6>=6.11.1\"",
            ]
        lines += [
            "",
            f"  Python executable: {sys.executable}",
            f"  Python version:    {sys.version.split()[0]}",
            "═" * 60,
            "",
        ]
        msg = "\n".join(lines)
        _log(msg)
        # Also try to show a simple Tk message box if Tk is available,
        # so users who double-clicked the script see something on screen.
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            install_cmd = 'pip install "PyQt6>=6.11.1" "PyQt6-WebEngine>=6.11.1" "PyQt6-Qt6>=6.11.1"'
            messagebox.showerror(
                "Orbit Embed — Missing dependencies",
                f"PyQt6 is not installed in this Python environment.\n\n"
                f"Run this command to install it:\n\n  {install_cmd}\n\n"
                f"Python: {sys.executable}"
            )
            root.destroy()
        except Exception:
            pass   # Tk not available — log output is sufficient
        sys.exit(1)

_check_dependencies()

# ── PyQt6 imports ─────────────────────────────────────────────────────────────

from PyQt6.QtCore import (
    QUrl, Qt, pyqtSignal, pyqtSlot, QSize, QObject, QTimer,
    QByteArray, QEvent, QBuffer, QIODevice, QMimeData, QFileSystemWatcher
)
from PyQt6.QtGui import (
    QColor, QKeySequence, QShortcut, QIcon, QPixmap, QCursor, QMouseEvent,
    QGuiApplication, QScreen, QDesktopServices
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QLabel, QSizePolicy, QStatusBar, QFrame,
    QSystemTrayIcon, QMenu, QFileDialog
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import (
    QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
    QWebEngineUrlRequestInterceptor, QWebEngineUrlRequestInfo,
    QWebEngineScript, QWebEngineUrlSchemeHandler,
    QWebEngineUrlScheme, QWebEngineUrlRequestJob,
    QWebEngineDownloadRequest
)

# QLocalServer/QLocalSocket: used for single-instance enforcement
try:
    from PyQt6.QtNetwork import QLocalServer, QLocalSocket
    _LOCALSERVER_AVAILABLE = True
except ImportError:
    _LOCALSERVER_AVAILABLE = False
    _log("[orbit_embed] PyQt6.QtNetwork not found — single-instance disabled.")

# QWebChannel: optional — bridge is disabled gracefully if not installed
try:
    from PyQt6.QtWebChannel import QWebChannel
    _WEBCHANNEL_AVAILABLE = True
except ImportError:
    _WEBCHANNEL_AVAILABLE = False
    _log("[orbit_embed] PyQt6.QtWebChannel not found — JS bridge disabled. "
         "Install PyQt6 (>=6.11.1) to enable it.")

# ── Custom scheme registration (MUST happen before QApplication) ───────────────
#
# app:// serves the bundled HTML app from a root directory.
# Marked as SecureScheme so the renderer treats it like https for permissions,
# LocalScheme so localStorage / cookies work, CorsEnabled so fetch() works.

_EMBED_SCHEME = b"app"

def _register_app_scheme():
    s = QWebEngineUrlScheme(_EMBED_SCHEME)
    s.setFlags(
        # SecureScheme intentionally omitted: marking app:// as a secure context
        # causes Chromium to apply mixed-content blocking against http:// fetch()
        # calls (e.g. to Flask on http://127.0.0.1:PORT), silently failing every
        # API request.  app:// is a local offline shell — it does not need the
        # security guarantees of https:// and should not be treated as one.
        QWebEngineUrlScheme.Flag.LocalScheme
        | QWebEngineUrlScheme.Flag.LocalAccessAllowed
        | QWebEngineUrlScheme.Flag.CorsEnabled
        | QWebEngineUrlScheme.Flag.ContentSecurityPolicyIgnored
    )
    QWebEngineUrlScheme.registerScheme(s)

_register_app_scheme()

# ── Theme engine ─────────────────────────────────────────────────────────────
#
# A Theme is a plain dict of color keys. Only a handful are required;
# everything else is derived so minimal theme.json files work.
#
# Required keys (with defaults shown):
#   bg        #0a0a0f   Window / page background
#   surface   #111118   Toolbar / tab bar / status bar surface
#   border    #1e1e2e   Borders and separators
#   accent    #6c63ff   Primary accent (buttons, focus rings, active tabs)
#   text      #e8e8f0   Primary text
#   muted     #6b6b85   Inactive / secondary text and icons
#   success   #43e8b2   Online, OK, secure states
#   warning   #febc2e   Warnings, HTTP, in-progress
#   error     #ff6584   Errors, blocked
#
# Optional derived keys (computed if absent):
#   hover     surface lightened ~8%
#   pressed   surface lightened ~16%
#   border2   border lightened ~40%  (stronger border for active states)

APP_VERSION = "1.0.0"

# ── Embedded application icon ─────────────────────────────────────────────────
# SVG encoded as base64 so orbit_embed stays a single portable file.
# Used for: window icon, taskbar, Alt+Tab, and system tray.
# Colors match the default theme; icon renders well from 16×16 to 256×256.

_ORBIT_ICON_SVG_B64 = (
    "PHN2ZyB3aWR0aD0iMjU2IiBoZWlnaHQ9IjI1NiIgdmlld0JveD0iMCAwIDI1NiAyNTYiIHhtbG5zPSJodHRwOi8v"
    "d3d3LnczLm9yZy8yMDAwL3N2ZyI+CiAgPCEtLSBCYWNrZ3JvdW5kIGNpcmNsZSAtLT4KICA8Y2lyY2xlIGN4PSIx"
    "MjgiIGN5PSIxMjgiIHI9IjEyMCIgZmlsbD0iIzBhMGEwZiIvPgogIDwhLS0gT3V0ZXIgb3JiaXQgcmluZyAtLT4K"
    "ICA8ZWxsaXBzZSBjeD0iMTI4IiBjeT0iMTI4IiByeD0iOTAiIHJ5PSIzOCIKICAgICAgICAgICBmaWxsPSJub25l"
    "IiBzdHJva2U9IiM2YzYzZmYiIHN0cm9rZS13aWR0aD0iNyIgb3BhY2l0eT0iMC45IgogICAgICAgICAgIHRyYW5z"
    "Zm9ybT0icm90YXRlKC0zMCAxMjggMTI4KSIvPgogIDwhLS0gSW5uZXIgb3JiaXQgcmluZyAtLT4KICA8ZWxsaXBz"
    "ZSBjeD0iMTI4IiBjeT0iMTI4IiByeD0iNTYiIHJ5PSIyMiIKICAgICAgICAgICBmaWxsPSJub25lIiBzdHJva2U9"
    "IiM2YzYzZmYiIHN0cm9rZS13aWR0aD0iNCIgb3BhY2l0eT0iMC40NSIKICAgICAgICAgICB0cmFuc2Zvcm09InJv"
    "dGF0ZSgtMzAgMTI4IDEyOCkiLz4KICA8IS0tIENlbnRyYWwgYm9keSAtLT4KICA8Y2lyY2xlIGN4PSIxMjgiIGN5"
    "PSIxMjgiIHI9IjIyIiBmaWxsPSIjNmM2M2ZmIiBvcGFjaXR5PSIwLjk1Ii8+CiAgPGNpcmNsZSBjeD0iMTI4IiBj"
    "eT0iMTI4IiByPSIxMyIgZmlsbD0iIzBhMGEwZiIvPgogIDwhLS0gU2F0ZWxsaXRlIGRvdCBvbiBvdXRlciByaW5n"
    "IChwb3NpdGlvbmVkIG9uIHRoZSByaW5nKSAtLT4KICA8Y2lyY2xlIGN4PSIxOTUiIGN5PSIxMDgiIHI9IjEwIiBm"
    "aWxsPSIjNDNlOGIyIi8+CiAgPCEtLSBTdWJ0bGUgYm9yZGVyIC0tPgogIDxjaXJjbGUgY3g9IjEyOCIgY3k9IjEy"
    "OCIgcj0iMTE5IiBmaWxsPSJub25lIiBzdHJva2U9IiM2YzYzZmYiIHN0cm9rZS13aWR0aD0iMiIgb3BhY2l0eT0i"
    "MC4yNSIvPgo8L3N2Zz4="
)

def _orbit_icon() -> QIcon:
    """Return the embedded Orbit Embed application icon."""
    svg_bytes = base64.b64decode(_ORBIT_ICON_SVG_B64)
    pm = QPixmap()
    pm.loadFromData(QByteArray(svg_bytes), "SVG")
    return QIcon(pm)


_DEFAULT_THEME: dict[str, str] = {
    "bg":      "#0a0a0f",
    "surface": "#111118",
    "border":  "#1e1e2e",
    "accent":  "#6c63ff",
    "text":    "#e8e8f0",
    "muted":   "#6b6b85",
    "success": "#43e8b2",
    "warning": "#febc2e",
    "error":   "#ff6584",
}

def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{max(0,min(255,r)):02x}{max(0,min(255,g)):02x}{max(0,min(255,b)):02x}"

def _lighten(color: str, amount: int) -> str:
    """Lighten a hex color by adding `amount` (0-255) to each channel."""
    r, g, b = _hex_to_rgb(color)
    return _rgb_to_hex(r + amount, g + amount, b + amount)

def _resolve_theme(partial: dict) -> dict:
    """Merge partial theme over defaults and derive missing computed keys."""
    t = dict(_DEFAULT_THEME)
    t.update({k: v for k, v in partial.items() if isinstance(v, str) and v.startswith("#")})
    t.setdefault("hover",   _lighten(t["surface"], 20))
    t.setdefault("pressed", _lighten(t["surface"], 36))
    t.setdefault("border2", _lighten(t["border"],  40))
    return t

def _build_qss(t: dict, scale: float = 1.0) -> str:
    """
    Generate the full application stylesheet from a resolved theme dict.
    scale — device pixel ratio (1.0 = 96dpi, 2.0 = 192dpi/4K).
    Font sizes, padding, and border radii all scale proportionally so
    the chrome does not appear tiny on HiDPI displays.
    """
    def px(n: float) -> int:
        return max(1, round(n * scale))
    return f"""
QMainWindow, QWidget {{ background-color: {t['bg']}; color: {t['text']}; }}

QPushButton {{
    background: transparent; border: none; color: {t['muted']};
    border-radius: {px(7)}px; padding: {px(4)}px {px(8)}px;
}}
QPushButton:hover   {{ background: {t['hover']};   color: {t['text']}; }}
QPushButton:pressed {{ background: {t['pressed']}; }}
QPushButton:disabled {{ color: {t['border2']}; }}

QLineEdit {{
    background: {t['surface']}; border: 1px solid {t['border']}; border-radius: {px(9)}px;
    color: {t['text']}; padding: {px(5)}px {px(12)}px;
    font-family: 'Courier New', monospace; font-size: {px(13)}px;
    selection-background-color: {t['accent']};
}}
QLineEdit:focus {{ border-color: {t['accent']}; }}

QStatusBar {{
    background: {t['surface']}; color: {t['muted']};
    border-top: 1px solid {t['border']};
    font-family: 'Courier New', monospace; font-size: {px(10)}px;
}}
QStatusBar::item {{ border: none; }}

QScrollBar:vertical {{ background: transparent; width: {px(4)}px; }}
QScrollBar::handle:vertical {{ background: {t['border2']}; border-radius: {px(2)}px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""

# Active theme — module-level so icon helpers can read it without a window ref
_THEME: dict[str, str] = _resolve_theme({})
_DPI_SCALE: float = 1.0   # set in main() after QApplication exists

def _dpi_scale() -> float:
    """Return the device pixel ratio of the primary screen (1.0 on standard, 2.0 on 4K)."""
    app = QApplication.instance()
    if app is None:
        return 1.0
    screen = app.primaryScreen()
    return screen.devicePixelRatio() if screen else 1.0

def _smart_size(screen: QScreen | None, pct: float = 0.85,
                min_w: int = 800, min_h: int = 520) -> tuple[int, int]:
    """
    Return (width, height) as pct% of the screen's available geometry
    (excludes taskbar), clamped to min_w × min_h.
    Falls back to 1280×820 if no screen is available.
    """
    if screen is None:
        return 1280, 820
    geom = screen.availableGeometry()
    w = max(min_w, round(geom.width()  * pct))
    h = max(min_h, round(geom.height() * pct))
    return w, h

# Shortcuts for the most-used colors (updated on every theme change)
def _C(key: str) -> str:
    """Get a color from the active theme."""
    return _THEME.get(key, _DEFAULT_THEME.get(key, "#ffffff"))

# ── SVG Icon System (minimal set) ─────────────────────────────────────────────

_SVG: dict[str, str] = {
    "back":    '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><polyline points="12,4 6,10 12,16" fill="none" stroke="{c}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "forward": '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><polyline points="8,4 14,10 8,16" fill="none" stroke="{c}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "reload":  '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><path d="M15.5 8.5A5.5 5.5 0 1 0 14.2 13" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round"/><polyline points="13.5,6.5 15.5,8.5 17.5,6.5" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "stop":    '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><line x1="5" y1="5" x2="15" y2="15" stroke="{c}" stroke-width="2.2" stroke-linecap="round"/><line x1="15" y1="5" x2="5" y2="15" stroke="{c}" stroke-width="2.2" stroke-linecap="round"/></svg>',
    "home":    '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><polyline points="2,10 10,3 18,10" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><polyline points="5,10 5,17 15,17 15,10" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "close":   '<svg viewBox="0 0 14 14" xmlns="http://www.w3.org/2000/svg"><line x1="2.5" y1="2.5" x2="11.5" y2="11.5" stroke="{c}" stroke-width="2" stroke-linecap="round"/><line x1="11.5" y1="2.5" x2="2.5" y2="11.5" stroke="{c}" stroke-width="2" stroke-linecap="round"/></svg>',
    "new_tab": '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><line x1="10" y1="4" x2="10" y2="16" stroke="{c}" stroke-width="2.5" stroke-linecap="round"/><line x1="4" y1="10" x2="16" y2="10" stroke="{c}" stroke-width="2.5" stroke-linecap="round"/></svg>',
    "lock":    '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><rect x="4" y="9" width="12" height="9" rx="2" fill="none" stroke="{c}" stroke-width="2"/><path d="M7 9V6.5a3 3 0 0 1 6 0V9" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round"/><circle cx="10" cy="14" r="1.5" fill="{c}"/></svg>',
    "block":   '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><circle cx="10" cy="10" r="7" fill="none" stroke="{c}" stroke-width="2"/><line x1="5.1" y1="5.1" x2="14.9" y2="14.9" stroke="{c}" stroke-width="2" stroke-linecap="round"/></svg>',
    "warn":    '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><path d="M10 3L18.5 17H1.5L10 3z" fill="none" stroke="{c}" stroke-width="2" stroke-linejoin="round"/><line x1="10" y1="9" x2="10" y2="13" stroke="{c}" stroke-width="2" stroke-linecap="round"/><circle cx="10" cy="15.5" r="1.1" fill="{c}"/></svg>',
    "mic":     '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><rect x="7" y="2" width="6" height="9" rx="3" fill="none" stroke="{c}" stroke-width="2"/><path d="M4 10a6 6 0 0 0 12 0" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round"/><line x1="10" y1="16" x2="10" y2="18" stroke="{c}" stroke-width="2.5" stroke-linecap="round"/></svg>',
    "camera":  '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><path d="M1 7h18a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H1a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1z" fill="none" stroke="{c}" stroke-width="2"/><circle cx="10" cy="12.5" r="2.8" fill="none" stroke="{c}" stroke-width="2"/><path d="M7 7l1.5-2h3L13 7" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "location":'<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><path d="M10 2a6 6 0 0 1 6 6c0 4.5-6 10-6 10S4 12.5 4 8a6 6 0 0 1 6-6z" fill="none" stroke="{c}" stroke-width="2"/><circle cx="10" cy="8" r="2" fill="{c}"/></svg>',
    "bell":    '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><path d="M10 2a5 5 0 0 1 5 5v4l2 2H3l2-2V7a5 5 0 0 1 5-5z" fill="none" stroke="{c}" stroke-width="2" stroke-linejoin="round"/><path d="M8 16a2 2 0 0 0 4 0" fill="none" stroke="{c}" stroke-width="2"/></svg>',
}

def _icon(name: str, color: str | None = None, size: int = 20) -> QIcon:
    """Render a named SVG icon. color defaults to the active theme's muted color."""
    c   = color if color is not None else _C("muted")
    svg = _SVG.get(name, _SVG["warn"]).replace("{c}", c)
    svg = svg.replace("<svg ", f'<svg width="{size}" height="{size}" ', 1)
    pm  = QPixmap()
    pm.loadFromData(QByteArray(svg.encode()), "SVG")
    return QIcon(pm)

# ── Home / error pages ────────────────────────────────────────────────────────

_HOME_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0a0a0f; color: #e8e8f0;
    height: 100vh; display: flex; flex-direction: column;
    align-items: center; justify-content: center; overflow: hidden; gap: 10px;
  }
  .clock {
    font-size: 80px; font-weight: 800;
    background: linear-gradient(135deg, #6c63ff, #43e8b2);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }
  .date { color: #6b6b85; font-size: 14px; font-family: monospace; }
  .hint { color: #3a3a55; font-size: 12px; font-family: monospace; margin-top: 24px; }
</style></head><body>
  <div class="clock" id="clock">--:--</div>
  <div class="date"  id="date">--</div>
  <div class="hint">No app loaded. Pass --root and --entry to launch your HTML app.</div>
  <script>
    function tick() {
      const n = new Date();
      document.getElementById('clock').textContent =
        String(n.getHours()).padStart(2,'0') + ':' + String(n.getMinutes()).padStart(2,'0');
      document.getElementById('date').textContent =
        n.toLocaleDateString('en-US', {weekday:'long', month:'long', day:'numeric', year:'numeric'});
    }
    tick(); setInterval(tick, 10000);
  </script>
</body></html>"""

# NOTE: _BLOCKED_HTML is not currently served.
# EmbedInterceptor calls info.block(True) which lets Chromium show its own
# ERR_BLOCKED_BY_CLIENT page. This template is kept as a reference in case
# a future version serves a custom blocked page via a redirect.
_BLOCKED_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0a0a0f; color: #e8e8f0;
    height: 100vh; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 14px; padding: 32px;
  }}
  h1 {{ color: #ff6584; font-size: 20px; }}
  .url {{ font-family:'Courier New'; font-size: 12px; color: #6b6b85;
          word-break: break-all; max-width: 600px; text-align: center; }}
  .note {{ background:#1e1e2e; border:1px solid #2d2d45; border-radius:8px;
           padding:12px 20px; font-family:'Courier New'; font-size:11px;
           color:#febc2e; max-width:600px; width:100%; }}
</style></head><body>
  <h1>⊘ External Request Blocked</h1>
  <div class="url">{url}</div>
  <div class="note">
    ORBIT EMBED only allows app://, file://, data:, blob:, and about: by default.<br>
    To allow a local server, pass <b>--allow-host localhost:PORT</b> at startup.
  </div>
</body></html>"""

_CERT_ERROR_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0a0a0f; color: #e8e8f0;
    height: 100vh; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 14px; padding: 32px;
  }}
  h1 {{ color: #ff6584; font-size: 20px; }}
  .url {{ font-family:'Courier New'; font-size: 12px; color: #6b6b85; }}
  .detail {{ background:#1e1e2e; border:1px solid #2d2d45; border-radius:8px;
             padding:12px 20px; font-family:'Courier New'; font-size:11px;
             color:#febc2e; max-width:600px; width:100%; }}
</style></head><body>
  <h1>⚠ Certificate Error</h1>
  <div class="url">{url}</div>
  <div class="detail">{desc}</div>
</body></html>"""

# ── Bridge JS (injected into every page) ──────────────────────────────────────

_BRIDGE_SETUP_JS = r"""
(function() {
  // Wait for QWebChannel to be ready, then expose the orbit API
  if (typeof qt === 'undefined') return;

  var _readyCallbacks = [];
  var _orbit = null;
  var _eventListeners = [];

  function _notifyReady() {
    _readyCallbacks.forEach(function(cb) { try { cb(_orbit); } catch(e) {} });
    _readyCallbacks = [];
  }

  // Expose window.__orbitReady(cb) — calls cb(orbit) once bridge is live
  window.__orbitReady = function(cb) {
    if (_orbit) { cb(_orbit); } else { _readyCallbacks.push(cb); }
  };

  // Convenience promise-based invoke.
  // timeoutMs — optional; defaults to 10 000 ms.  Pass 0 to disable the timeout.
  window.orbitInvoke = function(method, args, timeoutMs) {
    return new Promise(function(resolve, reject) {
      if (!_orbit) { reject(new Error("orbit bridge not ready")); return; }
      var ms      = (timeoutMs === undefined) ? 10000 : timeoutMs;
      var settled = false;
      var timer   = ms > 0
        ? setTimeout(function() {
            if (!settled) {
              settled = true;
              reject(new Error(
                "orbitInvoke timeout: \"" + method + "\" did not respond within " + ms + "ms"
              ));
            }
          }, ms)
        : null;
      _orbit.invoke(method, args || [],
        function(result) {
          if (!settled) {
            settled = true;
            if (timer !== null) clearTimeout(timer);
            try { resolve(JSON.parse(result)); }
            catch(e) { resolve(result); }
          }
        }
      );
    });
  };

  new QWebChannel(qt.webChannelTransport, function(channel) {
    var raw = channel.objects.orbit;
    _orbit = {
      invoke: function(method, args, callback) {
        var payload = JSON.stringify({ method: method, args: args || [] });
        raw.invoke(payload, function(result) {
          if (callback) {
            try { callback(JSON.parse(result)); }
            catch(e) { callback(result); }
          }
        });
      },
      onEvent: function(listener) {
        _eventListeners.push(listener);
      }
    };
    // Wire raw event signal → registered listeners
    raw.event.connect(function(name, dataJson) {
      var data;
      try { data = JSON.parse(dataJson); } catch(e) { data = dataJson; }
      _eventListeners.forEach(function(cb) { try { cb(name, data); } catch(e) {} });
    });
    _notifyReady();
  });
})();
"""

# ── app:// Scheme Handler ─────────────────────────────────────────────────────

class AppSchemeHandler(QWebEngineUrlSchemeHandler):
    """
    Serves files from a root directory as app://<host>/<path>.
    The <host> portion is ignored — all app:// URLs resolve relative to root.

    MIME types are detected from file extension via Python's mimetypes module.
    Falls back to application/octet-stream for unknown types.

    Security:
      · Path traversal is prevented — any resolved path outside root returns 403.
      · Symlinks that escape root are also blocked.
      · HTML responses have a strict Content-Security-Policy injected unless the
        document already contains its own CSP meta tag.

    The default CSP allows:
      - Scripts / styles / images / fonts from app:// and data: only
      - No inline eval, no external connections, no plugins, no framing
    Pass csp=None to disable injection entirely, or supply a custom policy string.
    """

    # Default CSP for app:// HTML pages.
    # Tight but practical for offline apps:
    #   - data: URIs allowed for images/fonts (canvas export, embedded assets)
    #   - blob: for Web Workers and generated object URLs
    #   - 'unsafe-inline' for styles so app CSS works without a nonce pipeline
    #   - no 'unsafe-eval' — apps that need it must supply their own CSP
    DEFAULT_CSP = (
        "default-src app: data: blob: 'self'; "
        "script-src app: 'self'; "
        "style-src app: 'unsafe-inline' 'self'; "
        "img-src app: data: blob: 'self'; "
        "font-src app: data: 'self'; "
        "connect-src app: 'self'; "
        "frame-src 'none'; "
        "object-src 'none'; "
        "base-uri 'self';"
    )

    _CSP_META = (
        '<meta http-equiv="Content-Security-Policy" content="{csp}">'
    )

    def __init__(self, root: Path, entry: str = "index.html",
                 csp: str | None = DEFAULT_CSP, parent=None):
        super().__init__(parent)
        self._root  = root.resolve()
        self._entry = entry
        self._csp   = csp   # None = disabled
        mimetypes.init()

    def _inject_csp(self, html_bytes: bytes) -> bytes:
        """
        Inject a CSP <meta> tag into the <head> of an HTML document,
        unless the document already has its own CSP meta tag.
        Fast and allocation-light: works entirely on bytes.
        """
        if self._csp is None:
            return html_bytes
        low = html_bytes[:4096].lower()          # only scan the head region
        if b'content-security-policy' in low:
            return html_bytes                    # document has its own CSP
        meta = self._CSP_META.format(csp=self._csp).encode("utf-8")
        # Prefer inserting right after <head>; fall back to after <html>;
        # fall back to prepending if neither tag is found.
        for tag in (b'<head>', b'<html>'):
            idx = low.find(tag)
            if idx >= 0:
                insert_at = idx + len(tag)
                return html_bytes[:insert_at] + b'\n' + meta + html_bytes[insert_at:]
        return meta + b'\n' + html_bytes

    def requestStarted(self, job: QWebEngineUrlRequestJob):
        try:
            url   = job.requestUrl()
            raw   = url.path()             # e.g. /index.html or /assets/app.js
            # Strip leading slash and decode percent-encoding
            rel   = unquote(raw.lstrip("/"))
            # Empty path → serve index.html
            if not rel:
                rel = "index.html"

            target = (self._root / rel).resolve()

            # Security: block path traversal — require resolved path to be
            # inside root (startswith alone would match e.g. /root_evil/).
            root_str = str(self._root)
            target_str = str(target)
            if target_str != root_str and not target_str.startswith(root_str + os.sep):
                _log(f"[app://] Path traversal blocked: {raw!r}")
                job.fail(QWebEngineUrlRequestJob.Error.RequestDenied)
                return

            if not target.is_file():
                # If the entry point itself is missing, serve the built-in home
                # page instead of a blank Chromium error screen.  All other
                # missing files (assets, etc.) still return a proper 404.
                if rel in ("index.html", "") or rel == self._entry:
                    _log(f"[app://] Entry not found — serving built-in home page")
                    data = _HOME_HTML.encode("utf-8")
                    buf  = QBuffer(job)
                    buf.open(QIODevice.OpenModeFlag.WriteOnly)
                    buf.write(data)
                    buf.seek(0)
                    buf.open(QIODevice.OpenModeFlag.ReadOnly)
                    job.reply(b"text/html; charset=utf-8", buf)
                    return
                _log(f"[app://] Not found: {target}")
                job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
                return

            mime, _ = mimetypes.guess_type(str(target))
            if mime is None:
                mime = "application/octet-stream"

            data = target.read_bytes()
            # Inject CSP meta tag into HTML responses
            if mime in ("text/html", "application/xhtml+xml"):
                data = self._inject_csp(data)
                mime = mime + "; charset=utf-8"  # must come after CSP check uses bare mime
            buf  = QBuffer(job)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            buf.write(data)
            buf.seek(0)
            buf.open(QIODevice.OpenModeFlag.ReadOnly)
            job.reply(mime.encode(), buf)

        except Exception as e:
            _log(f"[app://] Handler error: {e}")
            job.fail(QWebEngineUrlRequestJob.Error.RequestFailed)

# ── Request Interceptor ───────────────────────────────────────────────────────

_LOCAL_SCHEMES = frozenset(["app", "file", "qrc", "data", "blob", "about", "chrome", "devtools"])

class EmbedInterceptor(QWebEngineUrlRequestInterceptor):
    """
    Enforces the offline contract by default:
      · app://, file://, data:, blob:, about:, qrc: always pass.
      · Hosts in allowed_hosts (e.g. localhost:3000) always pass.
      · Everything else is hard-blocked — unless online mode is active.

    Online mode (temporary):
      Call go_online(idle_secs, max_secs=None) to open the gate.
      Every passing http/https request updates _last_activity_time.
      OrbitEmbed's watchdog timer checks every second:
        - if now - _last_activity_time > idle_secs  → go_offline()
        - if now - _online_start_time   > max_secs   → go_offline()  (only if max_secs set)
      go_offline() closes the gate immediately.

    Thread safety:
      interceptRequest() runs on a background thread.
      _online, _last_activity_time, _online_start_time are read/written
      under _lock so the Qt main thread and the interceptor thread
      never race.
    """

    # Emitted on the Qt thread when online mode expires or is cancelled.
    # OrbitEmbed connects this to fire the "networkLocked" JS event.
    went_offline = pyqtSignal(str)   # reason: "idle" | "timeout" | "manual"

    def __init__(self, allowed_hosts: set[str] | None = None, parent=None):
        super().__init__(parent)
        self._allowed: set[str] = {h.lower().rstrip("/") for h in (allowed_hosts or set())}
        self._lock = threading.Lock()

        # Online-mode state (all guarded by _lock)
        self._online           = False
        self._last_activity    = 0.0   # epoch seconds of last passed http/https request
        self._online_start     = 0.0   # epoch seconds when go_online() was called
        self._idle_secs        = 10.0  # lock after this many idle seconds
        self._max_secs: float | None = None  # None = no hard ceiling; idle-only

    # ── Allowlist management ──────────────────────────────────────────────

    def allow_host(self, host: str):
        """Add a permanent host exemption (e.g. 'localhost:3000')."""
        with self._lock:
            self._allowed.add(host.lower().rstrip("/"))

    def remove_host(self, host: str):
        with self._lock:
            self._allowed.discard(host.lower().rstrip("/"))

    # ── Online window control (called from Qt main thread) ────────────────

    def go_online(self, idle_secs: float = 10.0, max_secs: float | None = None):
        """
        Open the network gate. All http/https requests pass until:
          - idle_secs have elapsed with no network activity (required), OR
          - max_secs have elapsed since go_online() was called (optional ceiling).

        If max_secs is None (the default) there is no hard ceiling — the gate
        stays open as long as bytes keep flowing, regardless of how long that
        takes. This is the right mode for large downloads where duration is
        unpredictable.

        Pass max_secs only when you want an absolute upper bound, e.g. for
        a license check that should never take more than 30 seconds.

        The watchdog in OrbitEmbed ticks every second and calls
        check_should_lock() to evaluate these conditions.
        """
        with self._lock:
            self._online        = True
            self._idle_secs     = max(1.0, idle_secs)
            self._max_secs      = max_secs   # None = disabled
            now                 = _time.monotonic()
            self._online_start  = now
            self._last_activity = now   # treat open itself as activity
        ceiling = f"{max_secs}s" if max_secs is not None else "none"
        _log(f"[intercept] online gate OPEN  idle={idle_secs}s  max={ceiling}")

    def go_offline(self, reason: str = "manual"):
        """Close the network gate immediately."""
        with self._lock:
            if not self._online:
                return
            self._online = False
        _log(f"[intercept] online gate CLOSED  reason={reason}")
        # Marshal to Qt thread before emitting signal
        QTimer.singleShot(0, lambda r=reason: self.went_offline.emit(r))

    @property
    def is_online(self) -> bool:
        with self._lock:
            return self._online

    def check_should_lock(self, active_downloads: int = 0):
        """
        Called every second from OrbitEmbed's watchdog timer (Qt main thread).
        Evaluates idle and (optional) hard-timeout conditions.

        active_downloads — number of downloads currently in flight.
        While any download is active the idle clock is suppressed:
        _last_activity is refreshed each tick so the gate never closes
        mid-transfer due to the interceptor seeing no new requests.
        The hard ceiling (max_secs) still applies if set.

        Returns True if the gate was just closed, False otherwise.
        """
        with self._lock:
            if not self._online:
                return False
            now     = _time.monotonic()
            # Suppress idle countdown while downloads are active
            if active_downloads > 0:
                self._last_activity = now
            idle    = now - self._last_activity
            elapsed = now - self._online_start
            if idle >= self._idle_secs:
                reason = "idle"
            elif self._max_secs is not None and elapsed >= self._max_secs:
                reason = "timeout"
            else:
                return False
        self.go_offline(reason)
        return True

    # ── Request interception (background thread) ──────────────────────────

    def interceptRequest(self, info: QWebEngineUrlRequestInfo):
        url    = info.requestUrl()
        scheme = url.scheme().lower()

        # Always allow local/internal schemes
        if scheme in _LOCAL_SCHEMES:
            return

        host      = url.host().lower()
        port      = url.port(-1)
        host_port = f"{host}:{port}" if port != -1 else host

        with self._lock:
            allowed = set(self._allowed)   # snapshot under lock; set may be mutated by allow_host()
            online  = self._online

        # Permanent allowlist (e.g. localhost:3000)
        if host in allowed or host_port in allowed:
            return

        # Temporary online gate
        if online:
            with self._lock:
                self._last_activity = _time.monotonic()
            _log(f"[intercept] ALLOWED (online) {scheme}://{host}{url.path()[:60]}")
            return

        # Default: block
        _log(f"[intercept] BLOCKED {scheme}://{host}{url.path()[:60]}")
        info.block(True)

# ── JS↔Python Bridge ─────────────────────────────────────────────────────────

def _default_tray_icon() -> QIcon:
    """Generate a simple default SVG tray icon using the active accent color."""
    c   = _C("accent")
    svg = (
        '<svg width="32" height="32" viewBox="0 0 32 32" '
        'xmlns="http://www.w3.org/2000/svg">'
        f'<circle cx="16" cy="16" r="13" fill="{c}" opacity="0.9"/>'
        f'<circle cx="16" cy="16" r="7" fill="none" stroke="#ffffff" '
        'stroke-width="2.5"/>'
        '</svg>'
    )
    pm = QPixmap()
    pm.loadFromData(QByteArray(svg.encode()), "SVG")
    return QIcon(pm)


class OrbitBridge(QObject):
    """
    JS↔Python bridge base class. Subclass and override handle_invoke() for
    app-specific methods. All built-in services are handled automatically.

    ── BUILT-IN METHODS ──────────────────────────────────────────────────────

    Network:
      requestOnline({reason?, idleSecs?, maxSecs?})
        Open the temporary online window.
        idleSecs  – idle seconds before auto-lock  (default 10)
        maxSecs   – hard ceiling, None = no limit   (default None)
        → {granted: true}

      goOffline()
        Immediately close the online gate.
        → {locked: true}

      networkState()
        → {online: bool}

      allowHost(host)         e.g. "localhost:3000"
        Add a permanent host exemption at runtime.
        → {ok: true}

      removeHost(host)
        Remove a permanent host exemption.
        → {ok: true}

    File I/O  (all paths are sandboxed to data_root by default):
      readFile(path, encoding?)
        Read a file. encoding defaults to "utf-8"; pass "binary" for base64.
        → {content: str}

      writeFile(path, content, encoding?)
        Write a file, creating parent directories as needed.
        encoding: "utf-8" (default) | "binary" (content is base64)
        → {ok: true}

      appendFile(path, content)
        Append UTF-8 text to a file (creates if missing).
        → {ok: true}

      deleteFile(path)
        Delete a file.
        → {ok: true}

      listDir(path?)
        List directory contents. path defaults to data_root.
        → [{name, isDir, size, modified}]

      makeDir(path)
        Create a directory (and all parents).
        → {ok: true}

      fileExists(path)
        → {exists: bool, isDir: bool}

      fileInfo(path)
        → {name, path, size, modified, isDir}

    Persistent store  (survives restarts, stored in data_root/.orbit_store.json):
      storeGet(key)
        → {value: any} or {value: null} if not found

      storeSet(key, value)
        value can be any JSON-serializable type.
        → {ok: true}

      storeDelete(key)
        → {ok: true}

      storeList(prefix?)
        List all keys, optionally filtered by prefix.
        → {keys: [str]}

    Window control:
      minimize()              → {ok: true}
      maximize()              → {ok: true}  (toggles if already maximized)
      restore()               → {ok: true}
      setTitle(text)          → {ok: true}
      setAlwaysOnTop(bool)    → {ok: true}
      center()                → {ok: true}
      getWindowState()        → {state, width, height, x, y}

    Notifications:
      notify({title, body})   → {ok: true}

    System tray:
      setTrayTooltip(text)    → {ok: true}
      setMinimizeToTray(bool) → {ok: true}
      setTrayIcon(path?)      → {ok: true}

    Print / PDF:
      printToPdf({path?})     → {ok: true, path: str}

    Clipboard:
      clipboardRead()         → {text: str}
      clipboardWrite(text)    → {ok: true}

    File dialogs  (native OS file picker — blocks until user dismisses):
      openFileDialog({title?, filter?, multiple?, initialDir?})
        Open the OS file picker.  filter uses Qt syntax: "Images (*.png *.jpg)"
        multiple: false (default) → {path, cancelled}
        multiple: true            → {paths: [str], cancelled}

      saveFileDialog({title?, filter?, defaultName?, initialDir?})
        Open the OS save-file picker.
        → {path, cancelled}

    Tab control:
      setNewTabsAllowed(bool)
        Allow or prevent the user from opening new tabs (Ctrl+T, + button).
        Defaults to false — the app decides when multi-tab is appropriate.
        → {ok: true, allowed: bool}

      getNewTabsAllowed()
        → {allowed: bool}

    ── CROSS-TAB SHARING (B) — no bridge needed ──────────────────────────────
    All tabs share the same app:// origin and profile, so standard browser
    storage APIs work across tabs within a session automatically:
      · localStorage     — synchronous key-value, persists for the session
      · sessionStorage   — per-tab; not shared
      · BroadcastChannel — real-time messaging between tabs (recommended)
      · IndexedDB        — structured storage for larger data

    Note: all in-memory only (off-the-record profile). Use storeSet/storeGet
    for data that must survive a restart.

    ── EXAMPLE SUBCLASS ──────────────────────────────────────────────────────
    class MyBridge(OrbitBridge):
        def handle_invoke(self, method, args):
            if method == "myMethod":
                return {"result": do_something(args)}
            return super().handle_invoke(method, args)
    """

    event = pyqtSignal(str, str)   # (event_name, json_data)

    def __init__(self, data_root: Path | str | None = None,
                 sandboxed: bool = True, parent=None):
        """
        data_root  — root directory for all file I/O and the persistent store.
                     Defaults to ~/.orbit_embed/<app_title>/data/.
                     Set at OrbitEmbed construction time if not provided here.
        sandboxed  — if True (default), all file paths must resolve inside
                     data_root. Set False only if your app genuinely needs
                     unrestricted filesystem access.
        """
        super().__init__(parent)
        self._data_root: Path | None = Path(data_root).resolve() if data_root else None
        self._sandboxed  = sandboxed
        self._store_path: Path | None = None   # set when data_root is known
        self._store: dict  = {}
        self._store_lock   = threading.Lock()
        self._win: "OrbitEmbed | None" = None

    # ── Internal setup (called by OrbitEmbed) ─────────────────────────────

    def _set_data_root(self, root: Path):
        """Called by OrbitEmbed to inject the data root after construction."""
        if self._data_root is None:
            self._data_root = root
        self._data_root.mkdir(parents=True, exist_ok=True)
        self._store_path = self._data_root / ".orbit_store.json"
        self._load_store()

    def _resolve_path(self, rel: str) -> Path:
        """
        Resolve a path from JS against data_root.
        Raises PermissionError if the resolved path escapes the sandbox.
        """
        if self._data_root is None:
            raise RuntimeError("data_root not set")
        p = (self._data_root / rel).resolve()
        if self._sandboxed:
            root_str = str(self._data_root)
            p_str    = str(p)
            if p_str != root_str and not p_str.startswith(root_str + os.sep):
                raise PermissionError(f"Path escapes sandbox: {rel!r}")
        return p

    # ── Persistent store internals ────────────────────────────────────────

    def _load_store(self):
        if self._store_path and self._store_path.exists():
            try:
                with open(self._store_path, encoding="utf-8") as f:
                    self._store = json.load(f)
            except Exception as e:
                _log(f"[store] load error: {e}  — starting fresh")
                self._store = {}

    def _save_store(self):
        if self._store_path is None:
            return
        try:
            tmp = self._store_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._store, f, indent=2)
            tmp.replace(self._store_path)   # atomic on all platforms
        except Exception as e:
            _log(f"[store] save error: {e}")

    # ── Bridge entry point ────────────────────────────────────────────────

    @pyqtSlot(str, result=str)
    def invoke(self, payload: str) -> str:
        try:
            msg    = json.loads(payload)
            method = msg.get("method", "")
            args   = msg.get("args", [])
            result = self.handle_invoke(method, args)
            return json.dumps({"ok": True, "result": result})
        except PermissionError as e:
            _log(f"[bridge] permission denied: {e}")
            return json.dumps({"ok": False, "error": f"Permission denied: {e}"})
        except Exception as e:
            _log(f"[bridge] invoke error ({method}): {e}")
            return json.dumps({"ok": False, "error": str(e)})

    def handle_invoke(self, method: str, args: list):
        """Override in subclass. Call super() to keep built-in methods."""

        # ── Network ───────────────────────────────────────────────────────
        if method == "requestOnline":
            opts    = args[0] if args and isinstance(args[0], dict) else {}
            idle    = float(opts.get("idleSecs", 10))
            raw_max = opts.get("maxSecs", None)
            max_s   = float(raw_max) if raw_max is not None else None
            reason  = opts.get("reason", "js request")
            ceiling = f"{max_s}s" if max_s is not None else "none"
            _log(f"[bridge] requestOnline: {reason}  idle={idle}s  max={ceiling}")
            if self._win:
                self._win.go_online_for(idle_secs=idle, max_secs=max_s)
            return {"granted": True}

        if method == "goOffline":
            if self._win:
                self._win.go_offline()
            return {"locked": True}

        if method == "networkState":
            return {"online": self._win._interceptor.is_online if self._win else False}

        if method == "allowHost":
            host = args[0] if args else ""
            if host and self._win:
                self._win.allow_host(host)
            return {"ok": True, "host": host}

        if method == "removeHost":
            host = args[0] if args else ""
            if host and self._win:
                self._win._interceptor.remove_host(host)
            return {"ok": True}

        # ── File I/O ──────────────────────────────────────────────────────
        if method == "readFile":
            p        = self._resolve_path(args[0])
            encoding = args[1] if len(args) > 1 else "utf-8"
            if encoding == "binary":
                return {"content": base64.b64encode(p.read_bytes()).decode()}
            return {"content": p.read_text(encoding=encoding)}

        if method == "writeFile":
            p        = self._resolve_path(args[0])
            content  = args[1] if len(args) > 1 else ""
            encoding = args[2] if len(args) > 2 else "utf-8"
            p.parent.mkdir(parents=True, exist_ok=True)
            if encoding == "binary":
                p.write_bytes(base64.b64decode(content))
            else:
                p.write_text(content, encoding=encoding)
            return {"ok": True}

        if method == "appendFile":
            p = self._resolve_path(args[0])
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(args[1] if len(args) > 1 else "")
            return {"ok": True}

        if method == "deleteFile":
            p = self._resolve_path(args[0])
            p.unlink()
            return {"ok": True}

        if method == "listDir":
            p = self._resolve_path(args[0]) if args else self._data_root
            entries = []
            for child in sorted(p.iterdir()):
                st = child.stat()
                entries.append({
                    "name":     child.name,
                    "isDir":    child.is_dir(),
                    "size":     st.st_size,
                    "modified": st.st_mtime,
                })
            return entries

        if method == "makeDir":
            p = self._resolve_path(args[0])
            p.mkdir(parents=True, exist_ok=True)
            return {"ok": True}

        if method == "fileExists":
            try:
                p = self._resolve_path(args[0])
                return {"exists": p.exists(), "isDir": p.is_dir()}
            except PermissionError:
                return {"exists": False, "isDir": False}

        if method == "fileInfo":
            p  = self._resolve_path(args[0])
            st = p.stat()
            return {
                "name":     p.name,
                "path":     str(p),
                "size":     st.st_size,
                "modified": st.st_mtime,
                "isDir":    p.is_dir(),
            }

        # ── Persistent store ──────────────────────────────────────────────
        if method == "storeGet":
            key = args[0] if args else ""
            with self._store_lock:
                return {"value": self._store.get(key, None)}

        if method == "storeSet":
            key   = args[0] if args else ""
            value = args[1] if len(args) > 1 else None
            with self._store_lock:
                self._store[key] = value
                self._save_store()
            return {"ok": True}

        if method == "storeDelete":
            key = args[0] if args else ""
            with self._store_lock:
                self._store.pop(key, None)
                self._save_store()
            return {"ok": True}

        if method == "storeList":
            prefix = args[0] if args else ""
            with self._store_lock:
                keys = [k for k in self._store if k.startswith(prefix)]
            return {"keys": sorted(keys)}

        # ── Theme ─────────────────────────────────────────────────────────
        if method == "setTheme":
            partial = args[0] if args and isinstance(args[0], dict) else {}
            if self._win:
                self._win._apply_theme(partial)
                # Persist to theme file if one is configured
                tf = self._win._theme_file
                if tf:
                    try:
                        tf.parent.mkdir(parents=True, exist_ok=True)
                        with open(tf, "w", encoding="utf-8") as f:
                            json.dump(_THEME, f, indent=2)
                        _log(f"[theme] saved to {tf}")
                    except Exception as e:
                        _log(f"[theme] save failed: {e}")
            return {"ok": True, "theme": _THEME}

        if method == "getTheme":
            return {"theme": _THEME}

        # ── Window control ────────────────────────────────────────────────
        if method == "minimize":
            if self._win: self._win.showMinimized()
            return {"ok": True}

        if method == "maximize":
            if self._win:
                if self._win.isMaximized(): self._win.showNormal()
                else: self._win.showMaximized()
            return {"ok": True}

        if method == "restore":
            if self._win: self._win.showNormal()
            return {"ok": True}

        if method == "setTitle":
            text = args[0] if args else ""
            if self._win: self._win.setWindowTitle(str(text))
            return {"ok": True}

        if method == "setAlwaysOnTop":
            on_top = bool(args[0]) if args else False
            if self._win:
                flags = self._win.windowFlags()
                if on_top:
                    flags |= Qt.WindowType.WindowStaysOnTopHint
                else:
                    flags &= ~Qt.WindowType.WindowStaysOnTopHint
                self._win.setWindowFlags(flags)
                self._win.show()   # required after flag change
            return {"ok": True}

        if method == "center":
            if self._win:
                screen = QApplication.instance().primaryScreen()
                if screen:
                    geom = screen.availableGeometry()
                    self._win.move(
                        geom.left() + (geom.width()  - self._win.width())  // 2,
                        geom.top()  + (geom.height() - self._win.height()) // 2,
                    )
            return {"ok": True}

        if method == "getWindowState":
            if not self._win:
                return {"state": "unknown"}
            w = self._win
            if w.isFullScreen():  state = "fullscreen"
            elif w.isMaximized(): state = "maximized"
            elif w.isMinimized(): state = "minimized"
            else:                 state = "normal"
            return {
                "state": state,
                "width": w.width(), "height": w.height(),
                "x": w.x(),         "y": w.y(),
            }

        # ── Native notifications ──────────────────────────────────────────
        if method == "notify":
            opts  = args[0] if args and isinstance(args[0], dict) else {}
            title = str(opts.get("title", "Notification"))
            body  = str(opts.get("body",  ""))
            if self._win and hasattr(self._win, "_tray") and self._win._tray:
                self._win._tray.showMessage(
                    title, body,
                    QSystemTrayIcon.MessageIcon.Information, 4000
                )
                return {"ok": True}
            return {"ok": False, "error": "tray not available"}

        # ── System tray control ───────────────────────────────────────────
        if method == "setTrayTooltip":
            text = str(args[0]) if args else ""
            if self._win and hasattr(self._win, "_tray") and self._win._tray:
                self._win._tray.setToolTip(text)
            return {"ok": True}

        if method == "setMinimizeToTray":
            enabled = bool(args[0]) if args else True
            if self._win:
                self._win._minimize_to_tray = enabled
            return {"ok": True}

        if method == "setTrayIcon":
            # args[0] = path relative to data_root, or None to reset to default
            if self._win and hasattr(self._win, "_tray") and self._win._tray:
                icon_path = args[0] if args else None
                if icon_path:
                    resolved = self._resolve_path(str(icon_path))
                    self._win._tray.setIcon(QIcon(str(resolved)))
                else:
                    self._win._tray.setIcon(_default_tray_icon())
            return {"ok": True}

        # ── Print / export to PDF ─────────────────────────────────────────
        if method == "printToPdf":
            opts     = args[0] if args and isinstance(args[0], dict) else {}
            filename = opts.get("path", None)
            if filename is None:
                ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"export_{ts}.pdf"
            out_path = self._resolve_path(str(filename))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if self._win:
                tab = self._win._active_tab()
                if tab:
                    tab._page.printToPdf(
                        str(out_path),
                        tab._page.pageLayout() if hasattr(tab._page, "pageLayout") else None
                    )
                    _log(f"[pdf] printing to {out_path}")
                    return {"ok": True, "path": str(out_path)}
            return {"ok": False, "error": "no active tab"}

        # ── Clipboard ─────────────────────────────────────────────────────
        if method == "clipboardRead":
            cb = QGuiApplication.clipboard()
            return {"text": cb.text() if cb else ""}

        if method == "clipboardWrite":
            text = args[0] if args else ""
            cb   = QGuiApplication.clipboard()
            if cb:
                cb.setText(str(text))
            return {"ok": True}

        # ── Tab control ───────────────────────────────────────────────────
        if method == "setNewTabsAllowed":
            allowed = bool(args[0]) if args else False
            if self._win:
                self._win.set_new_tabs_allowed(allowed)
            return {"ok": True, "allowed": allowed}

        if method == "getNewTabsAllowed":
            return {"allowed": self._win._allow_new_tabs if self._win else False}

        # ── File dialogs ──────────────────────────────────────────────────
        if method == "openFileDialog":
            opts        = args[0] if args and isinstance(args[0], dict) else {}
            dlg_title   = str(opts.get("title",   "Open File"))
            filter_str  = str(opts.get("filter",  "All Files (*)"))
            multiple    = bool(opts.get("multiple", False))
            initial_dir = str(opts.get("initialDir",
                                       str(self._data_root or Path.home())))
            parent_win  = self._win
            if multiple:
                paths, _ = QFileDialog.getOpenFileNames(
                    parent_win, dlg_title, initial_dir, filter_str)
                return {"paths": paths, "cancelled": len(paths) == 0}
            else:
                path, _ = QFileDialog.getOpenFileName(
                    parent_win, dlg_title, initial_dir, filter_str)
                return {"path": path, "cancelled": not bool(path)}

        if method == "saveFileDialog":
            opts         = args[0] if args and isinstance(args[0], dict) else {}
            dlg_title    = str(opts.get("title",       "Save File"))
            filter_str   = str(opts.get("filter",      "All Files (*)"))
            default_name = str(opts.get("defaultName", ""))
            initial_dir  = str(opts.get("initialDir",
                                        str(self._data_root or Path.home())))
            initial_path = (str(Path(initial_dir) / default_name)
                            if default_name else initial_dir)
            parent_win   = self._win
            path, _ = QFileDialog.getSaveFileName(
                parent_win, dlg_title, initial_path, filter_str)
            return {"path": path, "cancelled": not bool(path)}

        _log(f"[bridge] unhandled invoke: {method}({args})")
        return None

    def emit_event(self, name: str, data=None):
        """Send a named event to all JS listeners."""
        self.event.emit(name, json.dumps(data))

# ── Web Page ──────────────────────────────────────────────────────────────────

class EmbedWebPage(QWebEnginePage):
    """
    QWebEnginePage subclass:
      · Intercepts new-tab/popup requests and opens them in the same window.
      · Shows a friendly certificate error page instead of Chromium's default.
      · Denies all permissions (mic, camera, geolocation, notifications) by default.
        Override permission_policy to change this per-feature.
      · In dev_mode, restores the standard Chromium context menu and enables
        DevTools; in production mode the context menu is suppressed and a
        contextMenu bridge event is fired instead.
    """

    new_tab_requested = pyqtSignal(QUrl)
    cert_error_signal = pyqtSignal(str, str)

    # Permission policy: dict[feature_int → bool].
    # True = allow, False = deny (default = deny all).
    permission_policy: dict = {}

    # Set to True by BrowserTab when OrbitEmbed is started with --dev.
    dev_mode: bool = False

    # Populated by BrowserTab from OrbitEmbed's interceptor allowed-host set.
    # window.open() calls to these http/https hosts open in the system browser.
    _allowed_hosts: set = set()

    def createWindow(self, win_type):
        """
        Gate new-tab and popup requests.

        Qt calls this before the target URL is known, so we create a temporary
        gatekeeper page and inspect the URL once it arrives via urlChanged.

        Policy: only app:// URLs are allowed to open a new tab.
          - app://  → emit new_tab_requested so OrbitEmbed opens a real tab.
          - anything else → show a blocked page in the gatekeeper; no new tab.

        This means your HTML app can open new app:// panels freely, but cannot
        spawn external tabs — even if something calls window.open("https://...").
        """
        gate = EmbedWebPage(self.profile(), self.parent())

        def _on_url(url: QUrl):
            scheme = url.scheme().lower()
            # Ignore empty URLs, about:blank, and data:/blob: URLs that are
            # generated by gate.setHtml() itself — those would re-trigger this
            # handler and cause an infinite loop.
            if url.isEmpty() or url.toString() in ("about:blank", ""):
                return
            if scheme in ("data", "blob", "about", "qrc"):
                return
            # Disconnect immediately — we only want to act on the first real URL.
            gate.urlChanged.disconnect(_on_url)
            if scheme == "app":
                _log(f"[createWindow] allowed app:// -> {url.toString()[:80]}")
                self.new_tab_requested.emit(url)
            elif scheme in ("http", "https"):
                host = url.host().lower()
                port = url.port(-1)
                host_port = f"{host}:{port}" if port != -1 else host
                if host_port in self._allowed_hosts or host in self._allowed_hosts:
                    _log(f"[createWindow] forwarding to system browser -> {url.toString()[:80]}")
                    QDesktopServices.openUrl(url)
                else:
                    _log(f"[createWindow] blocked http/https -> {url.toString()[:80]}")
                    gate.setHtml(
                        f"<html><body style='background:#0a0a0f;color:#ff6584;"
                        f"font-family:monospace;padding:32px'>"
                        f"<h2>Tab blocked</h2>"
                        f"<p style='color:#6b6b85'>Only app:// pages may open new tabs.<br>"
                        f"Blocked: {url.toString()}</p></body></html>"
                    )
            else:
                _log(f"[createWindow] blocked non-app:// -> {url.toString()[:80]}")
                gate.setHtml(
                    f"<html><body style='background:#0a0a0f;color:#ff6584;"
                    f"font-family:monospace;padding:32px'>"
                    f"<h2>Tab blocked</h2>"
                    f"<p style='color:#6b6b85'>Only app:// pages may open new tabs.<br>"
                    f"Blocked: {url.toString()}</p></body></html>"
                )

        gate.urlChanged.connect(_on_url)
        return gate

    def certificateError(self, error):
        # Accept self-signed certs for localhost — used by the local HTTP/2
        # dev server (Hypercorn).  All other cert errors are rejected as usual.
        if error.url().host() in ("localhost", "127.0.0.1"):
            error.acceptCertificate()
            return True
        url  = error.url().toString()
        desc = error.description()
        _log(f"[cert] {url} — {desc}")
        if hasattr(error, "rejectCertificate"):
            error.rejectCertificate()
        html = _CERT_ERROR_HTML.format(url=url, desc=desc)
        self.setHtml(html)
        self.cert_error_signal.emit(url, desc)
        return True

    def renderProcessTerminated(self, status, exit_code):
        """
        Called when the Chromium renderer process crashes or is killed.
        Shows a recovery message and auto-reloads after 1.5 seconds.
        The JS app receives a "rendererCrash" event before the reload.
        """
        reason = {
            0: "abnormal exit",
            1: "crashed",
            2: "killed",
            3: "killed by OOM",
        }.get(int(status) if hasattr(status, "__int__") else 0, "unknown")
        _log(f"[renderer] process terminated: {reason} (code {exit_code})")
        self.setHtml(
            f"<html><body style='background:{_C('bg')};color:{_C('text')};"  
            f"font-family:monospace;padding:48px;text-align:center'>"
            f"<h2 style='color:{_C('warning')}'>⚠ Renderer process terminated</h2>"
            f"<p style='color:{_C('muted')};margin-top:12px'>"
            f"Reason: {reason}<br>Reloading in 1.5 seconds…</p></body></html>"
        )
        # Notify JS before the reload wipes the page
        # (bridge may be gone — best-effort only)
        QTimer.singleShot(1500, self._crash_reload)

    def _crash_reload(self):
        """Triggered after crash recovery delay."""
        _log("[renderer] reloading after crash")
        self.triggerAction(QWebEnginePage.WebAction.Reload)

    def createStandardContextMenu(self):
        """
        In production (dev_mode=False):
          Suppress the Chromium context menu; instead emit a contextMenu bridge
          event so the JS app renders its own native-feeling menu.
          Event payload: {x, y, href?, selectedText?, mediaType?}

        In dev mode (dev_mode=True):
          Return the standard Chromium menu so the developer gets Inspect Element
          and all normal DevTools triggers.
        """
        if self.dev_mode:
            return super().createStandardContextMenu()

        data = self.contextMenuData()
        pos  = self.view().mapFromGlobal(QCursor.pos()) if self.view() else None
        payload: dict = {
            "x": pos.x() if pos else 0,
            "y": pos.y() if pos else 0,
        }
        if hasattr(data, "linkUrl") and not data.linkUrl().isEmpty():
            payload["href"] = data.linkUrl().toString()
        if hasattr(data, "selectedText") and data.selectedText():
            payload["selectedText"] = data.selectedText()
        if hasattr(data, "mediaType"):
            payload["mediaType"] = str(data.mediaType()).split(".")[-1].lower()
        # Fire through the window bridge if wired
        win = self.parent()
        while win and not isinstance(win, QMainWindow):
            win = win.parent()
        if win and hasattr(win, "emit_event"):
            win.emit_event("contextMenu", payload)
        # Return empty menu — nothing to show at the Qt level
        return QMenu()

    def featurePermissionRequested(self, origin: QUrl, feature):
        decision = self.permission_policy.get(int(feature), False)
        self.setFeaturePermission(
            origin, feature,
            QWebEnginePage.PermissionPolicy.PermissionGrantedByUser if decision
            else QWebEnginePage.PermissionPolicy.PermissionDeniedByUser
        )

    def javaScriptConsoleMessage(self, level, message, line, source):
        lvl = {0: "DBG", 1: "INF", 2: "WRN", 3: "ERR"}.get(
            level.value if hasattr(level, "value") else int(level), "???")
        _log(f"[JS:{lvl}] {message}  ({source}:{line})")

# ── Browser Tab ───────────────────────────────────────────────────────────────

class BrowserTab(QWidget):
    """One tab: wraps a QWebEngineView + EmbedWebPage."""

    title_changed = pyqtSignal(str)
    url_changed   = pyqtSignal(QUrl)
    new_tab_url   = pyqtSignal(QUrl)
    loading_state = pyqtSignal(bool)   # True = loading
    icon_changed  = pyqtSignal(object) # QIcon when the page favicon updates

    def __init__(self, profile: QWebEngineProfile,
                 bridge: "OrbitBridge | None",
                 url: str = "",
                 dev_mode: bool = False,
                 allowed_hosts: "set | None" = None,
                 parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.webview = QWebEngineView(self)
        self._page   = EmbedWebPage(profile, self)
        self._page.dev_mode = dev_mode
        self._page._allowed_hosts = set(allowed_hosts) if allowed_hosts else set()
        self.webview.setPage(self._page)
        self.webview.setStyleSheet(f"background: {_C('bg')};")
        self._page.setBackgroundColor(QColor(_C('bg')))

        # ── Security settings ──────────────────────────────────────────────
        s = self.webview.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        # Disable features that make no sense offline
        s.setAttribute(QWebEngineSettings.WebAttribute.DnsPrefetchEnabled, False)
        # Without SecureScheme, app:// is treated as a local scheme (like file://).
        # LocalContentCanAccessRemoteUrls must be True so the page can fetch
        # http://127.0.0.1 — without it every JS fetch() is silently blocked.
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False)
        # Let WebGL work for canvas-heavy UIs
        s.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)
        # No autoplay restriction — offline apps control their own media
        s.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)

        layout.addWidget(self.webview)

        # Disable the webview's own drop handling so file drops propagate to
        # OrbitEmbed.dropEvent instead of being intercepted by Chromium.
        # Without this, dropping a file navigates the page or fires the HTML
        # drop handler with empty dataTransfer (Qt consumed the OS event).
        self.webview.setAcceptDrops(False)

        self._page.new_tab_requested.connect(self.new_tab_url.emit)
        self.webview.titleChanged.connect(self.title_changed.emit)
        self.webview.urlChanged.connect(self.url_changed.emit)
        self.webview.loadStarted.connect(lambda: self.loading_state.emit(True))
        self.webview.loadFinished.connect(lambda ok: self.loading_state.emit(False))
        # Favicon: forward iconChanged from the view so TabBar can update.
        self.webview.iconChanged.connect(self.icon_changed.emit)
        # Qt 6.4+ deprecates the certificateError() virtual override in favour of
        # a signal.  Connect both so localhost self-signed certs are accepted
        # regardless of Qt version.
        try:
            self._page.certificateError.connect(self._on_cert_error)
        except AttributeError:
            pass  # Qt < 6.4 — virtual override in EmbedWebPage handles it

        # ── JS Bridge injection ────────────────────────────────────────────
        if bridge is not None and _WEBCHANNEL_AVAILABLE:
            self._channel = QWebChannel(self._page)
            self._channel.registerObject("orbit", bridge)
            self._page.setWebChannel(self._channel)
            self._inject_bridge_scripts(profile)

        if url:
            self.navigate(url)
        else:
            self._page.setHtml(_HOME_HTML, QUrl("about:blank"))

    @staticmethod
    def _on_cert_error(error):
        """Signal-based cert handler for Qt 6.4+ (complements the virtual override)."""
        if error.url().host() in ("localhost", "127.0.0.1"):
            error.acceptCertificate()

    def _inject_bridge_scripts(self, profile: QWebEngineProfile):
        """Inject qwebchannel.js (from Qt resources) + bridge setup into every page."""
        # 1. qwebchannel.js — provided by Qt, available at this qrc path
        qwc = QWebEngineScript()
        qwc.setName("qwebchannel_js")
        qwc.setSourceUrl(QUrl("qrc:///qtwebchannel/qwebchannel.js"))
        qwc.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        qwc.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        qwc.setRunsOnSubFrames(False)
        profile.scripts().insert(qwc)

        # 2. Bridge setup — initializes window.__orbitReady / window.orbitInvoke
        setup = QWebEngineScript()
        setup.setName("orbit_bridge_setup")
        setup.setSourceCode(_BRIDGE_SETUP_JS)
        setup.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        setup.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        setup.setRunsOnSubFrames(False)
        profile.scripts().insert(setup)

    def navigate(self, url: str):
        qurl = QUrl(url) if "://" in url else QUrl.fromUserInput(url)
        self.webview.setUrl(qurl)

    def reload(self):
        self.webview.reload()

    def back(self):
        self.webview.back()

    def forward(self):
        self.webview.forward()

    def current_url(self) -> str:
        return self.webview.url().toString()

    def current_title(self) -> str:
        return self.webview.title()

# ── Tab Bar ───────────────────────────────────────────────────────────────────

class TabBar(QWidget):
    """
    Lightweight custom tab bar (no QTabBar dependency).
    Supports drag-to-reorder and per-tab close.
    """

    tab_selected      = pyqtSignal(int)
    tab_closed        = pyqtSignal(int)
    new_tab_requested = pyqtSignal()
    tab_moved         = pyqtSignal(int, int)   # from_index, to_index

    _DRAG_THRESHOLD = 6
    _TAB_H          = 32

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self._TAB_H + 2)
        self.setStyleSheet(f"background:{_C('surface')};border-bottom:1px solid {_C('border')};")

        row = QHBoxLayout(self)
        row.setContentsMargins(4, 0, 4, 0)
        row.setSpacing(2)

        self._tab_area = QWidget()
        self._tab_area.setStyleSheet("background:transparent;")
        self._tab_layout = QHBoxLayout(self._tab_area)
        self._tab_layout.setContentsMargins(0, 0, 0, 0)
        self._tab_layout.setSpacing(2)
        self._tab_layout.addStretch(1)
        row.addWidget(self._tab_area, 1)

        self._add_btn = QPushButton()
        self._add_btn.setFixedSize(28, 28)
        self._add_btn.setIcon(_icon("new_tab", _C("muted"), 16))
        self._add_btn.setIconSize(QSize(16, 16))
        self._add_btn.setToolTip("New tab  (Ctrl+T)")
        self._add_btn.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:6px;}"
            "QPushButton:hover{background:#1e1e2e;}"
        )
        self._add_btn.clicked.connect(self.new_tab_requested)
        self._add_btn.setVisible(False)   # hidden until set_new_tabs_allowed(True)
        row.addWidget(self._add_btn)

        self._tabs:   list[QWidget] = []
        self._active_idx: int       = -1
        self._drag_src  = -1
        self._drag_x0   = 0
        self._dragging  = False
        self._drop_slot = -1

    # ── Public API ────────────────────────────────────────────────────────

    def set_new_tabs_allowed(self, allowed: bool):
        """
        Show or hide the + button.  OrbitEmbed calls this at startup and
        again whenever the JS app calls setNewTabsAllowed().
        """
        self._add_btn.setVisible(allowed)

    def add_tab(self, title: str = "New Tab") -> int:
        w = self._make_pill(title)
        # Insert before the stretch (last item)
        count = self._tab_layout.count()
        self._tab_layout.insertWidget(count - 1, w)
        self._tabs.append(w)
        idx = len(self._tabs) - 1
        self.set_active(idx)
        return idx

    def set_active(self, idx: int):
        self._active_idx = idx
        for i, t in enumerate(self._tabs):
            self._style_pill(t, active=(i == idx))

    def set_title(self, idx: int, title: str):
        if 0 <= idx < len(self._tabs):
            lbl = self._tabs[idx].findChild(QLabel, "tab_lbl")
            if lbl:
                lbl.setText(title[:28] + "…" if len(title) > 30 else title)

    def set_favicon(self, idx: int, icon):
        """Update the favicon on tab pill at idx. icon may be a QIcon or None."""
        if 0 <= idx < len(self._tabs):
            fav = self._tabs[idx].findChild(QLabel, "tab_fav")
            if fav is None:
                return
            if icon and not icon.isNull():
                pm = icon.pixmap(QSize(14, 14))
                fav.setPixmap(pm)
                fav.setVisible(True)
            else:
                fav.setVisible(False)

    def count(self) -> int:
        return len(self._tabs)

    # ── Internals ─────────────────────────────────────────────────────────

    def _make_pill(self, title: str) -> QWidget:
        w = QWidget()
        w.setFixedHeight(self._TAB_H - 2)
        w.setMinimumWidth(90)
        w.setMaximumWidth(200)
        w.setCursor(Qt.CursorShape.PointingHandCursor)

        hl = QHBoxLayout(w)
        hl.setContentsMargins(8, 0, 4, 0)
        hl.setSpacing(3)

        # Favicon (16×16, hidden until the page provides one)
        fav = QLabel()
        fav.setObjectName("tab_fav")
        fav.setFixedSize(14, 14)
        fav.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        fav.setStyleSheet("background:transparent;border:none;")
        fav.setVisible(False)
        hl.addWidget(fav)

        lbl = QLabel(title[:28] + "…" if len(title) > 30 else title)
        lbl.setObjectName("tab_lbl")
        lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        hl.addWidget(lbl)

        xb = QPushButton()
        xb.setFixedSize(16, 16)
        xb.setIcon(_icon("close", _C("muted"), 12))
        xb.setIconSize(QSize(10, 10))
        xb.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:3px;}"
            "QPushButton:hover{background:#2d2d45;}"
        )
        xb.clicked.connect(lambda _, ww=w: self._close_pill(ww))
        hl.addWidget(xb)

        w.installEventFilter(self)
        return w

    def _style_pill(self, w: QWidget, active: bool):
        lbl = w.findChild(QLabel, "tab_lbl")
        if active:
            w.setStyleSheet(f"""
                QWidget {{ background:{_C('bg')};
                    border-top-left-radius:6px; border-top-right-radius:6px;
                    border:1px solid {_C('border2')}; border-bottom:1px solid {_C('bg')}; }}
            """)
            if lbl: lbl.setStyleSheet(f"color:{_C('text')};font-size:12px;background:transparent;border:none;")
        else:
            w.setStyleSheet(f"""
                QWidget {{ background:{_C('surface')};
                    border-top-left-radius:6px; border-top-right-radius:6px;
                    border:1px solid transparent; }}
                QWidget:hover {{ background:{_C('hover')}; }}
            """)
            if lbl: lbl.setStyleSheet(f"color:{_C('muted')};font-size:12px;background:transparent;border:none;")

    def _idx_of(self, w) -> int:
        return next((i for i, t in enumerate(self._tabs) if t is w), -1)

    def _close_pill(self, w: QWidget):
        idx = self._idx_of(w)
        if idx < 0: return
        self._tab_layout.removeWidget(w)
        w.deleteLater()
        self._tabs.pop(idx)
        remaining = len(self._tabs)
        if remaining == 0:
            self._active_idx = -1
        else:
            self._active_idx = min(idx, remaining - 1)
            for i, t in enumerate(self._tabs):
                self._style_pill(t, active=(i == self._active_idx))
        self.tab_closed.emit(idx)

    def eventFilter(self, obj, event):
        try:
            et = event.type()
            if et == QEvent.Type.MouseButtonPress:
                if isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.LeftButton:
                    idx = self._idx_of(obj)
                    if idx >= 0:
                        gp = QCursor.pos()
                        lp = self._tab_area.mapFromGlobal(gp)
                        self._drag_src  = idx
                        self._drag_x0   = lp.x()
                        self._dragging  = False
                        self._drop_slot = idx
                        QTimer.singleShot(0, lambda i=idx: self._deferred_select(i))
                        return True
            elif et == QEvent.Type.MouseMove:
                if self._drag_src >= 0:
                    gp = QCursor.pos()
                    lp = self._tab_area.mapFromGlobal(gp)
                    x  = lp.x()
                    if not self._dragging and abs(x - self._drag_x0) >= self._DRAG_THRESHOLD:
                        self._dragging = True
                        obj.grabMouse()
                    if self._dragging:
                        # Find drop slot by x position
                        slot = 0
                        for i, t in enumerate(self._tabs):
                            mid = t.x() + t.width() // 2
                            if x > mid: slot = i + 1
                        self._drop_slot = min(slot, len(self._tabs) - 1)
                    return True
            elif et == QEvent.Type.MouseButtonRelease:
                if isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.LeftButton:
                    if self._dragging:
                        obj.releaseMouse()
                        fr = self._drag_src
                        to = self._drop_slot
                        if fr != to and 0 <= to < len(self._tabs):
                            self._tabs.insert(to, self._tabs.pop(fr))
                            # Re-add widgets in new order
                            for t in self._tabs:
                                self._tab_layout.removeWidget(t)
                            for i, t in enumerate(self._tabs):
                                self._tab_layout.insertWidget(i, t)
                            self._active_idx = to
                            for i, t in enumerate(self._tabs):
                                self._style_pill(t, active=(i == self._active_idx))
                            self.tab_moved.emit(fr, to)
                        self._end_drag()
                        return True
                    self._end_drag()
        except Exception:
            _log("\n── tabbar eventFilter exception ──\n" + traceback.format_exc())
            self._end_drag()
        return super().eventFilter(obj, event)

    def _deferred_select(self, idx: int):
        try:
            if 0 <= idx < len(self._tabs):
                self.set_active(idx)
                self.tab_selected.emit(idx)
        except Exception:
            pass

    def _end_drag(self):
        self._drag_src = -1
        self._dragging = False
        self._drop_slot = -1
        self.unsetCursor()

# ── Nav Toolbar ───────────────────────────────────────────────────────────────

class NavBar(QWidget):
    """
    Minimal navigation bar: back, forward, reload/stop, address bar, home.

    address_bar_mode controls what the address bar displays:
      "url"    — show the stripped app:// URL  (default)
      "title"  — show the page title instead of the URL
      "hidden" — hide the address bar entirely (nav buttons still visible)
    """

    navigate    = pyqtSignal(str)
    go_back     = pyqtSignal()
    go_forward  = pyqtSignal()
    go_reload   = pyqtSignal()
    go_stop     = pyqtSignal()
    go_home     = pyqtSignal()

    def __init__(self, entry_url: str = "",
                 address_bar_mode: str = "url",
                 parent=None):
        super().__init__(parent)
        self._entry_url       = entry_url
        self._is_loading      = False
        self._address_bar_mode = address_bar_mode   # "url" | "title" | "hidden"
        self._current_title   = ""
        self.setFixedHeight(46)
        self.setStyleSheet(f"background:{_C('surface')};border-bottom:1px solid {_C('border')};")

        hl = QHBoxLayout(self)
        hl.setContentsMargins(8, 6, 8, 6)
        hl.setSpacing(4)

        def _nb(icon_name, tip, slot):
            b = QPushButton()
            b.setFixedSize(30, 30)
            b.setIcon(_icon(icon_name, _C("muted"), 18))
            b.setIconSize(QSize(18, 18))
            b.setToolTip(tip)
            b.clicked.connect(slot)
            return b

        self._back_btn   = _nb("back",   "Back  (Alt+Left)",    self.go_back)
        self._fwd_btn    = _nb("forward","Forward  (Alt+Right)", self.go_forward)
        self._reload_btn = _nb("reload", "Reload  (Ctrl+R)",     self._on_reload_click)
        self._home_btn   = _nb("home",   "Home",                 self.go_home)

        # Security lock icon (display only)
        self._lock_lbl = QLabel()
        self._lock_lbl.setPixmap(_icon("lock", _C("success"), 16).pixmap(QSize(16, 16)))
        self._lock_lbl.setFixedSize(22, 30)
        self._lock_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lock_lbl.setStyleSheet("background:transparent;")

        self._addr = QLineEdit()
        self._addr.setPlaceholderText("app:// or file:// path…")
        self._addr.returnPressed.connect(self._on_navigate)
        self._addr.setMinimumHeight(30)

        # In "hidden" mode the address bar is not visible; in "title" mode it
        # is read-only and shows the page title.
        if address_bar_mode == "hidden":
            self._addr.setVisible(False)
            self._lock_lbl.setVisible(False)
        elif address_bar_mode == "title":
            self._addr.setReadOnly(True)
            self._addr.setPlaceholderText("Page title…")

        for w in [self._back_btn, self._fwd_btn, self._reload_btn,
                  self._lock_lbl, self._addr, self._home_btn]:
            hl.addWidget(w)

    def _on_navigate(self):
        # In title mode the bar is read-only; navigation via typing is disabled
        if self._address_bar_mode != "title":
            self.navigate.emit(self._addr.text().strip())

    def _on_reload_click(self):
        if self._is_loading:
            self.go_stop.emit()
        else:
            self.go_reload.emit()

    def set_page_title(self, title: str):
        """Call this whenever the page title changes (only used in title mode)."""
        self._current_title = title
        if self._address_bar_mode == "title" and not self._addr.hasFocus():
            self._addr.setText(title)

    def set_url(self, url: str):
        # Update the lock icon regardless of display mode
        if url.startswith("app://") or url.startswith("about:"):
            color = _C("success")
        elif url.startswith("file://"):
            color = _C("warning")
        else:
            color = _C("error")
        self._lock_lbl.setPixmap(_icon("lock", color, 16).pixmap(QSize(16, 16)))

        if self._address_bar_mode != "url":
            return   # title / hidden modes don't reflect the URL in the bar

        # Strip scheme prefix for cleanliness
        display = url
        for prefix in ("app://", "about:blank"):
            if display.startswith(prefix):
                display = display[len(prefix):].lstrip("/")
                break
        if not self._addr.hasFocus():
            self._addr.setText(display)

    def set_loading(self, loading: bool):
        self._is_loading = loading
        if loading:
            self._reload_btn.setIcon(_icon("stop", _C("warning"), 18))
            self._reload_btn.setIconSize(QSize(18, 18))
            self._reload_btn.setToolTip("Stop loading")
        else:
            self._reload_btn.setIcon(_icon("reload", _C("muted"), 18))
            self._reload_btn.setIconSize(QSize(18, 18))
            self._reload_btn.setToolTip("Reload  (Ctrl+R)")

    def set_can_back(self, yes: bool):
        self._back_btn.setEnabled(yes)

    def set_can_forward(self, yes: bool):
        self._fwd_btn.setEnabled(yes)

# ── Main Window ───────────────────────────────────────────────────────────────

class OrbitEmbed(QMainWindow):
    """
    The main window. Coordinates the tab bar, nav bar, browser tabs,
    the app:// scheme handler, and the request interceptor.

    Parameters
    ----------
    root        : Path | str
        Root directory served as app://. Defaults to the directory of this script.
    entry       : str
        Entry HTML file relative to root. Defaults to "index.html".
    bridge      : OrbitBridge | None
        JS↔Python bridge instance. None disables the bridge.
    title       : str
        Window title.
    width, height : int
        Initial window dimensions.
    frameless   : bool
        Removes OS window decorations.
    show_tabs   : bool
        Show the tab bar (True) or single-tab mode (False).
    show_nav    : bool
        Show the navigation bar.
    allow_new_tabs : bool
        Whether the user can open new tabs (Ctrl+T, + button).
        Defaults to False — the embedded app controls this via the bridge
        (setNewTabsAllowed / getNewTabsAllowed).  Set True at construction
        time or pass --new-tabs on the CLI to enable immediately.
    dev_mode : bool
        Enable developer mode: F12 opens DevTools, the standard Chromium
        context menu is restored, and hot-reload watches the root directory.
    address_bar_mode : str
        Controls what the address bar shows: "url" (default, stripped app://
        path), "title" (page title, read-only), or "hidden" (bar not shown).
    allowed_hosts : set[str]
        Additional hosts to allow through the offline interceptor.
    """

    def __init__(
        self,
        root: "Path | str | None" = None,
        entry: str = "index.html",
        bridge: "OrbitBridge | None" = None,
        title: str = "App",
        width: "int | None" = None,
        height: "int | None" = None,
        frameless: bool = False,
        show_tabs: bool = True,
        show_nav: bool = True,
        allow_new_tabs: bool = False,
        allowed_hosts: "set[str] | None" = None,
        data_root: "Path | str | None" = None,
        sandboxed: bool = True,
        theme_file: "Path | str | None" = None,
        theme: "dict | None" = None,
        minimize_to_tray: bool = False,
        single_instance: bool = True,
        dev_mode: bool = False,
        address_bar_mode: str = "url",
        parent=None,
    ):
        super().__init__(parent)
        # Resolve root
        if root is None:
            self._root = Path(__file__).parent
        else:
            self._root = Path(root).resolve()

        self._entry            = entry
        self._bridge           = bridge
        self._show_tabs        = show_tabs
        self._show_nav         = show_nav
        self._entry_url        = f"app://app/{self._entry}"
        self._dev_mode         = dev_mode
        self._address_bar_mode = address_bar_mode
        self._allow_new_tabs   = allow_new_tabs

        # Resolve data root for file I/O and persistent store.
        # Defaults to a per-title directory inside ~/.orbit_embed/
        if data_root is not None:
            self._data_root = Path(data_root).resolve()
        else:
            safe_title = "".join(c if c.isalnum() or c in "-_" else "_" for c in title)
            self._data_root = Path.home() / ".orbit_embed" / safe_title / "data"
        self._data_root.mkdir(parents=True, exist_ok=True)
        self._sandboxed = sandboxed

        # ── Profile (always off-the-record — nothing written to disk) ──────
        self._profile = QWebEngineProfile(self)   # unnamed = off-the-record
        self._profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        self._profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
        )
        self._profile.setHttpUserAgent(
            "Mozilla/5.0 (compatible; OrbitEmbed/1.0; offline)"
        )

        # ── Scheme handler ─────────────────────────────────────────────────
        self._scheme_handler = AppSchemeHandler(self._root, self._entry, parent=self)
        self._profile.installUrlSchemeHandler(_EMBED_SCHEME, self._scheme_handler)

        # ── Request interceptor ────────────────────────────────────────────
        self._interceptor = EmbedInterceptor(allowed_hosts, self)
        self._profile.setUrlRequestInterceptor(self._interceptor)

        # Wire bridge back-reference and data root
        if bridge is not None:
            bridge._win = self
            bridge._sandboxed = sandboxed
            bridge._set_data_root(self._data_root)

        # Watchdog: ticks every second while the online gate is open.
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(1_000)
        self._watchdog.timeout.connect(self._on_watchdog_tick)
        self._interceptor.went_offline.connect(self._on_went_offline)

        # Download tracking: holds the gate open while downloads are in flight.
        # Keys are QWebEngineDownloadRequest objects; values are their file names
        # for logging. Only populated while the online gate is open.
        self._active_downloads: dict[int, str] = {}  # id(dl) → filename
        self._profile.downloadRequested.connect(self._on_download_requested)


        # ── Single instance ────────────────────────────────────────────────
        self._instance_server: QLocalServer | None = None
        if single_instance and _LOCALSERVER_AVAILABLE:
            self._start_instance_server(title)

        # ── System tray ────────────────────────────────────────────────────
        self._minimize_to_tray = minimize_to_tray
        self._tray: QSystemTrayIcon | None = None
        self._setup_tray(title)

        # ── Drag and drop ──────────────────────────────────────────────────
        self.setAcceptDrops(True)

        # ── Window setup ───────────────────────────────────────────────────
        if frameless:
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle(title)
        self.setWindowIcon(_orbit_icon())
        # B: Smart default size — use 85% of available screen geometry when
        # no explicit size is provided. Falls back to 1280×820 if no screen.
        _screen = QApplication.instance().primaryScreen() if QApplication.instance() else None
        _sw, _sh = _smart_size(_screen)
        self.resize(width if width is not None else _sw,
                    height if height is not None else _sh)
        self.setMinimumSize(480, 320)

        self._tab_stack: list[BrowserTab] = []

        # Load initial theme from file, dict, or defaults
        self._theme_file: Path | None = (
            Path(theme_file).resolve() if theme_file else
            (self._root / "theme.json")  # auto-detect beside entry HTML
        )
        self._apply_theme(theme or self._load_theme_file())

        # ── Hot-reload file watcher (active when dev_mode=True) ────────────
        # Watches the app root for any file change and triggers a debounced
        # reload of the active tab so changes in the editor appear instantly.
        self._hot_reload_timer  = QTimer(self)
        self._hot_reload_timer.setSingleShot(True)
        self._hot_reload_timer.setInterval(300)   # debounce: 300 ms
        self._hot_reload_timer.timeout.connect(self._do_hot_reload)
        self._fs_watcher: QFileSystemWatcher | None = None
        if dev_mode:
            self._start_hot_reload_watcher()

        self._build_ui()
        self._update_online_pill()   # set correct initial style (OFFLINE muted grey)
        self._build_shortcuts()

        # Open first tab
        self._new_tab(self._entry_url)

    # ── Online window ─────────────────────────────────────────────────────

    def go_online_for(self, idle_secs: float = 10.0, max_secs: float | None = None):
        """
        Open the temporary online window from Python.

        idle_secs  — close the gate after this many seconds with no network
                     activity (default 10). Resets on every passing request.
        max_secs   — optional hard ceiling (None = no limit, suitable for
                     large downloads where duration is unpredictable).

        While online the status bar shows an amber "● ONLINE" pill.
        Gate closes automatically on idle; JS receives a "networkLocked" event.
        """
        self._interceptor.go_online(idle_secs=idle_secs, max_secs=max_secs)
        self._watchdog.start()
        self._update_online_pill()
        ceiling = f"  max={max_secs}s" if max_secs is not None else "  (no ceiling)"
        _log(f"[embed] go_online_for idle={idle_secs}s{ceiling}")

    def go_offline(self):
        """Immediately close the online gate from Python."""
        self._interceptor.go_offline("manual")

    def _on_watchdog_tick(self):
        """Called every second while online. Delegates lock logic to interceptor."""
        self._interceptor.check_should_lock(active_downloads=len(self._active_downloads))

    def _on_went_offline(self, reason: str):
        """Called (on Qt thread) when the gate closes for any reason."""
        self._watchdog.stop()
        if self._active_downloads:
            names = list(self._active_downloads.values())
            _log(f"[embed] gate closed with {len(names)} download(s) still tracked: {names}")
            self._active_downloads.clear()
        _log(f"[embed] network locked  reason={reason}")
        self.emit_event("networkLocked", {"reason": reason})
        self._update_online_pill()

    # ── Hot reload (dev mode) ─────────────────────────────────────────────

    def _start_hot_reload_watcher(self):
        """
        Watch the app root directory tree for file changes.
        QFileSystemWatcher only monitors the top-level directory by default,
        so we also add every existing subdirectory recursively.
        On a file change the active tab reloads after a 300 ms debounce.
        New subdirectories created after startup are watched automatically.
        """
        self._fs_watcher = QFileSystemWatcher(self)
        paths = [str(self._root)]
        for sub in self._root.rglob("*"):
            if sub.is_dir():
                paths.append(str(sub))
        self._fs_watcher.addPaths(paths)
        self._fs_watcher.fileChanged.connect(self._on_fs_change)
        self._fs_watcher.directoryChanged.connect(self._on_dir_change)
        _log(f"[hot-reload] watching {len(paths)} path(s) under {self._root}")

    def _on_dir_change(self, path: str):
        """
        Called when a directory's contents change (file added/removed/renamed).
        Re-scans the directory to pick up any new subdirectories so they also
        get watched, then triggers the debounced reload.
        """
        if self._fs_watcher:
            # Add any newly created subdirs that aren't yet watched
            try:
                new_dirs = [
                    str(p) for p in Path(path).iterdir()
                    if p.is_dir() and str(p) not in self._fs_watcher.directories()
                ]
                if new_dirs:
                    self._fs_watcher.addPaths(new_dirs)
                    _log(f"[hot-reload] watching {len(new_dirs)} new subdir(s)", level="debug")
            except Exception:
                pass
        self._on_fs_change(path)

    def _on_fs_change(self, path: str):
        """Debounce file-system events and schedule a reload."""
        _log(f"[hot-reload] change detected: {path}", level="debug")
        self._hot_reload_timer.start()   # restart timer on each event

    def _do_hot_reload(self):
        """Reload the active tab after the debounce window expires."""
        tab = self._active_tab()
        if tab:
            _log("[hot-reload] reloading active tab")
            tab.reload()
            self.emit_event("hotReload", {"path": str(self._root)})

    def _on_download_requested(self, dl: QWebEngineDownloadRequest):
        """
        Called by Qt when a download starts. Only tracks downloads while the
        online gate is open — the app must call requestOnline first.
        """
        dl.accept()
        if not self._interceptor.is_online:
            return
        key  = id(dl)
        name = dl.downloadFileName() or dl.url().fileName() or "unknown"
        self._active_downloads[key] = name
        _log(f"[download] tracking started: {name}  ({key})")
        self._update_online_pill()
        self.emit_event("downloadStarted", {"file": name})
        dl.stateChanged.connect(
            lambda state, k=key, d=dl: self._on_download_state_changed(k, d, state)
        )

    def _on_download_state_changed(
        self, key: int, dl: QWebEngineDownloadRequest,
        state: QWebEngineDownloadRequest.DownloadState
    ):
        """Remove download from tracking when it completes, cancels, or errors."""
        DS = QWebEngineDownloadRequest.DownloadState
        terminal = {DS.DownloadCompleted, DS.DownloadCancelled, DS.DownloadInterrupted}
        if state in terminal:
            name = self._active_downloads.pop(key, "unknown")
            reason_str = {
                DS.DownloadCompleted:   "completed",
                DS.DownloadCancelled:   "cancelled",
                DS.DownloadInterrupted: "interrupted",
            }.get(state, "done")
            _log(f"[download] {reason_str}: {name}  remaining={len(self._active_downloads)}")
            self._update_online_pill()
            self.emit_event("downloadFinished", {
                "file": name, "status": reason_str,
                "activeDownloads": len(self._active_downloads)
            })

    def _update_online_pill(self):
        """
        Refresh the status bar network state pill.
        Always visible — muted grey when offline, amber when online.
        """
        if not self._interceptor.is_online:
            self._online_lbl.setText("● OFFLINE")
            self._online_lbl.setStyleSheet(
                f"color:{_C('muted')};font-family:'Courier New';font-size:10px;"
                f"padding:1px 8px;border-radius:4px;margin-right:8px;"
            )
        else:
            n = len(self._active_downloads)
            self._online_lbl.setText(
                f"● ONLINE  ↓ {n} downloading" if n > 0 else "● ONLINE"
            )
            self._online_lbl.setStyleSheet(
                f"color:{_C('warning')};font-family:'Courier New';font-size:10px;"
                f"padding:1px 8px;border-radius:4px;margin-right:8px;"
            )

    # ── Single instance ───────────────────────────────────────────────────

    def _start_instance_server(self, title: str):
        """
        Create a local socket server named after the app title.
        A second instance will connect to this server, which triggers
        the first instance to raise its window, then the second exits.
        """
        name = f"orbit_embed_{title.replace(' ', '_').lower()}"
        # Clean up stale socket from a previous crash
        QLocalServer.removeServer(name)
        self._instance_server = QLocalServer(self)
        self._instance_server.newConnection.connect(self._on_instance_connection)
        if self._instance_server.listen(name):
            _log(f"[instance] listening on {name}")
        else:
            _log(f"[instance] server listen failed: {self._instance_server.errorString()}")

    def _on_instance_connection(self):
        """A second instance connected — raise our window and acknowledge."""
        conn = self._instance_server.nextPendingConnection()
        payload = b""
        if conn:
            conn.waitForReadyRead(200)
            payload = bytes(conn.readAll())
            conn.write(b"raise")
            conn.flush()
        _log("[instance] second instance detected — raising window")
        self.setWindowState(
            self.windowState() & ~Qt.WindowState.WindowMinimized
        )
        self.raise_()
        self.activateWindow()

        # If the second instance was launched with --url, fire protocolActivated
        # here in the first instance rather than the usual instanceRaised event.
        if payload.startswith(b"url:"):
            url = payload[4:].decode("utf-8", errors="replace")
            _log(f"[instance] protocolActivated from second instance: {url}")
            self.emit_event("protocolActivated", {"url": url})
        else:
            self.emit_event("instanceRaised", {})

    # ── System tray ───────────────────────────────────────────────────────

    def _setup_tray(self, title: str):
        """Build the system tray icon and context menu."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            _log("[tray] system tray not available on this platform")
            return
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(_orbit_icon())
        self._tray.setToolTip(title)
        menu = QMenu()
        menu.addAction("Show",  self._tray_show)
        menu.addAction("Minimize", self.showMinimized)
        menu.addSeparator()
        menu.addAction("Quit", QApplication.instance().quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()
        _log("[tray] system tray icon created")

    def _tray_show(self):
        """Restore and raise the window from tray."""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason):
        """Double-click on tray icon restores the window."""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._tray_show()

    def closeEvent(self, event: QEvent):
        """Minimize to tray instead of closing if _minimize_to_tray is set."""
        if self._minimize_to_tray and self._tray and self._tray.isVisible():
            event.ignore()
            self.hide()
            self._tray.showMessage(
                self.windowTitle(),
                "App is still running in the system tray.",
                QSystemTrayIcon.MessageIcon.Information,
                2000
            )
            self.emit_event("minimizedToTray", {})
        else:
            if self._tray:
                self._tray.hide()
            if self._instance_server:
                self._instance_server.close()
            event.accept()

    # ── Drag and drop ─────────────────────────────────────────────────────

    def dragEnterEvent(self, event):
        """Accept drag events that contain file URLs."""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        """
        Intercept file drops at the window level.
        Emits a "fileDrop" bridge event with real filesystem paths:
            {files: [{path, name, size, modified}]}
        The HTML app receives this and handles it however it needs.
        """
        urls = event.mimeData().urls()
        files = []
        for url in urls:
            p = Path(url.toLocalFile())
            if p.exists():
                try:
                    st = p.stat()
                    files.append({
                        "path":     str(p),
                        "name":     p.name,
                        "size":     st.st_size,
                        "modified": st.st_mtime,
                        "isDir":    p.is_dir(),
                    })
                except Exception as e:
                    _log(f"[drop] stat failed for {p}: {e}")
        if files:
            _log(f"[drop] {len(files)} file(s) dropped")
            self.emit_event("fileDrop", {"files": files})
        event.acceptProposedAction()

    # ── Theme ─────────────────────────────────────────────────────────────

    def _load_theme_file(self) -> dict:
        """
        Try to load theme.json from the app root directory.
        Returns an empty dict (defaults apply) if not found or invalid.
        """
        candidates = [
            self._theme_file,
            self._root / "theme.json",
        ]
        for candidate in candidates:
            if candidate and candidate.is_file():
                try:
                    with open(candidate, encoding="utf-8") as f:
                        data = json.load(f)
                    _log(f"[theme] loaded from {candidate}")
                    return data
                except Exception as e:
                    _log(f"[theme] failed to load {candidate}: {e}")
        return {}

    def _apply_theme(self, partial: dict):
        """
        Resolve and apply a (possibly partial) theme dict.
        Updates the module-level _THEME, regenerates QSS, repaints all
        chrome widgets, and sets the Windows 11 title bar color.
        Called at startup and by setTheme from JS or Python.
        """
        global _THEME
        _THEME = _resolve_theme(partial)
        qss    = _build_qss(_THEME, scale=_dpi_scale())
        QApplication.instance().setStyleSheet(qss)
        # Repaint chrome widgets that set their own inline styles
        if hasattr(self, 'tab_bar') and self.tab_bar:
            self.tab_bar.setStyleSheet(
                f"background:{_C('surface')};border-bottom:1px solid {_C('border')};"
            )
            # Re-style all existing tab pills
            for i, t in enumerate(self.tab_bar._tabs):
                self.tab_bar._style_pill(t, active=(i == self.tab_bar._active_idx))
        if hasattr(self, 'nav_bar') and self.nav_bar:
            self.nav_bar.setStyleSheet(
                f"background:{_C('surface')};border-bottom:1px solid {_C('border')};"
            )
            # Refresh the address bar display for the current mode
            tab = self._active_tab() if hasattr(self, '_tab_stack') else None
            if self.nav_bar._address_bar_mode == "title":
                self.nav_bar.set_page_title(tab.current_title() if tab else "")
            else:
                self.nav_bar.set_url(self.nav_bar._addr.text())
        if hasattr(self, '_status_lbl'):
            self._status_lbl.setStyleSheet(
                f"color:{_C('muted')};font-family:'Courier New';font-size:10px;"
            )
        if hasattr(self, '_online_lbl'):
            self._update_online_pill()   # reapply correct style for current state
        self._set_titlebar_color(_THEME['bg'])
        _log(f"[theme] applied  bg={_THEME['bg']}  accent={_THEME['accent']}")

    def _set_titlebar_color(self, color: str):
        """
        Set the OS title bar background color.
        Windows 11: uses the DWM DWMWA_CAPTION_COLOR attribute via ctypes.
        macOS / Linux: no-op (frameless mode is the practical alternative).
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = int(self.winId())
            r, g, b = _hex_to_rgb(color)
            # COLORREF is 0x00BBGGRR
            colorref = b << 16 | g << 8 | r
            DWMWA_CAPTION_COLOR = 35
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                DWMWA_CAPTION_COLOR,
                ctypes.byref(ctypes.c_int(colorref)),
                ctypes.sizeof(ctypes.c_int)
            )
        except Exception as e:
            _log(f"[theme] title bar color failed: {e}")

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_lay = QVBoxLayout(central)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        # Tab bar
        if self._show_tabs:
            self.tab_bar = TabBar()
            self.tab_bar.tab_selected.connect(self._on_tab_selected)
            self.tab_bar.tab_closed.connect(self._on_tab_closed)
            self.tab_bar.tab_moved.connect(self._on_tab_moved)
            # + button and new_tab signal only active when new tabs are allowed
            self.tab_bar.set_new_tabs_allowed(self._allow_new_tabs)
            if self._allow_new_tabs:
                self.tab_bar.new_tab_requested.connect(self._new_tab)
            root_lay.addWidget(self.tab_bar)
        else:
            self.tab_bar = None

        # Nav bar
        if self._show_nav:
            self.nav_bar = NavBar(entry_url=self._entry_url,
                                  address_bar_mode=self._address_bar_mode)
            self.nav_bar.navigate.connect(self._navigate_to)
            self.nav_bar.go_back.connect(self._go_back)
            self.nav_bar.go_forward.connect(self._go_forward)
            self.nav_bar.go_reload.connect(self._reload)
            self.nav_bar.go_stop.connect(self._stop)
            self.nav_bar.go_home.connect(self._go_home)
            root_lay.addWidget(self.nav_bar)
        else:
            self.nav_bar = None

        # Content area
        self._content = QWidget()
        self._content.setStyleSheet(f"background:{_C('bg')};")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(0)
        root_lay.addWidget(self._content, 1)

        # Status bar (minimal)
        self._sb = QStatusBar()
        sb = self._sb
        sb.setStyleSheet(f"QStatusBar{{background:{_C('surface')};border-top:1px solid {_C('border')};}}")
        self.setStatusBar(sb)
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet(f"color:{_C('muted')};font-family:'Courier New';font-size:10px;")
        sb.addWidget(self._status_lbl)

        # DEV MODE badge — shown when --dev is active
        if self._dev_mode:
            dev_badge = QLabel("⚙ DEV MODE")
            dev_badge.setStyleSheet(
                f"color:{_C('warning')};font-family:'Courier New';font-size:10px;"
                f"padding:1px 8px;border:1px solid {_C('warning')};border-radius:4px;"
                f"margin-right:4px;"
            )
            dev_badge.setToolTip(
                "Dev mode active: DevTools (F12), context menu, and hot-reload are enabled."
            )
            sb.addWidget(dev_badge)

        ver_lbl = QLabel(f"ORBIT EMBED v{APP_VERSION}")
        ver_lbl.setStyleSheet(f"color:{_C('border2')};font-family:'Courier New';font-size:10px;padding-right:8px;")
        sb.addPermanentWidget(ver_lbl)
        # Persistent network state indicator — always visible.
        # _update_online_pill() sets the correct text and style on first call.
        # Shows: ● OFFLINE (muted grey) / ● ONLINE (amber) / ● ONLINE ↓N (amber)
        self._online_lbl = QLabel("● OFFLINE")
        sb.addPermanentWidget(self._online_lbl)

    def _build_shortcuts(self):
        def sc(keys, fn):
            s = QShortcut(QKeySequence(keys), self)
            s.activated.connect(fn)

        sc("Alt+Left",       self._go_back)
        sc("Alt+Right",      self._go_forward)
        sc("Alt+Home",       self._go_home)
        sc("Ctrl+R",         self._reload)
        sc("F5",             self._reload)
        sc("Escape",         self._stop)
        sc("Ctrl+L",         self._focus_address)
        # Ctrl+T: only active when new tabs are permitted AND the tab bar is shown
        if self._show_tabs and self._allow_new_tabs:
            sc("Ctrl+T",     self._new_tab)
        sc("Ctrl+W",         self._close_current_tab)
        sc("Ctrl+Tab",       self._next_tab)
        sc("Ctrl+Shift+Tab", self._prev_tab)
        sc("F11",            self._toggle_fullscreen)
        if self._dev_mode:
            sc("F12",        self._open_devtools)
        for i in range(1, 10):
            sc(f"Ctrl+{i}", lambda idx=i-1: self._switch_tab(idx))

    # ── Tab management ────────────────────────────────────────────────────

    def _new_tab(self, url: str = ""):
        effective_url = url or self._entry_url
        tab = BrowserTab(
            profile=self._profile,
            bridge=self._bridge,
            url=effective_url,
            dev_mode=self._dev_mode,
            allowed_hosts=self._interceptor._allowed,
            parent=self,
        )
        tab.title_changed.connect(lambda t, tab=tab: self._on_title_changed(tab, t))
        tab.url_changed.connect(lambda u, tab=tab: self._on_url_changed(tab, u))
        tab.loading_state.connect(lambda v, tab=tab: self._on_loading(tab, v))
        tab.new_tab_url.connect(
            lambda u: self._new_tab(u.toString()) if self._allow_new_tabs else None
        )
        tab.icon_changed.connect(lambda icon, tab=tab: self._on_icon_changed(tab, icon))

        self._tab_stack.append(tab)
        self._content_layout.addWidget(tab)

        if self.tab_bar:
            self.tab_bar.add_tab("Loading…")

        self._switch_to_tab(len(self._tab_stack) - 1)

    def _active_tab(self) -> BrowserTab | None:
        idx = self._active_idx()
        if 0 <= idx < len(self._tab_stack):
            return self._tab_stack[idx]
        return None

    def _active_idx(self) -> int:
        if not self._tab_stack:
            return -1
        if self.tab_bar:
            return self.tab_bar._active_idx
        return 0

    def _switch_to_tab(self, idx: int):
        for i, tab in enumerate(self._tab_stack):
            tab.setVisible(i == idx)
        if self.tab_bar:
            self.tab_bar.set_active(idx)
        tab = self._tab_stack[idx] if 0 <= idx < len(self._tab_stack) else None
        if tab and self.nav_bar:
            self.nav_bar.set_url(tab.current_url())
            # In title mode, restore the correct tab's title in the address bar
            if self.nav_bar._address_bar_mode == "title":
                self.nav_bar.set_page_title(tab.current_title())
        self._update_nav_buttons()

    def _on_tab_selected(self, idx: int):
        self._switch_to_tab(idx)

    def _on_tab_closed(self, idx: int):
        if idx < len(self._tab_stack):
            tab = self._tab_stack.pop(idx)
            self._content_layout.removeWidget(tab)
            tab.deleteLater()
        if not self._tab_stack:
            self._new_tab()
        else:
            new_idx = min(idx, len(self._tab_stack) - 1)
            self._switch_to_tab(new_idx)

    def _on_tab_moved(self, fr: int, to: int):
        self._tab_stack.insert(to, self._tab_stack.pop(fr))
        self._switch_to_tab(to)

    def _on_title_changed(self, tab: BrowserTab, title: str):
        idx = self._tab_stack.index(tab) if tab in self._tab_stack else -1
        if self.tab_bar and idx >= 0:
            self.tab_bar.set_title(idx, title or "Untitled")
        if tab is self._active_tab():
            self.setWindowTitle(title or self.windowTitle())
            # In "title" address-bar mode, show page title in the address bar
            if self.nav_bar:
                self.nav_bar.set_page_title(title or "")

    def _on_url_changed(self, tab: BrowserTab, url: QUrl):
        if tab is self._active_tab() and self.nav_bar:
            self.nav_bar.set_url(url.toString())
        self._update_nav_buttons()

    def _on_icon_changed(self, tab: BrowserTab, icon):
        """Update the favicon in the tab bar when the page favicon changes."""
        idx = self._tab_stack.index(tab) if tab in self._tab_stack else -1
        if self.tab_bar and idx >= 0:
            self.tab_bar.set_favicon(idx, icon)

    def _open_devtools(self):
        """Open Chromium DevTools for the active tab (dev mode only)."""
        tab = self._active_tab()
        if tab:
            tab._page.triggerAction(QWebEnginePage.WebAction.OpenDevTools)
            _log("[dev] DevTools opened")

    def _on_loading(self, tab: BrowserTab, loading: bool):
        if tab is self._active_tab():
            if self.nav_bar:
                self.nav_bar.set_loading(loading)
            self._status_lbl.setText("Loading…" if loading else "Ready")

    def _update_nav_buttons(self):
        tab = self._active_tab()
        if tab and self.nav_bar:
            self.nav_bar.set_can_back(tab.webview.history().canGoBack())
            self.nav_bar.set_can_forward(tab.webview.history().canGoForward())

    # ── Navigation actions ────────────────────────────────────────────────

    def _navigate_to(self, url: str):
        tab = self._active_tab()
        if tab:
            # If user types a bare path, treat as relative to root
            if not any(url.startswith(s) for s in ["app://", "file://", "http://", "https://", "about:", "data:"]):
                url = f"app://app/{url}"
            tab.navigate(url)

    def _go_back(self):
        if tab := self._active_tab(): tab.back()

    def _go_forward(self):
        if tab := self._active_tab(): tab.forward()

    def _reload(self):
        if tab := self._active_tab(): tab.reload()

    def _stop(self):
        if tab := self._active_tab(): tab.webview.stop()

    def _go_home(self):
        if tab := self._active_tab(): tab.navigate(self._entry_url)

    def _close_current_tab(self):
        idx = self._active_idx()
        if idx < 0:
            return
        if self.tab_bar and 0 <= idx < len(self.tab_bar._tabs):
            self.tab_bar._close_pill(self.tab_bar._tabs[idx])
        elif not self.tab_bar:
            self._on_tab_closed(0)

    def _next_tab(self):
        n = len(self._tab_stack)
        if n > 1:
            self._switch_to_tab((self._active_idx() + 1) % n)

    def _prev_tab(self):
        n = len(self._tab_stack)
        if n > 1:
            self._switch_to_tab((self._active_idx() - 1) % n)

    def _switch_tab(self, idx: int):
        if 0 <= idx < len(self._tab_stack):
            self._switch_to_tab(idx)
            if self.tab_bar:
                self.tab_bar.set_active(idx)

    def _focus_address(self):
        if self.nav_bar:
            self.nav_bar._addr.setFocus()
            self.nav_bar._addr.selectAll()

    def _toggle_fullscreen(self):
        """Toggle true OS fullscreen. Hides chrome bars while fullscreen."""
        if self.isFullScreen():
            self.showNormal()
            self._set_chrome_visible(True)
        else:
            self._set_chrome_visible(False)
            self.showFullScreen()

    def _set_chrome_visible(self, visible: bool):
        """Show or hide the tab bar and nav bar (used for fullscreen toggle)."""
        if hasattr(self, "tab_bar") and self.tab_bar:
            self.tab_bar.setVisible(visible and self._show_tabs)
        if hasattr(self, "nav_bar") and self.nav_bar:
            self.nav_bar.setVisible(visible and self._show_nav)
        if hasattr(self, "_sb") and self._sb:
            self._sb.setVisible(visible)

    def changeEvent(self, event: QEvent):
        """Sync chrome visibility when fullscreen state changes externally
        (e.g. OS-initiated or double-click on title bar)."""
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._set_chrome_visible(not self.isFullScreen())

    # ── Public API ────────────────────────────────────────────────────────

    def emit_event(self, name: str, data=None):
        """Send a named event to all JS listeners across all tabs."""
        if self._bridge:
            self._bridge.emit_event(name, data)

    def set_new_tabs_allowed(self, allowed: bool):
        """
        Enable or disable user-initiated new tabs (Ctrl+T, + button).
        Called internally when the JS bridge fires setNewTabsAllowed.
        Can also be called directly from Python after construction.
        """
        self._allow_new_tabs = allowed
        if self.tab_bar:
            self.tab_bar.set_new_tabs_allowed(allowed)
            if allowed:
                try:
                    self.tab_bar.new_tab_requested.disconnect(self._new_tab)
                except RuntimeError:
                    pass
                self.tab_bar.new_tab_requested.connect(self._new_tab)
            else:
                try:
                    self.tab_bar.new_tab_requested.disconnect(self._new_tab)
                except RuntimeError:
                    pass
        _log(f"[embed] new tabs {'enabled' if allowed else 'disabled'}")

    def allow_host(self, host: str):
        """Dynamically allow an additional host (e.g. 'localhost:3000')."""
        self._interceptor.allow_host(host)

    def set_status(self, text: str):
        """Update the status bar text from Python."""
        self._status_lbl.setText(text)

    def apply_theme(self, partial: dict):
        """
        Apply a (partial) theme dict from Python at any time.
        Same as calling orbitInvoke("setTheme", [{...}]) from JS.

        Example:
            win.apply_theme({"bg": "#1a1a2e", "accent": "#e94560"})
        """
        self._apply_theme(partial)

    def navigate(self, url: str):
        """Navigate the active tab to a URL."""
        self._navigate_to(url)

# ── OS Protocol Handler Registration ─────────────────────────────────────────
#
# Allows other apps (or browsers typing myapp:// in the address bar) to launch
# an orbit_embed app by registering a custom URL scheme with the OS.
#
# Usage:
#   from orbit_embed import register_protocol_handler
#   register_protocol_handler("myapp", sys.executable, __file__, title="My App")
#
# This writes the OS-level registry/plist entry so that myapp:// URLs launch:
#   python orbit_embed.py --url "%1"   (Windows)
#   python orbit_embed.py --url %u     (Linux/macOS)
#
# The running orbit_embed instance receives the URL via the "protocolActivated"
# bridge event: { url: "myapp://path/to/thing?param=value" }
# (single-instance enforcement means the first instance gets the call).

def register_protocol_handler(
    scheme: str,
    python_exe: str | None = None,
    script_path: str | None = None,
    title: str | None = None,
) -> bool:
    """
    Register a custom URL scheme with the OS so that scheme:// URLs
    launch this orbit_embed script.

    Parameters
    ----------
    scheme      : URL scheme to register, e.g. "myapp"
    python_exe  : Path to the Python interpreter (defaults to sys.executable)
    script_path : Path to orbit_embed.py (defaults to __file__)
    title       : Human-readable name for the handler (Windows registry only)

    Returns True if registration succeeded, False otherwise.
    Requires appropriate OS permissions (elevated on Windows, user-level on Linux/macOS).

    Windows  : writes to HKEY_CURRENT_USER\\Software\\Classes\\<scheme>
    Linux    : writes ~/.local/share/applications/orbit-embed-<scheme>.desktop
    macOS    : writes ~/Library/LaunchAgents/orbit-embed-<scheme>.plist
               (requires user to log out / log in, or launchctl load)
    """
    exe    = python_exe  or sys.executable
    script = script_path or str(Path(__file__).resolve())
    name   = title or f"Orbit Embed ({scheme}://)"
    scheme = scheme.lower().strip()

    try:
        if sys.platform == "win32":
            import winreg
            base = fr"Software\Classes\{scheme}"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, name)
                winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
            cmd_key = fr"{base}\shell\open\command"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cmd_key) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ,
                                  f'"{exe}" "{script}" --url "%1"')
            _log(f"[protocol] registered {scheme}:// in HKCU\\Software\\Classes")
            return True

        elif sys.platform == "linux":
            desktop_dir = Path.home() / ".local" / "share" / "applications"
            desktop_dir.mkdir(parents=True, exist_ok=True)
            desktop_file = desktop_dir / f"orbit-embed-{scheme}.desktop"
            desktop_file.write_text(
                f"[Desktop Entry]\n"
                f"Name={name}\n"
                f"Exec={exe} {script} --url %u\n"
                f"Type=Application\n"
                f"NoDisplay=true\n"
                f"MimeType=x-scheme-handler/{scheme};\n",
                encoding="utf-8"
            )
            import subprocess
            subprocess.run(
                ["xdg-mime", "default",
                 f"orbit-embed-{scheme}.desktop",
                 f"x-scheme-handler/{scheme}"],
                check=False
            )
            subprocess.run(
                ["update-desktop-database", str(desktop_dir)],
                check=False
            )
            _log(f"[protocol] registered {scheme}:// via .desktop: {desktop_file}")
            return True

        elif sys.platform == "darwin":
            plist_dir  = Path.home() / "Library" / "LaunchAgents"
            plist_dir.mkdir(parents=True, exist_ok=True)
            plist_file = plist_dir / f"orbit-embed-{scheme}.plist"
            plist_file.write_text(
                f'<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
                f' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                f'<plist version="1.0"><dict>\n'
                f'  <key>Label</key><string>orbit-embed-{scheme}</string>\n'
                f'  <key>ProgramArguments</key><array>\n'
                f'    <string>{exe}</string>\n'
                f'    <string>{script}</string>\n'
                f'    <string>--url</string><string>%u</string>\n'
                f'  </array>\n'
                f'  <key>LSHandlerRank</key><string>Owner</string>\n'
                f'  <key>CFBundleURLTypes</key><array><dict>\n'
                f'    <key>CFBundleURLSchemes</key>'
                f'<array><string>{scheme}</string></array>\n'
                f'  </dict></array>\n'
                f'</dict></plist>\n',
                encoding="utf-8"
            )
            _log(f"[protocol] registered {scheme}:// via LaunchAgent: {plist_file}")
            _log(f"[protocol] run: launchctl load {plist_file}")
            return True

        else:
            _log(f"[protocol] unsupported platform: {sys.platform}")
            return False

    except Exception as e:
        _log(f"[protocol] registration failed: {e}")
        return False


def unregister_protocol_handler(scheme: str) -> bool:
    """
    Remove the OS protocol handler registration created by register_protocol_handler().
    Returns True if unregistration succeeded, False otherwise.
    """
    scheme = scheme.lower().strip()
    try:
        if sys.platform == "win32":
            import winreg
            base = fr"Software\Classes\{scheme}"
            # Delete sub-keys before the parent (registry requirement)
            for sub in [
                fr"{base}\shell\open\command",
                fr"{base}\shell\open",
                fr"{base}\shell",
                base,
            ]:
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
                except FileNotFoundError:
                    pass
            _log(f"[protocol] unregistered {scheme}:// from registry")
            return True

        elif sys.platform == "linux":
            desktop_file = (Path.home() / ".local" / "share" / "applications"
                            / f"orbit-embed-{scheme}.desktop")
            if desktop_file.exists():
                desktop_file.unlink()
                import subprocess
                subprocess.run(
                    ["update-desktop-database",
                     str(desktop_file.parent)],
                    check=False
                )
            _log(f"[protocol] unregistered {scheme}:// desktop entry")
            return True

        elif sys.platform == "darwin":
            plist_file = (Path.home() / "Library" / "LaunchAgents"
                          / f"orbit-embed-{scheme}.plist")
            if plist_file.exists():
                import subprocess
                subprocess.run(["launchctl", "unload", str(plist_file)], check=False)
                plist_file.unlink()
            _log(f"[protocol] unregistered {scheme}:// LaunchAgent")
            return True

        else:
            return False

    except Exception as e:
        _log(f"[protocol] unregistration failed: {e}")
        return False

# ── CLI Entry Point ───────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ORBIT EMBED — offline HTML app shell",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("DEPENDENCIES")[0].strip(),
    )
    p.add_argument("--root",        default=None,
                   help="Root directory to serve as app:// (default: script dir)")
    p.add_argument("--entry",       default="index.html",
                   help="Entry HTML file relative to root (default: index.html)")
    p.add_argument("--title",       default="App",
                   help="Window title")
    p.add_argument("--width",       type=int, default=1280,
                   help="Window width  (default: 1280)")
    p.add_argument("--height",      type=int, default=820,
                   help="Window height (default: 820)")
    p.add_argument("--frameless",   action="store_true",
                   help="Frameless window (no OS chrome)")
    p.add_argument("--no-tabs",     action="store_true",
                   help="Disable tab bar (single-tab mode)")
    p.add_argument("--no-nav",      action="store_true",
                   help="Hide navigation bar")
    p.add_argument("--allow-host",  action="append", dest="allow_hosts", default=[],
                   metavar="HOST",
                   help="Allow http/https to HOST (e.g. localhost:3000). Repeat for multiple.")
    p.add_argument("--kiosk",       action="store_true",
                   help="Kiosk mode: --frameless + --no-nav + --no-tabs")
    p.add_argument("--data-root",   default=None, dest="data_root",
                   help="Directory for file I/O and persistent store "
                        "(default: ~/.orbit_embed/<title>/data/)")
    p.add_argument("--no-sandbox",  action="store_true", dest="no_sandbox",
                   help="Allow file I/O outside data-root (full filesystem access)")
    p.add_argument("--theme",       default=None,
                   help="Path to theme.json file (default: <root>/theme.json if present)")
    p.add_argument("--screen",      type=int, default=None, dest="screen_idx",
                   help="Monitor index to launch on (0=primary). Default: primary screen.")
    p.add_argument("--minimize-to-tray", action="store_true", dest="minimize_to_tray",
                   help="Minimize to system tray instead of closing the window")
    p.add_argument("--no-single-instance", action="store_true", dest="no_single_instance",
                   help="Allow multiple instances to run simultaneously")
    p.add_argument("--new-tabs",     action="store_true", dest="allow_new_tabs",
                   help="Allow the user to open new tabs via Ctrl+T and the + button. "
                        "Disabled by default — the embedded app controls this via the bridge.")
    p.add_argument("--dev",          action="store_true", dest="dev_mode",
                   help="Dev mode: enable DevTools (F12), context menu, and hot-reload")
    p.add_argument("--address-bar-mode", default="url", dest="address_bar_mode",
                   choices=["url", "title", "hidden"],
                   help="Address bar display: 'url' (default), 'title' (page title), "
                        "'hidden' (address bar hidden)")
    p.add_argument("--log",          default=None, dest="log_path",
                   help="Path to log file (default: ~/orbit_embed.log).  Pass 'none' to disable.")
    p.add_argument("--quiet",        action="store_true",
                   help="Suppress all stdout output (log file still written unless --log none)")
    p.add_argument("--verbose",      action="store_true",
                   help="Log debug-level messages (file-system events, etc.)")
    p.add_argument("--url",          default=None, metavar="URL",
                   help="URL to open on launch (e.g. myapp://open?doc=123). "
                        "Fires a 'protocolActivated' bridge event instead of navigating.")
    p.add_argument("--register-protocol", default=None, metavar="SCHEME",
                   dest="register_protocol",
                   help="Register SCHEME:// as an OS protocol handler for this script "
                        "(e.g. --register-protocol myapp), then exit.")
    p.add_argument("--unregister-protocol", default=None, metavar="SCHEME",
                   dest="unregister_protocol",
                   help="Remove the OS protocol handler for SCHEME:// and exit.")
    return p


def main():
    args = _build_arg_parser().parse_args()

    # ── Logging configuration ──────────────────────────────────────────────
    global _LOG_PATH, _LOG_QUIET, _LOG_VERBOSE
    if args.log_path:
        if args.log_path.lower() == "none":
            # Disable file logging — redirect to /dev/null equivalent
            _LOG_PATH = Path(os.devnull)
        else:
            _LOG_PATH = Path(args.log_path).expanduser().resolve()
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LOG_QUIET   = args.quiet
    _LOG_VERBOSE = args.verbose
    try:
        _LOG_PATH.write_text("=== orbit_embed session ===\n", encoding="utf-8")
    except Exception:
        pass
    _log(f"[orbit_embed] Python {sys.version}")
    if args.dev_mode:
        _log("[orbit_embed] DEV MODE active — hot-reload, DevTools, context menu enabled")

    # ── Protocol handler registration (no window needed) ──────────────────
    if args.register_protocol:
        ok = register_protocol_handler(args.register_protocol)
        print(f"{'OK' if ok else 'FAILED'}: {args.register_protocol}:// protocol handler "
              f"{'registered' if ok else 'registration failed'}")
        sys.exit(0 if ok else 1)

    if args.unregister_protocol:
        ok = unregister_protocol_handler(args.unregister_protocol)
        print(f"{'OK' if ok else 'FAILED'}: {args.unregister_protocol}:// protocol handler "
              f"{'unregistered' if ok else 'unregistration failed'}")
        sys.exit(0 if ok else 1)

    _kiosk = args.kiosk
    if _kiosk:
        args.no_nav  = True
        args.no_tabs = True
        # frameless not set — showFullScreen() handles window decorations.

    # ── A: HiDPI — must be set before QApplication ────────────────────
    # Qt 6 enables high-DPI scaling by default, but we set these
    # explicitly for clarity and Windows compatibility.
    # Must come BEFORE QApplication — including the single-instance probe.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    # Windows: declare process DPI awareness via DWM so the OS does not
    # bitmap-scale the window. Must happen before QApplication on Win 10+.
    if sys.platform == "win32":
        try:
            import ctypes
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
            ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
        except Exception:
            pass  # Older Windows — Qt fallback handles it

    # ── Single instance check — uses QLocalSocket before full QApplication
    # QLocalSocket needs a QCoreApplication event loop to exist.
    # We create the real QApplication here so HiDPI policy is already set.
    app = QApplication.instance() or QApplication(sys.argv)

    if not args.no_single_instance and _LOCALSERVER_AVAILABLE:
        safe_title = (args.title or "App").replace(" ", "_").lower()
        name       = f"orbit_embed_{safe_title}"
        probe      = QLocalSocket()
        probe.connectToServer(name)
        if probe.waitForConnected(300):
            _log("[instance] another instance is running — activating it and exiting")
            # If launched with --url, pass it to the running instance so it fires
            # a protocolActivated event there instead of starting a new window.
            payload = f"url:{args.url}" if args.url else "raise"
            probe.write(payload.encode("utf-8"))
            probe.flush()
            probe.disconnectFromServer()
            sys.exit(0)
        probe.close()

    # QApplication already created above
    app.setStyle("Fusion")
    app.setWindowIcon(_orbit_icon())

    # ── A: Read DPI scale now that QApplication + screen are available ─
    global _DPI_SCALE
    _DPI_SCALE = _dpi_scale()
    _log(f"[orbit_embed] DPI scale={_DPI_SCALE:.2f}")
    app.setStyleSheet(_build_qss(_THEME, scale=_DPI_SCALE))

    # ── B: Resolve target screen ───────────────────────────────────────
    screens = app.screens()
    if args.screen_idx is not None and args.screen_idx < len(screens):
        target_screen = screens[args.screen_idx]
    else:
        target_screen = app.primaryScreen()
    _log(f"[orbit_embed] screen={target_screen.name() if target_screen else 'unknown'}")

    win = OrbitEmbed(
        root          = args.root,
        entry         = args.entry,
        bridge        = OrbitBridge(),
        title         = args.title,
        width         = args.width if args.width != 1280 else None,  # None = smart size
        height        = args.height if args.height != 820 else None,
        frameless     = args.frameless,
        show_tabs     = not args.no_tabs,
        show_nav      = not args.no_nav,
        allowed_hosts = set(args.allow_hosts) if args.allow_hosts else None,
        data_root     = args.data_root,
        sandboxed     = not args.no_sandbox,
        theme_file       = args.theme,
        minimize_to_tray = args.minimize_to_tray,
        single_instance  = not args.no_single_instance,
        dev_mode         = args.dev_mode,
        address_bar_mode = args.address_bar_mode,
        allow_new_tabs   = args.allow_new_tabs,
    )
    # Center window on target screen
    if target_screen:
        geom = target_screen.availableGeometry()
        win.move(
            geom.left() + (geom.width()  - win.width())  // 2,
            geom.top()  + (geom.height() - win.height()) // 2,
        )
    if _kiosk and target_screen:
        # Move to correct screen before going fullscreen, otherwise Qt
        # will fullscreen on whichever screen the window currently occupies.
        geom = target_screen.geometry()
        win.move(geom.left(), geom.top())
        win.showFullScreen()
    else:
        win.show()

    _log(f"[orbit_embed] started  root={win._root}  entry={args.entry}")
    _log(f"[orbit_embed] bridge={'enabled' if _WEBCHANNEL_AVAILABLE else 'DISABLED (no QtWebChannel)'}  "
         f"allowed_hosts={args.allow_hosts}")

    # If launched with --url (e.g. from an OS protocol handler), fire the
    # protocolActivated event once the window and bridge are ready.
    if args.url:
        _url = args.url
        QTimer.singleShot(
            500,
            lambda: win.emit_event("protocolActivated", {"url": _url})
        )
        _log(f"[protocol] protocolActivated deferred: {_url}")

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
