"""Metrics collection (spec §5 "Collect", API_NOTES "Insights").

- Per reel: checkpoints at +1 h, +6 h, +24 h, +72 h, +7 d, +28 d after publish.
- Metrics are requested in small groups so one unsupported metric can't fail the
  whole call; a failing group is recorded and its metrics stay NULL.
- Empty data is NULL, never 0. Raw payloads are stored after redaction (paging URLs
  carry the token, so `paging` is dropped).
- Account: daily follower count + account insights (the call also keeps the app
  active, since access can be reduced after 30 days of inactivity).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import db
from .graph import GraphClient, GraphError, parse_metric_values
from .log import dumps_redacted, get_logger, redact

log = get_logger("insights")
MAX_ATTEMPTS = 5


def schedule_checkpoints(cfg, conn, reel_id: int, media_id: str, published_at: str) -> int:
    base = db.parse_ts(published_at) or db.utcnow()
    n = 0
    for h in cfg["instagram"]["insights_checkpoints_h"]:
        due = (base + timedelta(hours=float(h))).isoformat(timespec="seconds")
        cur = conn.execute(
            "INSERT OR IGNORE INTO insights(reel_id, media_id, checkpoint_h, due_at) VALUES(?,?,?,?)",
            (reel_id, media_id, float(h), due),
        )
        n += cur.rowcount
    return n


def collect_media(cfg, conn, client: GraphClient, row) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    raw: dict[str, Any] = {}
    errors = []
    for group in cfg["instagram"]["media_metric_groups"]:
        try:
            payload = client.media_insights(row["media_id"], group)
            raw[",".join(group)] = payload
            metrics.update(parse_metric_values(payload, group))
        except GraphError as exc:
            if exc.kind in ("auth", "rate_limit"):
                raise
            errors.append(f"{','.join(group)}: {exc.kind} {redact(str(exc))[:160]}")
            metrics.update({m: None for m in group})
    return {"metrics": metrics, "raw": raw, "errors": errors}


def collect_due(cfg, conn, client: GraphClient, now: datetime | None = None, limit: int = 30) -> dict:
    now = now or db.utcnow()
    rows = conn.execute(
        "SELECT i.*, r.dry_run FROM insights i JOIN reels r ON r.id=i.reel_id "
        "WHERE i.fetched_at IS NULL AND i.due_at <= ? AND i.attempts < ? AND r.dry_run=0 AND r.is_demo=0 "
        "ORDER BY i.due_at LIMIT ?",
        (now.isoformat(timespec="seconds"), MAX_ATTEMPTS, limit),
    ).fetchall()
    done = failed = 0
    for row in rows:
        try:
            res = collect_media(cfg, conn, client, row)
        except GraphError as exc:
            conn.execute("UPDATE insights SET attempts=attempts+1, error=? WHERE id=?", (redact(str(exc))[:300], row["id"]))
            failed += 1
            if exc.kind in ("auth", "rate_limit"):
                raise
            continue
        conn.execute(
            "UPDATE insights SET fetched_at=?, attempts=attempts+1, metrics_json=?, raw_json=?, error=? WHERE id=?",
            (db.now_iso(), db.jdump(res["metrics"]), dumps_redacted(res["raw"]), "; ".join(res["errors"]) or None, row["id"]),
        )
        done += 1
    return {"due": len(rows), "fetched": done, "failed": failed}


def collect_account(cfg, conn, client: GraphClient, ig_user_id: str, day: datetime | None = None) -> dict:
    """Yesterday's account insights (Asia/Riyadh day) + current follower count."""
    tz = cfg.tz
    local_now = (day or db.utcnow()).astimezone(tz)
    start_local = (local_now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    since, until = int(start_local.timestamp()), int((start_local + timedelta(days=1)).timestamp())
    raw: dict[str, Any] = {}
    metrics: dict[str, Any] = {}
    names = cfg["instagram"]["account_metrics"]
    for k in range(0, len(names), 4):
        grp = names[k:k + 4]
        try:
            p = client.account_insights(ig_user_id, grp, since, until)
            raw[",".join(grp)] = p
            metrics.update(parse_metric_values(p, grp))
        except GraphError as exc:
            if exc.kind in ("auth", "rate_limit"):
                raise
            metrics.update({m: None for m in grp})
            raw[",".join(grp)] = {"error": redact(str(exc))[:200]}
    followers = None
    try:
        u = client.user_counts(ig_user_id)
        followers = u.get("followers_count")
        metrics["media_count"] = u.get("media_count")
        raw["user"] = u
    except GraphError as exc:
        if exc.kind in ("auth", "rate_limit"):
            raise
    metrics["follower_count"] = followers
    day_s = start_local.date().isoformat()
    conn.execute(
        "INSERT INTO account_insights(day, fetched_at, follower_count, metrics_json, raw_json) VALUES(?,?,?,?,?) "
        "ON CONFLICT(day) DO UPDATE SET fetched_at=excluded.fetched_at, follower_count=excluded.follower_count, "
        "metrics_json=excluded.metrics_json, raw_json=excluded.raw_json",
        (day_s, db.now_iso(), followers, db.jdump(metrics), dumps_redacted(raw)),
    )
    return {"day": day_s, "follower_count": followers, "metrics": metrics}
