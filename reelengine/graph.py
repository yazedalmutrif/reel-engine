"""Minimal Instagram Graph API client (Instagram API with Instagram Login), per
docs/API_NOTES.md. Hosts, API version and the auth style are configurable because
some details are marked VERIFY and are confirmed by `reel doctor --live`.

- The token is read from keyring per call (never stored on the object or logged).
- auth_mode "header" sends `Authorization: Bearer <token>`; "query" sends access_token=.
  The rupload host always uses `Authorization: OAuth <token>` (API_NOTES step 2).
- Every error is classified: retryable | rate_limit | auth | quota | content | expired.
- X-App-Usage is recorded in api_usage; callers back off above app_usage_backoff_pct.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import requests

from . import db
from .log import get_logger, redact, redact_obj

log = get_logger("graph")

AUTH_CODES = {190, 102, 10, 200, 463, 467}
RATE_CODES = {4, 17, 32, 613, 80001, 80002, 80006}
RETRY_CODES = {1, 2}
RETRY_SUBCODES = {2207001, 2207003, 2207027, 2207032, 2207053, 2207057}
QUOTA_SUBCODES = {2207042}
EXPIRED_SUBCODES = {2207020}


class GraphError(Exception):
    def __init__(self, kind: str, message: str, *, code: int | None = None, subcode: int | None = None, http: int | None = None):
        super().__init__(f"[{kind}] {message}")
        self.kind = kind
        self.code = code
        self.subcode = subcode
        self.http = http


def classify(http: int | None, err: dict | None) -> str:
    err = err or {}
    code, sub = err.get("code"), err.get("error_subcode")
    if code in AUTH_CODES:
        return "auth"
    if sub in QUOTA_SUBCODES:
        return "quota"
    if sub in EXPIRED_SUBCODES:
        return "expired"
    if code in RATE_CODES or http == 429:
        return "rate_limit"
    if code in RETRY_CODES or sub in RETRY_SUBCODES or err.get("is_transient") or (http is not None and http >= 500):
        return "retryable"
    # Graph uses type OAuthException for most errors (transient ones too), so the type
    # alone means "auth" only after the known codes/subcodes above didn't match.
    if err.get("type") == "OAuthException" and code != 100:
        return "auth"
    return "content"


class GraphClient:
    def __init__(self, cfg, conn, token_getter: Callable[[], str], session: requests.Session | None = None):
        self.cfg = cfg
        self.conn = conn
        self.ig = cfg["instagram"]
        self._token = token_getter
        self.s = session or requests.Session()
        self.last_app_usage: float | None = None

    # ---------------------------------------------------------------- urls
    def url(self, path: str, host: str | None = None, versioned: bool = True) -> str:
        base = (host or self.ig["graph_host"]).rstrip("/")
        v = f"/{self.ig['api_version']}" if versioned else ""
        return f"{base}{v}/{path.lstrip('/')}"

    # ---------------------------------------------------------------- core
    def request(self, method: str, url: str, *, params: dict | None = None, data: dict | None = None,
                headers: dict | None = None, body: bytes | None = None, auth: str = "default", timeout: float | None = None) -> dict:
        params = dict(params or {})
        headers = dict(headers or {})
        token = self._token()
        if auth == "oauth":
            headers["Authorization"] = f"OAuth {token}"
        elif auth == "query" or (auth == "default" and self.ig["auth_mode"] == "query"):
            params["access_token"] = token
        elif auth == "default":
            headers["Authorization"] = f"Bearer {token}"
        try:
            r = self.s.request(method, url, params=params, data=(body if body is not None else data), headers=headers,
                               timeout=timeout or self.ig["request_timeout_s"])
        except (requests.ConnectionError, requests.Timeout) as exc:
            self._record(url, None, None, f"network: {type(exc).__name__}")
            raise GraphError("retryable", f"network error on {self._safe(url)}: {type(exc).__name__}") from None
        self._app_usage(r)
        try:
            payload = r.json() if r.content else {}
        except ValueError:
            payload = {"raw": r.text[:300]}
        if r.status_code >= 400 or (isinstance(payload, dict) and "error" in payload):
            err = payload.get("error", {}) if isinstance(payload, dict) else {}
            kind = classify(r.status_code, err)
            msg = redact(err.get("message") or r.text[:300])
            self._record(url, r.status_code, None, f"{kind} code={err.get('code')} sub={err.get('error_subcode')}")
            raise GraphError(kind, f"{msg} (http {r.status_code}, code {err.get('code')}, subcode {err.get('error_subcode')})",
                             code=err.get("code"), subcode=err.get("error_subcode"), http=r.status_code)
        self._record(url, r.status_code, None, None)
        return payload

    def get(self, path: str, **kw: Any) -> dict:
        return self.request("GET", self.url(path, kw.pop("host", None)), **kw)

    def post(self, path: str, **kw: Any) -> dict:
        return self.request("POST", self.url(path, kw.pop("host", None)), **kw)

    # ---------------------------------------------------------------- helpers
    def _safe(self, url: str) -> str:
        return redact(url.split("?")[0])

    def _app_usage(self, r: requests.Response) -> None:
        raw = r.headers.get("X-App-Usage") or r.headers.get("x-app-usage")
        if not raw:
            return
        try:
            d = json.loads(raw)
            self.last_app_usage = float(max(d.get("call_count", 0), d.get("total_cputime", 0), d.get("total_time", 0)))
        except (ValueError, TypeError):
            pass

    def _record(self, url: str, status: int | None, quota: tuple | None, detail: str | None) -> None:
        try:
            self.conn.execute(
                "INSERT INTO api_usage(ts, endpoint, http_status, app_usage_pct, quota_usage, quota_total, detail) VALUES(?,?,?,?,?,?,?)",
                (db.now_iso(), self._safe(url)[-120:], status, self.last_app_usage, quota[0] if quota else None, quota[1] if quota else None, detail),
            )
        except Exception:  # noqa: BLE001 - usage logging must never break a call
            pass  # nosec B110 - usage logging is best-effort

    def over_budget(self) -> bool:
        return self.last_app_usage is not None and self.last_app_usage >= self.ig["app_usage_backoff_pct"]

    # ---------------------------------------------------------------- endpoints (API_NOTES)
    def me(self) -> dict:
        return self.get("me", params={"fields": "user_id,username,account_type,media_count"})

    def publishing_limit(self, ig_user_id: str) -> tuple[int, int]:
        d = self.get(f"{ig_user_id}/content_publishing_limit", params={"fields": "quota_usage,config"})
        row = (d.get("data") or [{}])[0]
        usage = int(row.get("quota_usage") or 0)
        total = int((row.get("config") or {}).get("quota_total") or 0)
        self._record(f"{ig_user_id}/content_publishing_limit", 200, (usage, total), "quota")
        return usage, total

    def create_reel_container(self, ig_user_id: str, caption: str, thumb_offset_ms: int) -> dict:
        data = {
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "share_to_feed": "true" if self.ig["share_to_feed"] else "false",
            "thumb_offset": str(int(thumb_offset_ms)),
        }
        if self.ig["send_is_ai_generated"]:
            data["is_ai_generated"] = "false"  # real footage of Yazeed (param added 2026-06-22, VERIFY)
        return self.post(f"{ig_user_id}/media", host=self.ig["container_host"], data=data)

    def upload(self, container_id: str, video: Path, upload_uri: str | None = None) -> dict:
        size = video.stat().st_size
        default = f"{self.ig['upload_host'].rstrip('/')}/ig-api-upload/{self.ig['api_version']}/{container_id}"
        url = upload_uri if upload_uri and same_origin(upload_uri, self.ig["upload_host"]) else default
        if upload_uri and url != upload_uri:
            log.warning("ignoring upload uri on an unexpected host; using the configured upload host")
        with open(video, "rb") as f:
            body = f.read()
        return self.request("POST", url, headers={"offset": "0", "file_size": str(size)}, body=body, auth="oauth",
                            timeout=max(120, self.ig["request_timeout_s"]))

    def container_status(self, container_id: str) -> dict:
        return self.get(container_id, params={"fields": "status_code,status"})

    def media_publish(self, ig_user_id: str, container_id: str) -> dict:
        return self.post(f"{ig_user_id}/media_publish", data={"creation_id": container_id})

    def media(self, media_id: str) -> dict:
        return self.get(media_id, params={"fields": "id,permalink,timestamp,media_type,media_product_type"})

    def recent_media(self, ig_user_id: str, limit: int = 25) -> list[dict]:
        d = self.get(f"{ig_user_id}/media", params={"fields": "id,caption,timestamp,permalink,media_product_type", "limit": str(limit)})
        return d.get("data") or []

    def media_insights(self, media_id: str, metrics: list[str]) -> dict:
        return self.get(f"{media_id}/insights", params={"metric": ",".join(metrics)})

    def account_insights(self, ig_user_id: str, metrics: list[str], since: int, until: int) -> dict:
        return self.get(f"{ig_user_id}/insights", params={"metric": ",".join(metrics), "period": "day", "metric_type": "total_value",
                                                           "since": str(since), "until": str(until)})

    def user_counts(self, ig_user_id: str) -> dict:
        return self.get(ig_user_id, params={"fields": "followers_count,media_count,username"})

    def refresh_token(self) -> dict:
        # the refresh endpoint takes the token as a query parameter by design (API_NOTES)
        return self.request("GET", self.url("refresh_access_token", versioned=False),
                            params={"grant_type": "ig_refresh_token"}, auth="query")


def same_origin(url: str, base: str) -> bool:
    """True if url has the same scheme://host[:port] as base (the token is only ever sent
    to the configured upload host, never to a host named in an API response)."""
    from urllib.parse import urlsplit

    a, b = urlsplit(url), urlsplit(base)
    return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port)


def parse_metric_values(payload: dict, wanted: list[str]) -> dict[str, Any]:
    """Graph insights -> {metric: value|None}. Handles `values[0].value` and
    `total_value.value`; a metric with no data is None, never 0 (API_NOTES)."""
    out: dict[str, Any] = {m: None for m in wanted}
    for item in (payload or {}).get("data", []) or []:
        name = item.get("name")
        if name not in out:
            continue
        val = None
        if isinstance(item.get("total_value"), dict):
            val = item["total_value"].get("value")
        elif item.get("values"):
            val = item["values"][-1].get("value")
        out[name] = val
    return out


def safe_payload(payload: dict) -> dict:
    return redact_obj(payload)
