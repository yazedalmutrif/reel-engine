"""Saudi occasions calendar: seed, Umm al-Qura dates, CRUD + tombstones, look-ahead, reminders,
the scheduler's occasion window, the brief integration and the `reel calendar` CLI."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone

import pytest
import yaml
from hijridate import Hijri

from reelengine import brain, db, occasions as oc, scheduler
from reelengine.cli import main as cli_main
from reelengine.config import PACKAGE_ROOT

SEED = PACKAGE_ROOT / "brand" / "occasions.yaml"


@pytest.fixture(autouse=True)
def _fake_brain_only():
    """v1.2 reminders can ask the brain for ideas: never let a test reach the real `claude` CLI."""
    brain.set_provider(brain.FakeBrain({}))  # any task -> BrainError -> template ideas
    yield
    brain.set_provider(None)


@pytest.fixture
def seeded(cfg, conn):
    assert oc.ensure_seeded(conn, cfg) > 0
    return conn


# ------------------------------------------------------------------ seed
def test_every_seed_day_has_a_source_and_check_date():
    items = yaml.safe_load(SEED.read_text(encoding="utf-8"))["occasions"]
    assert len(items) >= 15
    for o in items:
        assert o["source_url"].startswith("https://"), o["slug"]
        date.fromisoformat(str(o["checked_on"]))
        assert o["name_ar"] and o["name_en"]


def test_seed_has_the_required_days_with_the_right_status():
    by = {o["slug"]: o for o in oc.load_seed(SEED)}
    assert (by["founding-day"]["month"], by["founding-day"]["day"], by["founding-day"]["official"]) == (2, 22, True)
    assert (by["flag-day"]["month"], by["flag-day"]["day"], by["flag-day"]["official"]) == (3, 11, True)
    assert (by["national-day"]["month"], by["national-day"]["day"], by["national-day"]["official"]) == (9, 23, True)
    assert (by["teachers-day"]["month"], by["teachers-day"]["day"], by["teachers-day"]["official"]) == (10, 5, False)
    assert by["bosses-day"]["official"] is False and "unofficial" in by["bosses-day"]["name_en"].lower()
    for slug, (m, d) in {"ramadan-start": (9, 1), "eid-al-fitr": (10, 1), "eid-al-adha": (12, 10)}.items():
        assert by[slug]["rule"] == "hijri" and (by[slug]["month"], by[slug]["day"]) == (m, d)


def test_seed_is_idempotent_and_keeps_edits_and_deletions(cfg, seeded):
    conn = seeded
    oc.edit(conn, "teachers-day", {"angle": "my own angle"})
    assert oc.delete(conn, "bosses-day")
    assert oc.ensure_seeded(conn, cfg) == 0
    assert oc.get(conn, "teachers-day")["angle"] == "my own angle"
    assert oc.get(conn, "bosses-day") is None  # tombstone: not resurrected


def test_brand_pack_seed_wins(cfg, conn, tmp_path):
    own = tmp_path / "brand"
    own.mkdir()
    (own / "occasions.yaml").write_text(yaml.safe_dump({"occasions": [{
        "slug": "only-one", "name_ar": "يوم", "name_en": "Only one", "rule": "gregorian", "month": 1, "day": 2,
        "category": "career", "official": False, "source_url": "https://example.com/a", "checked_on": "2026-10-14"}]},
        allow_unicode=True), encoding="utf-8")
    c2 = cfg.with_overrides({"paths": {"brand_dir": str(own)}})
    assert oc.seed_path(c2) == own / "occasions.yaml"
    assert oc.ensure_seeded(conn, c2) == 1
    assert [o["slug"] for o in oc.all_occasions(conn)] == ["only-one"]


def test_calendar_disabled_seeds_nothing(make_cfg, conn):
    assert oc.ensure_seeded(conn, make_cfg({"calendar": {"enabled": False}})) == 0


# ------------------------------------------------------------------ dates
def test_hijri_dates_come_from_umm_al_qura(seeded):
    occ = {x.occasion["slug"]: x for x in oc.between(oc.all_occasions(seeded), date(2027, 1, 1), date(2027, 6, 30))}
    assert occ["ramadan-start"].start == date(2027, 2, 8) and occ["ramadan-start"].approximate
    assert occ["eid-al-fitr"].start == date(2027, 3, 9)
    assert occ["eid-al-adha"].start == date(2027, 5, 16)
    g = Hijri(1448, 9, 1).to_gregorian()
    assert occ["ramadan-start"].start == date(g.year, g.month, g.day)
    assert occ["ramadan-start"].hijri == "1 Ramadan 1448"
    assert not occ["founding-day"].approximate and occ["founding-day"].hijri == ""


def test_fixed_and_one_off_and_multi_day(seeded):
    occ = oc.between(oc.all_occasions(seeded), date(2026, 10, 1), date(2026, 12, 31), date(2026, 10, 1))
    by = {x.occasion["slug"]: x for x in occ}
    assert by["teachers-day"].start == date(2026, 10, 5)
    assert by["fii10-2026"].start == date(2026, 10, 26) and by["fii10-2026"].end == date(2026, 10, 29)
    assert "leap-2027" not in by
    # a multi-day event still shows when the window starts inside it
    mid = {x.occasion["slug"] for x in oc.between(oc.all_occasions(seeded), date(2026, 10, 28), date(2026, 10, 28))}
    assert "fii10-2026" in mid


def test_dates_outside_umm_al_qura_range_dont_crash(seeded):
    assert [x for x in oc.between(oc.all_occasions(seeded), date(2090, 1, 1), date(2090, 12, 31)) if x.occasion["rule"] == "hijri"] == []


def test_29_feb_falls_back_in_common_years(conn):
    oc.add(conn, {"name_ar": "اختبار", "name_en": "Leap test", "rule": "gregorian", "month": 2, "day": 29, "category": "tech"})
    got = [x.start for x in oc.between(oc.all_occasions(conn), date(2027, 1, 1), date(2028, 12, 31))]
    assert date(2027, 2, 28) in got and date(2028, 2, 29) in got


def test_upcoming_window_and_film_by(cfg, seeded):
    items = oc.upcoming(seeded, cfg, today=date(2027, 1, 25))  # 3..21 days ahead
    slugs = [x.occasion["slug"] for x in items]
    assert "ramadan-start" in slugs and "safer-internet-day-2027" in slugs and "founding-day" not in slugs  # 28 days out
    r = next(x for x in items if x.occasion["slug"] == "ramadan-start")
    assert r.days_until == 14 and r.film_by == date(2027, 2, 5)
    txt = oc.brief_text(items)
    assert "id=ramadan-start" in txt and "moon sighting" in txt and "not an official occasion" in txt
    assert "official Saudi occasion" in oc.brief_text(oc.upcoming(seeded, cfg, today=date(2027, 2, 10)))  # Founding Day


# ------------------------------------------------------------------ CRUD validation
@pytest.mark.parametrize("bad, msg", [
    ({"name_en": "x", "rule": "gregorian", "month": 1, "day": 1, "category": "tech"}, "name_ar"),
    ({"name_ar": "س", "name_en": "X day", "rule": "weekly", "month": 1, "day": 1, "category": "tech"}, "rule"),
    ({"name_ar": "س", "name_en": "X day", "rule": "gregorian", "month": 13, "day": 1, "category": "tech"}, "month"),
    ({"name_ar": "س", "name_en": "X day", "rule": "gregorian", "month": 2, "day": 30, "category": "tech"}, "day"),
    ({"name_ar": "س", "name_en": "X day", "rule": "hijri", "month": 9, "day": 31, "category": "religious"}, "Hijri day"),
    ({"name_ar": "س", "name_en": "X day", "rule": "once", "month": 2, "day": 1, "category": "tech"}, "year"),
    ({"name_ar": "س", "name_en": "X day", "rule": "gregorian", "month": 2, "day": 1, "category": "party"}, "category"),
    ({"name_ar": "س", "name_en": "X day", "rule": "gregorian", "month": 2, "day": 1, "category": "tech", "source_url": "javascript:alert(1)"}, "source_url"),
    ({"name_ar": "س", "name_en": "X day", "rule": "gregorian", "month": 2, "day": 1, "category": "tech", "checked_on": "yesterday"}, "checked_on"),
    ({"name_ar": "س" * 81, "name_en": "X day", "rule": "gregorian", "month": 2, "day": 1, "category": "tech"}, "too long"),
])
def test_invalid_occasions_are_refused(conn, bad, msg):
    with pytest.raises(oc.OccasionError, match=msg):
        oc.add(conn, bad)


def test_add_edit_delete_and_readd(conn):
    o = oc.add(conn, {"name_ar": "يوم الهاكاثون", "name_en": "Hackathon Day", "rule": "once", "year": 2027, "month": 3, "day": 1,
                      "category": "tech", "official": "no", "source_url": "https://example.com/h", "keywords": "hackathon, هاكاثون"})
    assert o["slug"] == "hackathon-day" and o["keywords"] == ["hackathon", "هاكاثون"] and o["official"] is False
    with pytest.raises(oc.OccasionError, match="already exists"):
        oc.add(conn, {"name_ar": "x", "name_en": "Hackathon Day", "rule": "gregorian", "month": 1, "day": 1, "category": "tech"})
    e = oc.edit(conn, "hackathon-day", {"rule": "gregorian", "month": 4, "day": 2})
    assert (e["rule"], e["month"], e["day"], e["year"]) == ("gregorian", 4, 2, None)
    assert oc.delete(conn, "hackathon-day") and not oc.delete(conn, "hackathon-day")
    assert oc.add(conn, {"name_ar": "x", "name_en": "Hackathon Day", "rule": "gregorian", "month": 1, "day": 1, "category": "tech"})


# ------------------------------------------------------------------ reminders, matching, scheduler window
def test_reminders_fire_once_per_occurrence(cfg, seeded):
    sent = []
    notify = lambda level, title, body="", rid=None: sent.append(title)  # noqa: E731
    first = oc.remind(seeded, cfg, notify, today=date(2027, 1, 25))
    again = oc.remind(seeded, cfg, notify, today=date(2027, 1, 26))
    assert "ramadan-start" in first and "ramadan-start" not in again
    assert any("Start of Ramadan in 14 days" in t for t in sent)


def test_match_text_uses_names_and_keywords(seeded):
    occs = oc.all_occasions(seeded)
    assert "national-day" in [o["slug"] for o in oc.match_text("نصائح لليوم الوطني للخريجين", occs)]
    assert [o["slug"] for o in oc.match_text("نصائح لليوم الوطني", occs)] == ["national-day"]
    assert "founding-day" in [o["slug"] for o in oc.match_text("What Founding Day teaches about careers", occs)]
    assert oc.match_text("3 أدوات ذكاء اصطناعي", occs) == []
    assert oc.match_text("take a leap of faith with your first job", occs) == []  # Latin keys match whole words only
    assert "black-hat-mea-2026" in [o["slug"] for o in oc.match_text("what I saw at Black Hat MEA in Riyadh", occs)]  # multi-word Latin key (v1.2: "black hat" alone no longer tags it)


def _qa_passed_reel(conn, topic: str) -> int:
    with db.tx(conn):
        rid = db.insert_reel(conn, "a" * 64, "x.mov", "")
    plan = {"topic": topic, "title_card": topic, "hook": {"on_screen": topic, "spoken": topic}, "hook_type": "problem", "pillar": "ai_tools"}
    for st in ("transcribed", "planned", "rendered", "qa_passed"):
        db.transition(conn, rid, st, "test")
    db.update_reel(conn, rid, plan_json=json.dumps(plan, ensure_ascii=False), duration_s=30.0)
    return rid


def test_scheduler_posts_an_occasion_reel_in_its_window(cfg, seeded):
    rid = _qa_passed_reel(seeded, "خطة اليوم الوطني")
    now = datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc)  # 9 days before National Day
    res = scheduler.schedule_reel(seeded, cfg, rid, now=now)
    local = res["when_local"].date()
    assert res.get("occasion") == "national-day"
    assert date(2026, 9, 21) <= local <= date(2026, 9, 23), local
    feats = json.loads(db.get_reel(seeded, rid)["features_json"])
    assert feats["occasion"] == "national-day"


def test_scheduler_ignores_occasions_for_other_reels(cfg, seeded):
    rid = _qa_passed_reel(seeded, "أدوات للمذاكرة")
    res = scheduler.schedule_reel(seeded, cfg, rid, now=datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc))
    assert "occasion" not in res


def test_month_grid_starts_on_sunday(seeded):
    occ = oc.between(oc.all_occasions(seeded), date(2026, 9, 1), date(2026, 9, 30), date(2026, 9, 10))
    g = oc.month_grid(2026, 9, occ, date(2026, 9, 10))
    assert g["weeks"][0][0]["date"].weekday() == 6  # Sunday
    cells = {c["date"]: c for wk in g["weeks"] for c in wk}
    assert [x.occasion["slug"] for x in cells[date(2026, 9, 23)]["items"]] == ["national-day"]
    assert cells[date(2026, 9, 10)]["today"]


# ------------------------------------------------------------------ brief
def _fake_brief(prompt: str, schema: dict) -> dict:
    slots = re.findall(r"hook_type=(\w+) · duration_band=([\d-]+)", prompt)
    scripts = []
    for i, (ht, band) in enumerate(slots):
        lo = int(band.split("-")[0])
        scripts.append({"title": f"Script {i}", "pillar": "ai_tools", "hook_type": ht, "duration_band": band, "target_length_s": lo,
                        "hook_line": "أداة تختصر عليك ساعة", "on_screen": "أداة تختصر ساعة", "beats": ["واحد", "اثنين", "ثلاثة"],
                        "cta": "", "cta_type": "none", "shot_note": "وجهك في النص", "experiment": "baseline", "sources": [],
                        "occasion": "ramadan-start" if i == 0 else ""})
    return {"scripts": scripts, "notes": "ok"}


def test_brief_gets_upcoming_occasions_and_checklist(cfg, conn, monkeypatch):
    from reelengine import brief as bm
    from reelengine.pipeline import Ctx

    oc.ensure_seeded(conn, cfg)
    monkeypatch.setattr(oc, "local_today", lambda cfg, now=None: date(2027, 1, 25))
    fb = brain.FakeBrain({"brief": _fake_brief})
    brain.set_provider(fb)
    try:
        b = bm.make_brief(Ctx.create(cfg, conn, toast=False))
    finally:
        brain.set_provider(None)
    prompt = fb.calls[0][1]
    assert "id=ramadan-start" in prompt and "Occasion filming checklist" in prompt and "never pair an occasion with an offer" in prompt
    assert b["scripts"][0]["occasion"] == "ramadan-start"
    assert "ramadan-start" in [o["slug"] for o in b["occasions"]]
    assert b["occasion_checklist"] and any("flag" in c.lower() for c in b["occasion_checklist"])
    md = bm.brief_markdown(b)
    assert "Saudi occasions calendar" in md and "film by" in md and "- [ ]" in md


def test_brief_rejects_an_unknown_occasion_id(cfg, conn, monkeypatch):
    from reelengine import brief as bm
    from reelengine.pipeline import Ctx

    oc.ensure_seeded(conn, cfg)
    monkeypatch.setattr(oc, "local_today", lambda cfg, now=None: date(2027, 1, 25))

    def bad(prompt, schema):
        out = _fake_brief(prompt, schema)
        out["scripts"][0]["occasion"] = "made-up-day"
        return out

    brain.set_provider(brain.FakeBrain({"brief": bad}))
    try:
        with pytest.raises(brain.BrainError, match="occasion must be one of"):
            bm.make_brief(Ctx.create(cfg, conn, toast=False))
    finally:
        brain.set_provider(None)


# ------------------------------------------------------------------ CLI
def test_cli_calendar_round_trip(cfg, capsys, monkeypatch):
    monkeypatch.setenv("REELENGINE_CONFIG", str(cfg.source))
    monkeypatch.setattr("reelengine.cli.load_config", lambda path=None: cfg)
    assert cli_main(["calendar", "seed"]) == 0
    assert cli_main(["calendar", "add", "--name-ar", "يوم المبرمج السعودي", "--name-en", "Test Coders Day", "--rule", "gregorian",
                     "--month", "11", "--day", "3", "--category", "tech", "--official", "no", "--source-url", "https://example.com/c"]) == 0
    assert cli_main(["calendar", "edit", "--slug", "test-coders-day", "--day", "4"]) == 0
    capsys.readouterr()
    assert cli_main(["calendar", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert any(r["slug"] == "test-coders-day" and r["day"] == 4 for r in rows)
    assert cli_main(["calendar", "add", "--name-ar", "x", "--name-en", "Bad", "--rule", "once", "--month", "2", "--day", "30",
                     "--year", "2027", "--category", "tech"]) == 2
    assert cli_main(["calendar", "delete", "--slug", "test-coders-day"]) == 0
    assert cli_main(["calendar", "upcoming", "--days", "400", "--json"]) == 0
    assert json.loads(capsys.readouterr().out.split("\n", 1)[1])  # first line is the delete message


# ------------------------------------------------------------------ v1.2 re-seed refresh
def _seed_file(tmp_path, angle_suffix: str = "", extra: list | None = None) -> "Path":
    items = []
    for slug, cat in (("alpha-day", "tech"), ("beta-day", "career"), ("gamma-day", "international")):
        items.append({"slug": slug, "name_ar": "يوم", "name_en": slug.replace("-", " ").title(), "rule": "gregorian", "month": 5,
                      "day": 1, "category": cat, "official": False, "angle": f"angle {slug}{angle_suffix}",
                      "source_url": "https://example.com/s", "checked_on": "2026-10-14", "keywords": [slug]})
    p = tmp_path / f"seed{angle_suffix or '0'}.yaml"
    p.write_text(yaml.safe_dump({"occasions": items + (extra or [])}, allow_unicode=True), encoding="utf-8")
    return p


def test_reseed_refreshes_only_unedited_seed_rows(cfg, conn, tmp_path):
    assert oc.seed(conn, cfg, _seed_file(tmp_path)) == {"added": 3, "refreshed": 0}
    before = {r["slug"]: dict(r) for r in conn.execute("SELECT * FROM occasions")}
    oc.edit(conn, "beta-day", {"notes": "my note"})        # edited by Yazeed
    assert oc.delete(conn, "gamma-day")                    # tombstone
    new = _seed_file(tmp_path, " v2", extra=[{"slug": "delta-day", "name_ar": "يوم", "name_en": "Delta Day", "rule": "gregorian",
                                              "month": 6, "day": 2, "category": "tech", "official": False}])
    assert oc.seed(conn, cfg, new) == {"added": 1, "refreshed": 1}
    a = oc.get(conn, "alpha-day")
    assert a["angle"] == "angle alpha-day v2"
    assert (a["created_at"], a["updated_at"]) == (before["alpha-day"]["created_at"], before["alpha-day"]["updated_at"])  # still "unedited"
    b = oc.get(conn, "beta-day")
    assert b["angle"] == "angle beta-day" and b["notes"] == "my note"       # edited row untouched
    assert oc.get(conn, "gamma-day") is None                                # tombstone stays deleted ...
    g = conn.execute("SELECT angle, deleted FROM occasions WHERE slug='gamma-day'").fetchone()
    assert g["deleted"] == 1 and g["angle"] == "angle gamma-day"            # ... and unchanged
    assert oc.seed(conn, cfg, new) == {"added": 0, "refreshed": 0}         # nothing changed: no-op
    assert oc.seed(conn, cfg, _seed_file(tmp_path)) == {"added": 0, "refreshed": 1}  # still refreshable (back to v1)
    assert oc.get(conn, "alpha-day")["angle"] == "angle alpha-day"
    assert oc.ensure_seeded(conn, cfg, new) == 0  # the old API still returns the number ADDED


def test_an_edit_in_the_same_second_still_counts_as_edited(cfg, conn, tmp_path, monkeypatch):
    from datetime import datetime, timezone

    fixed = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(db, "utcnow", lambda: fixed)
    oc.seed(conn, cfg, _seed_file(tmp_path))
    oc.edit(conn, "alpha-day", {"angle": "mine"})
    r = oc.get(conn, "alpha-day")
    assert r["updated_at"] != r["created_at"]
    assert oc.seed(conn, cfg, _seed_file(tmp_path, " v2"))["refreshed"] == 2  # beta + gamma, not alpha
    assert oc.get(conn, "alpha-day")["angle"] == "mine"


# ------------------------------------------------------------------ v1.2 fact-check: word-boundary matching
@pytest.mark.parametrize("text", ["اليوم العلمي في الجامعة", "مهارات الشباب اللي يحتاجها السوق", "black hat seo tricks"])
def test_common_phrases_do_not_tag_a_day(text):
    seed = oc.load_seed(SEED)
    assert not [o["slug"] for o in oc.match_text(text, seed) if o["slug"] in ("flag-day", "youth-skills-day", "black-hat-mea-2026")]


@pytest.mark.parametrize("text,slug", [("بمناسبة يوم العلم السعودي", "flag-day"), ("وباليوم الوطني نفخر", "national-day"),
                                       ("ليوم التأسيس قصة", "founding-day"), ("Black Hat MEA in Riyadh", "black-hat-mea-2026")])
def test_days_still_match_with_prefixes(text, slug):
    assert slug in [o["slug"] for o in oc.match_text(text, oc.load_seed(SEED))]
