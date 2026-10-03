"""Facts gate + legal gate + caption limits, against the FICTIONAL example brand pack in brand.example/.

The same gate engine runs on the owner's private brand pack (its own trap rules); this file checks the engine with
the example pack, so it runs in any clone.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reelengine.config import ConfigError, load_config
from reelengine.gates import check_caption_limits, hashtags_of, load_brand, normalize, run_content_gates

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config().with_overrides({"paths": {"brand_dir": "brand.example"}})
BRAND = load_brand(ROOT / "brand.example")


def hard(text: str, field: str = "caption", cfg=CFG) -> list[str]:
    return [h.rule for h in run_content_gates({field: text}, None, cfg, BRAND) if h.severity == "hard"]


PASS = {
    "mixed_tip": "أول خطوة: سوّ CV واضح وحط رابط GitHub",
    "second_place": "حصل مشروعنا كيو وايز على المركز الثاني في هاكاثون المثال",
    "first_step": "أول شي تسويه تفتح LinkedIn وتحدث بروفايلك",
    "topic_plan": "كان عندنا خطة من 3 أشهر للمواضيع في التدريب",
    "internship_dates": "تدربت في شركة المثال للخدمات اللوجستية من 1 يونيو إلى 31 يوليو",
    "ocr_ok": "Receipt Reader يقرأ الفواتير العربية بالـOCR ويطلعها في Excel",
    "title_ok": "شكرًا لسعادة الأستاذ عبدالله المثالي على دعمه",
}
HOLD = {
    "winner": ("كيو وايز هو الفائز في هاكاثون المثال", "hackathon_winner"),
    "first_place": ("جبنا المركز الأول في الهاكاثون", "hackathon_winner"),
    "english_winner": ("QueueWise was the winner of the hackathon", "hackathon_winner"),
    "placeholder_count": ("حليت ~200 تذكرة في التدريب", "placeholder_ticket_count"),
    "three_months_ar": ("التدريب كان ثلاثة أشهر", "internship_three_months"),
    "three_months_en": ("my internship lasted three months", "internship_three_months"),
    "surname": ("I'm Huda Al-Example", "surname_spelling"),
    "accuracy": ("دقة Receipt Reader وصلت 95% على الفواتير", "receipt_accuracy"),
    "title_missing": ("شكرًا لعبدالله المثالي على دعمه", "example_official_title"),
    "bait_yes": ("اكتب نعم في التعليقات إذا تبي الجزء الثاني", "engagement_bait"),
    "bait_tag": ("منشن 3 من أصحابك", "engagement_bait"),
    "bait_send10": ("أرسله لـ10 من أصحابك", "engagement_bait"),
    "ad_code": ("استخدم كود خصم HUDA10", "ads_third_party"),
    "sponsored": ("هذا المقطع برعاية شركة", "ads_third_party"),
    "guarantee": ("بهالطريقة وظيفة مضمونة لك", "guarantees"),
    "job_in_days": ("تتوظف خلال 30 يوم", "guarantees"),
    "salary": ("راتب الـ AI Engineer يوصل 15 ألف", "salary_as_fact"),
    "fear": ("الذكاء الاصطناعي بياخذ وظيفتك", "fear_claims"),
    "service_cta": ("تبي خارطة وظائف مبنية على CVك؟ التفاصيل في الرابط بالبايو", "service_cta"),
    "repost": ("مقطع منقول من حساب ثاني", "own_footage"),
}


@pytest.mark.parametrize("name", PASS)
def test_clean_texts_pass(name):
    assert hard(PASS[name]) == [], PASS[name]


@pytest.mark.parametrize("name", HOLD)
def test_traps_are_held(name):
    text, rule = HOLD[name]
    rules = hard(text)
    assert rule in rules, (text, rules)


def test_gate_runs_on_every_field():
    for field in ("captions", "hook", "title", "caption"):
        assert "hackathon_winner" in hard("كيو وايز الفائز في الهاكاثون", field)


def test_service_cta_allowed_only_when_enabled():
    text = HOLD["service_cta"][0]
    on = CFG.with_overrides({"cta": {"service_enabled": True, "allowed": ["save", "service"]}})
    assert "service_cta" in hard(text)
    assert "service_cta" not in hard(text, cfg=on)


@pytest.mark.parametrize("text", ["كلمني بالخاص وأراجع لك سيرتك", "أراجع لك السيرة الذاتية", "DM me for a CV review"])
def test_dm_style_service_offers_blocked(text):
    assert "service_cta" in hard(text)


def test_normalize():
    assert normalize("أَحْمَد") == "احمد"
    assert normalize("١٥٠") == "150"
    assert normalize("مدرسة") == "مدرسه"
    assert normalize("‏GitHub") == "github"


def cap(text: str, cta: str = "save") -> dict:
    return {"caption": text, "first_line": text.split("\n")[0], "cta_type": cta}


def test_hashtag_hard_cap_is_5():
    six = "نص\n\n#a1 #b2 #c3 #d4 #e5 #f6"
    assert "hashtag_count" in [h.rule for h in check_caption_limits(cap(six), CFG, BRAND)]
    assert hashtags_of("#وظائف #AI x#no") == ["#وظائف", "#AI"]


def test_caption_length_and_cta_rules():
    assert "caption_length" in [h.rule for h in check_caption_limits(cap("ا" * 2201), CFG, BRAND)]
    assert "cta_type" in [h.rule for h in check_caption_limits(cap("نص", "service"), CFG, BRAND)]
    assert [h.rule for h in check_caption_limits(cap("نص قصير\n\n#وظائف #AI #خريجين"), CFG, BRAND) if h.severity == "hard"] == []


def test_config_cannot_raise_hashtag_cap():
    with pytest.raises(ConfigError):
        CFG.with_overrides({"qa": {"hashtags_max": 6}})
