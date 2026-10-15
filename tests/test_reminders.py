"""v1.2 occasion reminders: lead times (14/7/3/0), one reminder per lead, missed leads never sent late,
ideas (one brain call per run, cached per occurrence, template fallback and upgrade), the legal
checklist, dry-run, the dashboard read path and `reel calendar remind`."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from reelengine import brain, db, ideas, occasions as oc
from reelengine.cli import main as cli_main
from reelengine.config import ConfigError, PACKAGE_ROOT
from reelengine.gates import load_brand, load_occasion_rules

SEED = PACKAGE_ROOT / "brand" / "occasions.yaml"
DAY = date(2027, 3, 1)  # the test occasion's day


@pytest.fixture(autouse=True)
def _fake_brain_only():
    brain.set_provider(brain.FakeBrain({}))  # any task -> BrainError -> templates (tests override per case)
    yield
    brain.set_provider(None)


class Notes:
    """A notify callable that takes the v1.2 `kind` kwarg and records everything."""

    def __init__(self):
        self.sent: list[dict] = []

    def __call__(self, level, title, body="", reel_id=None, kind="general"):
        self.sent.append({"level": level, "title": title, "body": body, "kind": kind})

    @property
    def titles(self):
        return [s["title"] for s in self.sent]


@pytest.fixture
def one(conn):
    """A single tech one-off (no seed), so day counts are easy to read."""
    oc.add(conn, {"name_ar": "يوم الاختبار التقني", "name_en": "Test Tech Day", "rule": "once", "year": DAY.year, "month": DAY.month,
                  "day": DAY.day, "category": "tech", "official": False, "angle": "an angle"})
    return conn


def at(days_before: int) -> date:
    return DAY - timedelta(days=days_before)


def _ideas_answer(prompt: str, n: int = 3, hook: str = "ثلاث أدوات تختصر عليك وقت المذاكرة") -> dict:
    data = json.loads(prompt.rsplit("<data>", 1)[1].split("</data>", 1)[0])
    return {"occasions": [{"id": o["id"], "start": o["start"], "ideas": [
        {"title": f"Idea {k}", "hook_line": hook, "beats": ["أول نقطة", "ثاني نقطة", "ثالث نقطة"],
         "shot_note": "Face in the centre, window light.", "length_s": 30} for k in range(n)]} for o in data]}


# ------------------------------------------------------------------ config
def test_config_defaults_and_old_configs_load(cfg):
    assert cfg["calendar"]["remind_days"] == [14, 7, 3, 0]
    assert cfg["calendar"]["remind_ideas"] is True and cfg["calendar"]["ideas_per_reminder"] == 3
    assert cfg["calendar"]["brain_reserve_calls"] == 10
    assert cfg["calendar"]["lookahead_max_days"] == 21 and cfg["calendar"]["notify"] is True  # v1.1 keys still there


@pytest.mark.parametrize("bad", [{"remind_days": [61]}, {"remind_days": [7, 7]}, {"remind_days": [1, 2, 3, 4, 5, 6, 7]},
                                 {"remind_days": []}, {"remind_days": [-1]}, {"ideas_per_reminder": 4}, {"ideas_per_reminder": 1}])
def test_bad_reminder_config_is_refused(make_cfg, bad):
    with pytest.raises(ConfigError):
        make_cfg({"calendar": bad})


# ------------------------------------------------------------------ lead times
def test_each_lead_fires_once_and_is_idempotent(cfg, one):
    n = Notes()
    assert oc.remind(one, cfg, n, today=at(15)) == []            # further than every lead
    assert oc.remind(one, cfg, n, today=at(14)) == ["test-tech-day"]
    assert oc.remind(one, cfg, n, today=at(14)) == []            # same day again: nothing
    assert oc.remind(one, cfg, n, today=at(10)) == []            # between leads: nothing
    assert oc.remind(one, cfg, n, today=at(7)) == ["test-tech-day"]
    assert oc.remind(one, cfg, n, today=at(3)) == ["test-tech-day"]
    assert oc.remind(one, cfg, n, today=at(0)) == ["test-tech-day"]
    assert oc.remind(one, cfg, n, today=at(0)) == []
    assert oc.remind(one, cfg, n, today=DAY + timedelta(days=1)) == []  # over
    assert n.titles == ["Test Tech Day in 14 days (2027-03-01)", "Test Tech Day in 7 days (2027-03-01)",
                        "Test Tech Day in 3 days (2027-03-01)", "Test Tech Day is today"]
    assert {s["kind"] for s in n.sent} == {"occasion"} and {s["level"] for s in n.sent} == {"info"}


def test_a_missed_lead_is_never_sent_late(cfg, one):
    """The Mac slept through the 14- and 7-day reminders: the 5-days-left run sends the 7-day one (the due
    lead) and marks 14 as done; the 14-day one never comes later."""
    n = Notes()
    assert oc.remind(one, cfg, n, today=at(5)) == ["test-tech-day"]
    for lead in (14, 7):
        assert db.meta_get(one, f"occasion_reminder:test-tech-day:2027-03-01:{lead}")
    assert db.meta_get(one, "occasion_reminder:test-tech-day:2027-03-01:3") is None
    assert oc.remind(one, cfg, n, today=at(4)) == []
    assert oc.remind(one, cfg, n, today=at(1)) == ["test-tech-day"]  # the 3-day lead
    assert n.titles == ["Test Tech Day in 5 days (2027-03-01)", "Test Tech Day in 1 day (2027-03-01)"]


def test_custom_leads_and_disabled_notify(make_cfg, conn):
    oc.add(conn, {"name_ar": "يوم", "name_en": "Custom Day", "rule": "once", "year": 2027, "month": 3, "day": 1, "category": "career"})
    c = make_cfg({"calendar": {"remind_days": [2]}})
    n = Notes()
    assert oc.remind(conn, c, n, today=at(3)) == []
    assert oc.remind(conn, c, n, today=at(2)) == ["custom-day"]
    assert oc.remind(conn, make_cfg({"calendar": {"notify": False}}), n, today=at(0)) == []


def test_multi_day_event_in_progress_is_not_reminded(cfg, conn):
    oc.add(conn, {"name_ar": "معرض", "name_en": "Long Expo", "rule": "once", "year": 2027, "month": 3, "day": 1,
                  "duration_days": 5, "category": "tech"})
    n = Notes()
    assert oc.remind(conn, cfg, n, today=date(2027, 3, 2)) == []
    assert oc.upcoming_reminders(conn, cfg, today=date(2027, 3, 2)) == []


def test_reminder_body_has_day_date_hijri_status_ideas_and_checklist_once(cfg, conn):
    oc.ensure_seeded(conn, cfg)
    n = Notes()
    today = date(2027, 2, 8) - timedelta(days=7)  # Ramadan 1448 starts 2027-02-08 (Umm al-Qura)
    assert "ramadan-start" in oc.remind(conn, cfg, n, today=today)
    msg = next(s for s in n.sent if s["title"].startswith("Start of Ramadan"))
    body = msg["body"]
    assert msg["title"] == "Start of Ramadan in 7 days (2027-02-08)"
    assert "بداية رمضان" in body and "Start of Ramadan" in body
    assert "When: 2027-02-08" in body and "1 Ramadan 1448" in body and "may move ±1 day after moon sighting" in body
    assert "Days left: 7" in body and "Not an official occasion or holiday" in body
    assert body.count("\n1. ") == 1 and "\n3. " in body and "\n4. " not in body
    assert "Hook: " in body and "   1) " in body and "   3) " in body and "Shot: " in body and "Length: ~" in body
    checklist = load_occasion_rules(cfg.brand_dir)["category_checklist"]["religious"]
    for line in checklist:
        assert body.count(line) == 1
    assert "Re-check the date after the moon-sighting announcement." in body


def test_official_day_says_so(cfg, conn):
    oc.ensure_seeded(conn, cfg)
    n = Notes()
    oc.remind(conn, cfg, n, today=date(2027, 9, 23))
    name = oc.get(conn, "national-day")["name_en"]
    msg = next(s for s in n.sent if s["title"] == f"{name} is today")
    assert "Official Saudi occasion" in msg["body"] and "Days left: today" in msg["body"] and "Hijri" not in msg["body"]


def test_notify_without_kind_still_works(cfg, one):
    got = []
    oc.remind(one, cfg, lambda level, title, body="", rid=None: got.append(title), today=at(3))  # a v1.1 callable
    assert got == ["Test Tech Day in 3 days (2027-03-01)"]


# ------------------------------------------------------------------ ideas: brain, cache, fallback, upgrade
def test_one_brain_call_for_all_occurrences_cached_and_reused(cfg, conn):
    for k, d in enumerate((1, 2, 3), 1):
        oc.add(conn, {"name_ar": f"يوم {k}", "name_en": f"Day {k}", "rule": "once", "year": 2027, "month": 3, "day": d, "category": "tech"})
    fb = brain.FakeBrain({"occasion_ideas": lambda p, s: _ideas_answer(p)})
    brain.set_provider(fb)
    n = Notes()
    assert sorted(oc.remind(conn, cfg, n, today=date(2027, 2, 20))) == ["day-1", "day-2", "day-3"]
    assert len(fb.calls) == 1  # one call for the whole window
    prompt = fb.calls[0][1]
    assert "<data>" in prompt and "DATA" in prompt and "day-1" in prompt
    assert conn.execute("SELECT COUNT(*) FROM occasion_ideas WHERE source='brain'").fetchone()[0] == 3
    assert all("Idea 0" in s["body"] and "(templates)" not in s["body"] for s in n.sent)
    # the next lead reuses the cached generation; the dashboard reads the same ideas
    oc.remind(conn, cfg, n, today=date(2027, 2, 24))
    assert len(fb.calls) == 1
    up = oc.upcoming_reminders(conn, cfg, today=date(2027, 2, 24))
    assert {u["ideas_source"] for u in up} == {"brain"} and up[0]["ideas"][0]["title"] == "Idea 0"
    assert conn.execute("SELECT task, ok FROM brain_calls").fetchall()[0]["task"] == "occasion_ideas"


@pytest.mark.parametrize("hook, beats_ok", [
    ("علق بكلمة نعم وأرسل لك الملف", True),                      # engagement bait (gates.yaml)
    ("تابع @someone عشان تعرف أكثر", True),                       # @handle
    ("شوف الرابط https://example.com", True),                     # link
    ("كلمة " * 15, True),                                        # over 14 words
    ("فكرة عادية وبسيطة", False),                                  # 4 beats
])
def test_bad_brain_ideas_fall_back_to_templates_then_upgrade(cfg, one, hook, beats_ok):
    def bad(prompt, schema):
        out = _ideas_answer(prompt, hook=hook)
        if not beats_ok:
            for i in out["occasions"][0]["ideas"]:
                i["beats"].append("رابع")
        return out

    if not beats_ok:  # the schema already refuses 4 beats; check validate() too
        assert ideas.idea_problems({"title": "t", "hook_line": "ok", "beats": ["a", "b", "c", "d"], "shot_note": ""}, cfg,
                                   load_brand(cfg.brand_dir)) == ["exactly 3 beats"]
    fb = brain.FakeBrain({"occasion_ideas": bad})
    brain.set_provider(fb)
    n = Notes()
    oc.remind(one, cfg, n, today=at(14))
    assert len(fb.calls) == 1 + cfg["brain"]["retries"]  # retried inside ONE brain.ask
    assert conn_rows(one, "SELECT COUNT(*) FROM brain_calls") == 1
    row = one.execute("SELECT source FROM occasion_ideas").fetchone()
    assert row["source"] == "template" and "(templates)" in n.sent[0]["body"]
    # next run with a working brain upgrades the cached templates (still one call)
    fb2 = brain.FakeBrain({"occasion_ideas": lambda p, s: _ideas_answer(p)})
    brain.set_provider(fb2)
    oc.remind(one, cfg, n, today=at(7))
    assert len(fb2.calls) == 1 and one.execute("SELECT source FROM occasion_ideas").fetchone()["source"] == "brain"
    assert "(templates)" not in n.sent[-1]["body"]


def conn_rows(conn, sql) -> int:
    return conn.execute(sql).fetchone()[0]


def test_unknown_slug_and_missing_ideas_are_rejected(cfg, one):
    def wrong(prompt, schema):
        out = _ideas_answer(prompt)
        out["occasions"][0]["id"] = "made-up-day"
        return out

    fb = brain.FakeBrain({"occasion_ideas": wrong})
    brain.set_provider(fb)
    oc.remind(one, cfg, Notes(), today=at(14))
    assert one.execute("SELECT source FROM occasion_ideas").fetchone()["source"] == "template"
    assert "unknown occasion" in one.execute("SELECT error FROM brain_calls").fetchone()["error"]


def test_ideas_copying_a_stored_caption_are_rejected(cfg, one):
    from reelengine import accounts as ac

    cap = "ثلاث أدوات تختصر عليك وقت المذاكرة وتنظم يومك كله بسهولة"
    ac.import_rows(one, cfg, [{"permalink": "https://www.instagram.com/reel/COPY1234/", "caption": cap}], default_username="comp_x")
    brain.set_provider(brain.FakeBrain({"occasion_ideas": lambda p, s: _ideas_answer(p, hook=cap)}))
    oc.remind(one, cfg, Notes(), today=at(14))
    assert one.execute("SELECT source FROM occasion_ideas").fetchone()["source"] == "template"


def test_brain_skipped_near_the_cap_or_when_off(make_cfg, cfg, conn):
    oc.add(conn, {"name_ar": "يوم", "name_en": "Cap Day", "rule": "once", "year": 2027, "month": 3, "day": 1, "category": "tech"})
    fb = brain.FakeBrain({"occasion_ideas": lambda p, s: _ideas_answer(p)})
    brain.set_provider(fb)
    left = cfg["brain"]["max_calls_per_day"] - cfg["calendar"]["brain_reserve_calls"] + 1
    conn.execute("INSERT INTO brain_calls(ts, task, model, attempts, ok) VALUES(?,?,?,?,1)", (db.now_iso(), "caption", "x", left))
    oc.remind(conn, cfg, Notes(), today=at(14))
    assert fb.calls == [] and conn.execute("SELECT source FROM occasion_ideas").fetchone()["source"] == "template"
    conn.execute("DELETE FROM brain_calls")
    oc.remind(conn, make_cfg({"calendar": {"remind_ideas": False}}), Notes(), today=at(7))
    assert fb.calls == []


def test_brain_budget_exceeded_falls_back(make_cfg, conn):
    c = make_cfg({"brain": {"max_calls_per_day": 1}, "calendar": {"brain_reserve_calls": 0}})
    oc.add(conn, {"name_ar": "يوم", "name_en": "Budget Day", "rule": "once", "year": 2027, "month": 3, "day": 1, "category": "career"})
    conn.execute("INSERT INTO brain_calls(ts, task, model, attempts, ok) VALUES(?,?,?,?,1)", (db.now_iso(), "caption", "x", 1))
    fb = brain.FakeBrain({"occasion_ideas": lambda p, s: _ideas_answer(p)})
    brain.set_provider(fb)
    n = Notes()
    assert oc.remind(conn, c, n, today=at(14)) == ["budget-day"] and fb.calls == []
    assert "(templates)" in n.sent[0]["body"]


def test_dry_run_sends_marks_caches_and_asks_nothing(cfg, one):
    fb = brain.FakeBrain({"occasion_ideas": lambda p, s: _ideas_answer(p)})
    brain.set_provider(fb)
    n, report = Notes(), []
    assert oc.remind(one, cfg, n, today=at(14), dry_run=True, report=report) == ["test-tech-day"]
    assert n.sent == [] and fb.calls == [] and report[0]["lead"] == 14 and report[0]["title"].startswith("Test Tech Day in 14")
    assert one.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'occasion_reminder:%'").fetchone()[0] == 0
    assert one.execute("SELECT COUNT(*) FROM occasion_ideas").fetchone()[0] == 0
    assert oc.remind(one, cfg, n, today=at(14)) == ["test-tech-day"]  # the real run still sends it


def test_upcoming_reminders_is_read_only(cfg, one):
    up = oc.upcoming_reminders(one, cfg, today=at(10))
    assert len(up) == 1 and up[0]["next_lead"] == 14 and up[0]["ideas_source"] == "template" and len(up[0]["ideas"]) == 3
    assert up[0]["checklist"] and one.execute("SELECT COUNT(*) FROM occasion_ideas").fetchone()[0] == 0
    oc.remind(one, cfg, Notes(), today=at(10))  # sends the 14-day one (due lead)
    assert oc.upcoming_reminders(one, cfg, today=at(10))[0]["next_lead"] == 7
    assert oc.upcoming_reminders(one, cfg, today=at(0))[0]["next_lead"] == 0
    assert oc.upcoming_reminders(one, cfg, today=at(15)) == []


# ------------------------------------------------------------------ templates + checklist
def test_templates_pass_the_gates_for_every_seed_occasion(cfg):
    brand = load_brand(cfg.brand_dir)
    seed = oc.load_seed(SEED)
    assert len(seed) >= 40
    for o in seed:
        got = ideas.template_ideas(o, 3)
        assert len(got) == 3, o["slug"]
        for i in got:
            assert ideas.idea_problems(i, cfg, brand) == [], (o["slug"], i)
            assert len(i["beats"]) == 3 and len(i["hook_line"].split()) <= ideas.MAX_HOOK_WORDS and 20 <= i["length_s"] <= 90
            assert o["name_ar"] in i["hook_line"] and (o["name_en"] in i["title"])


def test_every_category_has_a_short_checklist(cfg):
    rules = load_occasion_rules(cfg.brand_dir)
    for cat in oc.CATEGORIES:
        assert 2 <= len(rules["category_checklist"][cat]) <= 4, cat
    assert rules["occasion_checklist"] == {} or isinstance(rules["occasion_checklist"], dict)


def test_brand_pack_can_only_add_checklist_lines(tmp_path):
    own = tmp_path / "pack"
    own.mkdir()
    (own / "gates_occasions.yaml").write_text(yaml.safe_dump({
        "category_checklist": {"tech": ["My extra tech line."], "national": []},
        "occasion_checklist": {"national-day": ["Wear the thobe."]}}, allow_unicode=True), encoding="utf-8")
    r = load_occasion_rules(own)
    generic = load_occasion_rules(PACKAGE_ROOT / "brand")
    assert r["category_checklist"]["tech"] == generic["category_checklist"]["tech"] + ["My extra tech line."]
    assert r["category_checklist"]["national"] == generic["category_checklist"]["national"]  # can't drop lines
    assert ideas.checklist_for(type("B", (), {"occasion_rules": r})(), {"slug": "national-day", "category": "national"})[-1] == "Wear the thobe."


def test_prompt_marks_data_and_rules(cfg):
    p = (PACKAGE_ROOT / "prompts" / "occasion_ideas.md").read_text(encoding="utf-8")
    for must in ("DATA", "<data>", "no offers", "flag", "Own content only", "no @handles"):
        assert must.lower() in p.lower(), must


# ------------------------------------------------------------------ CLI
def test_cli_calendar_remind_dry_run_and_real(cfg, capsys, monkeypatch):
    monkeypatch.setattr("reelengine.cli.load_config", lambda path=None: cfg)
    c = db.connect(cfg.db_path)
    oc.ensure_seeded(c, cfg)
    d = oc.local_today(cfg) + timedelta(days=3)
    oc.add(c, {"name_ar": "يوم سي ال اي", "name_en": "CLI Day", "rule": "once", "year": d.year, "month": d.month, "day": d.day,
               "category": "tech"})
    c.close()
    assert cli_main(["calendar", "remind", "--dry-run", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert "cli-day" in [r["slug"] for r in rep] and next(r for r in rep if r["slug"] == "cli-day")["lead"] == 3
    c = db.connect(cfg.db_path)
    assert c.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'occasion_reminder:%'").fetchone()[0] == 0
    c.close()
    assert cli_main(["calendar", "remind"]) == 0
    out = capsys.readouterr().out
    assert "[sent] CLI Day in 3 days" in out
    assert cli_main(["calendar", "remind"]) == 0
    assert "no occasion reminder due today" in capsys.readouterr().out  # idempotent


# ------------------------------------------------------------------ upgrade from v1.1
def test_v11_reminder_covers_advance_leads_but_not_the_day_itself(cfg, one):
    """v1.1 sent one reminder 3-21 days ahead (meta occasion_reminded:<slug>:<start>). After the upgrade
    the same occurrence must not be announced again in advance; the day-of reminder still goes out."""
    slug = oc.all_occasions(one)[0]["slug"]
    db.meta_set(one, f"occasion_reminded:{slug}:{DAY.isoformat()}", "2026-10-14T00:00:00+00:00")
    n = Notes()
    for d in (14, 7, 3, 1):
        oc.remind(one, cfg, n, today=at(d))
    assert n.sent == []
    oc.remind(one, cfg, n, today=at(0))
    assert len(n.sent) == 1 and "today" in n.titles[0]


def test_without_a_v11_flag_the_advance_lead_is_sent(cfg, one):
    n = Notes()
    oc.remind(one, cfg, n, today=at(7))
    assert len(n.sent) == 1
