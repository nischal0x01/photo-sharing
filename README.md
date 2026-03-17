# Secure Client Gallery

A desktop app for safely sharing a folder over the network — locally or via a public ngrok link.  
Built with **PySide6** (Qt6 desktop GUI) + **pyngrok** (managed ngrok tunnel).  
Works on **macOS** and **Windows**. No Tkinter, no web framework, pure pip dependencies.

---

## Table of Contents

1. [What This App Does](#what-this-app-does)  
2. [Architecture Overview](#architecture-overview)  
3. [Security Model](#security-model)  
4. [Code Walkthrough](#code-walkthrough)  
5. [Prerequisites](#prerequisites)  
6. [macOS Setup](#macos-setup)  
7. [Windows Setup](#windows-setup)  
8. [ngrok Setup](#ngrok-setup)  
9. [Daily Workflow](#daily-workflow)  
10. [Troubleshooting](#troubleshooting)  

---

## What This App Does

You open the app, pick a folder, and click **Start Sharing**.  
The app spins up an HTTP server on your machine and exposes it via an ngrok HTTPS tunnel.  
Anyone who has the link can browse the folder and download files through a clean web interface — they cannot upload, delete, or access anything outside the shared folder.

**Key capabilities:**
- Share any folder (photos, videos, ZIPs, PDFs) without cloud uploads.
- Optional ngrok link so clients outside your LAN can download.
- One-click download: a **Download entire folder** button creates a ZIP of the shared folder.
- 50 GB+ downloads work (16 MB streaming chunks, HTTP Range support for resume).
- Hidden files (`.DS_Store`, `.git`, etc.) are filtered by default.
- Access log visible in real time inside the desktop app.
- Settings are saved per-machine; re-launch restores your last session.

---

## Architecture Overview

```
┌────────────────────────────────────────────────────────┐
│                    PySide6 Window                      │
│                                                        │
│  Settings form → AppConfig dataclass                  │
│  Start button  → ShareController.start(cfg)           │
│  QTimer(400ms) → drain ShareController log buffer     │
│  Stop button   → ShareController.stop()               │
└─────────────────────┬──────────────────────────────────┘
                      │
          ┌───────────▼─────────────┐
          │    ShareController      │
          │                         │
          │  ThreadingHTTPServer    │◄── runs on daemon thread
          │  pyngrok tunnel         │◄── optional HTTPS tunnel
          │  log buffer + lock      │◄── thread-safe, GUI drains it
          └───────────┬─────────────┘
                      │  build_handler(root, title, …)
          ┌───────────▼─────────────┐
          │    SecureHandler        │
          │  (per-request class)    │
          │                         │
          │  do_GET → traversal     │
          │         → containment   │
          │         → hidden filter │
          │         → serve dir/file│
          │  do_POST/PUT/DELETE/    │
          │    PATCH → 405          │
          └─────────────────────────┘
```

**Data flow for a single download:**

```
Browser (client)
  └─ GET /Photos/IMG_001.jpg
       │
  ngrok HTTPS tunnel  (if public link enabled)
       │
  ThreadingHTTPServer  (0.0.0.0:8080)
       │
  SecureHandler.do_GET()
       ├─ decode + sanitise URL  403 if ".."
       ├─ Path containment check 403 if escape attempt
       ├─ hidden file check      403 if dot-file
       └─ _serve_file()          200/206 streaming response
```

---

## Security Model

### 1. Path Containment (anti-traversal)

```python
def is_within_root(root: Path, candidate: Path) -> bool:
    candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
```

`Path.resolve()` follows every symlink and collapses `..` before comparison.  
`relative_to()` throws `ValueError` if candidate is not a descendant of root.  
This is checked **after** URL decoding and path joining, so `%2e%2e%2f` tricks don't work.

### 2. Read-only Enforcement

Only `do_GET` does real work.  `do_POST`, `do_PUT`, `do_DELETE`, `do_PATCH` all return **405 Read-only** unconditionally. There is no way to write to disk through this server.

### 3. Hidden File Filtering

```python
def is_hidden(relative_path: Path) -> bool:
    return any(part.startswith(".") for part in relative_path.parts)
```
Any component starting with `.` is blocked at 403.  This hides `.DS_Store`, `.git/`, `.env`, etc. at every depth level.

### 4. Security Response Headers

Every response sends:
```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Content-Security-Policy: default-src 'self'; style-src 'unsafe-inline'
Cache-Control: no-store
```

### 5. Large-file Streaming

Files are never loaded into RAM. `_serve_file()` opens the file in binary read mode, seeks to the requested range, and writes `CHUNK_SIZE = 16 MB` at a time:
```python
with file_path.open("rb") as src:
    src.seek(start)
    while remaining > 0:
        chunk = src.read(min(CHUNK_SIZE, remaining))
        self.wfile.write(chunk)
        remaining -= len(chunk)
```
`Accept-Ranges: bytes` is advertised, so download managers and browsers can resume interrupted transfers.

---

## Code Walkthrough

### `AppConfig` (dataclass)

Holds every user-configured value:

| Field | Type | Meaning |
|---|---|---|
| `share_folder` | str | Absolute path being shared |
| `title` | str | Gallery title shown in the browser |
| `port` | int | TCP port (default 8080) |
| `show_hidden` | bool | Whether to serve dot-files |
| `enable_public_link` | bool | Whether to start an ngrok tunnel |

ngrok auth is loaded from environment (see [ngrok Setup](#ngrok-setup)).

Saved to / loaded from JSON in the home directory between sessions.

---

### `load_config()` / `save_config()`

**Mac:** `~/.secure_gallery_mac.json`  
**Windows:** `%USERPROFILE%\AppData\Local\.secure_gallery_windows.json`

Uses `json.loads` / `json.dumps` with `dataclasses.asdict()`.  Gracefully falls back to a fresh `AppConfig()` if the file is missing or corrupt.

---

### `build_handler(...)` — factory function

```python
def build_handler(share_root, title, show_hidden, event_logger):
    class SecureHandler(http.server.BaseHTTPRequestHandler):
        ...
    return SecureHandler
```

This pattern **closes over** the configuration values so the handler class doesn't need instance state.  `http.server` instantiates a new `SecureHandler` per connection, but all instances share the same `root`, `title`, etc. via closure.

---

### `SecureHandler`

**`do_GET`** — the only real method:
1. URL-decode the path, split on `/`, reject any `..` or `.` component.
2. Join decoded parts onto `share_root`.
3. `is_within_root()` — resolve and compare → 403 if escape.
4. Existence check → 404 if missing.
5. Hidden file check → 403 if any component starts with `.`.
6. Dispatch to `_serve_directory()` or `_serve_file()`.

**`_serve_directory()`** — generates an HTML page:
- Breadcrumb navigation links.
- Entries sorted folders-first, then files, both case-insensitively.
- Each rendered as a card with emoji icon, name, and size (for files) or "Folder" (for dirs).
- A "Download" badge on every file card.
- Inline CSS (no external resources needed — works offline).

**`_serve_file()`**:
- Reads `Range:` header if present (calls `parse_range_header()`).
- Responds 416 for invalid ranges.
- Sets `Content-Disposition: attachment` so the browser downloads rather than previews.
- Streams in 16 MB chunks.

---

### `ShareController`

Manages the server lifecycle and the pyngrok tunnel.

**`start(cfg)`:**
1. Calls `stop()` first (idempotent restart).
2. Resolves and validates the folder path.
3. Builds the handler class via `build_handler()`.
4. Creates `ThreadingHTTPServer("0.0.0.0", port)`.  Binding to `0.0.0.0` makes it reachable from other devices on the LAN, not just localhost.
5. Starts `serve_forever()` on a **daemon thread** — this thread dies automatically when the Python process exits.
6. Detects local IP with a UDP connect trick (no packet sent; just reads the kernel's routing decision).
7. If `enable_public_link`: calls `ngrok.set_auth_token()` (if `NGROK_AUTH_TOKEN` is set) then `ngrok.connect(addr=port, bind_tls=True)`.  pyngrok launches the ngrok binary, starts a HTTPS tunnel, and returns a tunnel object with `.public_url`.
8. Returns `(local_url, public_url)`.

**`stop()`:**
1. `server.shutdown()` — signals `serve_forever()` to stop.
2. `server.server_close()` — releases the port.
3. Joins the server thread.
4. `ngrok.disconnect(tunnel.public_url)` — tears down the tunnel.

**Log buffer:**  
`_log()` appends timestamped strings to `self._logs` under a `threading.Lock`. Capped at 500 entries.  
`pop_logs()` drains and clears under the same lock — called by the GUI timer every 400 ms.

---

### `Window` (QWidget)

The main GUI class.  Built entirely with PySide6 Qt widgets:

| Widget | Purpose |
|---|---|
| `QGroupBox` + `QFormLayout` | Settings section (labeled rows) |
| `QLineEdit` | Text inputs (folder, title, link display) |
| `QSpinBox` | Port number (range 1024–65535) |
| `QCheckBox` | Boolean options |
| `QPushButton` | Browse, Start, Stop, Open, Copy |
| `QFileDialog` | Folder picker |
| `QTextEdit` | Dark-background access log panel |
| `QTimer` | 400ms tick to drain the log buffer |

**`_start()` slot:**
1. Reads the form into an `AppConfig`.
2. Saves config to disk.
3. Calls `controller.start(cfg)` — may raise on bad config.
4. Populates the link fields.
5. Automatically opens the most useful link in the browser.

**`closeEvent()`:**  
Called when the window X button is pressed. Stops the controller, saves config, then accepts the event (allowing the window to close).

---

## Prerequisites

- **Python 3.10 or newer** (uses `tuple[int, int] | None` type hints)
- **pip** (comes with Python)
- **ngrok account** (free tier works; only needed for public link feature)

No Tkinter. No system GUI libraries. No external native dependencies beyond Python itself.

---

## macOS Setup

```bash
# 1. Clone / copy the project folder
cd ~/Downloads/file-sharing

# 2. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the app
python3 main.py
```

> **Tip — convenience launcher:**  Create a shell script `start.sh`:
> ```bash
> #!/bin/bash
> cd "$(dirname "$0")"
> source .venv/bin/activate
> python3 main.py
> ```
> Then `chmod +x start.sh` and double-click it from Finder (you may need to allow it under System Settings → Privacy & Security).

---

## Windows Setup

```powershell
# 1. Open PowerShell in the project folder (Shift+right-click → Open PowerShell)

# 2. Create virtual environment
python -m venv .venv
.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run
python main.py
```

> **Tip — convenience launcher:**  Create `start_gallery.bat` in the project root:
> ```bat
> @echo off
> cd /d "%~dp0"
> call .venv\Scripts\activate
> python main.py
> pause
> ```
> Double-click it to launch. Remove `pause` if you don't want the terminal window.

> **Note on Windows Defender Firewall:**  The first time you start sharing, Windows may ask whether to allow Python on private/public networks. Allow it for your chosen network type.

---

## ngrok Setup

ngrok is optional. Skip it if you only need to share on your local network (same Wi-Fi).

1. Create a free account at [https://ngrok.com](https://ngrok.com).
2. Go to your [ngrok dashboard](https://dashboard.ngrok.com/get-started/your-authtoken) and copy your authtoken.
3. Create a local `.env` file (see `.env.example`) and set:
    ```
    NGROK_AUTH_TOKEN=your_token_here
    ```
    (Alternatively, export `NGROK_AUTH_TOKEN` in your shell environment.)
4. Check **Enable public ngrok link**.
5. Click **Start Sharing** — the public HTTPS link will appear in the Public ngrok field.

> **Free tier limits:**  One concurrent tunnel, 1 GB/month bandwidth. Sufficient for occasional client deliveries.  
> For heavy use, a paid ngrok plan removes bandwidth caps.

---

## Daily Workflow

```
1. Open terminal  →  source .venv/bin/activate  →  python3 main.py
2. Click Browse   →  select your delivery folder
3. (Optional) Set `NGROK_AUTH_TOKEN` in `.env` and check "Enable public ngrok link"
4. Click ▶ Start Sharing
5. Copy the Link  →  send to client (Local link for LAN, Public for remote)
6. Watch the Access Log  →  see when files are downloaded
7. Click ⏹ Stop  →  tunnel and server tear down cleanly
```

Your client opens the link in any browser, browses the gallery, and clicks files to download.  
No app install required on the client side.

---

## Troubleshooting

### "Port already in use"
Another process is using port 8080.  Change the port number in the app to 8081 or any free port.  
To find what's using a port:
- **Mac:** `lsof -i :8080`
- **Windows:** `netstat -ano | findstr :8080`

### "ngrok not connecting"
- Verify `NGROK_AUTH_TOKEN` is set correctly (dashboard → Your Authtoken).
- Check your internet connection.
- Free ngrok allows only one concurrent tunnel — close any other ngrok sessions.
- If `pyngrok` can't find the ngrok binary, it will download it automatically on first run. Allow it.

### "Folder not found" error after start
The path in the folder field doesn't exist or was moved.  Click Browse again to reselect.

### Client says "403 Forbidden"
- You're likely trying to access a hidden file/folder (dotfiles are blocked by default), or the URL contains invalid path components like `..`.
- If you intentionally want to share hidden files, enable "Show hidden files".

### Downloaded file is corrupt or incomplete
- The download was interrupted. The server supports HTTP Range requests — use a download manager (e.g. `wget`, `curl -C -`) to resume.
- Check that you have enough disk space on the client side.

### App won't start — "No module named PySide6"
The virtual environment is not activated, or `pip install -r requirements.txt` was not run inside it.

```bash
source .venv/bin/activate   # Mac
pip install -r requirements.txt
```

### High CPU when idle
The `QTimer` polls logs every 400 ms — negligible load. The HTTP server threads are blocked waiting for connections. Total idle CPU should be < 0.1%.
