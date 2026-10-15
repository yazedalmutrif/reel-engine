"""Notifications (spec §10): a DB row (shown on the dashboard), a desktop notification
on this OS, and optionally Telegram (+ vault note for briefs/reviews, written by their
modules).

Desktop (`[notify] toast = true`; the key name is kept for old config files, it now means
"desktop notification on this OS"):
- Windows: a toast. Windows PowerShell runs with the script on STDIN and the text
  base64-encoded inside it, so title/body never reach a command line.
- macOS: `osascript -` with a fixed AppleScript on STDIN; title and body are passed as
  argv items (never spliced into the script), so they can't inject AppleScript.
- Linux/other: no desktop channel (the dashboard row is still written).
A desktop failure is logged and ignored; it never breaks a tick.

Telegram (PDPL ruling, art. 29): `[notify] telegram = true` + a `telegram_chat_ids`
allow-list + a bot token in the OS credential store. Only notification KINDS listed in
`telegram_kinds` are sent (fixed set: occasion, action, error, published, brief). A
caller's kind "general" is derived from the level (action/warn -> action, error -> error,
otherwise general = never sent). Content: "occasion" sends title + body; every other kind
sends the title only plus "Details on the dashboard.". A message is REFUSED (not rewritten)
if its final text has an @handle, any URL, a path inside the home folder, or anything the
log redactor would change. The token is part of the request URL, so the URL is never
logged or stored; request exceptions (which embed the URL) are reduced to their class name.
Chat ids never appear in logs or notification rows. Incoming messages are ignored.
"""
from __future__ import annotations

import base64
import re
import subprocess  # nosec B404 - fixed binaries, script via stdin, list args
import sys
from pathlib import Path
from xml.sax.saxutils import escape  # nosec B406 - used to ESCAPE our own output, never to parse XML

import requests

from . import db, secrets
from .log import MASK, get_logger, redact

log = get_logger("notify")
LEVELS = ("info", "warn", "error", "action")
TELEGRAM_KINDS = ("occasion", "action", "error", "published", "brief")  # mirrored in config.SCHEMA
AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

DESKTOP_TITLE_MAX = 80
DESKTOP_BODY_MAX = 220
TELEGRAM_TEXT_MAX = 4000
TELEGRAM_TIMEOUT_S = 10
TELEGRAM_DETAILS_LINE = "Details on the dashboard."
OSASCRIPT = "/usr/bin/osascript"
# Fixed script; the text arrives as argv items, so nothing user-controlled is ever parsed as AppleScript.
MAC_SCRIPT = "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run\n"

CHAT_ID_RE = re.compile(r"^-?\d{1,20}$")
_URL_RE = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)\S+|\b[a-z0-9-]+\.(?:com|net|org|sa|io|me|ly|gg|co|app|dev)\b(?:/\S*)?")
_HANDLE_RE = re.compile(r"(?<![\w.])@[A-Za-z0-9._]{1,30}")


def _trunc(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ------------------------------------------------------------------ Windows toast
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


# ------------------------------------------------------------------ macOS notification
def show_mac_notification(title: str, body: str, timeout: float = 10) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        p = subprocess.run(  # nosec B603 - fixed absolute binary; script on stdin, text as argv items, no shell
            [OSASCRIPT, "-", f"Reel Engine: {title}", body or " "],
            input=MAC_SCRIPT.encode("utf-8"), capture_output=True, timeout=timeout,
        )
        if p.returncode != 0:
            log.warning("macOS notification failed: %s", redact(p.stderr.decode("utf-8", "replace")[-300:]))
        return p.returncode == 0
    except Exception as exc:  # noqa: BLE001
        log.warning("macOS notification failed: %s", exc)
        return False


def desktop_channel() -> str | None:
    """Channel name for this OS ('toast' on Windows, 'desktop' on macOS), None if there is none."""
    return {"win32": "toast", "darwin": "desktop"}.get(sys.platform)


def show_desktop(title: str, body: str) -> bool:
    title, body = _trunc(title, DESKTOP_TITLE_MAX), _trunc(body, DESKTOP_BODY_MAX)
    if sys.platform == "win32":
        return show_toast(title, body)
    if sys.platform == "darwin":
        return show_mac_notification(title, body)
    return False


# ------------------------------------------------------------------ Telegram
_http: requests.Session | None = None
_off_logged = False


def _session() -> requests.Session:
    global _http
    if _http is None:
        _http = requests.Session()
    return _http


def effective_kind(level: str, kind: str) -> str:
    """'general' is derived from the level; explicit kinds pass through."""
    if kind and kind != "general":
        return kind
    if level in ("action", "warn"):
        return "action"
    if level == "error":
        return "error"
    return "general"


def telegram_text(kind: str, title: str, body: str) -> str:
    """The exact text for Telegram: occasion = title + body; anything else = title + fixed line."""
    if kind == "occasion":
        text = f"{title}\n\n{body}" if body else title
    else:
        text = f"{title}\n\n{TELEGRAM_DETAILS_LINE}"
    return _trunc(text, TELEGRAM_TEXT_MAX)


def _home_variants() -> list[str]:
    h = str(Path.home())
    return [v for v in {h, h.replace("\\", "/"), h.replace("/", "\\")} if len(v) > 1]


def telegram_refusal(raw_text: str) -> str | None:
    """Why this text must NOT go to Telegram (None = OK to send). Never rewrites."""
    if redact(raw_text) != raw_text or MASK in raw_text:
        return "contains something the redactor masks"
    if _HANDLE_RE.search(raw_text):
        return "contains an @handle"
    if _URL_RE.search(raw_text):
        return "contains a URL"
    if any(h in raw_text for h in _home_variants()) or re.search(r"(?:^|[\s(\"'])~[/\\]", raw_text):
        return "contains a path inside the home folder"
    return None


def chat_ids(cfg) -> list[str]:
    return [c for c in cfg["notify"].get("telegram_chat_ids", []) if isinstance(c, str) and CHAT_ID_RE.match(c)][:5]


def telegram_state(cfg) -> tuple[str, str]:
    """('off'|'ready'|'no_chat_ids'|'no_token', human detail). Reveals nothing secret (no ids, no token)."""
    if not cfg["notify"]["telegram"]:
        return "off", "not configured (optional; [notify] telegram = false)"
    ids = chat_ids(cfg)
    if not ids:
        return "no_chat_ids", "on, but telegram_chat_ids is empty (run `reel notify telegram-chat-id`)"
    if not secrets.has_secret(secrets.TELEGRAM_TOKEN):
        return "no_token", f"on with {len(ids)} chat id(s), but the bot token is missing (run `reel notify setup-telegram`)"
    return "ready", f"configured with {len(ids)} chat id(s); kinds {cfg['notify']['telegram_kinds']}"


def _clean_error(text: str) -> str:
    """An error description, redacted, with URLs and long digit runs (chat ids) removed."""
    s = _URL_RE.sub("[url]", redact(str(text)))
    return re.sub(r"-?\d{6,}", "[id]", s)[:200]


def telegram_api(cfg, method: str, *, token: str, http_method: str = "GET", payload: dict | None = None,
                 session: requests.Session | None = None) -> dict:
    """One Bot API call. Raises RuntimeError with a URL-free message on any failure."""
    s = session or _session()
    url = f"{cfg['notify']['telegram_host'].rstrip('/')}/bot{token}/{method}"
    try:
        if http_method == "POST":
            r = s.post(url, json=payload or {}, timeout=TELEGRAM_TIMEOUT_S, allow_redirects=False)
        else:
            r = s.get(url, params=payload or None, timeout=TELEGRAM_TIMEOUT_S, allow_redirects=False)
    except Exception as exc:  # noqa: BLE001 - requests exceptions embed the URL (= the token): class name only
        raise RuntimeError(f"telegram {method}: {type(exc).__name__}") from None
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        data = {}
    if r.status_code != 200 or not isinstance(data, dict) or not data.get("ok"):
        desc = data.get("description", "") if isinstance(data, dict) else ""
        raise RuntimeError(f"telegram {method}: HTTP {r.status_code} {_clean_error(desc)}".rstrip())
    return data


def send_telegram(cfg, kind: str, title: str, body: str, *, session: requests.Session | None = None) -> int:
    """Send to every allow-listed chat id. Returns how many sends succeeded (0 when off or refused).
    `title`/`body` must be the RAW (unredacted) text, so a redaction hit can be detected and refused."""
    global _off_logged
    state, detail = telegram_state(cfg)
    if state != "ready":
        if not _off_logged:
            log.debug("telegram not sent: %s", detail)
            _off_logged = True
        return 0
    text = telegram_text(kind, title, body)
    why = telegram_refusal(text)
    if why:
        log.warning("telegram message refused (%s); kind=%s, it stays on the dashboard", why, kind)
        return 0
    token = secrets.get_secret(secrets.TELEGRAM_TOKEN, required=False)  # registers it with the redactor
    if not token:
        return 0
    ok = 0
    for cid in chat_ids(cfg):
        try:
            telegram_api(cfg, "sendMessage", token=token, http_method="POST", session=session,
                         payload={"chat_id": cid, "text": text, "protect_content": True,
                                  "link_preview_options": {"is_disabled": True}})
            ok += 1
        except Exception as exc:  # noqa: BLE001 - never break a tick; the chat id is never logged
            log.warning("telegram send to one chat failed: %s", _clean_error(str(exc)))
    token = ""  # noqa: F841  # nosec B105 - drops the reference, not a password
    return ok


# ------------------------------------------------------------------ entry point
def notify(cfg, conn, level: str, title: str, body: str = "", reel_id: int | None = None, toast: bool | None = None,
           *, kind: str = "general", telegram: bool | None = None, session: requests.Session | None = None) -> dict:
    """Record a notification and fan it out.

    toast: None = `[notify] toast`; True/False forces the desktop channel on/off.
    telegram: None = config (master switch + kind filter); True = send if Telegram is
    configured, whatever the kind (`reel notify test`); False = never.
    """
    level = level if level in LEVELS else "info"
    raw_title, raw_body = title or "", body or ""
    body = redact(raw_body)
    title = redact(raw_title)
    channels = ["dashboard"]
    want_toast = cfg["notify"]["toast"] if toast is None else toast
    if want_toast and level in LEVELS and show_desktop(title, body):
        channels.append(desktop_channel() or "desktop")
    n = cfg["notify"]
    k = effective_kind(level, kind)
    if telegram is None:
        want_tg = bool(n["telegram"]) and k in n.get("telegram_kinds", []) and k in TELEGRAM_KINDS
    else:
        want_tg = telegram and bool(n["telegram"])
    if want_tg:
        try:
            if send_telegram(cfg, k, raw_title, raw_body, session=session) > 0:
                channels.append("telegram")
        except Exception as exc:  # noqa: BLE001 - never break a tick
            log.warning("telegram failed: %s", _clean_error(str(exc)))
    conn.execute(
        "INSERT INTO notifications(ts, level, title, body, reel_id, channels_json) VALUES(?,?,?,?,?,?)",
        (db.now_iso(), level, title, body, reel_id, db.jdump(channels)),
    )
    return {"level": level, "channels": channels}


def notifier(cfg, conn, toast: bool | None = None):
    """A notify(level, title, body, reel_id, kind) callable bound to cfg/conn."""
    return lambda level, title, body="", reel_id=None, kind="general": notify(cfg, conn, level, title, body, reel_id, toast, kind=kind)
