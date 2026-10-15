"""Thin, safe wrappers around the portable ffmpeg/ffprobe (resolved from config).

Arguments are always passed as a list (no shell), windows are suppressed, and every
call has a timeout. Filter-graph file paths are passed relative to a working
directory so Windows drive letters never need escaping inside filter strings.
"""
from __future__ import annotations

import json
import subprocess  # nosec B404 - fixed local binaries, list args, no shell
import sys
from pathlib import Path
from typing import Any, Sequence

from .log import get_logger, redact

log = get_logger("ffmpeg")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def setup_hint() -> str:
    """The setup script for this OS (used in error messages and doctor hints)."""
    return "scripts/setup-mac.sh" if sys.platform == "darwin" else "scripts/setup.ps1"


class FFmpegError(RuntimeError):
    def __init__(self, msg: str, stderr: str = ""):
        super().__init__(msg)
        self.stderr = stderr


def run(
    binary: Path,
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = 1800,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    if not Path(binary).exists():
        raise FFmpegError(f"binary not found: {binary} (run {setup_hint()})")
    cmd = [str(binary), *map(str, args)]
    log.debug("run %s", " ".join(cmd)[:2000])
    try:
        p = subprocess.run(  # nosec B603
            cmd,
            cwd=str(cwd) if cwd else None,
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"{Path(binary).name} timed out after {timeout}s") from exc
    if check and p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace")
        raise FFmpegError(f"{Path(binary).name} failed (rc={p.returncode}): {redact(err[-1500:])}", err)
    return p


def ffprobe_json(ffprobe: Path, media: Path, timeout: float = 60) -> dict[str, Any]:
    p = run(
        ffprobe,
        ["-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(media)],
        timeout=timeout,
    )
    return json.loads(p.stdout.decode("utf-8", "replace"))


def ffmpeg(cfg_ffmpeg: Path, args: Sequence[str], **kw: Any) -> subprocess.CompletedProcess:
    return run(cfg_ffmpeg, ["-hide_banner", "-nostdin", "-y", *args], **kw)


def stderr_text(p: subprocess.CompletedProcess) -> str:
    return p.stderr.decode("utf-8", "replace")


def filter_path(p: Path, cwd: Path) -> str:
    """Path for use inside an ffmpeg filter string: relative to cwd, forward slashes,
    with filter-special characters escaped."""
    try:
        rel = Path(p).resolve().relative_to(Path(cwd).resolve())
    except ValueError as exc:
        raise FFmpegError(f"{p} must be inside {cwd} for filter use") from exc
    s = rel.as_posix()
    for ch in ("\\", ":", "'", "[", "]", ",", ";"):
        s = s.replace(ch, "\\" + ch)
    return s
