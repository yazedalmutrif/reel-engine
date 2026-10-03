"""Download and verify the engine's binary assets (idempotent).

- Portable ffmpeg full build (gyan.dev, via its GitHub release), sha256 pinned and
  cross-checked on two hosts (gyan.dev .sha256 file and the GitHub asset digest).
- OFL fonts from the official google/fonts repo, verified against the git blob SHA-1
  that the GitHub API lists, then recorded (sha256) in brand/fonts/MANIFEST.json.
- faster-whisper large-v3 model (Systran/faster-whisper-large-v3) into models/.

Nothing is installed system-wide and PATH is not changed.
Usage:  .venv\\Scripts\\python.exe scripts\\bootstrap_assets.py [--skip-model]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FFMPEG_VERSION = "9.0.2"
FFMPEG_URL = f"https://github.com/GyanD/codexffmpeg/releases/download/{FFMPEG_VERSION}/ffmpeg-{FFMPEG_VERSION}-full_build.7z"
# Verified 2026-09-29: identical on gyan.dev (packages/ffmpeg-9.0.2-full_build.7z.sha256)
# and in the GitHub release asset digest.
FFMPEG_SHA256 = "f0e46253c70dfe902bac915dfb4224f0cf1b7c6eeab9da2ccc9a5581f9a71b13"

FONTS = {
    "ibmplexsansarabic": [
        "IBMPlexSansArabic-Regular.ttf",
        "IBMPlexSansArabic-Medium.ttf",
        "IBMPlexSansArabic-SemiBold.ttf",
        "IBMPlexSansArabic-Bold.ttf",
        "OFL.txt",
    ],
    "inter": ["Inter[opsz,wght].ttf", "OFL.txt"],
    "jetbrainsmono": ["JetBrainsMono[wght].ttf", "OFL.txt"],
}
WHISPER_REPO = "Systran/faster-whisper-large-v3"

UA = {"User-Agent": "ReelEngine-bootstrap/1.0"}


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 - fixed https URLs
        return r.read()


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:  # nosec B310
        shutil.copyfileobj(r, f, length=1024 * 1024)
    tmp.replace(dest)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def ensure_ffmpeg() -> None:
    target = ROOT / "bin" / "ffmpeg"
    exe = target / "bin" / "ffmpeg.exe"
    if exe.exists() and (target / "bin" / "ffprobe.exe").exists():
        print(f"[ffmpeg] present: {exe}")
        return
    archive = ROOT / "data" / "downloads" / f"ffmpeg-{FFMPEG_VERSION}-full_build.7z"
    if not archive.exists() or sha256_file(archive) != FFMPEG_SHA256:
        print(f"[ffmpeg] downloading {FFMPEG_URL}")
        _download(FFMPEG_URL, archive)
    got = sha256_file(archive)
    if got != FFMPEG_SHA256:
        archive.unlink(missing_ok=True)
        raise SystemExit(f"[ffmpeg] CHECKSUM MISMATCH: {got} != {FFMPEG_SHA256}")
    print("[ffmpeg] sha256 verified")
    staging = ROOT / "data" / "downloads" / "ffmpeg_extract"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    # The archive uses the BCJ2 filter, which py7zr can't decode. Windows 10/11 ships
    # bsdtar (libarchive) as System32\tar.exe, which can; nothing gets installed.
    import os
    import subprocess

    tar = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
    subprocess.run([str(tar), "-xf", str(archive), "-C", str(staging)], check=True)  # nosec B603 - fixed args
    inner = next(p for p in staging.iterdir() if p.is_dir())
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(inner), str(target))
    shutil.rmtree(staging, ignore_errors=True)
    (target / "SOURCE.txt").write_text(
        f"{FFMPEG_URL}\nsha256={FFMPEG_SHA256}\n", encoding="utf-8"
    )
    print(f"[ffmpeg] installed to {target}")


def ensure_fonts() -> None:
    fonts_dir = ROOT / "brand" / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = fonts_dir / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else {}
    for family, files in FONTS.items():
        listing = None
        for name in files:
            local_name = name if name != "OFL.txt" else f"OFL-{family}.txt"
            dest = fonts_dir / local_name
            rec = manifest.get(local_name)
            if dest.exists() and rec and sha256_file(dest) == rec["sha256"]:
                continue
            if listing is None:
                api = f"https://api.github.com/repos/google/fonts/contents/ofl/{family}"
                listing = {e["name"]: e for e in json.loads(_get(api))}
            entry = listing[name]
            data = _get(entry["download_url"])
            if git_blob_sha1(data) != entry["sha"]:
                raise SystemExit(f"[fonts] blob hash mismatch for {name}")
            dest.write_bytes(data)
            manifest[local_name] = {
                "source": entry["download_url"],
                "git_blob_sha1": entry["sha"],
                "sha256": hashlib.sha256(data).hexdigest(),
                "license": "SIL Open Font License 1.1",
            }
            print(f"[fonts] {local_name} verified")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[fonts] ok ({len(manifest)} files)")


def ensure_model() -> None:
    dest = ROOT / "models" / "faster-whisper-large-v3"
    if (dest / "model.bin").exists() and (dest / "SOURCE.json").exists():
        print(f"[model] present: {dest}")
        return
    from huggingface_hub import HfApi, snapshot_download

    info = HfApi().model_info(WHISPER_REPO)
    print(f"[model] downloading {WHISPER_REPO}@{info.sha} (~3 GB)")
    snapshot_download(repo_id=WHISPER_REPO, revision=info.sha, local_dir=str(dest))
    (dest / "SOURCE.json").write_text(
        json.dumps({"repo": WHISPER_REPO, "revision": info.sha}, indent=2), encoding="utf-8"
    )
    print(f"[model] ok -> {dest}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-model", action="store_true")
    ap.add_argument("--only", choices=["ffmpeg", "fonts", "model"])
    a = ap.parse_args()
    steps = {"ffmpeg": ensure_ffmpeg, "fonts": ensure_fonts, "model": ensure_model}
    for name, fn in steps.items():
        if a.only and a.only != name:
            continue
        if name == "model" and a.skip_model:
            continue
        fn()
    return 0


if __name__ == "__main__":
    sys.exit(main())
