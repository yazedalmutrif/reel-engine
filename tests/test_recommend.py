"""v1.2 "Link account": niche, pseudonymised recommendation input, the brain task with its validation,
the deterministic template, fitting occasions, storage/forget/prune, DB v3 migration and the CLI."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from reelengine import accounts as ac, brain, db, occasions as oc
from reelengine.cli import main as cli_main
from reelengine.config import PACKAGE_ROOT

TODAY = date(2026, 10, 15)
NICHE = "AI tools and careers for Saudi students, like @other_creator does https://example.com/x"


@pytest.fixture(autouse=True)
def _fake_brain_only():
    brain.set_provider(brain.FakeBrain({}))
    yield
    brain.set_provider(None)


@pytest.fixture
def linked(cfg, conn):
    oc.ensure_seeded(conn, cfg)
    ac.attach(conn, "my_acc", "own", niche=NICHE)
    ac.attach(conn, "other_creator", "reference")
    return conn


def _good(prompt: str, schema: dict) -> dict:
    data = json.loads(prompt.rsplit("<data>", 1)[1].split("</data>", 1)[0])
    ids = [o["id"] for o in data["occasions"]]
    return {"ideas": [{"topic": f"Topic {k}", "hook_line": "ثلاث أدوات تختصر عليك وقت المذاكرة", "format": "talking head",
                       "length": "30-45 s", "best_time": "21:30 weekday", "why": "playbook R1"} for k in range(5)],
            "occasions": [{"slug": i, "angle": f"brain angle {i}"} for i in ids[:2]], "summary": "Based on the description."}


def _rendered_prompt(fb) -> str:
    return fb.calls[0][1]


# ------------------------------------------------------------------ niche
def test_niche_is_stored_validated_and_updated(conn):
    r = ac.attach(conn, "acc_one", "reference", niche="  careers for grads  ")
    assert r["niche"] == "careers for grads"
    assert ac.attach(conn, "acc_one", "reference")["niche"] == "careers for grads"      # None keeps it
    assert ac.attach(conn, "acc_one", "reference", niche="AI tools")["niche"] == "AI tools"  # re-attach updates
    with pytest.raises(ac.AccountError, match="niche: max 200"):
        ac.attach(conn, "acc_one", "reference", niche="x" * 201)
    with pytest.raises(ac.AccountError, match="control"):
        ac.attach(conn, "acc_one", "reference", niche="line one\nline two")
    with pytest.raises(ac.AccountError, match="control"):
        ac.attach(conn, "acc_two", "reference", niche="bell\x07")
    assert ac.get(conn, "acc_two") is None


# ------------------------------------------------------------------ privacy
def test_username_never_reaches_the_prompt_and_niche_is_pseudonymised(cfg, linked):
    fb = brain.FakeBrain({"recommend": _good})
    brain.set_provider(fb)
    out = ac.recommend(cfg, linked, "my_acc", today=TODAY)
    p = _rendered_prompt(fb)
    assert "my_acc" not in p and "@my_acc" not in p
    assert "other_creator" not in p and "example.com" not in p and "http" not in p
    acc = ac.get(linked, "my_acc")
    other = ac.get(linked, "other_creator")
    assert acc["pseudonym"] in p and other["pseudonym"] in p and "[link]" in p   # @mention -> its pseudonym, URL -> [link]
    assert "<data>" in p and "DATA" in p
    assert "NO reel numbers" in p and "based on the owner's description" in p
    assert out["source"] == "brain" and out["basis"] == "description" and out["note"] == ac.REC_NOTE_DESCRIPTION


def test_unknown_mentions_become_at_user(cfg, conn):
    ac.attach(conn, "solo", "own", niche="tips like @stranger_one shows")
    fb = brain.FakeBrain({"recommend": _good})
    brain.set_provider(fb)
    ac.recommend(cfg, conn, "solo", today=TODAY)
    p = _rendered_prompt(fb)
    assert "stranger_one" not in p and "@user" in p and "solo" not in p


def _numbers(cfg, conn, user="my_acc"):
    ac.import_rows(conn, cfg, [
        {"permalink": "https://www.instagram.com/reel/NUMS1111/", "posted_at": "2026-10-05 21:00", "views": "5000", "likes": "300",
         "duration_s": "40", "caption": "my own caption about tools"},
        {"permalink": "https://www.instagram.com/reel/NUMS2222/", "posted_at": "2026-10-07 21:00", "views": "8000", "likes": "500",
         "duration_s": "50"},
    ], default_username=user)


def test_with_numbers_the_prompt_uses_them(cfg, linked):
    _numbers(cfg, linked)
    fb = brain.FakeBrain({"recommend": _good})
    brain.set_provider(fb)
    out = ac.recommend(cfg, linked, "my_acc", today=TODAY)
    p = _rendered_prompt(fb)
    assert out["basis"] == "numbers" and out["note"] == ""
    assert "never invent" in p and "8000" in p and "21:00" in p and "NUMS" not in p


# ------------------------------------------------------------------ template
def test_template_is_deterministic_and_uses_slots_and_occasions(cfg, linked):
    a = ac.recommend(cfg, linked, "my_acc", use_brain=False, today=TODAY)
    b = ac.recommend(cfg, linked, "my_acc", use_brain=False, today=TODAY)
    assert a["ideas"] == b["ideas"] and a["source"] == "template" and len(a["ideas"]) == 5
    assert linked.execute("SELECT COUNT(*) FROM brain_calls").fetchone()[0] == 0
    assert all(ac._HHMM.search(i["best_time"]) for i in a["ideas"])
    pb_times = {s["time"] for s in json.loads(linked.execute("SELECT json FROM playbooks").fetchone()[0])["arms"]["slot"]}
    assert {i["best_time"].split()[0] for i in a["ideas"]} <= pb_times  # description basis: the playbook's slots
    assert "AI tools" in a["ideas"][0]["topic"]
    assert a["occasions"] and len(a["occasions"]) <= 4
    assert any(o["slug"] in ("fii10-2026", "black-hat-mea-2026", "gew-2026", "csedweek-2026") for o in a["occasions"])
    assert all(o["days_until"] >= 0 and o["name_ar"] for o in a["occasions"])
    assert a["note"] == ac.REC_NOTE_DESCRIPTION


def test_template_with_numbers_posts_at_the_accounts_top_hours(cfg, linked):
    _numbers(cfg, linked)
    r = ac.recommend(cfg, linked, "my_acc", use_brain=False, today=TODAY)
    assert r["basis"] == "numbers" and {i["best_time"] for i in r["ideas"]} == {"21:00"}
    assert r["ideas"][0]["length"].startswith("~45 s")


def test_fitting_occasions_by_niche(cfg, conn):
    oc.ensure_seeded(conn, cfg)
    tech = [x.occasion["slug"] for x in ac.fitting_occasions(conn, cfg, "AI and coding tutorials", TODAY)]
    career = [x.occasion["slug"] for x in ac.fitting_occasions(conn, cfg, "jobs and CV tips for students", TODAY)]
    assert tech and all(oc.get(conn, s)["category"] == "tech" for s in tech[:2])
    assert career and oc.get(conn, career[0])["category"] == "career"
    feb = [x.occasion["slug"] for x in ac.fitting_occasions(conn, cfg, "cooking", date(2027, 2, 1))]
    assert "founding-day" in feb  # every niche gets the national days
    assert len(ac.fitting_occasions(conn, cfg, "AI careers students coding", TODAY)) <= 4


# ------------------------------------------------------------------ brain validation
@pytest.mark.parametrize("mutate, why", [
    (lambda o: o["ideas"][0].update(hook_line="تابع @someone وتعرف"), "@handles"),
    (lambda o: o["ideas"][1].update(why="see https://x.example"), "links"),
    (lambda o: o["occasions"].append({"slug": "made-up-day", "angle": "x"}), "unknown occasion"),
    (lambda o: o["ideas"][2].update(hook_line="علق بكلمة نعم وأرسل لك الملف"), "breaks a rule"),
    (lambda o: o["ideas"][3].update(best_time="evenings"), "best_time"),
    (lambda o: o.update(summary="ask @someone"), "summary"),
])
def test_invalid_brain_answers_fall_back_to_the_template(cfg, linked, mutate, why):
    def bad(prompt, schema):
        out = _good(prompt, schema)
        mutate(out)
        return out

    brain.set_provider(brain.FakeBrain({"recommend": bad}))
    r = ac.recommend(cfg, linked, "my_acc", today=TODAY)
    assert r["source"] == "template"
    err = linked.execute("SELECT error FROM brain_calls WHERE task='recommend'").fetchone()["error"]
    assert why in err


def test_copied_caption_is_rejected(cfg, linked):
    cap = "ثلاث أدوات تختصر عليك وقت المذاكرة وتنظم يومك كله بسهولة تامة"
    ac.import_rows(linked, cfg, [{"permalink": "https://www.instagram.com/reel/COPY9999/", "caption": cap}], default_username="other_creator")

    def copy(prompt, schema):
        out = _good(prompt, schema)
        out["ideas"][0]["hook_line"] = cap
        return out

    brain.set_provider(brain.FakeBrain({"recommend": copy}))
    assert ac.recommend(cfg, linked, "my_acc", today=TODAY)["source"] == "template"


def test_brain_angles_are_kept_for_the_deterministic_occasions(cfg, linked):
    brain.set_provider(brain.FakeBrain({"recommend": _good}))
    r = ac.recommend(cfg, linked, "my_acc", today=TODAY)
    assert r["occasions"][0]["angle"].startswith("brain angle ")
    assert len(r["occasions"]) == len(ac.fitting_occasions(linked, cfg, NICHE, TODAY))  # the brain only adds angles


def test_cap_reached_uses_the_template(make_cfg, linked):
    c = make_cfg({"brain": {"max_calls_per_day": 1}})
    linked.execute("INSERT INTO brain_calls(ts, task, model, attempts, ok) VALUES(?,?,?,?,1)", (db.now_iso(), "caption", "x", 1))
    fb = brain.FakeBrain({"recommend": _good})
    brain.set_provider(fb)
    assert ac.recommend(c, linked, "my_acc", today=TODAY)["source"] == "template" and fb.calls == []


def test_niche_argument_updates_and_not_attached_is_refused(cfg, linked):
    ac.recommend(cfg, linked, "my_acc", niche="data science careers", use_brain=False, today=TODAY)
    assert ac.get(linked, "my_acc")["niche"] == "data science careers"
    assert ac.latest_recommendation(linked, "my_acc")["niche"] == "data science careers"
    with pytest.raises(ac.AccountError, match="not attached.*reel accounts attach"):
        ac.recommend(cfg, linked, "nobody_here", use_brain=False)
    assert ac.get(linked, "nobody_here") is None
    assert ac.latest_recommendation(linked, "other_creator") is None


# ------------------------------------------------------------------ storage, forget, prune, migration
def test_forget_and_prune_delete_recommendations(cfg, linked):
    ac.recommend(cfg, linked, "my_acc", use_brain=False, today=TODAY)
    ac.recommend(cfg, linked, "other_creator", niche="tips", use_brain=False, today=TODAY)
    assert linked.execute("SELECT COUNT(*) FROM account_recommendations").fetchone()[0] == 2
    assert ac.forget(linked, "other_creator")
    assert linked.execute("SELECT COUNT(*) FROM account_recommendations").fetchone()[0] == 1
    now = datetime.now(timezone.utc)
    linked.execute("UPDATE account_recommendations SET created_at=?", ((now - timedelta(days=181)).isoformat(timespec="seconds"),))
    assert ac.prune(linked, cfg, now)["recommendations_deleted"] == 1
    assert ac.latest_recommendation(linked, "my_acc") is None


def test_v2_db_file_migrates_to_v3(tmp_path, cfg):
    p = tmp_path / "v2.sqlite"
    c = sqlite3.connect(str(p))
    c.executescript(db.DDL)
    c.executescript(db.DDL_V2)
    c.execute("PRAGMA user_version=2")
    c.execute("INSERT INTO accounts(username, pseudonym, role, source, added_at) VALUES('old','ACC_01','reference','manual','2026-10-01')")
    c.commit()
    c.close()
    c = db.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 3
    assert "niche" in [r[1] for r in c.execute("PRAGMA table_info(accounts)")]
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"occasion_ideas", "account_recommendations"} <= tables
    assert ac.get(c, "old")["niche"] is None
    oc.ensure_seeded(c, cfg)
    assert ac.recommend(cfg, c, "old", niche="careers", use_brain=False)["source"] == "template"
    c.close()
    db.connect(p).close()  # idempotent


def test_half_migrated_v3_is_repaired(tmp_path):
    """Tables created but the niche column missing (a crash mid-way): the next connect finishes it."""
    p = tmp_path / "half3.sqlite"
    c = sqlite3.connect(str(p))
    c.executescript(db.DDL)
    c.executescript(db.DDL_V2)
    c.executescript(db.DDL_V3)
    c.execute("PRAGMA user_version=3")
    c.commit()
    c.close()
    c = db.connect(p)
    assert "niche" in [r[1] for r in c.execute("PRAGMA table_info(accounts)")]
    c.close()


def test_prompt_and_schema_files():
    p = (PACKAGE_ROOT / "prompts" / "recommend.md").read_text(encoding="utf-8")
    assert "DATA" in p and "never invent" in p and "No @handles" in p
    s = json.loads((PACKAGE_ROOT / "prompts" / "recommend.schema.json").read_text(encoding="utf-8"))
    assert s["properties"]["ideas"]["minItems"] == 5 and s["properties"]["ideas"]["maxItems"] == 7
    assert s["properties"]["occasions"]["maxItems"] == 4


# ------------------------------------------------------------------ CLI
def test_cli_recommend(cfg, capsys, monkeypatch):
    monkeypatch.setattr("reelengine.cli.load_config", lambda path=None: cfg)
    assert cli_main(["accounts", "recommend", "ghost_acc", "--no-brain"]) == 2
    assert "not attached" in capsys.readouterr().err
    c = db.connect(cfg.db_path)
    assert ac.get(c, "ghost_acc") is None
    c.close()
    assert cli_main(["accounts", "attach", "cli_acc", "--role", "own", "--niche", "AI tools for students"]) == 0
    capsys.readouterr()
    assert cli_main(["accounts", "recommend", "cli_acc", "--no-brain", "--json"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["source"] == "template" and len(r["ideas"]) == 5 and r["niche"] == "AI tools for students"
    assert cli_main(["accounts", "recommend", "cli_acc", "--no-brain", "--niche", "careers"]) == 0
    out = capsys.readouterr().out
    assert ac.REC_NOTE_DESCRIPTION in out and "source: template" in out
