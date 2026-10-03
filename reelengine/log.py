"""Logging with secret redaction.

Everything that is logged, stored as an error message, or stored as a raw API
payload goes through `redact()`. Known secret values (from keyring) are registered
at runtime and replaced exactly; token-shaped strings are replaced by pattern.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import threading
from pathlib import Path
from typing import Any

MASK = "***REDACTED***"

_known: set[str] = set()
_known_lock = threading.Lock()

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # query / form parameters
    (re.compile(r"(?i)\b(access_token|client_secret|fb_exchange_token|refresh_token|appsecret_proof)=([^&\s\"'<>]+)"), r"\1=" + MASK),
    # JSON fields
    (re.compile(r"(?i)(\"(?:access_token|client_secret|token|refresh_token|app_secret)\"\s*:\s*\")([^\"]+)(\")"), r"\1" + MASK + r"\3"),
    # HTTP auth headers
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*[\"']?(?:bearer|oauth)\s+)([^\s\"',}]+)"), r"\1" + MASK),
    (re.compile(r"(?i)\b((?:bearer|oauth)\s+)([A-Za-z0-9_\-\.]{20,})"), r"\1" + MASK),
    # Instagram / Facebook token shapes
    (re.compile(r"\bIG[A-Za-z0-9_\-]{20,}"), MASK),
    (re.compile(r"\bEAA[A-Za-z0-9]{20,}"), MASK),
    # Telegram bot tokens
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"), MASK),
    # generic high-entropy strings (mixed case + digit, 40+ chars); pure hex hashes are kept
    (re.compile(r"\b(?=[A-Za-z0-9_\-]*[A-Z])(?=[A-Za-z0-9_\-]*[a-z])(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{40,}\b"), MASK),
]


def register_secret(value: str | None) -> None:
    """Remember a secret value so every later log line / stored string masks it."""
    if value and len(value) >= 6:
        with _known_lock:
            _known.add(value)


def redact(text: Any) -> str:
    s = text if isinstance(text, str) else str(text)
    with _known_lock:
        known = sorted(_known, key=len, reverse=True)
    for v in known:
        if v in s:
            s = s.replace(v, MASK)
    for pat, repl in _PATTERNS:
        s = pat.sub(repl, s)
    return s


def redact_obj(obj: Any) -> Any:
    """Redact a JSON-like structure (dict/list/str). `paging` blocks are dropped
    because Graph API paging URLs carry the access token."""
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items() if k != "paging"}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


def dumps_redacted(obj: Any) -> str:
    return json.dumps(redact_obj(obj), ensure_ascii=False, sort_keys=True)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = ()
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


_configured = False


def setup_logging(log_dir: Path | None = None, level: int = logging.INFO, console: bool = True) -> None:
    """Configure the 'reelengine' logger once: rotating file in data/logs + console."""
    global _configured
    logger = logging.getLogger("reelengine")
    if _configured:
        return
    logger.setLevel(level)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    filt = RedactingFilter()
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / "engine.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(filt)
        logger.addHandler(fh)
    if console:
        stream = sys.stderr
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass  # nosec B110 - console without reconfigure()
        ch = logging.StreamHandler(stream)
        ch.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        ch.addFilter(filt)
        ch.setLevel(logging.WARNING)
        logger.addHandler(ch)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    lg = logging.getLogger(f"reelengine.{name}")
    if not any(isinstance(f, RedactingFilter) for f in lg.filters):
        lg.addFilter(RedactingFilter())
    return lg
