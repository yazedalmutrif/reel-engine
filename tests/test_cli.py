"""The `reel` command line (the contract agents and the /reel skill rely on).

Every test runs against a temp copy of config.toml whose data_dir/inbox are in tmp_path,
a temp DECISIONS.md, and the in-memory keyring from conftest. Nothing here can reach
Instagram: the only command that makes API calls (`connect`) points at the local mock.
"""
from __future__ import annotations

import argparse
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from mock_graph import VALID
from reelengine import cli, db, doctor, secrets
from reelengine.config import Config, load_config

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ["status", "tick", "process", "show", "rerender", "held", "approve", "pause", "resume", "mode", "stats", "export",
            "brief", "review", "doctor", "dashboard", "connect", "refresh-token"]
NEEDS_JSON = ["status", "tick", "process", "show", "rerender", "held", "approve", "stats", "brief", "review", "doctor", "refresh-token"]


class TTYInput(io.StringIO):
    def isatty(self) -> bool:
        return True


def _write_config(tmp_path: Path, graph_base: str | None = None) -> Path:
    src = ROOT / "config.toml" if (ROOT / "config.toml").exists() else ROOT / "config.example.toml"  # fresh clone
    text = src.read_text(encoding="utf-8")
    subs = {
        "data_dir": (tmp_path / "data").as_posix(),
        "inbox": (tmp_path / "inbox").as_posix(),
        "onedrive_copy_dir": (tmp_path / "onedrive").as_posix(),
    }
    for key, val in subs.items():
        text, n = re.subn(rf'(?m)^{key}\s*=.*$', f'{key} = "{val}"', text)
        assert n == 1, key
    text, n = re.subn(r"(?m)^toast\s*=.*$", "toast = false", text)
    assert n == 1
    if graph_base:
        for key in ("graph_host", "container_host", "upload_host"):
            text, n = re.subn(rf'(?m)^{key}\s*=.*$', f'{key} = "{graph_base}"', text)
            assert n == 1, key
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    cfg = load_config(path)
    assert cfg.data_dir == tmp_path / "data" and cfg["vault"]["enabled"] is False
    return path


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    decisions = tmp_path / "DECISIONS.md"
    monkeypatch.setattr(Config, "decisions_file", property(lambda self: decisions))
    monkeypatch.delenv("REELENGINE_CONFIG", raising=False)
    path = _write_config(tmp_path)
    return SimpleNamespace(path=path, data=tmp_path / "data", decisions=decisions, cfg=lambda: load_config(path))


def run(env, capsys, *args, stdin: io.StringIO | None = None, monkeypatch=None) -> tuple[int, str, str]:
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", stdin)
    code = cli.main(["--config", str(env.path), *args])
    out, err = capsys.readouterr()
    return code, out, err


# ------------------------------------------------------------------ parser contract
def _subparsers(p: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    return next(a for a in p._actions if isinstance(a, argparse._SubParsersAction)).choices


def test_parser_has_every_contract_command():
    subs = _subparsers(cli.build_parser())
    missing = [c for c in CONTRACT if c not in subs]
    assert missing == []
    no_json = [c for c in NEEDS_JSON if "--json" not in subs[c]._option_string_actions]
    assert no_json == []


@pytest.mark.parametrize("argv", [
    ["status", "--json"], ["tick", "--dry-run", "--no-toast", "--json"], ["process", "clip.mov", "--json"], ["show", "3", "--json"],
    ["rerender", "3", "--style", "font_size=110", "margin_bottom=700"], ["held", "--json"], ["approve", "3", "--json"],
    ["pause"], ["resume"], ["mode", "live"], ["mode", "dry-run"], ["stats", "--since", "2026-09-01", "--json"],
    ["export", "--since", "2026-09-01", "--out", "x.csv"], ["brief", "--now", "--json"], ["review", "--json"],
    ["doctor", "--live", "--json"], ["dashboard", "--build-only", "--demo", "--port", "8766"], ["connect", "--stdin"],
    ["refresh-token", "--json"], ["secret", "set", "ig_access_token", "--stdin"], ["requeue", "3"], ["daily"], ["weekly"],
])
def test_contract_invocations_parse(argv):
    a = cli.build_parser().parse_args(argv)
    assert callable(a.fn)


@pytest.mark.parametrize("argv", [["mode", "yolo"], ["secret", "set", "password"], ["show"], ["show", "abc"], []])
def test_bad_invocations_are_rejected(argv, capsys):
    with pytest.raises(SystemExit) as ei:
        cli.build_parser().parse_args(argv)
    assert ei.value.code == 2
    capsys.readouterr()


# ------------------------------------------------------------------ kill switch
def test_pause_creates_the_file_and_resume_removes_it(cli_env, capsys):
    pause = cli_env.data / "PAUSE"
    code, out, _ = run(cli_env, capsys, "pause")
    assert code == 0 and pause.exists() and out.startswith("PAUSED")
    assert pause == cli_env.cfg().pause_file

    code, out, _ = run(cli_env, capsys, "status", "--json")
    st = json.loads(out)
    assert code == 0 and st["paused"] and st["paused"].startswith("paused by")

    code, out, _ = run(cli_env, capsys, "resume")
    assert code == 0 and not pause.exists() and out.startswith("RESUMED")
    assert json.loads(run(cli_env, capsys, "status", "--json")[1])["paused"] is None


def test_resume_warns_when_instagram_needs_reconnecting(cli_env, capsys):
    run(cli_env, capsys, "pause")
    conn = db.connect(cli_env.cfg().db_path)
    conn.execute("UPDATE token_state SET status='auth_needed' WHERE id=1")
    conn.close()
    code, out, _ = run(cli_env, capsys, "resume")
    assert code == 1 and "reel connect" in out
    assert not (cli_env.data / "PAUSE").exists()


# ------------------------------------------------------------------ mode
def test_mode_live_refuses_without_live_ok(cli_env, capsys, monkeypatch):
    monkeypatch.setattr(doctor, "run", lambda cfg, conn, live=False, probe_container=False: {"status": "ok", "checks": []})
    secrets.set_secret(secrets.ACCESS_TOKEN, VALID)
    assert not cli_env.decisions.exists()

    code, out, _ = run(cli_env, capsys, "mode", "live", stdin=TTYInput("GO LIVE\n"), monkeypatch=monkeypatch)
    assert code == 2 and "Refusing to go live" in out and "no LIVE-OK line" in out
    assert cli_env.cfg().mode == "dry-run"

    # a LIVE-OK that isn't on its own dated line doesn't count
    cli_env.decisions.write_text("# Decisions\nWe will record LIVE-OK later.\n", encoding="utf-8")
    code, out, _ = run(cli_env, capsys, "mode", "live", stdin=TTYInput("GO LIVE\n"), monkeypatch=monkeypatch)
    assert code == 2 and "no LIVE-OK line" in out and cli_env.cfg().mode == "dry-run"


def test_mode_live_needs_token_and_passing_doctor(cli_env, capsys, monkeypatch):
    cli_env.decisions.write_text("- 2026-10-05 LIVE-OK: Yazeed approved going live\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "run", lambda cfg, conn, live=False, probe_container=False: {
        "status": "fail", "checks": [{"check": "ffmpeg", "status": "fail", "detail": "not found"}]})
    code, out, _ = run(cli_env, capsys, "mode", "live", stdin=TTYInput("GO LIVE\n"), monkeypatch=monkeypatch)
    assert code == 2 and "no Instagram token" in out and "ffmpeg: not found" in out and "LIVE-OK" not in out
    assert cli_env.cfg().mode == "dry-run"


def test_mode_live_needs_an_interactive_typed_confirmation(cli_env, capsys, monkeypatch):
    cli_env.decisions.write_text("- 2026-10-05 LIVE-OK: Yazeed approved going live\n", encoding="utf-8")
    secrets.set_secret(secrets.ACCESS_TOKEN, VALID)
    monkeypatch.setattr(doctor, "run", lambda cfg, conn, live=False, probe_container=False: {"status": "ok", "checks": []})

    code, out, _ = run(cli_env, capsys, "mode", "live", stdin=io.StringIO("GO LIVE\n"), monkeypatch=monkeypatch)  # piped
    assert code == 2 and "interactively" in out and cli_env.cfg().mode == "dry-run"
    code, out, _ = run(cli_env, capsys, "mode", "live", stdin=TTYInput("yes\n"), monkeypatch=monkeypatch)
    assert code == 1 and "Not confirmed" in out and cli_env.cfg().mode == "dry-run"

    code, out, _ = run(cli_env, capsys, "mode", "live", stdin=TTYInput("GO LIVE\n"), monkeypatch=monkeypatch)
    assert code == 0 and "Mode: LIVE" in out and cli_env.cfg().mode == "live"
    assert json.loads(run(cli_env, capsys, "status", "--json")[1])["mode"] == "live"

    code, out, _ = run(cli_env, capsys, "mode", "dry-run")
    assert code == 0 and cli_env.cfg().mode == "dry-run"


# ------------------------------------------------------------------ read-only commands
def test_status_json_is_valid_json(cli_env, capsys):
    conn = db.connect(cli_env.cfg().db_path)
    rid = db.insert_reel(conn, "abc", "IMG_1.MOV", "raw/x.mov", state="scheduled", scheduled_at="2026-10-01T15:00:00+00:00")
    db.insert_reel(conn, "def", "IMG_2.MOV", "raw/y.mov", state="held", held_reason="QA: too quiet")
    conn.close()
    code, out, _ = run(cli_env, capsys, "status", "--json")
    st = json.loads(out)
    assert code == 0
    assert st["mode"] == "dry-run" and st["config_mode"] == "dry-run" and st["live_ok_recorded"] is False
    assert st["paused"] is None and st["queue"] == {"scheduled": 1, "held": 1}
    assert st["next_post"] == {"reel": rid, "at": "2026-10-01 18:00"}  # Asia/Riyadh
    assert st["token"] == {"status": "missing", "days_left": None}

    code, out, _ = run(cli_env, capsys, "status")
    assert code == 0 and out.startswith("Mode: DRY-RUN") and "Next post: #1 at 2026-10-01 18:00" in out


def test_show_and_held_json(cli_env, capsys):
    conn = db.connect(cli_env.cfg().db_path)
    rid = db.insert_reel(conn, "abc", "IMG_1.MOV", "raw/x.mov")
    db.transition(conn, rid, "held", "QA", held_reason="QA: too quiet")
    conn.close()
    d = json.loads(run(cli_env, capsys, "show", str(rid), "--json")[1])
    assert d["state"] == "held" and d["held_reason"] == "QA: too quiet" and [e["to_state"] for e in d["events"]] == ["new", "held"]
    held = json.loads(run(cli_env, capsys, "held", "--json")[1])
    assert [(h["id"], h["reason"]) for h in held] == [(rid, "QA: too quiet")]


# ------------------------------------------------------------------ secrets
def test_secret_set_stdin_stores_in_keyring_and_prints_only_stored(cli_env, capsys, monkeypatch, memory_keyring):
    value = "AppSecretValue0123456789abcdefXYZqwerty"
    code, out, err = run(cli_env, capsys, "secret", "set", "ig_app_secret", "--stdin", stdin=io.StringIO(value + "\n"), monkeypatch=monkeypatch)
    assert code == 0 and out.strip() == "stored"
    assert value not in out and value not in err
    assert memory_keyring.store == {(secrets.SERVICE, "ig_app_secret"): value}
    assert not list(cli_env.data.rglob("*")) or all(value.encode() not in p.read_bytes() for p in cli_env.data.rglob("*") if p.is_file())


def test_secret_set_refuses_the_instagram_token(cli_env, capsys, monkeypatch, memory_keyring):
    # fact-check #16: the token must go through `reel connect` (verified with /me + token_state)
    value = "IGAAcliSecretValue0123456789abcdefXYZ"
    code, out, err = run(cli_env, capsys, "secret", "set", "ig_access_token", "--stdin", stdin=io.StringIO(value + "\n"), monkeypatch=monkeypatch)
    assert code == 2 and "reel connect" in out
    assert memory_keyring.store == {} and value not in out + err


def test_connect_stdin_uses_the_token_without_printing_it(tmp_path, capsys, monkeypatch, mock_graph):
    monkeypatch.setattr(Config, "decisions_file", property(lambda self: tmp_path / "DECISIONS.md"))
    env = SimpleNamespace(path=_write_config(tmp_path, graph_base=mock_graph.base))
    assert load_config(env.path)["instagram"]["graph_host"] == mock_graph.base
    code, out, err = run(env, capsys, "connect", "--stdin", stdin=io.StringIO(VALID + "\n"), monkeypatch=monkeypatch)
    assert code == 0 and "Connected @yazeed.test" in out
    assert VALID not in out and VALID not in err
    assert secrets.get_secret(secrets.ACCESS_TOKEN) == VALID

    code, out, _ = run(env, capsys, "connect", "--stdin", stdin=io.StringIO("IGAAbogusTokenThatTheMockRejects0000\n"), monkeypatch=monkeypatch)
    assert code == 2 and "Token rejected" in out and "IGAAbogus" not in out
    assert secrets.get_secret(secrets.ACCESS_TOKEN) == VALID
