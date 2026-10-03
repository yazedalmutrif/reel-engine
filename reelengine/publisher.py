"""Publishing (spec §4.10, §2.3-2.5, API_NOTES "Publishing a Reel").

Every step persists its result BEFORE the next side effect, so a crash or a rerun
never double-posts:
  scheduled --(live quota check)--> publishing
  container_id (+created_at)   persisted right after the container call
  upload_done_at               persisted after the rupload POST
  poll status_code             IN_PROGRESS past the timeout = try again next tick (not a failure)
  publish_requested_at         persisted BEFORE media_publish
  media_id, permalink          read back and stored -> published
On restart in `publishing` with publish_requested_at set, the container status is read
first: PUBLISHED -> find the media in /{ig}/media (caption hash + time) and mark it
published; FINISHED -> the publish never happened, so it is safe to publish.
A second container is never created while the reel has a live (non-expired) one.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import db
from .graph import GraphClient, GraphError
from .log import get_logger, redact

log = get_logger("publisher")
STUCK_NOTIFY_AFTER = 6  # PUBLISHED-but-not-found deferrals before Yazeed is told


class PublishDeferred(Exception):
    """Try again later (state is kept). delay_s = when to retry."""

    def __init__(self, reason: str, delay_s: float = 900):
        super().__init__(reason)
        self.reason = reason
        self.delay_s = delay_s


class PublishHeld(Exception):
    """Needs a human (content error from the API). The reel is held."""


def caption_hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def crashpoint(name: str) -> None:
    """Test hook: REELENGINE_CRASH_AT=<name> simulates the process dying right here."""
    if os.environ.get("REELENGINE_CRASH_AT") == name:
        raise SystemExit(f"simulated crash at {name}")


def paused(cfg) -> bool:
    return cfg.pause_file.exists()


def _caption(row) -> str:
    return (db.jload(row["caption_json"], {}) or {}).get("caption", "")


def thumb_offset_ms(row) -> int:
    plan = db.jload(row["plan_json"], {}) or {}
    return int(round(float(plan.get("cover_t_out") or 1.1) * 1000))


class DryRunPublisher:
    """Default mode: logs exactly what would be sent, never calls Instagram."""

    name = "dry-run"

    def __init__(self, cfg, conn):
        self.cfg, self.conn = cfg, conn

    def publish(self, reel_id: int) -> dict[str, Any]:
        row = db.get_reel(self.conn, reel_id)
        if row["state"] == "published":
            return {"media_id": row["media_id"], "dry_run": True, "already": True}
        if row["state"] == "scheduled":
            db.transition(self.conn, reel_id, "publishing", "dry-run publish started", expect_from="scheduled")
        crashpoint("dryrun_after_publishing")
        logdir = self.cfg.dir("out") / "dryrun"
        logdir.mkdir(parents=True, exist_ok=True)
        rec_path = logdir / f"{reel_id:05d}.json"
        cap = _caption(row)
        out = Path(row["out_path"]) if row["out_path"] else None
        record = {
            "reel_id": reel_id,
            "at": db.now_iso(),
            "mode": "dry-run",
            "would_create_container": {
                "media_type": "REELS", "upload_type": "resumable", "caption": cap,
                "share_to_feed": self.cfg["instagram"]["share_to_feed"], "thumb_offset": thumb_offset_ms(row),
            },
            "would_upload": {"file": str(out) if out else None, "bytes": out.stat().st_size if out and out.exists() else None},
            "caption_hash": caption_hash(cap),
            "scheduled_at": row["scheduled_at"],
        }
        if not rec_path.exists():
            rec_path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        crashpoint("dryrun_after_log")
        db.transition(self.conn, reel_id, "published", f"dry-run: logged to {rec_path.name}", expect_from="publishing",
                      media_id=f"dryrun-{reel_id}", permalink=None, published_at=db.now_iso(), dry_run=1, caption_hash=caption_hash(cap))
        return {"media_id": f"dryrun-{reel_id}", "dry_run": True, "log": str(rec_path)}


class GraphPublisher:
    name = "graph"

    def __init__(self, cfg, conn, client: GraphClient, ig_user_id: str, sleep: Callable[[float], None] = time.sleep):
        self.cfg, self.conn, self.c, self.ig_id = cfg, conn, client, ig_user_id
        self.ig = cfg["instagram"]
        self.sleep = sleep

    # -------------------------------------------------------------- steps
    def _check_quota(self) -> None:
        usage, total = self.c.publishing_limit(self.ig_id)
        if total <= 0 or usage >= total - self.ig["quota_safety_margin"]:
            raise PublishDeferred(f"publishing quota {usage}/{total} (margin {self.ig['quota_safety_margin']})", 6 * 3600)
        if self.c.over_budget():
            raise PublishDeferred(f"app usage {self.c.last_app_usage}% >= {self.ig['app_usage_backoff_pct']}%", 3600)

    def _container_expired(self, row) -> bool:
        created = db.parse_ts(row["container_created_at"])
        return created is not None and db.utcnow() - created > timedelta(hours=self.ig["container_expiry_hours"])

    def _poll(self, container_id: str) -> str:
        deadline = time.monotonic() + self.ig["poll_timeout_s"]
        while True:
            st = self.c.container_status(container_id)
            code = (st.get("status_code") or "").upper()
            if code in ("FINISHED", "PUBLISHED", "ERROR", "EXPIRED"):
                if code == "ERROR":
                    raise PublishHeld(f"Instagram could not process the video: {redact(str(st.get('status') or ''))[:200]}")
                return code
            if time.monotonic() >= deadline:
                raise PublishDeferred(f"container {code or 'IN_PROGRESS'} after {self.ig['poll_timeout_s']} s; re-poll next tick", self.ig["poll_interval_s"])
            self.sleep(self.ig["poll_interval_s"])

    def _find_published(self, row) -> dict | None:
        """Only called after the container status said PUBLISHED. First an exact caption-hash
        match; if Instagram altered the caption text (so the hash never matches), fall back to
        the newest REELS item posted at/after publish_requested_at - 10 min (fact-check #19).
        Safe: the engine posts at most one reel at a time and the container IS published."""
        want = row["caption_hash"] or caption_hash(_caption(row))
        since = (db.parse_ts(row["publish_requested_at"]) or db.utcnow()) - timedelta(minutes=10)
        recent: list[tuple[datetime | None, dict]] = []
        for m in self.c.recent_media(self.ig_id):
            ts = m.get("timestamp")
            try:
                t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z") if ts else None
            except ValueError:
                t = db.parse_ts(ts)
            if caption_hash(m.get("caption") or "") == want and (t is None or t >= since):
                return m
            recent.append((t, m))
        fallback = [(t, m) for t, m in recent if t is not None and t >= since and m.get("media_product_type") in (None, "REELS")]
        if fallback:
            t, m = max(fallback, key=lambda x: x[0])
            db.add_event(self.conn, row["id"], f"media {m.get('id')} matched by time (caption hash differed)")
            return m
        return None

    def _finish(self, reel_id: int, media_id: str) -> dict:
        info = self.c.media(media_id)
        db.transition(self.conn, reel_id, "published", f"published media {media_id}", expect_from="publishing",
                      media_id=str(media_id), permalink=info.get("permalink"), published_at=db.now_iso(), dry_run=0,
                      next_attempt_at=None, last_error=None)
        return {"media_id": str(media_id), "permalink": info.get("permalink"), "dry_run": False}

    # -------------------------------------------------------------- main
    def publish(self, reel_id: int) -> dict[str, Any]:
        row = db.get_reel(self.conn, reel_id)
        if row["state"] == "published":
            return {"media_id": row["media_id"], "already": True}
        if row["state"] == "scheduled":
            self._check_quota()
            db.transition(self.conn, reel_id, "publishing", "live publish started", expect_from="scheduled",
                          caption_hash=caption_hash(_caption(row)))
            row = db.get_reel(self.conn, reel_id)
        if row["state"] != "publishing":
            raise PublishDeferred(f"reel {reel_id} is {row['state']}", 0)

        # recovery: a publish may already have happened
        if row["publish_requested_at"] and row["container_id"]:
            code = (self.c.container_status(row["container_id"]).get("status_code") or "").upper()
            if code == "PUBLISHED":
                m = self._find_published(row)
                if m is None:
                    raise PublishDeferred("container is PUBLISHED but the media isn't listed yet; re-check next tick", 300)
                return self._finish(reel_id, m["id"])
            if code == "EXPIRED":
                db.update_reel(self.conn, reel_id, container_id=None, container_created_at=None, upload_done_at=None, publish_requested_at=None)
                row = db.get_reel(self.conn, reel_id)

        if row["container_id"] and self._container_expired(row):
            code = (self.c.container_status(row["container_id"]).get("status_code") or "").upper()
            if code == "PUBLISHED":
                m = self._find_published(row)
                if m:
                    return self._finish(reel_id, m["id"])
            db.update_reel(self.conn, reel_id, container_id=None, container_created_at=None, upload_done_at=None, publish_requested_at=None)
            row = db.get_reel(self.conn, reel_id)

        # the upload URI belongs to the container created in THIS call; a container from an
        # earlier run (or another reel, when one publisher serves several) uses the default URL
        upload_uri = None
        if not row["container_id"]:
            crashpoint("before_container")
            resp = self.c.create_reel_container(self.ig_id, _caption(row), thumb_offset_ms(row))
            db.update_reel(self.conn, reel_id, container_id=str(resp["id"]), container_created_at=db.now_iso(), upload_done_at=None)
            db.add_event(self.conn, reel_id, f"container {resp['id']} created")
            upload_uri = resp.get("uri")
            crashpoint("after_container")
            row = db.get_reel(self.conn, reel_id)
        cid = row["container_id"]

        if not row["upload_done_at"]:
            out = Path(row["out_path"])
            self.c.upload(cid, out, upload_uri)
            db.update_reel(self.conn, reel_id, upload_done_at=db.now_iso())
            crashpoint("after_upload")

        code = self._poll(cid)
        if code == "EXPIRED":
            db.update_reel(self.conn, reel_id, container_id=None, container_created_at=None, upload_done_at=None, publish_requested_at=None)
            raise PublishDeferred("container expired; a new one will be created", 0)
        if code == "PUBLISHED":
            m = self._find_published(db.get_reel(self.conn, reel_id))
            if m:
                return self._finish(reel_id, m["id"])
            raise PublishDeferred("container PUBLISHED but media not found yet", 300)

        if paused(self.cfg):
            raise PublishDeferred("paused (kill switch) before media_publish", 900)
        db.update_reel(self.conn, reel_id, publish_requested_at=db.now_iso())
        crashpoint("before_media_publish")
        res = self.c.media_publish(self.ig_id, cid)
        crashpoint("after_media_publish")
        return self._finish(reel_id, res["id"])


# ---------------------------------------------------------------- orchestration helpers
def backoff_s(retries: int, base_minutes: int) -> float:
    return min(base_minutes * 60 * (2 ** max(0, retries)), 12 * 3600)


def handle_publish_error(cfg, conn, reel_id: int, exc: BaseException, notify: Callable[..., Any]) -> str:
    """Map an exception to an action. Returns the outcome label."""
    row = db.get_reel(conn, reel_id)
    now = db.utcnow()
    if isinstance(exc, PublishDeferred):
        fields: dict[str, Any] = {"next_attempt_at": (now + timedelta(seconds=exc.delay_s)).isoformat(timespec="seconds"), "last_error": exc.reason}
        if "PUBLISHED" in exc.reason and row["state"] == "publishing":
            # the post exists but can't be matched yet: count it, and tell Yazeed after 6 tries
            fields["retries"] = row["retries"] + 1
            if fields["retries"] == STUCK_NOTIFY_AFTER:
                notify("action", "A reel is posted but not confirmed",
                       f"Reel {reel_id} was published on Instagram but the engine can't find it in the media list. "
                       f"Check the profile, then `reel show {reel_id}`.", reel_id)
        db.update_reel(conn, reel_id, **fields)
        db.add_event(conn, reel_id, f"publish deferred: {exc.reason}")
        return "deferred"
    if isinstance(exc, PublishHeld):
        db.transition(conn, reel_id, "held", str(exc), held_reason=f"Publish: {exc}")
        notify("warn", "Reel held at publish", f"Reel {reel_id}: {exc}", reel_id)
        return "held"
    if isinstance(exc, GraphError):
        if exc.kind == "auth":
            from .tokens import mark_auth_needed

            mark_auth_needed(cfg, conn, str(exc), notify)
            db.update_reel(conn, reel_id, next_attempt_at=(now + timedelta(hours=1)).isoformat(timespec="seconds"), last_error=redact(str(exc))[:500])
            return "auth_needed"
        if exc.kind == "content":
            db.transition(conn, reel_id, "held", redact(str(exc)), held_reason=f"Instagram rejected it: {redact(str(exc))[:300]}")
            notify("warn", "Instagram rejected a reel", f"Reel {reel_id}: {redact(str(exc))[:300]}", reel_id)
            return "held"
        if exc.kind == "expired":
            db.update_reel(conn, reel_id, container_id=None, container_created_at=None, upload_done_at=None, publish_requested_at=None)
            return "deferred"
        delay = 3600 if exc.kind == "rate_limit" else 6 * 3600 if exc.kind == "quota" else backoff_s(row["retries"], cfg["engine"]["retry_base_minutes"])
        db.update_reel(conn, reel_id, next_attempt_at=(now + timedelta(seconds=delay)).isoformat(timespec="seconds"),
                       last_error=redact(str(exc))[:500], retries=row["retries"] + (1 if exc.kind == "retryable" else 0))
        db.add_event(conn, reel_id, f"publish {exc.kind}: retry in {int(delay)} s")
        return exc.kind
    raise exc
