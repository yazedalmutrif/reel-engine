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
