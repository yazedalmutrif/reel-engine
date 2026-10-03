"""Notifications (spec §10). v1: a DB row (shown on the dashboard) + a Windows toast
(+ vault note for briefs/reviews, written by their modules). v2 (Telegram) is a stub
that stays off until Yazeed creates a bot and a chat_id allow-list.

The toast runs Windows PowerShell with the script on STDIN and the text base64-encoded
inside it, so title/body never reach a command line and can't inject anything. A toast
failure is logged and ignored; it never breaks a tick.
"""
from __future__ import annotations

import base64
import subprocess  # nosec B404 - fixed binary, script via stdin
import sys
from xml.sax.saxutils import escape  # nosec B406 - used to ESCAPE our own output, never to parse XML

from . import db
from .log import get_logger, redact

log = get_logger("notify")
LEVELS = ("info", "warn", "error", "action")
AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def build_toast_xml(title: str, body: str) -> str:
    t = escape(f"Reel Engine: {title}", {'"': "&quot;", "'": "&apos;"})
    b = escape(body or "", {'"': "&quot;", "'": "&apos;"})
    return f'<toast><visual><binding template="ToastGeneric"><text>{t}</text><text>{b}</text></binding></visual></toast>'


def _ps_script(xml: str) -> str:
    b64 = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    return (
        "$ErrorActionPreference='Stop'\n"
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null\n"
        f"$x = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}'))\n"
        "$d = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        "$d.LoadXml($x)\n"
        "$t = New-Object Windows.UI.Notifications.ToastNotification $d\n"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{AUMID}').Show($t)\n"
    )


def show_toast(title: str, body: str, timeout: float = 15) -> bool:
    if sys.platform != "win32":
        return False
    try:
        p = subprocess.run(  # nosec B603 B607 - Windows PowerShell, script on stdin
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "-"],
            input=_ps_script(build_toast_xml(title, body)).encode("utf-8"),
            capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW,
        )
        if p.returncode != 0:
            log.warning("toast failed: %s", redact(p.stderr.decode("utf-8", "replace")[-300:]))
        return p.returncode == 0
    except Exception as exc:  # noqa: BLE001
        log.warning("toast failed: %s", exc)
        return False


def notify(cfg, conn, level: str, title: str, body: str = "", reel_id: int | None = None, toast: bool | None = None) -> dict:
    level = level if level in LEVELS else "info"
    body = redact(body or "")
    title = redact(title)
    channels = ["dashboard"]
    want_toast = cfg["notify"]["toast"] if toast is None else toast
    if want_toast and level in ("warn", "error", "action", "info") and show_toast(title, body):
        channels.append("toast")
    if cfg["notify"]["telegram"]:
        log.warning("telegram notifications are a v2 feature and are not implemented")
    conn.execute(
        "INSERT INTO notifications(ts, level, title, body, reel_id, channels_json) VALUES(?,?,?,?,?,?)",
        (db.now_iso(), level, title, body, reel_id, db.jdump(channels)),
    )
    return {"level": level, "channels": channels}


def notifier(cfg, conn, toast: bool | None = None):
    """A notify(level, title, body, reel_id) callable bound to cfg/conn."""
    return lambda level, title, body="", reel_id=None: notify(cfg, conn, level, title, body, reel_id, toast)
