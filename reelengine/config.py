"""Configuration: load config.toml, validate it, resolve paths.

Hard limits that config can never loosen live here as constants (HARD_*), so a bad
edit to config.toml (or the learner) cannot switch a gate off.
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import jsonschema
import tomli

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

# ---- hard limits (spec §2 and API_NOTES); config may be stricter, never looser ----
HARD_HASHTAG_CAP = 5
HARD_CAPTION_MAX = 2200
HARD_MIN_DURATION_S = 5.0
HARD_MAX_DURATION_S = 90.0
HARD_MAX_POSTS_PER_DAY = 3
HARD_EXPLORE_MIN = 0.25
ALLOWED_MODES = ("dry-run", "live")
KNOWN_CTAS = ("follow", "save", "send", "comment", "none", "service")
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


class ConfigError(Exception):
    """config.toml is missing, unreadable or invalid."""


_HHMM = {"type": "string", "pattern": r"^([01]\d|2[0-3]):[0-5]\d$"}
_COLOR = {"type": "string", "pattern": r"^#[0-9A-Fa-f]{6}$"}
_NUM = {"type": "number"}
_INT = {"type": "integer"}
_STR = {"type": "string"}
_BOOL = {"type": "boolean"}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required if required is not None else list(props),
        "additionalProperties": False,
    }


SCHEMA: dict = _obj(
    {
        "engine": _obj(
            {
                "mode": {"enum": list(ALLOWED_MODES)},
                "timezone": _STR,
                "tick_budget_minutes": {"type": "integer", "minimum": 1, "maximum": 14},
                "max_retries": {"type": "integer", "minimum": 0, "maximum": 10},
                "retry_base_minutes": {"type": "integer", "minimum": 1},
            }
        ),
        "paths": _obj(
            {k: _STR for k in ("inbox", "data_dir", "ffmpeg", "ffprobe", "fonts_dir", "brand_dir", "prompts_dir")}
        ),
        "intake": _obj(
            {
                "extensions": {"type": "array", "items": _STR, "minItems": 1},
                "stable_ticks": {"type": "integer", "minimum": 1},
                "min_duration_s": {"type": "number", "minimum": 3},
            }
        ),
        "whisper": _obj(
            {
                "model_path": _STR,
                "device": {"enum": ["cuda", "cpu", "auto"]},
                "compute_type": _STR,
                "cpu_compute_type": _STR,
                "language": _STR,
                "beam_size": {"type": "integer", "minimum": 1, "maximum": 10},
                "vad_filter": _BOOL,
                "initial_prompt": _STR,
            }
        ),
        "brain": _obj(
            {
                "provider": {"enum": ["claude-cli", "fake"]},
                "cli_path": _STR,
                "model": _STR,
                "review_model": _STR,
                "timeout_s": {"type": "integer", "minimum": 10, "maximum": 1800},
                "trends_timeout_s": {"type": "integer", "minimum": 60, "maximum": 3600},
                "retries": {"type": "integer", "minimum": 0, "maximum": 5},
                "max_calls_per_day": {"type": "integer", "minimum": 1, "maximum": 500},
            }
        ),
        "edit": _obj(
            {
                "phrase_gap_s": {"type": "number", "exclusiveMinimum": 0},
                "gap_threshold_s": {"type": "number", "exclusiveMinimum": 0},
                "gap_target_s": {"type": "number", "minimum": 0},
                "pad_s": {"type": "number", "minimum": 0, "maximum": 0.5},
                "cover_mode": {"enum": ["hook_card", "brain"]},
                "zoom_levels": {"type": "array", "items": {"type": "number", "minimum": 1.0, "maximum": 1.3}, "minItems": 1},
                "hook_card_s": {"type": "number", "minimum": 0, "maximum": 6},
                "progress_bar": _BOOL,
                "min_output_s": _NUM,
                "max_output_s": _NUM,
            }
        ),
        "captions": _obj(
            {
                "font": _STR,
                "font_size": {"type": "integer", "minimum": 30, "maximum": 160},
                "max_words": {"type": "integer", "minimum": 1, "maximum": 4},
                "max_chars": {"type": "integer", "minimum": 6, "maximum": 22},
                "text_color": _COLOR,
                "highlight_color": _COLOR,
                "outline_color": _COLOR,
                "outline": {"type": "number", "minimum": 0, "maximum": 12},
                "shadow": {"type": "number", "minimum": 0, "maximum": 12},
                "margin_bottom": {"type": "integer", "minimum": 384, "maximum": 1400},
                "margin_left": {"type": "integer", "minimum": 0},
                "margin_right": {"type": "integer", "minimum": 150},
                "hook_font_size": {"type": "integer", "minimum": 30, "maximum": 180},
                "hook_margin_top": {"type": "integer", "minimum": 250},
            }
        ),
        "render": _obj(
            {
                "width": {"const": 1080},
                "height": {"const": 1920},
                "fps": {"type": "integer", "minimum": 23, "maximum": 60},
                "crf": {"type": "integer", "minimum": 14, "maximum": 30},
                "preset": _STR,
                "maxrate": {"type": "string", "pattern": r"^\d+(\.\d+)?[kM]$"},
                "bufsize": {"type": "string", "pattern": r"^\d+(\.\d+)?[kM]$"},
                "gop": {"type": "integer", "minimum": 1},
                "audio_bitrate": {"enum": ["96k", "128k"]},
                "audio_rate": {"enum": [44100, 48000]},
                "loudness_i": {"type": "number", "minimum": -24, "maximum": -9},
                "true_peak": {"type": "number", "maximum": -0.5},
                "lra": _NUM,
            }
        ),
        "qa": _obj(
            {
                "min_score": {"type": "integer", "minimum": 7, "maximum": 10},
                "duration_min_s": _NUM,
                "duration_max_s": _NUM,
                "loudness_tolerance_lu": {"type": "number", "minimum": 0.2, "maximum": 3},
                "max_file_mb": {"type": "number", "maximum": 300},
                "max_video_mbps": {"type": "number", "maximum": 25},
                "caption_max_chars": {"type": "integer", "maximum": HARD_CAPTION_MAX},
                "hashtags_max": {"type": "integer", "minimum": 0, "maximum": HARD_HASHTAG_CAP},
                "black_max_s": _NUM,
                "freeze_max_s": _NUM,
            }
        ),
        "schedule": _obj(
            {
                "max_posts_per_day": {"type": "integer", "minimum": 1, "maximum": HARD_MAX_POSTS_PER_DAY},
                "min_gap_hours": {"type": "number", "minimum": 1},
                "quiet_start": _HHMM,
                "quiet_end": _HHMM,
                "min_lead_minutes": {"type": "integer", "minimum": 0},
            }
        ),
        "instagram": _obj(
            {
                "api_version": {"type": "string", "pattern": r"^v\d+\.\d+$"},
                "graph_host": {"type": "string", "pattern": r"^(https://|http://127\.0\.0\.1:\d+)"},  # http only for the local mock
                "container_host": {"type": "string", "pattern": r"^(https://|http://127\.0\.0\.1:\d+)"},  # http only for the local mock
                "upload_host": {"type": "string", "pattern": r"^(https://|http://127\.0\.0\.1:\d+)"},  # http only for the local mock
                "auth_mode": {"enum": ["header", "query"]},
                "ig_user_id": {"type": "string", "pattern": r"^\d*$"},
                "share_to_feed": _BOOL,
                "send_is_ai_generated": _BOOL,
                "poll_interval_s": {"type": "integer", "minimum": 5},
                "poll_timeout_s": {"type": "integer", "minimum": 10, "maximum": 900},
                "container_expiry_hours": {"type": "number", "minimum": 1},
                "quota_safety_margin": {"type": "integer", "minimum": 0},
                "app_usage_backoff_pct": {"type": "number", "minimum": 10, "maximum": 100},
                "refresh_min_age_days": {"type": "number", "minimum": 1, "maximum": 50},
                "request_timeout_s": {"type": "number", "minimum": 5},
                "insights_checkpoints_h": {"type": "array", "items": {"type": "number", "exclusiveMinimum": 0}, "minItems": 1},
                "media_metric_groups": {"type": "array", "items": {"type": "array", "items": _STR, "minItems": 1}},
                "account_metrics": {"type": "array", "items": _STR},
                "skip_rate_is_percent": _BOOL,
            }
        ),
        "cta": _obj(
            {
                "service_enabled": _BOOL,
                "allowed": {"type": "array", "items": {"enum": list(KNOWN_CTAS)}, "minItems": 1},
            }
        ),
        "learning": _obj(
            {
                "baseline_posts": {"type": "integer", "minimum": 3},
                "rolling_window": {"type": "integer", "minimum": 5},
                "reward_checkpoint_h": _NUM,
                "explore_min": {"type": "number", "minimum": HARD_EXPLORE_MIN, "maximum": 1},
                "explore_until_posts": {"type": "integer", "minimum": 30},
                "min_n_per_arm": {"type": "integer", "minimum": 3},
                "trial_reels_min_followers": {"type": "integer", "minimum": 1000},
                "weights": _obj(
                    {
                        k: {"type": "number", "minimum": 0}
                        for k in (
                            "skip_rate",
                            "watch_ratio",
                            "shares_per_reach",
                            "saves_per_reach",
                            "likes_per_reach",
                            "views_per_follower",
                        )
                    }
                ),
            }
        ),
        "vault": _obj({"path": _STR, "subdir": {"const": "12 Social Media"}, "enabled": _BOOL}),  # pinned: the engine writes nowhere else
        "notify": _obj({"toast": _BOOL, "telegram": _BOOL}),
        "backup": _obj({"keep": {"type": "integer", "minimum": 1}, "onedrive_copy_dir": _STR, "min_free_gb": _NUM}),
        "dashboard": _obj({"host": {"enum": list(LOOPBACK_HOSTS)}, "port": {"type": "integer", "minimum": 1024, "maximum": 65535}}),
    }
)


def _semantic_errors(d: dict) -> list[str]:
    errs: list[str] = []
    try:
        ZoneInfo(d["engine"]["timezone"])
    except Exception:  # noqa: BLE001
        errs.append(f"engine.timezone: unknown zone {d['engine']['timezone']!r}")
    e = d["edit"]
    if e["gap_target_s"] >= e["gap_threshold_s"]:
        errs.append("edit.gap_target_s must be smaller than edit.gap_threshold_s")
    if e["min_output_s"] < HARD_MIN_DURATION_S or e["max_output_s"] > HARD_MAX_DURATION_S:
        errs.append(f"edit output band must stay inside {HARD_MIN_DURATION_S}-{HARD_MAX_DURATION_S} s")
    q = d["qa"]
    if q["duration_min_s"] < HARD_MIN_DURATION_S or q["duration_max_s"] > HARD_MAX_DURATION_S:
        errs.append(f"qa duration band must stay inside {HARD_MIN_DURATION_S}-{HARD_MAX_DURATION_S} s")
    if q["duration_min_s"] >= q["duration_max_s"]:
        errs.append("qa.duration_min_s must be below qa.duration_max_s")
    w = d["learning"]["weights"]
    total = sum(w.values())
    if abs(total - 1.0) > 1e-6:
        errs.append(f"learning.weights must sum to 1.0 (got {total:.3f})")
    cps = d["instagram"]["insights_checkpoints_h"]
    if cps != sorted(cps):
        errs.append("instagram.insights_checkpoints_h must be ascending")
    if d["learning"]["reward_checkpoint_h"] not in cps:
        errs.append("learning.reward_checkpoint_h must be one of instagram.insights_checkpoints_h")
    if d["cta"]["service_enabled"] is False and "service" in d["cta"]["allowed"]:
        errs.append("cta.allowed contains 'service' while cta.service_enabled is false")
    hosts = [d["instagram"][k] for k in ("graph_host", "container_host", "upload_host")]
    if d["engine"]["mode"] == "live" and any(h.startswith("http://") for h in hosts):
        errs.append("engine.mode is live but an Instagram host is the local mock (http://127.0.0.1)")
    return errs


def validate(data: dict) -> list[str]:
    """Return a list of human-readable problems (empty = valid)."""
    v = jsonschema.Draft202012Validator(SCHEMA)
    errs = [f"{'.'.join(str(p) for p in er.absolute_path) or '<root>'}: {er.message}" for er in v.iter_errors(data)]
    if errs:
        return sorted(errs)
    return _semantic_errors(data)


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    """Validated configuration with path helpers. Read access via cfg["section"]["key"]."""

    def __init__(self, data: dict, root: Path, source: Path | None):
        self.data = data
        self.root = root
        self.source = source

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def resolve(self, p: str | os.PathLike) -> Path:
        path = Path(os.path.expandvars(os.path.expanduser(str(p))))
        return path if path.is_absolute() else (self.root / path)

    # --- common locations -------------------------------------------------
    @property
    def mode(self) -> str:
        return self.data["engine"]["mode"]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.data["engine"]["timezone"])

    @property
    def data_dir(self) -> Path:
        return self.resolve(self.data["paths"]["data_dir"])

    def dir(self, name: str) -> Path:
        """A sub-directory of data/ (created on demand)."""
        p = self.data_dir / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return self.data_dir / "engine.sqlite"

    @property
    def pause_file(self) -> Path:
        return self.data_dir / "PAUSE"

    @property
    def inbox(self) -> Path:
        return self.resolve(self.data["paths"]["inbox"])

    @property
    def ffmpeg(self) -> Path:
        return self.resolve(self.data["paths"]["ffmpeg"])

    @property
    def ffprobe(self) -> Path:
        return self.resolve(self.data["paths"]["ffprobe"])

    @property
    def fonts_dir(self) -> Path:
        return self.resolve(self.data["paths"]["fonts_dir"])

    @property
    def brand_dir(self) -> Path:
        return self.resolve(self.data["paths"]["brand_dir"])

    @property
    def prompts_dir(self) -> Path:
        return self.resolve(self.data["paths"]["prompts_dir"])

    @property
    def decisions_file(self) -> Path:
        return self.root / "docs" / "DECISIONS.md"

    def with_overrides(self, overrides: dict) -> "Config":
        data = _deep_merge(self.data, overrides)
        errs = validate(data)
        if errs:
            raise ConfigError("invalid config override: " + "; ".join(errs))
        return Config(data, self.root, self.source)


def load_config(path: str | os.PathLike | None = None, overrides: dict | None = None, root: Path | None = None) -> Config:
    """Load and validate config.toml.

    Resolution: explicit path > $REELENGINE_CONFIG > <root>/config.toml > <root>/config.example.toml.
    The last one only applies to a fresh clone with no config.toml (dry-run, fictional example brand pack).
    Root: explicit > $REELENGINE_ROOT > the package's parent folder.
    """
    root = Path(root or os.environ.get("REELENGINE_ROOT") or PACKAGE_ROOT)
    src = Path(path or os.environ.get("REELENGINE_CONFIG") or (root / "config.toml"))
    if not path and not os.environ.get("REELENGINE_CONFIG") and not src.exists() and (root / "config.example.toml").exists():
        src = root / "config.example.toml"
    try:
        data = tomli.loads(src.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {src}") from exc
    except tomli.TOMLDecodeError as exc:
        raise ConfigError(f"config.toml is not valid TOML: {exc}") from exc
    if overrides:
        data = _deep_merge(data, overrides)
    errs = validate(data)
    if errs:
        raise ConfigError("config.toml is invalid:\n  - " + "\n  - ".join(errs))
    return Config(data, root, src)


_LIVE_OK = re.compile(r"^\s*[-*]?\s*(\d{4}-\d{2}-\d{2})\s+LIVE-OK\b.*$", re.MULTILINE)


def live_ok_recorded(cfg: Config) -> str | None:
    """Return the LIVE-OK line from docs/DECISIONS.md if Yazeed's go-live OK is recorded."""
    try:
        text = cfg.decisions_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    m = _LIVE_OK.search(text)
    return m.group(0).strip() if m else None
