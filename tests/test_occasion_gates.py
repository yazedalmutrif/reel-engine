"""Occasion rules in the legal gate (brand/gates_occasions.yaml): flag, Shahada, emblem, leaders, religion,
occasion + offer. Additive: every existing gates.yaml rule still holds, and a brand pack can only add occasion rules."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reelengine.config import load_config
from reelengine.gates import check_occasion_rules, load_brand, run_content_gates

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config().with_overrides({"paths": {"brand_dir": "brand.example"}})
BRAND = load_brand(ROOT / "brand.example")


def rules(text: str, field: str = "caption") -> dict[str, str]:
    return {h.rule: h.severity for h in run_content_gates({field: text}, None, CFG, BRAND) if h.rule.startswith("occasion.")}


HOLD = {
    "discount": ("خصم 30% بمناسبة اليوم الوطني على كل الدورات", "occasion.occ_commercial"),
    "en_offer": ("National Day offer: book now", "occasion.occ_commercial"),
    "flag_ad": ("العلم السعودي على عرض خاص للخدمة", "occasion.flag_commercial"),
    "flag_emoji_price": ("🇸🇦 احجز استشارتك بسعر خاص", "occasion.flag_emoji_commercial"),
    "shahada_promo": ("لا إله إلا الله محمد رسول الله على كوبون الخصم", "occasion.shahada_commercial"),
    "emblem": ("حطيت السيفين والنخلة في الثمبنيل", "occasion.emblem_any"),
    "leader_promo": ("ولي العهد قال كذا، اشترك بسعر خاص الحين", "occasion.leader_commercial"),
    "religion_meme": ("ميمز عن الصيام 😂", "occasion.religion_mock"),
    "flag_logo": ("حطيت شعاري فوق العلم", "occasion.flag_alter"),
    "giveaway": ("سحب على لابتوب بمناسبة العيد", "occasion.occ_giveaway"),
}
WARN = {
    "leader": ("كلمة ولي العهد عن الشباب", "occasion.leader_mention"),
    "identity": ("هوية اليوم الوطني هذي السنة: عزنا بطبعنا", "occasion.identity_logo"),
}
CLEAN = (
    "كل عام وأنتم بخير بمناسبة اليوم الوطني 🇸🇦",
    "تعلم بايثون في رمضان بخطة 30 يوم",
    "3 أدوات تساعدك تذاكر في رمضان",
    "Happy Founding Day! What our history teaches about building careers",
    "عيدكم مبارك",
    "كود بايثون بسيط تتعلمه في رمضان",
    "نصيحة للخريجين في اليوم الوطني: تعلم كتابة الكود",
)


@pytest.mark.parametrize("name", HOLD)
def test_occasion_holds(name):
    text, rule = HOLD[name]
    got = rules(text)
    assert got.get(rule) == "hard", (text, got)


@pytest.mark.parametrize("name", WARN)
def test_occasion_warnings_are_soft(name):
    text, rule = WARN[name]
    assert rules(text).get(rule) == "soft"


@pytest.mark.parametrize("text", CLEAN)
def test_respectful_greetings_pass(text):
    assert [r for r, s in rules(text).items() if s == "hard"] == []


def test_quran_warning_only_on_spoken_words():
    assert rules("بسم الله الرحمن الرحيم", field="captions").get("occasion.quran_audio") == "soft"
    assert "occasion.quran_audio" not in rules("بسم الله الرحمن الرحيم", field="caption")


def test_flag_emoji_rule_is_caption_only_and_anywhere():
    assert rules("🇸🇦 كل عام وأنتم بخير ... وفي آخر الكلام كود خصم")["occasion.flag_emoji_commercial"] == "hard"
    assert "occasion.flag_emoji_commercial" not in rules("🇸🇦 كود خصم", field="hook")


def test_existing_rules_still_fire_next_to_occasions():
    hits = {h.rule for h in run_content_gates({"caption": "منشن 3 من أصحابك بمناسبة اليوم الوطني"}, None, CFG, BRAND)}
    assert "engagement_bait" in hits


def test_brand_pack_can_add_but_not_drop_occasion_rules(tmp_path):
    own = tmp_path / "pack"
    own.mkdir()
    for f in (ROOT / "brand.example").iterdir():
        (own / f.name).write_bytes(f.read_bytes())
    (own / "gates_occasions.yaml").write_text(yaml.safe_dump({
        "groups": {"OCC": "nothing-matches-this", "MINE": "(كلمه خاصه)"},
        "rules": [{"id": "occ_commercial", "match": "MINE", "severity": "warn", "reason": "try to weaken"},
                  {"id": "pack_extra", "match": "MINE", "severity": "hold", "reason": "pack rule"}],
        "filming_checklist": ["Pack line"]}, allow_unicode=True), encoding="utf-8")
    b = load_brand(own)
    got = {h.rule: h.severity for h in check_occasion_rules({"caption": "خصم 30% بمناسبة اليوم الوطني كلمة خاصة"}, b.occasion_rules)}
    assert got["occasion.occ_commercial"] == "hard" and got["occasion.pack_extra"] == "hard"
    assert "Pack line" in b.occasion_rules["filming_checklist"] and len(b.occasion_rules["filming_checklist"]) >= 9
    # the base legal gate is independent of the occasion file
    assert {h.rule for h in run_content_gates({"caption": "استخدم كود خصم HUDA10"}, None, CFG, b)} >= {"ads_third_party"}


def test_every_rule_has_reason_and_known_severity():
    data = yaml.safe_load((ROOT / "brand" / "gates_occasions.yaml").read_text(encoding="utf-8"))
    ids = [r["id"] for r in data["rules"]]
    assert len(ids) == len(set(ids))
    for r in data["rules"]:
        assert r["severity"] in ("hold", "warn") and r["reason"]
        assert r["match"] in data["groups"] or r["match"].startswith("(") or r["match"] == "🇸🇦"
    assert len(data["filming_checklist"]) >= 8
