"""The reel state machine (spec §4, §2.5, §12 "the state machine and idempotency (a
crash at every step)").

The heavy stage internals (ffprobe, ffmpeg, whisper, the renderer, QA measurements)
are replaced by fast fakes that write the same artifacts and fields, and the brain is a
FakeBrain, so these tests exercise the real stage functions, transitions, crashpoints
and retry logic without a GPU, ffmpeg or a Claude call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from reelengine import brain, db, pipeline, qa, transcribe
from reelengine.publisher import backoff_s

PIPELINE_CRASHPOINTS = [
    ("after_transcript", "new"),
    ("after_plan_brain", "transcribed"),
    ("after_render_output", "planned"),
    ("after_caption_persist", "rendered"),
    ("after_qa", "rendered"),
]
FLOW = ["transcribed", "planned", "rendered", "qa_passed", "scheduled"]

SENTENCES = [
    "أول خطوة: سوّ CV واضح.",
    "ثاني خطوة: حدّث LinkedIn كل أسبوع.",
    "ثالث خطوة: ابن مشروع صغير وحطه في GitHub.",
]


def make_transcript(n_words: int | None = None) -> dict:
    words, t = [], 0.5
    for seg, sentence in enumerate(SENTENCES):
        for w in sentence.split():
            words.append({"i": len(words), "start": round(t, 3), "end": round(t + 0.4, 3), "text": w, "prob": 0.95, "seg": seg})
            t += 0.5
        t += 0.8
    if n_words is not None:
        words = words[:n_words]
    return {"language": "ar", "language_probability": 0.99, "duration": round(t, 3), "device": "cpu", "model": "fake",
            "elapsed_s": 0.01, "words": words, "segments": []}


def _fake_brain() -> brain.FakeBrain:
    def edit_plan(prompt: str, schema: dict) -> dict:
        segs = [int(ln.split()[0][2:]) for ln in prompt.splitlines() if ln.startswith("[S")]
        return {"keep_segments": segs, "drop_word_ids": [], "hook": {"spoken": "أول خطوة سوّ CV واضح", "on_screen": "3 خطوات قبل ما تقدم"},
                "title_card": "3 خطوات قبل التقديم", "pillar": "job_hunt", "topic": "3 steps before applying", "hook_type": "list_number",
                "cover_word_id": None, "risk_flags": [], "caption_fixes": [], "notes": "fake"}

    def caption(prompt: str, schema: dict) -> dict:
        n = int(prompt.split("exactly ")[1].split("**")[0])
        opts = [] if "he already SAYS" in prompt else [
            ln.strip()[2:] for ln in prompt.split("or \"\" if the type is none:")[1].split("- Hashtags")[0].splitlines() if ln.strip().startswith("- ")]
        tags = ["#وظائف", "#خريجين", "#AI", "#توظيف", "#LinkedIn"][:n]
        return {"first_line": "3 خطوات تسويها قبل ما تقدم على أي وظيفة", "body": "سوّ CV واضح، حدّث LinkedIn، وابن مشروع صغير.",
                "cta_text": opts[0] if opts else "", "hashtags": tags, "keywords": ["CV", "LinkedIn", "وظائف"]}

    return brain.FakeBrain({"edit_plan": edit_plan, "caption": caption})


@dataclass
class Pipe:
    cfg: Any
    conn: Any
    ctx: Any
    fake_brain: brain.FakeBrain
    raw_dir: Path
    calls: dict = field(default_factory=lambda: {"probe": 0, "ffmpeg": 0, "transcribe": 0, "render": 0, "qa": 0})
    n_words: int | None = None
    has_audio: bool = True
    transcribe_error: Exception | None = None
    qa_pass: bool = True
    _n: int = 0

    def reel(self) -> int:
        self._n += 1
        raw = self.raw_dir / f"clip{self._n}.mov"
        raw.write_bytes(b"not really a video")
        return db.insert_reel(self.conn, f"{self._n:02d}" + "ab" * 31, raw.name, str(raw))

    def row(self, rid: int):
        return db.get_reel(self.conn, rid)

    def brain_calls(self, task: str) -> int:
        return sum(1 for t, _ in self.fake_brain.calls if t == task)

    def transitions(self, rid: int) -> list[tuple[str | None, str | None]]:
        return [(r["from_state"], r["to_state"]) for r in self.conn.execute(
            "SELECT from_state, to_state FROM events WHERE reel_id=? AND to_state IS NOT NULL ORDER BY id", (rid,))]

    def notifications(self, rid: int | None = None) -> list:
        return list(self.conn.execute("SELECT * FROM notifications WHERE reel_id IS ? ORDER BY id", (rid,)))


@pytest.fixture
def pipe(cfg, conn, monkeypatch, tmp_path):
    raw_dir = tmp_path / "rawsrc"
    raw_dir.mkdir()
    fb = _fake_brain()
    brain.set_provider(fb)
    p = Pipe(cfg, conn, pipeline.Ctx.create(cfg, conn, toast=False), fb, raw_dir)

    def fake_probe(ffprobe, media):
        p.calls["probe"] += 1
        is_out = Path(media).suffix == ".mp4"
        dur = 9.3 if is_out else 14.0
        return {"duration": dur, "width": 1080, "height": 1920, "display_width": 1080, "display_height": 1920, "rotation": 0,
                "fps": 30.0, "vcodec": "h264", "pix_fmt": "yuv420p", "color_transfer": None, "is_hdr": False, "has_video": True,
                "has_audio": p.has_audio, "audio_rate": 48000, "audio_channels": 1, "size_bytes": 1000}

    def fake_ffmpeg(binary, args, timeout=None):
        p.calls["ffmpeg"] += 1
        Path(args[-1]).write_bytes(b"RIFFfakewav")

    def fake_transcribe(cfg_, wav):
        p.calls["transcribe"] += 1
        if p.transcribe_error is not None:
            raise p.transcribe_error
        return make_transcript(p.n_words)

    def fake_render(cfg_, raw, probe_, plan, ow, work, out, thumb, style_overrides=None, keep_together=()):
        p.calls["render"] += 1
        out.write_bytes(b"fake mp4 " * 64)
        thumb.write_bytes(b"\x89PNG fake")
        return {"cover_t_out": 1.1, "cuts": plan.get("cuts", 0), "chunks": 6, "style_overrides": dict(style_overrides or {})}

    def fake_evaluate(cfg_, conn_, brand, *, out, plan, out_words, caption, render_info, reel_id, run_critique=True):
        p.calls["qa"] += 1
        assert Path(out).exists() and caption["caption"]
        fails = [] if p.qa_pass else ["duration_band: 3.0 s is below 5 s"]
        return {"passed": not fails, "hard_fails": fails, "checks": [], "scores": {"hook": 8, "clarity": 8, "value": 8, "accuracy": 9},
                "critique": {}, "measures": {}, "at": db.now_iso()}

    monkeypatch.setattr(pipeline, "probe", fake_probe)
    monkeypatch.setattr(pipeline, "ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(transcribe, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline, "render_reel", fake_render)
    monkeypatch.setattr(qa, "evaluate", fake_evaluate)
    try:
        yield p
    finally:
        brain.set_provider(None)


# ------------------------------------------------------------------ db.transition
def _new_reel(conn, sha="s1", **fields) -> int:
    return db.insert_reel(conn, sha, "c.mov", "raw/c.mov", **fields)


def test_transition_rejects_illegal_and_unknown_states(conn):
    rid = _new_reel(conn)
    with pytest.raises(db.TransitionError, match="illegal transition new -> published"):
        db.transition(conn, rid, "published", media_id="123")
    with pytest.raises(db.TransitionError, match="unknown state"):
        db.transition(conn, rid, "posted")
    with pytest.raises(db.TransitionError, match="not found"):
        db.transition(conn, 999, "transcribed")
    row = db.get_reel(conn, rid)
    assert row["state"] == "new" and row["media_id"] is None  # a rejected transition writes nothing
    assert conn.execute("SELECT COUNT(*) FROM events WHERE reel_id=?", (rid,)).fetchone()[0] == 1  # only the intake event
    assert not conn.in_transaction
    with pytest.raises(ValueError):
        db.update_reel(conn, rid, state="published")


def test_transition_enforces_expect_from_and_writes_one_event_each(conn):
    rid = _new_reel(conn)
    with pytest.raises(db.TransitionError, match="expected"):
        db.transition(conn, rid, "planned", expect_from="transcribed")
    db.transition(conn, rid, "transcribed", "t", expect_from="new", transcript_path="x.json")
    db.transition(conn, rid, "planned", "p", expect_from=("transcribed", "held"))
    with pytest.raises(db.TransitionError):
        db.transition(conn, rid, "rendered", expect_from="transcribed")  # it is planned now
    ev = [(r["from_state"], r["to_state"], r["detail"]) for r in conn.execute("SELECT * FROM events WHERE reel_id=? ORDER BY id", (rid,))]
    assert ev == [(None, "new", "intake c.mov"), ("new", "transcribed", "t"), ("transcribed", "planned", "p")]
    row = db.get_reel(conn, rid)
    assert row["state"] == "planned" and row["transcript_path"] == "x.json"


def test_transition_inside_an_outer_transaction_rolls_back_with_it(conn):
    rid = _new_reel(conn)
    with pytest.raises(RuntimeError):
        with db.tx(conn):
            db.transition(conn, rid, "transcribed")
            raise RuntimeError("crash before commit")
    assert db.get_reel(conn, rid)["state"] == "new"
    assert conn.execute("SELECT COUNT(*) FROM events WHERE reel_id=?", (rid,)).fetchone()[0] == 1


def test_held_reason_is_cleared_when_leaving_held(conn):
    rid = _new_reel(conn)
    db.transition(conn, rid, "held", "x", held_reason="QA: too quiet")
    assert db.get_reel(conn, rid)["held_reason"] == "QA: too quiet"
    db.transition(conn, rid, "new", "approve")
    assert db.get_reel(conn, rid)["held_reason"] is None


def test_real_published_reel_cannot_be_requeued_but_dry_run_can(conn):
    real = _new_reel(conn, "real", state="published", dry_run=0, media_id="1790001")
    with pytest.raises(db.TransitionError, match="really published"):
        db.transition(conn, real, "qa_passed", "requeue")
    assert db.get_reel(conn, real)["state"] == "published"
    for bad in ("scheduled", "publishing", "held", "failed", "new"):
        with pytest.raises(db.TransitionError):
            db.transition(conn, real, bad)

    dry = _new_reel(conn, "dry", state="published", dry_run=1, media_id="dryrun-2")
    db.transition(conn, dry, "qa_passed", "re-queued after a dry-run rehearsal", media_id=None, dry_run=0)
    row = db.get_reel(conn, dry)
    assert row["state"] == "qa_passed" and row["media_id"] is None and row["dry_run"] == 0


# ------------------------------------------------------------------ pipeline happy path
def test_process_reel_runs_every_stage_once(pipe):
    rid = pipe.reel()
    assert pipeline.process_reel(pipe.ctx, rid) == "scheduled"
    row = pipe.row(rid)
    assert row["scheduled_at"] and row["slot_arm"] and row["out_path"] and Path(row["transcript_path"]).exists()
    assert db.jload(row["qa_json"])["passed"] is True and db.jload(row["caption_json"])["caption"]
    assert [to for _, to in pipe.transitions(rid)] == ["new"] + FLOW
    assert pipe.calls["transcribe"] == pipe.calls["render"] == pipe.calls["qa"] == 1
    assert pipe.brain_calls("edit_plan") == 1 and pipe.brain_calls("caption") == 1
    # a rerun does nothing (scheduled is the pipeline's stop state; publishing is jobs.publish_due)
    assert pipeline.process_reel(pipe.ctx, rid) == "scheduled"
    assert pipe.calls["render"] == 1 and len(pipe.transitions(rid)) == 6


# ------------------------------------------------------------------ crash at every pipeline crashpoint
@pytest.mark.parametrize("point,state_at_crash", PIPELINE_CRASHPOINTS)
def test_crash_at_every_pipeline_crashpoint_resumes(pipe, monkeypatch, point, state_at_crash):
    rid = pipe.reel()
    monkeypatch.setenv("REELENGINE_CRASH_AT", point)
    with pytest.raises(SystemExit):
        pipeline.process_reel(pipe.ctx, rid)
    monkeypatch.delenv("REELENGINE_CRASH_AT")
    row = pipe.row(rid)
    assert row["state"] == state_at_crash  # the transition after the side effect was never written
    assert not pipe.conn.in_transaction
    done = 0 if state_at_crash == "new" else FLOW.index(state_at_crash) + 1
    assert [to for _, to in pipe.transitions(rid)] == ["new"] + FLOW[:done]
    if point == "after_caption_persist":
        assert db.jload(row["caption_json"])["caption"] and row["caption_hash"]

    assert pipeline.process_reel(pipe.ctx, rid) == "scheduled"
    assert pipe.conn.execute("SELECT COUNT(*) FROM reels").fetchone()[0] == 1
    assert [to for _, to in pipe.transitions(rid)] == ["new"] + FLOW  # every transition exactly once
    assert pipe.calls["transcribe"] == (2 if point == "after_transcript" else 1)
    assert pipe.brain_calls("edit_plan") == (2 if point == "after_plan_brain" else 1)
    assert pipe.calls["render"] == (2 if point == "after_render_output" else 1)
    assert pipe.brain_calls("caption") == 1  # persisted before QA, never regenerated
    assert pipe.calls["qa"] == (2 if point == "after_qa" else 1)
    assert len(list(pipe.cfg.dir("out").glob("*.mp4"))) == 1


# ------------------------------------------------------------------ failures, retries, held
def test_failing_stage_retries_with_backoff_then_holds(pipe):
    rid = pipe.reel()
    pipe.transcribe_error = RuntimeError("CUDA fell over")
    max_retries = pipe.cfg["engine"]["max_retries"]
    base = pipe.cfg["engine"]["retry_base_minutes"]
    for attempt in range(1, max_retries + 1):
        assert pipeline.process_reel(pipe.ctx, rid) == "failed"
        row = pipe.row(rid)
        assert row["failed_from"] == "new" and row["retries"] == attempt
        assert "RuntimeError: CUDA fell over" in row["last_error"]
        due = db.parse_ts(row["next_attempt_at"])
        assert abs((due - db.utcnow()).total_seconds() - backoff_s(attempt - 1, base)) < 60
        assert pipeline.retry_failed(pipe.ctx) == []  # not due yet
        assert pipeline.retry_failed(pipe.ctx, now=due + timedelta(seconds=1)) == [rid]
        assert pipe.row(rid)["state"] == "new"
        db.update_reel(pipe.conn, rid, next_attempt_at=(db.utcnow() - timedelta(seconds=1)).isoformat(timespec="seconds"))

    assert pipeline.process_reel(pipe.ctx, rid) == "held"
    row = pipe.row(rid)
    assert row["retries"] == max_retries + 1 and row["failed_from"] == "new"
    assert row["held_reason"].startswith(f"Failed {max_retries + 1} times at 'new'")
    notes = pipe.notifications(rid)
    assert len(notes) == 1 and notes[0]["level"] == "error"
    assert pipe.calls["transcribe"] == max_retries + 1


def test_retry_failed_resumes_at_the_failed_stage(pipe, monkeypatch):
    rid = pipe.reel()
    assert pipeline.process_reel(pipe.ctx, rid, stop_at="planned") == "planned"
    working_render = pipeline.render_reel

    def broken_render(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline, "render_reel", broken_render)
    assert pipeline.process_reel(pipe.ctx, rid) == "failed"
    row = pipe.row(rid)
    assert row["failed_from"] == "planned" and "OSError: disk full" in row["last_error"]

    monkeypatch.setattr(pipeline, "render_reel", working_render)
    db.update_reel(pipe.conn, rid, next_attempt_at=(db.utcnow() - timedelta(seconds=1)).isoformat(timespec="seconds"))
    assert pipeline.retry_failed(pipe.ctx) == [rid]
    assert pipe.row(rid)["state"] == "planned"
    assert pipeline.process_reel(pipe.ctx, rid) == "scheduled"
    assert pipe.calls["transcribe"] == 1 and pipe.brain_calls("edit_plan") == 1


@pytest.mark.parametrize("knob,reason", [("n_words", "No speech detected"), ("has_audio", "Clip rejected: no audio track")])
def test_held_exception_holds_and_notifies(pipe, knob, reason):
    setattr(pipe, knob, 2 if knob == "n_words" else False)
    rid = pipe.reel()
    assert pipeline.process_reel(pipe.ctx, rid) == "held"
    row = pipe.row(rid)
    assert row["held_reason"].startswith(reason) and row["retries"] == 0
    notes = pipe.notifications(rid)
    assert len(notes) == 1 and notes[0]["level"] == "warn" and reason in notes[0]["body"]
    assert pipe.transitions(rid) == [(None, "new"), ("new", "held")]


def test_qa_fail_holds_with_reason_and_notification(pipe):
    pipe.qa_pass = False
    rid = pipe.reel()
    assert pipeline.process_reel(pipe.ctx, rid) == "held"
    row = pipe.row(rid)
    assert row["held_reason"].startswith("QA:") and "duration_band" in row["held_reason"]
    assert db.jload(row["qa_json"])["passed"] is False
    assert [n["title"] for n in pipe.notifications(rid)] == ["Reel held by QA"]


# ------------------------------------------------------------------ approve / rerender
def test_approve_reenters_a_qa_held_reel_at_rendered(pipe):
    pipe.qa_pass = False
    rid = pipe.reel()
    assert pipeline.process_reel(pipe.ctx, rid) == "held"
    assert pipeline.reentry_state(pipe.row(rid)) == "rendered"
    pipe.qa_pass = True
    assert pipeline.approve(pipe.ctx, rid) == "scheduled"
    assert ("held", "rendered") in pipe.transitions(rid)
    assert pipe.calls["render"] == 1 and pipe.calls["qa"] == 2 and pipe.calls["transcribe"] == 1
    assert pipe.row(rid)["held_reason"] is None and pipe.row(rid)["retries"] == 0


def test_approve_reenters_an_early_held_reel_at_new(pipe):
    pipe.n_words = 2
    rid = pipe.reel()
    assert pipeline.process_reel(pipe.ctx, rid) == "held"
    assert pipeline.reentry_state(pipe.row(rid)) == "new"
    pipe.n_words = None
    assert pipeline.approve(pipe.ctx, rid) == "scheduled"
    assert ("held", "new") in pipe.transitions(rid)


def test_approve_reenters_after_transcript_at_transcribed(pipe):
    rid = pipe.reel()
    assert pipeline.process_reel(pipe.ctx, rid, stop_at="transcribed") == "transcribed"
    db.transition(pipe.conn, rid, "held", "manual hold", held_reason="checking the plan")
    assert pipeline.reentry_state(pipe.row(rid)) == "transcribed"
    assert pipeline.approve(pipe.ctx, rid) == "scheduled"
    assert ("held", "transcribed") in pipe.transitions(rid)
    assert pipe.calls["transcribe"] == 1


def test_approve_refuses_a_reel_that_is_not_held(pipe):
    rid = pipe.reel()
    with pytest.raises(ValueError, match="not held"):
        pipeline.approve(pipe.ctx, rid)


def test_rerender_reruns_render_and_qa_with_overrides(pipe):
    rid = pipe.reel()
    assert pipeline.process_reel(pipe.ctx, rid) == "scheduled"
    assert pipeline.rerender(pipe.ctx, rid, {"font_size": 110}) == "scheduled"
    row = pipe.row(rid)
    assert db.jload(row["style_overrides_json"]) == {"font_size": 110}
    assert pipe.calls["render"] == 2 and pipe.calls["qa"] == 2 and pipe.brain_calls("caption") == 1
    assert ("scheduled", "planned") in pipe.transitions(rid)


@pytest.mark.parametrize("state,dry_run", [("publishing", 0), ("published", 0), ("published", 1)])
def test_rerender_refuses_publishing_and_published(pipe, state, dry_run):
    rid = db.insert_reel(pipe.conn, f"x{state}{dry_run}", "c.mov", "raw/c.mov", state=state, dry_run=dry_run, plan_json=db.jdump({"cuts": 1}))
    with pytest.raises(ValueError, match="can't be re-rendered"):
        pipeline.rerender(pipe.ctx, rid, {"font_size": 110})
    assert pipe.row(rid)["state"] == state and pipe.calls["render"] == 0


def test_rerender_refuses_without_a_plan(pipe):
    rid = pipe.reel()
    with pytest.raises(ValueError, match="no edit plan"):
        pipeline.rerender(pipe.ctx, rid, {})
