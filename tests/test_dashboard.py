"""Dashboard: static build (demo and empty DB) and the local server's security rules."""
from __future__ import annotations

import html
import http.client
import re
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reelengine import db
from reelengine.config import load_config
from reelengine.dashboard.build import CSP, PAGES, build_dashboard
from reelengine.dashboard.demo import DemoError, seed_demo
from reelengine.dashboard.server import SECURITY_HEADERS, make_server

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
BANNER = "fake reels for layout review, nothing here is real"


@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    return load_config(overrides={"paths": {"data_dir": str(data)}})


@pytest.fixture(scope="module")
def demo(cfg, tmp_path_factory):
    root = tmp_path_factory.mktemp("demo")
    seed_demo(root / "demo.sqlite", cfg, root / "thumbs", now=NOW)
    conn = db.connect(root / "demo.sqlite")
    before = conn.total_changes
    site = build_dashboard(cfg, conn, out_dir=root / "site", demo=True, now=NOW)
    info = {
        "site": site,
        "root": root,
        "changes": conn.total_changes - before,
        "reels": conn.execute("SELECT COUNT(*) FROM reels").fetchone()[0],
        "states": {r[0] for r in conn.execute("SELECT DISTINCT state FROM reels")},
    }
    conn.close()
    return info


@pytest.fixture(scope="module")
def empty(cfg, tmp_path_factory):
    root = tmp_path_factory.mktemp("empty")
    conn = db.connect(root / "fresh.sqlite")
    site = build_dashboard(cfg, conn, out_dir=root / "site", now=NOW)
    conn.close()
    return site


def _pages(site: Path) -> dict[str, str]:
    return {str(p.relative_to(site)): p.read_text(encoding="utf-8") for p in sorted(site.rglob("*.html"))}


# ------------------------------------------------------------------------------ build
def test_demo_seed_shape(demo):
    assert demo["reels"] == 30
    assert {"new", "transcribed", "rendered", "qa_passed", "scheduled", "published", "held", "failed"} <= demo["states"]
    conn = sqlite3.connect(demo["root"] / "demo.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM reels WHERE is_demo=0").fetchone()[0] == 0
    reasons = [r[0] for r in conn.execute("SELECT held_reason FROM reels WHERE state='held'")]
    assert any("Siyaq placed 3rd" in r for r in reasons)
    assert [r[0] for r in conn.execute("SELECT version FROM playbooks ORDER BY version")] == [1, 2, 3]
    assert "rejected" in conn.execute("SELECT changes_json FROM playbooks WHERE version=3").fetchone()[0]
    conn.close()
    assert len(list((demo["root"] / "thumbs").glob("*.png"))) >= 20


def test_every_page_exists(demo):
    site = demo["site"]
    for name, _ in PAGES:
        assert (site / f"{name}.html").is_file(), name
    assert len(list((site / "reels").glob("*.html"))) == demo["reels"]
    assert (site / "assets" / "app.css").is_file()
    for font in ("Inter-var.ttf", "JetBrainsMono-var.ttf", "IBMPlexSansArabic-Regular.ttf"):
        assert (site / "assets" / "fonts" / font).is_file(), font
    # no leftover temp dirs from the atomic swap
    assert not [p for p in site.parent.iterdir() if p.name.startswith(".site.")]


def test_build_is_read_only(demo):
    assert demo["changes"] == 0


def test_rebuild_swaps_in_place(cfg, demo):
    conn = db.connect(demo["root"] / "demo.sqlite")
    out = build_dashboard(cfg, conn, out_dir=demo["site"], demo=True, now=NOW)
    conn.close()
    assert out == demo["site"] and (out / "index.html").is_file()


@pytest.mark.parametrize("which", ["demo", "empty"])
def test_no_inline_styles_scripts_or_handlers(which, demo, empty):
    site = demo["site"] if which == "demo" else empty
    for name, text in _pages(site).items():
        assert not re.search(r"\sstyle\s*=", text), f"style= in {name}"
        assert "<style" not in text.lower(), f"<style> in {name}"
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text, re.I), f"inline <script> in {name}"
        assert not re.search(r"<script[^>]*>\s*[^<\s]", text, re.I), f"script body in {name}"
        assert not re.search(r"\son[a-z]+\s*=\s*[\"']", text, re.I), f"on* handler in {name}"
        assert "javascript:" not in text.lower(), name


@pytest.mark.parametrize("which", ["demo", "empty"])
def test_no_external_urls(which, demo, empty):
    site = demo["site"] if which == "demo" else empty
    for name, text in _pages(site).items():
        assert "http://" not in text and "https://" not in text, f"external URL in {name}"
        assert not re.search(r"(?:src|href|poster|srcset)\s*=\s*\"//", text), f"protocol-relative URL in {name}"
    css = (site / "assets" / "app.css").read_text(encoding="utf-8")
    assert "http:" not in css and "https:" not in css and "@import" not in css
    for url in re.findall(r"url\(([^)]*)\)", css):
        assert url.strip("\"'").startswith("fonts/"), url


@pytest.mark.parametrize("which", ["demo", "empty"])
def test_csp_meta_present(which, demo, empty):
    site = demo["site"] if which == "demo" else empty
    for name, text in _pages(site).items():
        m = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', text)
        assert m, f"no CSP meta in {name}"
        assert html.unescape(m.group(1)) == CSP


def test_demo_banner_only_in_demo(demo, empty):
    for name, text in _pages(demo["site"]).items():
        assert 'class="demo-banner"' in text and BANNER in text, name
    for name, text in _pages(empty).items():
        assert "demo-banner" not in text and BANNER not in text, name


def test_arabic_is_marked_rtl(demo):
    text = (demo["site"] / "brief.html").read_text(encoding="utf-8")
    assert re.search(r'<p class="say-line" lang="ar" dir="rtl">', text)
    detail = (demo["site"] / "reels" / "1.html").read_text(encoding="utf-8")
    assert '<p class="caption" lang="ar" dir="rtl">' in detail
    assert '<video class="player"' not in detail or 'preload="none"' in detail


def test_empty_db_builds_with_empty_states(empty):
    pages = _pages(empty)
    assert set(pages) == {f"{n}.html" for n, _ in PAGES}  # no reel pages
    expect = {
        "index.html": ["No follower history yet", "No published reels yet", "Nothing scheduled", "✍️", "Instagram is not connected yet"],
        "pipeline.html": ["No clips yet", "Nothing held or failed"],
        "reels.html": ["No reels yet"],
        "learning.html": ["Only the starting playbook", "No changes yet", "starting priors", "Nothing scored yet"],
        "brief.html": ["No brief yet"],
        "health.html": ["No ticks yet", "Not connected"],
        "money.html": ["Off until the offer page is live and the legal checklist is done ✍️", "No orders yet", "No link taps recorded"],
    }
    for page, needles in expect.items():
        for needle in needles:
            assert needle in pages[page], f"{needle!r} missing from {page}"


def test_seed_refuses_the_real_db(cfg, tmp_path):
    with pytest.raises(DemoError):
        seed_demo(cfg.db_path, cfg, tmp_path / "thumbs")


def test_seed_refuses_a_non_demo_file(cfg, tmp_path):
    other = tmp_path / "mine.sqlite"
    c = db.connect(other)
    c.close()
    with pytest.raises(DemoError):
        seed_demo(other, cfg, tmp_path / "thumbs")
    assert other.exists()


# ------------------------------------------------------------------------------ server
class StubCfg:
    def __init__(self, host: str, data_dir: Path):
        self.data = {"dashboard": {"host": host, "port": 0}}
        self.data_dir = data_dir

    def __getitem__(self, key):
        return self.data[key]


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.5", "example.com", ""])
def test_server_refuses_non_loopback(host, tmp_path):
    with pytest.raises(ValueError):
        make_server(StubCfg(host, tmp_path), tmp_path)


@pytest.fixture(scope="module")
def server(demo):
    media = demo["root"] / "media"
    (media / "out").mkdir(parents=True, exist_ok=True)
    (media / "thumbs").mkdir(exist_ok=True)
    (media / "out" / "7.mp4").write_bytes(bytes(range(256)) * 40)  # 10 240 bytes
    shutil.copyfile(demo["root"] / "thumbs" / "1.png", media / "thumbs" / "7.png")
    (media / "engine.sqlite").write_text("SECRET-DB", encoding="utf-8")
    (demo["root"] / "engine.sqlite").write_text("SECRET-DB", encoding="utf-8")
    httpd = make_server(StubCfg("127.0.0.1", media), demo["site"], port=0, media_root=media)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _req(port: int, method: str, path: str, headers: dict | None = None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = headers or {}
    c.putrequest(method, path, skip_host="Host" in headers, skip_accept_encoding=True)
    for k, v in headers.items():
        c.putheader(k, v)
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


def _assert_security_headers(r):
    for k, v in SECURITY_HEADERS.items():
        assert r.getheader(k) == v, k


def test_server_serves_pages_with_security_headers(server):
    r, body = _req(server, "GET", "/")
    assert r.status == 200 and body.startswith(b"<!doctype html>")
    assert r.getheader("Content-Type").startswith("text/html")
    _assert_security_headers(r)
    r, _ = _req(server, "GET", "/assets/app.css")
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/css")
    r, _ = _req(server, "GET", "/thumbs/7.png")
    assert r.status == 200 and r.getheader("Content-Type") == "image/png"


@pytest.mark.parametrize(
    "path",
    [
        "/media/../engine.sqlite",
        "/media/%2e%2e/engine.sqlite",
        "/media/%2E%2E/engine.sqlite",
        "/media/..%2fengine.sqlite",
        "/media/..%5cengine.sqlite",
        "/media/%252e%252e/engine.sqlite",
        "/media/..\\engine.sqlite",
        "/thumbs/../../engine.sqlite",
        "/thumbs/..%2f..%2fengine.sqlite",
        "/%2e%2e/engine.sqlite",
        "/../engine.sqlite",
        "/reels/../../engine.sqlite",
        "/assets/../../media/engine.sqlite",
        "//engine.sqlite",
        "/engine.sqlite",
        "/media/engine.sqlite",
        "/media/7.mp4/../../engine.sqlite",
        "/.%2e/engine.sqlite",
        "/media/7.mp4%00.png",
        "/thumbs/7.PNG/..",
    ],
)
def test_server_blocks_traversal(server, path):
    r, body = _req(server, "GET", path)
    assert r.status in (400, 403, 404), (path, r.status)
    assert b"SECRET-DB" not in body
    _assert_security_headers(r)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "BREW"])
def test_server_only_get_and_head(server, method):
    r, _ = _req(server, method, "/index.html", {"Content-Length": "0"})
    assert r.status == 405
    assert r.getheader("Allow") == "GET, HEAD"
    _assert_security_headers(r)


def test_server_head(server):
    r, body = _req(server, "HEAD", "/media/7.mp4")
    assert r.status == 200 and body == b""
    assert r.getheader("Content-Length") == "10240" and r.getheader("Accept-Ranges") == "bytes"


def test_server_range_requests(server):
    data = bytes(range(256)) * 40
    r, body = _req(server, "GET", "/media/7.mp4", {"Range": "bytes=100-199"})
    assert r.status == 206 and r.getheader("Content-Range") == "bytes 100-199/10240"
    assert body == data[100:200]
    r, body = _req(server, "GET", "/media/7.mp4", {"Range": "bytes=-16"})
    assert r.status == 206 and body == data[-16:]
    r, body = _req(server, "GET", "/media/7.mp4", {"Range": "bytes=10000-"})
    assert r.status == 206 and body == data[10000:]
    r, _ = _req(server, "GET", "/media/7.mp4", {"Range": "bytes=20000-"})
    assert r.status == 416 and r.getheader("Content-Range") == "bytes */10240"
    r, body = _req(server, "GET", "/media/7.mp4")
    assert r.status == 200 and body == data and r.getheader("Content-Type") == "video/mp4"


def test_server_rejects_foreign_host_header(server):
    r, _ = _req(server, "GET", "/", {"Host": "evil.example:8765"})
    assert r.status == 403
    r, _ = _req(server, "GET", "/", {"Host": "localhost:8765"})
    assert r.status == 200
