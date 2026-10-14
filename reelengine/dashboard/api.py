"""The dashboard's small write API (feature 2026-10-14): clip upload, calendar edits, attached accounts.

It only exists when the server is started with writes on (`[dashboard] allow_writes = true`, the
default; `reel dashboard --read-only` turns it off). Every rule of the read-only server still holds
(127.0.0.1 only, Host-header check, strict CSP and security headers). On top, every POST needs:

- an `Origin` header naming this same loopback origin (and `Sec-Fetch-Site: same-origin` when the
  browser sends it), so another website can't post here;
- `X-CSRF-Token` equal to the per-process token from GET /api/session. That response carries no CORS
  headers, so only a same-origin page can read the token; a custom header also forces a CORS
  preflight, and OPTIONS is answered 405;
- a `Content-Length` (no chunked bodies) inside the endpoint's cap.

Uploads stream to a dot-prefixed temp file in the inbox (intake ignores dot files), are hashed on the
way, checked (size, type allowlist, MP4/QuickTime signature, duplicate sha256), then moved into place
with os.replace. From there the normal intake path takes over: stability ticks, sha256 dedupe, the
move to data/raw, and every QA gate. Uploading never schedules or publishes anything, and dry-run
stays the default.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import unicodedata
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

from .. import accounts as acctmod
from .. import db
from .. import occasions as occmod
from ..log import get_logger, redact

log = get_logger("dashboard.api")

JSON_MAX = 256 * 1024
UPLOAD_TYPES = {
    "video/mp4": (".mp4", ".m4v"),
    "video/quicktime": (".mov",),
    "video/x-m4v": (".m4v", ".mp4"),
}
# ISO-BMFF / QuickTime top-level atoms a phone clip can start with (bytes 4..8)
MP4_ATOMS = (b"ftyp", b"moov", b"wide", b"mdat", b"free", b"skip", b"pnot")
CHUNK = 1024 * 1024
SOCKET_TIMEOUT_S = 60


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def safe_filename(name: str, allowed_exts: tuple[str, ...]) -> str:
    """Basename only, ASCII letters/digits/._- , no leading dot, max 64 chars, allowed extension."""
    base = re.split(r"[\\/]", unicodedata.normalize("NFKC", name or ""))[-1].strip()
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in allowed_exts:
        raise ApiError(415, f"only {', '.join(allowed_exts)} files")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem, flags=re.ASCII).strip("-_")[:56]
    return f"{stem or 'clip'}{ext}"


class WriteApi:
    def __init__(self, cfg, db_path: Path, site_dir: Path, *, demo: bool = False,
                 rebuild: Callable[[], Any] | None = None, inbox: Path | None = None):
        self.cfg = cfg
        self.db_path = Path(db_path)
        self.site_dir = Path(site_dir)
        self.demo = demo
        self.inbox = Path(inbox or cfg.inbox)
        self.csrf = secrets.token_urlsafe(32)
        self._rebuild = rebuild
        self._build_lock = threading.Lock()
        self._upload_lock = threading.Lock()
        exts = tuple(e.lower() for e in cfg["intake"]["extensions"])
        self.exts = tuple(e for e in exts if e in (".mp4", ".mov", ".m4v"))
        self.upload_on = bool(cfg.get("upload.enabled", True)) and not demo
        self.max_bytes = int(cfg.get("upload.max_mb", 2048)) * 1024 * 1024

    # ------------------------------------------------------------------ plumbing
    def _conn(self):
        return db.connect(self.db_path)

    def rebuild(self) -> None:
        if self._rebuild is None:
            return
        with self._build_lock:
            try:
                self._rebuild()
            except Exception as exc:  # noqa: BLE001 - a failed rebuild must not lose the write
                log.warning("dashboard rebuild after a write failed: %s", exc)

    def session(self) -> dict:
        return {"csrf": self.csrf, "writes": True, "upload": self.upload_on, "demo": self.demo,
                "max_mb": self.max_bytes // (1024 * 1024), "extensions": list(self.exts)}

    def check_post(self, h) -> None:
        """Origin + Sec-Fetch-Site + CSRF + no chunked bodies. Raises ApiError."""
        host = h.headers.get("Host") or ""
        origin = h.headers.get("Origin")
        if not origin or origin != f"http://{host}":
            raise ApiError(403, "cross-origin request refused")
        sfs = h.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs not in ("same-origin", "none"):
            raise ApiError(403, "cross-site request refused")
        tok = h.headers.get("X-CSRF-Token") or ""
        if not hmac.compare_digest(tok.encode(), self.csrf.encode()):
            raise ApiError(403, "missing or wrong CSRF token (reload the page)")
        if h.headers.get("Transfer-Encoding"):
            raise ApiError(411, "chunked bodies are not accepted")

    @staticmethod
    def length(h, cap: int) -> int:
        raw = h.headers.get("Content-Length")
        if raw is None or not raw.isdigit():
            raise ApiError(411, "Content-Length required")
        n = int(raw)
        if n > cap:
            raise ApiError(413, f"too large (max {cap // (1024 * 1024) or 1} MB)" if cap >= 1024 * 1024 else "request body too large")
        return n

    def read_json(self, h) -> dict:
        ctype = (h.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "JSON only")
        n = self.length(h, JSON_MAX)
        h.connection.settimeout(SOCKET_TIMEOUT_S)
        body = h.rfile.read(n)
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "bad JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "bad JSON")
        return data

    # ------------------------------------------------------------------ routes
    def get(self, path: str) -> tuple[int, dict]:
        if path == "/api/session":
            return 200, self.session()
        if path == "/api/uploads":
            return 200, {"uploads": self.uploads()}
        raise ApiError(404, "not found")

    def post(self, h, path: str) -> tuple[int, dict]:
        self.check_post(h)
        if path == "/api/upload":
            return self.upload(h)
        if path == "/api/calendar":
            return self.calendar(self.read_json(h))
        if path == "/api/accounts":
            return self.accounts(self.read_json(h))
        raise ApiError(404, "not found")

    # ------------------------------------------------------------------ calendar
    def calendar(self, d: dict) -> tuple[int, dict]:
        action = d.get("action")
        fields = d.get("fields") if isinstance(d.get("fields"), dict) else {}
        conn = self._conn()
        try:
            if action == "add":
                o = occmod.add(conn, fields, is_demo=self.demo)
            elif action == "edit":
                o = occmod.edit(conn, str(d.get("slug") or ""), fields)
            elif action == "delete":
                if not occmod.delete(conn, str(d.get("slug") or "")):
                    raise ApiError(404, "no such occasion")
                o = None
            else:
                raise ApiError(400, "action must be add, edit or delete")
        except occmod.OccasionError as exc:
            raise ApiError(400, str(exc)) from None
        finally:
            conn.close()
        self.rebuild()
        return 200, {"ok": True, "slug": o["slug"] if o else d.get("slug")}

    # ------------------------------------------------------------------ accounts
    def accounts(self, d: dict) -> tuple[int, dict]:
        action = d.get("action")
        conn = self._conn()
        try:
            if action == "attach":
                r = acctmod.attach(conn, str(d.get("username") or ""), str(d.get("role") or "reference"),
                                   notes=str(d.get("notes") or ""), is_demo=self.demo)
                res: dict = {"ok": True, "username": r["username"]}
            elif action == "forget":
                if not acctmod.forget(conn, str(d.get("username") or "")):
                    raise ApiError(404, "not attached")
                res = {"ok": True}
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # deleted rows must not linger in the WAL ...
                    conn.execute("VACUUM")  # ... or in free pages (secure_delete is on in forget)
                except Exception as exc:  # noqa: BLE001 - a busy engine: the delete itself is committed
                    log.warning("VACUUM after forget deferred: %s", exc)
                    res["vacuum"] = "deferred"
            elif action == "import":
                text = str(d.get("text") or "")
                rows = acctmod.parse_csv(text) if d.get("format") == "csv" else acctmod.parse_paste(text)
                out = acctmod.import_rows(conn, self.cfg, rows, default_username=str(d.get("username") or "") or None)
                res = {"ok": not out["errors"], "stored": out["stored"], "errors": out["errors"][:20], "accounts": out["accounts"]}
            else:
                raise ApiError(400, "action must be attach, forget or import")
        except acctmod.AccountError as exc:
            raise ApiError(400, str(exc)) from None
        finally:
            conn.close()
        self.rebuild()
        return 200, res

    # ------------------------------------------------------------------ upload
    def upload(self, h) -> tuple[int, dict]:
        if not self.upload_on:
            raise ApiError(403, "uploads are off here (demo data, or [upload] enabled = false)")
        ctype = (h.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in UPLOAD_TYPES:
            raise ApiError(415, "only MP4 / MOV / M4V video")
        name = safe_filename(unquote(h.headers.get("X-Filename") or ""), self.exts)
        if Path(name).suffix not in UPLOAD_TYPES[ctype] and not (ctype == "video/quicktime" and name.endswith(".mov")):
            raise ApiError(415, "the file extension does not match its type")
        n = self.length(h, self.max_bytes)
        if n < 1024:
            raise ApiError(400, "file is empty or too small to be a video")
        self.inbox.mkdir(parents=True, exist_ok=True)
        min_free = float(self.cfg.get("upload.min_free_gb", 5)) * 1024 ** 3
        if shutil.disk_usage(self.inbox).free - n < min_free:
            raise ApiError(507, "not enough free disk space for this upload")
        if not self._upload_lock.acquire(blocking=False):
            raise ApiError(409, "another upload is running; wait for it to finish")
        tmp = self.inbox / f".upload-{secrets.token_hex(8)}.part"
        try:
            h.connection.settimeout(SOCKET_TIMEOUT_S)
            digest = hashlib.sha256()
            got = 0
            head = b""
            with open(tmp, "xb") as f:
                while got < n:
                    chunk = h.rfile.read(min(CHUNK, n - got))
                    if not chunk:
                        break
                    if len(head) < 12:
                        head += chunk[: 12 - len(head)]
                        if len(head) >= 12 and head[4:8] not in MP4_ATOMS:
                            raise ApiError(415, "this is not an MP4/MOV video file")
                    digest.update(chunk)
                    f.write(chunk)
                    got += len(chunk)
                f.flush()
                os.fsync(f.fileno())
            if got != n:
                raise ApiError(400, "upload interrupted; try again")
            sha = digest.hexdigest()
            conn = self._conn()
            try:
                dup = conn.execute("SELECT id FROM reels WHERE sha256=?", (sha,)).fetchone()
                prev = conn.execute("SELECT stored_name FROM uploads WHERE sha256=? AND status='queued'", (sha,)).fetchone()
                if dup or (prev and (self.inbox / prev["stored_name"]).exists()):
                    tmp.unlink()
                    with db.tx(conn):
                        cur = conn.execute("INSERT INTO uploads(created_at, original_name, stored_name, size_bytes, sha256, status, duplicate_of) VALUES(?,?,?,?,?,?,?)",
                                           (db.now_iso(), name, name, n, sha, "duplicate", dup["id"] if dup else None))
                    return 200, {"id": cur.lastrowid, "name": name, "size": n, "status": "duplicate", "reel": dup["id"] if dup else None}
                dest = self._place(tmp, name)
                with db.tx(conn):
                    cur = conn.execute("INSERT INTO uploads(created_at, original_name, stored_name, size_bytes, sha256, status) VALUES(?,?,?,?,?,?)",
                                       (db.now_iso(), name, dest.name, n, sha, "queued"))
            finally:
                conn.close()
            log.info("upload: %s (%.1f MB) queued for intake", dest.name, n / 1e6)
            self.rebuild()
            return 200, {"id": cur.lastrowid, "name": dest.name, "size": n, "status": "queued"}
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            self._upload_lock.release()

    def _place(self, tmp: Path, name: str) -> Path:
        """Move tmp into the inbox under a name nobody uses, atomically and without overwriting:
        os.link fails if the name exists (e.g. a OneDrive clip landed meanwhile), then the temp is unlinked."""
        stem, ext = Path(name).stem, Path(name).suffix
        for k in range(1000):
            dest = self.inbox / (name if k == 0 else f"{stem}-{k}{ext}")
            try:
                os.link(tmp, dest)
            except FileExistsError:
                continue
            except OSError:  # a filesystem without hard links: reserve the name exclusively, then replace it
                try:
                    with open(dest, "xb"):
                        pass
                except FileExistsError:
                    continue
                os.replace(tmp, dest)
                return dest
            tmp.unlink()
            return dest
        raise ApiError(409, "too many files with this name in the inbox")

    def uploads(self) -> list[dict]:
        conn = self._conn()
        try:
            return resolve_uploads(conn, self.inbox)
        finally:
            conn.close()


def resolve_uploads(conn, inbox: Path, limit: int = 10) -> list[dict]:
    """Recent uploads with what happened to them: waiting in the inbox, or the reel's state."""
    out = []
    try:
        rows = conn.execute("SELECT * FROM uploads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    except Exception:  # noqa: BLE001 - old DB
        return []
    for r in rows:
        reel = conn.execute("SELECT id, state, held_reason FROM reels WHERE sha256=?", (r["sha256"],)).fetchone()
        item = {"id": r["id"], "name": r["stored_name"], "size": r["size_bytes"], "at": r["created_at"], "status": r["status"],
                "reel": None, "state": None, "held_reason": None}
        if r["status"] == "duplicate":
            item["reel"] = r["duplicate_of"] or (reel["id"] if reel else None)
            item["state"] = "duplicate"
        elif reel:
            item.update(reel=reel["id"], state=reel["state"], held_reason=redact(reel["held_reason"] or "")[:200] or None)
        elif (inbox / r["stored_name"]).exists():
            item["state"] = "waiting"
        else:
            item["state"] = "taken"
        out.append(item)
    return out
