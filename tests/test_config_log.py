"""Config validation and log/secret redaction (spec §2.2, §9, §12)."""
from __future__ import annotations

import json
import logging

import pytest

from reelengine.config import ConfigError, live_ok_recorded, load_config
from reelengine.log import MASK, RedactingFilter, dumps_redacted, redact, register_secret


def test_repo_config_is_valid_and_safe():
    cfg = load_config()
    assert cfg.mode == "dry-run"
    assert cfg["dashboard"]["host"] == "127.0.0.1"
    assert cfg["cta"]["service_enabled"] is False
    assert cfg["qa"]["hashtags_max"] <= 5
    assert cfg["schedule"]["max_posts_per_day"] == 1
    assert cfg["vault"]["enabled"] is False


@pytest.mark.parametrize(
    "override",
    [
        {"dashboard": {"host": "0.0.0.0"}},
        {"qa": {"hashtags_max": 6}},
        {"qa": {"min_score": 5}},
        {"qa": {"duration_max_s": 120}},
        {"learning": {"explore_min": 0.1}},
        {"learning": {"weights": {"skip_rate": 0.9}}},
        {"engine": {"mode": "yolo"}},
        {"engine": {"mode": "live"}, "instagram": {"graph_host": "http://127.0.0.1:9"}},
        {"instagram": {"graph_host": "http://evil.example"}},
        {"captions": {"margin_bottom": 100}},
        {"cta": {"allowed": ["save", "service"]}},
        {"schedule": {"max_posts_per_day": 10}},
        {"edit": {"gap_target_s": 0.5}},
        {"engine": {"timezone": "Mars/Olympus"}},
    ],
)
def test_invalid_configs_rejected(override):
    with pytest.raises(ConfigError):
        load_config(overrides=override)


def test_live_ok_detection(tmp_path, monkeypatch):
    cfg = load_config()
    (tmp_path / "docs").mkdir()
    monkeypatch.setattr(type(cfg), "decisions_file", property(lambda self: tmp_path / "docs" / "DECISIONS.md"))
    assert live_ok_recorded(cfg) is None
    (tmp_path / "docs" / "DECISIONS.md").write_text("# Decisions\n- 2026-10-05 LIVE-OK: Yazeed approved going live\n", encoding="utf-8")
    assert "LIVE-OK" in live_ok_recorded(cfg)


TOKEN = "IGAAQ9xYzAbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


@pytest.mark.parametrize(
    "text",
    [
        f"GET https://graph.instagram.com/v26.0/me?fields=id&access_token={TOKEN}",
        f"Authorization: Bearer {TOKEN}",
        f"Authorization: OAuth {TOKEN}",
        f'{{"access_token": "{TOKEN}", "token_type": "bearer"}}',
        f"token is {TOKEN} ok",
        "fb EAABfakeMetaTokenForTheRedactionTest0000000000",
        "client_secret=abcdef0123456789abcdef&x=1",
    ],
)
def test_redaction_patterns(text):
    out = redact(text)
    assert MASK in out
    assert TOKEN not in out and "EAABfakeMeta" not in out and "abcdef0123456789abcdef" not in out


def test_registered_secret_and_hashes():
    register_secret("my-very-secret-value-42")
    assert "my-very-secret-value-42" not in redact("x my-very-secret-value-42 y")
    sha = "f0e46253c70dfe902bac915dfb4224f0cf1b7c6eeab9da2ccc9a5581f9a71b13"
    assert redact(sha) == sha  # plain hex hashes stay readable


def test_paging_dropped_from_stored_payloads():
    payload = {"data": [{"id": "1"}], "paging": {"next": f"https://graph.instagram.com/x?access_token={TOKEN}"}}
    s = dumps_redacted(payload)
    assert TOKEN not in s and "paging" not in json.loads(s)


def test_logger_filter_redacts_messages_and_exceptions():
    rec = logging.LogRecord("reelengine.t", logging.ERROR, __file__, 1, "call %s failed", (f"access_token={TOKEN}",), None)
    try:
        raise RuntimeError(f"boom {TOKEN}")
    except RuntimeError:
        import sys

        rec.exc_info = sys.exc_info()
    RedactingFilter().filter(rec)
    assert TOKEN not in rec.getMessage() and TOKEN not in (rec.exc_text or "")
