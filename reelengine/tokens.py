"""Token lifecycle (API_NOTES "Tokens"). Only dates/status go in the DB; the token
itself lives in keyring. A refresh failure or error 190 -> auth_needed + pause +
notification with the reconnect command. No silent retry loops."""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable

from . import db, secrets
from .graph import GraphClient, GraphError
from .log import get_logger, redact

log = get_logger("tokens")
LONG_LIVED_DAYS = 60


def state(conn) -> dict:
    row = conn.execute("SELECT * FROM token_state WHERE id=1").fetchone()
    return dict(row) if row else {}


def ig_user_id(cfg, conn) -> str | None:
    return cfg["instagram"]["ig_user_id"] or state(conn).get("ig_user_id") or None


def _set(conn, **fields: Any) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE token_state SET {sets} WHERE id=1", tuple(fields.values()))  # nosec B608 - fixed column names


def days_left(conn) -> float | None:
    exp = db.parse_ts(state(conn).get("expires_at"))
    return None if exp is None else round((exp - db.utcnow()).total_seconds() / 86400, 1)


def mark_auth_needed(cfg, conn, reason: str, notify: Callable[..., Any]) -> None:
    already = state(conn).get("status") == "auth_needed"
    _set(conn, status="auth_needed", detail=redact(reason)[:300], last_check_at=db.now_iso())
    cfg.pause_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.pause_file.write_text(f"auth_needed: {redact(reason)[:200]}\nset {db.now_iso()} by the engine\n", encoding="utf-8")
    if not already:
        notify("action", "Instagram needs reconnecting", "Publishing is paused. Run:  reel connect   (then  reel resume)", None)


def client(cfg, conn, token: str | None = None) -> GraphClient:
    getter = (lambda: token) if token else (lambda: secrets.get_secret(secrets.ACCESS_TOKEN))
    return GraphClient(cfg, conn, getter)


def connect(cfg, conn, token: str, notify: Callable[..., Any] | None = None, gclient: GraphClient | None = None) -> dict:
    """Verify a long-lived token with /me, then store it in keyring and the metadata in the DB."""
    from .log import register_secret

    register_secret(token)
    c = gclient or client(cfg, conn, token)
    me = c.me()
    uid = str(me.get("user_id") or me.get("id") or "")
    if not uid:
        raise GraphError("auth", "the token did not return a user id")
    secrets.set_secret(secrets.ACCESS_TOKEN, token)
    now = db.utcnow()
    _set(conn, status="ok", issued_at=now.isoformat(timespec="seconds"),
         expires_at=(now + timedelta(days=LONG_LIVED_DAYS)).isoformat(timespec="seconds"),
         last_check_at=now.isoformat(timespec="seconds"), ig_user_id=uid, username=me.get("username"),
         account_type=me.get("account_type"), detail="expiry estimated (<= 60 days); exact after the first refresh")
    if cfg.pause_file.exists() and cfg.pause_file.read_text(encoding="utf-8").startswith("auth_needed"):
        cfg.pause_file.unlink()
    return {"ig_user_id": uid, "username": me.get("username"), "account_type": me.get("account_type")}


def refresh_if_due(cfg, conn, notify: Callable[..., Any], gclient: GraphClient | None = None, force: bool = False) -> dict:
    st = state(conn)
    if st.get("status") in (None, "missing") or not secrets.has_secret(secrets.ACCESS_TOKEN):
        return {"refreshed": False, "reason": "no token"}
    if st.get("status") == "auth_needed":
        return {"refreshed": False, "reason": "auth_needed: run reel connect"}
    now = db.utcnow()
    issued = db.parse_ts(st.get("issued_at")) or now
    last = db.parse_ts(st.get("last_refresh_at")) or issued
    exp = db.parse_ts(st.get("expires_at"))
    min_age = timedelta(days=cfg["instagram"]["refresh_min_age_days"])
    if exp and exp <= now:
        mark_auth_needed(cfg, conn, "token expired", notify)
        return {"refreshed": False, "reason": "expired"}
    if not force and (now - issued < min_age or now - last < min_age):
        return {"refreshed": False, "reason": "not due"}
    if now - issued < timedelta(hours=24):
        return {"refreshed": False, "reason": "token younger than 24 h"}
    c = gclient or client(cfg, conn)
    try:
        d = c.refresh_token()
    except GraphError as exc:
        if exc.kind in ("auth", "content"):
            mark_auth_needed(cfg, conn, f"token refresh failed: {exc}", notify)
        else:
            _set(conn, detail=f"refresh deferred: {redact(str(exc))[:200]}", last_check_at=db.now_iso())
        return {"refreshed": False, "reason": str(exc)}
    new = d.get("access_token")
    if not new:
        mark_auth_needed(cfg, conn, "refresh returned no token", notify)
        return {"refreshed": False, "reason": "no token in response"}
    secrets.set_secret(secrets.ACCESS_TOKEN, new)
    exp_new = now + timedelta(seconds=int(d.get("expires_in") or LONG_LIVED_DAYS * 86400))
    _set(conn, status="ok", issued_at=now.isoformat(timespec="seconds"), last_refresh_at=now.isoformat(timespec="seconds"),
         expires_at=exp_new.isoformat(timespec="seconds"), last_check_at=now.isoformat(timespec="seconds"), detail="refreshed")
    return {"refreshed": True, "expires_at": exp_new.isoformat(timespec="seconds")}


def health(cfg, conn, notify: Callable[..., Any]) -> dict:
    left = days_left(conn)
    st = state(conn).get("status")
    if st == "ok" and left is not None:
        if left <= 3:
            notify("action", "Instagram token expires in days", f"{left} days left. Run: reel refresh-token (or reel connect)", None)
        elif left <= 14:
            notify("warn", "Instagram token expiring soon", f"{left} days left; the weekly job refreshes it automatically.", None)
    return {"status": st, "days_left": left}
