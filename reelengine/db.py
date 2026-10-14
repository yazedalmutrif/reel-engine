"""SQLite store: schema, the reel state machine and small helpers.

State machine (spec §4):
    new → transcribed → planned → rendered → qa_passed | held → scheduled → publishing → published
    plus `failed` (retry count, last error, the stage it failed in, next attempt time).

Every transition is written (with an `events` row) in one transaction *before* the
side effect of the next stage, so a crash at any point resumes cleanly.

JSON columns (shapes are the contract for the dashboard, learner and CLI):
  probe_json    {duration, width, height, rotation, fps, vcodec, pix_fmt, color_transfer,
                 is_hdr, has_audio, audio_rate, audio_channels, size_bytes}
  plan_json     {keep_segments:[int], drop_word_ids:[int], intervals:[[s,e]], output_duration,
                 cuts, hook:{spoken,on_screen}, title_card, pillar, topic, hook_type,
                 cover_t_src, cover_t_out, risk_flags:[str], caption_fixes:[{word_id,text}],
                 notes, brain_raw:{...}}
  caption_json  {caption, first_line, body, cta_type, cta_text, hashtags:[str], keywords:[str],
                 caption_style}
  qa_json       {passed, hard_fails:[str], checks:[{name, ok, hard, detail}],
                 scores:{hook, clarity, value, accuracy}, critique:{...}, at}
  features_json {pillar, topic, hook_type, duration_band, duration_s, wpm, cuts_per_10s,
                 caption_len, hashtags:[str], hashtag_count, cta_type, caption_style,
                 slot, weekday, hour, cover_style}
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .log import redact

SCHEMA_VERSION = 2

STATES = (
    "new",
    "transcribed",
    "planned",
    "rendered",
    "qa_passed",
    "held",
    "scheduled",
    "publishing",
    "published",
    "failed",
)
ACTIVE_STATES = ("new", "transcribed", "planned", "rendered", "qa_passed", "scheduled", "publishing")

# allowed transitions: from -> set(to)
TRANSITIONS: dict[str, set[str]] = {
    "new": {"transcribed", "held", "failed"},
    "transcribed": {"planned", "held", "failed"},
    "planned": {"rendered", "held", "failed"},
    "rendered": {"qa_passed", "held", "failed", "planned"},
    "qa_passed": {"scheduled", "held", "failed", "rendered", "planned"},
    "scheduled": {"publishing", "held", "failed", "qa_passed", "rendered", "planned"},
    "publishing": {"published", "scheduled", "held", "failed", "publishing"},
    "failed": {"new", "transcribed", "planned", "rendered", "qa_passed", "scheduled", "publishing", "held"},
    "held": {"new", "transcribed", "planned", "rendered"},  # never straight to qa_passed: QA must run again
    "published": {"qa_passed"},  # only for dry-run rehearsals (guarded in transition())
}

DDL = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS reels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sha256 TEXT NOT NULL UNIQUE,
  source_name TEXT,
  raw_path TEXT,
  state TEXT NOT NULL DEFAULT 'new',
  held_reason TEXT,
  failed_from TEXT,
  retries INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  next_attempt_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  probe_json TEXT,
  transcript_path TEXT,
  plan_json TEXT,
  caption_json TEXT,
  qa_json TEXT,
  style_overrides_json TEXT,
  out_path TEXT,
  thumb_path TEXT,
  src_duration_s REAL,
  duration_s REAL,
  features_json TEXT,
  playbook_version INTEGER,
  scheduled_at TEXT,
  slot_arm TEXT,
  container_id TEXT,
  container_created_at TEXT,
  upload_done_at TEXT,
  publish_requested_at TEXT,
  caption_hash TEXT,
  media_id TEXT,
  permalink TEXT,
  published_at TEXT,
  dry_run INTEGER NOT NULL DEFAULT 0,
  rewarded_at TEXT,
  composite_score REAL,
  is_demo INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_reels_state ON reels(state);
CREATE INDEX IF NOT EXISTS ix_reels_sched ON reels(scheduled_at);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reel_id INTEGER,
  ts TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_reel ON events(reel_id);

CREATE TABLE IF NOT EXISTS inbox_watch (
  path TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  stable_count INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL
);

-- one row per (reel, checkpoint); metrics_json holds NULL for metrics with no data
CREATE TABLE IF NOT EXISTS insights (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reel_id INTEGER NOT NULL,
  media_id TEXT,
  checkpoint_h REAL NOT NULL,
  due_at TEXT NOT NULL,
  fetched_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  metrics_json TEXT,
  raw_json TEXT,
  error TEXT,
  UNIQUE(reel_id, checkpoint_h)
);
CREATE INDEX IF NOT EXISTS ix_insights_due ON insights(fetched_at, due_at);

CREATE TABLE IF NOT EXISTS account_insights (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT NOT NULL UNIQUE,          -- YYYY-MM-DD (Asia/Riyadh)
  fetched_at TEXT NOT NULL,
  follower_count INTEGER,            -- NULL when the API returns no data (<100 followers)
  metrics_json TEXT,
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS bandit_arms (
  dimension TEXT NOT NULL,           -- slot | hook_type | duration_band
  arm TEXT NOT NULL,
  alpha REAL NOT NULL DEFAULT 1,
  beta REAL NOT NULL DEFAULT 1,
  n INTEGER NOT NULL DEFAULT 0,
  wins INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT,
  PRIMARY KEY (dimension, arm)
);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  reel_id INTEGER,
  dimension TEXT NOT NULL,
  arm TEXT NOT NULL,
  explore INTEGER NOT NULL DEFAULT 0,
  detail_json TEXT
);

CREATE TABLE IF NOT EXISTS playbooks (
  version INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  json TEXT NOT NULL,
  changes_json TEXT,                 -- [{field, before, after, evidence:{n, effect, ...}, confidence}]
  summary TEXT,
  path TEXT
);

CREATE TABLE IF NOT EXISTS experiments (
  id TEXT PRIMARY KEY,               -- e.g. E1
  hypothesis TEXT NOT NULL,
  dimension TEXT,
  arms_json TEXT,
  status TEXT NOT NULL,              -- running | logging | next | concluded | parked | dropped
  started_at TEXT,
  ended_at TEXT,
  result TEXT
);

CREATE TABLE IF NOT EXISTS ticks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                -- tick | daily | weekly | process
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT,                       -- ok | error | skipped | locked | paused
  summary_json TEXT,
  error TEXT
);

CREATE TABLE IF NOT EXISTS brain_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  task TEXT NOT NULL,
  model TEXT,
  reel_id INTEGER,
  duration_s REAL,
  attempts INTEGER,
  ok INTEGER NOT NULL,
  error TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,               -- info | warn | error | action
  title TEXT NOT NULL,
  body TEXT,
  reel_id INTEGER,
  channels_json TEXT
);

CREATE TABLE IF NOT EXISTS token_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  status TEXT,                       -- ok | auth_needed | missing | unknown
  issued_at TEXT,
  expires_at TEXT,
  last_refresh_at TEXT,
  last_check_at TEXT,
  ig_user_id TEXT,
  username TEXT,
  account_type TEXT,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS api_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  endpoint TEXT,
  http_status INTEGER,
  app_usage_pct REAL,
  quota_usage INTEGER,
  quota_total INTEGER,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS briefs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  week_start TEXT NOT NULL,
  created_at TEXT NOT NULL,
  json TEXT NOT NULL,
  path TEXT
);

CREATE TABLE IF NOT EXISTS trends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  week_start TEXT NOT NULL,
  created_at TEXT NOT NULL,
  json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  from_version INTEGER,
  to_version INTEGER,
  json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT NOT NULL,
  amount_sar REAL NOT NULL,
  item TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  is_demo INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS backups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  path TEXT NOT NULL,
  size_bytes INTEGER,
  onedrive_copy TEXT,
  ok INTEGER NOT NULL
);
"""


# v2 (2026-10-14): occasions calendar, attached accounts, dashboard uploads
DDL_V2 = """
CREATE TABLE IF NOT EXISTS occasions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL UNIQUE,
  name_ar TEXT NOT NULL,
  name_en TEXT NOT NULL,
  rule TEXT NOT NULL,                -- gregorian (every year) | hijri (Umm al-Qura, every year) | once (one date)
  month INTEGER NOT NULL,
  day INTEGER NOT NULL,
  year INTEGER,                      -- only for rule = once
  duration_days INTEGER NOT NULL DEFAULT 1,
  category TEXT NOT NULL,            -- national | religious | international | tech | career
  official INTEGER NOT NULL DEFAULT 0,
  angle TEXT,
  notes TEXT,
  source_url TEXT,
  checked_on TEXT,
  keywords_json TEXT,                -- extra match words for tagging reels (the names always match)
  origin TEXT NOT NULL DEFAULT 'user',  -- seed | user
  deleted INTEGER NOT NULL DEFAULT 0,   -- tombstone: a deleted seed day never comes back on reseed
  is_demo INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  pseudonym TEXT NOT NULL UNIQUE,    -- ACC_01...: the only name the brain ever sees (PDPL art. 29)
  role TEXT NOT NULL,                -- own | reference | competitor
  source TEXT NOT NULL DEFAULT 'manual',  -- manual | config | api
  notes TEXT,
  added_at TEXT NOT NULL,
  last_fetched_at TEXT,
  last_error TEXT,
  followers INTEGER,
  media_count INTEGER,
  is_demo INTEGER NOT NULL DEFAULT 0
);

-- public posts of attached accounts: minimal fields, pruned after accounts.retention_days
CREATE TABLE IF NOT EXISTS account_media (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  media_key TEXT NOT NULL,           -- IG media id, or a hash of the permalink for manual rows
  permalink TEXT,
  posted_at TEXT,
  media_type TEXT,                   -- REELS | IMAGE | CAROUSEL | VIDEO | NULL
  caption TEXT,
  likes INTEGER,
  comments INTEGER,
  views INTEGER,
  duration_s REAL,
  source TEXT NOT NULL,              -- api | manual | csv
  fetched_at TEXT NOT NULL,          -- last import
  first_seen_at TEXT NOT NULL,       -- first import: the retention clock (never updated)
  UNIQUE(account_id, media_key)
);

CREATE TABLE IF NOT EXISTS account_analyses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  json TEXT NOT NULL,
  stats_json TEXT
);

CREATE TABLE IF NOT EXISTS uploads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  original_name TEXT,
  stored_name TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  status TEXT NOT NULL,              -- queued | duplicate
  duplicate_of INTEGER
);
"""


class TransitionError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (and migrate) the DB. isolation_level=None: we manage transactions explicitly."""
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if str(db_path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver < 1:
        conn.executescript(DDL)
        conn.execute("PRAGMA user_version=1")
        conn.execute("INSERT OR IGNORE INTO token_state(id, status) VALUES (1, 'missing')")
        ver = 1
    if ver < 2:
        conn.executescript(DDL_V2)
        conn.execute("PRAGMA user_version=2")
    # a DB created by an early build of v2 lacks account_media.first_seen_at (the retention clock)
    # One IMMEDIATE transaction: two processes opening an old DB at once can't both ALTER, and a crash can't
    # leave the column added but not backfilled. The backfill runs on every connect, so it also repairs a
    # half-migrated DB (rows with no clock would never be pruned).
    if [r[1] for r in conn.execute("PRAGMA table_info(account_media)")]:
        with tx(conn):
            cols = [r[1] for r in conn.execute("PRAGMA table_info(account_media)")]
            if "first_seen_at" not in cols:
                conn.execute("ALTER TABLE account_media ADD COLUMN first_seen_at TEXT")
            conn.execute("UPDATE account_media SET first_seen_at=fetched_at WHERE first_seen_at IS NULL")


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit IMMEDIATE transaction (commit on success, rollback on error)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------- JSON helpers
def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def jload(s: str | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    return json.loads(s)


# ---------------------------------------------------------------- meta
def meta_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------- reels
_REEL_COLUMNS: set[str] | None = None


def reel_columns(conn: sqlite3.Connection) -> set[str]:
    global _REEL_COLUMNS
    if _REEL_COLUMNS is None:
        _REEL_COLUMNS = {r["name"] for r in conn.execute("PRAGMA table_info(reels)")}
    return _REEL_COLUMNS


def get_reel(conn: sqlite3.Connection, reel_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM reels WHERE id=?", (reel_id,)).fetchone()


def insert_reel(conn: sqlite3.Connection, sha256: str, source_name: str, raw_path: str, **fields: Any) -> int:
    ts = now_iso()
    cols = {"sha256": sha256, "source_name": source_name, "raw_path": raw_path, "state": "new", "created_at": ts, "updated_at": ts}
    cols.update(fields)
    _check_cols(conn, cols)
    q = f"INSERT INTO reels({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})"  # nosec B608 - column names checked against the schema; values bound as parameters
    cur = conn.execute(q, tuple(cols.values()))
    rid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO events(reel_id, ts, from_state, to_state, detail) VALUES(?,?,?,?,?)",
        (rid, ts, None, "new", f"intake {source_name}"),
    )
    return rid


def _check_cols(conn: sqlite3.Connection, fields: dict) -> None:
    bad = set(fields) - reel_columns(conn)
    if bad:
        raise KeyError(f"unknown reels columns: {sorted(bad)}")


def update_reel(conn: sqlite3.Connection, reel_id: int, **fields: Any) -> None:
    """Update columns without a state change (e.g. store an artifact path)."""
    if "state" in fields:
        raise ValueError("use transition() to change state")
    if not fields:
        return
    fields["updated_at"] = now_iso()
    _check_cols(conn, fields)
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE reels SET {sets} WHERE id=?", (*fields.values(), reel_id))  # nosec B608 - column names are checked against the schema


def transition(
    conn: sqlite3.Connection,
    reel_id: int,
    to_state: str,
    detail: str = "",
    expect_from: str | tuple[str, ...] | None = None,
    **fields: Any,
) -> None:
    """Atomically move a reel to `to_state` (validated) and store `fields` + an event row."""
    if to_state not in STATES:
        raise TransitionError(f"unknown state {to_state}")
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT state FROM reels WHERE id=?", (reel_id,)).fetchone()
        if row is None:
            raise TransitionError(f"reel {reel_id} not found")
        cur = row["state"]
        if expect_from is not None:
            allowed_from = (expect_from,) if isinstance(expect_from, str) else expect_from
            if cur not in allowed_from:
                raise TransitionError(f"reel {reel_id} is {cur}, expected {allowed_from}")
        if to_state not in TRANSITIONS[cur]:
            raise TransitionError(f"illegal transition {cur} -> {to_state} for reel {reel_id}")
        if cur == "published":
            dr = conn.execute("SELECT dry_run FROM reels WHERE id=?", (reel_id,)).fetchone()["dry_run"]
            if dr != 1:
                raise TransitionError(f"reel {reel_id} is really published on Instagram; it can't be re-queued")
        fields = dict(fields)
        fields["state"] = to_state
        fields["updated_at"] = now_iso()
        if to_state != "held" and "held_reason" not in fields:
            fields["held_reason"] = None
        _check_cols(conn, fields)
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE reels SET {sets} WHERE id=?", (*fields.values(), reel_id))  # nosec B608
        conn.execute(
            "INSERT INTO events(reel_id, ts, from_state, to_state, detail) VALUES(?,?,?,?,?)",
            (reel_id, fields["updated_at"], cur, to_state, redact(detail)[:2000]),
        )
        if own_tx:
            conn.execute("COMMIT")
    except BaseException:
        if own_tx:
            conn.execute("ROLLBACK")
        raise


def add_event(conn: sqlite3.Connection, reel_id: int | None, detail: str) -> None:
    conn.execute(
        "INSERT INTO events(reel_id, ts, from_state, to_state, detail) VALUES(?,?,?,?,?)",
        (reel_id, now_iso(), None, None, redact(detail)[:2000]),
    )


def reels_in(conn: sqlite3.Connection, *states: str, include_demo: bool = False) -> list[sqlite3.Row]:
    q = f"SELECT * FROM reels WHERE state IN ({', '.join('?' for _ in states)})"  # nosec B608 - only ? placeholders are formatted in
    if not include_demo:
        q += " AND is_demo=0"
    return list(conn.execute(q + " ORDER BY id", states))


def queue_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {s: 0 for s in STATES}
    for r in conn.execute("SELECT state, COUNT(*) AS n FROM reels WHERE is_demo=0 GROUP BY state"):
        counts[r["state"]] = r["n"]
    return counts
