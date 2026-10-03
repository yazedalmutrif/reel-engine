"""Edit-plan maths (spec §4.4, §12): segments, tightening (0.45 s → 0.12 s, 80 ms pad),
time mapping and caption fixes."""
from __future__ import annotations

import pytest

from reelengine.edit_plan import build_phrases, is_filler, map_time, output_words, tighten, validate_plan


def W(i, s, e, text="كلمة", seg=0):
    return {"i": i, "start": s, "end": e, "text": text, "prob": 0.9, "seg": seg}


def test_phrases_split_on_pause_and_punctuation():
    ws = [W(0, 0.0, 0.4), W(1, 0.45, 0.8, "نهاية."), W(2, 0.85, 1.2), W(3, 2.0, 2.4)]
    ph = build_phrases(ws, 0.35)
    assert [p.word_ids for p in ph] == [[0, 1], [2], [3]]


def test_long_gap_compressed_to_target():
    ws = [W(0, 1.0, 1.5), W(1, 3.0, 3.5)]  # 1.5 s gap inside one kept segment
    ph = build_phrases(ws, gap_s=10)
    t = tighten(ws, ph, [0], [], gap_threshold=0.45, gap_target=0.12, pad=0.08)
    (a, b), (c, d) = t["intervals"]
    assert c - b == pytest.approx(3.0 - 1.5 - 0.12, abs=1e-3)  # removed exactly gap - 0.12
    assert b - 1.5 == pytest.approx(0.06) and 3.0 - c == pytest.approx(0.06)
    assert t["output_duration"] == pytest.approx((1.5 + 0.06 - 0.92) + (3.58 - 2.94), abs=1e-3)


def test_short_gap_kept():
    ws = [W(0, 1.0, 1.5), W(1, 1.9, 2.3)]  # 0.4 s < 0.45
    t = tighten(ws, build_phrases(ws, 10), [0], [])
    assert len(t["intervals"]) == 1


def test_removed_content_gets_padding_both_sides():
    ws = [W(0, 1.0, 1.5), W(1, 1.6, 2.0), W(2, 2.1, 2.5)]
    ph = build_phrases(ws, 10)
    t = tighten(ws, ph, [0], [1], pad=0.08)  # drop the middle word
    (a, b), (c, d) = t["intervals"]
    assert b == pytest.approx(1.58) and c == pytest.approx(2.02)
    assert t["cuts"] == 1


def test_dropped_segments_and_fillers():
    ws = [W(0, 0.5, 1.0, "طيب"), W(1, 2.0, 2.5, "أول", 1), W(2, 2.6, 3.0, "إمم", 1), W(3, 3.1, 3.5, "خطوة", 1)]
    ph = build_phrases(ws, 0.35)
    t = tighten(ws, ph, [p.id for p in ph if p.id != 0], [])
    assert 0 not in t["kept_word_ids"] and 2 not in t["kept_word_ids"]
    assert t["dropped_fillers"] == [2]
    assert is_filler("إممم") and is_filler("um") and not is_filler("يعني") and not is_filler("امس")


def test_map_time_and_output_words_with_fix():
    ws = [W(0, 1.0, 1.5, "سوسيفي"), W(1, 3.0, 3.5, "واضح")]
    ph = build_phrases(ws, 10)
    t = tighten(ws, ph, [0], [])
    assert map_time(1.0, t["intervals"]) == pytest.approx(0.08)
    assert map_time(2.2, t["intervals"]) is None  # inside the removed silence
    ow = output_words(ws, t["kept_word_ids"], t["intervals"], [{"word_id": 0, "text": "سوّ CV"}])
    assert [w["text"] for w in ow] == ["سوّ", "CV", "واضح"]
    assert ow[0]["start"] < ow[1]["start"] < ow[2]["start"]
    assert ow[2]["break_before"] is True  # a cut sits before the next interval


def test_validate_plan_catches_bad_ids_and_long_hook():
    ws = [W(0, 0, 0.5), W(1, 0.6, 1.0)]
    ph = build_phrases(ws, 10)
    pb = {"arms": {"hook_type": [{"id": "problem"}, {"id": "list_number"}]}}
    plan = {"keep_segments": [0, 9], "drop_word_ids": [7], "hook": {"on_screen": "واحد اثنين ثلاثة أربعة خمسة ستة سبعة ثمانية"},
            "title_card": "عنوان", "caption_fixes": [{"word_id": 5, "text": "x"}], "cover_word_id": None, "hook_type": "story"}
    errs = " | ".join(validate_plan(plan, ph, ws, pb))
    for frag in ("unknown ids", "drop_word_ids", "7 words", "caption_fixes", "hook_type"):
        assert frag in errs
