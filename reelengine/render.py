"""Render the final reel (spec §4.6).

Design (D-009): every kept interval is rendered to a short lossless-ish intermediate
(accurate seek, HDR→SDR tone-map, cover-crop to 1080x1920, 30 fps CFR, punch-in zoom
alternating 1.00/1.06, 10 ms audio fades against clicks), then the intermediates are
joined with the concat demuxer and encoded once with the burned-in ASS captions and
two-pass EBU R128 loudnorm (-14 LUFS). Per-segment files keep A/V drift bounded to
one frame per cut (it can't accumulate) and memory flat for any clip length.

Output: H.264 High, yuv420p, BT.709, closed GOP (60), CFR 30 fps, AAC-LC 48 kHz stereo
128 kb/s, moov at the front (+faststart) and no edit lists (API_NOTES "Reel specs").
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Sequence

from .captions import CaptionStyle, build_ass, title_card_ass, words_from_iter
from .ffmpeg import FFmpegError, ffmpeg, filter_path, stderr_text
from .log import get_logger

log = get_logger("render")

W, H = 1080, 1920


def _even(x: float) -> int:
    return int(x) // 2 * 2


def video_chain(probe: dict, zoom: float, fps: int) -> str:
    """Filter chain for one segment: tone-map (if HDR) -> cover-scale -> crop -> fps."""
    parts: list[str] = []
    if probe.get("is_hdr"):
        tin = probe.get("color_transfer") or "arib-std-b67"
        pin = probe.get("color_primaries") or "bt2020"
        min_ = "2020_ncl" if (probe.get("color_space") or "bt2020nc").startswith("bt2020") else "709"
        parts.append(
            f"zscale=tin={tin}:pin={pin}:min={min_}:t=linear:npl=100,format=gbrpf32le,"
            "zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
        )
    zw, zh = _even(W * zoom), _even(H * zoom)
    parts.append(f"scale=w={zw}:h={zh}:force_original_aspect_ratio=increase:force_divisible_by=2:flags=lanczos")
    # slightly above centre: faces sit in the upper half of a talking-head frame
    parts.append(f"crop={W}:{H}:(iw-{W})/2:(ih-{H})*0.42")
    parts.append(f"fps={fps},setsar=1,format=yuv420p")
    return ",".join(parts)


def _snap(t: float, fps: int) -> float:
    return round(round(t * fps) / fps, 4)


def render_segments(cfg, src: Path, probe: dict, intervals: Sequence[Sequence[float]], work: Path) -> list[Path]:
    fps = cfg["render"]["fps"]
    zooms = cfg["edit"]["zoom_levels"]
    rate = cfg["render"]["audio_rate"]
    segs: list[Path] = []
    for k, (s, e) in enumerate(intervals):
        s, e = _snap(s, fps), _snap(e, fps)
        d = max(1.0 / fps, e - s)
        fade = min(0.01, d / 4)
        out = work / f"seg{k:03d}.mkv"
        vf = video_chain(probe, zooms[k % len(zooms)], fps)
        af = (
            f"aresample={rate},aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={d - fade:.3f}:d={fade:.3f}"
        )
        ffmpeg(
            cfg.ffmpeg,
            [
                "-ss", f"{s:.4f}", "-i", str(src), "-t", f"{d:.4f}",
                "-map", "0:v:0", "-map", "0:a:0",
                "-vf", vf, "-af", af,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-g", str(fps),
                "-c:a", "pcm_s16le", "-ar", str(rate), "-ac", "2",
                str(out),
            ],
            timeout=900,
        )
        segs.append(out)
    return segs


def _concat_list(segs: Sequence[Path], work: Path) -> Path:
    lst = work / "concat.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in segs), encoding="utf-8")
    return lst


def measure_loudness(cfg, work: Path, lst: Path) -> dict[str, Any]:
    r = cfg["render"]
    p = ffmpeg(
        cfg.ffmpeg,
        ["-f", "concat", "-safe", "0", "-i", lst.name, "-vn",
         "-af", f"loudnorm=I={r['loudness_i']}:TP={r['true_peak']}:LRA={r['lra']}:print_format=json", "-f", "null", "-"],
        cwd=work, timeout=600,
    )
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr_text(p), re.S)
    if not m:
        raise FFmpegError("loudnorm pass 1 printed no measurement")
    return json.loads(m.group(0))


def _loudnorm_pass2(cfg, meas: dict) -> str:
    r = cfg["render"]
    return (
        f"loudnorm=I={r['loudness_i']}:TP={r['true_peak']}:LRA={r['lra']}"
        f":measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:measured_LRA={meas['input_lra']}"
        f":measured_thresh={meas['input_thresh']}:offset={meas['target_offset']}:linear=true:print_format=summary"
    )


def prepare_fonts(cfg, work: Path) -> Path:
    fonts = work / "fonts"
    if not fonts.exists():
        shutil.copytree(cfg.fonts_dir, fonts, ignore=shutil.ignore_patterns("*.txt", "*.json"))
    return fonts


def render_reel(
    cfg,
    src: Path,
    probe: dict,
    plan: dict,
    out_words: Sequence[dict],
    work: Path,
    out_mp4: Path,
    thumb_png: Path,
    style_overrides: dict | None = None,
    keep_together: Sequence[str] = (),
) -> dict[str, Any]:
    """Render `src` according to `plan` (intervals + hook/title). Returns render facts."""
    work.mkdir(parents=True, exist_ok=True)
    intervals = plan["intervals"]
    if not intervals:
        raise FFmpegError("empty edit plan (nothing kept)")
    st = CaptionStyle.from_config(cfg, style_overrides)
    total = round(sum(e - s for s, e in intervals), 3)
    words = words_from_iter(out_words)
    raws = [w.get("raw", w["text"]) for w in out_words]
    ass, chunks = build_ass(
        words, st,
        hook_text=plan["hook"]["on_screen"],
        hook_duration=cfg["edit"]["hook_card_s"],
        total_duration=total,
        raw_texts=raws,
        keep_together=keep_together,
    )
    (work / "captions.ass").write_text(ass, encoding="utf-8")
    (work / "title.ass").write_text(title_card_ass(plan.get("title_card", ""), st, duration=3600), encoding="utf-8")
    fonts = prepare_fonts(cfg, work)

    for old in work.glob("seg*.mkv"):
        old.unlink()
    segs = render_segments(cfg, src, probe, intervals, work)
    lst = _concat_list(segs, work)
    meas = measure_loudness(cfg, work, lst)

    r = cfg["render"]
    vf = f"subtitles={filter_path(work / 'captions.ass', work)}:fontsdir={filter_path(fonts, work)},format=yuv420p"
    tmp_out = work / "final.mp4"
    ffmpeg(
        cfg.ffmpeg,
        [
            "-f", "concat", "-safe", "0", "-i", lst.name,
            "-vf", vf, "-af", _loudnorm_pass2(cfg, meas) + f",aresample={r['audio_rate']}",
            "-c:v", "libx264", "-profile:v", "high", "-preset", r["preset"], "-crf", str(r["crf"]),
            "-maxrate", r["maxrate"], "-bufsize", r["bufsize"],
            "-g", str(r["gop"]), "-keyint_min", str(r["gop"]), "-sc_threshold", "0", "-flags", "+cgop",
            "-x264-params", "open-gop=0", "-pix_fmt", "yuv420p", "-r", str(r["fps"]), "-fps_mode", "cfr",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
            "-c:a", "aac", "-b:a", r["audio_bitrate"], "-ar", str(r["audio_rate"]), "-ac", "2",
            "-movflags", "+faststart", "-use_editlist", "0", "-map_metadata", "-1",
            tmp_out.name,
        ],
        cwd=work, timeout=1800,
    )
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp_out), str(out_mp4))

    # cover / thumbnail: clean frame (no captions) + the title card, centred for the 3:4 grid crop
    cover_t = cover_time(cfg, plan, total)
    thumb_png.parent.mkdir(parents=True, exist_ok=True)
    tvf = f"subtitles={filter_path(work / 'title.ass', work)}:fontsdir={filter_path(fonts, work)}"
    ffmpeg(cfg.ffmpeg, ["-ss", f"{cover_t:.3f}", "-f", "concat", "-safe", "0", "-i", lst.name, "-frames:v", "1", "-vf", tvf, "thumb.png"], cwd=work, timeout=120)
    shutil.move(str(work / "thumb.png"), str(thumb_png))
    for sgm in segs:
        sgm.unlink(missing_ok=True)
    return {
        "output_duration": total,
        "cuts": len(intervals) - 1,
        "chunks": len(chunks),
        "cover_t_out": cover_t,
        "loudness_pass1": {k: meas.get(k) for k in ("input_i", "input_tp", "input_lra", "target_offset")},
        "style": {k: getattr(st, k) for k in ("font_size", "max_words", "max_chars", "margin_bottom")},
    }


def cover_time(cfg, plan: dict, total: float) -> float:
    if cfg["edit"]["cover_mode"] == "brain" and plan.get("cover_t_out") is not None:
        return max(0.0, min(total - 0.1, float(plan["cover_t_out"])))
    return round(min(cfg["edit"]["hook_card_s"] / 2, max(0.0, total - 0.1)), 3)


def extract_frames(cfg, video: Path, times: Sequence[float], out_dir: Path, prefix: str = "frame") -> list[Path]:
    """PNG frames at the given output times (QA evidence)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for k, t in enumerate(times):
        p = out_dir / f"{prefix}-{k:02d}-{t:05.2f}s.png"
        ffmpeg(cfg.ffmpeg, ["-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", str(p)], timeout=120)
        paths.append(p)
    return paths
