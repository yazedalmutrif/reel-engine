"""Unit tests for reelengine.learner: features, composite, rewards, bandits, reviews, versions."""
from __future__ import annotations

import itertools
import json
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import jinja2
import pytest

from reelengine import db, learner
from reelengine.config import load_config

RIYADH = ZoneInfo("Asia/Riyadh")
T0 = datetime(2026, 10, 4, 21, 30, tzinfo=RIYADH)  # a Sunday (weekday) 21:30
_seq = itertools.count()


# ---------------------------------------------------------------- fixtures and helpers
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


def add_reel(conn, *, state="published", hook="problem", duration=50.0, slot="weekday-21:30", when=None,
             dry_run=0, is_demo=0, hashtags=3, cta="save", style="short", **extra) -> int:
    when = when or T0
    iso = when.astimezone(timezone.utc).isoformat(timespec="seconds")
    fields = dict(
        state=state, duration_s=duration, slot_arm=slot, dry_run=dry_run, is_demo=is_demo,
        plan_json=db.jdump({"hook_type": hook, "output_duration": duration}),
        caption_json=db.jdump({"caption": "نص", "hashtags": [f"#t{i}" for i in range(hashtags)],
                               "cta_type": cta, "caption_style": style}),
        scheduled_at=iso, published_at=iso if state == "published" else None,
    )
    fields.update(extra)
    return db.insert_reel(conn, f"sha{next(_seq)}", "clip.mp4", "raw/clip.mp4", **fields)


def add_insights(conn, reel_id, metrics, checkpoint=72, fetched_at="2026-10-01T00:00:00+00:00"):
    conn.execute(
        "INSERT INTO insights(reel_id, checkpoint_h, due_at, fetched_at, metrics_json) VALUES (?,?,?,?,?)",
        (reel_id, checkpoint, "2026-10-01T00:00:00+00:00", fetched_at, db.jdump(metrics) if metrics is not None else None),
    )


def quality_metrics(q: float) -> dict:
    """Metrics that all improve with q (skip rate in percent, as the API is configured)."""
    return {"reach": 1000, "views": 1200, "likes": 10 * q, "shares": q, "saved": 2 * q,
            "ig_reels_avg_watch_time": 5000 * q, "reels_skip_rate": 60 - 5 * q}


def arm_row(conn, dim, arm):
    return conn.execute("SELECT * FROM bandit_arms WHERE dimension=? AND arm=?", (dim, arm)).fetchone()


def set_arm(conn, dim, arm, n, wins=None):
    wins = n if wins is None else wins
    conn.execute("UPDATE bandit_arms SET n=?, wins=?, alpha=1+?, beta=1+? WHERE dimension=? AND arm=?",
                 (n, wins, wins, n - wins, dim, arm))


def later(days: float) -> datetime:
    return T0 + timedelta(days=days)


NOW = datetime(2027, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------- playbook seeding
def test_load_playbook_seeds_once(conn, cfg, tmp_path):
    pb = learner.load_playbook(conn, cfg)
    assert pb["version"] == 1 and learner.current_version(conn) == 1
    assert (tmp_path / "playbooks" / "v1.json").exists() and (tmp_path / "playbooks" / "v1.md").exists()
    assert conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 8
    assert conn.execute("SELECT COUNT(*) FROM bandit_arms").fetchone()[0] == 8 + 6 + 4
    fav = arm_row(conn, "duration_band", "46-60")
    assert (fav["alpha"], fav["beta"], fav["n"]) == (2, 1, 0)
    again = learner.load_playbook(conn, cfg)
    assert again == pb
    assert conn.execute("SELECT COUNT(*) FROM playbooks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM bandit_arms").fetchone()[0] == 18


# ---------------------------------------------------------------- features
@pytest.mark.parametrize("seconds, band", [
    (12, None), (19.4, None), (19.6, "20-30"), (20, "20-30"), (30.4, "20-30"), (30.6, "31-45"),
    (45.5, "46-60"), (60, "46-60"), (61, "61-90"), (90.4, "61-90"), (90.6, None), (None, None), ("x", None),
])
def test_duration_band_edges(pb, seconds, band):
    assert learner.duration_band(seconds, pb) == band


@pytest.mark.parametrize("local, arm", [
    (datetime(2026, 10, 4, 21, 30), "weekday-21:30"),   # Sunday
    (datetime(2026, 10, 1, 18, 30), "weekday-18:30"),   # Thursday
    (datetime(2026, 10, 2, 13, 30), "weekend-13:30"),   # Friday
    (datetime(2026, 10, 3, 22, 30), "weekend-22:30"),   # Saturday
    (datetime(2026, 10, 4, 21, 31), None),
    (datetime(2026, 10, 4, 12, 0), None),
])
def test_slot_arm_for(pb, local, arm):
    assert learner.slot_arm_for(local.replace(tzinfo=RIYADH), pb) == arm


def test_extract_features_full_row(pb, cfg):
    row = {
        "duration_s": 50.0, "scheduled_at": "2026-10-04T18:30:00+00:00",  # 21:30 Riyadh, Sunday
        "plan_json": db.jdump({"hook_type": "Mistake", "pillar": "ai_tools", "topic": "cv", "cuts": [1, 2, 3, 4, 5],
                               "output_duration": 50.0, "output_word_count": 125}),
        "caption_json": db.jdump({"caption": "abcdef", "hashtags": ["#a", "#b", "#c"], "cta_type": "Save",
                                  "caption_style": "short"}),
    }
    f = learner.extract_features(row, pb, cfg)
    assert f["hook_type"] == "problem"          # brand label mapped to the arm id
    assert f["duration_band"] == "46-60" and f["duration_s"] == 50.0
    assert f["wpm"] == 150.0 and f["cuts_per_10s"] == 1.0
    assert f["caption_len"] == 6 and f["hashtag_count"] == 3 and f["cta_type"] == "save"
    assert (f["slot"], f["weekday"], f["hour"]) == ("weekday-21:30", "Sun", 21)
    assert f["cover_style"] == "frame+title"
    assert set(f) == {"pillar", "topic", "hook_type", "duration_band", "duration_s", "wpm", "cuts_per_10s",
                      "caption_len", "hashtags", "hashtag_count", "cta_type", "caption_style", "slot",
                      "weekday", "hour", "cover_style"}


def test_extract_features_missing_inputs_never_crash(pb, cfg):
    f = learner.extract_features({"plan_json": "not json", "caption_json": None}, pb, cfg)
    assert f["cover_style"] == "frame+title"
    assert all(v is None for k, v in f.items() if k != "cover_style")


# ---------------------------------------------------------------- composite
W = {"skip_rate": 0.25, "watch_ratio": 0.25, "shares_per_reach": 0.20, "saves_per_reach": 0.12,
     "likes_per_reach": 0.10, "views_per_follower": 0.08}


def test_composite_renormalises_over_present_metrics():
    values = {"skip_rate": 0.4, "watch_ratio": 0.6, "shares_per_reach": None, "views_per_follower": None}
    medians = {"skip_rate": 0.5, "watch_ratio": 0.5, "shares_per_reach": 0.01, "saves_per_reach": 0.02}
    expected = (0.25 * math.log(0.5 / 0.4) + 0.25 * math.log(0.6 / 0.5)) / 0.5
    assert learner.composite_score(values, medians, W) == pytest.approx(expected)
    assert learner.composite_score({"skip_rate": None}, medians, W) is None


def test_skip_rate_is_inverted():
    better = learner.composite_score({"skip_rate": 0.30}, {"skip_rate": 0.50}, W)
    worse = learner.composite_score({"skip_rate": 0.70}, {"skip_rate": 0.50}, W)
    assert better > 0 > worse


def test_composite_clips_and_guards_zero():
    assert learner.composite_score({"shares_per_reach": 0.0}, {"shares_per_reach": 0.01}, W) == pytest.approx(-2.0)
    assert learner.composite_score({"shares_per_reach": 0.05}, {"shares_per_reach": 0.0}, W) == pytest.approx(2.0)
    assert learner.composite_score({"shares_per_reach": 0.0}, {"shares_per_reach": 0.0}, W) == pytest.approx(0.0)


def test_skip_rate_percent_handling(cfg):
    # percent mode (the default, VERIFY): ALWAYS divide by 100, so a real 0.45 % isn't read as 45 % (fact-check #13)
    assert learner.normalise_skip_rate(45, cfg) == pytest.approx(0.45)
    assert learner.normalise_skip_rate(0.45, cfg) == pytest.approx(0.0045)
    assert learner.normalise_skip_rate(None, cfg) is None
    frac = cfg.with_overrides({"instagram": {"skip_rate_is_percent": False}})
    assert learner.normalise_skip_rate(0.45, frac) == pytest.approx(0.45)
    assert learner.normalise_skip_rate(45, frac) == pytest.approx(0.45)  # safety net, with a warning


def test_derived_metrics_ratios_followers_and_nulls(conn, cfg, pb):
    for day, fc in (("2026-10-03", 200), ("2026-10-05", 300)):
        conn.execute("INSERT INTO account_insights(day, fetched_at, follower_count) VALUES (?,?,?)", (day, day, fc))
    rid = add_reel(conn)  # published 2026-10-04 local -> nearest day <= is 2026-10-03
    row = db.get_reel(conn, rid)
    m = {"reach": 500, "views": 1000, "likes": 25, "shares": 5, "saved": 0,
         "ig_reels_avg_watch_time": 25000, "reels_skip_rate": 40}
    d = learner.derived_metrics(conn, cfg, row, m)
    assert d["views_per_follower"] == pytest.approx(5.0)
    assert d["watch_ratio"] == pytest.approx(0.5)
    assert d["shares_per_reach"] == pytest.approx(0.01) and d["saves_per_reach"] == 0.0
    assert d["likes_per_reach"] == pytest.approx(0.05) and d["skip_rate"] == pytest.approx(0.40)
    empty = learner.derived_metrics(conn, cfg, row, {"reach": 0, "views": None})
    assert all(empty[k] is None for k in learner.METRICS)
    conn.execute("UPDATE account_insights SET follower_count=NULL WHERE day='2026-10-03'")
    assert learner.derived_metrics(conn, cfg, row, m)["views_per_follower"] is None


# ---------------------------------------------------------------- rewards
def seed_reels(conn, qualities, start=0, **kw):
    ids = []
    for i, q in enumerate(qualities):
        rid = add_reel(conn, when=later(start + i), **kw)
        add_insights(conn, rid, quality_metrics(q))
        ids.append(rid)
    return ids


def test_no_rewards_before_baseline_then_retroactive(conn, cfg, pb):
    ids = seed_reels(conn, [1, 2, 3, 4])
    assert learner.compute_rewards(conn, cfg, now=NOW) == []
    assert arm_row(conn, "slot", "weekday-21:30")["n"] == 0
    ids += seed_reels(conn, [5], start=4)
    assert sorted(learner.compute_rewards(conn, cfg, now=NOW)) == ids
    scores = {r["id"]: r["composite_score"] for r in conn.execute("SELECT id, composite_score FROM reels")}
    assert scores[ids[2]] == pytest.approx(0.0)          # the median reel ties its own baseline
    assert scores[ids[0]] < 0 < scores[ids[4]]
    slot = arm_row(conn, "slot", "weekday-21:30")
    assert (slot["n"], slot["wins"], slot["alpha"], slot["beta"]) == (5, 2, 3, 4)   # q=4,5 win
    band = arm_row(conn, "duration_band", "46-60")
    assert (band["alpha"], band["beta"]) == (2 + 2, 1 + 3)                           # prior kept
    hook = arm_row(conn, "hook_type", "problem")
    assert (hook["n"], hook["wins"]) == (5, 2)


def test_rolling_median_uses_previous_reels_only(conn, cfg, pb):
    seed_reels(conn, [1, 2, 3, 4, 5])
    learner.compute_rewards(conn, cfg, now=NOW)
    six, seven = seed_reels(conn, [3, 10], start=5)
    assert sorted(learner.compute_rewards(conn, cfg, now=NOW)) == [six, seven]
    assert db.get_reel(conn, six)["composite_score"] == pytest.approx(0.0)
    assert db.get_reel(conn, seven)["composite_score"] > 0


def test_compute_rewards_is_idempotent(conn, cfg, pb):
    seed_reels(conn, [1, 2, 3, 4, 5, 6])
    first = learner.compute_rewards(conn, cfg, now=NOW)
    arms_before = [tuple(r) for r in conn.execute("SELECT * FROM bandit_arms ORDER BY dimension, arm")]
    assert len(first) == 6
    assert learner.compute_rewards(conn, cfg, now=NOW) == []
    assert learner.compute_rewards(conn, cfg, now=NOW + timedelta(days=30)) == []
    assert [tuple(r) for r in conn.execute("SELECT * FROM bandit_arms ORDER BY dimension, arm")] == arms_before
    assert conn.execute("SELECT COUNT(*) FROM reels WHERE rewarded_at IS NULL").fetchone()[0] == 0


def test_rewards_ignore_dry_run_demo_unfetched_and_other_checkpoints(conn, cfg, pb):
    seed_reels(conn, [1, 2, 3, 4])
    for kw in ({"dry_run": 1}, {"is_demo": 1}):
        rid = add_reel(conn, when=later(10), **kw)
        add_insights(conn, rid, quality_metrics(3))
    rid = add_reel(conn, when=later(11))
    add_insights(conn, rid, quality_metrics(3), fetched_at=None)
    rid = add_reel(conn, when=later(12))
    add_insights(conn, rid, quality_metrics(3), checkpoint=24)
    rid = add_reel(conn, when=later(13), state="scheduled")
    add_insights(conn, rid, quality_metrics(3))
    rid = add_reel(conn, when=later(14))
    add_insights(conn, rid, quality_metrics(3), fetched_at="2027-06-01T00:00:00+00:00")  # after `now`
    assert learner.compute_rewards(conn, cfg, now=NOW) == []


def test_all_null_metrics_are_not_scored(conn, cfg, pb):
    seed_reels(conn, [1, 2, 3, 4])
    rid = add_reel(conn, when=later(5))
    add_insights(conn, rid, {"reach": None, "views": None})
    assert learner.compute_rewards(conn, cfg, now=NOW) == []


# ---------------------------------------------------------------- bandit maths
def test_beta_interval_matches_closed_forms():
    lo, hi = learner.beta_interval(1, 1)
    assert (lo, hi) == (pytest.approx(0.05, abs=1e-9), pytest.approx(0.95, abs=1e-9))
    lo, hi = learner.beta_interval(2, 1)                 # CDF x^2
    assert (lo, hi) == (pytest.approx(math.sqrt(0.05), abs=1e-9), pytest.approx(math.sqrt(0.95), abs=1e-9))
    assert learner.beta_ppf(0.5, 5, 5) == pytest.approx(0.5, abs=1e-9)
    assert learner.beta_cdf(0.3, 3, 7) + learner.beta_cdf(0.7, 7, 3) == pytest.approx(1.0, abs=1e-12)


def test_arm_stats_shape_and_verdicts(conn, cfg, pb):
    seed_reels(conn, [1, 2, 3, 4, 5, 6])
    learner.compute_rewards(conn, cfg, now=NOW)
    set_arm(conn, "hook_type", "story", 40, 38)
    set_arm(conn, "hook_type", "question", 40, 10)
    stats = learner.arm_stats(conn, cfg, pb)
    assert set(stats) == set(learner.ARM_DIMENSIONS + learner.LOGGED_FACTORS)
    for row in stats["slot"] + stats["hook_type"] + stats["duration_band"]:
        assert row["ci_low"] <= row["mean"] <= row["ci_high"]
        assert {"arm", "n", "wins", "alpha", "beta", "p_best", "mean_composite", "effect_vs_rest", "verdict"} <= set(row)
    hooks = {r["arm"]: r for r in stats["hook_type"]}
    assert hooks["story"]["verdict"] == "better" and hooks["question"]["verdict"] == "worse"
    assert hooks["list_number"]["verdict"] == "too_few"
    assert sum(r["p_best"] for r in stats["hook_type"]) == pytest.approx(1.0, abs=1e-3)
    tags = {r["arm"]: r for r in stats["hashtag_count"]}
    assert tags["3"]["n"] == 6 and tags["5"]["n"] == 0 and "alpha" not in tags["3"]
    assert learner.arm_stats(conn, cfg, pb) == stats                  # deterministic


# ---------------------------------------------------------------- explore rule
def run_hook_posts(conn, cfg, pb, seed, posts=30):
    """Choose a hook for `posts` reels in a row (each becomes a real post) and return explore flags."""
    rng = learner.as_rng(seed)
    flags = []
    for i in range(posts):
        rid = add_reel(conn, state="qa_passed", when=later(i))
        pick = learner.choose_arm(conn, cfg, pb, "hook_type", rng=rng, reel_id=rid)
        conn.execute("UPDATE reels SET state='published', plan_json=? WHERE id=?",
                     (db.jdump({"hook_type": pick["arm"], "output_duration": 50}), rid))
        flags.append(pick["explore"])
    return flags


@pytest.mark.parametrize("seed", range(8))
def test_explore_share_first_30_posts_at_least_25pct(conn, cfg, pb, seed):
    flags = run_hook_posts(conn, cfg, pb, seed)
    assert sum(flags) / 30 >= 0.25
    assert all(flags[:6])                                   # every hook is tried once first
    logged = conn.execute("SELECT SUM(explore), COUNT(*) FROM decisions WHERE dimension='hook_type'").fetchone()
    assert (logged[0], logged[1]) == (sum(flags), 30)
    after = run_hook_posts(conn, cfg, pb, seed + 100, posts=10)  # posts 31-40: window closed
    assert not any(after)


def test_explore_picks_least_tried(conn, cfg, pb):
    always = cfg.with_overrides({"learning": {"explore_min": 1.0}})
    for arm in learner._arm_ids(pb, "hook_type"):
        set_arm(conn, "hook_type", arm, 1 if arm == "story" else 5)
    for seed in range(5):
        pick = learner.choose_arm(conn, always, pb, "hook_type", rng=seed, log=False)
        assert pick["arm"] == "story" and pick["explore"] is True
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_rank_arms_orders_thompson_draws(conn, cfg, pb):
    set_arm(conn, "slot", "weekday-21:30", 200, 200)
    ranked = learner.rank_arms(conn, cfg, pb, "slot", rng=1)
    assert len(ranked) == 8 and ranked[0][0] == "weekday-21:30"
    assert [s for _, s in ranked] == sorted((s for _, s in ranked), reverse=True)


def test_recommend_brief_covers_every_arm_first(conn, cfg, pb):
    brief = learner.recommend_brief(conn, cfg, pb, 6, rng=3)
    assert len(brief) == 6
    assert {b["hook_type"] for b in brief} == set(learner._arm_ids(pb, "hook_type"))
    assert {b["duration_band"] for b in brief[:4]} == set(learner._arm_ids(pb, "duration_band"))
    assert all(b["explore"] for b in brief) and brief[0]["experiment"] == "E1"
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE reel_id IS NULL").fetchone()[0] == 12


def test_recommend_brief_skips_retired_arms(conn, cfg, pb):
    pb = json.loads(json.dumps(pb))
    for arm in pb["arms"]["hook_type"]:
        arm["retired"] = arm["id"] in ("story", "question")
    brief = learner.recommend_brief(conn, cfg, pb, 12, rng=5)
    assert not {b["hook_type"] for b in brief} & {"story", "question"}


def test_trial_reels_eligibility(conn, cfg):
    assert learner.trial_reels_eligible(conn, cfg) == (False, None)
    conn.execute("INSERT INTO account_insights(day, fetched_at, follower_count) VALUES ('2026-10-01','x',999)")
    assert learner.trial_reels_eligible(conn, cfg) == (False, 999)
    conn.execute("INSERT INTO account_insights(day, fetched_at, follower_count) VALUES ('2026-10-02','x',1000)")
    conn.execute("INSERT INTO account_insights(day, fetched_at, follower_count) VALUES ('2026-10-03','x',NULL)")
    assert learner.trial_reels_eligible(conn, cfg) == (True, 1000)


# ---------------------------------------------------------------- weekly review
def change(field, after, n=5, dim=None, arm=None, confidence="medium"):
    return {"field": field, "after": after, "evidence": {"n": n, "effect_size": 0.2, "metric": "composite",
            "dimension": dim, "arm": arm}, "confidence": confidence, "rationale": "test"}


def review(*changes, findings=(), experiments=()):
    return {"summary": "Test review.", "findings": list(findings), "changes": list(changes),
            "next_experiments": list(experiments)}


def statuses(pb_new):
    return [(c["field"], c["status"]) for c in pb_new["changes"]]


def test_apply_review_rejects_locked_rule_edits(conn, cfg, pb):
    new = learner.apply_review(conn, cfg, pb, review(
        change("rules.R4.text", "Use up to 10 hashtags."),
        change("rules.R7.evidence", "FOLKLORE"),
        change("rules.R1.text", "Hook in the first 2 seconds, captions burned in."),
    ))
    assert statuses(new) == [("rules.R4.text", "rejected"), ("rules.R7.evidence", "rejected"), ("rules.R1.text", "applied")]
    assert "locked" in new["changes"][0]["reason"]
    old_locked = [r for r in pb["rules"] if r["locked"]]
    assert all(r in new["rules"] for r in old_locked)


def test_apply_review_enforces_hard_limits(conn, cfg, pb):
    conn.execute("INSERT INTO account_insights(day, fetched_at, follower_count) VALUES ('2026-10-01','x',500)")
    new = learner.apply_review(conn, cfg, pb, review(
        change("defaults.hashtag_count", 6, dim="hashtag_count", arm="6"),
        change("defaults.cta_type", "service", dim="cta_type", arm="service"),
        change("defaults.target_band", "91-120", dim="duration_band", arm="46-60"),
        change("arms.duration_band.61-90.max_s", 120),
        change("rules.new", {"text": "Film 120 s deep dives."}),
        change("rules.new", {"text": "Use 8 hashtags per reel."}),
        change("trial_reels.enabled", True),
        change("cta.service_enabled", True),
    ))
    assert all(s == "rejected" for _, s in statuses(new)), statuses(new)
    reasons = " | ".join(c["reason"] for c in new["changes"])
    for fragment in ("hashtag_count must be", "service CTA", "not a duration band", "schema", "120 s",
                     "8 hashtags", "Trial Reels need", "cta.service_enabled"):
        assert fragment in reasons, fragment
    assert new["defaults"] == pb["defaults"] and new["trial_reels"] == pb["trial_reels"]
    assert new["arms"] == pb["arms"]


def test_apply_review_min_n_gate(conn, cfg, pb):
    set_arm(conn, "slot", "weekday-21:30", 2)
    set_arm(conn, "slot", "weekday-18:30", 3)
    new = learner.apply_review(conn, cfg, pb, review(
        change("arms.slot.weekday-21:30.preferred", True, n=9, dim="slot", arm="weekday-21:30"),
        change("arms.slot.weekday-18:30.preferred", True, n=3, dim="slot", arm="weekday-18:30"),
        change("rules.R1.text", "Hook fast.", dim="hook_type", arm="story"),   # cites an arm with n=0
        change("defaults.cta_type", "send", dim="cta_type", arm="send"),       # logged factor, n=0
        findings=[{"dimension": "slot", "arm": "weekday-21:30", "claim": "best slot", "n": 2, "effect_size": 0.4,
                   "metric": "composite", "confidence": "high"}],
    ))
    assert statuses(new) == [("arms.slot.weekday-21:30.preferred", "rejected"), ("arms.slot.weekday-18:30.preferred", "applied"),
                             ("rules.R1.text", "rejected"), ("defaults.cta_type", "rejected")]
    assert "n=2 < min_n_per_arm=3" in new["changes"][0]["reason"]
    assert new["changes"][0]["evidence"]["n_actual"] == 2
    assert learner._find(new["arms"]["slot"], "weekday-18:30")["preferred"] is True
    assert new["findings"][0]["status"] == "rejected"


def test_trial_reels_allowed_once_eligible(conn, cfg, pb):
    conn.execute("INSERT INTO account_insights(day, fetched_at, follower_count) VALUES ('2026-10-01','x',1200)")
    new = learner.apply_review(conn, cfg, pb, review(change("trial_reels.enabled", True)))
    assert statuses(new) == [("trial_reels.enabled", "applied")] and new["trial_reels"]["enabled"] is True


def test_apply_review_versions_files_diff_and_experiments(conn, cfg, pb, tmp_path):
    set_arm(conn, "hook_type", "problem", 6, 5)
    new = learner.apply_review(conn, cfg, pb, review(
        change("experiments.E1.status", "concluded", n=6, dim="hook_type", arm="problem"),
        change("hypotheses.H1.status", "concluded", n=6, dim="hook_type", arm="problem"),
        change("caption_style.emoji_max", 2),
        experiments=[{"hypothesis": "Face in the first frame beats text-only first frames.", "dimension": "format",
                      "metric": "skip_rate", "why": "cover test"}],
    ))
    assert new["version"] == 2 and new["parent_version"] == 1 and learner.current_version(conn) == 2
    assert all(s == "applied" for _, s in statuses(new))
    assert (tmp_path / "playbooks" / "v2.json").exists()
    md = (tmp_path / "playbooks" / "v2.md").read_text(encoding="utf-8")
    assert "Reels Playbook v2" in md and "experiments.E1.status" in md
    row = conn.execute("SELECT * FROM playbooks WHERE version=2").fetchone()
    assert [c["status"] for c in json.loads(row["changes_json"])] == ["applied"] * 4
    assert learner.load_playbook(conn, cfg) == new
    exp = conn.execute("SELECT * FROM experiments WHERE id='E1'").fetchone()
    assert exp["status"] == "concluded" and exp["ended_at"] is not None
    assert conn.execute("SELECT status FROM experiments WHERE id='E9'").fetchone()["status"] == "next"
    paths = {d["path"] for d in learner.playbook_diff(pb, new)}
    assert {"experiments.E1.status", "hypotheses.H1.status", "caption_style.emoji_max", "experiments.E9", "hypotheses.H9"} <= paths
    assert not paths & {"version", "changes", "created_at"}
    with pytest.raises(learner.ReviewError):                  # stale base playbook
        learner.apply_review(conn, cfg, pb, review())


def test_apply_review_rejects_malformed_document(conn, cfg, pb):
    with pytest.raises(learner.ReviewError):
        learner.apply_review(conn, cfg, pb, {"changes": []})
    assert learner.current_version(conn) == 1


def test_playbook_diff_paths(pb):
    new = json.loads(json.dumps(pb))
    new["rules"][0]["text"] = "changed"
    new["rules"].append({"id": "R16", "text": "new", "evidence": "OURS", "locked": False})
    learner._find(new["arms"]["slot"], "weekday-21:30")["preferred"] = True
    new["defaults"]["cta_type"] = "send"
    new["version"] = 2
    diff = {d["path"]: d for d in learner.playbook_diff(pb, new)}
    assert set(diff) == {"rules.R1.text", "rules.R16", "arms.slot.weekday-21:30.preferred", "defaults.cta_type"}
    assert diff["rules.R16"]["before"] is None and diff["defaults.cta_type"]["after"] == "send"


def test_invariants_catch_out_of_range_bands(pb, cfg):
    bad = json.loads(json.dumps(pb))
    learner._find(bad["arms"]["duration_band"], "61-90")["max_s"] = 120
    problems = learner._invariant_problems(bad, pb, cfg, trial_ok=False)
    assert any("outside 5-90" in p for p in problems)
    assert learner._invariant_problems(pb, pb, cfg, trial_ok=False) == []


def test_review_inputs_serialise_and_render_prompt(conn, cfg, pb):
    seed_reels(conn, [1, 2, 3, 4, 5, 6])
    learner.compute_rewards(conn, cfg, now=NOW)
    inputs = learner.review_inputs(conn, cfg, pb)
    text = json.dumps(inputs, ensure_ascii=False)
    assert inputs["counts"]["rewarded"] == 6 and len(inputs["recent_reels"]) == 6
    assert inputs["limits"]["locked_rule_ids"] == ["R4", "R6", "R7", "R8", "R10", "R12", "R13"]
    assert "service" not in inputs["limits"]["allowed_cta"]
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
    template = env.from_string((cfg.prompts_dir / "learner.md").read_text(encoding="utf-8"))
    prompt = template.render(inputs=inputs, inputs_json=text)
    assert "playbook v2" in prompt and "R4, R6" in prompt
