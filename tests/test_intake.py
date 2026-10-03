"""Inbox intake (spec §3 "Inbox", §4.1): stable-size wait, ignore rules, sha256 dedupe,
move (not copy) to data/raw, duplicates kept in raw/_duplicates, missing inbox reported."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from reelengine import db, intake


@pytest.fixture
def inbox(cfg):
    p = cfg.inbox
    p.mkdir(parents=True, exist_ok=True)
    return p


def _clip(path, payload: bytes | None = None):
    path.write_bytes(payload if payload is not None else os.urandom(2048))
    return path


def _reels(conn):
    return list(conn.execute("SELECT * FROM reels ORDER BY id"))


def test_file_is_taken_only_after_two_unchanged_scans(cfg, conn, inbox):
    src = _clip(inbox / "IMG_0001.MOV")
    data = src.read_bytes()
    assert cfg["intake"]["stable_ticks"] == 2
    r1 = intake.scan(cfg, conn)
    assert (r1["seen"], r1["waiting"], r1["taken"]) == (1, 1, [])      # first sighting
    r2 = intake.scan(cfg, conn)
    assert (r2["waiting"], r2["taken"]) == (1, [])                       # unchanged once
    r3 = intake.scan(cfg, conn)                                          # unchanged twice -> taken
    assert len(r3["taken"]) == 1 and r3["errors"] == []
    rid = r3["taken"][0]
    row = db.get_reel(conn, rid)
    assert not src.exists()  # moved out of OneDrive, not copied
    raw = cfg.data_dir / "raw"
    assert os.path.dirname(row["raw_path"]) == str(raw)
    assert os.path.basename(row["raw_path"]).startswith(f"{rid:05d}-") and row["raw_path"].endswith(".mov")
    with open(row["raw_path"], "rb") as f:
        assert f.read() == data
    assert row["sha256"] == intake.sha256_file(Path(row["raw_path"]))
    assert row["state"] == "new" and row["source_name"] == "IMG_0001.MOV"
    assert conn.execute("SELECT COUNT(*) FROM inbox_watch").fetchone()[0] == 0
    assert intake.scan(cfg, conn)["seen"] == 0


def test_growing_file_is_not_taken(cfg, conn, inbox):
    src = _clip(inbox / "upload.mp4", b"x" * 100)
    for i in range(4):
        res = intake.scan(cfg, conn)
        assert res["taken"] == [] and res["waiting"] == 1
        with open(src, "ab") as f:  # OneDrive is still writing
            f.write(b"y" * (100 + i))
    assert _reels(conn) == []
    assert intake.scan(cfg, conn)["taken"] == []   # sees the last append
    assert intake.scan(cfg, conn)["taken"] == []   # unchanged once
    assert len(intake.scan(cfg, conn)["taken"]) == 1  # unchanged twice -> taken
    assert os.path.getsize(_reels(conn)[0]["raw_path"]) == 100 + 100 + 101 + 102 + 103


def test_empty_file_is_never_taken(cfg, conn, inbox):
    _clip(inbox / "zero.mp4", b"")
    for _ in range(4):
        assert intake.scan(cfg, conn)["taken"] == []
    assert _reels(conn) == []


def test_partial_temp_hidden_and_other_extensions_are_ignored(cfg, conn, inbox):
    for name in ("clip.mp4.part", "clip.mov.tmp", "clip.crdownload", ".hidden.mp4", "~$lock.mp4", "_staging.mp4", "notes.txt", "photo.jpg"):
        _clip(inbox / name)
    (inbox / "folder.mp4").mkdir()
    for _ in range(3):
        res = intake.scan(cfg, conn)
        assert res["seen"] == 0 and res["taken"] == [] and res["errors"] == []
    assert _reels(conn) == [] and conn.execute("SELECT COUNT(*) FROM inbox_watch").fetchone()[0] == 0
    assert len(list(inbox.iterdir())) == 9  # nothing moved


def test_extension_match_is_case_insensitive(cfg, conn, inbox):
    _clip(inbox / "A.MP4")
    _clip(inbox / "b.M4v")
    for _ in range(3):
        res = intake.scan(cfg, conn)
    assert len(_reels(conn)) == 2


def test_duplicate_is_moved_to_duplicates_not_deleted(cfg, conn, inbox):
    payload = os.urandom(4096)
    _clip(inbox / "first.mp4", payload)
    for _ in range(3):
        intake.scan(cfg, conn)
    assert len(_reels(conn)) == 1
    rid = _reels(conn)[0]["id"]

    _clip(inbox / "again.mp4", payload)  # same bytes, new name
    results = [intake.scan(cfg, conn) for _ in range(3)]
    assert results[-1]["duplicates"] == 1 and results[-1]["taken"] == []
    assert len(_reels(conn)) == 1  # no new row
    dup = cfg.data_dir / "raw" / "_duplicates" / "again.mp4"
    assert dup.exists() and dup.read_bytes() == payload and not (inbox / "again.mp4").exists()
    ev = [r["detail"] for r in conn.execute("SELECT detail FROM events WHERE reel_id=? ORDER BY id", (rid,))]
    assert any("duplicate upload ignored: again.mp4" in d for d in ev)

    # a second duplicate with the same name is kept too (no overwrite)
    _clip(inbox / "again.mp4", payload)
    for _ in range(3):
        intake.scan(cfg, conn)
    assert sorted(p.name for p in (cfg.data_dir / "raw" / "_duplicates").iterdir()) == ["again-1.mp4", "again.mp4"]


def test_take_without_move_copies(cfg, conn, tmp_path):
    src = _clip(tmp_path / "by_hand.mov")
    rid = intake.take(cfg, conn, src, move=False)
    row = db.get_reel(conn, rid)
    assert src.exists()  # `reel process <file>` never moves the user's file
    with open(row["raw_path"], "rb") as f:
        assert f.read() == src.read_bytes()
    assert intake.take(cfg, conn, src, move=False) is None  # same file again = duplicate
    assert src.exists() and len(_reels(conn)) == 1
    assert not (cfg.data_dir / "raw" / "_duplicates").exists()


def test_failed_move_removes_the_row_so_the_file_is_retried(cfg, conn, inbox, monkeypatch):
    src = _clip(inbox / "locked.mp4")

    def boom(*a, **kw):
        raise PermissionError("file is locked by OneDrive")

    with monkeypatch.context() as m:
        m.setattr(intake.shutil, "move", boom)
        for _ in range(2):
            intake.scan(cfg, conn)
        res = intake.scan(cfg, conn)
    assert res["taken"] == [] and "locked by OneDrive" in res["errors"][0]
    assert _reels(conn) == [] and src.exists()
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    res = intake.scan(cfg, conn)  # still stable, so the next tick retries the move
    assert len(res["taken"]) == 1 and len(_reels(conn)) == 1 and not src.exists()


def test_missing_inbox_is_reported(cfg, conn):
    assert not cfg.inbox.exists()
    res = intake.scan(cfg, conn)
    assert res["taken"] == [] and res["seen"] == 0
    assert res["errors"] == [f"inbox not found: {cfg.inbox}"]


def test_file_that_disappears_is_forgotten(cfg, conn, inbox):
    src = _clip(inbox / "gone.mp4")
    intake.scan(cfg, conn)
    assert conn.execute("SELECT COUNT(*) FROM inbox_watch").fetchone()[0] == 1
    src.unlink()
    intake.scan(cfg, conn)
    assert conn.execute("SELECT COUNT(*) FROM inbox_watch").fetchone()[0] == 0
