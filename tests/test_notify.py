"""reelengine.notify: toast XML escaping, the notifications row (redacted),
toast failures never raising, and no raw text in the PowerShell toast script."""
from __future__ import annotations

import base64
import re
import subprocess
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from reelengine import db, notify
from reelengine.config import load_config
from reelengine.log import MASK

ARABIC = "اختبار الإشعار ✓"
FAKE_IG_TOKEN = "IGQWR" + "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7" * 2  # token-shaped, not a real token


@pytest.fixture()
def cfg():
    return load_config().with_overrides({"notify": {"toast": False}})


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def _texts(xml: str) -> list[str]:
    root = ET.fromstring(xml)  # must be well-formed
    return [t.text or "" for t in root.iter("text")]


# --- build_toast_xml --------------------------------------------------------

def test_toast_xml_escapes_markup_chars():
    title = 'A <b>bold</b> & "quoted" \'single\''
    body = "x < y && z > 1 </text><text>injected"
    xml = notify.build_toast_xml(title, body)
    assert "<b>" not in xml and "</text><text>injected" not in xml
    assert "&lt;b&gt;" in xml and "&amp;" in xml and "&quot;" in xml and "&apos;" in xml
    assert _texts(xml) == [f"Reel Engine: {title}", body]  # round-trips exactly, only 2 <text> nodes


def test_toast_xml_keeps_arabic_verbatim():
    xml = notify.build_toast_xml("تنبيه", ARABIC)
    assert ARABIC in xml
    assert _texts(xml) == ["Reel Engine: تنبيه", ARABIC]


def test_toast_xml_empty_body():
    assert _texts(notify.build_toast_xml("t", "")) == ["Reel Engine: t", ""]


# --- notify(): DB row + redaction ---------------------------------------------

def test_notify_writes_redacted_row_without_toast(cfg, conn, monkeypatch):
    called = []
    monkeypatch.setattr(notify, "show_toast", lambda *a, **k: called.append(a) or True)
    res = notify.notify(
        cfg, conn, "error",
        title=f"token {FAKE_IG_TOKEN} failed",
        body=f"GET /me?access_token=secret123value&fields=id  Authorization: Bearer {FAKE_IG_TOKEN}",
        reel_id=7,
    )
    assert called == []  # toast disabled in cfg
    assert res == {"level": "error", "channels": ["dashboard"]}
    rows = conn.execute("SELECT level, title, body, reel_id, channels_json FROM notifications").fetchall()
    assert len(rows) == 1
    level, title, body, reel_id, channels = tuple(rows[0])
    assert (level, reel_id, channels) == ("error", 7, '["dashboard"]')
    for field in (title, body):
        assert FAKE_IG_TOKEN not in field
        assert MASK in field
    assert "secret123value" not in body


def test_notify_unknown_level_becomes_info(cfg, conn):
    assert notify.notify(cfg, conn, "loud", "hello")["level"] == "info"
    assert conn.execute("SELECT level FROM notifications").fetchone()[0] == "info"


# --- toast failures never raise ---------------------------------------------------

@pytest.mark.parametrize("fake_run", [
    pytest.param(lambda *a, **k: (_ for _ in ()).throw(OSError("powershell missing")), id="oserror"),
    pytest.param(lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("powershell.exe", 15)), id="timeout"),
    pytest.param(lambda *a, **k: SimpleNamespace(returncode=1, stderr=b"boom", stdout=b""), id="nonzero-exit"),
])
def test_toast_failure_returns_false_and_notify_still_records(cfg, conn, monkeypatch, fake_run):
    monkeypatch.setattr(notify.sys, "platform", "win32")
    monkeypatch.setattr(notify.subprocess, "run", fake_run)
    assert notify.show_toast("t", "b") is False
    res = notify.notify(cfg, conn, "warn", "t", "b", toast=True)
    assert res["channels"] == ["dashboard"]
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


def test_toast_success_adds_channel(cfg, conn, monkeypatch):
    monkeypatch.setattr(notify.sys, "platform", "win32")
    monkeypatch.setattr(notify.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr=b"", stdout=b""))
    assert notify.notify(cfg, conn, "action", "t", "b", toast=True)["channels"] == ["dashboard", "toast"]


def test_show_toast_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(notify.sys, "platform", "linux")
    monkeypatch.setattr(notify.subprocess, "run", lambda *a, **k: pytest.fail("must not run PowerShell"))
    assert notify.show_toast("t", "b") is False


# --- the PowerShell script carries no raw text ------------------------------------

def test_powershell_script_has_base64_only(monkeypatch):
    title = "'; Remove-Item C:\\x -Recurse; '$(calc)"
    body = f'{ARABIC} <b>&"`$env:USERNAME'
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs["input"]
        return SimpleNamespace(returncode=0, stderr=b"", stdout=b"")

    monkeypatch.setattr(notify.sys, "platform", "win32")
    monkeypatch.setattr(notify.subprocess, "run", fake_run)
    assert notify.show_toast(title, body) is True

    # the text is not on the command line; the script is read from stdin
    assert seen["argv"][-2:] == ["-Command", "-"]
    assert not any(title in a or ARABIC in a for a in seen["argv"])

    script = seen["input"].decode("utf-8")
    assert script.isascii()  # so no Arabic / raw text can be in it
    for raw in (title, body, ARABIC, "Remove-Item", "$(calc)", "$env:USERNAME", "&quot;"):
        assert raw not in script
    blobs = re.findall(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script)
    assert len(blobs) == 1
    assert base64.b64decode(blobs[0]).decode("utf-8") == notify.build_toast_xml(title, body)


# =====================================================================================
# v1.2: macOS desktop notifications, the `kind` keyword, Telegram (PDPL ruling)
# =====================================================================================
import requests  # noqa: E402

from reelengine import secrets  # noqa: E402

FAKE_BOT_TOKEN = "123456789:" + "AAbbCCddEEffGGhhIIjjKKllMMnnOOpp_-"  # token-shaped, not a real token
CHAT = "987654321"


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    """No test in this module can reach api.telegram.org: a real Session request to it raises."""
    real = requests.Session.request

    def guarded(self, method, url, *a, **k):
        if "api.telegram.org" in str(url):
            raise AssertionError("a test tried to reach the real Telegram API")
        return real(self, method, url, *a, **k)

    monkeypatch.setattr(requests.Session, "request", guarded)
    monkeypatch.setattr(notify, "_http", None)
    monkeypatch.setattr(notify, "_off_logged", False)


class FakeResp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = {"ok": True, "result": {}} if data is None else data

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, resp=None, exc=None):
        self.calls: list[tuple[str, str, dict]] = []
        self.resp = resp or FakeResp()
        self.exc = exc

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if self.exc:
            raise self.exc
        return self.resp

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        if self.exc:
            raise self.exc
        return self.resp


@pytest.fixture()
def tg_cfg():
    return load_config().with_overrides({"notify": {"toast": False, "telegram": True, "telegram_chat_ids": [CHAT]}})


@pytest.fixture()
def with_token(memory_keyring):
    secrets.set_secret(secrets.TELEGRAM_TOKEN, FAKE_BOT_TOKEN)
    return FAKE_BOT_TOKEN


def test_guard_blocks_real_telegram_host():
    with pytest.raises(AssertionError, match="real Telegram API"):
        requests.Session().get("https://api.telegram.org/botX/getMe", timeout=1)


# --- macOS ---------------------------------------------------------------------------

def test_mac_notification_uses_stdin_script_and_argv(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return SimpleNamespace(returncode=0, stderr=b"", stdout=b"")

    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.subprocess, "run", fake_run)
    title = 'x" & do shell script "rm -rf ~" & "'
    assert notify.show_mac_notification(title, ARABIC) is True
    assert seen["argv"] == [notify.OSASCRIPT, "-", f"Reel Engine: {title}", ARABIC]
    assert seen["kw"]["input"].decode() == notify.MAC_SCRIPT
    assert "shell" not in seen["kw"] and seen["kw"]["timeout"] > 0
    assert title not in notify.MAC_SCRIPT and "on run argv" in notify.MAC_SCRIPT


@pytest.mark.parametrize("fake_run", [
    pytest.param(lambda *a, **k: (_ for _ in ()).throw(OSError("no osascript")), id="oserror"),
    pytest.param(lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("osascript", 10)), id="timeout"),
    pytest.param(lambda *a, **k: SimpleNamespace(returncode=1, stderr=b"boom", stdout=b""), id="nonzero"),
])
def test_mac_notification_failure_is_ignored(cfg, conn, monkeypatch, fake_run):
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.subprocess, "run", fake_run)
    assert notify.show_mac_notification("t", "b") is False
    assert notify.notify(cfg, conn, "warn", "t", "b", toast=True)["channels"] == ["dashboard"]


def test_mac_success_adds_desktop_channel_and_truncates(cfg, conn, monkeypatch):
    seen = {}
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.subprocess, "run", lambda argv, **k: seen.update(argv=argv) or SimpleNamespace(returncode=0, stderr=b"", stdout=b""))
    res = notify.notify(cfg, conn, "action", "T" * 300, "B" * 900, toast=True)
    assert res["channels"] == ["dashboard", "desktop"]
    assert len(seen["argv"][2]) <= len("Reel Engine: ") + notify.DESKTOP_TITLE_MAX
    assert len(seen["argv"][3]) <= notify.DESKTOP_BODY_MAX
    # the DB row keeps the full text
    assert len(conn.execute("SELECT body FROM notifications").fetchone()[0]) == 900


def test_linux_has_no_desktop_channel(cfg, conn, monkeypatch):
    monkeypatch.setattr(notify.sys, "platform", "linux")
    monkeypatch.setattr(notify.subprocess, "run", lambda *a, **k: pytest.fail("no subprocess on linux"))
    assert notify.desktop_channel() is None
    assert notify.notify(cfg, conn, "error", "t", "b", toast=True)["channels"] == ["dashboard"]


# --- kind keyword / back-compat ------------------------------------------------------

def test_notifier_accepts_kind_and_old_positional_calls(cfg, conn):
    n = notify.notifier(cfg, conn, toast=False)
    assert n("info", "a", "b", None)["channels"] == ["dashboard"]  # old style
    assert n("info", "a", kind="occasion")["level"] == "info"
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 2


@pytest.mark.parametrize("level,kind,expected", [
    ("action", "general", "action"), ("warn", "general", "action"), ("error", "general", "error"),
    ("info", "general", "general"), ("info", "occasion", "occasion"), ("error", "brief", "brief"),
])
def test_effective_kind(level, kind, expected):
    assert notify.effective_kind(level, kind) == expected


def test_config_schema_restricts_kinds_and_chat_ids():
    from reelengine.config import ConfigError

    base = load_config()
    assert base["notify"]["telegram_kinds"] == ["occasion", "action", "error"]
    assert base["notify"]["telegram_host"] == "https://api.telegram.org"
    for bad in ({"telegram_kinds": ["general"]}, {"telegram_kinds": ["stats"]}, {"telegram_chat_ids": ["@me"]},
                {"telegram_chat_ids": [str(i) for i in range(6)]}, {"telegram_chat_ids": [123]},
                {"telegram_host": "http://evil.example"}, {"telegram_host": "https://x.org/path"}):
        with pytest.raises(ConfigError):
            base.with_overrides({"notify": bad})
    assert base.with_overrides({"notify": {"telegram_host": "http://127.0.0.1:8099", "telegram_chat_ids": ["-1001234567890"]}})


# --- Telegram: gating ------------------------------------------------------------------

def test_telegram_off_by_default_sends_nothing(cfg, conn, with_token):
    s = FakeSession()
    res = notify.notify(cfg, conn, "error", "x", "y", session=s)
    assert s.calls == [] and res["channels"] == ["dashboard"]


def test_telegram_needs_chat_ids_and_token(conn, memory_keyring):
    s = FakeSession()
    no_ids = load_config().with_overrides({"notify": {"toast": False, "telegram": True}})
    assert notify.notify(no_ids, conn, "error", "x", session=s)["channels"] == ["dashboard"]
    no_tok = no_ids.with_overrides({"notify": {"telegram_chat_ids": [CHAT]}})
    assert notify.telegram_state(no_tok)[0] == "no_token"
    assert notify.notify(no_tok, conn, "error", "x", session=s)["channels"] == ["dashboard"]
    assert s.calls == []


def test_telegram_sends_allowed_kinds_only(tg_cfg, conn, with_token):
    s = FakeSession()
    assert notify.notify(tg_cfg, conn, "info", "Reel published", session=s)["channels"] == ["dashboard"]  # general: never
    assert notify.notify(tg_cfg, conn, "info", "Brief ready", kind="brief", session=s)["channels"] == ["dashboard"]  # not in default kinds
    assert s.calls == []
    assert notify.notify(tg_cfg, conn, "error", "Tick failed", "Traceback: boom", session=s)["channels"] == ["dashboard", "telegram"]
    assert notify.notify(tg_cfg, conn, "info", "National Day in 5 days", "Film by Friday", kind="occasion", session=s)["channels"] == ["dashboard", "telegram"]
    assert len(s.calls) == 2


def test_telegram_request_shape(tg_cfg, conn, with_token):
    s = FakeSession()
    notify.notify(tg_cfg, conn, "action", "Instagram needs reconnecting", "raw body text", session=s)
    (method, url, kw), = s.calls
    assert method == "POST" and url == f"https://api.telegram.org/bot{FAKE_BOT_TOKEN}/sendMessage"
    assert kw["timeout"] == 10 and kw["allow_redirects"] is False
    assert kw["json"] == {"chat_id": CHAT, "text": "Instagram needs reconnecting\n\nDetails on the dashboard.",
                          "protect_content": True, "link_preview_options": {"is_disabled": True}}
    assert "parse_mode" not in kw["json"] and "disable_web_page_preview" not in kw["json"]


def test_telegram_only_to_allow_listed_chats(conn, with_token):
    c = load_config().with_overrides({"notify": {"toast": False, "telegram": True, "telegram_chat_ids": ["111", "-222"]}})
    s = FakeSession()
    notify.notify(c, conn, "error", "x", session=s)
    assert [kw["json"]["chat_id"] for _, _, kw in s.calls] == ["111", "-222"]


# --- Telegram: content by kind -----------------------------------------------------------

def test_non_occasion_kinds_send_title_only(tg_cfg, conn, with_token):
    s = FakeSession()
    notify.notify(tg_cfg, conn, "error", "Tick failed", "OSError: /secret/file hook text caption", session=s)
    text = s.calls[0][2]["json"]["text"]
    assert text == "Tick failed\n\nDetails on the dashboard."
    assert "OSError" not in text and "caption" not in text


def test_occasion_sends_title_and_body(tg_cfg, conn, with_token):
    s = FakeSession()
    notify.notify(tg_cfg, conn, "info", "اليوم الوطني بعد ٥ أيام", "صوّر قبل الخميس", kind="occasion", session=s)
    assert s.calls[0][2]["json"]["text"] == "اليوم الوطني بعد ٥ أيام\n\nصوّر قبل الخميس"


def test_telegram_text_truncated_to_4000(tg_cfg, conn, with_token):
    s = FakeSession()
    notify.notify(tg_cfg, conn, "info", "t", "ب" * 5000, kind="occasion", session=s)
    assert len(s.calls[0][2]["json"]["text"]) == 4000


# --- Telegram: refusals (never rewritten) -----------------------------------------------

@pytest.mark.parametrize("title,body,kind", [
    ("Reel by @some.creator is trending", "", "action"),
    ("National Day", "see @other_account for ideas", "occasion"),
    ("Occasion", "https://www.instagram.com/reel/ABC123/", "occasion"),
    ("Occasion", "details at example.com/x", "occasion"),
    ("Occasion", "www.example.org", "occasion"),
    ("Error in ~/Projects/reel-engine/data", "", "error"),
    (f"Token {FAKE_IG_TOKEN} failed", "", "error"),
    ("Occasion", "access_token=abcdef123456", "occasion"),
])
def test_telegram_refuses_unsafe_text(tg_cfg, conn, with_token, title, body, kind):
    s = FakeSession()
    res = notify.notify(tg_cfg, conn, "error", title, body, kind=kind, session=s)
    assert s.calls == [] and res["channels"] == ["dashboard"]


def test_telegram_refuses_home_path(tg_cfg, conn, with_token):
    from pathlib import Path as _P

    s = FakeSession()
    notify.notify(tg_cfg, conn, "error", f"Missing {_P.home()}/Movies/x.mov", session=s)
    assert s.calls == []


def test_telegram_refuses_registered_secret(tg_cfg, conn, with_token):
    from reelengine.log import register_secret

    register_secret("plainsecretvalue42")
    s = FakeSession()
    notify.notify(tg_cfg, conn, "info", "x", "contains plainsecretvalue42", kind="occasion", session=s)
    assert s.calls == []


def test_refusal_never_rewrites_handle():
    assert notify.telegram_refusal("hi @someone") == "contains an @handle"
    assert notify.telegram_refusal("plain text ✓ ٥") is None


# --- Telegram: failures never leak the token / URL / chat id -------------------------------

@pytest.mark.parametrize("session", [
    pytest.param(FakeSession(exc=requests.ConnectionError(
        f"HTTPSConnectionPool(host='api.telegram.org'): Max retries exceeded with url: /bot{FAKE_BOT_TOKEN}/sendMessage")), id="conn-error"),
    pytest.param(FakeSession(exc=requests.Timeout(f"https://api.telegram.org/bot{FAKE_BOT_TOKEN}/sendMessage timed out")), id="timeout"),
    pytest.param(FakeSession(resp=FakeResp(400, {"ok": False, "description": f"Bad Request: chat {CHAT} not found https://api.telegram.org/bot{FAKE_BOT_TOKEN}"})), id="http-400"),
    pytest.param(FakeSession(resp=FakeResp(502, None)), id="http-502"),
])
def test_telegram_failure_logs_no_url_token_or_chat_id(tg_cfg, conn, with_token, caplog, session):
    import logging

    caplog.set_level(logging.DEBUG, logger="reelengine")
    lg = logging.getLogger("reelengine")
    lg.addHandler(caplog.handler)
    try:
        res = notify.notify(tg_cfg, conn, "error", "Tick failed", session=session)
    finally:
        lg.removeHandler(caplog.handler)
    assert res["channels"] == ["dashboard"]
    logged = caplog.text
    assert "telegram" in logged
    for leak in (FAKE_BOT_TOKEN, "api.telegram.org", "/bot", CHAT):
        assert leak not in logged
    row = " ".join(str(v) for v in tuple(conn.execute("SELECT title, body, channels_json FROM notifications").fetchone()))
    assert CHAT not in row and FAKE_BOT_TOKEN not in row


def test_telegram_channel_row_has_no_chat_id(tg_cfg, conn, with_token):
    notify.notify(tg_cfg, conn, "error", "x", session=FakeSession())
    assert conn.execute("SELECT channels_json FROM notifications").fetchone()[0] == '["dashboard", "telegram"]'


def test_telegram_force_flag_for_test_command(tg_cfg, conn, with_token):
    s = FakeSession()
    assert notify.notify(tg_cfg, conn, "info", "Test notification", telegram=True, session=s)["channels"] == ["dashboard", "telegram"]
    off = tg_cfg.with_overrides({"notify": {"telegram": False}})
    assert notify.notify(off, conn, "info", "Test", telegram=True, session=s)["channels"] == ["dashboard"]
    assert notify.notify(tg_cfg, conn, "error", "x", telegram=False, session=s)["channels"] == ["dashboard"]
    assert len(s.calls) == 1


def test_default_session_is_used_when_none_injected(tg_cfg, conn, with_token, monkeypatch):
    s = FakeSession()
    monkeypatch.setattr(notify, "_http", s)
    notify.notify(tg_cfg, conn, "error", "x")
    assert len(s.calls) == 1
