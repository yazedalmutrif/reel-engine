"""End-to-end (spec §12 "Integration"): the synthetic Arabic clip through the whole
pipeline in dry-run.

  pytest -m e2e tests/test_e2e.py                 # FakeBrain (no subscription use), GPU + ffmpeg
  pytest -m "e2e and brain" tests/test_e2e.py     # real Claude CLI brain (3 calls)

Assertions: intake waits for a stable size, ffprobe specs of the output, the edit made
it shorter (false start / filler / bad retake removed), captions burned in (accent
pixels), QA passed, a slot assigned, the dry-run publish logged, a rerun doesn't
publish twice, and the vault log line exists (in the preview folder). Frames are saved
to docs/qa/e2e/ for visual inspection.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from reelengine import brain, db, intake, jobs, pipeline, render
from reelengine.config import load_config
from reelengine.probe import probe

ROOT = Path(__file__).resolve().parents[1]
CLIP = ROOT / "tests" / "fixtures" / "synthetic_ar.mov"
QA_DIR = ROOT / "docs" / "qa" / "e2e"


def _fake_brain():
    def edit_plan(prompt: str, schema: dict) -> dict:
        # keep every segment that isn't the false start / filler / abandoned retake / meta talk
        segs = []
        for line in prompt.splitlines():
            if not line.startswith("[S"):
                continue
            sid = int(line.split()[0][2:])
            text = line.split("] ", 1)[1]
            if any(k in text for k in ("طيب", "إمم", "يعني", "جرب", "لحظة", "أعيدها")):
                continue
            segs.append(sid)
        return {"keep_segments": segs, "drop_word_ids": [], "hook": {"spoken": "أول خطوة، سوّ CV واضح", "on_screen": "3 خطوات قبل ما تقدم"},
                "title_card": "3 خطوات قبل التقديم", "pillar": "job_hunt", "topic": "3 steps before applying", "hook_type": "list_number",
                "cover_word_id": None, "risk_flags": [], "caption_fixes": [], "notes": "fake"}

    def caption(prompt: str, schema: dict) -> dict:
        n = int(prompt.split("exactly ")[1].split("**")[0])
        opts = [] if "he already SAYS" in prompt else [ln.strip()[2:] for ln in prompt.split("or \"\" if the type is none:")[1].split("- Hashtags")[0].splitlines() if ln.strip().startswith("- ")]
        tags = ["#وظائف", "#خريجين", "#AI", "#توظيف", "#LinkedIn"][:n]
        return {"first_line": "3 خطوات تسويها قبل ما تقدم على أي وظيفة", "body": "سوّ CV واضح، حدّث LinkedIn، وابن مشروع صغير.",
                "cta_text": opts[0] if opts else "", "hashtags": tags, "keywords": ["CV", "LinkedIn", "وظائف"]}

    def critique(prompt: str, schema: dict) -> dict:
        return {"scores": {"hook": 8, "clarity": 8, "value": 8, "accuracy": 9}, "dialect_ok": True, "spelling_issues": [],
                "claims_about_yazeed": [], "violations": [], "summary": "fake", "fixes": []}

    return brain.FakeBrain({"edit_plan": edit_plan, "caption": caption, "critique": critique})


def _run(tmp_path: Path, use_real_brain: bool) -> None:
    assert CLIP.exists(), "run scripts/make_test_clip.py first"
    data = tmp_path / "data"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    cfg = load_config(overrides={"paths": {"data_dir": str(data), "inbox": str(inbox)}, "notify": {"toast": False},
                                 "vault": {"enabled": False}, "engine": {"mode": "dry-run"}})
    brain.set_provider(None if use_real_brain else _fake_brain())
    try:
        conn = db.connect(cfg.db_path)
        ctx = pipeline.Ctx.create(cfg, conn, toast=False)
        shutil.copy2(CLIP, inbox / "IMG_0001.MOV")
        # intake: size must be stable for 2 ticks before the move
        assert intake.scan(cfg, conn)["taken"] == []
        assert intake.scan(cfg, conn)["taken"] == []
        taken = intake.scan(cfg, conn)["taken"]
        assert len(taken) == 1 and not (inbox / "IMG_0001.MOV").exists()
        rid = taken[0]
        state = pipeline.process_reel(ctx, rid)
        row = db.get_reel(conn, rid)
        qa = db.jload(row["qa_json"]) or {}
        assert state == "scheduled", f"state {state}: {row['held_reason']} | {row['last_error']} | {qa.get('hard_fails')}"
        # output specs
        out = probe(cfg.ffprobe, Path(row["out_path"]))
        assert (out["width"], out["height"], out["fps"], out["vcodec"], out["pix_fmt"]) == (1080, 1920, 30.0, "h264", "yuv420p")
        assert out["acodec"] == "aac" and out["audio_rate"] == 48000
        assert out["duration"] < row["src_duration_s"] - 10, "the edit should remove the false start, filler and bad retake"
        checks = {c["name"]: c for c in qa["checks"]}
        for name in ("captions_burned", "loudness", "faststart", "no_edit_list", "resolution", "duration_band"):
            assert checks[name]["ok"], checks[name]
        assert row["scheduled_at"] and row["slot_arm"]
        # frames for visual inspection
        QA_DIR.mkdir(parents=True, exist_ok=True)
        tag = "real-brain" if use_real_brain else "fake-brain"
        ow = json.loads((pipeline.work_dir(cfg, rid) / "words_out.json").read_text(encoding="utf-8"))
        times = [0.8] + [(w["start"] + w["end"]) / 2 for w in ow[3::max(1, len(ow) // 6)]][:6]
        render.extract_frames(cfg, Path(row["out_path"]), times, QA_DIR, prefix=tag)
        shutil.copy2(row["thumb_path"], QA_DIR / f"{tag}-thumbnail.png")
        shutil.copy2(row["out_path"], QA_DIR / f"{tag}-sample-reel.mp4")
        (QA_DIR / f"{tag}-caption.txt").write_text(db.jload(row["caption_json"])["caption"], encoding="utf-8")
        (QA_DIR / f"{tag}-qa.json").write_text(json.dumps(qa, ensure_ascii=False, indent=1), encoding="utf-8")
        # dry-run publish at the slot, then a rerun must not publish again
        when = db.parse_ts(row["scheduled_at"]) + timedelta(minutes=1)
        res = jobs.publish_due(ctx, now=when)
        assert res["published"] == [rid], res
        row = db.get_reel(conn, rid)
        assert row["state"] == "published" and row["dry_run"] == 1 and row["media_id"] == f"dryrun-{rid}"
        assert (cfg.data_dir / "out" / "dryrun" / f"{rid:05d}.json").exists()
        assert jobs.publish_due(ctx, now=when + timedelta(minutes=15))["published"] == []
        log = cfg.data_dir / "vault_preview" / "12 Social Media" / "Reels Log.md"
        assert log.exists() and f"#{rid}" in log.read_text(encoding="utf-8")
        plan = db.jload(row["plan_json"])
        tr = json.loads(Path(row["transcript_path"]).read_text(encoding="utf-8"))
        ev = [(r["to_state"], db.parse_ts(r["ts"])) for r in conn.execute("SELECT to_state, ts FROM events WHERE reel_id=? AND to_state IS NOT NULL ORDER BY id", (rid,))]
        stage_s = {ev[k][0]: round((ev[k][1] - ev[k - 1][1]).total_seconds(), 1) for k in range(1, len(ev))}
        (QA_DIR / f"{tag}-summary.json").write_text(json.dumps({
            "reel": rid, "source_s": row["src_duration_s"], "output_s": out["duration"], "cuts": plan["cuts"],
            "keep_segments": plan["keep_segments"], "segments_total": plan.get("segments_total"), "caption_fixes": plan["caption_fixes"],
            "hook_on_screen": plan["hook"]["on_screen"],
            "transcription": {"device": tr["device"], "elapsed_s": tr["elapsed_s"], "words": len(tr["words"])},
            "stage_seconds_to_reach_state": stage_s,
            "loudness_lufs": qa["measures"]["integrated_lufs"], "true_peak_dbfs": qa["measures"]["true_peak_dbfs"],
            "scores": qa.get("scores"), "scheduled_at": row["scheduled_at"], "slot": row["slot_arm"], "media_id": row["media_id"],
            "brain_calls": [dict(r) for r in conn.execute("SELECT task, attempts, ok, duration_s FROM brain_calls")],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        brain.set_provider(None)


@pytest.mark.e2e
def test_e2e_fake_brain(tmp_path):
    _run(tmp_path, use_real_brain=False)


@pytest.mark.e2e
@pytest.mark.brain
def test_e2e_real_brain(tmp_path):
    if os.environ.get("REELENGINE_SKIP_BRAIN"):
        pytest.skip("REELENGINE_SKIP_BRAIN set")
    _run(tmp_path, use_real_brain=True)
