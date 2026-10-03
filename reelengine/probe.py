"""ffprobe a clip: duration, geometry (rotation-aware), fps, audio, HDR."""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any

from .ffmpeg import ffprobe_json

HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}


class ProbeReject(Exception):
    """The clip can't be used (reason is shown to Yazeed)."""


def _rate(s: str | None) -> float:
    try:
        return float(Fraction(s)) if s and s != "0/0" else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def _rotation(stream: dict) -> int:
    rot = 0
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(round(float(sd["rotation"])))
    if not rot and "rotate" in (stream.get("tags") or {}):
        rot = int(stream["tags"]["rotate"])
    return rot % 360


def probe(ffprobe: Path, media: Path) -> dict[str, Any]:
    data = ffprobe_json(ffprobe, media)
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(fmt.get("duration") or (v or {}).get("duration") or 0.0)
    info: dict[str, Any] = {
        "duration": round(duration, 3),
        "size_bytes": int(fmt.get("size") or media.stat().st_size),
        "format": fmt.get("format_name"),
        "bit_rate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        "has_video": v is not None,
        "has_audio": a is not None,
    }
    if v:
        rot = _rotation(v)
        w, h = int(v.get("width", 0)), int(v.get("height", 0))
        dw, dh = (h, w) if rot in (90, 270) else (w, h)
        transfer = v.get("color_transfer")
        info.update(
            {
                "vcodec": v.get("codec_name"),
                "profile": v.get("profile"),
                "width": w,
                "height": h,
                "rotation": rot,
                "display_width": dw,
                "display_height": dh,
                "fps": round(_rate(v.get("avg_frame_rate")) or _rate(v.get("r_frame_rate")), 3),
                "pix_fmt": v.get("pix_fmt"),
                "color_transfer": transfer,
                "color_primaries": v.get("color_primaries"),
                "color_space": v.get("color_space"),
                "is_hdr": transfer in HDR_TRANSFERS,
                "field_order": v.get("field_order"),
                "nb_frames": int(v["nb_frames"]) if str(v.get("nb_frames", "")).isdigit() else None,
            }
        )
    if a:
        info.update(
            {
                "acodec": a.get("codec_name"),
                "audio_rate": int(a.get("sample_rate") or 0),
                "audio_channels": int(a.get("channels") or 0),
                "audio_bit_rate": int(a["bit_rate"]) if a.get("bit_rate") else None,
            }
        )
    return info


def check_usable(info: dict[str, Any], min_duration_s: float) -> None:
    """Raise ProbeReject for clips the pipeline must not process (spec §4.2)."""
    if not info.get("has_video"):
        raise ProbeReject("no video stream")
    if not info.get("has_audio"):
        raise ProbeReject("no audio track (the reel needs his voice; Instagram's music library can't be added via the API)")
    if info["duration"] < min_duration_s:
        raise ProbeReject(f"clip is {info['duration']:.1f} s, shorter than {min_duration_s:.0f} s")
    if info.get("display_width", 0) < 360 or info.get("display_height", 0) < 360:
        raise ProbeReject(f"resolution too low ({info.get('display_width')}x{info.get('display_height')})")
