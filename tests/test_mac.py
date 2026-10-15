"""macOS slice of v1.2: transcription device choice, claude CLI discovery, the mac example
config, doctor's mac lines + the FriBidi probe, OS-neutral secret messages, setup-mac.sh,
and the `reel schedule` / `reel notify` commands. Subprocess, launchctl and HTTP are mocked;
no test reaches api.telegram.org, launchd, the Keychain or the network."""
from __future__ import annotations

import io
import logging
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from reelengine import cli, doctor, macsched, notify, secrets, transcribe
from reelengine import brain as brain_mod
from reelengine import ffmpeg as ffmpeg_mod
from reelengine.config import load_config

ROOT = Path(__file__).resolve().parents[1]
FAKE_BOT_TOKEN = "123456789:" + "AAbbCCddEEffGGhhIIjjKKllMMnnOOpp_-"  # token-shaped, not a real token


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Any real requests.Session call to api.telegram.org fails the test."""
    real = requests.Session.request

    def guarded(self, method, url, *a, **k):
        if "api.telegram.org" in str(url):
            raise AssertionError("a test tried to reach the real Telegram API")
        return real(self, method, url, *a, **k)

    monkeypatch.setattr(requests.Session, "request", guarded)
    monkeypatch.setattr(notify, "_http", None)


# ------------------------------------------------------------------ transcription
def test_cuda_available_on_darwin_is_quick_false(monkeypatch):
    monkeypatch.setattr(transcribe.sys, "platform", "darwin")
    assert transcribe.cuda_available() == (False, "macOS: no CUDA; CPU int8")


@pytest.mark.parametrize("device", ["cuda", "auto"])
def test_darwin_goes_straight_to_cpu_int8_without_warning(cfg, monkeypatch, device, caplog):
    c = cfg.with_overrides({"whisper": {"device": device}})
    loads = []
    monkeypatch.setattr(transcribe.sys, "platform", "darwin")
    monkeypatch.setattr(transcribe, "_model", None)
    monkeypatch.setattr(transcribe, "_model_key", None)
    monkeypatch.setattr(transcribe, "_load", lambda path, dev, ct: loads.append((dev, ct)) or object())
    monkeypatch.setattr(transcribe, "add_cuda_dll_dirs", lambda: pytest.fail("no CUDA setup on macOS"))
    lg = logging.getLogger("reelengine")
    lg.addHandler(caplog.handler)
    try:
        caplog.set_level(logging.INFO, logger="reelengine")
        _, used = transcribe.get_model(c)
    finally:
        lg.removeHandler(caplog.handler)
    assert used == "cpu" and loads == [("cpu", "int8")]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.skipif(sys.platform != "darwin", reason="checks this Mac's ctranslate2 build")
def test_ctranslate2_on_this_mac_has_no_gpu_backend_but_cpu_int8():
    import ctranslate2

    assert ctranslate2.get_cuda_device_count() == 0
    assert "int8" in ctranslate2.get_supported_compute_types("cpu")


# ------------------------------------------------------------------ claude CLI discovery
def _exe(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return p


@pytest.fixture
def mac_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(brain_mod.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(brain_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(brain_mod.sys, "platform", "darwin")
    real_is_file = Path.is_file
    # hide this machine's /opt/homebrew and /usr/local so only tmp paths count
    monkeypatch.setattr(Path, "is_file", lambda self: False if str(self).startswith(("/opt/homebrew", "/usr/local")) else real_is_file(self))
    return home


def test_resolve_cli_prefers_local_bin(mac_home):
    a = _exe(mac_home / ".local" / "bin" / "claude")
    _exe(mac_home / ".claude" / "local" / "claude")
    assert brain_mod.resolve_cli() == a


def test_resolve_cli_claude_local(mac_home):
    b = _exe(mac_home / ".claude" / "local" / "claude")
    assert brain_mod.resolve_cli() == b


def test_resolve_cli_newest_desktop_app_binary(mac_home):
    base = mac_home / "Library" / "Application Support" / "Claude" / "claude-code"
    rel = Path("claude.app") / "Contents" / "MacOS" / "claude"
    _exe(base / "2.1.293" / "8433d0d9cd0d" / rel)
    newest = _exe(base / "2.1.1000" / "aaaa" / rel)  # version-sorted, not string-sorted
    _exe(base / "2.1.295" / "9d6405864fdc" / rel)
    assert brain_mod.resolve_cli() == newest


def test_resolve_cli_none_found(mac_home):
    assert brain_mod.resolve_cli() is None


def test_resolve_cli_path_wins(mac_home, monkeypatch):
    monkeypatch.setattr(brain_mod.shutil, "which", lambda name: "/somewhere/claude")
    _exe(mac_home / ".local" / "bin" / "claude")
    assert brain_mod.resolve_cli() == Path("/somewhere/claude")


def test_resolve_cli_windows_ignores_mac_paths(mac_home, monkeypatch):
    monkeypatch.setattr(brain_mod.sys, "platform", "win32")
    _exe(mac_home / ".local" / "bin" / "claude")
    vs = mac_home / ".vscode" / "extensions" / "anthropic.claude-code-2.1.283-win32-x64" / "resources" / "native-binary" / "claude.exe"
    _exe(vs)
    assert brain_mod.resolve_cli() == vs


# ------------------------------------------------------------------ configs
def test_mac_example_config_validates():
    c = load_config(ROOT / "config.mac.example.toml", root=ROOT)
    assert c.mode == "dry-run"
    assert c.inbox == Path.home() / "Movies" / "Reel Inbox"  # "~" expanded by cfg.resolve
    assert c.ffmpeg == ROOT / "bin" / "ffmpeg-mac" / "ffmpeg" and c.ffprobe == ROOT / "bin" / "ffmpeg-mac" / "ffprobe"
    w = c["whisper"]
    assert (w["device"], w["compute_type"], w["cpu_compute_type"]) == ("cpu", "int8", "int8")
    assert c["brain"]["cli_path"] == "" and c["vault"]["enabled"] is False
    assert c["notify"]["toast"] is True and c["notify"]["telegram"] is False
    assert c["backup"]["onedrive_copy_dir"] == "data/backup-copy" and c["paths"]["brand_dir"] == "brand.example"


def test_example_config_still_validates():
    c = load_config(ROOT / "config.example.toml", root=ROOT)
    assert c["notify"]["telegram"] is False and c["notify"]["telegram_chat_ids"] == []


def test_mac_example_has_every_section_of_the_main_example():
    import tomli

    main = tomli.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))
    mac = tomli.loads((ROOT / "config.mac.example.toml").read_text(encoding="utf-8"))
    assert set(main) == set(mac)
    for k in main:
        if isinstance(main[k], dict):
            assert set(main[k]) == set(mac[k]), k


def test_old_notify_section_still_loads(tmp_path):
    text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    text = re.sub(r"(?ms)^\[notify\].*?(?=^\[backup\])", "[notify]\ntoast = true\ntelegram = false\n\n", text)
    p = tmp_path / "old.toml"
    p.write_text(text, encoding="utf-8")
    c = load_config(p, root=ROOT)
    assert c["notify"]["telegram_kinds"] == ["occasion", "action", "error"]


# ------------------------------------------------------------------ secrets / ffmpeg messages
def test_secret_missing_message_is_os_neutral():
    with pytest.raises(secrets.SecretMissing) as ei:
        secrets.get_secret(secrets.TELEGRAM_TOKEN)
    msg = str(ei.value)
    assert "macOS Keychain" in msg and "Windows Credential Manager" in msg and "reel notify setup-telegram" in msg


@pytest.mark.skipif(sys.platform != "darwin", reason="checks this Mac's real keyring backend (read-only)")
def test_real_backend_on_this_mac_is_keychain(memory_keyring):
    from keyring.backends import macOS

    assert macOS.Keyring.priority > 0  # the backend keyring picks here; tests still use the in-memory one


@pytest.mark.parametrize("plat,hint", [("darwin", "scripts/setup-mac.sh"), ("win32", "scripts/setup.ps1")])
def test_ffmpeg_missing_binary_hint(monkeypatch, tmp_path, plat, hint):
    monkeypatch.setattr(ffmpeg_mod.sys, "platform", plat)
    with pytest.raises(ffmpeg_mod.FFmpegError, match=re.escape(hint)):
        ffmpeg_mod.run(tmp_path / "nope", ["-version"])


# ------------------------------------------------------------------ doctor
def _proc(stdout=b"", stderr=b"", rc=0):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


VERSION = b"ffmpeg version 9.0.2 Copyright\nconfiguration: --enable-libass --enable-libharfbuzz --enable-libx264 --enable-libzimg\n"


def test_ffmpeg_check_fribidi_via_libass(cfg, monkeypatch):
    calls = []

    def fake_run(binary, args, **kw):
        calls.append((list(args), kw))
        if "-version" in args:
            return _proc(VERSION)
        assert kw["cwd"] and kw["timeout"] and kw["check"] is False
        assert (Path(kw["cwd"]) / "probe.ass").read_text(encoding="utf-8").count("مرحبا") == 1
        return _proc(stderr=b"[Parsed_ass_0 @ 0x1] libass API version: 0x1703000\n[Parsed_ass_0 @ 0x1] Shaper: FriBidi 1.0.16 (SIMPLE) HarfBuzz-ng 14.4.0 (COMPLEX)\n")

    monkeypatch.setattr(doctor, "run_bin", fake_run)
    status, detail = doctor.ffmpeg_check(cfg)
    assert status == "ok" and "FriBidi via libass" in detail and "Shaper: FriBidi 1.0.16" in detail
    probe_args = calls[1][0]
    assert "-vf" in probe_args and probe_args[probe_args.index("-vf") + 1] == "ass=probe.ass" and "verbose" in probe_args


def test_ffmpeg_check_flag_skips_probe(cfg, monkeypatch):
    v = VERSION.replace(b"--enable-libzimg", b"--enable-libzimg --enable-libfribidi")
    monkeypatch.setattr(doctor, "run_bin", lambda b, a, **k: _proc(v) if "-version" in a else pytest.fail("no probe needed"))
    assert doctor.ffmpeg_check(cfg) == ("ok", "ffmpeg version 9.0.2 Copyright; FriBidi: --enable-libfribidi")


def test_ffmpeg_check_fails_without_fribidi_or_flag(cfg, monkeypatch):
    v = VERSION.replace(b" --enable-libzimg", b"")
    monkeypatch.setattr(doctor, "run_bin", lambda b, a, **k: _proc(v) if "-version" in a else _proc(stderr=b"Shaper: SIMPLE\n"))
    status, detail = doctor.ffmpeg_check(cfg)
    assert status == "fail" and "FriBidi missing" in detail and "--enable-libzimg" in detail


def test_doctor_mac_lines(cfg, conn, monkeypatch, memory_keyring):
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    monkeypatch.setattr(doctor, "run_bin", lambda b, a, **k: _proc(VERSION) if "-version" in a else _proc(stderr=b"Shaper: FriBidi 1.0.16"))
    monkeypatch.setattr(doctor, "resolve_cli", lambda p: None)
    monkeypatch.setattr(macsched, "status", lambda **k: {l: {"exists": False} for l in macsched.LABELS.values()})
    monkeypatch.setattr(macsched, "tz_warning", lambda c: None)
    c = cfg.with_overrides({"notify": {"toast": True, "telegram": True, "telegram_chat_ids": ["123"]}})
    secrets.set_secret(secrets.TELEGRAM_TOKEN, FAKE_BOT_TOKEN)
    checks = {x["check"]: x for x in doctor.offline_checks(c, conn)}
    assert "(" in checks["os"]["detail"]
    assert checks["cuda"]["detail"].startswith("n/a on macOS: CPU int8 (ctranslate2 has no Metal backend)")
    assert checks["scheduled_tasks"]["status"] == "warn" and "install --mac after Yazeed's OK" in checks["scheduled_tasks"]["detail"]
    assert checks["desktop_notifications"]["detail"].startswith("macOS notifications via /usr/bin/osascript") or checks["desktop_notifications"]["status"] == "warn"
    assert checks["telegram"] == {"check": "telegram", "status": "ok", "detail": checks["telegram"]["detail"]}
    assert "1 chat id(s)" in checks["telegram"]["detail"]
    blob = str(checks)
    assert FAKE_BOT_TOKEN not in blob and "'123'" not in blob
    assert "~/.local/bin" in checks["claude_cli"]["detail"]
    if not cfg.inbox.exists():
        assert "setup-mac.sh" in checks["inbox"]["detail"]


def test_doctor_telegram_token_missing_is_warn(cfg, conn, monkeypatch):
    c = cfg.with_overrides({"notify": {"telegram": True, "telegram_chat_ids": ["123"]}})
    assert doctor._telegram_check(c) == ("warn", "on with 1 chat id(s), but the bot token is missing (run `reel notify setup-telegram`)")
    assert doctor._telegram_check(cfg)[0] == "ok"


def test_doctor_windows_still_uses_winsched(cfg, conn, monkeypatch):
    from reelengine import winsched

    monkeypatch.setattr(doctor.sys, "platform", "win32")
    monkeypatch.setattr(doctor, "run_bin", lambda b, a, **k: _proc(VERSION + b" --enable-libfribidi"))
    monkeypatch.setattr(doctor, "resolve_cli", lambda p: None)
    monkeypatch.setattr(winsched, "tasks_status", lambda: {n: {"exists": True} for n in winsched.TASK_NAMES})
    monkeypatch.setattr(macsched, "status", lambda **k: pytest.fail("no launchctl on Windows"))
    checks = {x["check"]: x for x in doctor.offline_checks(cfg, conn)}
    assert checks["scheduled_tasks"] == {"check": "scheduled_tasks", "status": "ok", "detail": "all registered"}
    assert "ctranslate2 has no Metal" not in checks["cuda"]["detail"]


# ------------------------------------------------------------------ setup-mac.sh (static)
SETUP = ROOT / "scripts" / "setup-mac.sh"


def test_setup_mac_script_static_rules():
    text = SETUP.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text
    assert os.access(SETUP, os.X_OK)
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "sudo " not in code and "brew " not in code
    assert re.search(r'^FFMPEG_SHA256="[0-9a-f]{64}"', text, re.M) and re.search(r'^FFPROBE_SHA256="[0-9a-f]{64}"', text, re.M)
    assert re.search(r'^FFMPEG_URL="https://', text, re.M) and re.search(r'^FFPROBE_URL="https://', text, re.M)
    assert "shasum -a 256 -c" in code and "com.apple.quarantine" in code and "--skip-ffmpeg" in code and "--dry-run" in code
    for flag in ("--enable-libass", "--enable-libharfbuzz", "--enable-libx264", "--enable-libzimg"):
        assert flag in text
    assert "Shaper: FriBidi" in code
    assert "nvidia-|pywin32-ctypes|pyreadline3" in code
    assert 'cp "$ROOT/config.mac.example.toml" "$ROOT/config.toml"' in code and '[ -f "$ROOT/config.toml" ]' in code


@pytest.mark.skipif(sys.platform == "win32", reason="bash")
def test_setup_mac_script_parses():
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash")
    assert subprocess.run([bash, "-n", str(SETUP)], capture_output=True, timeout=30).returncode == 0  # nosec B603


# ------------------------------------------------------------------ reel schedule
def _args(**kw):
    base = {"json": False, "mac": False, "out": None, "yes": False, "stdin": False, "verify": False, "channel": "all"}
    base.update(kw)
    return SimpleNamespace(**base)


def test_cli_schedule_plist(cfg, tmp_path, capsys):
    out = tmp_path / "pl"
    assert cli.cmd_schedule(cfg, _args(action="plist", out=str(out))) == 0
    assert len(list(out.glob("local.reelengine.*.plist"))) == 3
    text = capsys.readouterr().out
    assert "launchctl bootstrap gui/" in text and "nothing installed" in text


def test_cli_install_refuses_without_phrase(cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(macsched, "install", lambda *a, **k: pytest.fail("must not install"))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))  # not a tty
    assert cli.cmd_schedule(cfg, _args(action="install", mac=True)) == 1
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "install")  # wrong case
    assert cli.cmd_schedule(cfg, _args(action="install", mac=True)) == 1
    assert "This will:" in capsys.readouterr().out


def test_cli_install_with_phrase_calls_install(cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    seen = []
    monkeypatch.setattr(macsched, "install", lambda c, **k: seen.append(1) or ["installed x"])
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "INSTALL")
    assert cli.cmd_schedule(cfg, _args(action="install", mac=True)) == 0 and seen == [1]


def test_cli_install_needs_mac_flag_and_macos(cfg, monkeypatch):
    monkeypatch.setattr(macsched, "install", lambda *a, **k: pytest.fail("must not install"))
    assert cli.cmd_schedule(cfg, _args(action="install")) == 2
    monkeypatch.setattr(cli.sys, "platform", "linux")
    assert cli.cmd_schedule(cfg, _args(action="install", mac=True, yes=True)) == 2


def test_cli_uninstall_refuses_without_phrase(cfg, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(macsched, "uninstall", lambda *a, **k: pytest.fail("must not uninstall"))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    assert cli.cmd_schedule(cfg, _args(action="uninstall", mac=True)) == 1


def test_cli_status_mac(cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(macsched, "status", lambda **k: {"local.reelengine.tick": {"exists": True, "state": "not running", "last_exit_code": "0", "runs": 3},
                                                          "local.reelengine.daily": {"exists": False, "plist_installed": False}})
    monkeypatch.setattr(macsched, "tz_warning", lambda c: None)
    assert cli.cmd_schedule(cfg, _args(action="status", mac=True)) == 0
    out = capsys.readouterr().out
    assert "local.reelengine.tick: loaded; state=not running; last exit code=0; runs=3" in out and "daily: not loaded" in out


def test_cli_status_windows_uses_winsched(cfg, monkeypatch, capsys):
    from reelengine import winsched

    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(winsched, "tasks_status", lambda: {"Brain - Reel Engine Tick": {"exists": True}})
    monkeypatch.setattr(macsched, "status", lambda **k: pytest.fail("no launchctl on Windows"))
    assert cli.cmd_schedule(cfg, _args(action="status")) == 0
    assert "Brain - Reel Engine Tick" in capsys.readouterr().out


def test_schedule_parser():
    a = cli.build_parser().parse_args(["schedule", "install", "--mac", "--yes"])
    assert (a.action, a.mac, a.yes, a.fn) == ("install", True, True, cli.cmd_schedule)


# ------------------------------------------------------------------ reel notify
class FakeTG:
    def __init__(self, data):
        self.data, self.calls = data, []

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return SimpleNamespace(status_code=200, json=lambda: self.data)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {}})


def test_setup_telegram_rejects_bad_shape(cfg, monkeypatch, memory_keyring, capsys):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("not-a-token\n"))
    assert cli.cmd_notify(cfg, _args(action="setup-telegram", stdin=True)) == 2
    assert not secrets.has_secret(secrets.TELEGRAM_TOKEN)
    assert "not-a-token" not in capsys.readouterr().out


def test_setup_telegram_stores_and_prints_next_steps_without_network(cfg, monkeypatch, memory_keyring, capsys):
    monkeypatch.setattr(notify, "telegram_api", lambda *a, **k: pytest.fail("no network unless --verify"))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(FAKE_BOT_TOKEN + "\n"))
    assert cli.cmd_notify(cfg, _args(action="setup-telegram", stdin=True)) == 0
    assert memory_keyring.get_password(secrets.SERVICE, secrets.TELEGRAM_TOKEN) == FAKE_BOT_TOKEN
    out = capsys.readouterr().out
    assert FAKE_BOT_TOKEN not in out
    assert "telegram = true" in out and 'telegram_chat_ids = ["<your chat id>"]' in out and "reel notify telegram-chat-id" in out


def test_setup_telegram_verify_uses_get_me_only(cfg, monkeypatch, memory_keyring, capsys):
    fake = FakeTG({"ok": True, "result": {"username": "reel_test_bot"}})
    monkeypatch.setattr(notify, "_http", fake)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(FAKE_BOT_TOKEN + "\n"))
    assert cli.cmd_notify(cfg, _args(action="setup-telegram", stdin=True, verify=True)) == 0
    assert [(m, u.rsplit("/", 1)[1]) for m, u, _ in fake.calls] == [("GET", "getMe")]
    assert "@reel_test_bot" in capsys.readouterr().out


def test_telegram_chat_id_lists_distinct_chats(cfg, monkeypatch, memory_keyring, capsys):
    secrets.set_secret(secrets.TELEGRAM_TOKEN, FAKE_BOT_TOKEN)
    upd = {"ok": True, "result": [
        {"message": {"chat": {"id": 555, "first_name": "Yazeed"}, "text": "/start"}},
        {"message": {"chat": {"id": 555, "first_name": "Yazeed"}, "text": "hi"}},
        {"message": {"chat": {"id": -100777, "title": "group"}}},
    ]}
    fake = FakeTG(upd)
    monkeypatch.setattr(notify, "_http", fake)
    assert cli.cmd_notify(cfg, _args(action="telegram-chat-id")) == 0
    out = capsys.readouterr().out
    assert out.count("555") == 1 and "-100777" in out and "Yazeed" in out
    assert all(m == "GET" for m, _, _ in fake.calls)


def test_notify_test_reports_channels(cfg, monkeypatch, memory_keyring, capsys):
    c = cfg.with_overrides({"notify": {"toast": False, "telegram": True, "telegram_chat_ids": ["555"]}})
    secrets.set_secret(secrets.TELEGRAM_TOKEN, FAKE_BOT_TOKEN)
    fake = FakeTG({})
    monkeypatch.setattr(notify, "_http", fake)
    assert cli.cmd_notify(c, _args(action="test", channel="telegram")) == 0
    assert "sent via: dashboard, telegram" in capsys.readouterr().out
    assert fake.calls[0][2]["json"]["text"] == "Test notification\n\nDetails on the dashboard."


def test_notify_test_telegram_not_enabled(cfg, capsys):
    assert cli.cmd_notify(cfg, _args(action="test", channel="telegram")) == 0
    out = capsys.readouterr().out
    assert "telegram: not enabled" in out and "sent via: dashboard" in out
