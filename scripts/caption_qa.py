"""Caption rendering QA: burn real mixed Arabic + English sentences with word
highlighting into a 1080x1920 test video, extract PNG frames, and draw the Reels
safe-zone guides on copies so a human (or Claude) can inspect them.

Usage: .venv\\Scripts\\python.exe scripts\\caption_qa.py [--out docs/qa/captions]
Frames: <out>/NN-<label>.png (clean) and <out>/NN-<label>-zones.png (with guides).
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reelengine.captions import CaptionStyle, Word, build_ass  # noqa: E402
from reelengine.config import load_config  # noqa: E402
from reelengine.ffmpeg import ffmpeg, filter_path  # noqa: E402

SENTENCES = [
    "أول خطوة: سوّ CV واضح وحط رابط GitHub.",
    "CV واضح يفرق كثير عن CV عادي.",
    "استخدم ChatGPT أو Claude عشان تجهز للمقابلة؟",
    "حقق مشروعنا المركز الثالث في هاكاثون AgentX.",
    "غيرت شعار الإقلاع لـ418 جهاز بسكربت .bat واحد.",
    "AI Engineer ولا ML Engineer؟ ترى مو نفس الشي!",
]
HOOK = "أول خطوة: سوّ CV واضح"


def timeline(sentences: list[str], wdur: float = 0.42, gap: float = 0.05, sgap: float = 0.5) -> tuple[list[Word], list[str]]:
    words: list[Word] = []
    raws: list[str] = []
    t = 0.3
    for s in sentences:
        for tok in s.split():
            words.append(Word(tok, t, t + wdur))
            raws.append(tok)
            t += wdur + gap
        t += sgap
    return words, raws


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "qa" / "captions"))
    a = ap.parse_args()
    cfg = load_config()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    st = CaptionStyle.from_config(cfg)
    words, raws = timeline(SENTENCES)
    total = words[-1].end + 0.6
    import yaml

    keep = (yaml.safe_load((cfg.brand_dir / "facts.yaml").read_text(encoding="utf-8")) or {}).get("keep_together", [])
    ass, chunks = build_ass(words, st, hook_text=HOOK, hook_duration=2.2, total_duration=total, raw_texts=raws, keep_together=keep)

    work = Path(tempfile.mkdtemp(prefix="capqa_", dir=str(cfg.dir("work"))))
    try:
        (work / "c.ass").write_text(ass, encoding="utf-8")
        fonts = work / "fonts"
        shutil.copytree(cfg.fonts_dir, fonts, ignore=shutil.ignore_patterns("*.txt", "*.json"))
        # A dim "face" gradient so contrast is realistic (not a flat black card).
        src = f"gradients=s=1080x1920:c0=0x3a3f4a:c1=0x14161a:x0=540:y0=300:x1=540:y1=1900:d={total:.2f}:r=30"
        vf = f"subtitles={filter_path(work / 'c.ass', work)}:fontsdir={filter_path(fonts, work)}"
        ffmpeg(cfg.ffmpeg, ["-f", "lavfi", "-i", src, "-vf", vf, "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "cap.mp4"], cwd=work)
        # one frame per chunk: at the middle of the 2nd word (or 1st)
        picks: list[tuple[str, float]] = [("hook", 1.0)]
        for i, ch in enumerate(chunks):
            w = ch.words[min(1, len(ch.words) - 1)]
            picks.append((f"chunk{i:02d}", (w.start + w.end) / 2))
        for n, (label, t) in enumerate(picks):
            name = f"{n:02d}-{label}"
            ffmpeg(cfg.ffmpeg, ["-ss", f"{t:.3f}", "-i", "cap.mp4", "-frames:v", "1", str(out / f"{name}.png")], cwd=work)
            # guides: bottom 20% (IG caption/UI) and right 150 px (buttons), top 14% header
            guides = (
                "drawbox=x=0:y=ih*0.80:w=iw:h=ih*0.20:color=red@0.35:t=fill,"
                "drawbox=x=iw-150:y=0:w=150:h=ih:color=orange@0.30:t=fill,"
                "drawbox=x=0:y=0:w=iw:h=ih*0.14:color=yellow@0.20:t=fill"
            )
            ffmpeg(cfg.ffmpeg, ["-i", str(out / f"{name}.png"), "-vf", guides, str(out / f"{name}-zones.png")], cwd=work)
        (out / "chunks.txt").write_text(
            "\n".join(f"{i:02d} [{c.start:.2f}-{c.end:.2f}] {c.text()}" for i, c in enumerate(chunks)), encoding="utf-8"
        )
        (out / "captions.ass").write_text(ass, encoding="utf-8")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"{len(picks)} frames -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
