"""Inbox intake (spec §3, §4.1).

A file in the OneDrive inbox is taken only after its size (and mtime) has been stable
for `intake.stable_ticks` consecutive ticks. Then it is hashed (sha256, dedupe) and
MOVED to data/raw/ (out of OneDrive, to save cloud space) and a `reels` row is created.
Duplicates (same sha256) are moved to data/raw/_duplicates/, never deleted.
OneDrive "online-only" files are downloaded on first read; SETUP.md recommends
"Always keep on this device" for the inbox folder.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from . import db
from .log import get_logger

log = get_logger("intake")

IGNORE_PREFIXES = (".", "~$", "_")
IGNORE_SUFFIXES = (".tmp", ".part", ".crdownload", ".partial", ".download")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _candidates(inbox: Path, exts: tuple[str, ...]) -> list[Path]:
    out = []
    for p in inbox.iterdir():
        if not p.is_file() or p.name.startswith(IGNORE_PREFIXES) or p.name.lower().endswith(IGNORE_SUFFIXES):
            continue
        if p.suffix.lower() in exts:
            out.append(p)
    return sorted(out)


def _safe_move(src: Path, dest_dir: Path, name: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    k = 1
    while dest.exists():
        dest = dest_dir / f"{Path(name).stem}-{k}{Path(name).suffix}"
        k += 1
    shutil.move(str(src), str(dest))
    return dest


def scan(cfg, conn, inbox: Path | None = None) -> dict:
    """One intake pass. Returns {'seen', 'waiting', 'taken':[reel ids], 'duplicates', 'errors'}."""
    inbox = inbox or cfg.inbox
    res = {"seen": 0, "waiting": 0, "taken": [], "duplicates": 0, "errors": []}
    if not inbox.exists():
        res["errors"].append(f"inbox not found: {inbox}")
        return res
    exts = tuple(e.lower() for e in cfg["intake"]["extensions"])
    need = int(cfg["intake"]["stable_ticks"])
    now = db.now_iso()
    present = set()
    for p in _candidates(inbox, exts):
        res["seen"] += 1
        key = str(p.resolve())
        present.add(key)
        try:
            st = p.stat()
        except OSError as exc:
            res["errors"].append(f"{p.name}: {exc}")
            continue
        row = conn.execute("SELECT * FROM inbox_watch WHERE path=?", (key,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO inbox_watch(path, size, mtime, stable_count, first_seen, last_seen) VALUES(?,?,?,?,?,?)",
                (key, st.st_size, st.st_mtime, 0, now, now),
            )
            res["waiting"] += 1
            continue
        if row["size"] != st.st_size or abs(row["mtime"] - st.st_mtime) > 1e-6 or st.st_size == 0:
            conn.execute("UPDATE inbox_watch SET size=?, mtime=?, stable_count=0, last_seen=? WHERE path=?", (st.st_size, st.st_mtime, now, key))
            res["waiting"] += 1
            continue
        stable = row["stable_count"] + 1
        conn.execute("UPDATE inbox_watch SET stable_count=?, last_seen=? WHERE path=?", (stable, now, key))
        if stable < need:
            res["waiting"] += 1
            continue
        try:
            rid = take(cfg, conn, p)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the tick
            log.exception("intake failed for %s", p.name)
            res["errors"].append(f"{p.name}: {exc}")
            continue
        conn.execute("DELETE FROM inbox_watch WHERE path=?", (key,))
        if rid is None:
            res["duplicates"] += 1
        else:
            res["taken"].append(rid)
    # forget files that disappeared from the inbox
    for r in conn.execute("SELECT path FROM inbox_watch").fetchall():
        if r["path"] not in present and Path(r["path"]).parent.resolve() == inbox.resolve():
            conn.execute("DELETE FROM inbox_watch WHERE path=?", (r["path"],))
    return res


def take(cfg, conn, src: Path, move: bool = True) -> int | None:
    """Hash, move to data/raw and insert a reel row. Returns the reel id, or None for a
    duplicate. The DB row is written before the move completes; if the move fails the
    row is removed again so the file is retried next tick."""
    digest = sha256_file(src)
    raw_dir = cfg.dir("raw")
    dup = conn.execute("SELECT id FROM reels WHERE sha256=?", (digest,)).fetchone()
    if dup:
        dest = _safe_move(src, raw_dir / "_duplicates", src.name) if move else src
        log.info("duplicate of reel %s: %s -> %s", dup["id"], src.name, dest)
        db.add_event(conn, dup["id"], f"duplicate upload ignored: {src.name}")
        return None
    ext = src.suffix.lower()
    with db.tx(conn):
        rid = db.insert_reel(conn, digest, src.name, "")
    name = f"{rid:05d}-{digest[:10]}{ext}"
    try:
        if move:
            dest = _safe_move(src, raw_dir, name)
        else:
            dest = raw_dir / name
            shutil.copy2(src, dest)
    except Exception:
        with db.tx(conn):
            conn.execute("DELETE FROM events WHERE reel_id=?", (rid,))
            conn.execute("DELETE FROM reels WHERE id=?", (rid,))
        raise
    db.update_reel(conn, rid, raw_path=str(dest))
    log.info("intake reel %s: %s (%.1f MB)", rid, src.name, os.path.getsize(dest) / 1e6)
    return rid
