"""QA gate (spec §4.8): technical checks on the rendered file + content gates + an
independent brain critique. Any hard failure (or a critique score below qa.min_score)
means the reel is HELD, never posted (spec §2.9).
"""
from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Any

from . import brain, db
from .ffmpeg import ffmpeg, stderr_text
from .gates import Brand, Hit, facts_for_prompt, run_content_gates
from .log import get_logger
from .probe import probe

log = get_logger("qa")


def _check(checks: list, name: str, ok: bool, detail: str = "", hard: bool = True) -> None:
    checks.append({"name": name, "ok": bool(ok), "hard": hard, "detail": detail})


def mp4_atoms(path: Path) -> list[str]:
    """Top-level MP4 atom order (to verify moov before mdat = faststart)."""
    names = []
    with open(path, "rb") as f:
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            size, typ = struct.unpack(">I4s", hdr)
            if size == 1:
                size = struct.unpack(">Q", f.read(8))[0]
                f.seek(size - 16, 1)
            elif size == 0:
                names.append(typ.decode("latin-1"))
                break
            else:
                f.seek(size - 8, 1)
            names.append(typ.decode("latin-1"))
    return names


def has_edit_list(path: Path) -> bool:
    with open(path, "rb") as f:
        head = f.read(4 * 1024 * 1024)  # moov is at the front (faststart)
    return b"elst" in head


def measure_output(cfg, path: Path) -> dict[str, Any]:
    """Loudness (EBU R128), black and frozen segments of the final file."""
    p = ffmpeg(cfg.ffmpeg, ["-i", str(path), "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"], timeout=600)
    err = stderr_text(p)
    summ = err[err.rfind("Summary:"):] if "Summary:" in err else err
    i = re.search(r"I:\s*(-?[\d.]+)\s*LUFS", summ)
    tp = re.search(r"Peak:\s*(-?[\d.]+)\s*dBFS", summ)
    q = cfg["qa"]
    p2 = ffmpeg(
        cfg.ffmpeg,
        ["-i", str(path), "-map", "0:v:0", "-vf", f"blackdetect=d=0.3:pix_th=0.08,freezedetect=n=0.001:d={q['freeze_max_s']}", "-an", "-f", "null", "-"],
        timeout=900,
    )
    e2 = stderr_text(p2)
    black = sum(float(x) for x in re.findall(r"black_duration:\s*([\d.]+)", e2))
    freezes = [float(x) for x in re.findall(r"freeze_duration:\s*([\d.]+)", e2)]
    return {
        "integrated_lufs": float(i.group(1)) if i else None,
        "true_peak_dbfs": float(tp.group(1)) if tp else None,
        "black_s": round(black, 2),
        "freeze_max_s": max(freezes) if freezes else 0.0,
    }


def caption_pixels(cfg, path: Path, t: float) -> float:
    """Share of accent-coloured pixels in the caption band at time t (proves captions
    were burned in with the highlight)."""
    import numpy as np

    h = cfg["render"]["height"]
    band_top = h - cfg["captions"]["margin_bottom"] - 420
    p = ffmpeg(
        cfg.ffmpeg,
        ["-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-vf", f"crop=iw:420:0:{band_top},scale=270:105", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        timeout=60,
    )
    arr = np.frombuffer(p.stdout, dtype=np.uint8).reshape(-1, 3).astype(int)
    hx = cfg["captions"]["highlight_color"].lstrip("#")
    target = np.array([int(hx[i:i + 2], 16) for i in (0, 2, 4)])
    dist = np.abs(arr - target).sum(axis=1)
    return float((dist < 90).mean())


def technical_checks(cfg, out: Path, render_info: dict, out_words: list[dict]) -> tuple[list[dict], dict]:
    checks: list[dict] = []
    q, r = cfg["qa"], cfg["render"]
    pr = probe(cfg.ffprobe, out)
    _check(checks, "container", pr.get("format", "").startswith("mov,mp4"), pr.get("format", ""))
    _check(checks, "video_codec", pr.get("vcodec") == "h264" and pr.get("profile") == "High", f"{pr.get('vcodec')} {pr.get('profile')}")
    _check(checks, "resolution", (pr.get("width"), pr.get("height")) == (r["width"], r["height"]) and pr.get("rotation", 0) == 0, f"{pr.get('width')}x{pr.get('height')} rot {pr.get('rotation')}")
    _check(checks, "fps", abs(pr.get("fps", 0) - r["fps"]) < 0.01, str(pr.get("fps")))
    _check(checks, "pix_fmt", pr.get("pix_fmt") == "yuv420p", str(pr.get("pix_fmt")))
    _check(checks, "audio", pr.get("acodec") == "aac" and pr.get("audio_rate") == r["audio_rate"] and pr.get("audio_channels") in (1, 2), f"{pr.get('acodec')} {pr.get('audio_rate')} Hz {pr.get('audio_channels')} ch")
    dur = pr.get("duration", 0)
    _check(checks, "duration_band", q["duration_min_s"] <= dur <= q["duration_max_s"], f"{dur:.1f} s (allowed {q['duration_min_s']:.0f}-{q['duration_max_s']:.0f} s)")
    size_mb = pr["size_bytes"] / 1e6
    _check(checks, "file_size", size_mb <= q["max_file_mb"], f"{size_mb:.1f} MB")
    mbps = (pr.get("bit_rate") or 0) / 1e6
    _check(checks, "bitrate", mbps <= q["max_video_mbps"], f"{mbps:.1f} Mb/s")
    atoms = mp4_atoms(out)
    moov_first = "moov" in atoms and "mdat" in atoms and atoms.index("moov") < atoms.index("mdat")
    _check(checks, "faststart", moov_first, " ".join(atoms[:6]))
    _check(checks, "no_edit_list", not has_edit_list(out), "")
    m = measure_output(cfg, out)
    lufs = m["integrated_lufs"]
    _check(checks, "loudness", lufs is not None and abs(lufs - r["loudness_i"]) <= q["loudness_tolerance_lu"], f"{lufs} LUFS (target {r['loudness_i']})")
    _check(checks, "true_peak", m["true_peak_dbfs"] is not None and m["true_peak_dbfs"] <= -0.5, f"{m['true_peak_dbfs']} dBFS")
    _check(checks, "black_frames", m["black_s"] <= q["black_max_s"], f"{m['black_s']} s black")
    _check(checks, "frozen_frames", m["freeze_max_s"] < q["freeze_max_s"], f"longest freeze {m['freeze_max_s']} s")
    # captions: ASS events exist AND the accent highlight is visible in the caption band
    _check(checks, "captions_present", render_info.get("chunks", 0) > 0 and len(out_words) > 0, f"{render_info.get('chunks', 0)} chunks")
    if out_words:
        mid = out_words[len(out_words) // 2]
        t = max(cfg["edit"]["hook_card_s"] + 0.1, (mid["start"] + mid["end"]) / 2) if dur > cfg["edit"]["hook_card_s"] + 1 else (mid["start"] + mid["end"]) / 2
        share = caption_pixels(cfg, out, min(t, dur - 0.1))
        _check(checks, "captions_burned", share > 0.0005, f"{share * 100:.2f}% accent pixels in the caption band at {t:.1f} s")
    return checks, {"probe": pr, **m}


def content_texts(plan: dict, out_words: list[dict], caption: dict | None) -> dict[str, str]:
    t = {
        "captions": " ".join(w["text"] for w in out_words),
        "hook": plan.get("hook", {}).get("on_screen", ""),
        "title": plan.get("title_card", ""),
    }
    if caption:
        t["caption"] = caption.get("caption", "")
    return t


def critique(cfg, conn, brand: Brand, plan: dict, out_words: list[dict], caption: dict, duration: float, reel_id: int | None) -> dict:
    ns = brand.never_say or {}
    never = "\n".join(f"- {k}: " + "، ".join(v) for k, v in ns.items() if isinstance(v, list) and v)
    ctx = {
        "hook_on_screen": plan["hook"]["on_screen"],
        "title_card": plan.get("title_card", ""),
        "duration": round(duration, 1),
        "transcript": " ".join(w["text"] for w in out_words),
        "caption": caption["caption"],
        "facts": facts_for_prompt(brand),
        "never_say": never,
        "service_enabled": bool(cfg["cta"]["service_enabled"]),
        "approved_ctas": "\n".join(f"- ({c['cta_type']}) {c['text']}" for c in (brand.captions or {}).get("ctas", []) if not c.get("condition")),
    }
    return brain.ask(cfg, conn, "critique", ctx, reel_id=reel_id)


def evaluate(cfg, conn, brand: Brand, *, out: Path, plan: dict, out_words: list[dict], caption: dict, render_info: dict, reel_id: int | None, run_critique: bool = True) -> dict:
    """Full QA. Returns qa_json (see db.py) with passed + hard_fails."""
    checks, measures = technical_checks(cfg, out, render_info, out_words)
    hits: list[Hit] = run_content_gates(content_texts(plan, out_words, caption), caption, cfg, brand)
    for h in hits:
        _check(checks, f"gate:{h.gate}:{h.rule}", False, h.short(), hard=(h.severity == "hard"))
    for flag in plan.get("risk_flags", []) or []:
        _check(checks, "edit_risk_flag", True, flag, hard=False)
    scores: dict[str, int] = {}
    crit: dict = {}
    hard_fails = [c["detail"] or c["name"] for c in checks if c["hard"] and not c["ok"]]
    if run_critique and not hard_fails:  # don't spend a brain call on a reel that already failed
        crit = critique(cfg, conn, brand, plan, out_words, caption, measures["probe"]["duration"], reel_id)
        scores = crit["scores"]
        low = {k: v for k, v in scores.items() if v < cfg["qa"]["min_score"]}
        _check(checks, "critique_scores", not low, f"scores {scores}" + (f"; below {cfg['qa']['min_score']}: {low}" if low else ""))
        bad_claims = [c["claim"] for c in crit["claims_about_yazeed"] if not c["in_facts"]]
        _check(checks, "critique_facts", not bad_claims, "claims not in facts.yaml: " + "; ".join(bad_claims) if bad_claims else "all claims verified")
        _check(checks, "critique_violations", not crit["violations"], "; ".join(f"{v['rule']}: {v['detail']}" for v in crit["violations"]))
        _check(checks, "dialect", crit["dialect_ok"], "; ".join(crit["spelling_issues"]))
        for s in crit.get("spelling_issues", []):
            _check(checks, "spelling", True, s, hard=False)
        hard_fails = [c["detail"] or c["name"] for c in checks if c["hard"] and not c["ok"]]
    return {
        "passed": not hard_fails,
        "hard_fails": hard_fails,
        "checks": checks,
        "scores": scores,
        "critique": crit,
        "measures": {k: v for k, v in measures.items() if k != "probe"},
        "at": db.now_iso(),
    }


def held_reason(qa: dict) -> str:
    fails = qa.get("hard_fails") or []
    return "QA: " + " | ".join(fails[:3]) + (f" (+{len(fails) - 3} more)" if len(fails) > 3 else "")
