"""Self-learning core: features, composite reward, Thompson-sampling bandits, playbook versions.

Spec §5 (metrics and learning), §6 (the scheduler asks us for slot order) and §2.8
(the learner can't rewrite the rules).

How a published reel becomes a lesson
-------------------------------------
1. `extract_features` turns plan/caption/schedule data into `features_json`.
2. Once the reward-checkpoint insights (72 h) are fetched, `compute_rewards` turns raw metrics
   into ratios (`derived_metrics`), compares each ratio with the rolling median of earlier
   reels (log-ratio, clipped to ±2, skip rate inverted), combines them with the config weights
   (re-normalised over the metrics that exist) into a composite score, and counts
   composite > 0 as a *win* for the reel's slot, hook type and duration band
   (Beta-Bernoulli bandit on "beat the median").
3. `choose_arm` / `rank_arms` / `arm_order` / `recommend_brief` use Thompson sampling, with at
   least `explore_min` exploration during the first `explore_until_posts` posts
   (coverage first, then a random draw, then a catch-up quota that guarantees the share).
4. The weekly brain review reads `review_inputs`; its answer goes through `apply_review`,
   which validates every proposed change (schema, locked rules, hard limits, min n per arm)
   and writes playbook version N+1 (DB row + data/playbooks/vN.json + vN.md).

Nothing here touches config, gates or secrets.
"""
from __future__ import annotations

import copy
import json
import math
import random
import re
import sqlite3
import statistics
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import jsonschema
import numpy as np

from . import db
from .config import HARD_HASHTAG_CAP, HARD_MAX_DURATION_S, HARD_MIN_DURATION_S, KNOWN_CTAS, Config
from .log import get_logger

_log = get_logger("learner")

ARM_DIMENSIONS = ("slot", "hook_type", "duration_band")
LOGGED_FACTORS = ("cta_type", "hashtag_count", "caption_style")
METRICS = ("skip_rate", "watch_ratio", "shares_per_reach", "saves_per_reach", "likes_per_reach", "views_per_follower")
LOWER_IS_BETTER = frozenset({"skip_rate"})

EPS = 1e-4                 # floor for ratios before taking logs (0 shares -> 0.0001 of reach)
LOG_RATIO_CLIP = 2.0       # one metric can move the composite by at most ±2 (x7.4)
CI_LEVEL = 0.90            # equal-tailed Beta interval shown on the dashboard
CLAIM_ALPHA = 0.05         # family-wise false-claim budget per dimension (verdict better/worse)
P_BEST_DRAWS = 4000        # Monte Carlo draws for P(arm is best); fixed seed = deterministic
P_BEST_SEED = 20260929
COVERAGE_MIN_TRIES = 1     # during the explore window every arm is tried at least once first

POSTED_STATES = ("scheduled", "publishing", "published")
WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DEFAULT_WEEKEND_DAYS = ("Fri", "Sat")
STATUSES = ("running", "logging", "concluded", "next", "parked", "dropped")
LEARNER_EVIDENCE_TAGS = ("OURS", "FOLKLORE")   # the learner may never tag a rule OFFICIAL/OBSERVED
ARM_FLAGS = ("preferred", "retired")
MIN_HASHTAGS = 3
META_KEYS = ("version", "created_at", "parent_version", "source", "summary", "changes", "findings")
MAX_RULES = 30
MAX_NEW_EXPERIMENTS = 3
EXPERIMENT_DIMENSIONS = ARM_DIMENSIONS + LOGGED_FACTORS + ("format",)
EDITABLE_FIELDS = (
    "rules.<id>.text (unlocked rules only)",
    "rules.<id>.evidence (OURS | FOLKLORE, unlocked rules only)",
    "rules.new (after = {text}; tagged OURS, never locked)",
    "hypotheses.<id>.status",
    "experiments.<id>.status",
    "experiments.<id>.result",
    "arms.<dimension>.<arm>.preferred | retired (bool; arms themselves are fixed)",
    "defaults.target_band | cta_type | hashtag_count | caption_style",
    "topics (whole list of {id, label, purpose})",
    "caption_style.first_line_max_chars | keywords | tone | emoji_max",
    "trial_reels.enabled (true only when eligible)",
)


class ReviewError(ValueError):
    """The brain's review can't be applied at all (wrong shape or stale playbook)."""


# ============================================================ small helpers
def as_rng(rng: Any = None) -> np.random.Generator:
    """Accept None, a seed, a numpy Generator or a random.Random and return a numpy Generator."""
    if isinstance(rng, np.random.Generator):
        return rng
    if rng is None:
        return np.random.default_rng()
    if isinstance(rng, random.Random):
        return np.random.default_rng(rng.getrandbits(64))
    return np.random.default_rng(rng)


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Join the caller's transaction if there is one, else open an IMMEDIATE one."""
    if conn.in_transaction:
        yield conn
    else:
        with db.tx(conn):
            yield conn


def _get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, sqlite3.Row):
        return row[key] if key in row.keys() else default
    if isinstance(row, Mapping):
        return row.get(key, default)
    return default


def json_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        out = db.jload(value, {})
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return db.parse_ts(value)
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _local(value: Any, cfg: Config) -> datetime | None:
    dt = _utc(value)
    return dt.astimezone(cfg.tz) if dt else None


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(float(x), nd)


def _arms(playbook: Mapping, dimension: str) -> list[dict]:
    return [a for a in (playbook.get("arms") or {}).get(dimension, []) if isinstance(a, dict) and "id" in a]


def _arm_ids(playbook: Mapping, dimension: str) -> list[str]:
    return [str(a["id"]) for a in _arms(playbook, dimension)]


def _flagged(playbook: Mapping, dimension: str, flag: str) -> set[str]:
    return {str(a["id"]) for a in _arms(playbook, dimension) if a.get(flag)}


def _prior(playbook: Mapping, dimension: str, arm: str) -> tuple[float, float]:
    p = ((playbook.get("priors") or {}).get(dimension) or {}).get(arm) or {}
    return float(p.get("alpha", 1.0)), float(p.get("beta", 1.0))


# ============================================================ playbook storage
def current_version(conn: sqlite3.Connection) -> int:
    """Latest playbook version in the DB (0 when none is stored yet)."""
    row = conn.execute("SELECT MAX(version) AS v FROM playbooks").fetchone()
    return int(row["v"] or 0)


def _latest_json(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT json FROM playbooks ORDER BY version DESC LIMIT 1").fetchone()
    return row["json"] if row else None


def load_playbook(conn: sqlite3.Connection, cfg: Config) -> dict:
    """Return the latest playbook; seed version 1 from brand/playbook_v1.json the first time.

    Seeding writes the `playbooks` row, `experiments` rows, `bandit_arms` with priors and
    data/playbooks/v1.json + v1.md. Idempotent (a second call just reads)."""
    raw = _latest_json(conn)
    if raw is not None:
        return db.jload(raw)
    seed = json.loads((cfg.brand_dir / "playbook_v1.json").read_text(encoding="utf-8"))
    seed["version"] = 1
    json_path = cfg.dir("playbooks") / "v1.json"
    inserted = False
    with _atomic(conn):
        if _latest_json(conn) is None:
            conn.execute(
                "INSERT INTO playbooks(version, created_at, json, changes_json, summary, path) VALUES (1, ?, ?, ?, ?, ?)",
                (db.now_iso(), db.jdump(seed), "[]", seed.get("summary"), str(json_path)),
            )
            _sync_experiments(conn, seed)
            _ensure_arms(conn, seed)
            inserted = True
    if inserted:
        _write_playbook_files(cfg, seed, [])
        _log.info("seeded playbook v1 from %s", cfg.brand_dir / "playbook_v1.json")
    return db.jload(_latest_json(conn))


def _ensure_arms(conn: sqlite3.Connection, playbook: Mapping) -> None:
    ts = db.now_iso()
    for dim in ARM_DIMENSIONS:
        for arm in _arm_ids(playbook, dim):
            a, b = _prior(playbook, dim, arm)
            conn.execute(
                "INSERT OR IGNORE INTO bandit_arms(dimension, arm, alpha, beta, n, wins, updated_at) VALUES (?,?,?,?,0,0,?)",
                (dim, arm, a, b, ts),
            )


def _sync_experiments(conn: sqlite3.Connection, playbook: Mapping) -> None:
    """Upsert the `experiments` table from the playbook (start/end dates kept once set)."""
    ts = db.now_iso()
    hyps = {h.get("id"): h for h in playbook.get("hypotheses", []) if isinstance(h, dict)}
    for exp in playbook.get("experiments", []):
        if not isinstance(exp, dict) or not exp.get("id"):
            continue
        hyp = hyps.get(exp.get("hypothesis")) or {}
        dim = exp.get("dimension")
        arms = _arm_ids(playbook, dim) if dim in ARM_DIMENSIONS else (playbook.get("logged_factors") or {}).get(dim)
        status = exp.get("status") or "next"
        conn.execute(
            """INSERT INTO experiments(id, hypothesis, dimension, arms_json, status, started_at, ended_at, result)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 hypothesis=excluded.hypothesis, dimension=excluded.dimension, arms_json=excluded.arms_json,
                 status=excluded.status,
                 started_at=COALESCE(experiments.started_at, excluded.started_at),
                 ended_at=CASE WHEN excluded.status IN ('concluded','dropped')
                               THEN COALESCE(experiments.ended_at, excluded.ended_at) ELSE NULL END,
                 result=COALESCE(excluded.result, experiments.result)""",
            (
                exp["id"],
                hyp.get("text") or str(exp.get("hypothesis") or exp["id"]),
                dim,
                db.jdump(arms) if arms is not None else None,
                status,
                ts if status in ("running", "logging") else None,
                ts if status in ("concluded", "dropped") else None,
                exp.get("result"),
            ),
        )


def _write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _write_playbook_files(cfg: Config, playbook: Mapping, diff: list[dict]) -> Path:
    folder = cfg.dir("playbooks")
    v = playbook["version"]
    json_path = folder / f"v{v}.json"
    try:
        _write_text_atomic(json_path, json.dumps(playbook, ensure_ascii=False, indent=2))
        _write_text_atomic(folder / f"v{v}.md", playbook_markdown(playbook, diff))
    except OSError as exc:  # the DB row is the source of truth; files are copies
        _log.error("could not write playbook v%s files: %s", v, exc)
    return json_path


# ============================================================ features
def duration_band(duration_s: Any, playbook: Mapping) -> str | None:
    """Band id for a duration (edges inclusive, fractional seconds rounded half up; None outside)."""
    d = _num(duration_s)
    if d is None or d <= 0:
        return None
    rounded = math.floor(d + 0.5)
    for band in _arms(playbook, "duration_band"):
        lo, hi = _num(band.get("min_s")), _num(band.get("max_s"))
        if lo is not None and hi is not None and lo <= rounded <= hi:
            return str(band["id"])
    return None


def day_type_of(local_date: date, playbook: Mapping) -> str:
    """'weekend' when the local weekday is in playbook.weekend_days (Fri, Sat), else 'weekday'."""
    weekend = playbook.get("weekend_days") or DEFAULT_WEEKEND_DAYS
    return "weekend" if WEEKDAY_ABBR[local_date.weekday()] in weekend else "weekday"


def slot_arm_for(local_dt: datetime | None, playbook: Mapping) -> str | None:
    """Slot arm id (e.g. 'weekday-21:30') when a local datetime matches a slot to the minute."""
    if local_dt is None:
        return None
    kind = day_type_of(local_dt, playbook)
    hhmm = f"{local_dt.hour:02d}:{local_dt.minute:02d}"
    for arm in _arms(playbook, "slot"):
        if arm.get("day_type") == kind and arm.get("time") == hhmm:
            return str(arm["id"])
    return None


def normalise_hook_type(value: Any, playbook: Mapping) -> str | None:
    """Map a hook label from the edit plan ('Mistake', 'bold claim', 'problem') to an arm id.
    Unknown labels are returned unchanged (they are logged but never rewarded)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    def key(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")

    wanted = key(text)
    for arm in _arms(playbook, "hook_type"):
        labels = [arm["id"], arm.get("label", "")] + list(arm.get("brand_types") or [])
        if any(key(str(lbl)) == wanted for lbl in labels if lbl):
            return str(arm["id"])
    return text


def extract_features(reel_row: Any, playbook: Mapping, cfg: Config) -> dict:
    """Features of one reel (shape = features_json in db.py). Missing inputs give None fields."""
    plan = json_dict(_get(reel_row, "plan_json"))
    cap = json_dict(_get(reel_row, "caption_json"))
    duration = _num(_get(reel_row, "duration_s"))
    if duration is None or duration <= 0:
        duration = _num(plan.get("output_duration"))
    has_duration = duration is not None and duration > 0
    words = _num(plan.get("output_word_count"))
    cuts_raw = plan.get("cuts")
    cuts = float(len(cuts_raw)) if isinstance(cuts_raw, (list, tuple)) else _num(cuts_raw)
    tags_raw = cap.get("hashtags")
    hashtags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else None
    caption = cap.get("caption")
    local = _local(_get(reel_row, "scheduled_at"), cfg)
    slot = _get(reel_row, "slot_arm") or (slot_arm_for(local, playbook) if local else None)
    cta = cap.get("cta_type")
    return {
        "pillar": plan.get("pillar"),
        "topic": plan.get("topic"),
        "hook_type": normalise_hook_type(plan.get("hook_type"), playbook),
        "duration_band": duration_band(duration, playbook) if has_duration else None,
        "duration_s": round(duration, 2) if has_duration else None,
        "wpm": round(words / duration * 60.0, 1) if words is not None and has_duration else None,
        "cuts_per_10s": round(cuts / duration * 10.0, 2) if cuts is not None and has_duration else None,
        "caption_len": len(caption) if isinstance(caption, str) else None,
        "hashtags": hashtags,
        "hashtag_count": len(hashtags) if hashtags is not None else None,
        "cta_type": str(cta).lower() if cta else None,
        "caption_style": cap.get("caption_style"),
        "slot": slot,
        "weekday": WEEKDAY_ABBR[local.weekday()] if local else None,
        "hour": local.hour if local else None,
        "cover_style": plan.get("cover_style") or "frame+title",
    }


def _reel_factors(row: Any, playbook: Mapping, cfg: Config) -> dict[str, Any]:
    """The arm / logged-factor values of a reel. Stored features (captured at scheduling) win."""
    merged = extract_features(row, playbook, cfg)
    merged.update({k: v for k, v in json_dict(_get(row, "features_json")).items() if v is not None})
    out = {dim: merged.get(dim) for dim in ARM_DIMENSIONS + LOGGED_FACTORS}
    out["slot"] = _get(row, "slot_arm") or merged.get("slot")
    return out


# ============================================================ metrics and composite
def normalise_skip_rate(value: Any, cfg: Config) -> float | None:
    """reels_skip_rate as a fraction 0..1.

    skip_rate_is_percent = true (VERIFY in API_NOTES): the API sends percent, so ALWAYS divide
    by 100 (a real 0.8 % skip rate must not be read as 80 %). If it's false, values are
    fractions; a value > 1 then still gets treated as percent, with a warning."""
    x = _num(value)
    if x is None or x < 0:
        return None
    if cfg["instagram"]["skip_rate_is_percent"]:
        x /= 100.0
    elif x > 1.0:
        _log.warning("reels_skip_rate %.3f > 1 although skip_rate_is_percent=false; treating it as percent", x)
        x /= 100.0
    return min(x, 1.0)


def _derived(cfg: Config, reel_row: Any, metrics: Mapping | None, follower_count: int | None) -> dict:
    m = metrics or {}
    reach = _num(m.get("reach"))
    duration = _num(_get(reel_row, "duration_s"))
    if duration is None or duration <= 0:
        duration = _num(json_dict(_get(reel_row, "plan_json")).get("output_duration"))
    avg_ms = _num(m.get("ig_reels_avg_watch_time"))
    views = _num(m.get("views"))
    fc = int(follower_count) if follower_count is not None and follower_count > 0 else None

    def per_reach(key: str) -> float | None:
        v = _num(m.get(key))
        return v / reach if v is not None and v >= 0 and reach is not None and reach > 0 else None

    return {
        "skip_rate": normalise_skip_rate(m.get("reels_skip_rate"), cfg),
        "watch_ratio": avg_ms / 1000.0 / duration if avg_ms is not None and avg_ms >= 0 and duration and duration > 0 else None,
        "shares_per_reach": per_reach("shares"),
        "saves_per_reach": per_reach("saved"),
        "likes_per_reach": per_reach("likes"),
        "views_per_follower": views / fc if views is not None and views >= 0 and fc else None,
        "follower_count": fc,
    }


def _publish_day(reel_row: Any, cfg: Config) -> str | None:
    local = _local(_get(reel_row, "published_at") or _get(reel_row, "scheduled_at"), cfg)
    return local.date().isoformat() if local else None


class _FollowerIndex:
    """follower_count of the nearest account_insights day <= a given day (None when unknown)."""

    def __init__(self, conn: sqlite3.Connection):
        rows = conn.execute("SELECT day, follower_count FROM account_insights ORDER BY day").fetchall()
        self.days = [r["day"] for r in rows]
        self.counts = [r["follower_count"] for r in rows]

    def on(self, day: str | None) -> int | None:
        if day is None:
            return None
        i = bisect_right(self.days, day) - 1
        if i < 0 or self.counts[i] is None:
            return None
        return int(self.counts[i])


def derived_metrics(conn: sqlite3.Connection, cfg: Config, reel_row: Any, metrics: dict) -> dict:
    """Per-reel ratios used by the composite (None where unavailable, never 0-for-missing).

    skip_rate (fraction), watch_ratio (avg watch s / duration s), shares/saves/likes per reach,
    views per follower (follower_count from account_insights, nearest day <= publish day)."""
    day = _publish_day(reel_row, cfg)
    fc = None
    if day is not None:
        row = conn.execute(
            "SELECT follower_count FROM account_insights WHERE day <= ? ORDER BY day DESC LIMIT 1", (day,)
        ).fetchone()
        fc = row["follower_count"] if row else None
    return _derived(cfg, reel_row, metrics, fc)


def composite_score(values: Mapping[str, float | None], medians: Mapping[str, float | None], weights: Mapping[str, float]) -> float | None:
    """Weighted mean of clipped log-ratios vs the medians, over the metrics present in both.

    Higher-is-better metrics use log(x / median); skip_rate uses log(median / x). Each term is
    clipped to ±LOG_RATIO_CLIP; values are floored at EPS. None when no metric overlaps."""
    num = wsum = 0.0
    for key, w in weights.items():
        x, med = values.get(key), medians.get(key)
        if w <= 0 or x is None or med is None:
            continue
        x, med = max(float(x), EPS), max(float(med), EPS)
        lr = math.log(med / x) if key in LOWER_IS_BETTER else math.log(x / med)
        num += w * max(-LOG_RATIO_CLIP, min(LOG_RATIO_CLIP, lr))
        wsum += w
    return num / wsum if wsum > 0 else None


def _medians(rows: Sequence[Mapping[str, float | None]], keys: Sequence[str]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for key in keys:
        vals = [r[key] for r in rows if r.get(key) is not None]
        out[key] = float(statistics.median(vals)) if vals else None
    return out


# ============================================================ rewards
_REWARD_ROWS_SQL = """
SELECT r.*, i.metrics_json AS _metrics_json, i.fetched_at AS _fetched_at
FROM reels r JOIN insights i ON i.reel_id = r.id AND i.checkpoint_h = ? AND i.fetched_at IS NOT NULL
WHERE r.state = 'published' AND r.dry_run = 0 AND r.is_demo = 0
ORDER BY COALESCE(r.published_at, r.scheduled_at, r.created_at), r.id
"""


def _reward_rows(conn: sqlite3.Connection, cfg: Config, now: datetime) -> list[sqlite3.Row]:
    cp = float(cfg["learning"]["reward_checkpoint_h"])
    rows = conn.execute(_REWARD_ROWS_SQL, (cp,)).fetchall()
    return [r for r in rows if (_utc(r["_fetched_at"]) or now) <= now]


def compute_rewards(conn: sqlite3.Connection, cfg: Config, now: datetime | None = None) -> list[int]:
    """Reward every reel whose reward-checkpoint insights are in (once). Returns the new reel ids.

    Baseline: nothing is rewarded until `baseline_posts` reels are scorable; then those are
    scored against their own median. Later reels are scored against the median of the previous
    `rolling_window` scorable reels (by publish time)."""
    now_dt = _utc(now) or db.utcnow()
    rows = _reward_rows(conn, cfg, now_dt)
    if not any(r["rewarded_at"] is None for r in rows):
        return []
    learn = cfg["learning"]
    weights = {k: float(v) for k, v in learn["weights"].items()}
    playbook = load_playbook(conn, cfg)
    followers = _FollowerIndex(conn)
    cache: dict[int, dict] = {}

    def derived(row: sqlite3.Row) -> dict:
        if row["id"] not in cache:
            metrics = json_dict(row["_metrics_json"])
            cache[row["id"]] = _derived(cfg, row, metrics, followers.on(_publish_day(row, cfg)))
        return cache[row["id"]]

    scorable = [r for r in rows if any(derived(r).get(k) is not None for k, w in weights.items() if w > 0)]
    base_n, window = int(learn["baseline_posts"]), int(learn["rolling_window"])
    if len(scorable) < base_n:
        return []
    newly: list[int] = []
    for j, row in enumerate(scorable):
        if row["rewarded_at"] is not None:
            continue
        ref = scorable[:base_n] if j < base_n else scorable[max(0, j - window): j]
        score = composite_score(derived(row), _medians([derived(r) for r in ref], list(weights)), weights)
        if score is None:
            _log.info("reel %s: no metric overlaps its reference median yet; reward postponed", row["id"])
            continue
        if _reward_one(conn, cfg, playbook, row, score, now_dt):
            newly.append(int(row["id"]))
    if newly:
        _log.info("rewarded %d reel(s): %s", len(newly), newly)
    return newly


def _reward_one(conn: sqlite3.Connection, cfg: Config, playbook: Mapping, row: sqlite3.Row, score: float, now: datetime) -> bool:
    """Store the composite and update the reel's three arms, atomically and only once."""
    win = 1 if score > 0 else 0
    ts = _iso(now)
    factors = _reel_factors(row, playbook, cfg)
    with _atomic(conn):
        cur = conn.execute(
            "UPDATE reels SET rewarded_at=?, composite_score=?, features_json=COALESCE(features_json, ?), updated_at=? "
            "WHERE id=? AND rewarded_at IS NULL",
            (ts, float(score), db.jdump(extract_features(row, playbook, cfg)), db.now_iso(), row["id"]),
        )
        if cur.rowcount != 1:
            return False
        for dim in ARM_DIMENSIONS:
            arm = factors.get(dim)
            if arm is None or str(arm) not in _arm_ids(playbook, dim):
                _log.info("reel %s: %s=%r is not an arm; not rewarded in that dimension", row["id"], dim, arm)
                continue
            _bump_arm(conn, playbook, dim, str(arm), win, ts)
    return True


def _bump_arm(conn: sqlite3.Connection, playbook: Mapping, dim: str, arm: str, win: int, ts: str) -> None:
    a, b = _prior(playbook, dim, arm)
    conn.execute(
        "INSERT OR IGNORE INTO bandit_arms(dimension, arm, alpha, beta, n, wins, updated_at) VALUES (?,?,?,?,0,0,?)",
        (dim, arm, a, b, ts),
    )
    conn.execute(
        "UPDATE bandit_arms SET alpha=alpha+?, beta=beta+?, n=n+1, wins=wins+?, updated_at=? WHERE dimension=? AND arm=?",
        (win, 1 - win, win, ts, dim, arm),
    )


# ============================================================ bandits
def _check_dim(dimension: str) -> None:
    if dimension not in ARM_DIMENSIONS:
        raise ValueError(f"unknown bandit dimension {dimension!r} (expected one of {ARM_DIMENSIONS})")


def _arm_params(conn: sqlite3.Connection, playbook: Mapping, dimension: str) -> dict[str, dict]:
    """alpha, beta, n, wins per arm in playbook order (missing rows = prior, n=0)."""
    rows = {r["arm"]: r for r in conn.execute("SELECT * FROM bandit_arms WHERE dimension=?", (dimension,))}
    out: dict[str, dict] = {}
    for arm in _arm_ids(playbook, dimension):
        r = rows.get(arm)
        if r is not None:
            out[arm] = {"alpha": float(r["alpha"]), "beta": float(r["beta"]), "n": int(r["n"]), "wins": int(r["wins"])}
        else:
            a, b = _prior(playbook, dimension, arm)
            out[arm] = {"alpha": a, "beta": b, "n": 0, "wins": 0}
    return out


def _thompson(params: Mapping[str, dict], arms: Sequence[str], rng: np.random.Generator) -> dict[str, float]:
    return {arm: float(rng.beta(params[arm]["alpha"], params[arm]["beta"])) for arm in arms}


def _posts_so_far(conn: sqlite3.Connection, exclude_reel_id: int | None = None) -> int:
    """Real posts (scheduled/publishing/published, not demo, not dry-run) that count for the explore window."""
    q = "SELECT COUNT(*) AS n FROM reels WHERE state IN ('scheduled','publishing','published') AND is_demo=0 AND dry_run=0"
    args: tuple = ()
    if exclude_reel_id is not None:
        q += " AND id != ?"
        args = (exclude_reel_id,)
    return int(conn.execute(q, args).fetchone()["n"])


def _pending_counts(conn: sqlite3.Connection, cfg: Config, playbook: Mapping, dimension: str) -> dict[str, int]:
    """Posted-but-not-yet-rewarded reels per arm (so exploration doesn't pile onto one arm)."""
    rows = conn.execute(
        "SELECT * FROM reels WHERE state IN ('scheduled','publishing','published') AND is_demo=0 AND dry_run=0 "
        "AND rewarded_at IS NULL"
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        arm = _reel_factors(row, playbook, cfg).get(dimension)
        if arm is not None:
            counts[str(arm)] = counts.get(str(arm), 0) + 1
    return counts


def _window_counts(conn: sqlite3.Connection, dimension: str, exclude_reel_id: int | None) -> tuple[int, int]:
    """(decisions, explores) made inside the explore window for a dimension (latest per reel)."""
    latest: dict[tuple, int] = {}
    for r in conn.execute("SELECT id, reel_id, explore, detail_json FROM decisions WHERE dimension=? ORDER BY id", (dimension,)):
        if exclude_reel_id is not None and r["reel_id"] == exclude_reel_id:
            continue
        if not json_dict(r["detail_json"]).get("window"):
            continue
        key = ("reel", r["reel_id"]) if r["reel_id"] is not None else ("id", r["id"])
        latest[key] = int(r["explore"])
    return len(latest), sum(latest.values())


def _explore_state(
    conn: sqlite3.Connection, cfg: Config, dimension: str, tries: Mapping[str, int], rng: np.random.Generator, reel_id: int | None
) -> tuple[bool, str | None, bool]:
    """(explore?, reason, inside the explore window?).

    Inside the window (fewer than explore_until_posts real posts): explore when an arm is still
    untried ("coverage"), with probability explore_min ("random"), or when the explore share of
    earlier window decisions would fall below explore_min ("quota": guarantees the share)."""
    learn = cfg["learning"]
    draw = float(rng.random())  # always drawn, so a seeded run stays reproducible
    if _posts_so_far(conn, reel_id) >= int(learn["explore_until_posts"]):
        return False, None, False
    if any(t < COVERAGE_MIN_TRIES for t in tries.values()):
        return True, "coverage", True
    explore_min = float(learn["explore_min"])
    if draw < explore_min:
        return True, "random", True
    decided, explored = _window_counts(conn, dimension, reel_id)
    if explored < math.ceil(explore_min * (decided + 1) - 1e-9):
        return True, "quota", True
    return False, None, True


def arm_order(
    conn: sqlite3.Connection,
    cfg: Config,
    playbook: Mapping,
    dimension: str,
    rng: Any = None,
    reel_id: int | None = None,
    exclude: Sequence[str] = (),
    extra_tries: Mapping[str, int] | None = None,
) -> dict:
    """The order in which to try arms, with the explore rule applied (no logging).

    Explore: least-tried first (tries = rewarded n + pending posts + extra_tries), ties random.
    Exploit: Thompson order. Returns {order, explore, reason, window, samples, tries}."""
    _check_dim(dimension)
    gen = as_rng(rng)
    params = _arm_params(conn, playbook, dimension)
    skip = {str(a) for a in exclude}
    cands = [a for a in params if a not in skip]
    if not cands:
        raise ValueError(f"no {dimension} arms left to choose from")
    samples = _thompson(params, cands, gen)
    pending = _pending_counts(conn, cfg, playbook, dimension)
    extra = extra_tries or {}
    tries = {a: params[a]["n"] + pending.get(a, 0) + int(extra.get(a, 0)) for a in cands}
    explore, reason, window = _explore_state(conn, cfg, dimension, tries, gen, reel_id)
    if explore:
        ties = gen.random(len(cands))
        order = [a for _, _, a in sorted((tries[a], float(ties[i]), a) for i, a in enumerate(cands))]
    else:
        order = sorted(cands, key=lambda a: -samples[a])
    return {"order": order, "explore": explore, "reason": reason, "window": window, "samples": samples, "tries": tries}


def log_decision(
    conn: sqlite3.Connection, reel_id: int | None, dimension: str, arm: str, explore: bool, detail: Mapping | None = None
) -> None:
    """Append a row to `decisions` (explore=1 for exploration picks)."""
    conn.execute(
        "INSERT INTO decisions(ts, reel_id, dimension, arm, explore, detail_json) VALUES (?,?,?,?,?,?)",
        (db.now_iso(), reel_id, dimension, arm, int(bool(explore)), db.jdump(dict(detail or {}))),
    )


def decision_detail(plan: Mapping, source: str, **extra: Any) -> dict:
    detail = {
        "source": source,
        "window": plan["window"],
        "reason": plan["reason"],
        "samples": {a: round(s, 4) for a, s in plan["samples"].items()},
        "tries": dict(plan["tries"]),
    }
    detail.update(extra)
    return detail


def choose_arm(
    conn: sqlite3.Connection,
    cfg: Config,
    playbook: Mapping,
    dimension: str,
    rng: Any = None,
    reel_id: int | None = None,
    exclude: Sequence[str] = (),
    log: bool = True,
) -> dict:
    """Pick one arm (Thompson, or exploration inside the window) and log it in `decisions`."""
    plan = arm_order(conn, cfg, playbook, dimension, rng=rng, reel_id=reel_id, exclude=exclude)
    arm = plan["order"][0]
    if log:
        log_decision(conn, reel_id, dimension, arm, plan["explore"], decision_detail(plan, "choose_arm"))
    return {"arm": arm, "explore": plan["explore"], "samples": plan["samples"]}


def rank_arms(conn: sqlite3.Connection, cfg: Config, playbook: Mapping, dimension: str, rng: Any = None) -> list[tuple[str, float]]:
    """All arms of a dimension ordered by one Thompson draw each, best first (no explore, no log)."""
    _check_dim(dimension)
    params = _arm_params(conn, playbook, dimension)
    samples = _thompson(params, list(params), as_rng(rng))
    return sorted(samples.items(), key=lambda kv: -kv[1])


# ============================================================ statistics
def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        de = d * c
        h *= de
        if abs(de - 1.0) < 3e-15:
            break
    return h


def beta_cdf(x: float, a: float, b: float) -> float:
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    front = math.exp(ln_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def beta_ppf(q: float, a: float, b: float) -> float:
    """Beta quantile by bisection on beta_cdf (deterministic, |error| < 1e-12)."""
    lo, hi = 0.0, 1.0
    for _ in range(50):
        mid = (lo + hi) / 2.0
        if beta_cdf(mid, a, b) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def beta_interval(a: float, b: float, level: float = CI_LEVEL) -> tuple[float, float]:
    """Equal-tailed credible interval of Beta(a, b)."""
    tail = (1.0 - level) / 2.0
    return beta_ppf(tail, a, b), beta_ppf(1.0 - tail, a, b)


def _p_best(params: Mapping[str, dict]) -> dict[str, float]:
    arms = list(params)
    if not arms:
        return {}
    gen = np.random.default_rng(P_BEST_SEED)
    draws = np.column_stack([gen.beta(params[a]["alpha"], params[a]["beta"], P_BEST_DRAWS) for a in arms])
    counts = np.bincount(draws.argmax(axis=1), minlength=len(arms))
    return {a: float(counts[i]) / P_BEST_DRAWS for i, a in enumerate(arms)}


def verdict_level(n_arms: int) -> float:
    """Posterior probability an arm needs to be called better (or 1 - it: worse) than the rest:
    CLAIM_ALPHA per dimension split over its arms (Bonferroni), e.g. 99.375% for the 8 slot arms,
    so a dimension with more arms doesn't produce more false wins."""
    return 1.0 - CLAIM_ALPHA / max(1, n_arms)


def _verdict(p: Mapping, rest_wins: int, rest_losses: int, min_n: int, n_arms: int = 1) -> str:
    """'better' / 'worse' only when n >= min_n and the posterior overlap with the pooled rest of
    the dimension (Beta(1+wins, 1+losses)) is small: P(arm > rest) >= verdict_level(n_arms)
    (or <= 1 - level). Otherwise 'unclear', or 'too_few' below min_n."""
    if p["n"] < min_n:
        return "too_few"
    level = verdict_level(n_arms)
    p_sup = p_superior(p["alpha"], p["beta"], 1.0 + rest_wins, 1.0 + rest_losses)
    if p_sup >= level:
        return "better"
    if p_sup <= 1.0 - level:
        return "worse"
    return "unclear"


def p_superior(a1: float, b1: float, a2: float, b2: float, draws: int = 20000) -> float:
    """P(theta1 > theta2) for independent Beta posteriors (Monte Carlo, fixed seed: deterministic)."""
    gen = np.random.default_rng(P_BEST_SEED)
    return float(np.mean(gen.beta(a1, b1, draws) > gen.beta(a2, b2, draws)))


def _rewarded(conn: sqlite3.Connection, cfg: Config, playbook: Mapping) -> list[tuple[dict, float]]:
    rows = conn.execute(
        "SELECT * FROM reels WHERE rewarded_at IS NOT NULL AND composite_score IS NOT NULL AND is_demo=0 AND dry_run=0 "
        "ORDER BY COALESCE(published_at, scheduled_at, created_at), id"
    ).fetchall()
    return [(_reel_factors(r, playbook, cfg), float(r["composite_score"])) for r in rows]


def _mean_and_effect(scores: Mapping[str, list[float]], key: str) -> tuple[float | None, float | None]:
    mine = scores.get(key) or []
    rest = [s for k, v in scores.items() if k != key for s in v]
    mean = statistics.fmean(mine) if mine else None
    effect = mean - statistics.fmean(rest) if mine and rest else None
    return mean, effect


def _factor_key(value: Any) -> str | None:
    return None if value is None else str(value)


def arm_stats(conn: sqlite3.Connection, cfg: Config, playbook: Mapping) -> dict[str, list[dict]]:
    """Per-dimension arm statistics for the dashboard and the weekly review.

    Bandit arms: n, wins, alpha, beta, mean (posterior), ci_low/ci_high (90% equal-tailed Beta
    interval), p_best, mean_composite, effect_vs_rest (mean composite minus the rest), verdict,
    preferred/retired flags. Logged factors: n, wins, win_rate, mean_composite, effect_vs_rest."""
    min_n = int(cfg["learning"]["min_n_per_arm"])
    rewarded = _rewarded(conn, cfg, playbook)
    out: dict[str, list[dict]] = {}
    for dim in ARM_DIMENSIONS:
        params = _arm_params(conn, playbook, dim)
        p_best = _p_best(params)
        scores: dict[str, list[float]] = {}
        for factors, score in rewarded:
            if factors.get(dim) is not None:
                scores.setdefault(str(factors[dim]), []).append(score)
        tot_n = sum(p["n"] for p in params.values())
        tot_w = sum(p["wins"] for p in params.values())
        flags = {str(a["id"]): a for a in _arms(playbook, dim)}
        rows = []
        for arm, p in params.items():
            lo, hi = beta_interval(p["alpha"], p["beta"])
            mean_c, effect = _mean_and_effect(scores, arm)
            rest_w = tot_w - p["wins"]
            rest_l = (tot_n - p["n"]) - rest_w
            rows.append({
                "arm": arm, "n": p["n"], "wins": p["wins"], "alpha": p["alpha"], "beta": p["beta"],
                "mean": _r(p["alpha"] / (p["alpha"] + p["beta"])), "ci_low": _r(lo), "ci_high": _r(hi),
                "p_best": _r(p_best.get(arm)), "mean_composite": _r(mean_c), "effect_vs_rest": _r(effect),
                "verdict": _verdict(p, rest_w, rest_l, min_n, len(params)),
                "preferred": bool(flags.get(arm, {}).get("preferred")), "retired": bool(flags.get(arm, {}).get("retired")),
            })
        out[dim] = rows
    for factor in LOGGED_FACTORS:
        out[factor] = _factor_stats(playbook, rewarded, factor, min_n)
    return out


def _factor_stats(playbook: Mapping, rewarded: list[tuple[dict, float]], factor: str, min_n: int) -> list[dict]:
    scores: dict[str, list[float]] = {}
    for factors, score in rewarded:
        key = _factor_key(factors.get(factor))
        if key is not None:
            scores.setdefault(key, []).append(score)
    values = [str(v) for v in (playbook.get("logged_factors") or {}).get(factor, [])]
    values += [k for k in scores if k not in values]
    rows = []
    for value in values:
        mine = scores.get(value, [])
        wins = sum(1 for s in mine if s > 0)
        mean_c, effect = _mean_and_effect(scores, value)
        rows.append({
            "arm": value, "n": len(mine), "wins": wins, "win_rate": _r(wins / len(mine)) if mine else None,
            "mean_composite": _r(mean_c), "effect_vs_rest": _r(effect), "claim_ok": len(mine) >= min_n,
        })
    return rows


def trial_reels_eligible(conn: sqlite3.Connection, cfg: Config) -> tuple[bool, int | None]:
    """(eligible, latest known follower_count). Trial Reels need >= trial_reels_min_followers (>= 1000)."""
    row = conn.execute(
        "SELECT follower_count FROM account_insights WHERE follower_count IS NOT NULL ORDER BY day DESC LIMIT 1"
    ).fetchone()
    fc = int(row["follower_count"]) if row else None
    need = max(int(cfg["learning"]["trial_reels_min_followers"]), 1000)
    return (fc is not None and fc >= need), fc


def _allowed_ctas(cfg: Config) -> list[str]:
    return [c for c in cfg["cta"]["allowed"] if c in KNOWN_CTAS and c != "service"]


def _hashtag_range(cfg: Config) -> tuple[int, int]:
    cap = min(HARD_HASHTAG_CAP, int(cfg["qa"]["hashtags_max"]))
    return min(MIN_HASHTAGS, cap), cap


def review_inputs(conn: sqlite3.Connection, cfg: Config, playbook: Mapping) -> dict:
    """Everything the weekly brain review needs, JSON-serialisable."""
    learn = cfg["learning"]
    stats = arm_stats(conn, cfg, playbook)
    eligible, followers = trial_reels_eligible(conn, cfg)
    index = _FollowerIndex(conn)
    cp = float(learn["reward_checkpoint_h"])
    rows = conn.execute(
        "SELECT r.*, i.metrics_json AS _metrics_json FROM reels r "
        "LEFT JOIN insights i ON i.reel_id = r.id AND i.checkpoint_h = ? "
        "WHERE r.rewarded_at IS NOT NULL AND r.composite_score IS NOT NULL AND r.is_demo=0 AND r.dry_run=0 "
        "ORDER BY COALESCE(r.published_at, r.scheduled_at, r.created_at) DESC, r.id DESC LIMIT 30",
        (cp,),
    ).fetchall()
    recent = []
    for r in rows:
        feats = extract_features(r, playbook, cfg)
        feats.update({k: v for k, v in json_dict(r["features_json"]).items() if v is not None})
        feats.pop("hashtags", None)
        metrics = _derived(cfg, r, json_dict(r["_metrics_json"]), index.on(_publish_day(r, cfg)))
        recent.append({
            "id": r["id"], "published_at": r["published_at"], "features": feats,
            "composite": _r(r["composite_score"]), "win": r["composite_score"] > 0,
            "metrics": {k: _r(v, 5) for k, v in metrics.items()},
        })
    counts_row = conn.execute(
        "SELECT SUM(state='published') AS published, SUM(rewarded_at IS NOT NULL) AS rewarded, "
        "SUM(state='published' AND rewarded_at IS NULL) AS awaiting FROM reels WHERE is_demo=0 AND dry_run=0"
    ).fetchone()
    lo, hi = _hashtag_range(cfg)
    return {
        "generated_at": db.now_iso(),
        "playbook_version": playbook.get("version"),
        "playbook": playbook,
        "arm_stats": {d: stats[d] for d in ARM_DIMENSIONS},
        "logged_factor_stats": {f: stats[f] for f in LOGGED_FACTORS},
        "recent_reels": recent,
        "min_n_per_arm": int(learn["min_n_per_arm"]),
        "follower_count": followers,
        "trial_reels": {"eligible": eligible, "follower_count": followers,
                        "min_followers": max(int(learn["trial_reels_min_followers"]), 1000)},
        "counts": {
            "posts": _posts_so_far(conn),
            "published": int(counts_row["published"] or 0),
            "rewarded": int(counts_row["rewarded"] or 0),
            "awaiting_reward": int(counts_row["awaiting"] or 0),
            "explore_until_posts": int(learn["explore_until_posts"]),
        },
        "composite": {
            "weights": dict(learn["weights"]), "reward_checkpoint_h": cp,
            "baseline_posts": int(learn["baseline_posts"]), "rolling_window": int(learn["rolling_window"]),
            "win": "composite > 0 (beats the rolling median)",
        },
        "limits": {
            "locked_rule_ids": [r["id"] for r in playbook.get("rules", []) if r.get("locked")],
            "hashtag_count_range": [lo, hi],
            "duration_range_s": [HARD_MIN_DURATION_S, HARD_MAX_DURATION_S],
            "allowed_cta": _allowed_ctas(cfg),
            "editable_fields": list(EDITABLE_FIELDS),
        },
    }


# ============================================================ brief
def _experiment_for(playbook: Mapping, dims: Sequence[str]) -> str | None:
    running = [e for e in playbook.get("experiments", []) if isinstance(e, dict) and e.get("status") == "running"]
    for dim in list(dims) + ["hook_type", "duration_band"]:
        for exp in running:
            if exp.get("dimension") == dim:
                return exp.get("id")
    return None


def recommend_brief(conn: sqlite3.Connection, cfg: Config, playbook: Mapping, k: int, rng: Any = None) -> list[dict]:
    """k (hook_type, duration_band) pairs for the weekly filming brief.

    Retired arms are skipped. Inside the explore window every arm is covered first, then the
    explore rule applies; picks already made in this brief count as tries, so a brief is
    balanced. Each pick is logged in `decisions` (reel_id NULL, source 'brief')."""
    gen = as_rng(rng)
    picks: dict[str, dict[str, int]] = {"hook_type": {}, "duration_band": {}}
    items: list[dict] = []
    for _ in range(max(0, int(k))):
        item: dict[str, Any] = {}
        explore_dims: list[str] = []
        for dim in ("hook_type", "duration_band"):
            retired = _flagged(playbook, dim, "retired")
            exclude = retired if len(retired) < len(_arm_ids(playbook, dim)) else set()
            plan = arm_order(conn, cfg, playbook, dim, rng=gen, exclude=sorted(exclude), extra_tries=picks[dim])
            arm = plan["order"][0]
            picks[dim][arm] = picks[dim].get(arm, 0) + 1
            log_decision(conn, None, dim, arm, plan["explore"], decision_detail(plan, "brief"))
            item[dim] = arm
            if plan["explore"]:
                explore_dims.append(dim)
        item["explore"] = bool(explore_dims)
        item["explore_dims"] = explore_dims
        item["experiment"] = _experiment_for(playbook, explore_dims)
        items.append(item)
    return items


# ============================================================ playbook diff + markdown
def _is_id_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, dict) and "id" in x for x in value)


def _diff(a: Any, b: Any, path: str, out: list[dict]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        keys = list(a) + [k for k in b if k not in a]
        for key in keys:
            _diff(a.get(key), b.get(key), f"{path}.{key}" if path else str(key), out)
    elif _is_id_list(a) and _is_id_list(b) and (a or b):
        amap = {str(x["id"]): x for x in a}
        bmap = {str(x["id"]): x for x in b}
        for key in list(amap) + [k for k in bmap if k not in amap]:
            _diff(amap.get(key), bmap.get(key), f"{path}.{key}", out)
    elif a != b:
        out.append({"path": path, "before": copy.deepcopy(a), "after": copy.deepcopy(b)})


def playbook_diff(old: dict, new: dict, ignore: Sequence[str] = ("version", "created_at", "parent_version", "source", "changes", "findings")) -> list[dict]:
    """Field-level differences [{path, before, after}]. Lists of {id: ...} objects are matched by
    id, so paths read like 'rules.R1.text' or 'arms.slot.weekday-21:30.preferred'."""
    skip = set(ignore)
    out: list[dict] = []
    _diff({k: v for k, v in (old or {}).items() if k not in skip}, {k: v for k, v in (new or {}).items() if k not in skip}, "", out)
    return out


def _cell(value: Any, limit: int = 90) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def playbook_markdown(playbook: Mapping, diff: Sequence[Mapping] | None = None) -> str:
    """Human-readable playbook version (changes, diff, defaults, rules, arms, experiments)."""
    v = playbook.get("version")
    lines = [f"# Reels Playbook v{v}", ""]
    meta = f"Created {playbook.get('created_at', '?')}"
    if playbook.get("parent_version"):
        meta += f" · from v{playbook['parent_version']}"
    lines += [meta, "", str(playbook.get("summary") or ""), ""]
    changes = playbook.get("changes") or []
    if changes:
        lines += ["## Changes proposed by the weekly review", "",
                  "| Status | Field | Before | After | Evidence (n, effect, metric) | Confidence | Reason |",
                  "|---|---|---|---|---|---|---|"]
        for c in changes:
            ev = c.get("evidence") or {}
            evs = f"n={ev.get('n')}, effect={ev.get('effect_size')}, {ev.get('metric')}"
            lines.append(f"| {c.get('status')} | `{_cell(c.get('field'))}` | {_cell(c.get('before'), 50)} | "
                         f"{_cell(c.get('after'), 50)} | {_cell(evs, 60)} | {c.get('confidence')} | {_cell(c.get('reason'), 120)} |")
        lines.append("")
    if diff:
        lines += [f"## Diff vs v{playbook.get('parent_version')}", ""]
        lines += [f"- `{d['path']}`: {_cell(d['before'], 80)} → {_cell(d['after'], 80)}" for d in diff]
        lines.append("")
    d = playbook.get("defaults") or {}
    lines += ["## Defaults", ""] + [f"- **{k}**: {_cell(val)}" for k, val in d.items()] + [""]
    lines += ["## Rules", "", "| # | Rule | Evidence | Locked |", "|---|---|---|---|"]
    for r in playbook.get("rules", []):
        lines.append(f"| {r.get('id')} | {_cell(r.get('text'), 200)} | {r.get('evidence')} | {'yes' if r.get('locked') else 'no'} |")
    lines += ["", "## Arms", ""]
    for dim in ARM_DIMENSIONS:
        parts = []
        for a in _arms(playbook, dim):
            tag = " (preferred)" if a.get("preferred") else " (retired)" if a.get("retired") else ""
            parts.append(f"`{a['id']}`{tag}")
        lines.append(f"- **{dim}**: " + ", ".join(parts))
    lines += ["", "## Hypotheses and experiments", "", "| Experiment | Hypothesis | Dimension | Status |", "|---|---|---|---|"]
    hyps = {h.get("id"): h for h in playbook.get("hypotheses", [])}
    for e in playbook.get("experiments", []):
        h = hyps.get(e.get("hypothesis"), {})
        lines.append(f"| {e.get('id')} | {_cell(h.get('text') or e.get('hypothesis'), 120)} | {e.get('dimension')} | {e.get('status')} |")
    lines += ["", "## Topics", ""] + [f"- `{t.get('id')}`: {t.get('label')} ({t.get('purpose')})" for t in playbook.get("topics", [])]
    return "\n".join(lines).rstrip() + "\n"


# ============================================================ weekly review
class _Reject(Exception):
    """A single proposed change is not allowed; the message is the recorded reason."""


@dataclass
class _Edit:
    before: Any
    after: Any
    subject: tuple[str, str] | None     # the arm / factor value the change is about
    needs_data: bool                    # a claim from data: needs n >= min_n evidence
    apply: Callable[[dict], None]


class _ReviewContext:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, playbook: Mapping):
        self.cfg = cfg
        self.min_n = int(cfg["learning"]["min_n_per_arm"])
        stats = arm_stats(conn, cfg, playbook)
        self.n = {dim: {row["arm"]: row["n"] for row in stats[dim]} for dim in ARM_DIMENSIONS + LOGGED_FACTORS}
        self.effect = {(dim, row["arm"]): row["effect_vs_rest"] for dim in stats for row in stats[dim]}
        self.trial_ok, self.followers = trial_reels_eligible(conn, cfg)

    def actual_n(self, dim: str, arm: Any) -> int | None:
        """Rewarded n behind an arm or logged-factor value (None = unknown arm)."""
        if dim in ARM_DIMENSIONS:
            return self.n[dim].get(str(arm))
        if dim in LOGGED_FACTORS:
            return self.n[dim].get(str(arm), 0)
        return None


def _find(items: list, item_id: str) -> dict | None:
    return next((x for x in items if isinstance(x, dict) and str(x.get("id")) == item_id), None)


_HASHTAG_NUM = re.compile(r"(\d+)\s*(?:hashtags?|هاشتاقات|هاشتاق|هاشتاغ|وسوم|وسم)", re.IGNORECASE)
_SECONDS_NUM = re.compile(r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds|ثانية|ثواني)\b", re.IGNORECASE)


def _rule_text_problem(text: Any, cfg: Config) -> str | None:
    if not isinstance(text, str) or not (3 <= len(text.strip()) <= 300):
        return "rule text must be a string of 3-300 characters"
    cap = _hashtag_range(cfg)[1]
    for m in _HASHTAG_NUM.finditer(text):
        if int(m.group(1)) > cap:
            return f"rule text asks for {m.group(1)} hashtags; the cap is {cap} (locked rule R4)"
    for m in _SECONDS_NUM.finditer(text):
        if float(m.group(1)) > HARD_MAX_DURATION_S:
            return f"rule text asks for {m.group(1)} s; reels stay inside {HARD_MIN_DURATION_S:g}-{HARD_MAX_DURATION_S:g} s"
    return None


def _next_id(items: list, prefix: str) -> str:
    nums = [int(m.group(1)) for x in items if isinstance(x, dict) for m in [re.fullmatch(prefix + r"(\d+)", str(x.get("id", "")))] if m]
    return f"{prefix}{max(nums, default=0) + 1}"


def _edit_rules(pb: dict, parts: list[str], after: Any, ctx: _ReviewContext) -> _Edit:
    rules = pb.setdefault("rules", [])
    if parts == ["new"]:
        text = after.get("text") if isinstance(after, dict) else after
        problem = _rule_text_problem(text, ctx.cfg)
        if problem:
            raise _Reject(problem)
        if len(rules) >= MAX_RULES:
            raise _Reject(f"the playbook already has {MAX_RULES} rules")
        rule = {"id": _next_id(rules, "R"), "text": text.strip(), "evidence": "OURS", "locked": False}
        return _Edit(None, rule, None, True, lambda p: p.setdefault("rules", []).append(copy.deepcopy(rule)))
    if len(parts) != 2 or parts[1] not in ("text", "evidence"):
        raise _Reject("only rules.<id>.text, rules.<id>.evidence and rules.new are editable")
    rule = _find(rules, parts[0])
    if rule is None:
        raise _Reject(f"unknown rule {parts[0]}")
    if rule.get("locked"):
        raise _Reject(f"rule {parts[0]} is locked (spec §2.8): the learner can't change it")
    key = parts[1]
    if key == "text":
        problem = _rule_text_problem(after, ctx.cfg)
        if problem:
            raise _Reject(problem)
        after = after.strip()
    elif after not in LEARNER_EVIDENCE_TAGS:
        raise _Reject(f"the learner may only tag evidence as {LEARNER_EVIDENCE_TAGS}")
    rid = parts[0]
    return _Edit(rule.get(key), after, None, key == "evidence" and after == "OURS",
                 lambda p: _find(p["rules"], rid).__setitem__(key, after))


def _edit_status_list(pb: dict, root: str, parts: list[str], after: Any) -> _Edit:
    items = pb.setdefault(root, [])
    allowed = ("status", "result") if root == "experiments" else ("status",)
    if len(parts) != 2 or parts[1] not in allowed:
        raise _Reject(f"only {root}.<id>.{'|'.join(allowed)} is editable (new ones go in next_experiments)")
    item = _find(items, parts[0])
    if item is None:
        raise _Reject(f"unknown {root[:-1]} {parts[0]}")
    key, item_id = parts[1], parts[0]
    if key == "status":
        if after not in STATUSES:
            raise _Reject(f"status must be one of {STATUSES}")
        needs = after == "concluded"
    else:
        if not isinstance(after, str) or not (1 <= len(after) <= 500):
            raise _Reject("result must be a string of 1-500 characters")
        needs = True
    return _Edit(item.get(key), after, None, needs, lambda p: _find(p[root], item_id).__setitem__(key, after))


def _edit_arms(pb: dict, parts: list[str], after: Any) -> _Edit:
    if len(parts) != 3 or parts[0] not in ARM_DIMENSIONS or parts[2] not in ARM_FLAGS:
        raise _Reject("arms are fixed in v1; only arms.<dimension>.<arm>.preferred|retired may change")
    dim, arm_id, flag = parts
    arm = _find(pb.get("arms", {}).get(dim, []), arm_id)
    if arm is None:
        raise _Reject(f"unknown {dim} arm {arm_id} (arms can't be added in v1)")
    if not isinstance(after, bool):
        raise _Reject(f"{flag} must be true or false")
    if flag == "retired" and after:
        active = [a for a in pb["arms"][dim] if not a.get("retired") and str(a["id"]) != arm_id]
        if not active:
            raise _Reject(f"can't retire the last active {dim} arm")
    other = "retired" if flag == "preferred" else "preferred"

    def apply(p: dict) -> None:
        target = _find(p["arms"][dim], arm_id)
        target[flag] = after
        if after and target.get(other):
            target[other] = False  # an arm can't be both preferred and retired

    return _Edit(bool(arm.get(flag)), after, (dim, arm_id), after, apply)


def _edit_defaults(pb: dict, parts: list[str], after: Any, ctx: _ReviewContext) -> _Edit:
    key = parts[0] if len(parts) == 1 else None
    defaults = pb.setdefault("defaults", {})
    if key == "target_band":
        band = _find(pb.get("arms", {}).get("duration_band", []), str(after))
        if band is None:
            raise _Reject(f"target_band {after!r} is not a duration band arm (bands are fixed, {HARD_MIN_DURATION_S:g}-{HARD_MAX_DURATION_S:g} s)")
        subject = ("duration_band", str(after))
    elif key == "cta_type":
        if after == "service":
            raise _Reject("the service CTA is not the learner's to switch on (cta.service_enabled, spec §2.7)")
        if after not in _allowed_ctas(ctx.cfg):
            raise _Reject(f"cta_type must be one of {_allowed_ctas(ctx.cfg)}")
        subject = ("cta_type", str(after))
    elif key == "hashtag_count":
        lo, hi = _hashtag_range(ctx.cfg)
        if not isinstance(after, int) or isinstance(after, bool) or not (lo <= after <= hi):
            raise _Reject(f"hashtag_count must be an integer {lo}..{hi} (hard cap {HARD_HASHTAG_CAP}, locked rule R4)")
        subject = ("hashtag_count", str(after))
    elif key == "caption_style":
        styles = [str(s) for s in (pb.get("logged_factors") or {}).get("caption_style", [])]
        if after not in styles:
            raise _Reject(f"caption_style must be one of {styles}")
        subject = ("caption_style", str(after))
    else:
        raise _Reject("only defaults.target_band|cta_type|hashtag_count|caption_style are editable")
    return _Edit(defaults.get(key), after, subject, True, lambda p: p.setdefault("defaults", {}).__setitem__(key, after))


def _edit_topics(pb: dict, parts: list[str], after: Any) -> _Edit:
    if parts:
        raise _Reject("topics are replaced as a whole list (field 'topics')")
    ok = isinstance(after, list) and 1 <= len(after) <= 8 and all(
        isinstance(t, dict) and set(t) <= {"id", "label", "purpose"}
        and isinstance(t.get("id"), str) and re.fullmatch(r"[a-z0-9_]{2,40}", t["id"])
        and isinstance(t.get("label"), str) and 1 <= len(t["label"]) <= 120
        and isinstance(t.get("purpose"), str) and 1 <= len(t["purpose"]) <= 40
        for t in after
    )
    if not ok or len({t["id"] for t in after}) != len(after):
        raise _Reject("topics must be 1-8 {id (a-z0-9_), label, purpose} objects with unique ids")
    return _Edit(pb.get("topics"), after, None, False, lambda p: p.__setitem__("topics", copy.deepcopy(after)))


_CAPTION_STYLE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "first_line_max_chars": lambda v: isinstance(v, int) and not isinstance(v, bool) and 40 <= v <= 125,
    "emoji_max": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 3,
    "keywords": lambda v: isinstance(v, str) and 1 <= len(v) <= 300,
    "tone": lambda v: isinstance(v, str) and 1 <= len(v) <= 300,
}


def _edit_caption_style(pb: dict, parts: list[str], after: Any) -> _Edit:
    key = parts[0] if len(parts) == 1 else ""
    check = _CAPTION_STYLE_CHECKS.get(key)
    if check is None:
        raise _Reject(f"caption_style.{key} is not editable (keys: {sorted(_CAPTION_STYLE_CHECKS)})")
    if not check(after):
        raise _Reject(f"caption_style.{key}: value {after!r} is out of range (first_line_max_chars 40-125, emoji_max 0-3)")
    return _Edit((pb.get("caption_style") or {}).get(key), after, None, False,
                 lambda p: p.setdefault("caption_style", {}).__setitem__(key, after))


def _edit_trial_reels(pb: dict, parts: list[str], after: Any, ctx: _ReviewContext) -> _Edit:
    if parts != ["enabled"] or not isinstance(after, bool):
        raise _Reject("only trial_reels.enabled (true/false) is editable")
    if after and not ctx.trial_ok:
        raise _Reject(f"Trial Reels need >= {max(int(ctx.cfg['learning']['trial_reels_min_followers']), 1000)} "
                      f"followers (latest follower_count: {ctx.followers}); locked rule R10")
    return _Edit((pb.get("trial_reels") or {}).get("enabled"), after, None, False,
                 lambda p: p.setdefault("trial_reels", {}).__setitem__("enabled", after))


def _plan_edit(pb: dict, field: str, after: Any, ctx: _ReviewContext) -> _Edit:
    root, *parts = field.split(".")
    if root == "rules":
        return _edit_rules(pb, parts, after, ctx)
    if root in ("hypotheses", "experiments"):
        return _edit_status_list(pb, root, parts, after)
    if root == "arms":
        return _edit_arms(pb, parts, after)
    if root == "defaults":
        return _edit_defaults(pb, parts, after, ctx)
    if root == "topics":
        return _edit_topics(pb, parts, after)
    if root == "caption_style":
        return _edit_caption_style(pb, parts, after)
    if root == "trial_reels":
        return _edit_trial_reels(pb, parts, after, ctx)
    raise _Reject(f"{field!r} is not editable by the learner (it may change: {', '.join(EDITABLE_FIELDS)})")


def _evidence_problem(change: Mapping, edit: _Edit, ctx: _ReviewContext) -> str | None:
    """Reject claims backed by fewer than min_n rewarded reels (checked against our own counts)."""
    ev = change.get("evidence") or {}
    cited: list[tuple[str, str]] = []
    if ev.get("dimension") and ev.get("arm") is not None:
        cited.append((str(ev["dimension"]), str(ev["arm"])))
    if edit.subject is not None and edit.subject not in cited:
        cited.append(edit.subject)
    for dim, arm in cited:
        n = ctx.actual_n(dim, arm)
        if n is None:
            return f"evidence cites unknown arm {dim}/{arm}"
        if n < ctx.min_n:
            return f"evidence cites {dim}/{arm} with n={n} < min_n_per_arm={ctx.min_n}"
    if edit.needs_data:
        claimed = ev.get("n")
        if not isinstance(claimed, int) or claimed < ctx.min_n:
            return f"evidence n={claimed} < min_n_per_arm={ctx.min_n}"
        if not cited:
            return "a data-backed change must cite the arm it rests on (evidence.dimension + evidence.arm)"
    return None


def _strip_flags(arms: Any) -> Any:
    if not isinstance(arms, dict):
        return arms
    return {dim: [{k: v for k, v in a.items() if k not in ARM_FLAGS} for a in lst] if isinstance(lst, list) else lst
            for dim, lst in arms.items()}


def _invariant_problems(new: Mapping, old: Mapping, cfg: Config, trial_ok: bool) -> list[str]:
    """Hard rules a playbook version must keep (defence in depth behind the per-field checks)."""
    problems: list[str] = []
    old_locked = {r["id"]: r for r in old.get("rules", []) if isinstance(r, dict) and r.get("locked")}
    new_rules = [r for r in new.get("rules", []) if isinstance(r, dict)]
    by_id = {r.get("id"): r for r in new_rules}
    for rid, rule in old_locked.items():
        if by_id.get(rid) != rule:
            problems.append(f"locked rule {rid} must stay identical")
    problems += [f"rule {r.get('id')} can't become locked" for r in new_rules if r.get("locked") and r.get("id") not in old_locked]
    if len(by_id) != len(new_rules):
        problems.append("rule ids must be unique")
    for key in ("priors", "weekend_days", "logged_factors"):
        if new.get(key) != old.get(key):
            problems.append(f"{key} is fixed")
    if _strip_flags(new.get("arms")) != _strip_flags(old.get("arms")):
        problems.append("arm sets are fixed in v1 (only preferred/retired flags may change)")
    for band in _arms(new, "duration_band"):
        lo, hi = _num(band.get("min_s")), _num(band.get("max_s"))
        if lo is None or hi is None or lo < HARD_MIN_DURATION_S or hi > HARD_MAX_DURATION_S:
            problems.append(f"duration band {band['id']} is outside {HARD_MIN_DURATION_S:g}-{HARD_MAX_DURATION_S:g} s")
    d = new.get("defaults") or {}
    lo, hi = _hashtag_range(cfg)
    hc = d.get("hashtag_count")
    if not isinstance(hc, int) or isinstance(hc, bool) or not (lo <= hc <= hi):
        problems.append(f"defaults.hashtag_count must be {lo}..{hi}")
    if d.get("cta_type") == "service" or d.get("cta_type") not in _allowed_ctas(cfg):
        problems.append("defaults.cta_type must be an allowed non-service CTA")
    if d.get("target_band") not in _arm_ids(new, "duration_band"):
        problems.append("defaults.target_band must be a duration band arm")
    if str(d.get("caption_style")) not in [str(s) for s in (new.get("logged_factors") or {}).get("caption_style", [])]:
        problems.append("defaults.caption_style must be a logged caption style")
    tr_new, tr_old = dict(new.get("trial_reels") or {}), dict(old.get("trial_reels") or {})
    if tr_new.get("enabled") and not trial_ok:
        problems.append("trial_reels.enabled needs >= 1000 followers")
    tr_new.pop("enabled", None)
    tr_old.pop("enabled", None)
    if tr_new != tr_old:
        problems.append("trial_reels settings other than 'enabled' are fixed")
    extra = set(new) - set(old) - set(META_KEYS)
    missing = set(old) - set(new) - set(META_KEYS)
    if extra or missing:
        problems.append(f"top-level keys can't be added/removed (added {sorted(extra)}, removed {sorted(missing)})")
    return problems


def _load_schema(cfg: Config) -> dict:
    return json.loads((cfg.prompts_dir / "learner.schema.json").read_text(encoding="utf-8"))


def _schema_errors(review: Any, cfg: Config) -> tuple[dict[tuple[str, int], str], list[str]]:
    """Schema problems split into per-item ones (('changes', i) -> msg) and document-level ones."""
    validator = jsonschema.Draft202012Validator(_load_schema(cfg))
    per_item: dict[tuple[str, int], str] = {}
    fatal: list[str] = []
    for err in validator.iter_errors(review):
        path = list(err.absolute_path)
        if len(path) >= 2 and path[0] in ("changes", "findings", "next_experiments") and isinstance(path[1], int):
            per_item.setdefault((path[0], path[1]), f"schema: {'.'.join(str(p) for p in path[2:]) or 'item'}: {err.message}")
        else:
            fatal.append(f"{'.'.join(str(p) for p in path) or '<root>'}: {err.message}")
    return per_item, fatal


def _change_record(change: Mapping, before: Any, after: Any, status: str, reason: str, ctx: _ReviewContext, subject: tuple[str, str] | None) -> dict:
    ev = change.get("evidence") if isinstance(change.get("evidence"), dict) else {}
    cited = (str(ev["dimension"]), str(ev["arm"])) if ev.get("dimension") and ev.get("arm") is not None else subject
    return {
        "field": change.get("field"),
        "before": copy.deepcopy(before),
        "after": copy.deepcopy(after),
        "evidence": {
            "n": ev.get("n"), "effect_size": ev.get("effect_size"), "metric": ev.get("metric"),
            "dimension": cited[0] if cited else None, "arm": cited[1] if cited else None,
            "n_actual": ctx.actual_n(*cited) if cited else None,
            "effect_vs_rest_actual": ctx.effect.get(cited) if cited else None,
        },
        "confidence": change.get("confidence"),
        "status": status,
        "reason": reason,
        "rationale": change.get("rationale"),
    }


def _apply_one(candidate: dict, change: Mapping, old: Mapping, ctx: _ReviewContext) -> tuple[dict, dict]:
    """Validate and apply one change on a copy. Returns (new candidate, change record)."""
    field = str(change.get("field") or "")
    after = change.get("after")
    try:
        edit = _plan_edit(candidate, field, after, ctx)
    except _Reject as rej:
        return candidate, _change_record(change, None, after, "rejected", str(rej), ctx, None)
    problem = _evidence_problem(change, edit, ctx)
    if problem:
        return candidate, _change_record(change, edit.before, edit.after, "rejected", problem, ctx, edit.subject)
    trial = copy.deepcopy(candidate)
    edit.apply(trial)
    already = set(_invariant_problems(candidate, old, ctx.cfg, ctx.trial_ok))
    new_problems = [p for p in _invariant_problems(trial, old, ctx.cfg, ctx.trial_ok) if p not in already]
    if new_problems:
        return candidate, _change_record(change, edit.before, edit.after, "rejected", "; ".join(new_problems), ctx, edit.subject)
    reason = str(change.get("rationale") or "applied")
    return trial, _change_record(change, edit.before, edit.after, "applied", reason, ctx, edit.subject)


def _add_experiment(candidate: dict, proposal: Mapping) -> tuple[dict, str]:
    """Add a proposed next experiment (hypothesis + experiment, status 'next')."""
    text = str(proposal.get("hypothesis") or "").strip()
    dim = proposal.get("dimension")
    if not (5 <= len(text) <= 300):
        raise _Reject("hypothesis must be 5-300 characters")
    if dim is not None and dim not in EXPERIMENT_DIMENSIONS:
        raise _Reject(f"dimension must be one of {EXPERIMENT_DIMENSIONS} or null")
    if any(str(h.get("text", "")).strip().lower() == text.lower() for h in candidate.get("hypotheses", [])):
        raise _Reject("this hypothesis already exists")
    new = copy.deepcopy(candidate)
    hid = _next_id(new.setdefault("hypotheses", []), "H")
    eid = _next_id(new.setdefault("experiments", []), "E")
    new["hypotheses"].append({"id": hid, "text": text, "metric": str(proposal.get("metric") or "composite"),
                              "dimension": dim, "status": "next"})
    new["experiments"].append({"id": eid, "hypothesis": hid, "dimension": dim, "status": "next"})
    return new, eid


def apply_review(conn: sqlite3.Connection, cfg: Config, playbook: dict, review: dict) -> dict:
    """Validate the brain's weekly review and store playbook version N+1.

    Each proposed change is checked against prompts/learner.schema.json, the locked rules and
    hard limits, and the min-n rule (using our own rewarded counts, not the brain's numbers).
    Rejected changes are kept in `changes` with the reason. Returns the new playbook."""
    per_item, fatal = _schema_errors(review, cfg)
    if fatal:
        raise ReviewError("review does not match learner.schema.json: " + "; ".join(fatal[:5]))
    base_version = int(playbook.get("version") or 0)
    if base_version != current_version(conn):
        raise ReviewError(f"playbook v{base_version} is not the latest (v{current_version(conn)}); reload it first")
    ctx = _ReviewContext(conn, cfg, playbook)
    old = copy.deepcopy(playbook)
    candidate = copy.deepcopy(playbook)
    for key in ("changes", "findings"):
        candidate.pop(key, None)
    records: list[dict] = []
    for i, change in enumerate(review.get("changes") or []):
        if ("changes", i) in per_item:
            c = change if isinstance(change, dict) else {}
            records.append(_change_record(c, None, c.get("after"), "rejected", per_item[("changes", i)], ctx, None))
            continue
        candidate, record = _apply_one(candidate, change, old, ctx)
        records.append(record)
    added = 0
    for i, proposal in enumerate(review.get("next_experiments") or []):
        rec_change = {"field": "experiments.new", "after": proposal, "evidence": {"n": 0, "effect_size": None, "metric": proposal.get("metric") if isinstance(proposal, dict) else None},
                      "confidence": "low", "rationale": proposal.get("why") if isinstance(proposal, dict) else None}
        if ("next_experiments", i) in per_item:
            records.append(_change_record(rec_change, None, proposal, "rejected", per_item[("next_experiments", i)], ctx, None))
            continue
        if added >= MAX_NEW_EXPERIMENTS:
            records.append(_change_record(rec_change, None, proposal, "rejected", f"at most {MAX_NEW_EXPERIMENTS} new experiments per review", ctx, None))
            continue
        try:
            candidate, eid = _add_experiment(candidate, proposal)
            added += 1
            records.append(_change_record(rec_change, None, {"id": eid, **proposal}, "applied", str(proposal.get("why") or "next experiment"), ctx, None))
        except _Reject as rej:
            records.append(_change_record(rec_change, None, proposal, "rejected", str(rej), ctx, None))
    findings = _check_findings(review.get("findings") or [], per_item, ctx)
    new_version = base_version + 1
    candidate.update({
        "version": new_version, "parent_version": base_version, "created_at": db.now_iso(),
        "source": "weekly review", "summary": str(review.get("summary") or "").strip(),
        "changes": records, "findings": findings,
    })
    problems = _invariant_problems(candidate, old, cfg, ctx.trial_ok)
    if problems:  # should be unreachable: every change was checked on its own
        raise ReviewError("refusing to store an invalid playbook: " + "; ".join(problems))
    diff = playbook_diff(old, candidate)
    json_path = cfg.dir("playbooks") / f"v{new_version}.json"
    with _atomic(conn):
        if current_version(conn) != base_version:
            raise ReviewError("another playbook version was written meanwhile; reload and retry")
        conn.execute(
            "INSERT INTO playbooks(version, created_at, json, changes_json, summary, path) VALUES (?,?,?,?,?,?)",
            (new_version, candidate["created_at"], db.jdump(candidate), db.jdump(records), candidate["summary"], str(json_path)),
        )
        _sync_experiments(conn, candidate)
        _ensure_arms(conn, candidate)
    _write_playbook_files(cfg, candidate, diff)
    applied = sum(1 for r in records if r["status"] == "applied")
    _log.info("playbook v%d stored: %d change(s) applied, %d rejected", new_version, applied, len(records) - applied)
    return candidate


def _check_findings(findings: list, per_item: Mapping[tuple[str, int], str], ctx: _ReviewContext) -> list[dict]:
    """Keep the brain's findings, flagging any whose arm has n < min_n (or fails the schema)."""
    out = []
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            continue
        rec = dict(f)
        n_actual = ctx.actual_n(str(f.get("dimension")), f.get("arm"))
        rec["n_actual"] = n_actual
        if ("findings", i) in per_item:
            rec.update(status="rejected", reason=per_item[("findings", i)])
        elif n_actual is None:
            rec.update(status="rejected", reason=f"unknown arm {f.get('dimension')}/{f.get('arm')}")
        elif n_actual < ctx.min_n:
            rec.update(status="rejected", reason=f"n={n_actual} < min_n_per_arm={ctx.min_n}: no claim yet")
        else:
            rec.update(status="ok", reason=None)
        out.append(rec)
    return out
