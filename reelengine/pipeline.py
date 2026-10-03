"""The per-reel state machine (spec §4): new → transcribed → planned → rendered →
qa_passed | held → scheduled → (publishing → published, in jobs.publish_due).

Each stage reads only stored inputs + artifacts in data/work/<id>/, writes its
artifacts first, then persists the state transition in one transaction. A crash
anywhere leaves the reel in its previous state and a rerun recomputes that stage.
Errors: ProbeReject / QA fail / too long or short -> held (reason + notification);
any other exception -> failed (retry with backoff); after engine.max_retries -> held.
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from . import brain, db, learner, qa, scheduler
from .edit_plan import build_phrases, output_words, phrases_for_prompt, tighten, validate_plan, words_per_minute
from .ffmpeg import ffmpeg
from .gates import Brand, facts_for_prompt, hashtags_of, load_brand, run_content_gates
from .log import get_logger, redact
from .notify import notifier
from .probe import ProbeReject, check_usable, probe
from .publisher import backoff_s, caption_hash, crashpoint
from .render import render_reel

log = get_logger("pipeline")

PRE_PUBLISH = ("new", "transcribed", "planned", "rendered", "qa_passed")


class Held(Exception):
    """Stop processing this reel and hold it with this reason."""


@dataclass
class Ctx:
    cfg: Any
    conn: Any
    brand: Brand
    notify: Callable[..., Any]
    rng: random.Random = field(default_factory=random.Random)

    @classmethod
    def create(cls, cfg, conn, toast: bool | None = None) -> "Ctx":
        return cls(cfg, conn, load_brand(cfg.brand_dir), notifier(cfg, conn, toast))


def work_dir(cfg, rid: int) -> Path:
    p = cfg.dir("work") / f"{rid:05d}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _row(ctx: Ctx, rid: int):
    return db.get_reel(ctx.conn, rid)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ stages
def stage_transcribe(ctx: Ctx, rid: int) -> None:
    from .transcribe import save, transcribe

    cfg, row = ctx.cfg, _row(ctx, rid)
    raw = Path(row["raw_path"])
    info = probe(cfg.ffprobe, raw)
    try:
        check_usable(info, cfg["intake"]["min_duration_s"])
    except ProbeReject as exc:
        raise Held(f"Clip rejected: {exc}") from None
    wd = work_dir(cfg, rid)
    wav = wd / "audio16k.wav"
    ffmpeg(cfg.ffmpeg, ["-i", str(raw), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", str(wav)], timeout=900)
    tr = transcribe(cfg, wav)
    tpath = wd / "transcript.json"
    save(tr, tpath)
    crashpoint("after_transcript")
    if len(tr["words"]) < 3:
        raise Held("No speech detected in the clip (fewer than 3 words transcribed)")
    db.transition(ctx.conn, rid, "transcribed", f"{len(tr['words'])} words on {tr['device']} in {tr['elapsed_s']} s",
                  expect_from="new", probe_json=db.jdump(info), transcript_path=str(tpath), src_duration_s=info["duration"])


def stage_plan(ctx: Ctx, rid: int) -> None:
    cfg, row = ctx.cfg, _row(ctx, rid)
    tr = _load_json(Path(row["transcript_path"]))
    words = tr["words"]
    pb = learner.load_playbook(ctx.conn, cfg)
    phrases = build_phrases(words, cfg["edit"]["phrase_gap_s"])
    ctx_prompt = {
        "segments": phrases_for_prompt(phrases, words),
        "duration": round(row["src_duration_s"] or tr["duration"], 1),
        "facts": facts_for_prompt(ctx.brand),
        "hook_types": ", ".join(a["id"] for a in pb["arms"]["hook_type"]),
        "target_band": (pb.get("defaults") or {}).get("target_band", "46-60"),
    }
    plan = brain.ask(cfg, ctx.conn, "edit_plan", ctx_prompt, reel_id=rid, validate=lambda p: validate_plan(p, phrases, words, pb))
    e = cfg["edit"]
    t = tighten(words, phrases, plan["keep_segments"], plan["drop_word_ids"], gap_threshold=e["gap_threshold_s"],
                gap_target=e["gap_target_s"], pad=e["pad_s"], src_duration=row["src_duration_s"])
    ow = output_words(words, t["kept_word_ids"], t["intervals"], plan["caption_fixes"])
    wd = work_dir(cfg, rid)
    (wd / "words_out.json").write_text(json.dumps(ow, ensure_ascii=False), encoding="utf-8")
    cover_out = None
    if plan.get("cover_word_id") is not None:
        cw = next((w for w in ow if w["i"] == plan["cover_word_id"]), None)
        cover_out = cw["start"] if cw else None
    plan_json = {
        **{k: plan[k] for k in ("keep_segments", "drop_word_ids", "hook", "title_card", "pillar", "topic", "hook_type", "risk_flags", "caption_fixes", "notes")},
        "cover_word_id": plan.get("cover_word_id"),
        "cover_t_brain": cover_out,
        "intervals": t["intervals"],
        "kept_word_ids": t["kept_word_ids"],
        "dropped_fillers": t["dropped_fillers"],
        "output_duration": t["output_duration"],
        "cuts": t["cuts"],
        "segments_total": len(phrases),
        "output_word_count": len(ow),
        "wpm": words_per_minute(len(ow), t["output_duration"]),
        "cover_style": "hook_card+title",
    }
    crashpoint("after_plan_brain")
    dur = t["output_duration"]
    if dur < e["min_output_s"]:
        db.update_reel(ctx.conn, rid, plan_json=db.jdump(plan_json))
        raise Held(f"Too short after editing ({dur:.1f} s; minimum {e['min_output_s']:.0f} s)")
    if dur > e["max_output_s"]:
        db.update_reel(ctx.conn, rid, plan_json=db.jdump(plan_json))
        raise Held(f"Too long after editing ({dur:.1f} s; maximum {e['max_output_s']:.0f} s). Film a shorter take or split it into two clips.")
    db.transition(ctx.conn, rid, "planned", f"kept {len(plan['keep_segments'])}/{len(phrases)} segments, {t['cuts']} cuts, {dur:.1f} s",
                  expect_from="transcribed", plan_json=db.jdump(plan_json))


def style_overrides(ctx: Ctx, row) -> dict:
    ov = dict(db.jload(row["style_overrides_json"], {}) or {})
    preset = ov.pop("preset", None)
    if preset:
        base = ((ctx.brand.styles or {}).get("presets") or {}).get(preset)
        if base is None:
            raise Held(f"unknown style preset '{preset}' (brand/styles.yaml)")
        ov = {**base, **ov}
    return ov


def stage_render(ctx: Ctx, rid: int) -> None:
    cfg, row = ctx.cfg, _row(ctx, rid)
    plan = db.jload(row["plan_json"])
    wd = work_dir(cfg, rid)
    ow = _load_json(wd / "words_out.json")
    plan_r = dict(plan)
    if plan.get("cover_t_brain") is not None:
        plan_r["cover_t_out"] = plan["cover_t_brain"]
    out = cfg.dir("out") / f"{rid:05d}.mp4"
    thumb = cfg.dir("thumbs") / f"{rid:05d}.png"
    info = render_reel(cfg, Path(row["raw_path"]), db.jload(row["probe_json"]), plan_r, ow, wd / "render", out, thumb,
                       style_overrides=style_overrides(ctx, row), keep_together=(ctx.brand.facts or {}).get("keep_together", []))
    crashpoint("after_render_output")
    plan["cover_t_out"] = info["cover_t_out"]
    plan["render"] = info
    outp = probe(cfg.ffprobe, out)
    db.transition(ctx.conn, rid, "rendered", f"{outp['duration']:.1f} s, {info['cuts']} cuts, {info['chunks']} caption chunks",
                  expect_from="planned", out_path=str(out), thumb_path=str(thumb), duration_s=outp["duration"], plan_json=db.jdump(plan))


def _cta_options(ctx: Ctx, cta_type: str) -> list[str]:
    if cta_type == "none":
        return []
    items = (ctx.brand.captions or {}).get("ctas", [])
    return [c["text"] for c in items if c["cta_type"] == cta_type and not c.get("condition")]


SPOKEN_CTA_CUES = {
    "save": ("احفظ", "احفظه", "احفظها", "احفظو"),
    "send": ("ارسله", "ارسلها", "ارسل", "شاركه", "شاركها", "منشن"),
    "follow": ("تابعني", "تابع الحساب", "تابع عشان", "تابع،", "تابع"),
    "comment": ("اكتب لي", "علمني", "قول لي", "قولي", "بالتعليقات", "في التعليقات"),
}


def detect_spoken_cta(ow: list[dict], tail_words: int = 14) -> str | None:
    """A CTA he already says on camera at the end (so the caption must not add a second one)."""
    from .gates import normalize

    tail = " " + normalize(" ".join(w["text"] for w in ow[-tail_words:])) + " "
    for cta, cues in SPOKEN_CTA_CUES.items():
        if any(f" {normalize(c)}" in tail for c in cues):
            return cta
    return None


def pick_factors(ctx: Ctx, row, pb: dict, ow: list[dict] | None = None) -> dict:
    """The engine (not the brain) picks the logged factors so the data varies. If he
    already says a CTA on camera, that is the reel's one CTA (brand rule: never stack)."""
    rng = random.Random(int(row["sha256"][:12], 16))  # nosec B311 - experiment variation, not security
    spoken = detect_spoken_cta(ow or [])
    allowed = [c for c in ctx.cfg["cta"]["allowed"] if c != "service"]
    weights = {"save": 3, "send": 3, "follow": 2, "comment": 2, "none": 1}
    ctas = [c for c in allowed if c == "none" or _cta_options(ctx, c)]
    cta = rng.choices(ctas, weights=[weights.get(c, 1) for c in ctas])[0]
    lf = pb.get("logged_factors") or {}
    cap = min(ctx.cfg["qa"]["hashtags_max"], 5)
    counts = [c for c in lf.get("hashtag_count", [3, 5]) if c <= cap] or [cap]
    styles = lf.get("caption_style", ["natural_keywords", "short"])
    if spoken and spoken in allowed:
        cta = spoken
    return {"cta_type": cta, "spoken_cta": bool(spoken and spoken in allowed), "hashtag_count": rng.choice(counts), "caption_style": rng.choice(styles)}


def assemble_caption(first: str, body: str, cta: str, tags: list[str]) -> str:
    parts = [first.strip()]
    if body.strip():
        parts.append(body.strip())
    if cta.strip():
        parts.append(cta.strip())
    if tags:
        parts.append(" ".join(tags))
    return "\n\n".join(parts)


def generate_caption(ctx: Ctx, rid: int, plan: dict, ow: list[dict], pb: dict) -> dict:
    cfg, row = ctx.cfg, _row(ctx, rid)
    f = pick_factors(ctx, row, pb, ow)
    opts = [] if f["spoken_cta"] else _cta_options(ctx, f["cta_type"])
    voice = ctx.brand.voice
    voice_excerpt = voice[: voice.find("## 6.")] if "## 6." in voice else voice[:6000]

    def validate(c: dict) -> list[str]:
        errs = []
        if f["cta_type"] == "none" or f["spoken_cta"]:
            if c["cta_text"].strip():
                errs.append("cta_text must be empty for CTA type none")
        elif c["cta_text"].strip() not in [o.strip() for o in opts]:
            errs.append("cta_text must be copied exactly from the allowed CTA list")
        if len(c["hashtags"]) != f["hashtag_count"]:
            errs.append(f"use exactly {f['hashtag_count']} hashtags")
        if len({h.lower() for h in c["hashtags"]}) != len(c["hashtags"]):
            errs.append("duplicate hashtags")
        full = assemble_caption(c["first_line"], c["body"], c["cta_text"], c["hashtags"])
        cj = {"caption": full, "first_line": c["first_line"], "cta_type": f["cta_type"]}
        hard = [h for h in run_content_gates({"caption": full}, cj, cfg, ctx.brand) if h.severity == "hard"]
        errs += [f"rule broken: {h.reason} ('{h.match}')" for h in hard]
        if len(hashtags_of(full)) != len(c["hashtags"]):
            errs.append("hashtags must only appear in the hashtags field")
        return errs

    pool = (ctx.brand.captions or {}).get("hashtag_pool", [])
    c = brain.ask(cfg, ctx.conn, "caption", {
        "hook_on_screen": plan["hook"]["on_screen"], "hook_spoken": plan["hook"]["spoken"], "pillar": plan["pillar"],
        "topic": plan["topic"], "hook_type": plan["hook_type"], "duration": round(row["duration_s"] or 0, 1),
        "transcript": " ".join(w["text"] for w in ow), "caption_style": f["caption_style"], "cta_type": f["cta_type"],
        "cta_options": opts, "spoken_cta": f["spoken_cta"], "hashtag_count": f["hashtag_count"], "hashtag_pool": " ".join(pool),
        "service_enabled": bool(cfg["cta"]["service_enabled"]), "voice": voice_excerpt, "facts": facts_for_prompt(ctx.brand),
    }, reel_id=rid, validate=validate)
    full = assemble_caption(c["first_line"], c["body"], c["cta_text"], c["hashtags"])
    return {"caption": full, "first_line": c["first_line"], "body": c["body"], "cta_type": f["cta_type"], "cta_text": c["cta_text"], "spoken_cta": f["spoken_cta"],
            "hashtags": c["hashtags"], "keywords": c["keywords"], "caption_style": f["caption_style"]}


def stage_qa(ctx: Ctx, rid: int, run_critique: bool = True) -> None:
    cfg, row = ctx.cfg, _row(ctx, rid)
    plan = db.jload(row["plan_json"])
    ow = _load_json(work_dir(cfg, rid) / "words_out.json")
    pb = learner.load_playbook(ctx.conn, cfg)
    cap = db.jload(row["caption_json"])
    if not cap:
        cap = generate_caption(ctx, rid, plan, ow, pb)
        db.update_reel(ctx.conn, rid, caption_json=db.jdump(cap), caption_hash=caption_hash(cap["caption"]))
        crashpoint("after_caption_persist")
    result = qa.evaluate(cfg, ctx.conn, ctx.brand, out=Path(row["out_path"]), plan=plan, out_words=ow, caption=cap,
                         render_info=plan.get("render", {}), reel_id=rid, run_critique=run_critique)
    crashpoint("after_qa")
    row = _row(ctx, rid)
    feats = learner.extract_features(row, pb, cfg)
    if result["passed"]:
        db.transition(ctx.conn, rid, "qa_passed", f"QA passed, scores {result['scores']}", expect_from="rendered",
                      qa_json=db.jdump(result), features_json=db.jdump(feats), playbook_version=learner.current_version(ctx.conn))
    else:
        reason = qa.held_reason(result)
        db.transition(ctx.conn, rid, "held", reason, expect_from="rendered", qa_json=db.jdump(result), held_reason=reason,
                      features_json=db.jdump(feats), playbook_version=learner.current_version(ctx.conn))
        ctx.notify("warn", "Reel held by QA", f"Reel {rid}: {reason}", rid)


def stage_schedule(ctx: Ctx, rid: int) -> None:
    res = scheduler.schedule_reel(ctx.conn, ctx.cfg, rid)
    db.add_event(ctx.conn, rid, f"scheduled for {res.get('when_local')} (arm {res.get('arm')})")


STAGES: dict[str, Callable[[Ctx, int], None]] = {
    "new": stage_transcribe,
    "transcribed": stage_plan,
    "planned": stage_render,
    "rendered": stage_qa,
    "qa_passed": stage_schedule,
}


# ------------------------------------------------------------------ runner
def step(ctx: Ctx, rid: int) -> str:
    """Run the stage for the reel's current state once. Returns the new state."""
    row = _row(ctx, rid)
    state = row["state"]
    fn = STAGES.get(state)
    if fn is None:
        return state
    try:
        fn(ctx, rid)
    except Held as exc:
        db.transition(ctx.conn, rid, "held", str(exc), held_reason=str(exc))
        ctx.notify("warn", "Reel held", f"Reel {rid}: {exc}", rid)
    except brain.BrainBudgetExceeded as exc:
        db.update_reel(ctx.conn, rid, next_attempt_at=(db.utcnow() + timedelta(hours=1)).isoformat(timespec="seconds"), last_error=str(exc))
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:  # noqa: BLE001 - one bad reel must not stop the tick
        log.exception("stage %s failed for reel %s", state, rid)
        fail(ctx, rid, state, exc)
    return _row(ctx, rid)["state"]


def fail(ctx: Ctx, rid: int, stage_state: str, exc: BaseException) -> None:
    row = _row(ctx, rid)
    retries = row["retries"] + 1
    msg = redact(f"{type(exc).__name__}: {exc}")[:800]
    if retries > ctx.cfg["engine"]["max_retries"]:
        reason = f"Failed {retries} times at '{stage_state}': {msg[:300]}"
        db.transition(ctx.conn, rid, "held", reason, held_reason=reason, retries=retries, last_error=msg, failed_from=stage_state)
        ctx.notify("error", "Reel held after repeated failures", f"Reel {rid}: {reason}", rid)
        return
    nxt = (db.utcnow() + timedelta(seconds=backoff_s(retries - 1, ctx.cfg["engine"]["retry_base_minutes"]))).isoformat(timespec="seconds")
    db.transition(ctx.conn, rid, "failed", msg, failed_from=stage_state, retries=retries, last_error=msg, next_attempt_at=nxt)


def retry_failed(ctx: Ctx, now=None) -> list[int]:
    now = (now or db.utcnow()).isoformat(timespec="seconds")
    ids = []
    for r in ctx.conn.execute("SELECT id, failed_from FROM reels WHERE state='failed' AND is_demo=0 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)", (now,)):
        db.transition(ctx.conn, r["id"], r["failed_from"] or "new", "retry after failure")
        ids.append(r["id"])
    return ids


def process_reel(ctx: Ctx, rid: int, deadline: float | None = None, stop_at: str = "scheduled") -> str:
    state = _row(ctx, rid)["state"]
    while state in STAGES and state != stop_at:
        if deadline is not None and time.monotonic() > deadline:
            break
        row = _row(ctx, rid)
        na = db.parse_ts(row["next_attempt_at"])
        if na and na > db.utcnow() and state not in ("failed",):
            break
        new = step(ctx, rid)
        if new == state:
            break
        state = new
    return state


def reentry_state(row) -> str:
    """Where a held reel re-enters the pipeline (approve / rerender)."""
    if row["out_path"] and Path(row["out_path"]).exists() and row["plan_json"]:
        return "rendered"
    if row["plan_json"] and row["transcript_path"]:
        return "planned"
    if row["transcript_path"]:
        return "transcribed"
    return "new"


def approve(ctx: Ctx, rid: int) -> str:
    """Re-run QA on a held reel (fresh critique); it is scheduled only if it passes."""
    row = _row(ctx, rid)
    if row["state"] != "held":
        raise ValueError(f"reel {rid} is {row['state']}, not held")
    target = reentry_state(row)
    db.transition(ctx.conn, rid, target, "approve: re-run from " + target, retries=0, next_attempt_at=None)
    return process_reel(ctx, rid)


def rerender(ctx: Ctx, rid: int, overrides: dict) -> str:
    row = _row(ctx, rid)
    if row["state"] in ("publishing", "published"):
        raise ValueError(f"reel {rid} is {row['state']}; it can't be re-rendered")
    if not row["plan_json"]:
        raise ValueError(f"reel {rid} has no edit plan yet")
    cur = dict(db.jload(row["style_overrides_json"], {}) or {})
    cur.update(overrides)
    db.update_reel(ctx.conn, rid, style_overrides_json=db.jdump(cur), qa_json=None, scheduled_at=None, slot_arm=None)
    if row["state"] != "planned":
        db.transition(ctx.conn, rid, "planned", f"rerender with {overrides}", retries=0, next_attempt_at=None)
    return process_reel(ctx, rid)
