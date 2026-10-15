"""Dashboard v1.2 front end: "Coming up" (Overview + Calendar), the "Link account" dialog and "What to post".
The templates are rendered with the real demo context plus a fixed v1.2 payload, so these tests don't depend
on what the collectors happen to find in the demo DB on a given day."""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reelengine import db
from reelengine.config import load_config
from reelengine.dashboard import build as B
from reelengine.dashboard.demo import seed_demo

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
STATIC = Path(B.__file__).resolve().parent / "static"

HOOK_AR = "عيد الوطن مو بس أعلام"
IDEA = {"title": "يوم الوطن بثلاث لقطات", "hook_line": HOOK_AR, "beats": ["لقطة أولى", "لقطة ثانية", "لقطة ثالثة"],
        "shot_note": "Phone on a tripod, natural light", "length_s": 25}
COMING_UP = [
    {"slug": "saudi-national-day", "name_ar": "اليوم الوطني", "name_en": "Saudi National Day", "category": "national",
     "cat_label": "National", "official": True, "start": "2026-10-02", "end": "2026-10-02", "days_until": 3,
     "approximate": False, "hijri": "20 Rabi al-Thani 1448", "next_lead": 3, "ideas_source": "brain",
     "ideas": [IDEA, dict(IDEA, title="Second idea", hook_line="A hook in English", length_s=30)],
     "checklist": ["تأكد من الشعار الرسمي", "No logos you don't own"], "film_by": "2026-09-29",
     "source_text": "www.my.gov.sa/national-day", "checked_on": "2026-09-01", "angle": "What the day means to students"},
    {"slug": "eid-test", "name_ar": "عيد تجريبي", "name_en": "Test Eid", "category": "religious", "cat_label": "Religious",
     "official": True, "start": "2026-09-29", "end": "2026-10-01", "days_until": 0, "approximate": True,
     "hijri": "1 Shawwal 1448", "next_lead": None, "ideas_source": "template", "ideas": [], "checklist": [],
     "film_by": None, "source_text": "", "checked_on": None, "angle": ""},
]


def _rec(basis: str) -> dict:
    ideas = [{"topic": f"Topic {n}", "hook_line": HOOK_AR if n == 1 else f"Hook {n}", "format": "Talking head",
              "length": "20-30 s", "best_time": "21:00", "why": "Saves are high on how-to reels"} for n in range(1, 6)]
    return {"created": NOW, "basis": basis, "source": "brain" if basis == "numbers" else "template",
            "niche": "نصائح للطلاب عن الوظائف", "ideas": ideas,
            "occasions": [{"slug": "saudi-national-day", "name_ar": "اليوم الوطني", "name_en": "Saudi National Day",
                           "start": "2026-10-02", "days_until": 3, "angle": "Career pride"},
                          {"slug": "far-day", "name_ar": "يوم بعيد", "name_en": "Far day", "start": "2026-12-01",
                           "days_until": 63, "angle": ""}],
            "note": "Written from a pseudonym only.", "summary": ""}


@pytest.fixture(scope="module")
def base_ctx(tmp_path_factory):
    root = tmp_path_factory.mktemp("v12")
    cfg = load_config(overrides={"paths": {"data_dir": str(root / "data")}})
    seed_demo(root / "demo.sqlite", cfg, root / "thumbs", now=NOW)
    conn = db.connect(root / "demo.sqlite")
    ctx = B.collect(cfg, conn, True, NOW)
    conn.close()
    return ctx


def _render(ctx: dict, out: Path) -> dict[str, str]:
    B.render_site(ctx, out)
    return {p.name: p.read_text(encoding="utf-8") for p in out.glob("*.html")}


@pytest.fixture(scope="module")
def site(base_ctx, tmp_path_factory):
    ctx = copy.copy(base_ctx)
    ctx["coming_up"] = COMING_UP
    acc = dict(ctx["accounts"])
    rows = acc["rows"]
    assert len(rows) >= 2, "the demo seeds at least two accounts"
    acc["recs"] = {rows[0]["username"]: _rec("description"), rows[1]["username"]: _rec("numbers")}
    ctx["accounts"] = acc
    return {"pages": _render(ctx, tmp_path_factory.mktemp("site")), "users": [rows[0]["username"], rows[1]["username"]]}


def test_overview_coming_up_is_compact(site):
    t = site["pages"]["index.html"]
    assert 'aria-labelledby="up-ov-h"' in t and 'class="up-list up-list-compact"' in t
    assert 'id="ov-up-saudi-national-day"' in t
    assert f'<p class="say-line" lang="ar" dir="rtl">{HOOK_AR}</p>' in t
    assert 'href="calendar.html#up-saudi-national-day"' in t and 'href="calendar.html#coming-up"' in t
    assert "up-idea-grid" not in t and "class=\"beats\"" not in t  # hook lines only on the Overview
    assert "Today" in t and "Template ideas: the brain was unavailable" in t


def test_calendar_coming_up_replaces_the_strip(site):
    t = site["pages"]["calendar.html"]
    assert 'id="coming-up"' in t and "Coming up · next 14 days" in t
    assert "cal-strip" not in t
    assert 'id="up-saudi-national-day"' in t and 'id="up-saudi-national-day-h"' in t
    assert "Next reminder 3 days before" in t and "film by" in t
    assert '<ol class="beats" lang="ar" dir="rtl">' in t
    assert "Before you post" in t and '<li lang="ar" dir="rtl">تأكد من الشعار الرسمي</li>' in t
    assert "www.my.gov.sa/national-day" in t and 'data-copy="src-saudi-national-day" data-write-only' in t
    assert "Ideas are written at the next daily check." in t  # the occasion with no ideas yet
    # month grids and the table still use the shared chips
    assert 'class="chip chip-sm cal-cat-national"' in t and "cal-flag-official" in t


def test_accounts_what_to_post(site):
    t = site["pages"]["accounts.html"]
    u0, u1 = site["users"]
    assert 'aria-labelledby="rec-h"' in t
    for u in (u0, u1):
        assert f'id="rec-{u}"' in t and f'id="rec-{u}-h" tabindex="-1"' in t
    assert t.count('class="rec-idea"') == 10  # 5 ideas x 2 accounts: 3 shown + 2 behind "Show 2 more ideas"
    assert "Show 2 more ideas" in t and '<ol class="rec-ideas" start="4">' in t
    assert "Based on your description; add reel numbers for sharper advice." in t
    assert f"reel accounts import {u0} --file posts.csv" in t  # the read-only hint
    assert f'data-write-only data-acct-import-for="{u0}"' in t
    assert "stored reel" in t  # the numbers-based plan
    assert "Saudi days that fit" in t
    assert 'href="calendar.html#up-saudi-national-day" lang="ar" dir="rtl"' in t
    assert 'href="calendar.html#up-far-day"' not in t  # beyond 14 days: no link (no card to land on)
    assert 'data-link-open aria-haspopup="dialog"' in t


def test_link_dialog_and_buttons_are_write_only(site):
    for name, t in site["pages"].items():
        assert t.count("<dialog") == 1, name
        assert re.search(r'<dialog class="link-dlg"[^>]*\bdata-write-only\b[^>]*\bdata-link-dialog\b', t), name
        for m in re.finditer(r"<button[^>]*data-link-open[^>]*>", t):
            # either the button itself or its wrapper carries data-write-only
            before = t[: m.start()].rsplit("<p", 1)[-1]
            assert "data-write-only" in m.group(0) or before.startswith(' class="btn-row" data-write-only'), name
        assert 'id="link-user-err"' in t and 'id="link-niche-err"' in t and 'id="link-text-err"' in t and 'id="link-file-err"' in t


@pytest.mark.parametrize("page", ["index.html", "calendar.html", "accounts.html"])
def test_new_markup_stays_csp_clean(site, page):
    t = site["pages"][page]
    assert not re.search(r"\sstyle\s*=", t)
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", t, re.I)
    assert not re.search(r"\son[a-z]+\s*=\s*[\"']", t, re.I)
    assert "http://" not in t and "https://" not in t


def test_old_collectors_still_render(base_ctx, tmp_path):
    """A build without the v1.2 keys (old DB, or an older collector) falls back instead of failing."""
    ctx = copy.copy(base_ctx)
    ctx.pop("coming_up", None)
    acc = dict(ctx["accounts"])
    acc.pop("recs", None)
    ctx["accounts"] = acc
    pages = _render(ctx, tmp_path / "site")
    assert "Coming up" in pages["index.html"] and 'id="coming-up"' in pages["calendar.html"]
    assert "No plan yet" in pages["accounts.html"]


def test_empty_coming_up_and_no_recs(base_ctx, tmp_path):
    ctx = copy.copy(base_ctx)
    ctx["coming_up"] = []
    ctx["accounts"] = dict(ctx["accounts"], recs={})
    pages = _render(ctx, tmp_path / "site")
    assert "Nothing in the next 14 days" in pages["index.html"]
    assert "Nothing in the next 14 days" in pages["calendar.html"]
    assert "No plan yet" in pages["accounts.html"]


def test_app_js_link_dialog_contract():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "document.write" not in js
    assert "initLink()" in js and "function focusRecTarget()" in js
    assert '"recommend_status"' in js and 'action: "link"' in js
    assert "e.field = j.field" in js  # post() keeps the server's field name
    # focus target works in read-only views too: it runs before the http-only/session gate
    assert js.index("setTimeout(focusRecTarget, 0)") < js.index('if (location.protocol !== "http:")')
