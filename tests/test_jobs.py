"""Jobs: tick on an empty inbox, the single-instance lock, daily backups (rotation +
integrity + OneDrive copy), and publish_due respecting the kill switch and the daily cap."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from reelengine import db, jobs, pipeline
from reelengine.config import load_config

# A fixed instant outside quiet hours (00:30-11:00 Riyadh): 2026-10-14 (Wed) 15:00 Asia/Riyadh.
# The publish tests used the real clock, so they failed whenever they ran during quiet hours.
FROZEN_NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def frozen_clock(monkeypatch):
    """Freeze db.utcnow (every engine timestamp and default `now` goes through it)."""
    monkeypatch.setattr(db, "utcnow", lambda: FROZEN_NOW)
    return FROZEN_NOW


@pytest.fixture()
def cfg(tmp_path):
    (tmp_path / "inbox").mkdir()
    return load_config(overrides={
        "paths": {"data_dir": str(tmp_path / "data"), "inbox": str(tmp_path / "inbox")},
        "notify": {"toast": False},
        "backup": {"keep": 3, "onedrive_copy_dir": str(tmp_path / "onedrive_backup")},
    })


def test_tick_on_empty_inbox(cfg):
    s = jobs.tick(cfg, toast=False)
    assert s["mode"] == "dry-run"
    assert s["intake"]["seen"] == 0 and s["publish"]["due"] == 0
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT kind, status FROM ticks ORDER BY id DESC").fetchone()
    assert (row["kind"], row["status"]) == ("tick", "ok")
    assert (cfg.data_dir / "dashboard" / "index.html").exists()


def test_single_instance_lock(cfg):
    with jobs.engine_lock(cfg):
        with pytest.raises(jobs.Locked):
            with jobs.engine_lock(cfg):
                pass
    with jobs.engine_lock(cfg):  # released again
        pass


def test_daily_backup_rotation_and_copy(cfg):
    conn = db.connect(cfg.db_path)
    for k in range(5):
        bdir = cfg.dir("backups")
        (bdir / f"engine-2026010{k}-0330.sqlite").write_bytes(b"old")
    res = jobs.backup_db(cfg, conn)
    assert res["ok"] and res["onedrive"]
    kept = sorted(cfg.dir("backups").glob("engine-*.sqlite"))
    assert len(kept) == 3 and kept[-1].name == res["path"].split("\\")[-1].split("/")[-1]
    assert sqlite3.connect(res["path"]).execute("PRAGMA quick_check").fetchone()[0] == "ok"


def _scheduled_reel(cfg, conn, sha="a" * 64, minutes_ago=5):
    rid = db.insert_reel(conn, sha, "x.mov", "raw.mov")
    for st in ("transcribed", "planned", "rendered", "qa_passed"):
        db.transition(conn, rid, st, "test")
    when = (db.utcnow() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    db.transition(conn, rid, "scheduled", "test", scheduled_at=when, slot_arm="weekday-21:30",
                  caption_json=db.jdump({"caption": "نص #وظائف", "first_line": "نص"}), plan_json=db.jdump({"hook": {"on_screen": "x"}, "cover_t_out": 1.1}),
                  duration_s=30.0)
    return rid


@pytest.mark.usefixtures("frozen_clock")
def test_publish_due_respects_kill_switch(cfg):
    conn = db.connect(cfg.db_path)
    ctx = pipeline.Ctx.create(cfg, conn, toast=False)
    rid = _scheduled_reel(cfg, conn)
    cfg.pause_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.pause_file.write_text("paused by test\n", encoding="utf-8")
    res = jobs.publish_due(ctx)
    assert "paused" in res.get("skipped", "")
    assert db.get_reel(conn, rid)["state"] == "scheduled"
    cfg.pause_file.unlink()
    res = jobs.publish_due(ctx)
    assert res["published"] == [rid]
    assert db.get_reel(conn, rid)["dry_run"] == 1


@pytest.mark.usefixtures("frozen_clock")
def test_daily_cap_reschedules_second_reel(cfg):
    conn = db.connect(cfg.db_path)
    ctx = pipeline.Ctx.create(cfg, conn, toast=False)
    first = _scheduled_reel(cfg, conn, "b" * 64, minutes_ago=10)
    assert jobs.publish_due(ctx)["published"] == [first]
    second = _scheduled_reel(cfg, conn, "c" * 64, minutes_ago=5)
    res = jobs.publish_due(ctx)
    assert res["published"] == [] and "daily cap" in res["outcomes"][second]
    row = db.get_reel(conn, second)
    assert row["state"] == "scheduled" and db.parse_ts(row["scheduled_at"]) > db.utcnow()


@pytest.mark.usefixtures("frozen_clock")
def test_late_or_quiet_reels_are_rescheduled_not_posted(cfg):  # fact-check #7
    conn = db.connect(cfg.db_path)
    ctx = pipeline.Ctx.create(cfg, conn, toast=False)
    rid = _scheduled_reel(cfg, conn, "d" * 64, minutes_ago=45)  # PC asleep: 45 min past the slot
    res = jobs.publish_due(ctx)
    assert res["published"] == [] and "missed its slot" in res["outcomes"][rid]
    row = db.get_reel(conn, rid)
    assert row["state"] == "scheduled" and db.parse_ts(row["scheduled_at"]) > db.utcnow()
    # quiet hours: a reel that is due right now is still not posted at 03:00 Riyadh
    rid2 = _scheduled_reel(cfg, conn, "e" * 64, minutes_ago=1)
    three_am = db.utcnow().astimezone(cfg.tz).replace(hour=3, minute=0, second=0, microsecond=0).astimezone(db.utcnow().tzinfo)
    conn.execute("UPDATE reels SET scheduled_at=? WHERE id=?", ((three_am - timedelta(minutes=1)).isoformat(timespec="seconds"), rid2))
    res = jobs.publish_due(ctx, now=three_am)
    assert "quiet hours" in res["outcomes"][rid2]


@pytest.mark.usefixtures("frozen_clock")
def test_requeue_only_for_dry_run_rehearsals(cfg, capsys):  # fact-check #1 (HIGH)
    from types import SimpleNamespace

    from reelengine import cli

    conn = db.connect(cfg.db_path)
    held = db.insert_reel(conn, "f" * 64, "x.mov", "raw.mov")
    db.transition(conn, held, "held", "facts gate", held_reason="Facts gate: 'الفائز'")
    rendered = db.insert_reel(conn, "g" * 64, "y.mov", "raw.mov")
    for st in ("transcribed", "planned", "rendered"):
        db.transition(conn, rendered, st, "t")
    failed = db.insert_reel(conn, "h" * 64, "z.mov", "raw.mov")
    db.transition(conn, failed, "failed", "boom", failed_from="new")
    for rid in (held, rendered, failed):
        assert cli.cmd_requeue(cfg, SimpleNamespace(id=rid)) == 2
        assert db.get_reel(conn, rid)["state"] != "scheduled"
    assert "reel approve" in capsys.readouterr().out
    with pytest.raises(db.TransitionError):  # QA can't be skipped from held at the DB level either
        db.transition(conn, held, "qa_passed", "sneak")
    # a dry-run rehearsal CAN be re-queued for a real post
    ctx = pipeline.Ctx.create(cfg, conn, toast=False)
    ok = _scheduled_reel(cfg, conn, "i" * 64, minutes_ago=5)
    jobs.publish_due(ctx)
    assert db.get_reel(conn, ok)["dry_run"] == 1
    assert cli.cmd_requeue(cfg, SimpleNamespace(id=ok)) == 0
    row = db.get_reel(conn, ok)
    assert row["state"] == "scheduled" and row["dry_run"] == 0 and row["media_id"] is None


def test_daily_sends_the_occasion_reminders_once(cfg):
    """jobs.daily runs occasions.remind (v1.2 leads); a second daily the same day sends nothing again."""
    from reelengine import brain, occasions

    brain.set_provider(brain.FakeBrain({}))  # ideas: templates (no real CLI)
    try:
        conn = db.connect(cfg.db_path)
        today = occasions.local_today(cfg)
        occasions.add(conn, {"name_ar": "يوم اليوم", "name_en": "Jobs Test Day", "rule": "once", "year": today.year,
                             "month": today.month, "day": today.day, "category": "tech"})
        s = jobs.daily(cfg, toast=False)
        assert "jobs-test-day" in s["occasion_reminders"]
        assert jobs.daily(cfg, toast=False)["occasion_reminders"] == []
        rows = conn.execute("SELECT title, body FROM notifications WHERE title LIKE 'Jobs Test Day%'").fetchall()
        assert len(rows) == 1 and rows[0]["title"] == "Jobs Test Day is today" and "Checklist:" in rows[0]["body"]
    finally:
        brain.set_provider(None)
