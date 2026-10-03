"""Slot scheduler (spec §6 and §4.9): when does a QA-passed reel go out?

Candidate times are the playbook's slot arms (Asia/Riyadh, weekday Sun-Thu / weekend Fri-Sat
x 13:30 / 18:30 / 21:30 / 22:30). The learner orders the arms (Thompson sampling, with the
explore rule); the scheduler takes the first arm whose earliest *valid* occurrence is within
7 days, else the earliest valid occurrence of any arm within the 14-day horizon.

Valid = at least `min_lead_minutes` after now, outside quiet hours, fewer than
`max_posts_per_day` posts on that local calendar day, and at least `min_gap_hours` from every
other scheduled/publishing/published reel (dry-run reels count; demo reels don't).
All stored times are UTC ISO strings.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Iterator, Mapping

from . import db, learner
from .config import Config
from .log import get_logger

_log = get_logger("scheduler")

HORIZON_DAYS = 14
PREFERRED_WITHIN_DAYS = 7
OCCUPYING_STATES = ("scheduled", "publishing", "published")


class NoSlotAvailable(RuntimeError):
    """No valid slot inside the 14-day horizon (the queue is full or the constraints conflict)."""


def _hhmm(text: str) -> dtime:
    hh, mm = text.split(":")
    return dtime(int(hh), int(mm))


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    try:
        return db.parse_ts(str(value))
    except ValueError:
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if conn.in_transaction:
        yield conn
    else:
        with db.tx(conn):
            yield conn


def is_quiet(local_time: dtime, cfg: Config) -> bool:
    """True inside [quiet_start, quiet_end) local time; the window may cross midnight.
    Equal start and end means no quiet window."""
    start, end = _hhmm(cfg["schedule"]["quiet_start"]), _hhmm(cfg["schedule"]["quiet_end"])
    t = dtime(local_time.hour, local_time.minute, local_time.second, local_time.microsecond)
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


def day_type(local_date: date, playbook: Mapping) -> str:
    """'weekend' on the playbook's weekend days (Fri, Sat in Saudi Arabia), else 'weekday'."""
    return learner.day_type_of(local_date, playbook)


def _occupied(conn: sqlite3.Connection, exclude_reel_id: int | None) -> list[datetime]:
    """UTC times already taken: published_at for published reels, scheduled_at otherwise."""
    rows = conn.execute(
        "SELECT id, state, scheduled_at, published_at FROM reels WHERE state IN ('scheduled','publishing','published') AND is_demo=0"
    ).fetchall()
    out = []
    for r in rows:
        if exclude_reel_id is not None and r["id"] == exclude_reel_id:
            continue
        ts = r["published_at"] if r["state"] == "published" and r["published_at"] else r["scheduled_at"]
        dt = _utc(ts)
        if dt is not None:
            out.append(dt)
    return out


class _Constraints:
    """Checks one candidate time against lead time, horizon, quiet hours, per-day cap and gap."""

    def __init__(self, cfg: Config, now_utc: datetime, occupied: list[datetime]):
        sched = cfg["schedule"]
        self.cfg = cfg
        self.earliest = now_utc + timedelta(minutes=int(sched["min_lead_minutes"]))
        self.horizon = now_utc + timedelta(days=HORIZON_DAYS)
        self.max_per_day = int(sched["max_posts_per_day"])
        self.gap = timedelta(hours=float(sched["min_gap_hours"]))
        self.occupied = occupied
        self.per_day: dict[date, int] = {}
        for o in occupied:
            d = o.astimezone(cfg.tz).date()
            self.per_day[d] = self.per_day.get(d, 0) + 1

    def ok(self, local: datetime, utc: datetime) -> bool:
        if utc < self.earliest or utc > self.horizon:
            return False
        if is_quiet(local.time(), self.cfg):
            return False
        if self.per_day.get(local.date(), 0) >= self.max_per_day:
            return False
        return all(abs(utc - o) >= self.gap for o in self.occupied)


def _earliest_valid(arm: Mapping, playbook: Mapping, cfg: Config, now_utc: datetime, rules: _Constraints) -> tuple[datetime, datetime] | None:
    """(local, utc) of the arm's first valid occurrence inside the horizon, or None."""
    t = _hhmm(str(arm["time"]))
    first_day = now_utc.astimezone(cfg.tz).date()
    for offset in range(HORIZON_DAYS + 1):
        day = first_day + timedelta(days=offset)
        if day_type(day, playbook) != arm.get("day_type"):
            continue
        local = datetime.combine(day, t, tzinfo=cfg.tz)
        utc = local.astimezone(timezone.utc)
        if utc > rules.horizon:
            return None
        if rules.ok(local, utc):
            return local, utc
    return None


def next_slot(
    conn: sqlite3.Connection,
    cfg: Config,
    playbook: Mapping,
    now: datetime | None = None,
    rng: Any = None,
    reel_id: int | None = None,
) -> dict:
    """Pick the next posting time and log the decision (dimension 'slot').

    Returns {"when_utc", "when_local", "arm", "explore"}. Raises NoSlotAvailable when no arm has
    a valid occurrence inside the 14-day horizon."""
    now_utc = _utc(now) or db.utcnow()
    rules = _Constraints(cfg, now_utc, _occupied(conn, reel_id))
    plan = learner.arm_order(conn, cfg, playbook, "slot", rng=rng, reel_id=reel_id)
    arms = {str(a["id"]): a for a in playbook["arms"]["slot"]}
    earliest = {a: occ for a in plan["order"] if (occ := _earliest_valid(arms[a], playbook, cfg, now_utc, rules))}
    if not earliest:
        raise NoSlotAvailable(f"no valid slot in the next {HORIZON_DAYS} days (queue full or constraints conflict)")
    cutoff = now_utc + timedelta(days=PREFERRED_WITHIN_DAYS)
    chosen = next((a for a in plan["order"] if a in earliest and earliest[a][1] <= cutoff), None)
    fallback = chosen is None
    if fallback:
        chosen = min(earliest, key=lambda a: (earliest[a][1], plan["order"].index(a)))
    local, utc = earliest[chosen]
    learner.log_decision(
        conn, reel_id, "slot", chosen, plan["explore"],
        learner.decision_detail(plan, "scheduler", order=plan["order"], when_utc=_iso(utc), fallback=fallback),
    )
    _log.info("reel %s -> %s at %s%s", reel_id, chosen, local.isoformat(), " (explore)" if plan["explore"] else "")
    return {"when_utc": utc, "when_local": local, "arm": chosen, "explore": plan["explore"]}


def schedule_reel(conn: sqlite3.Connection, cfg: Config, reel_id: int, now: datetime | None = None, rng: Any = None) -> dict:
    """qa_passed -> scheduled: pick a slot, store scheduled_at (UTC ISO) + slot_arm, and merge
    slot / weekday / hour into features_json. Raises db.TransitionError on a wrong state."""
    playbook = learner.load_playbook(conn, cfg)
    with _atomic(conn):
        row = db.get_reel(conn, reel_id)
        if row is None:
            raise db.TransitionError(f"reel {reel_id} not found")
        if row["state"] != "qa_passed":
            raise db.TransitionError(f"reel {reel_id} is {row['state']}, expected qa_passed")
        slot = next_slot(conn, cfg, playbook, now=now, rng=rng, reel_id=reel_id)
        when_iso = _iso(slot["when_utc"])
        fresh = learner.extract_features({**dict(row), "scheduled_at": when_iso, "slot_arm": slot["arm"]}, playbook, cfg)
        features = learner.json_dict(row["features_json"])
        features.update({k: v for k, v in fresh.items() if v is not None})
        local = slot["when_local"]
        features.update(slot=slot["arm"], weekday=learner.WEEKDAY_ABBR[local.weekday()], hour=local.hour)
        db.transition(
            conn, reel_id, "scheduled",
            detail=f"slot {slot['arm']} at {local.isoformat()}" + (" (explore)" if slot["explore"] else ""),
            expect_from="qa_passed", scheduled_at=when_iso, slot_arm=slot["arm"], features_json=db.jdump(features),
        )
    return {"reel_id": reel_id, "scheduled_at": when_iso, **slot}


def due_reels(conn: sqlite3.Connection, now: datetime | None = None) -> list[sqlite3.Row]:
    """Scheduled, non-demo reels whose scheduled_at <= now, oldest first."""
    now_utc = _utc(now) or db.utcnow()
    rows = conn.execute(
        "SELECT * FROM reels WHERE state='scheduled' AND is_demo=0 AND scheduled_at IS NOT NULL"
    ).fetchall()
    due = [(t, r["id"], r) for r in rows if (t := _utc(r["scheduled_at"])) is not None and t <= now_utc]
    return [r for _, _, r in sorted(due, key=lambda x: (x[0], x[1]))]
