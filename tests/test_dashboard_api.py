"""The dashboard's write API: same-origin + CSRF on every POST, the upload path (streamed to a dot temp
file, checked, moved atomically into the inbox, then the normal intake), calendar and account edits.
Uploading never schedules or publishes anything."""
from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from reelengine import db, intake
from reelengine.dashboard.api import WriteApi, safe_filename, ApiError
from reelengine.dashboard.build import build_dashboard
from reelengine.dashboard.server import make_server

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8 + bytes(range(256)) * 16  # a tiny ISO-BMFF-looking file


@pytest.fixture
def srv(cfg, conn, tmp_path):
    site = build_dashboard(cfg, conn, out_dir=tmp_path / "site")
    rebuilt = []

    def rebuild():
        c = db.connect(cfg.db_path)
        build_dashboard(cfg, c, out_dir=site)
        c.close()
        rebuilt.append(1)

    api = WriteApi(cfg, cfg.db_path, site, rebuild=rebuild)
    httpd = make_server(cfg, site, port=0, api=api)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    port = httpd.server_address[1]
    yield {"port": port, "api": api, "site": site, "rebuilt": rebuilt, "origin": f"http://127.0.0.1:{port}"}
    httpd.shutdown()
    httpd.server_close()


def req(s, method, path, body=b"", headers=None, csrf=True, origin=True):
    h = {"Host": f"127.0.0.1:{s['port']}"}
    if origin:
        h["Origin"] = s["origin"]
    if csrf:
        h["X-CSRF-Token"] = s["api"].csrf
    h.update(headers or {})
    c = http.client.HTTPConnection("127.0.0.1", s["port"], timeout=10)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in h.items():
        c.putheader(k, v)
    if body is not None and "Content-Length" not in h and method == "POST":
        c.putheader("Content-Length", str(len(body)))
    c.endheaders()
    if body:
        c.send(body)
    r = c.getresponse()
    data = r.read()
    c.close()
    try:
        return r, json.loads(data or b"{}")
    except ValueError:
        return r, {"raw": data}


def upload(s, data=MP4, name="IMG_0001.MOV", ctype="video/quicktime", **kw):
    return req(s, "POST", "/api/upload", data, {"Content-Type": ctype, "X-Filename": name}, **kw)


def jpost(s, path, obj, **kw):
    return req(s, "POST", path, json.dumps(obj).encode(), {"Content-Type": "application/json"}, **kw)


# ------------------------------------------------------------------ session + guards
def test_session_gives_the_token_without_cors(srv):
    r, j = req(srv, "GET", "/api/session")
    assert r.status == 200 and j["csrf"] == srv["api"].csrf and j["upload"] is True and j["writes"] is True
    assert r.getheader("Access-Control-Allow-Origin") is None
    assert r.getheader("Content-Security-Policy") and r.getheader("X-Frame-Options") == "DENY"


@pytest.mark.parametrize("kw, status", [
    ({"csrf": False}, 403),
    ({"origin": False}, 403),
    ({"headers": {"Origin": "http://evil.example"}}, 403),
    ({"headers": {"X-CSRF-Token": "wrong"}}, 403),
    ({"headers": {"Sec-Fetch-Site": "cross-site"}}, 403),
])
def test_post_needs_same_origin_and_csrf(srv, cfg, kw, status):
    h = {"Content-Type": "video/quicktime", "X-Filename": "a.mov", **kw.pop("headers", {})}
    r, j = req(srv, "POST", "/api/upload", MP4, h, **kw)
    assert r.status == status and "error" in j
    assert not list(cfg.inbox.glob("*")) if cfg.inbox.exists() else True


def test_foreign_host_header_is_refused(srv):
    r, _ = req(srv, "POST", "/api/upload", MP4, {"Host": "evil.example:80", "Origin": "http://evil.example:80", "Content-Type": "video/mp4", "X-Filename": "a.mp4"})
    assert r.status == 403


def test_non_api_post_is_still_405_and_options_too(srv):
    r, _ = req(srv, "POST", "/index.html", b"")
    assert r.status == 405 and r.getheader("Allow") == "GET, HEAD"
    r, _ = req(srv, "OPTIONS", "/api/upload", None)
    assert r.status == 405


def test_no_api_routes_without_writes(cfg, conn, tmp_path):
    site = build_dashboard(cfg, conn, out_dir=tmp_path / "ro")
    httpd = make_server(cfg, site, port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        c.request("GET", "/api/session")
        assert c.getresponse().status == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


# ------------------------------------------------------------------ upload
def test_upload_lands_in_inbox_then_normal_intake(srv, cfg, conn):
    r, j = upload(srv)
    assert r.status == 200 and j["status"] == "queued" and j["name"] == "IMG_0001.mov"
    files = sorted(p.name for p in cfg.inbox.iterdir())
    assert files == ["IMG_0001.mov"]  # no .part left behind
    assert (cfg.inbox / "IMG_0001.mov").read_bytes() == MP4
    assert srv["rebuilt"]
    # nothing happened to the pipeline yet: intake needs its stability ticks
    assert conn.execute("SELECT COUNT(*) FROM reels").fetchone()[0] == 0
    for _ in range(int(cfg["intake"]["stable_ticks"]) + 1):
        res = intake.scan(cfg, conn)
    assert len(res["taken"]) == 1
    row = db.get_reel(conn, res["taken"][0])
    assert row["state"] == "new" and row["dry_run"] == 0 and row["scheduled_at"] is None and row["media_id"] is None
    r, j = req(srv, "GET", "/api/uploads")
    assert j["uploads"][0]["reel"] == row["id"] and j["uploads"][0]["state"] == "new"


def test_upload_never_overwrites_an_inbox_file(srv, cfg):
    cfg.inbox.mkdir(parents=True, exist_ok=True)
    (cfg.inbox / "IMG_0001.mov").write_bytes(b"phone clip")
    r, j = upload(srv)
    assert j["name"] == "IMG_0001-1.mov"
    assert (cfg.inbox / "IMG_0001.mov").read_bytes() == b"phone clip"
    assert not [p for p in cfg.inbox.iterdir() if p.name.startswith(".")]


def test_duplicate_upload_is_spotted(srv, cfg, conn):
    upload(srv)
    r, j = upload(srv, name="again.mov")
    assert j["status"] == "duplicate"
    assert sorted(p.name for p in cfg.inbox.iterdir()) == ["IMG_0001.mov"]
    for _ in range(int(cfg["intake"]["stable_ticks"]) + 1):
        intake.scan(cfg, conn)
    r, j = upload(srv, name="third.mov")
    assert j["status"] == "duplicate" and j["reel"] == 1


@pytest.mark.parametrize("data, name, ctype, status", [
    (MP4, "clip.avi", "video/quicktime", 415),
    (MP4, "clip.mov", "application/octet-stream", 415),
    (MP4, "clip.mp4", "video/quicktime", 415),  # extension/type mismatch
    (b"MZ\x90\x00" + b"\x00" * 4000, "clip.mov", "video/quicktime", 415),  # not an MP4/MOV signature
    (b"\x00" * 100, "clip.mov", "video/quicktime", 400),  # too small
], ids=["avi", "octet-stream", "type-mismatch", "exe-signature", "too-small"])
def test_bad_uploads_are_refused_and_leave_nothing(srv, cfg, data, name, ctype, status):
    r, j = upload(srv, data=data, name=name, ctype=ctype)
    assert r.status == status, j
    assert not cfg.inbox.exists() or list(cfg.inbox.iterdir()) == []


def test_size_cap_and_length_required(srv, cfg):
    srv["api"].max_bytes = 2000
    r, j = upload(srv)
    assert r.status == 413
    r, j = req(srv, "POST", "/api/upload", None, {"Content-Type": "video/mp4", "X-Filename": "a.mp4", "Transfer-Encoding": "chunked"})
    assert r.status in (411, 413)


def test_interrupted_upload_leaves_no_file(srv, cfg):
    c = http.client.HTTPConnection("127.0.0.1", srv["port"], timeout=10)
    c.putrequest("POST", "/api/upload", skip_host=True, skip_accept_encoding=True)
    for k, v in {"Host": f"127.0.0.1:{srv['port']}", "Origin": srv["origin"], "X-CSRF-Token": srv["api"].csrf,
                 "Content-Type": "video/mp4", "X-Filename": "cut.mp4", "Content-Length": str(len(MP4) * 4)}.items():
        c.putheader(k, v)
    c.endheaders()
    c.send(MP4)
    c.sock.shutdown(1)  # the browser goes away mid-upload
    r = c.getresponse()
    assert r.status == 400
    c.close()
    assert not any(cfg.inbox.iterdir())


@pytest.mark.parametrize("raw, want", [
    ("../../evil.mov", "evil.mov"),
    ("..\\..\\Windows\\x.MP4", "x.mp4"),
    ("ملف عربي.mov", "clip.mov"),
    (".hidden.mov", "hidden.mov"),
    ("a b;rm -rf.mov", "a-b-rm--rf.mov"),
    ("x" * 300 + ".m4v", "x" * 56 + ".m4v"),
])
def test_safe_filename(raw, want):
    assert safe_filename(raw, (".mp4", ".mov", ".m4v")) == want


def test_safe_filename_refuses_other_types():
    with pytest.raises(ApiError):
        safe_filename("clip.exe", (".mp4", ".mov"))
    with pytest.raises(ApiError):
        safe_filename("clip.mov.exe", (".mp4", ".mov"))


def test_upload_off_in_demo(cfg, tmp_path):
    api = WriteApi(cfg, cfg.db_path, tmp_path, demo=True)
    assert api.session()["upload"] is False


# ------------------------------------------------------------------ calendar + accounts
def test_calendar_api_round_trip(srv, conn):
    r, j = jpost(srv, "/api/calendar", {"action": "add", "fields": {"name_ar": "يوم", "name_en": "Api Day", "rule": "hijri", "month": 9,
                                                                    "day": 15, "category": "religious", "official": True}})
    assert r.status == 200 and j["slug"] == "api-day"
    r, j = jpost(srv, "/api/calendar", {"action": "edit", "slug": "api-day", "fields": {"day": 31}})
    assert r.status == 400 and "Hijri day" in j["error"]
    r, j = jpost(srv, "/api/calendar", {"action": "edit", "slug": "api-day", "fields": {"day": 16, "angle": "<b>x</b>"}})
    assert r.status == 200
    page = (srv["site"] / "calendar.html").read_text(encoding="utf-8")
    assert "Api Day" in page and "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page  # autoescaped
    r, j = jpost(srv, "/api/calendar", {"action": "delete", "slug": "api-day"})
    assert r.status == 200
    r, j = jpost(srv, "/api/calendar", {"action": "delete", "slug": "api-day"})
    assert r.status == 404


def test_calendar_api_needs_json_and_csrf(srv):
    r, _ = req(srv, "POST", "/api/calendar", b"action=delete", {"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status == 415
    r, _ = jpost(srv, "/api/calendar", {"action": "delete", "slug": "x"}, csrf=False)
    assert r.status == 403


def test_accounts_api(srv, conn):
    r, j = jpost(srv, "/api/accounts", {"action": "attach", "username": "@ref_one", "role": "reference"})
    assert r.status == 200 and j["username"] == "ref_one"
    r, j = jpost(srv, "/api/accounts", {"action": "import", "username": "ref_one", "format": "paste",
                                        "text": "instagram.com/reel/EEEE5555 | 2026-10-08 21:00 | 10 | 1 | 400\nbad"})
    assert r.status == 200 and j["stored"] == 1 and len(j["errors"]) == 1
    r, j = jpost(srv, "/api/accounts", {"action": "attach", "username": "bad name", "role": "reference"})
    assert r.status == 400
    r, j = jpost(srv, "/api/accounts", {"action": "import", "username": ["not", "a", "string"], "text": "instagram.com/reel/FFFF6666"})
    assert r.status == 200 and j["stored"] == 0 and j["errors"]  # a bad type is a clear row error, not a 500
    r, j = jpost(srv, "/api/accounts", {"action": "forget", "username": "ref_one"})
    assert r.status == 200
    assert conn.execute("SELECT COUNT(*) FROM account_media").fetchone()[0] == 0


def test_body_cap_for_json(srv):
    r, _ = req(srv, "POST", "/api/accounts", b"{}", {"Content-Type": "application/json", "Content-Length": str(10 * 1024 * 1024)})
    assert r.status == 413
