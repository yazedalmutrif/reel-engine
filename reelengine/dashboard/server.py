"""Local, read-only web server for the dashboard (spec §8, §9).

- Binds loopback only (127.0.0.1 / ::1 / localhost); any other host is refused.
- GET and HEAD only; every other method gets 405.
- Serves the built site, plus /media/<name>.mp4 (data/out) and /thumbs/<name>.png
  (data/thumbs), each through a strict filename regex: no percent-encoding, no dots
  in directory names, no traversal, no other directories.
- HTTP Range for mp4 so the <video> element can seek.
- Security headers (strict CSP, nosniff, no-referrer, DENY framing, no-store) on every
  response, errors included. A Host-header check blocks DNS-rebinding requests.

Manual use:  python -m reelengine.dashboard.server [--demo] [--port N] [--open]
"""
from __future__ import annotations

import argparse
import http.server
import ipaddress
import re
import socket
import stat
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .build import CSP

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ttf": "font/ttf",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".mp4": "video/mp4",
}
# site files: up to 3 plain directory levels, a name that cannot start with a dot
SITE_PATH = re.compile(r"^/(?:[A-Za-z0-9_-]+/){0,3}[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}$")
MEDIA_PATH = re.compile(r"^/media/([A-Za-z0-9_-]{1,64}\.mp4)$")
THUMB_PATH = re.compile(r"^/thumbs/([A-Za-z0-9_-]{1,64}\.png)$")
RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
LOOPBACK_NAMES = ("localhost",)


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ReelEngineDashboard"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    error_message_format = "%(code)d %(message)s\n"
    error_content_type = "text/plain; charset=utf-8"
    site_dir: Path = Path(".")
    media_root: Path = Path(".")
    quiet: bool = True

    def version_string(self) -> str:
        return self.server_version

    # every response, errors included, carries the security headers
    def end_headers(self) -> None:
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        super().end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        if not self.quiet:
            super().log_message(format, *args)

    # anything that is not GET/HEAD -> 405 (BaseHTTPRequestHandler looks methods up by name)
    def __getattr__(self, name: str):
        if name.startswith("do_"):
            return self._not_allowed
        raise AttributeError(name)

    def _not_allowed(self) -> None:
        self.close_connection = True  # the request body (if any) is never read
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._serve(head=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head=True)

    # ------------------------------------------------------------------ routing
    def _host_ok(self) -> bool:
        host = self.headers.get("Host")
        if host is None:
            return True
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if host.startswith("["):
            name = host[: host.index("]") + 1]
        return is_loopback(name)

    def _serve(self, head: bool) -> None:
        if not self._host_ok():
            self.send_error(403, "Forbidden host")
            return
        raw = self.path.split("?", 1)[0].split("#", 1)[0]
        if raw == "/":
            raw = "/index.html"
        m = MEDIA_PATH.match(raw)
        if m:
            self._file(self.media_root / "out", m.group(1), head, allow_range=True)
            return
        m = THUMB_PATH.match(raw)
        if m:
            self._file(self.media_root / "thumbs", m.group(1), head)
            return
        suffix = Path(raw).suffix.lower()
        if not SITE_PATH.match(raw) or suffix not in CONTENT_TYPES or suffix == ".mp4":
            self.send_error(404, "Not found")
            return
        if not self.site_dir.exists():
            time.sleep(0.25)  # a rebuild may be swapping the directory right now
        self._file(self.site_dir, raw.lstrip("/"), head)

    def _file(self, base: Path, rel: str, head: bool, allow_range: bool = False) -> None:
        try:
            base_r = base.resolve(strict=True)
            path = (base_r / rel).resolve(strict=True)
            path.relative_to(base_r)  # ValueError if a symlink points outside
            st = path.stat()
        except (OSError, ValueError, RuntimeError):
            self.send_error(404, "Not found")
            return
        if not stat.S_ISREG(st.st_mode):
            self.send_error(404, "Not found")
            return
        size = st.st_size
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range") if allow_range else None
        if rng:
            m = RANGE.match(rng.strip())
            if m and (m.group(1) or m.group(2)):  # other forms (multi-range) are ignored: full 200
                if m.group(1) == "":
                    start = max(0, size - int(m.group(2)))
                else:
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(length))
        if allow_range:
            self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head or length == 0:
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionError, OSError):
            self.close_connection = True  # the browser aborted (e.g. while seeking)


class DashboardServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let another process share the port

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class DashboardServer6(DashboardServer):
    address_family = socket.AF_INET6


def make_server(cfg, site_dir: Path, port: int | None = None, media_root: Path | None = None, quiet: bool = True) -> DashboardServer:
    """Create (bind) the server without starting it. Raises ValueError for a non-loopback host."""
    host = str(cfg["dashboard"]["host"])
    if not is_loopback(host):
        raise ValueError(f"dashboard host {host!r} is not loopback; the dashboard only binds 127.0.0.1 / ::1")
    if host == "localhost":
        host = "127.0.0.1"  # never let name resolution pick a non-loopback address
    port = int(cfg["dashboard"]["port"] if port is None else port)
    handler = type(
        "DashboardHandler",
        (_Handler,),
        {"site_dir": Path(site_dir).resolve(), "media_root": Path(media_root or cfg.data_dir).resolve(), "quiet": quiet},
    )
    cls = DashboardServer6 if ":" in host else DashboardServer
    return cls((host.strip("[]"), port), handler)


def serve(cfg, site_dir: Path, port: int | None = None, open_browser: bool = False, media_root: Path | None = None) -> None:
    """Serve the dashboard until Ctrl+C."""
    httpd = make_server(cfg, site_dir, port=port, media_root=media_root)
    host, prt = httpd.server_address[:2]
    url = f"http://{'[' + host + ']' if ':' in host else host}:{prt}/"
    print(f"Reel Engine dashboard on {url}  (serving {Path(site_dir)}; Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def _demo_stale(db_path: Path, max_age_h: float = 12) -> bool:
    """Demo dates are relative to seeding time; reseed when they have drifted."""
    import sqlite3
    from datetime import datetime, timezone

    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = c.execute("SELECT value FROM meta WHERE key='demo_seeded_at'").fetchone()
        c.close()
        seeded = datetime.fromisoformat(row[0])
    except Exception:  # noqa: BLE001 - any problem means: reseed
        return True
    return (datetime.now(timezone.utc) - seeded).total_seconds() > max_age_h * 3600


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m reelengine.dashboard.server", description="Serve the Reel Engine dashboard on 127.0.0.1.")
    ap.add_argument("--demo", action="store_true", help="serve the demo site (seeds data/demo/ if needed)")
    ap.add_argument("--reseed", action="store_true", help="with --demo: recreate the demo database first")
    ap.add_argument("--no-build", action="store_true", help="serve the existing site without rebuilding it")
    ap.add_argument("--port", type=int, default=None, help="port (default: [dashboard] port in config.toml)")
    ap.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    args = ap.parse_args(argv)

    from .. import db
    from ..config import load_config
    from .build import build_dashboard

    cfg = load_config()
    if args.demo:
        from .demo import seed_demo

        demo_dir = cfg.data_dir / "demo"
        db_path = demo_dir / "engine.sqlite"
        if args.reseed or not db_path.exists() or _demo_stale(db_path):
            seed_demo(db_path, cfg, demo_dir / "thumbs")
        site = cfg.data_dir / "dashboard-demo"
        if not args.no_build or not site.exists():
            conn = db.connect(db_path)
            build_dashboard(cfg, conn, demo=True)
            conn.close()
        media = demo_dir
    else:
        site = cfg.data_dir / "dashboard"
        media = cfg.data_dir
        if not args.no_build or not site.exists():
            conn = db.connect(cfg.db_path if cfg.db_path.exists() else ":memory:")
            build_dashboard(cfg, conn)
            conn.close()
    serve(cfg, site, port=args.port, open_browser=args.open, media_root=media)
    return 0


if __name__ == "__main__":
    sys.exit(main())
