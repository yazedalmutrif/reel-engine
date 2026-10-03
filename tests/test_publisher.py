"""GraphPublisher + GraphClient + insights + tokens against the local mock Graph API
(spec §12 "Publisher and insights against a local mock Graph API", API_NOTES).

Nothing here talks to Instagram: every host is tests/mock_graph.py on 127.0.0.1, the
token lives in the in-memory keyring from conftest, and polling uses a fake clock.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from conftest import all_text_values
from mock_graph import VALID
from reelengine import db, insights, jobs, publisher, secrets, tokens
from reelengine.graph import GraphClient, GraphError, classify
from reelengine.log import dumps_redacted
from reelengine.notify import notifier
from reelengine.pipeline import Ctx
from reelengine.publisher import DryRunPublisher, GraphPublisher, PublishDeferred, PublishHeld, handle_publish_error

CAPTION = "3 خطوات تسويها قبل ما تقدم على أي وظيفة\n\nسوّ CV واضح وحدّث LinkedIn.\n\n#وظائف #خريجين #AI"
PUBLISH_CRASHPOINTS = ["before_container", "after_container", "after_upload", "before_media_publish", "after_media_publish"]


# ------------------------------------------------------------------ fixtures
class FakeClock:
    """Replaces publisher.time: sleep() advances monotonic() instantly; hooks run on each sleep."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []
        self.hooks: list = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s
        for h in self.hooks:
            h()


class RecordingSession(requests.Session):
    """Records method, url, params and headers of every request the client sends."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict[str, Any]] = []

    def request(self, method, url, **kw):  # noqa: D401
        self.sent.append({"method": method, "url": url, "params": dict(kw.get("params") or {}), "headers": dict(kw.get("headers") or {})})
        return super().request(method, url, **kw)


@dataclass
class Env:
    cfg: Any
    conn: Any
    mock: Any
    clock: FakeClock
    tmp: Any
    session: RecordingSession = field(default_factory=RecordingSession)
    _n: int = 0

    @property
    def st(self):
        return self.mock.state

    @property
    def ig(self) -> str:
        return self.st.ig_user_id

    def client(self) -> GraphClient:
        return GraphClient(self.cfg, self.conn, lambda: secrets.get_secret(secrets.ACCESS_TOKEN), session=self.session)

    def pub(self) -> GraphPublisher:
        """A fresh publisher (a new process after a crash has no in-memory state)."""
        return GraphPublisher(self.cfg, self.conn, self.client(), self.ig, sleep=self.clock.sleep)

    def notify(self, *a, **kw):
        return notifier(self.cfg, self.conn, toast=False)(*a, **kw)

    def ctx(self) -> Ctx:
        return Ctx(self.cfg, self.conn, None, self.notify)

    def reel(self, state: str = "scheduled", caption: str = CAPTION, **extra) -> int:
        self._n += 1
        out = self.tmp / f"out{self._n}.mp4"
        out.write_bytes(os.urandom(4096))
        fields = {
            "state": state,
            "out_path": str(out),
            "caption_json": db.jdump({"caption": caption, "first_line": caption.split("\n")[0], "hashtags": ["#وظائف"]}),
            "plan_json": db.jdump({"cover_t_out": 1.5, "hook": {"on_screen": "3 خطوات قبل ما تقدم"}, "hook_type": "list_number"}),
            "scheduled_at": (db.utcnow() - timedelta(minutes=1)).isoformat(timespec="seconds"),
            "duration_s": 31.0,
            **extra,
        }
        return db.insert_reel(self.conn, f"sha-{self._n}-{os.urandom(4).hex()}", f"IMG_{self._n}.MOV", str(self.tmp / "raw.mov"), **fields)

    def row(self, rid: int):
        return db.get_reel(self.conn, rid)

    def notifications(self) -> list:
        return list(self.conn.execute("SELECT * FROM notifications ORDER BY id"))

    def requests_to(self, fragment: str, method: str | None = None) -> list:
        return [(m, p) for m, p in self.st.requests if fragment in p and (method is None or m == method)]


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(publisher, "time", SimpleNamespace(monotonic=c.monotonic, sleep=c.sleep))
    return c


@pytest.fixture
def env(graph_cfg, graph_conn, mock_graph, clock, tmp_path):
    secrets.set_secret(secrets.ACCESS_TOKEN, VALID)
    e = Env(graph_cfg, graph_conn, mock_graph, clock, tmp_path)
    yield e
    e.session.close()


def _ts(s: str | None):
    return db.parse_ts(s)


def _approx_from_now(iso: str | None, seconds: float, tol: float = 120) -> bool:
    t = _ts(iso)
    return t is not None and abs((t - db.utcnow()).total_seconds() - seconds) <= tol


# ------------------------------------------------------------------ 1. happy path
def test_happy_path_publishes_once_and_schedules_insights(env):
    rid = env.reel()
    res = env.pub().publish(rid)
    st, row = env.st, env.row(rid)

    assert row["state"] == "published" and row["dry_run"] == 0
    assert res["media_id"] == row["media_id"] and row["media_id"] in st.media
    assert row["permalink"] == st.media[row["media_id"]]["permalink"]
    assert st.publish_calls == 1 and len(st.containers) == 1 and len(st.media) == 1
    (cid, c), = st.containers.items()
    assert row["container_id"] == cid and st.published_containers == [cid]
    assert c["caption"] == CAPTION and c["thumb_offset"] == "1500"
    assert c["uploaded"] and c["size"] == os.path.getsize(row["out_path"])
    assert c["polls"] == st.polls_before_finished + 1  # IN_PROGRESS twice, then FINISHED
    assert env.clock.sleeps == [5, 5]
    assert row["container_created_at"] and row["upload_done_at"] and row["publish_requested_at"] and row["published_at"]

    # the call order of API_NOTES: quota -> container -> rupload -> poll -> media_publish -> read back
    seq = [(m, p) for m, p in st.requests]
    idx = {k: next(i for i, (m, p) in enumerate(seq) if f(m, p)) for k, f in {
        "quota": lambda m, p: p.endswith("/content_publishing_limit"),
        "container": lambda m, p: m == "POST" and p.endswith(f"{env.ig}/media"),
        "upload": lambda m, p: p == f"/ig-api-upload/v26.0/{cid}",
        "poll": lambda m, p: m == "GET" and p.endswith(f"/{cid}") and "ig-api-upload" not in p,
        "publish": lambda m, p: p.endswith("/media_publish"),
        "readback": lambda m, p: m == "GET" and p.endswith(f"/{row['media_id']}"),
    }.items()}
    assert idx["quota"] < idx["container"] < idx["upload"] < idx["poll"] < idx["publish"] < idx["readback"]

    # rupload: Authorization OAuth + offset/file_size; other calls: Bearer header, never the token in the URL
    up = [r for r in env.session.sent if "ig-api-upload" in r["url"]]
    assert len(up) == 1
    assert up[0]["headers"]["Authorization"] == f"OAuth {VALID}"
    assert up[0]["headers"]["offset"] == "0" and up[0]["headers"]["file_size"] == str(c["size"])
    for r in env.session.sent:
        assert VALID not in r["url"] and VALID not in str(r["params"])
        if "ig-api-upload" not in r["url"]:
            assert r["headers"]["Authorization"] == f"Bearer {VALID}"

    # post-publish: 6 insights checkpoints
    jobs.after_publish(env.ctx(), rid)
    cps = [r["checkpoint_h"] for r in env.conn.execute("SELECT checkpoint_h FROM insights WHERE reel_id=? ORDER BY checkpoint_h", (rid,))]
    assert cps == [1, 6, 24, 72, 168, 672]
    # a second publish call is a no-op
    assert env.pub().publish(rid)["already"] is True
    assert st.publish_calls == 1


# ------------------------------------------------------------------ 2. live quota check
def test_quota_is_read_live_and_defers_near_the_limit(env):
    rid = env.reel()
    env.st.quota_usage, env.st.quota_total = 98, 100
    with pytest.raises(PublishDeferred) as ei:
        env.pub().publish(rid)
    assert "98/100" in ei.value.reason
    assert env.row(rid)["state"] == "scheduled" and env.st.containers == {}
    assert handle_publish_error(env.cfg, env.conn, rid, ei.value, env.notify) == "deferred"
    assert env.row(rid)["state"] == "scheduled" and _approx_from_now(env.row(rid)["next_attempt_at"], 6 * 3600)

    # the server's limit changes: 48/50 must also defer (a hardcoded total of 100 would publish)
    env.st.quota_usage, env.st.quota_total = 48, 50
    with pytest.raises(PublishDeferred):
        env.pub().publish(rid)
    assert env.st.containers == {}

    env.st.quota_usage = 47
    env.pub().publish(rid)
    assert env.row(rid)["state"] == "published" and env.st.publish_calls == 1
    assert len(env.requests_to("content_publishing_limit")) == 3
    q = env.conn.execute("SELECT quota_usage, quota_total FROM api_usage WHERE quota_total IS NOT NULL ORDER BY id").fetchall()
    assert [tuple(r) for r in q] == [(98, 100), (48, 50), (47, 50)]


# ------------------------------------------------------------------ 3. container ERROR
def test_container_error_holds_the_reel(env):
    rid = env.reel()
    env.st.container_result = "ERROR"
    with pytest.raises(PublishHeld) as ei:
        env.pub().publish(rid)
    assert handle_publish_error(env.cfg, env.conn, rid, ei.value, env.notify) == "held"
    row = env.row(rid)
    assert row["state"] == "held" and row["held_reason"].startswith("Publish:") and "could not process" in row["held_reason"]
    assert env.st.publish_calls == 0
    notes = env.notifications()
    assert len(notes) == 1 and notes[0]["reel_id"] == rid and notes[0]["level"] == "warn"


# ------------------------------------------------------------------ 4. container EXPIRED
def test_expired_container_is_replaced_and_published_once(env):
    rid = env.reel()
    env.st.container_result = "EXPIRED"
    with pytest.raises(PublishDeferred):
        env.pub().publish(rid)
    row = env.row(rid)
    assert row["state"] == "publishing" and row["container_id"] is None and row["upload_done_at"] is None
    first = next(iter(env.st.containers))

    env.st.container_result = "FINISHED"
    env.pub().publish(rid)
    row = env.row(rid)
    assert row["state"] == "published" and env.st.publish_calls == 1
    assert len(env.st.containers) == 2 and row["container_id"] != first
    assert env.st.published_containers == [row["container_id"]]
    assert env.st.containers[first]["status"] == "EXPIRED"


def test_container_older_than_24h_is_replaced(env):
    rid = env.reel()
    env.st.polls_before_finished = 1000
    with pytest.raises(PublishDeferred):
        env.pub().publish(rid)
    first = env.row(rid)["container_id"]
    old = (db.utcnow() - timedelta(hours=25)).isoformat(timespec="seconds")
    db.update_reel(env.conn, rid, container_created_at=old)

    env.st.polls_before_finished = 2
    env.pub().publish(rid)
    row = env.row(rid)
    assert row["state"] == "published" and row["container_id"] != first
    assert len(env.st.containers) == 2 and env.st.published_containers == [row["container_id"]]
    assert env.st.publish_calls == 1


# ------------------------------------------------------------------ 5. poll timeout
def test_poll_timeout_defers_then_resumes_without_a_second_container(env):
    rid = env.reel()
    env.st.polls_before_finished = 1000
    with pytest.raises(PublishDeferred) as ei:
        env.pub().publish(rid)
    assert ei.value.delay_s == env.cfg["instagram"]["poll_interval_s"]
    assert "re-poll" in ei.value.reason
    row = env.row(rid)
    assert row["state"] == "publishing" and row["container_id"] and row["upload_done_at"]
    assert handle_publish_error(env.cfg, env.conn, rid, ei.value, env.notify) == "deferred"
    assert env.row(rid)["state"] == "publishing"
    polls_first = env.st.containers[row["container_id"]]["polls"]
    assert polls_first == 3  # t = 0, 5, 10 s with a 10 s timeout

    env.st.polls_before_finished = polls_first  # the next poll reports FINISHED
    env.pub().publish(rid)
    row = env.row(rid)
    assert row["state"] == "published" and env.st.publish_calls == 1
    assert len(env.st.containers) == 1
    assert len(env.requests_to("/ig-api-upload/", "POST")) == 1  # uploaded once


# ------------------------------------------------------------------ 6. error 190
@pytest.mark.parametrize("where", ["content_publishing_limit", "container", "ig-api-upload", "media_publish"])
def test_error_190_pauses_publishing_and_notifies_once(env, where):
    rid = env.reel()
    frag = f"{env.ig}/media" if where == "container" else where
    env.st.fail_next = [(frag, "190")]
    with pytest.raises(GraphError) as ei:
        env.pub().publish(rid)
    assert ei.value.kind == "auth" and ei.value.code == 190
    assert handle_publish_error(env.cfg, env.conn, rid, ei.value, env.notify) == "auth_needed"

    assert tokens.state(env.conn)["status"] == "auth_needed"
    assert env.cfg.pause_file.exists() and env.cfg.pause_file.read_text(encoding="utf-8").startswith("auth_needed")
    notes = env.notifications()
    assert len(notes) == 1 and notes[0]["level"] == "action" and "reel connect" in notes[0]["body"]
    assert env.row(rid)["state"] == ("scheduled" if where == "content_publishing_limit" else "publishing")
    assert env.st.publish_calls == 0

    # a second error doesn't notify again
    handle_publish_error(env.cfg, env.conn, rid, ei.value, env.notify)
    assert len(env.notifications()) == 1

    # the next tick's publish step is skipped entirely
    n_req = len(env.st.requests)
    out = jobs.publish_due(env.ctx(), now=db.utcnow() + timedelta(days=1))
    assert out["skipped"].startswith("paused: auth_needed")
    assert len(env.st.requests) == n_req
    assert VALID not in str([tuple(r) for r in env.conn.execute("SELECT * FROM token_state")])


# ------------------------------------------------------------------ 7. rate limit / HTTP 500
def test_rate_limit_defers_one_hour_without_counting_a_retry(env, monkeypatch):
    rid = env.reel()
    monkeypatch.setattr(jobs, "make_publisher", lambda ctx, force_dry=False: env.pub())
    env.st.fail_next = [("content_publishing_limit", "rate")]
    out = jobs.publish_due(env.ctx())
    assert out["outcomes"][rid] == "rate_limit" and out["published"] == []
    row = env.row(rid)
    assert row["state"] == "scheduled" and row["retries"] == 0
    assert _approx_from_now(row["next_attempt_at"], 3600)
    assert "rate_limit" in row["last_error"]

    # not due again before the hour is up
    assert jobs.publish_due(env.ctx(), now=db.utcnow() + timedelta(minutes=30))["due"] == 0
    # after the hour the reel is > 30 min past its slot: it gets a fresh slot instead of
    # posting off-slot (fact-check #7), so it is never published late
    later = db.utcnow() + timedelta(minutes=61)
    out = jobs.publish_due(env.ctx(), now=later)
    assert out["published"] == [] and "rescheduled" in out["outcomes"][rid]
    row = env.row(rid)
    assert row["state"] == "scheduled" and db.parse_ts(row["scheduled_at"]) > later
    assert env.st.publish_calls == 0


def test_http_500_is_retryable_with_backoff(env):
    rid = env.reel()
    base = env.cfg["engine"]["retry_base_minutes"] * 60
    for attempt in (1, 2):
        env.st.fail_next = [(f"{env.ig}/media", "500")]
        with pytest.raises(GraphError) as ei:
            env.pub().publish(rid)
        assert ei.value.kind == "retryable" and ei.value.http == 500
        assert handle_publish_error(env.cfg, env.conn, rid, ei.value, env.notify) == "retryable"
        row = env.row(rid)
        assert row["retries"] == attempt and row["state"] == "publishing"
        assert _approx_from_now(row["next_attempt_at"], base * 2 ** (attempt - 1))
    assert tokens.state(env.conn)["status"] != "auth_needed" and not env.cfg.pause_file.exists()
    assert env.st.containers == {}
    env.pub().publish(rid)
    assert env.row(rid)["state"] == "published" and len(env.st.containers) == 1 and env.st.publish_calls == 1


@pytest.mark.parametrize("http,err,kind", [
    (500, {"code": 2, "type": "OAuthException", "message": "unexpected error"}, "retryable"),
    (400, {"code": 9007, "error_subcode": 2207027, "type": "OAuthException", "message": "Media is not ready"}, "retryable"),
    (400, {"code": 1, "type": "OAuthException", "is_transient": True}, "retryable"),
    (400, {"code": 4, "type": "OAuthException"}, "rate_limit"),
    (429, {}, "rate_limit"),
    (400, {"code": 190, "error_subcode": 463, "type": "OAuthException"}, "auth"),
    (400, {"code": 9, "error_subcode": 2207042, "type": "OAuthException"}, "quota"),
    (400, {"code": 9, "error_subcode": 2207020, "type": "OAuthException"}, "expired"),
    (400, {"code": 100, "type": "OAuthException", "message": "Invalid parameter"}, "content"),
    (400, {"code": 12345, "type": "OAuthException"}, "auth"),
])
def test_classify(http, err, kind):
    assert classify(http, err) == kind


# ------------------------------------------------------------------ 8. network drop mid-publish
def test_network_drop_after_media_publish_never_double_posts(env):
    rid = env.reel()
    env.st.fail_next = [("media_publish", "drop_after")]
    with pytest.raises(GraphError) as ei:
        env.pub().publish(rid)
    assert ei.value.kind == "retryable"
    assert handle_publish_error(env.cfg, env.conn, rid, ei.value, env.notify) == "retryable"
    row = env.row(rid)
    assert row["state"] == "publishing" and row["publish_requested_at"] and row["media_id"] is None
    assert env.st.publish_calls == 1  # the server did publish

    env.pub().publish(rid)
    row = env.row(rid)
    assert row["state"] == "published"
    assert env.st.publish_calls == 1 and len(env.st.media) == 1 and len(env.st.containers) == 1
    assert row["media_id"] == next(iter(env.st.media))
    assert len(env.requests_to("media_publish", "POST")) == 1
    assert env.requests_to(f"{env.ig}/media", "GET")  # found via /{ig}/media by caption hash


# ------------------------------------------------------------------ 9. kill switch
def test_pause_before_media_publish_stops_the_post(env):
    rid = env.reel()
    env.clock.hooks.append(lambda: env.cfg.pause_file.write_text("paused by test\n", encoding="utf-8"))
    with pytest.raises(PublishDeferred) as ei:
        env.pub().publish(rid)
    assert "paused" in ei.value.reason
    assert env.requests_to("media_publish") == [] and env.st.publish_calls == 0
    row = env.row(rid)
    assert row["state"] == "publishing" and row["publish_requested_at"] is None
    assert jobs.publish_due(env.ctx(), now=db.utcnow() + timedelta(hours=1))["skipped"].startswith("paused")

    env.clock.hooks.clear()
    env.cfg.pause_file.unlink()
    env.pub().publish(rid)
    assert env.row(rid)["state"] == "published" and env.st.publish_calls == 1 and len(env.st.containers) == 1


# ------------------------------------------------------------------ 10. crash at every publish crashpoint
@pytest.mark.parametrize("point", PUBLISH_CRASHPOINTS)
def test_crash_at_every_publish_crashpoint_publishes_exactly_once(env, monkeypatch, point):
    rid = env.reel()
    monkeypatch.setenv("REELENGINE_CRASH_AT", point)
    with pytest.raises(SystemExit):
        env.pub().publish(rid)
    monkeypatch.delenv("REELENGINE_CRASH_AT")
    row = env.row(rid)
    assert row["state"] == "publishing"
    expect_calls = 1 if point == "after_media_publish" else 0
    assert env.st.publish_calls == expect_calls

    env.pub().publish(rid)
    row = env.row(rid)
    assert row["state"] == "published" and row["media_id"] in env.st.media
    assert env.st.publish_calls == 1 and len(env.st.media) == 1
    assert len(env.st.containers) <= 2
    if point not in ("before_container", "after_container"):
        assert len(env.st.containers) == 1
    assert env.st.published_containers == [row["container_id"]]
    # a third run is a no-op
    assert env.pub().publish(rid)["already"] is True and env.st.publish_calls == 1


def test_one_publisher_for_two_reels_uploads_each_video_to_its_own_container(env, monkeypatch):
    """publish_due(limit=2) reuses one publisher: reel B (container made before a crash)
    must not be uploaded to the upload URI of reel A's container."""
    b = env.reel()
    monkeypatch.setenv("REELENGINE_CRASH_AT", "after_container")
    with pytest.raises(SystemExit):
        env.pub().publish(b)
    monkeypatch.delenv("REELENGINE_CRASH_AT")
    a = env.reel()
    shared = env.pub()
    monkeypatch.setattr(jobs, "make_publisher", lambda ctx, force_dry=False: shared)

    out = jobs.publish_due(env.ctx(), limit=2)
    ca, cb = env.row(a)["container_id"], env.row(b)["container_id"]
    assert ca != cb
    assert env.st.containers[ca]["size"] == os.path.getsize(env.row(a)["out_path"])
    assert env.st.containers[cb]["uploaded"], "reel B's bytes went to another container"
    assert out["published"] == [a, b] and env.st.publish_calls == 2
    assert env.row(a)["state"] == env.row(b)["state"] == "published"


# ------------------------------------------------------------------ 11. insights
def test_insights_store_null_never_zero_and_no_token(env):
    rid = env.reel()
    env.pub().publish(rid)
    jobs.after_publish(env.ctx(), rid)
    pub_at = _ts(env.row(rid)["published_at"])

    res = insights.collect_due(env.cfg, env.conn, env.client(), now=pub_at + timedelta(hours=25))
    assert res == {"due": 3, "fetched": 3, "failed": 0}
    rows = list(env.conn.execute("SELECT * FROM insights WHERE reel_id=? ORDER BY checkpoint_h", (rid,)))
    assert [r["fetched_at"] is not None for r in rows] == [True, True, True, False, False, False]
    for r in rows[:3]:
        m = db.jload(r["metrics_json"])
        assert m["views"] == 1500 and m["ig_reels_avg_watch_time"] == 9100
        assert "reels_skip_rate" in m and m["reels_skip_rate"] is None           # empty data -> NULL
        assert m["crossposted_views"] is None and m["facebook_views"] is None   # unsupported group -> NULL
        assert 0 not in [v for k, v in m.items() if k in ("reels_skip_rate", "crossposted_views", "facebook_views")]
        assert r["error"] and "crossposted_views" in r["error"]
        assert VALID not in r["raw_json"] and "access_token" not in r["raw_json"]
    # a second pass at the same time fetches nothing new
    assert insights.collect_due(env.cfg, env.conn, env.client(), now=pub_at + timedelta(hours=25))["due"] == 0

    # the /media listing carries the token in paging.next; the stored form drops it
    listing = env.client().get(f"{env.ig}/media", params={"fields": "id,caption"})
    assert VALID in listing["paging"]["next"]
    assert VALID not in dumps_redacted(listing)

    acct = insights.collect_account(env.cfg, env.conn, env.client(), env.ig)
    assert acct["follower_count"] == env.st.followers
    row = env.conn.execute("SELECT * FROM account_insights").fetchone()
    assert row["follower_count"] == env.st.followers and db.jload(row["metrics_json"])["reach"] == 10

    assert [x for x in all_text_values(env.conn) if VALID in x[2]] == []


# ------------------------------------------------------------------ 12. tokens
def _token_state(conn, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE token_state SET {sets} WHERE id=1", tuple(fields.values()))  # nosec B608 - test


def test_refresh_when_due_stores_the_new_token(env):
    now = db.utcnow()
    _token_state(env.conn, status="ok", issued_at=(now - timedelta(days=10)).isoformat(timespec="seconds"),
                 expires_at=(now + timedelta(days=50)).isoformat(timespec="seconds"), ig_user_id=env.ig)
    res = tokens.refresh_if_due(env.cfg, env.conn, env.notify, gclient=env.client())
    assert res["refreshed"] is True
    new = secrets.get_secret(secrets.ACCESS_TOKEN)
    assert new != VALID and new == env.st.token
    st = tokens.state(env.conn)
    assert st["status"] == "ok" and _approx_from_now(st["issued_at"], 0) and _approx_from_now(st["last_refresh_at"], 0)
    assert _approx_from_now(st["expires_at"], 5183944)
    ref = [r for r in env.session.sent if "refresh_access_token" in r["url"]]
    assert len(ref) == 1 and ref[0]["params"]["grant_type"] == "ig_refresh_token"
    assert [x for x in all_text_values(env.conn) if VALID in x[2] or new in x[2]] == []

    # right after a refresh it is not due again
    assert tokens.refresh_if_due(env.cfg, env.conn, env.notify, gclient=env.client())["reason"] == "not due"
    assert len([r for r in env.session.sent if "refresh_access_token" in r["url"]]) == 1


def test_refresh_not_due_for_a_young_token(env):
    now = db.utcnow()
    _token_state(env.conn, status="ok", issued_at=(now - timedelta(days=2)).isoformat(timespec="seconds"),
                 expires_at=(now + timedelta(days=58)).isoformat(timespec="seconds"))
    assert tokens.refresh_if_due(env.cfg, env.conn, env.notify, gclient=env.client()) == {"refreshed": False, "reason": "not due"}
    assert env.requests_to("refresh_access_token") == []
    assert secrets.get_secret(secrets.ACCESS_TOKEN) == VALID


def test_refresh_with_an_invalid_token_needs_reconnect(env):
    secrets.set_secret(secrets.ACCESS_TOKEN, "IGAAbogusTokenThatTheMockRejects0000")
    now = db.utcnow()
    _token_state(env.conn, status="ok", issued_at=(now - timedelta(days=10)).isoformat(timespec="seconds"),
                 expires_at=(now + timedelta(days=50)).isoformat(timespec="seconds"))
    res = tokens.refresh_if_due(env.cfg, env.conn, env.notify)
    assert res["refreshed"] is False
    assert tokens.state(env.conn)["status"] == "auth_needed"
    assert env.cfg.pause_file.exists() and env.cfg.pause_file.read_text(encoding="utf-8").startswith("auth_needed")
    notes = env.notifications()
    assert len(notes) == 1 and notes[0]["level"] == "action"
    # auth_needed: no silent retry loop
    assert tokens.refresh_if_due(env.cfg, env.conn, env.notify)["reason"].startswith("auth_needed")
    assert len(env.requests_to("refresh_access_token")) == 1


def test_connect_stores_metadata_but_never_the_token(env):
    secrets.delete_secret(secrets.ACCESS_TOKEN)
    env.cfg.pause_file.parent.mkdir(parents=True, exist_ok=True)
    env.cfg.pause_file.write_text("auth_needed: token expired\n", encoding="utf-8")
    info = tokens.connect(env.cfg, env.conn, VALID, env.notify)
    assert info == {"ig_user_id": env.ig, "username": env.st.username, "account_type": env.st.account_type}
    st = tokens.state(env.conn)
    assert (st["status"], st["ig_user_id"], st["username"], st["account_type"]) == ("ok", env.ig, env.st.username, env.st.account_type)
    assert st["issued_at"] and st["expires_at"]
    assert secrets.get_secret(secrets.ACCESS_TOKEN) == VALID
    assert not env.cfg.pause_file.exists()  # an auth_needed pause is lifted by a successful connect
    assert tokens.ig_user_id(env.cfg, env.conn) == env.ig
    hits = [(t, c) for t, c, v in all_text_values(env.conn) if VALID in v]
    assert hits == []


def test_connect_with_a_bad_token_stores_nothing(env):
    secrets.delete_secret(secrets.ACCESS_TOKEN)
    with pytest.raises(GraphError) as ei:
        tokens.connect(env.cfg, env.conn, "IGAAbogusTokenThatTheMockRejects0000", env.notify)
    assert ei.value.kind == "auth"
    assert not secrets.has_secret(secrets.ACCESS_TOKEN)
    assert tokens.state(env.conn)["status"] == "missing"


# ------------------------------------------------------------------ 13. dry-run publisher
@pytest.fixture
def dry(cfg, conn, tmp_path):
    e = Env(cfg, conn, None, FakeClock(), tmp_path)
    return e


def _dry_logs(cfg):
    d = cfg.data_dir / "out" / "dryrun"
    return sorted(d.glob("*.json")) if d.exists() else []


def test_dry_run_publisher_logs_once_and_is_idempotent(dry):
    rid = dry.reel()
    res = DryRunPublisher(dry.cfg, dry.conn).publish(rid)
    row = dry.row(rid)
    assert row["state"] == "published" and row["dry_run"] == 1 and row["media_id"] == f"dryrun-{rid}"
    assert res["dry_run"] is True
    logs = _dry_logs(dry.cfg)
    assert [p.name for p in logs] == [f"{rid:05d}.json"]
    rec = db.jload(logs[0].read_text(encoding="utf-8"))
    wc = rec["would_create_container"]
    assert wc["media_type"] == "REELS" and wc["upload_type"] == "resumable" and wc["thumb_offset"] == 1500 and wc["caption"] == CAPTION
    assert rec["would_upload"]["bytes"] == 4096 and rec["caption_hash"] == publisher.caption_hash(CAPTION)
    before = logs[0].read_bytes()

    again = DryRunPublisher(dry.cfg, dry.conn).publish(rid)
    assert again["already"] is True
    assert _dry_logs(dry.cfg) == logs and logs[0].read_bytes() == before
    assert dry.conn.execute("SELECT COUNT(*) FROM events WHERE reel_id=? AND to_state='published'", (rid,)).fetchone()[0] == 1


@pytest.mark.parametrize("point", ["dryrun_after_publishing", "dryrun_after_log"])
def test_dry_run_crash_then_rerun_leaves_one_log(dry, monkeypatch, point):
    rid = dry.reel()
    monkeypatch.setenv("REELENGINE_CRASH_AT", point)
    with pytest.raises(SystemExit):
        DryRunPublisher(dry.cfg, dry.conn).publish(rid)
    monkeypatch.delenv("REELENGINE_CRASH_AT")
    assert dry.row(rid)["state"] == "publishing"
    assert len(_dry_logs(dry.cfg)) == (1 if point == "dryrun_after_log" else 0)

    DryRunPublisher(dry.cfg, dry.conn).publish(rid)
    row = dry.row(rid)
    assert row["state"] == "published" and row["dry_run"] == 1
    assert len(_dry_logs(dry.cfg)) == 1
