"""Transcription with faster-whisper large-v3 (CUDA float16, word timestamps).

CUDA runtime libraries come from pip wheels (nvidia-cublas-cu12, nvidia-cudnn-cu12);
their bin folders are added with os.add_dll_directory before ctranslate2 loads.
If CUDA fails for any reason the model is reloaded on CPU (int8), which is slower
but keeps the pipeline running. The model is cached per process.

macOS: ctranslate2 (4.8.2) has no CUDA and no Metal/MPS backend, so on darwin a device of
"cuda" or "auto" goes straight to CPU with `cpu_compute_type` (int8, Apple Accelerate),
without trying CUDA and without a warning.
"""
from __future__ import annotations

import json
import os
import site
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .log import get_logger

log = get_logger("transcribe")

_model = None
_model_key: tuple | None = None
_lock = threading.Lock()
_dll_handles: list = []


def add_cuda_dll_dirs() -> list[str]:
    """Register the nvidia/*/bin folders from the pip wheels (Windows only)."""
    added: list[str] = []
    if sys.platform != "win32":
        return added
    roots = [Path(p) for p in site.getsitepackages()] + [Path(sys.prefix) / "Lib" / "site-packages"]
    for root in roots:
        nv = root / "nvidia"
        if not nv.is_dir():
            continue
        for sub in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
            b = nv / sub / "bin"
            if b.is_dir() and str(b) not in added:
                _dll_handles.append(os.add_dll_directory(str(b)))
                os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")
                added.append(str(b))
    return added


MAC_NO_CUDA = "macOS: no CUDA; CPU int8"


def cuda_available() -> tuple[bool, str]:
    if sys.platform == "darwin":
        return (False, MAC_NO_CUDA)
    try:
        add_cuda_dll_dirs()
        import ctranslate2

        n = ctranslate2.get_cuda_device_count()
        return (n > 0, f"{n} CUDA device(s), ctranslate2 {ctranslate2.__version__}")
    except Exception as exc:  # noqa: BLE001
        return (False, f"CUDA check failed: {exc}")


def _load(model_path: str, device: str, compute_type: str):
    from faster_whisper import WhisperModel

    t = time.time()
    m = WhisperModel(model_path, device=device, compute_type=compute_type)
    log.info("whisper loaded on %s/%s in %.1fs", device, compute_type, time.time() - t)
    return m


def get_model(cfg) -> tuple[Any, str]:
    """Return (model, device_used). Tries CUDA first, falls back to CPU int8."""
    global _model, _model_key
    w = cfg["whisper"]
    path = str(cfg.resolve(w["model_path"]))
    with _lock:
        if _model is not None and _model_key and _model_key[0] == path:
            return _model, _model_key[1]
        if w["device"] in ("cuda", "auto") and sys.platform == "darwin":
            log.info("macOS: whisper runs on CPU/%s (ctranslate2 has no CUDA or Metal backend)", w["cpu_compute_type"])
        elif w["device"] in ("cuda", "auto"):
            add_cuda_dll_dirs()
            try:
                _model = _load(path, "cuda", w["compute_type"])
                _model_key = (path, "cuda")
                return _model, "cuda"
            except Exception as exc:  # noqa: BLE001
                log.warning("CUDA whisper failed (%s); falling back to CPU int8", exc)
        _model = _load(path, "cpu", w["cpu_compute_type"])
        _model_key = (path, "cpu")
        return _model, "cpu"


def _run(model, audio: Path, w: dict) -> tuple[list, Any]:
    segments, info = model.transcribe(
        str(audio),
        language=w["language"],
        beam_size=w["beam_size"],
        word_timestamps=True,
        vad_filter=w["vad_filter"],
        vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 200},
        initial_prompt=w["initial_prompt"] or None,
        condition_on_previous_text=False,
        temperature=0.0,
    )
    return list(segments), info


def transcribe(cfg, audio: Path) -> dict[str, Any]:
    """Transcribe a WAV/any-audio file. Returns
    {language, language_probability, duration, device, model, words:[{i,start,end,text,prob,seg}],
     segments:[{id,start,end,text}]}"""
    w = cfg["whisper"]
    model, device = get_model(cfg)
    t0 = time.time()
    try:
        segs, info = _run(model, audio, w)
    except Exception as exc:  # noqa: BLE001  (e.g. cuDNN missing at first inference)
        if device != "cuda":
            raise
        log.warning("CUDA inference failed (%s); retrying on CPU int8", exc)
        global _model, _model_key
        with _lock:
            _model = _load(str(cfg.resolve(w["model_path"])), "cpu", w["cpu_compute_type"])
            _model_key = (str(cfg.resolve(w["model_path"])), "cpu")
        model, device = _model, "cpu"
        segs, info = _run(model, audio, w)
    words: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    for si, s in enumerate(segs):
        segments.append({"id": si, "start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip()})
        for wd in s.words or []:
            text = wd.word.strip()
            if not text:
                continue
            words.append(
                {
                    "i": len(words),
                    "start": round(float(wd.start), 3),
                    "end": round(float(max(wd.end, wd.start + 0.02)), 3),
                    "text": text,
                    "prob": round(float(wd.probability), 3),
                    "seg": si,
                }
            )
    out = {
        "language": info.language,
        "language_probability": round(float(info.language_probability), 3),
        "duration": round(float(info.duration), 3),
        "device": device,
        "model": Path(w["model_path"]).name,
        "elapsed_s": round(time.time() - t0, 2),
        "words": words,
        "segments": segments,
    }
    log.info("transcribed %s: %d words in %.1fs on %s", audio.name, len(words), out["elapsed_s"], device)
    return out


def save(transcript: dict, path: Path) -> None:
    path.write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
