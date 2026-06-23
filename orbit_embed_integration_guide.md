# Orbit Embed — Integration Guide

This document is a complete handoff for embedding `orbit_embed.py` into a Python application.
Read it fully before writing any code. Several behaviours are non-obvious and will cost you
time if you hit them cold.

---

## What orbit_embed is

A single-file PyQt6 + QtWebEngine desktop shell that serves your HTML app from a local
directory via a custom `app://` scheme. It is not a browser — it is a locked-down Chromium
window with a two-way JS↔Python bridge, permanent offline enforcement by default, and
zero disk profile.

The public surface area you care about as an embedder:

- `OrbitEmbed` — the `QMainWindow` subclass. Instantiate this.
- `OrbitBridge` — the bridge base class. Subclass this to add your own Python methods.
- `register_protocol_handler` / `unregister_protocol_handler` — OS-level URL scheme hooks.

---

## Dependencies

```
pip install PyQt6 PyQt6-WebEngine PyQt6-Qt6
```

`PyQt6-QtWebChannel` is required for the JS bridge. It is included in recent PyQt6 installs
but was a separate package on older versions — if `window.orbitInvoke` is silently undefined
in your app, this is why. Check with:

```python
from PyQt6.QtWebChannel import QWebChannel  # should not raise
```

`PyQt6.QtNetwork` is required for single-instance enforcement. Missing it only disables
that feature — everything else still works.

---

## Minimal embed (no custom bridge)

```python
import sys
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from orbit_embed import OrbitEmbed, OrbitBridge

app = QApplication(sys.argv)
win = OrbitEmbed(
    root=Path("/path/to/your/html/app"),
    entry="index.html",
    bridge=OrbitBridge(),
    title="My App",
)
win.show()
sys.exit(app.exec())
```

That's the entire integration for a pure HTML app that only needs file I/O and store APIs.
You do not have to subclass anything.

---

## Adding custom Python methods (the bridge)

Subclass `OrbitBridge` and override `handle_invoke`. Always call `super()` at the end so
built-in methods keep working.

```python
class MyBridge(OrbitBridge):
    def handle_invoke(self, method: str, args: list):
        if method == "greet":
            name = args[0] if args else "world"
            return {"message": f"Hello, {name}!"}

        if method == "processData":
            data = args[0]  # whatever the JS passed
            result = do_some_python_work(data)
            return {"result": result}

        return super().handle_invoke(method, args)
```

**Return values** must be JSON-serialisable. Anything you return becomes the `result` field
in the JS response. Raise an exception to send an error — it is caught and returned as
`{ok: false, error: "..."}`.

In your HTML:

```js
window.__orbitReady(function(orbit) {
    orbit.invoke("greet", ["Alice"], function(response) {
        console.log(response.message);  // "Hello, Alice!"
    });
});

// Or with the promise API:
const res = await window.orbitInvoke("greet", ["Alice"]);
console.log(res.result.message);
```

**Important:** `window.orbitInvoke` resolves to `response.result`, while `orbit.invoke`
gives you the full `{ok, result}` envelope. Pick one style and be consistent.

---

## The `app://` scheme — how URLs work

Your HTML app lives at `app://app/<path>`. The host portion (`app`) is ignored — everything
resolves relative to your `root` directory.

| You request | Resolves to |
|---|---|
| `app://app/index.html` | `<root>/index.html` |
| `app://app/assets/logo.png` | `<root>/assets/logo.png` |
| `app://app/` | `<root>/index.html` |

Use `app://` for all internal navigation. Do not use `file://` — it bypasses the scheme
handler and the CSP injection.

**Path traversal is blocked.** Any resolved path outside `root` returns 403. This is
enforced with an `os.sep`-guarded check, so sibling directories with similar names cannot
be accessed.

---

## The offline gate — the most important behaviour to understand

**All HTTP/HTTPS is hard-blocked by default.** Your app starts offline. This is not a
configuration — it is the core security contract.

Three ways traffic can pass:

1. **Permanent allowlist** — hosts passed at construction (`allowed_hosts`) or added at
   runtime (`win.allow_host("localhost:3000")`). These always pass.

2. **Temporary online window** — opened by JS calling `requestOnline` or Python calling
   `win.go_online_for()`. Closes automatically after `idleSecs` of no traffic.

3. **The `app://`, `file://`, `data:`, `blob:`, `about:`, `qrc:` schemes** — always pass.
   WebSockets are NOT in this list — they use `ws://`/`wss://` and are blocked unless
   online or allowlisted.

### If your app makes any network requests:

```js
// Before any fetch/XHR/WebSocket:
await window.orbitInvoke("requestOnline", [{ idleSecs: 15, reason: "API call" }]);

const res = await fetch("https://api.myservice.com/data");
// ... gate closes 15s after the last byte passes
```

If you have a local backend (e.g. Flask on port 5000, Vite on port 5173), allowlist it at
startup instead:

```python
win = OrbitEmbed(
    ...
    allowed_hosts={"localhost:5000", "localhost:5173"},
)
```

Allowlisted hosts work for both `fetch()`/XHR from JS **and** `window.open()` — the latter
forwards to the system browser rather than opening a tab inside OrbitEmbed.

### The watchdog and downloads

A 1-second watchdog timer runs whenever the gate is open. It measures idle time and
optionally enforces a hard ceiling (`maxSecs`). Active downloads suppress the idle
countdown — the gate will not close mid-transfer. When the gate closes, a `networkLocked`
event fires in JS:

```js
orbit.onEvent(function(name, data) {
    if (name === "networkLocked") {
        console.log("Network closed, reason:", data.reason);
        // "idle" | "timeout" | "manual"
    }
});
```

---

## File I/O and the sandbox

All bridge file methods (`readFile`, `writeFile`, `listDir`, etc.) are sandboxed to
`data_root`. The default is `~/.orbit_embed/<app_title>/data/`.

```python
win = OrbitEmbed(
    ...
    data_root="/my/app/data",   # override the default location
    sandboxed=True,             # True is the default — keep it unless you have a reason
)
```

With `sandboxed=True`, any JS path that resolves outside `data_root` raises a
`PermissionError` and returns `{ok: false, error: "Permission denied: ..."}`. The sandbox
check uses an `os.sep` guard — sibling directory prefix attacks are blocked.

Pass `sandboxed=False` (or `--no-sandbox` on CLI) only if your app genuinely needs to read
from arbitrary filesystem locations. Treat it like `sudo`.

The persistent store (`storeGet`/`storeSet`) lives in `data_root/.orbit_store.json` and
survives restarts. It is separate from `localStorage` (which is in-memory only, wiped on
exit due to the off-the-record profile).

---

## The JS bridge — timing and readiness

The bridge is injected via `QWebEngineScript` at `DocumentReady`, not `DocumentCreation`.
This means it may not be available the instant the page loads. Always gate your bridge
calls through `window.__orbitReady`:

```js
// WRONG — may fire before bridge is ready:
const result = await window.orbitInvoke("myMethod", []);

// RIGHT:
window.__orbitReady(async function(orbit) {
    const result = await window.orbitInvoke("myMethod", []);
});
```

`window.__orbitReady` queues callbacks and flushes them once the `QWebChannel` handshake
completes. It is safe to call multiple times and from anywhere in your code.

`window.orbitInvoke` has a **10-second timeout by default**. Long-running Python operations
will reject the promise with a timeout error. Pass `0` as the third argument to disable:

```js
const result = await window.orbitInvoke("longTask", [args], 0);  // no timeout
```

---

## Sending events from Python to JS

```python
# From OrbitEmbed:
win.emit_event("myEvent", {"count": 42, "status": "ok"})

# From inside your bridge subclass:
self.emit_event("myEvent", {"count": 42})

# Or hold a reference to the bridge:
bridge.emit_event("myEvent", {"count": 42})
```

In JS:

```js
orbit.onEvent(function(name, data) {
    if (name === "myEvent") {
        console.log(data.count);
    }
});
```

Events are broadcast to all open tabs. There is no per-tab targeting — if you need that,
encode a tab identifier in the event payload.

---

## CSP — what's injected and when

Every HTML response from `app://` gets a `Content-Security-Policy` meta tag injected into
`<head>` unless the document already has one. The default policy:

```
default-src app: data: blob: 'self';
script-src app: 'self';
style-src app: 'unsafe-inline' 'self';
img-src app: data: blob: 'self';
font-src app: data: 'self';
connect-src app: 'self';
frame-src 'none';
object-src 'none';
base-uri 'self';
```

Key points:
- `'unsafe-eval'` is **not** included. If your app uses `eval()`, `new Function()`, or a
  bundler that emits eval-based code, you must supply your own CSP with `'unsafe-eval'` in
  `script-src`.
- External CDN resources (`https://cdn.jsdelivr.net/...`) are blocked by default. Bundle
  your dependencies into `root` or open the online gate before the page loads.
- To supply your own policy, add a meta tag as the first child of `<head>` and the
  injector will leave it alone.

---

## Tabs

New tabs are **off by default**. Ctrl+T, the `+` button, and `window.open()` are all gated
by the `allow_new_tabs` flag.

```python
# Enable at construction:
win = OrbitEmbed(..., allow_new_tabs=True)

# Or let JS control it:
await window.orbitInvoke("setNewTabsAllowed", [true]);
```

`window.open()` targets are routed as follows:

- **`app://` URLs** — open a new tab inside OrbitEmbed (subject to `allow_new_tabs`).
- **`http`/`https` URLs whose host is in `allowed_hosts`** — forwarded to the system's
  default browser via `QDesktopServices.openUrl()`. No new tab is created inside OrbitEmbed.
- **All other URLs** — silently blocked; a brief error page is shown in a discarded tab.

---

## Single instance

By default, a second launch of your app will find the running instance via a local socket,
raise its window, and exit. If launched with `--url`, the URL is forwarded to the first
instance and fires a `protocolActivated` event there.

Disable with:

```python
win = OrbitEmbed(..., single_instance=False)
```

Requires `PyQt6.QtNetwork`. If that package is absent, single-instance is silently
disabled.

---

## Dev mode

```python
win = OrbitEmbed(..., dev_mode=True)
```

Enables:
- **F12** opens Chromium DevTools
- Standard Chromium context menu (instead of the bridge `contextMenu` event)
- **Hot reload** — any file change under `root` triggers a 300ms-debounced reload of the
  active tab. New subdirectories created at runtime are watched automatically.
- `⚙ DEV MODE` badge in the status bar

Never ship `dev_mode=True` to end users — it exposes DevTools and disables context menu
suppression.

---

## Theming

Pass a partial dict — only the keys you want to override are needed:

```python
win = OrbitEmbed(..., theme={"bg": "#1a1a2e", "accent": "#e94560"})
```

Or point at a `theme.json` file (auto-detected at `<root>/theme.json` if present):

```python
win = OrbitEmbed(..., theme_file="/path/to/theme.json")
```

Or change it at runtime from Python or JS:

```python
win.apply_theme({"accent": "#ff6b6b"})
```

```js
await window.orbitInvoke("setTheme", [{ accent: "#ff6b6b" }]);
```

The theme engine resolves missing keys from defaults and derives `hover`, `pressed`, and
`border2` automatically. You never need to supply all nine keys.

**Important:** The embedded HTML error pages (`_HOME_HTML`, `_CERT_ERROR_HTML`, the
"tab blocked" inline message) use hardcoded colours from the default theme. They will not
respond to theme changes. If you ship a heavily custom theme, be aware these pages will
look inconsistent.

---

## Window geometry

`OrbitEmbed` defaults to 85% of the primary screen's available geometry (excludes taskbar)
when `width` and `height` are not given. Pass explicit values to pin the size:

```python
win = OrbitEmbed(..., width=1400, height=900)
```

Minimum enforced size: 480×320. There is currently no geometry persistence — if you need
the window to remember its last size and position across restarts, you must save and
restore `win.geometry()` yourself using the store bridge or any other mechanism.

---

## QApplication must exist before OrbitEmbed

orbit_embed does not create `QApplication` for you when used as a library. You must create
it first. It must also be created before any Qt object — including before you import
`orbit_embed` if you're on a platform where the custom `app://` scheme registration
matters (it happens at import time, before `QApplication`).

```python
# Correct order:
from PyQt6.QtWidgets import QApplication
import sys

app = QApplication(sys.argv)
app.setStyle("Fusion")   # optional but recommended for cross-platform consistency

# Only now import and use OrbitEmbed:
from orbit_embed import OrbitEmbed, OrbitBridge
win = OrbitEmbed(...)
```

If your host app already has a `QApplication`, pass it — `QApplication.instance()` returns
the existing one. Do not create two.

---

## HiDPI

Qt 6 enables HiDPI scaling by default. On Windows, DWM per-monitor awareness is set
automatically before `QApplication` in CLI mode. In embedded mode, this is your
responsibility — set DPI policy before constructing `QApplication`:

```python
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

QApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
)

# Windows only:
if sys.platform == "win32":
    import ctypes
    ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)

app = QApplication(sys.argv)
```

orbit_embed handles this internally in `main()` (CLI mode) but does not repeat it for the
library case.

---

## Common problems and fixes

**`window.orbitInvoke` is undefined**
Bridge is disabled. `PyQt6.QtWebChannel` is not installed. Run:
```
pip install --upgrade PyQt6 PyQt6-WebEngine PyQt6-Qt6
```

**`window.__orbitReady` callback never fires**
The `QWebEngineScript` injection failed — usually because the profile's script list was
cleared after `BrowserTab` was created, or a second profile was constructed. Do not touch
`self._profile.scripts()` after the first tab is opened.

**`fetch()` fails with net::ERR_BLOCKED_BY_CLIENT**
The offline gate is closed. Call `requestOnline` first, or add the host to `allowed_hosts`.

**`fetch()` to `localhost` / `127.0.0.1` fails silently even though the host is allowlisted**
This was a bug in versions prior to orbit_embed-2 caused by `app://` being registered as a
`SecureScheme`, which made Chromium apply mixed-content blocking against `http://` targets.
It is fixed in the current version — upgrade to orbit_embed-2 and the calls will work
without any code changes on your side.

**CSP blocks my scripts**
Your bundler (Webpack, Vite, esbuild) is emitting `eval`-based source maps or hot-reload
code. Either disable eval in the bundler config, or add a custom CSP with `'unsafe-eval'`
in `script-src` as the first tag in `<head>`.

**Theme changes don't update the webview background**
`BrowserTab` sets `setBackgroundColor` from `_C('bg')` at construction time. Changing the
theme later does not repaint the Chromium background. Set `background-color` on `<html>`
in your CSS instead — that takes precedence over the Qt-level colour.

**Second instance raises the first but the URL is wrong**
Ensure `--url` is passed correctly by the OS protocol handler. The socket payload format
is `url:<your url>` — if the OS is passing the URL without the scheme or with extra
quoting, the forwarding will still work but `protocolActivated.url` may be malformed.
Validate the URL in your `protocolActivated` handler before using it.

**File drops don't fire the `fileDrop` event / `dataTransfer` is empty in JS**
Chromium was consuming the OS drag-and-drop event before OrbitEmbed's `dropEvent` could see
it. This is fixed in orbit_embed-2 — the webview's own drop handling is disabled so drops
propagate correctly to the Python layer. If you are on an older version, upgrade; no code
changes are required on your side.

**Hot reload fires on every keystroke in the editor**
The 300ms debounce is intentional but may feel short for editors that save frequently
(e.g. autosave on every character). Increase the interval by patching
`self._hot_reload_timer.setInterval(600)` after construction if needed.

---

## What the bridge does NOT do

- **No per-tab bridge instances.** All tabs share one `OrbitBridge` object. If you need
  tab-specific state, track it in the bridge using the tab index or a JS-provided session ID.
- **No async Python methods.** `handle_invoke` is synchronous and runs on the Qt main
  thread. Blocking the main thread blocks the UI. Offload heavy work to a `threading.Thread`
  and emit an event when done:

```python
import threading

class MyBridge(OrbitBridge):
    def handle_invoke(self, method, args):
        if method == "runHeavyTask":
            def _worker():
                result = do_heavy_work()
                self.emit_event("taskDone", {"result": result})
            threading.Thread(target=_worker, daemon=True).start()
            return {"started": True}
        return super().handle_invoke(method, args)
```

- **No `printToPdf` callback.** The `printToPdf` bridge method is fire-and-forget. The
  file will appear at the resolved path, but there is no JS notification when it is ready.
  If you need one, emit an event from a file watcher or poll `fileExists`.

---

## Quick reference — constructor parameters

```python
OrbitEmbed(
    root          = Path("/path/to/html"),  # required
    entry         = "index.html",           # entry point relative to root
    bridge        = MyBridge(),             # None disables bridge
    title         = "My App",
    width         = None,                   # None = 85% of screen
    height        = None,
    frameless     = False,                  # remove OS window chrome
    show_tabs     = True,
    show_nav      = True,
    allow_new_tabs = False,                 # Ctrl+T / + button
    allowed_hosts = {"localhost:3000"},     # permanent online allowlist
    data_root     = None,                   # defaults to ~/.orbit_embed/<title>/data
    sandboxed     = True,                   # enforce data_root boundary
    theme_file    = None,                   # path to theme.json
    theme         = {"accent": "#ff0000"},  # inline theme override
    minimize_to_tray = False,
    single_instance  = True,
    dev_mode         = False,
    address_bar_mode = "url",              # "url" | "title" | "hidden"
)
```

---

## File layout expected by orbit_embed

```
my_app/
├── orbit_embed.py      ← drop here; import from here
├── index.html          ← entry point (--entry)
├── theme.json          ← optional; auto-detected
└── assets/
    ├── app.js
    └── style.css
```

Everything under `my_app/` is served as `app://app/<relative path>`. The `theme.json`
and entry HTML do not need to be at the root — configure `entry` and `theme_file` to
point wherever you need.
