"""Local mock of the Instagram Graph API (Instagram Login path), following
docs/API_NOTES.md. Used by tests/test_publisher.py and `python tests/mock_graph.py`.

Endpoints: /{v}/me · /{v}/{ig}/content_publishing_limit · POST /{v}/{ig}/media ·
POST /ig-api-upload/{v}/{container} · /{v}/{container}?fields=status_code,status ·
POST /{v}/{ig}/media_publish · /{v}/{media} · /{v}/{ig}/media · /{v}/{media}/insights ·
/{v}/{ig}/insights · /{v}/{ig}?fields=followers_count · /refresh_access_token

Scenario knobs (MockState): polls_before_finished, container_result (FINISHED|ERROR|
EXPIRED), fail_next (list of ("path-substring", kind)) where kind is one of
"190", "rate", "500", "drop" (close the socket without answering), "drop_after"
(do the work, then close without answering: the network-drop-mid-publish case),
quota_usage/quota_total, unsupported_metrics, empty_metrics.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VALID = "IGAAmockTokenValue1234567890abcdefXYZ"


@dataclass
class MockState:
    token: str = VALID
    ig_user_id: str = "17841400000000001"
    username: str = "yazeed.test"
    account_type: str = "MEDIA_CREATOR"
    polls_before_finished: int = 2
    container_result: str = "FINISHED"
    quota_usage: int = 0
    quota_total: int = 100
    followers: int = 42
    unsupported_metrics: set = field(default_factory=lambda: {"crossposted_views"})
    empty_metrics: set = field(default_factory=lambda: {"reels_skip_rate"})
    fail_next: list = field(default_factory=list)
    containers: dict = field(default_factory=dict)   # id -> dict(status, polls, caption, uploaded, size)
    media: dict = field(default_factory=dict)        # media id -> dict
    publish_calls: int = 0
    published_containers: list = field(default_factory=list)
    requests: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    seq: int = 1000

    def next_id(self) -> str:
        self.seq += 1
        return f"1790000000{self.seq}"


def make_handler(state: MockState):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # quiet
            pass

        # ---------------------------------------------------------- utils
        def _send(self, code: int, obj: dict, headers: dict | None = None) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-App-Usage", json.dumps({"call_count": 5, "total_cputime": 1, "total_time": 2}))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _err(self, code: int, msg: str, ecode: int, sub: int | None = None, etype: str = "OAuthException") -> None:
            e = {"message": msg, "type": etype, "code": ecode, "fbtrace_id": "mock"}
            if sub:
                e["error_subcode"] = sub
            self._send(code, {"error": e})

        def _drop(self) -> None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.close_connection = True

        def _token(self, q: dict) -> str | None:
            a = self.headers.get("Authorization", "")
            m = re.match(r"(?i)(Bearer|OAuth)\s+(\S+)", a)
            if m:
                return m.group(2)
            return (q.get("access_token") or [None])[0]

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _injected(self, path: str) -> str | None:
            with state.lock:
                for k, (frag, kind) in enumerate(state.fail_next):
                    if frag in path:
                        state.fail_next.pop(k)
                        return kind
            return None

        # ---------------------------------------------------------- dispatch
        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

        def _handle(self, method: str) -> None:
            u = urlparse(self.path)
            q = parse_qs(u.query)
            body = self._body() if method == "POST" else b""
            form = parse_qs(body.decode("utf-8", "replace")) if method == "POST" and not u.path.startswith("/ig-api-upload") else {}
            path = u.path
            state.requests.append((method, path))
            kind = self._injected(path)
            if kind == "drop":
                return self._drop()
            if kind == "500":
                return self._err(500, "An unexpected error has occurred. Please retry your request later.", 2, etype="OAuthException")
            if kind == "rate":
                return self._err(400, "Application request limit reached", 4)
            if kind == "190":
                return self._err(400, "Error validating access token: Session has expired", 190, 463)
            if path == "/refresh_access_token":
                if self._token(q) != state.token:
                    return self._err(400, "Invalid OAuth access token", 190)
                state.token = state.token[::-1] + "R"
                return self._send(200, {"access_token": state.token, "token_type": "bearer", "expires_in": 5183944})
            if self._token(q) != state.token:
                return self._err(400, "Invalid OAuth access token - Cannot parse access token", 190)

            m = re.match(r"^/ig-api-upload/v[\d.]+/(\d+)$", path)
            if m and method == "POST":
                c = state.containers.get(m.group(1))
                if not c:
                    return self._err(400, "Unknown container", 100)
                if not self.headers.get("Authorization", "").startswith("OAuth "):
                    return self._err(400, "rupload needs Authorization: OAuth", 190)
                size = int(self.headers.get("file_size") or -1)
                if size != len(body) or self.headers.get("offset") != "0":
                    return self._err(400, "file_size/offset mismatch", 100)
                c["uploaded"] = True
                c["size"] = size
                return self._send(200, {"success": True, "message": "Upload successful."})

            m = re.match(r"^/v[\d.]+/(.+)$", path)
            if not m:
                return self._err(404, "Unknown path", 100)
            parts = m.group(1).split("/")
            ig = state.ig_user_id
            if parts == ["me"]:
                return self._send(200, {"id": ig, "user_id": ig, "username": state.username, "account_type": state.account_type, "media_count": len(state.media)})
            if parts == [ig, "content_publishing_limit"]:
                return self._send(200, {"data": [{"quota_usage": state.quota_usage, "config": {"quota_total": state.quota_total, "quota_duration": 86400}}]})
            if parts == [ig, "media"] and method == "POST":
                if form.get("media_type") != ["REELS"] or form.get("upload_type") != ["resumable"]:
                    return self._err(400, "media_type=REELS and upload_type=resumable required", 100)
                cid = state.next_id()
                state.containers[cid] = {"status": "IN_PROGRESS", "polls": 0, "caption": (form.get("caption") or [""])[0], "uploaded": False,
                                         "thumb_offset": (form.get("thumb_offset") or [None])[0]}
                return self._send(200, {"id": cid, "uri": f"http://{self.headers['Host']}/ig-api-upload/v26.0/{cid}"})
            if parts == [ig, "media_publish"] and method == "POST":
                cid = (form.get("creation_id") or [""])[0]
                c = state.containers.get(cid)
                if not c:
                    return self._err(400, "Invalid creation_id", 100)
                if c["status"] == "PUBLISHED":
                    return self._err(400, "This media has already been published", 100, 2207006)
                if c["status"] != "FINISHED":
                    return self._err(400, "Media is not ready to be published", 9007, 2207027)
                with state.lock:
                    state.publish_calls += 1
                    c["status"] = "PUBLISHED"
                    mid = state.next_id()
                    c["media_id"] = mid
                    state.published_containers.append(cid)
                    state.media[mid] = {"id": mid, "caption": c["caption"], "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime()),
                                        "permalink": f"https://www.instagram.com/reel/MOCK{mid[-6:]}/", "media_type": "VIDEO", "media_product_type": "REELS"}
                if kind == "drop_after":
                    return self._drop()
                return self._send(200, {"id": mid})
            if parts == [ig, "media"]:
                items = sorted(state.media.values(), key=lambda x: x["id"], reverse=True)
                return self._send(200, {"data": items, "paging": {"next": f"https://graph.instagram.com/v26.0/{ig}/media?access_token={state.token}&after=x"}})
            if parts == [ig, "insights"]:
                names = (q.get("metric") or [""])[0].split(",")
                return self._send(200, {"data": [{"name": n, "period": "day", "total_value": {"value": 10 + i}} for i, n in enumerate(names)]})
            if parts == [ig]:
                return self._send(200, {"id": ig, "followers_count": state.followers, "media_count": len(state.media), "username": state.username})
            if len(parts) == 2 and parts[1] == "insights" and parts[0] in state.media:
                names = (q.get("metric") or [""])[0].split(",")
                bad = [n for n in names if n in state.unsupported_metrics]
                if bad:
                    return self._err(400, f"(#100) metric[{bad[0]}] is not supported", 100, etype="OAuthException")
                data = []
                for i, n in enumerate(names):
                    if n in state.empty_metrics:
                        continue  # empty data: metric omitted -> engine stores NULL
                    val = 1500 + 7 * i if n not in ("ig_reels_avg_watch_time",) else 9100
                    data.append({"name": n, "period": "lifetime", "values": [{"value": val}]})
                return self._send(200, {"data": data})
            if len(parts) == 1 and parts[0] in state.containers:
                c = state.containers[parts[0]]
                if c["status"] == "IN_PROGRESS" and c["uploaded"]:
                    c["polls"] += 1
                    if c["polls"] > state.polls_before_finished:
                        c["status"] = state.container_result
                return self._send(200, {"status_code": c["status"], "status": f"{c['status']}: mock", "id": parts[0]})
            if len(parts) == 1 and parts[0] in state.media:
                return self._send(200, state.media[parts[0]])
            return self._err(404, f"Unsupported get request: {path}", 100)

    return H


class MockGraph:
    def __init__(self, state: MockState | None = None):
        self.state = state or MockState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "MockGraph":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    with MockGraph() as m:
        print(f"mock Graph API on {m.base} (token {VALID[:6]}…); Ctrl+C to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
