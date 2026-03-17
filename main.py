#!/usr/bin/env python3
"""
Secure Client Gallery — macOS build
Desktop GUI app (PySide6) that serves a selected folder as a read-only
HTTP gallery with optional ngrok tunnel.

Run:  python3 main.py
"""
from __future__ import annotations

import os
import html
import http.server
import json
import mimetypes
import shutil
import socket
import tempfile
import threading
import time
import urllib.parse
import zipfile
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path

from pyngrok import ngrok
try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QFileDialog,
)

# ─────────────────────────── constants ────────────────────────────────────────

APP_NAME = "Secure Gallery  ·  macOS"
CONFIG_PATH = Path.home() / ".secure_gallery_mac.json"
DEFAULT_TITLE = "Client Gallery"
DEFAULT_PORT = 8080
CHUNK_SIZE = 16 * 1024 * 1024  # 16 MB per read — handles 50 GB+ without RAM issues

if load_dotenv is not None:
    load_dotenv()

# ─────────────────────────── config ───────────────────────────────────────────


@dataclass
class AppConfig:
    share_folder: str = ""
    title: str = DEFAULT_TITLE
    port: int = DEFAULT_PORT
    show_hidden: bool = False
    enable_public_link: bool = True


def load_config() -> AppConfig:
    try:
        if CONFIG_PATH.exists():
            d = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return AppConfig(
                share_folder=str(d.get("share_folder", "")),
                title=str(d.get("title", DEFAULT_TITLE)),
                port=int(d.get("port", DEFAULT_PORT)),
                show_hidden=bool(d.get("show_hidden", False)),
                enable_public_link=bool(d.get("enable_public_link", True)),
            )
    except Exception:
        pass
    return AppConfig()


def save_config(cfg: AppConfig) -> None:
    CONFIG_PATH.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")


# ─────────────────────────── utils ────────────────────────────────────────────


def get_local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def format_size(size: int) -> str:
    val = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} TB"


def file_icon(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".cr2", ".nef", ".arw", ".raw", ".gif"}:
        return "🖼️"
    if ext in {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".mts"}:
        return "🎬"
    if ext in {".zip", ".rar", ".7z"}:
        return "🗜️"
    if ext == ".pdf":
        return "📄"
    return "📎"


def is_hidden(relative_path: Path) -> bool:
    """Return True when any path component starts with a dot."""
    return any(part.startswith(".") for part in relative_path.parts)


def is_within_root(root: Path, candidate: Path) -> bool:
    """Return True only when candidate resolves to inside root."""
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except Exception:
        return False


def parse_range_header(header: str, size: int) -> tuple[int, int] | None:
    """Parse a Range: bytes=X-Y header.  Returns (start, end) or None."""
    if not header or not header.startswith("bytes="):
        return None
    spec = header.split("=", 1)[1].strip()
    if "," in spec or "-" not in spec:
        return None
    s, e = spec.split("-", 1)
    if not s and not e:
        return None
    try:
        if s:
            start = int(s)
            end = int(e) if e else size - 1
        else:
            suffix = int(e)
            start = max(size - suffix, 0) if suffix > 0 else 0
            end = size - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= size:
        return None
    return start, min(end, size - 1)


def build_url(parts: list[str]) -> str:
    encoded = "/".join(urllib.parse.quote(p, safe="") for p in parts)
    base = f"/{encoded}" if encoded else "/"
    return base


# ─────────────────────────── HTTP handler factory ─────────────────────────────

CSS = """
body{font-family:Inter,system-ui,sans-serif;background:#f6f7fb;margin:0;color:#171717}
.wrap{max-width:1100px;margin:0 auto;padding:24px 16px}
.head{background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:20px 22px;margin-bottom:18px}
.head-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.head-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:8px;border:1px solid #e5e7eb;background:#fff;color:#111827;
    padding:8px 12px;border-radius:10px;text-decoration:none;font-size:.86rem;font-weight:600}
.btn:hover{border-color:#93c5fd;box-shadow:0 4px 16px rgba(59,130,246,.10)}
.btn-primary{background:#1d4ed8;color:#fff;border-color:#1d4ed8}
.btn-primary:hover{background:#1e40af;border-color:#1e40af}
h1{margin:0 0 6px;font-size:1.45rem;font-weight:600}
.crumb{font-size:.88rem;color:#6b7280}
.crumb a{color:#1d4ed8;text-decoration:none}
.crumb a:hover{text-decoration:underline}
.meta{font-size:.85rem;color:#6b7280;margin-top:6px}
.section-label{font-size:.72rem;text-transform:uppercase;letter-spacing:.1em;color:#9ca3af;margin:22px 0 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:0;
      text-decoration:none;color:inherit;display:block;overflow:hidden;
      transition:border-color .15s,box-shadow .15s}
.card:hover{border-color:#93c5fd;box-shadow:0 4px 16px rgba(59,130,246,.12)}
.thumb{height:110px;display:flex;align-items:center;justify-content:center;
       font-size:2.2rem;background:#f8fafc;position:relative}
.card-body{padding:10px 12px 12px}
.card-name{font-size:.88rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card-meta{font-size:.78rem;color:#9ca3af;margin-top:3px}
.dl-badge{position:absolute;bottom:6px;right:8px;background:rgba(24,24,27,.7);
          color:#fff;font-size:.65rem;padding:3px 7px;border-radius:999px;letter-spacing:.05em}
.empty{background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:60px 24px;text-align:center;color:#9ca3af}
"""


def build_handler(
    share_root: Path,
    title: str,
    show_hidden: bool,
    event_logger,
):
    class SecureHandler(http.server.BaseHTTPRequestHandler):
        root = share_root
        gallery_title = title

        def log_message(self, fmt, *args):  # silence default stderr output
            return

        # ── routing ──────────────────────────────────────────────────────────

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            _query = urllib.parse.parse_qs(parsed.query)

            # 1. Decode and sanitise path
            raw_parts = [p for p in urllib.parse.unquote(parsed.path).split("/") if p]
            if any(p in ("..", ".") for p in raw_parts):
                self._respond_text(403, b"403 Forbidden")
                event_logger(f"DENIED traversal: {self.path}")
                return

            # Special route: download folder as a ZIP
            # - /__download__            -> zip the shared root
            # - /__download__/A/B        -> zip that subfolder (must be inside root)
            if raw_parts and raw_parts[0] == "__download__":
                sub_parts = raw_parts[1:]
                target = self.root.joinpath(*sub_parts) if sub_parts else self.root
                if not is_within_root(self.root, target):
                    self._respond_text(403, b"403 Forbidden")
                    event_logger(f"DENIED zip escape: {self.path}")
                    return
                if not target.exists() or not target.is_dir():
                    self._respond_text(404, b"404 Not Found")
                    return
                if not show_hidden and sub_parts and is_hidden(Path(*sub_parts)):
                    self._respond_text(403, b"403 Forbidden")
                    event_logger(f"DENIED zip hidden: {self.path}")
                    return

                self._serve_folder_zip(target)
                event_logger(f"ZIP {self.path}")
                return

            # 2. Build absolute candidate and verify it lives inside root
            candidate = self.root.joinpath(*raw_parts)
            if not is_within_root(self.root, candidate):
                self._respond_text(403, b"403 Forbidden")
                event_logger(f"DENIED escape: {self.path}")
                return

            # 3. Existence check
            if not candidate.exists():
                self._respond_text(404, b"404 Not Found")
                return

            # 4. Hidden file check
            rel = candidate.relative_to(self.root) if raw_parts else Path(".")
            if not show_hidden and raw_parts and is_hidden(rel):
                self._respond_text(403, b"403 Forbidden")
                event_logger(f"DENIED hidden: {self.path}")
                return

            # 5. Serve
            if candidate.is_dir():
                self._serve_directory(candidate, raw_parts)
                event_logger(f"DIR {self.path}")
            elif candidate.is_file():
                self._serve_file(candidate)
                event_logger(f"FILE {self.path}")
            else:
                self._respond_text(404, b"404 Not Found")

        # Block all write methods
        def do_POST(self) -> None: self._respond_text(405, b"405 Read-only")
        def do_PUT(self) -> None: self._respond_text(405, b"405 Read-only")
        def do_DELETE(self) -> None: self._respond_text(405, b"405 Read-only")
        def do_PATCH(self) -> None: self._respond_text(405, b"405 Read-only")

        # ── helpers ──────────────────────────────────────────────────────────

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'unsafe-inline'")
            self.send_header("Cache-Control", "no-store")

        def _respond_text(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _serve_directory(self, folder: Path, path_parts: list[str]) -> None:
            download_current = build_url(["__download__"] + path_parts)
            download_root = build_url(["__download__"])

            # breadcrumb
            crumb_parts = ['<a href="{}">Home</a>'.format(build_url([]))]
            for i, part in enumerate(path_parts):
                crumb_parts.append(
                    '<a href="{}">{}</a>'.format(
                        build_url(path_parts[:i + 1]), html.escape(part)
                    )
                )
            breadcrumb = " / ".join(crumb_parts)

            # entries — sorted: folders first, then files, both alphabetical
            try:
                entries = sorted(folder.iterdir(),
                                 key=lambda p: (not p.is_dir(), p.name.lower()))
            except PermissionError:
                self._respond_text(403, b"403 Permission denied")
                return

            folders_html, files_html = [], []
            for entry in entries:
                rel = entry.relative_to(self.root)
                if not show_hidden and is_hidden(rel):
                    continue
                rel_parts = list(rel.parts)
                href = build_url(rel_parts)

                if entry.is_dir():
                    folders_html.append(
                        f'<a class="card" href="{href}">'
                        f'<div class="thumb">📁</div>'
                        f'<div class="card-body">'
                        f'<div class="card-name">{html.escape(entry.name)}</div>'
                        f'<div class="card-meta">Folder</div>'
                        f'</div></a>'
                    )
                else:
                    sz = format_size(entry.stat().st_size)
                    icon = file_icon(entry.name)
                    folders_html  # just referencing to avoid lint warning; appended below
                    files_html.append(
                        f'<a class="card" href="{href}">'
                        f'<div class="thumb">{icon}'
                        f'<span class="dl-badge">Download</span></div>'
                        f'<div class="card-body">'
                        f'<div class="card-name">{html.escape(entry.name)}</div>'
                        f'<div class="card-meta">{sz}</div>'
                        f'</div></a>'
                    )

            f_count = len(folders_html)
            fi_count = len(files_html)
            stats = f"{f_count} folder{'s' if f_count != 1 else ''}  ·  {fi_count} file{'s' if fi_count != 1 else ''}"

            body_html = ""
            if folders_html:
                body_html += '<div class="section-label">Folders</div>'
                body_html += '<div class="grid">' + "".join(folders_html) + "</div>"
            if files_html:
                body_html += '<div class="section-label">Files</div>'
                body_html += '<div class="grid">' + "".join(files_html) + "</div>"
            if not folders_html and not files_html:
                body_html = '<div class="empty">This folder is empty</div>'

            if not path_parts:
                actions_html = f'<a class="btn btn-primary" href="{download_root}">Download entire folder</a>'
            else:
                actions_html = (
                    f'<a class="btn btn-primary" href="{download_current}">Download this folder</a>'
                    f'<a class="btn" href="{download_root}">Download entire folder</a>'
                )

            page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(self.gallery_title)}</title>
<style>{CSS}</style>
</head><body><div class="wrap">
<div class="head">
    <div class="head-top">
        <div>
            <h1>{html.escape(self.gallery_title)}</h1>
            <div class="crumb">{breadcrumb}</div>
            <div class="meta">{stats}  ·  Read-only delivery  ·  Tap a file to download</div>
        </div>
        <div class="head-actions">
            {actions_html}
        </div>
    </div>
</div>
{body_html}
</div></body></html>"""

            payload = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._security_headers()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _serve_file(self, file_path: Path) -> None:
            size = file_path.stat().st_size
            mime, _ = mimetypes.guess_type(file_path.name)
            mime = mime or "application/octet-stream"

            range_hdr = self.headers.get("Range")
            byte_range = parse_range_header(range_hdr, size) if range_hdr else None
            if range_hdr and byte_range is None:
                self.send_response(416)
                self._security_headers()
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return

            start = 0
            end = size - 1
            status = 200
            if byte_range:
                start, end = byte_range
                status = 206

            length = end - start + 1
            safe_name = file_path.name.replace('"', "")

            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if byte_range:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
            self._security_headers()
            self.end_headers()

            with file_path.open("rb") as src:
                src.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = src.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def _serve_folder_zip(self, folder: Path) -> None:
            base_name = folder.name or (self.root.name or "shared-folder")
            download_name = f"{base_name}.zip".replace('"', "")

            tmp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(prefix="secure-gallery-", suffix=".zip", delete=False) as tmp:
                    tmp_path = Path(tmp.name)

                with zipfile.ZipFile(
                    tmp_path,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                ) as zf:
                    for p in folder.rglob("*"):
                        try:
                            rel = p.relative_to(folder)
                        except Exception:
                            continue

                        if not show_hidden and is_hidden(rel):
                            continue
                        if p.is_file():
                            zf.write(p, arcname=str(rel))

                size = tmp_path.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
                self._security_headers()
                self.end_headers()

                with tmp_path.open("rb") as src:
                    while True:
                        chunk = src.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except Exception:
                self._respond_text(500, b"500 Failed to build ZIP")
            finally:
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except Exception:
                        pass

    return SecureHandler


# ─────────────────────────── server controller ────────────────────────────────


class ThreadingServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True


class ShareController:
    def __init__(self) -> None:
        self.server: ThreadingServer | None = None
        self.server_thread: threading.Thread | None = None
        self._tunnel = None
        self.local_url = ""
        self.public_url = ""
        self._logs: list[str] = []
        self._lock = threading.Lock()

    # ── log buffer (thread-safe, drained by GUI timer) ────────────────────────

    def _log(self, msg: str) -> None:
        with self._lock:
            self._logs.append(f"[{time.strftime('%H:%M:%S')}]  {msg}")
            self._logs = self._logs[-500:]

    def pop_logs(self) -> list[str]:
        with self._lock:
            out = self._logs[:]
            self._logs.clear()
            return out

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, cfg: AppConfig) -> tuple[str, str]:
        self.stop()

        root = Path(cfg.share_folder).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError("Choose a valid folder to share.")

        handler_cls = build_handler(
            share_root=root,
            title=cfg.title.strip() or DEFAULT_TITLE,
            show_hidden=cfg.show_hidden,
            event_logger=self._log,
        )

        self.server = ThreadingServer(("0.0.0.0", cfg.port), handler_cls)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

        ip = get_local_ip()
        self.local_url = f"http://{ip}:{cfg.port}"
        self.public_url = ""

        if cfg.enable_public_link:
            ngrok_token = (os.getenv("NGROK_AUTH_TOKEN") or os.getenv("NGROK_AUTHTOKEN") or "").strip()
            if ngrok_token:
                ngrok.set_auth_token(ngrok_token)
            self._tunnel = ngrok.connect(addr=str(cfg.port), bind_tls=True)
            self.public_url = self._tunnel.public_url

        self._log("✅  Server started")
        return self.local_url, self.public_url

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(timeout=2)
        self.server_thread = None

        if self._tunnel is not None:
            try:
                ngrok.disconnect(self._tunnel.public_url)
            except Exception:
                pass
            self._tunnel = None

        self.local_url = ""
        self.public_url = ""
        self._log("⏹  Server stopped")


# ─────────────────────────── GUI ──────────────────────────────────────────────


class Window(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(820, 680)
        self.resize(920, 720)
        self.cfg = load_config()
        self.controller = ShareController()
        self._build_ui()
        self._load_values()

        # Poll log buffer every 400 ms
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(400)
        self._log_timer.timeout.connect(self._drain_logs)
        self._log_timer.start()

    # ── build ─────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setSpacing(12)
        root_layout.setContentsMargins(18, 18, 18, 18)

        # ── title
        h_title = QHBoxLayout()
        app_title = QLabel("Secure Gallery")
        f = QFont()
        f.setPointSize(22)
        f.setWeight(QFont.Weight.Bold)
        app_title.setFont(f)
        subtitle = QLabel("macOS  ·  Read-only share  ·  PySide6")
        subtitle.setStyleSheet("color:#6b7280; margin-left:12px;")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignBottom)
        h_title.addWidget(app_title)
        h_title.addWidget(subtitle)
        h_title.addStretch(1)
        root_layout.addLayout(h_title)

        # ── settings group
        settings_box = QGroupBox("Sharing Settings")
        settings_box.setStyleSheet("QGroupBox{font-weight:600;}")
        form = QFormLayout(settings_box)
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(14)

        # Folder row
        self.folder_input = QLineEdit()
        self.folder_input.setPlaceholderText("/path/to/your/delivery/folder")
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._choose_folder)
        folder_row = QHBoxLayout()
        folder_row.setSpacing(6)
        folder_row.addWidget(self.folder_input)
        folder_row.addWidget(browse_btn)
        folder_widget = QWidget()
        folder_widget.setLayout(folder_row)

        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("Client Gallery")
        self.port_input = QSpinBox()
        self.port_input.setRange(1024, 65535)
        self.port_input.setFixedWidth(90)

        self.show_hidden_cb = QCheckBox()
        self.show_hidden_cb.setChecked(False)

        self.public_link_cb = QCheckBox()

        form.addRow("Folder to share", folder_widget)
        form.addRow("Gallery title", self.title_input)
        form.addRow("Port", self.port_input)
        form.addRow("Show hidden files", self.show_hidden_cb)
        form.addRow("Enable public ngrok link", self.public_link_cb)
        root_layout.addWidget(settings_box)

        # ── links group
        links_box = QGroupBox("Links")
        links_box.setStyleSheet("QGroupBox{font-weight:600;}")
        lg = QHBoxLayout(links_box)

        def link_row(label: str, field: QLineEdit) -> QHBoxLayout:
            field.setReadOnly(True)
            field.setStyleSheet("background:#f3f4f6; color:#1d4ed8;")
            open_btn = QPushButton("Open")
            copy_btn = QPushButton("Copy")
            open_btn.setFixedWidth(56)
            copy_btn.setFixedWidth(56)
            open_btn.clicked.connect(lambda: self._open_link(field.text()))
            copy_btn.clicked.connect(lambda: self._copy_link(field.text()))
            h = QHBoxLayout()
            h.addWidget(QLabel(label))
            h.addWidget(field, 1)
            h.addWidget(open_btn)
            h.addWidget(copy_btn)
            return h

        self.local_link_field = QLineEdit()
        self.public_link_field = QLineEdit()

        links_v = QVBoxLayout()
        links_v.addLayout(link_row("Local network:", self.local_link_field))
        links_v.addLayout(link_row("Public ngrok: ", self.public_link_field))
        lg.addLayout(links_v)
        root_layout.addWidget(links_box)

        # ── action buttons
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("▶  Start Sharing")
        self.start_btn.setStyleSheet(
            "QPushButton{background:#1d4ed8;color:white;font-weight:600;padding:7px 20px;border-radius:6px;}"
            "QPushButton:hover{background:#1e40af;}"
            "QPushButton:disabled{background:#93c5fd;}"
        )
        self.stop_btn = QPushButton("⏹  Stop")
        self.stop_btn.setStyleSheet(
            "QPushButton{background:#dc2626;color:white;font-weight:600;padding:7px 20px;border-radius:6px;}"
            "QPushButton:hover{background:#b91c1c;}"
            "QPushButton:disabled{background:#fca5a5;}"
        )
        open_folder_btn = QPushButton("Open Selected Folder")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        open_folder_btn.clicked.connect(self._open_folder)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(open_folder_btn)
        btn_row.addStretch(1)
        root_layout.addLayout(btn_row)

        # ── status
        self.status_label = QLabel("Choose a folder and click Start Sharing.")
        self.status_label.setStyleSheet("color:#374151;")
        root_layout.addWidget(self.status_label)

        # ── access log
        log_box = QGroupBox("Access Log")
        log_box.setStyleSheet("QGroupBox{font-weight:600;}")
        log_v = QVBoxLayout(log_box)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet(
            "background:#111827;color:#d1fae5;font-family:monospace;font-size:12px;"
        )
        self.log_view.setPlaceholderText("Access events will appear here once the server is running…")
        log_v.addWidget(self.log_view)
        root_layout.addWidget(log_box, 1)

        self._set_running(False)

    # ── populate fields from saved config ─────────────────────────────────────

    def _load_values(self) -> None:
        self.folder_input.setText(self.cfg.share_folder)
        self.title_input.setText(self.cfg.title)
        self.port_input.setValue(self.cfg.port)
        self.show_hidden_cb.setChecked(self.cfg.show_hidden)
        self.public_link_cb.setChecked(self.cfg.enable_public_link)

    # ── collect current form values ───────────────────────────────────────────

    def _to_config(self) -> AppConfig:
        return AppConfig(
            share_folder=self.folder_input.text().strip(),
            title=self.title_input.text().strip() or DEFAULT_TITLE,
            port=self.port_input.value(),
            show_hidden=self.show_hidden_cb.isChecked(),
            enable_public_link=self.public_link_cb.isChecked(),
        )

    # ── UI state helpers ──────────────────────────────────────────────────────

    def _set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    # ── slot handlers ─────────────────────────────────────────────────────────

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose folder to share")
        if folder:
            self.folder_input.setText(folder)

    def _open_folder(self) -> None:
        folder = self.folder_input.text().strip()
        if not folder:
            QMessageBox.information(self, APP_NAME, "Choose a folder first.")
            return
        path = Path(folder).expanduser()
        if not path.exists():
            QMessageBox.warning(self, APP_NAME, "Folder does not exist.")
            return
        webbrowser.open(path.as_uri())

    def _open_link(self, link: str) -> None:
        if link:
            webbrowser.open(link)
        else:
            QMessageBox.information(self, APP_NAME, "No link yet — start sharing first.")

    def _copy_link(self, link: str) -> None:
        if link:
            QGuiApplication.clipboard().setText(link)
            self._set_status("Link copied to clipboard.")
        else:
            QMessageBox.information(self, APP_NAME, "No link yet — start sharing first.")

    def _start(self) -> None:
        try:
            cfg = self._to_config()
            if not cfg.share_folder:
                raise ValueError("No folder selected. Click Browse and choose a folder.")
            save_config(cfg)
            self.cfg = cfg
            local, public = self.controller.start(cfg)
            self.local_link_field.setText(local)
            self.public_link_field.setText(public)
            self._set_running(True)
            if public:
                self._set_status(f"Sharing live · Public: {public}")
                self._open_link(public)
            else:
                self._set_status(f"Sharing live · Local: {local}")
                self._open_link(local)
        except Exception as exc:
            self.controller.stop()
            self.local_link_field.clear()
            self.public_link_field.clear()
            self._set_running(False)
            self._set_status("Failed to start.")
            QMessageBox.critical(self, APP_NAME, str(exc))

    def _stop(self) -> None:
        self.controller.stop()
        self.local_link_field.clear()
        self.public_link_field.clear()
        self._set_running(False)
        self._set_status("Sharing stopped.")

    def _drain_logs(self) -> None:
        lines = self.controller.pop_logs()
        if lines:
            self.log_view.append("\n".join(lines))

    def closeEvent(self, event: QCloseEvent) -> None:
        self.controller.stop()
        save_config(self._to_config())
        event.accept()


# ─────────────────────────── entry point ──────────────────────────────────────


def main() -> None:
    app = QApplication([])
    app.setStyle("Fusion")
    win = Window()
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
