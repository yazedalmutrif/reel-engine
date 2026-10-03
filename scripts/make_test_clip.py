"""Build the synthetic Arabic test clip for the end-to-end test (spec §12).

Saudi-voice TTS (edge-tts, ar-SA-HamedNeural) with deliberate long pauses, a false
start, a retake (the last take is the good one) and fillers, muxed with a moving
background into an iPhone-like file: HEVC 10-bit, HLG (BT.2020) tags, stored
landscape with a 90° rotation flag, 48 kHz stereo AAC. This exercises probe
(rotation), HDR tone-mapping, transcription, the edit plan and the cuts.

Usage: .venv\\Scripts\\python.exe scripts\\make_test_clip.py [--out tests/fixtures]
Writes synthetic_ar.mov and synthetic_ar.json (the script, with what should be cut).
Needs network access once (edge-tts); the output is cached.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reelengine.config import load_config  # noqa: E402
from reelengine.ffmpeg import ffmpeg  # noqa: E402

VOICE = "ar-SA-HamedNeural"
# (text, pause_after_s, role)  role: keep | false_start | retake_bad | filler
SCRIPT = [
    ("طيب أول شي", 1.2, "false_start"),
    ("أول خطوة: سوّ CV واضح وحط رابط GitHub.", 0.9, "keep"),
    ("إمم، يعني", 1.3, "filler"),
    ("ثاني خطوة: افتح LinkedIn وحدّث بروفايلك كل أسبوع.", 1.6, "keep"),
    ("ثالث شي، جرب تبني مشروع، لا لحظة، خلني أعيدها.", 1.4, "retake_bad"),
    ("ثالث شي: ابن مشروع صغير بالذكاء الاصطناعي وحطه في GitHub.", 0.8, "keep"),
    ("احفظ المقطع، بتحتاجه وقت التقديم.", 0.8, "keep"),
]


async def _tts(text: str, out: Path) -> None:
    import edge_tts

    await edge_tts.Communicate(text, VOICE, rate="+0%").save(str(out))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "tests" / "fixtures"))
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / "synthetic_ar.mov"
    if target.exists() and not a.force:
        print(f"exists: {target}")
        return 0
    cfg = load_config()
    work = Path(tempfile.mkdtemp(prefix="clip_", dir=str(cfg.dir("work"))))
    try:
        parts = []
        timeline = []
        t = 0.5
        for i, (text, pause, role) in enumerate(SCRIPT):
            mp3 = work / f"p{i}.mp3"
            asyncio.run(_tts(text, mp3))
            wav = work / f"p{i}.wav"
            # to 48k stereo, then pad with the pause
            ffmpeg(cfg.ffmpeg, ["-i", str(mp3), "-af", f"aresample=48000,apad=pad_dur={pause}", "-ac", "2", "-ar", "48000", str(wav)])
            from reelengine.probe import probe

            d = probe(cfg.ffprobe, wav)["duration"]
            timeline.append({"text": text, "role": role, "start": round(t, 2), "end": round(t + d - pause, 2)})
            t += d
            parts.append(wav)
        lead = work / "lead.wav"
        ffmpeg(cfg.ffmpeg, ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "0.5", str(lead)])
        lst = work / "list.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in [lead, *parts]), encoding="utf-8")
        ffmpeg(cfg.ffmpeg, ["-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "voice.wav"], cwd=work)
        total = t + 0.3
        # landscape 1920x1080 frames + rotation 90 => displays as 1080x1920 portrait
        vsrc = f"gradients=s=1920x1080:c0=0x2a3550:c1=0x6a4a3a:c2=0x1d1d24:speed=0.02:d={total:.2f}:r=30"
        ffmpeg(
            cfg.ffmpeg,
            [
                "-f", "lavfi", "-i", vsrc, "-i", "voice.wav",
                "-map", "0:v", "-map", "1:a", "-shortest",
                "-c:v", "libx265", "-preset", "fast", "-crf", "24", "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
                "-x265-params", "log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
                "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                "clip0.mov",
            ],
            cwd=work,
        )
        # rotation is a display-matrix side data: set it on the input and stream-copy
        ffmpeg(cfg.ffmpeg, ["-display_rotation:v:0", "90", "-i", "clip0.mov", "-c", "copy", "-map", "0", "clip.mov"], cwd=work)
        shutil.copy2(work / "clip.mov", target)
        (out / "synthetic_ar.json").write_text(
            json.dumps({"voice": VOICE, "duration": round(total, 2), "timeline": timeline}, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"wrote {target} ({target.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
