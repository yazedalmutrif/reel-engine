"""Render + technical QA on a tiny synthetic clip (ffmpeg only, no GPU/brain):
the good render passes every technical check, and the QA gate catches a file
without faststart, a reel without captions, and an out-of-band duration."""
from __future__ import annotations

from pathlib import Path

import pytest

from reelengine.config import load_config
from reelengine.ffmpeg import ffmpeg
from reelengine.probe import ProbeReject, check_usable, probe
from reelengine.qa import has_edit_list, mp4_atoms, technical_checks
from reelengine.render import render_reel

CFG = load_config()
pytestmark = pytest.mark.skipif(not CFG.ffmpeg.exists(), reason="portable ffmpeg not installed")


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("clip")
    src = d / "src.mp4"
    ffmpeg(CFG.ffmpeg, ["-f", "lavfi", "-i", "testsrc2=s=720x1280:r=30:d=8", "-f", "lavfi", "-i", "sine=f=220:d=8:sample_rate=48000",
                        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)])
    return src


def _words() -> list[dict]:
    toks = "أول خطوة سوّ CV واضح وحط رابط GitHub".split()
    return [{"i": k, "text": t, "raw": t, "start": 0.3 + k * 0.6, "end": 0.8 + k * 0.6, "prob": 1.0, "break_before": k == 4} for k, t in enumerate(toks)]


def _plan() -> dict:
    return {"intervals": [[0.0, 3.0], [4.0, 7.0]], "hook": {"on_screen": "3 خطوات قبل ما تقدم", "spoken": ""}, "title_card": "3 خطوات"}


def test_render_passes_technical_qa(clip, tmp_path):
    out, thumb = tmp_path / "out.mp4", tmp_path / "thumb.png"
    info = render_reel(CFG, clip, probe(CFG.ffprobe, clip), _plan(), _words(), tmp_path / "work", out, thumb)
    assert info["cuts"] == 1 and info["chunks"] >= 2 and thumb.exists()
    checks, m = technical_checks(CFG, out, info, _words())
    bad = [c for c in checks if c["hard"] and not c["ok"]]
    assert not bad, bad
    assert abs(m["integrated_lufs"] - CFG["render"]["loudness_i"]) <= CFG["qa"]["loudness_tolerance_lu"]
    atoms = mp4_atoms(out)
    assert atoms.index("moov") < atoms.index("mdat") and not has_edit_list(out)
    assert not list((tmp_path / "work").glob("seg*.mkv")), "intermediates are cleaned up"


def test_qa_catches_missing_faststart_and_captions(clip, tmp_path):
    bad = tmp_path / "bad.mp4"
    ffmpeg(CFG.ffmpeg, ["-i", str(clip), "-vf", "scale=1080:1920,fps=30", "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-ar", "48000", str(bad)])  # no +faststart, no captions, not loudness-normalised
    checks, _ = technical_checks(CFG, bad, {"chunks": 0}, [])
    failed = {c["name"] for c in checks if not c["ok"]}
    assert {"faststart", "captions_present", "loudness"} <= failed, failed


def test_duration_band_enforced(clip, tmp_path):
    short = tmp_path / "short.mp4"
    ffmpeg(CFG.ffmpeg, ["-i", str(clip), "-t", "3", "-c", "copy", str(short)])
    checks, _ = technical_checks(CFG, short, {"chunks": 1}, [])
    assert not next(c for c in checks if c["name"] == "duration_band")["ok"]


def test_probe_rejects_short_and_silent(clip, tmp_path):
    silent = tmp_path / "silent.mp4"
    ffmpeg(CFG.ffmpeg, ["-i", str(clip), "-an", "-c:v", "copy", str(silent)])
    with pytest.raises(ProbeReject, match="audio"):
        check_usable(probe(CFG.ffprobe, silent), 3.0)
    with pytest.raises(ProbeReject, match="shorter"):
        check_usable(probe(CFG.ffprobe, clip), 30.0)
