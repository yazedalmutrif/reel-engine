"""Unit tests for reelengine.scheduler: quiet hours, day types, slot constraints, scheduling, due reels."""
from __future__ import annotations

import itertools
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from reelengine import db, learner, scheduler
from reelengine.config import load_config

RIYADH = ZoneInfo("Asia/Riyadh")
_seq = itertools.count()


@pytest.fixture
def cfg(tmp_path):
    return load_config().with_overrides({"paths": {"data_dir": str(tmp_path)}})


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "engine.sqlite")
    yield c
    c.close()


@pytest.fixture
def pb(conn, cfg):
    return learner.load_playbook(conn, cfg)


def local(y, mo, d, h=12, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=RIYADH)


def utc_iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def add_reel(conn, state, when=None, dry_run=0, is_demo=0, **extra):
    fields = {"state": state, "dry_run": dry_run, "is_demo": is_demo, **extra}
    if when is not None:
        fields["scheduled_at"] = utc_iso(when)
        if state == "published":
            fields["published_at"] = utc_iso(when)
    return db.insert_reel(conn, f"sha{next(_seq)}", "c.mp4", "raw/c.mp4", **fields)


def occupy(conn, days, at=(13, 30), **kw):
    for d in days:
        add_reel(conn, "published", when=datetime.combine(d, time(*at), tzinfo=RIYADH), **kw)


def day_range(start: date, n: int):
    return [start + timedelta(days=i) for i in range(n)]


def close_explore_window(conn):
    """30 real posts long ago, so the explore window (first 30 posts) is over."""
    for i in range(30):
        add_reel(conn, "published", when=local(2026, 6, 1) + timedelta(days=i))


# ---------------------------------------------------------------- quiet hours and day types
@pytest.mark.parametrize("hhmm, quiet", [("00:29", False), ("00:30", True), ("05:00", True), ("10:59", True),
                                         ("11:00", False), ("21:30", False), ("23:59", False)])
def test_default_quiet_window(cfg, hhmm, quiet):
    assert scheduler.is_quiet(time.fromisoformat(hhmm), cfg) is quiet


@pytest.mark.parametrize("hhmm, quiet", [("21:59", False), ("22:00", True), ("23:30", True), ("02:00", True),
                                         ("05:59", True), ("06:00", False), ("13:30", False)])
def test_quiet_window_crossing_midnight(cfg, hhmm, quiet):
    night = cfg.with_overrides({"schedule": {"quiet_start": "22:00", "quiet_end": "06:00"}})
    assert scheduler.is_quiet(time.fromisoformat(hhmm), night) is quiet


def test_equal_quiet_bounds_mean_no_quiet_window(cfg):
    none = cfg.with_overrides({"schedule": {"quiet_start": "03:00", "quiet_end": "03:00"}})
    assert not scheduler.is_quiet(time(3, 0), none)


@pytest.mark.parametrize("d, kind", [(date(2026, 10, 1), "weekday"), (date(2026, 10, 2), "weekend"),
                                     (date(2026, 10, 3), "weekend"), (date(2026, 10, 4), "weekday"),
                                     (date(2026, 10, 8), "weekday")])
def test_day_type_weekend_is_fri_sat(pb, d, kind):
    assert scheduler.day_type(d, pb) == kind


# ---------------------------------------------------------------- next_slot constraints
@pytest.mark.parametrize("seed", range(25))
def test_next_slot_basic_constraints(conn, cfg, pb, seed):
    now = local(2026, 10, 5, 21, 20)                              # Monday 21:20, lead 20 min
    slot = scheduler.next_slot(conn, cfg, pb, now=now, rng=seed)
    when_local, when_utc = slot["when_local"], slot["when_utc"]
    assert when_utc.tzinfo is not None and when_utc.utcoffset() == timedelta(0)
    assert when_utc >= now + timedelta(minutes=20)
    assert when_utc <= now + timedelta(days=scheduler.HORIZON_DAYS)
    assert not scheduler.is_quiet(when_local.time(), cfg)
    assert learner.slot_arm_for(when_local, pb) == slot["arm"]     # weekend arms land on Fri/Sat
    assert slot["arm"].split("-")[0] == scheduler.day_type(when_local.date(), pb)
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE dimension='slot'").fetchone()[0] == 1


def test_lead_time_skips_a_slot_that_is_too_close(conn, cfg, pb):
    close_explore_window(conn)
    conn.execute("UPDATE bandit_arms SET alpha=500, beta=1 WHERE dimension='slot' AND arm='weekday-21:30'")
    now = local(2026, 10, 5, 21, 15)                               # 15 min before a 21:30 slot
    for seed in range(5):
        slot = scheduler.next_slot(conn, cfg, pb, now=now, rng=seed)
        assert slot["arm"] == "weekday-21:30" and slot["when_local"] == local(2026, 10, 6, 21, 30)


@pytest.mark.parametrize("seed", range(10))
def test_max_posts_per_day(conn, cfg, pb, seed):
    occupy(conn, [date(2026, 10, 5)])                              # already one post Monday 13:30
    slot = scheduler.next_slot(conn, cfg, pb, now=local(2026, 10, 5, 12, 0), rng=seed)
    assert slot["when_local"].date() != date(2026, 10, 5)


def test_per_day_cap_counts_dry_run_but_not_demo(conn, cfg, pb):
    free = date(2026, 10, 8)
    occupy(conn, [d for d in day_range(date(2026, 10, 5), 16) if d != free])
    occupy(conn, [free], is_demo=1)                                # demo reels never block a day
    slot = scheduler.next_slot(conn, cfg, pb, now=local(2026, 10, 5, 12, 0), rng=1)
    assert slot["when_local"].date() == free
    occupy(conn, [free], dry_run=1)                                # dry-run posts do
    with pytest.raises(scheduler.NoSlotAvailable):
        scheduler.next_slot(conn, cfg, pb, now=local(2026, 10, 5, 12, 0), rng=1)


@pytest.mark.parametrize("seed", range(10))
def test_min_gap_hours(conn, cfg, pb, seed):
    two = cfg.with_overrides({"schedule": {"max_posts_per_day": 2, "min_gap_hours": 6}})
    today = date(2026, 10, 5)
    occupy(conn, [today])                                          # 13:30 taken
    others = day_range(today + timedelta(days=1), 15)
    occupy(conn, others)
    occupy(conn, others, at=(22, 30))                              # every other day is full
    slot = scheduler.next_slot(conn, two, pb, now=local(2026, 10, 5, 12, 0), rng=seed)
    assert slot["when_local"].date() == today
    assert slot["when_local"].time() in (time(21, 30), time(22, 30))   # 18:30 is only 5 h after 13:30


@pytest.mark.parametrize("seed", range(10))
def test_quiet_hours_are_never_scheduled(conn, cfg, pb, seed):
    evening = cfg.with_overrides({"schedule": {"quiet_start": "22:00", "quiet_end": "14:00"}})
    slot = scheduler.next_slot(conn, evening, pb, now=local(2026, 10, 5, 9, 0), rng=seed)
    assert slot["when_local"].time() in (time(18, 30), time(21, 30))


def test_prefers_an_arm_available_within_seven_days(conn, cfg, pb):
    close_explore_window(conn)
    conn.execute("UPDATE bandit_arms SET alpha=500, beta=1 WHERE dimension='slot' AND arm='weekday-21:30'")
    occupy(conn, [date(2026, 10, 1)] + day_range(date(2026, 10, 4), 5) + [date(2026, 10, 11)])
    slot = scheduler.next_slot(conn, cfg, pb, now=local(2026, 10, 1, 12, 0), rng=2)  # next free weekday: Oct 12
    assert slot["arm"].startswith("weekend-") and slot["when_local"].date() in (date(2026, 10, 2), date(2026, 10, 3))
    assert slot["explore"] is False


def test_falls_back_to_the_earliest_valid_occurrence(conn, cfg, pb):
    occupy(conn, day_range(date(2026, 10, 1), 8))                  # Oct 1-8 full
    slot = scheduler.next_slot(conn, cfg, pb, now=local(2026, 10, 1, 12, 0), rng=4)
    assert (slot["arm"], slot["when_local"]) == ("weekend-13:30", local(2026, 10, 9, 13, 30))


def test_fourteen_day_horizon(conn, cfg, pb):
    occupy(conn, day_range(date(2026, 10, 1), 15))                 # Oct 1-15; horizon ends Oct 15 12:00
    with pytest.raises(scheduler.NoSlotAvailable):
        scheduler.next_slot(conn, cfg, pb, now=local(2026, 10, 1, 12, 0), rng=1)


# ---------------------------------------------------------------- schedule_reel and due_reels
def test_schedule_reel_transitions_and_merges_features(conn, cfg, pb):
    rid = add_reel(conn, "qa_passed", duration_s=50.0,
                   plan_json=db.jdump({"hook_type": "problem", "output_duration": 50.0}),
                   caption_json=db.jdump({"caption": "x", "hashtags": ["#a", "#b", "#c"], "cta_type": "save"}))
    out = scheduler.schedule_reel(conn, cfg, rid, now=local(2026, 10, 5, 12, 0), rng=7)
    row = db.get_reel(conn, rid)
    assert row["state"] == "scheduled" and row["slot_arm"] == out["arm"]
    assert row["scheduled_at"] == out["scheduled_at"] and row["scheduled_at"].endswith("+00:00")
    feats = db.jload(row["features_json"])
    assert feats["slot"] == out["arm"] and feats["hour"] == out["when_local"].hour
    assert feats["weekday"] == out["when_local"].strftime("%a") and feats["hook_type"] == "problem"
    assert feats["duration_band"] == "46-60"
    dec = conn.execute("SELECT * FROM decisions WHERE reel_id=?", (rid,)).fetchone()
    assert dec["dimension"] == "slot" and dec["arm"] == out["arm"]
    with pytest.raises(db.TransitionError):
        scheduler.schedule_reel(conn, cfg, rid, now=local(2026, 10, 5, 12, 0), rng=7)


def test_scheduler_explores_at_least_25pct_of_first_30_posts(conn, cfg, pb):
    for seed in range(4):
        c = db.connect(":memory:")
        c.execute("DELETE FROM reels")
        learner.load_playbook(c, cfg)
        rng = learner.as_rng(seed)
        explores = []
        for day in range(30):
            rid = add_reel(c, "qa_passed")
            out = scheduler.schedule_reel(c, cfg, rid, now=local(2026, 10, 1) + timedelta(days=day), rng=rng)
            c.execute("UPDATE reels SET state='published', published_at=scheduled_at WHERE id=?", (rid,))
            explores.append(out["explore"])
        assert sum(explores) >= 8, (seed, sum(explores))
        assert all(explores[:8])                                   # every slot arm is tried once first
        c.close()


def test_due_reels(conn, cfg):
    now = local(2026, 10, 5, 22, 0)
    late = add_reel(conn, "scheduled", when=now - timedelta(hours=1))
    early = add_reel(conn, "scheduled", when=now - timedelta(days=1))
    add_reel(conn, "scheduled", when=now + timedelta(minutes=5))
    add_reel(conn, "scheduled", when=now - timedelta(hours=2), is_demo=1)
    add_reel(conn, "published", when=now - timedelta(hours=3))
    assert [r["id"] for r in scheduler.due_reels(conn, now=now)] == [early, late]
    assert scheduler.due_reels(conn, now=now - timedelta(days=2)) == []
