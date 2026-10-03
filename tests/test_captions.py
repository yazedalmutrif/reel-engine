"""Captions: chunking limits, sentence boundaries, keep-together phrases, ASS escaping,
RTL/LRM handling and the per-word highlight events (spec §4.5, §12)."""
from __future__ import annotations

import re

import pytest

from reelengine.captions import (
    LRM,
    RLM,
    CaptionStyle,
    Word,
    ass_color,
    ass_escape,
    ass_time,
    binds,
    build_ass,
    chunk_words,
    clean_word,
    is_rtl_text,
    isolate_latin,
    keep_pairs,
)


def words(sentence: str, t0: float = 0.0, d: float = 0.4, gap: float = 0.05) -> list[Word]:
    out, t = [], t0
    for tok in sentence.split():
        out.append(Word(tok, t, t + d))
        t += d + gap
    return out


SENTENCES = [
    "أول خطوة: سوّ CV واضح وحط رابط GitHub.",
    "استخدم ChatGPT أو Claude عشان تجهز للمقابلة؟",
    "حقق مشروعنا المركز الثالث في هاكاثون AgentX.",
    "غيرت شعار الإقلاع لـ418 جهاز بسكربت .bat واحد.",
    "AI Engineer ولا ML Engineer؟ ترى مو نفس الشي!",
]


@pytest.mark.parametrize("sentence", SENTENCES)
def test_chunk_limits(sentence):
    ws = words(sentence)
    chunks = chunk_words([Word(clean_word(w.text), w.start, w.end) for w in ws], 4, 22, [w.text for w in ws])
    assert chunks
    for c in chunks:
        assert 1 <= len(c.words) <= 4
        assert len(c.text()) <= 22, c.text()
    assert [w.text for c in chunks for w in c.words] == [clean_word(w.text) for w in ws]  # nothing lost or reordered


def test_never_crosses_sentence_boundary():
    ws = words("هذا أول سطر. وهذا سطر ثاني")
    chunks = chunk_words(ws, 4, 22)
    for c in chunks:
        joined = " ".join(w.text for w in c.words)
        assert not re.search(r"\.\s+\S", joined), joined


def test_breaks_at_long_pause_and_at_cuts():
    ws = words("واحد اثنين")
    ws[1].start += 1.0
    ws[1].end += 1.0
    assert len(chunk_words(ws, 4, 22)) == 2
    ws = words("واحد اثنين")
    ws[1].break_before = True
    assert len(chunk_words(ws, 4, 22)) == 2


def test_keep_together_and_definite_pairs():
    ws = words("حقق مشروعنا المركز الثالث في هاكاثون AgentX")
    chunks = chunk_words(ws, 4, 22, keep_together=keep_pairs(["المركز الثالث", "هاكاثون AgentX"]))
    texts = [c.text() for c in chunks]
    assert any("المركز الثالث" in t for t in texts), texts
    assert any("هاكاثون AgentX" in t for t in texts), texts
    assert binds("المركز", "الثالث")          # definite noun + adjective
    assert binds("بالذكاء", "الاصطناعي")
    assert binds("418", "جهاز")               # number + unit
    assert binds("ML", "Engineer")            # English run
    assert not binds("رابط", "GitHub")


def test_no_chunk_ends_on_connector_when_avoidable():
    ws = words("استخدم ChatGPT أو Claude عشان تجهز للمقابلة")
    for c in chunk_words(ws, 4, 22)[:-1]:
        assert clean_word(c.words[-1].text) not in ("أو", "في", "و"), c.text()


def test_ass_escape_blocks_override_injection():
    s = ass_escape("{\\pos(0,0)}hack\\N{\\c&H0000FF&}")
    assert "{" not in s and "}" not in s and "\\" not in s
    assert "\n" not in ass_escape("a\nb")


def test_colors_and_times():
    assert ass_color("#B570FF") == "&H00FF70B5"
    assert ass_color("#FFFFFF", 0x80) == "&H80FFFFFF"
    assert ass_time(0) == "0:00:00.00" and ass_time(61.234) == "0:01:01.23"


def test_rtl_detection_and_latin_isolation():
    assert is_rtl_text("سوّ CV واضح")
    assert not is_rtl_text("Hello world")
    iso = isolate_latin(".bat واحد؟")
    assert iso.startswith(LRM + ".bat" + LRM)
    assert isolate_latin("Engineer؟").endswith(LRM + "؟")  # Arabic punctuation stays outside


def test_build_ass_structure_and_highlight():
    ws = words("أول خطوة: سوّ CV واضح وحط رابط GitHub.")
    st = CaptionStyle()
    ass, chunks = build_ass(ws, st, hook_text="3 خطوات قبل ما تقدم", hook_duration=2.2, total_duration=10)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    styles = [ln for ln in ass.splitlines() if ln.startswith("Style: ")]
    assert all(ln.rstrip().endswith(",-1") for ln in styles), "Encoding -1 is required for RTL auto-direction"
    events = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 1,")]
    assert len(events) == sum(len(c.words) for c in chunks)  # one event per active word
    assert all(RLM in e for e in events)
    assert all(ass_color(st.highlight_color) in e for e in events)
    hook = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 2,")]
    assert hook and "0:00:02.20" in hook[0]


def test_safe_zone_margins_in_style():
    st = CaptionStyle()
    assert st.margin_bottom >= 0.2 * st.height  # clear of the bottom ~20% (IG UI)
    assert st.margin_right >= 150               # clear of the right-side buttons


def test_config_style_overrides():
    from reelengine.config import load_config

    st = CaptionStyle.from_config(load_config(), {"font_size": 110, "max_words": 3})
    assert st.font_size == 110 and st.max_words == 3
