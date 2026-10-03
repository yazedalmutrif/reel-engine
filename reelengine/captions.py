"""Burned-in captions as an ASS subtitle file (rendered by libass + HarfBuzz + FriBidi).

Rules (spec §4.5):
- chunks of 1-4 words, each <= 22 characters, never crossing a sentence boundary;
- the active word is highlighted in the brand accent;
- Arabic RTL shaping by libass/HarfBuzz; mixed Arabic + English in the right order;
- inside the Reels safe zone (clear of the bottom ~20% and the right-side buttons).

RTL notes (verified visually, see docs/qa/captions/):
- libass only auto-detects the paragraph direction when the style's Encoding is -1
  (otherwise it forces LTR, which puts "CV" and punctuation on the wrong side).
- Auto-detection uses the first strong character, so a chunk that starts with a Latin
  word ("CV واضح") would be laid out LTR. Every line of an Arabic reel is therefore
  prefixed with U+200F RIGHT-TO-LEFT MARK to force an RTL paragraph.
- Colour overrides are placed between words (never inside one), so Arabic joining is
  never broken by a style run boundary.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Sequence

RLM = "\u200f"
LRM = "\u200e"

_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-\ufeff]")
_LATIN = re.compile(r"[A-Za-z]")
_SENTENCE_END = re.compile(r"[.!?؟…](?:[\"'»”)\]]*)$")
_TRAILING_DROP = re.compile(r"[.,،؛;]+$")
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF\U00002B00-\U00002BFF"
    "\U0000FE0F\U0000200D\U000E0020-\U000E007F]"
)
_ZW = re.compile(r"[\u200b\u200c\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


@dataclass
class Word:
    """A word on the OUTPUT timeline (seconds)."""

    text: str
    start: float
    end: float
    prob: float = 1.0
    break_before: bool = False  # a cut / new kept segment starts at this word


@dataclass
class Chunk:
    words: list[Word] = field(default_factory=list)

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end

    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class CaptionStyle:
    font: str = "IBM Plex Sans Arabic"
    font_size: int = 84
    text_color: str = "#FFFFFF"
    highlight_color: str = "#B570FF"
    outline_color: str = "#000000"
    outline: float = 5
    shadow: float = 2
    margin_bottom: int = 560
    margin_left: int = 100
    margin_right: int = 190
    hook_font_size: int = 92
    hook_margin_top: int = 330
    width: int = 1080
    height: int = 1920
    max_words: int = 4
    max_chars: int = 22
    progress_bar: bool = False

    @classmethod
    def from_config(cls, cfg, overrides: dict | None = None) -> "CaptionStyle":
        c = dict(cfg["captions"])
        c.pop("font_size", None)
        st = cls(
            font=cfg["captions"]["font"],
            font_size=cfg["captions"]["font_size"],
            text_color=c["text_color"],
            highlight_color=c["highlight_color"],
            outline_color=c["outline_color"],
            outline=c["outline"],
            shadow=c["shadow"],
            margin_bottom=c["margin_bottom"],
            margin_left=c["margin_left"],
            margin_right=c["margin_right"],
            hook_font_size=c["hook_font_size"],
            hook_margin_top=c["hook_margin_top"],
            width=cfg["render"]["width"],
            height=cfg["render"]["height"],
            max_words=c["max_words"],
            max_chars=c["max_chars"],
            progress_bar=cfg["edit"]["progress_bar"],
        )
        for k, v in (overrides or {}).items():
            if hasattr(st, k):
                setattr(st, k, type(getattr(st, k))(v))
        return st


# ------------------------------------------------------------------ text helpers
def is_rtl_text(text: str) -> bool:
    """True when the text contains more Arabic letters than Latin letters."""
    return len(_ARABIC.findall(text)) >= max(1, len(_LATIN.findall(text)))


def strip_emoji(text: str) -> str:
    return re.sub(r"\s{2,}", " ", _EMOJI.sub("", text)).strip()


def clean_word(text: str) -> str:
    """Display form of a transcript word: trimmed, NFC, no zero-width marks, no trailing
    period/comma (question/exclamation marks and colons are kept)."""
    t = unicodedata.normalize("NFC", _ZW.sub("", text)).strip()
    return _TRAILING_DROP.sub("", t)


def ends_sentence(raw_text: str) -> bool:
    return bool(_SENTENCE_END.search(raw_text.strip()))


_LATIN_CORE = re.compile(r"[.#@]?[A-Za-z][A-Za-z0-9.+#_\-/]*[A-Za-z0-9+#]|[.#@]?[A-Za-z]")


def isolate_latin(text: str) -> str:
    """Wrap each Latin run in LRM marks so neutral characters that belong to it (the dot
    in ".bat", the plus signs in "C++") stay on the correct side inside an RTL line.
    Trailing Arabic punctuation (؟ ، !) is left outside so it follows RTL order."""
    return _LATIN_CORE.sub(lambda m: LRM + m.group(0) + LRM, text)


def ass_escape(text: str) -> str:
    """Make arbitrary text safe inside an ASS Dialogue line.

    Braces would open override blocks and backslashes would form tags like \\N, so they
    are replaced by look-alike full-width characters; newlines become spaces.
    """
    t = text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    return re.sub(r"[\r\n]+", " ", t)


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    """#RRGGBB -> &HAABBGGRR (ASS colour, alpha 0 = opaque)."""
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper().replace("&H", "&H", 1)


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


# ------------------------------------------------------------------ chunking
_CONNECTORS = {
    "و", "أو", "او", "في", "عن", "على", "من", "إلى", "الى", "لين", "مع", "بس", "ولا", "اللي", "الي",
    "يعني", "عشان", "لأن", "لان", "إذا", "اذا", "لو", "ثم", "بعد", "قبل", "حتى", "كل", "هذا", "هذي",
    "and", "or", "the", "a", "an", "to", "of", "in", "on", "for", "with",
}


def _is_connector(text: str) -> bool:
    return clean_word(text).lower() in _CONNECTORS


def _is_latin(text: str) -> bool:
    return bool(_LATIN.search(text)) and not _ARABIC.search(text)


def _chunk_len(words: Sequence[Word]) -> int:
    return len(" ".join(w.text for w in words))


_DEF_PREFIX = re.compile(r"^(?:[وفبكل]?ال|لل)[ء-ي]{2,}")
_NUM = re.compile(r"^[\d٠-٩][\d٠-٩.,%+]*$")


def _key(text: str) -> str:
    from .gates import normalize  # local import: gates imports config only

    return re.sub(r"[^\w؀-ۿ+#.-]", "", normalize(clean_word(text)))


def keep_pairs(phrases: Iterable[str]) -> set[tuple[str, str]]:
    """Adjacent word pairs that must stay in the same chunk (from facts.yaml keep_together)."""
    pairs: set[tuple[str, str]] = set()
    for ph in phrases:
        toks = [_key(t) for t in ph.split()]
        pairs.update(zip(toks, toks[1:]))
    return pairs


def binds(a: str, b: str, pairs: set[tuple[str, str]] = frozenset()) -> bool:
    """True when a chunk must not end between words a and b: a listed phrase, a definite
    noun + definite adjective («المركز الثالث», «بالذكاء الاصطناعي»), a number and its
    unit («418 جهاز»), or two English words («AI Engineer»)."""
    ca, cb = clean_word(a), clean_word(b)
    if (_key(a), _key(b)) in pairs:
        return True
    if _DEF_PREFIX.match(ca) and cb.startswith("ال"):
        return True
    if _NUM.match(ca) and cb and not _NUM.match(cb):
        return True
    return _is_latin(ca) and _is_latin(cb)


def chunk_words(
    words: Sequence[Word],
    max_words: int = 4,
    max_chars: int = 22,
    raw_texts: Sequence[str] | None = None,
    max_gap: float = 0.6,
    keep_together: set[tuple[str, str]] = frozenset(),
) -> list[Chunk]:
    """Greedy chunking with sentence boundaries, then orphan re-balancing.

    `raw_texts` are the transcript tokens before clean_word() (used to see sentence
    punctuation that clean_word() may have removed); defaults to the word texts.
    A new chunk starts when: the limit of words/characters would be exceeded, the
    previous word ended a sentence, there is a pause > max_gap, or a cut
    (break_before) sits between the words.
    """
    raws = list(raw_texts) if raw_texts is not None else [w.text for w in words]
    sentences: list[list[Word]] = []
    cur: list[Word] = []
    for idx, w in enumerate(words):
        if cur:
            prev = words[idx - 1]
            boundary = ends_sentence(raws[idx - 1]) or (w.start - prev.end) > max_gap or w.break_before
            if boundary:
                sentences.append(cur)
                cur = []
        cur.append(w)
    if cur:
        sentences.append(cur)

    chunks: list[Chunk] = []
    for sent in sentences:
        groups: list[list[Word]] = []
        g: list[Word] = []
        for w in sent:
            if g and (len(g) >= max_words or _chunk_len([*g, w]) > max_chars):
                carry: list[Word] = []
                # don't end a chunk on a connector ("أو", "في"...) or inside a bound phrase
                while len(g) >= 2 and (
                    _is_connector(g[-1].text) or binds(g[-1].text, (carry[0] if carry else w).text, keep_together)
                ):
                    carry.insert(0, g.pop())
                    if _chunk_len([*carry, w]) > max_chars or len(carry) + 1 > max_words:
                        g.extend(carry)
                        carry = []
                        break
                groups.append(g)
                g = carry
            g.append(w)
        if g:
            groups.append(g)
        # re-balance a 1-word orphan at the end of a sentence
        if len(groups) >= 2 and len(groups[-1]) == 1 and len(groups[-2]) >= 3:
            for take in (1, 2):
                cand_prev, cand_last = groups[-2][:-take], [*groups[-2][-take:], *groups[-1]]
                if not cand_prev or _chunk_len(cand_last) > max_chars or len(cand_last) > max_words:
                    break
                if not _is_connector(cand_prev[-1].text) and not binds(cand_prev[-1].text, cand_last[0].text, keep_together):
                    groups[-2], groups[-1] = cand_prev, cand_last
                    break
        chunks.extend(Chunk(list(gr)) for gr in groups)
    return chunks


# ------------------------------------------------------------------ ASS building
def _header(st: CaptionStyle) -> str:
    white = ass_color(st.text_color)
    outline = ass_color(st.outline_color)
    shadow = ass_color("#000000", 0x80)
    box = ass_color("#141416", 0x30)
    clear = ass_color("#141416", 0xFF)  # BorderStyle 4: the box uses BackColour; no outline
    accent = ass_color(st.highlight_color)
    return "\n".join(
        [
            "[Script Info]",
            "; generated by Reel Engine (reelengine/captions.py)",
            "ScriptType: v4.00+",
            f"PlayResX: {st.width}",
            f"PlayResY: {st.height}",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding",
            # Encoding -1 = let libass detect the paragraph direction (needed for RTL)
            f"Style: Caption,{st.font},{st.font_size},{white},{accent},{outline},{shadow},-1,0,0,0,100,100,0,0,1,"
            f"{st.outline},{st.shadow},2,{st.margin_left},{st.margin_right},{st.margin_bottom},-1",
            f"Style: Hook,{st.font},{st.hook_font_size},{white},{accent},{clear},{box},-1,0,0,0,100,100,0,0,4,"
            f"30,0,8,{st.margin_left},{st.margin_right},{st.hook_margin_top},-1",
            f"Style: HookAccent,{st.font},{st.hook_font_size},{accent},{accent},{box},{shadow},-1,0,0,0,100,100,0,0,1,"
            f"0,0,8,{st.margin_left},{st.margin_right},{st.hook_margin_top},-1",
            f"Style: Bar,{st.font},10,{accent},{accent},{accent},{accent},0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,-1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
    )


def _line(words: Sequence[Word], active: int | None, st: CaptionStyle, rtl: bool) -> str:
    white = ass_color(st.text_color)
    accent = ass_color(st.highlight_color)
    parts: list[str] = []
    for i, w in enumerate(words):
        t = ass_escape(isolate_latin(w.text) if rtl else w.text)
        if i == active:
            parts.append(f"{{\\1c{accent}}}{t}{{\\1c{white}}}")
        else:
            parts.append(t)
    body = " ".join(parts)
    return (RLM + body) if rtl else body


def build_events(chunks: Sequence[Chunk], st: CaptionStyle, rtl: bool, linger: float = 0.15, join_gap: float = 0.35) -> list[str]:
    """One Dialogue line per (chunk, active word). Between words the previous word stays
    highlighted, so there is no flicker."""
    events: list[str] = []
    for ci, ch in enumerate(chunks):
        nxt = chunks[ci + 1].start if ci + 1 < len(chunks) else None
        end = ch.end + linger
        if nxt is not None:
            end = nxt if (nxt - ch.end) <= join_gap else min(end, nxt)
        for wi, w in enumerate(ch.words):
            s = ch.start if wi == 0 else w.start
            e = ch.words[wi + 1].start if wi + 1 < len(ch.words) else end
            if e - s < 0.02:
                continue
            events.append(
                f"Dialogue: 1,{ass_time(s)},{ass_time(e)},Caption,,0,0,0,,{{\\blur0.6}}{_line(ch.words, wi, st, rtl)}"
            )
    return events


def hook_events(hook_text: str, duration: float, st: CaptionStyle) -> list[str]:
    """Hook text card for the first ~2.2 s: large text on a dark box near the top."""
    text = strip_emoji(hook_text or "").strip()
    if not text or duration <= 0:
        return []
    rtl = is_rtl_text(text)
    body = (RLM + ass_escape(isolate_latin(text))) if rtl else ass_escape(text)
    fade = "{\\fad(120,180)}"
    return [f"Dialogue: 2,{ass_time(0)},{ass_time(duration)},Hook,,0,0,0,,{fade}{body}"]


def progress_events(total: float, st: CaptionStyle) -> list[str]:
    """A thin accent progress bar along the top edge (optional, config edit.progress_bar)."""
    if total <= 0:
        return []
    ms = int(total * 1000)
    y0, y1 = 0, 8
    draw = f"m 0 {y0} l {st.width} {y0} {st.width} {y1} 0 {y1}"
    tag = f"{{\\an7\\pos(0,0)\\p1\\clip(0,{y0},0,{y1})\\t(0,{ms},\\clip(0,{y0},{st.width},{y1}))}}"
    return [f"Dialogue: 0,{ass_time(0)},{ass_time(total)},Bar,,0,0,0,,{tag}{draw}{{\\p0}}"]


def build_ass(
    words: Sequence[Word],
    st: CaptionStyle,
    hook_text: str = "",
    hook_duration: float = 2.2,
    total_duration: float | None = None,
    raw_texts: Sequence[str] | None = None,
    keep_together: Iterable[str] = (),
) -> tuple[str, list[Chunk]]:
    """Return (ASS document, chunks). Words must be on the output timeline.
    `keep_together`: phrases that must never be split across chunks (facts.yaml)."""
    cleaned = [Word(clean_word(w.text), w.start, w.end, w.prob, w.break_before) for w in words]
    keep = [i for i, w in enumerate(cleaned) if w.text]
    cleaned = [cleaned[i] for i in keep]
    raws = [(raw_texts[i] if raw_texts is not None else words[i].text) for i in keep]
    chunks = chunk_words(cleaned, st.max_words, st.max_chars, raws, keep_together=keep_pairs(keep_together))
    all_text = " ".join(w.text for w in cleaned) + " " + (hook_text or "")
    rtl = is_rtl_text(all_text)
    lines = [_header(st)]
    if st.progress_bar and total_duration:
        lines += progress_events(total_duration, st)
    lines += hook_events(hook_text, hook_duration, st)
    lines += build_events(chunks, st, rtl)
    return "\n".join(lines) + "\n", chunks


def title_card_ass(title: str, st: CaptionStyle, duration: float = 1.0) -> str:
    """ASS for the thumbnail/cover title (drawn on the cover frame PNG)."""
    t = strip_emoji(title or "").strip()
    lines = [_header(st)]
    if t:
        rtl = is_rtl_text(t)
        body = (RLM + ass_escape(isolate_latin(t))) if rtl else ass_escape(t)
        # centred in the middle ~1:1 area so the 3:4 grid crop keeps it
        lines.append(
            f"Dialogue: 2,{ass_time(0)},{ass_time(duration)},Hook,,0,0,0,,{{\\an5\\pos({st.width // 2},{int(st.height * 0.42)})\\fs{st.hook_font_size + 8}}}{body}"
        )
    return "\n".join(lines) + "\n"


def words_from_iter(items: Iterable[dict]) -> list[Word]:
    return [Word(str(d["text"]), float(d["start"]), float(d["end"]), float(d.get("prob", 1.0)), bool(d.get("break_before", False))) for d in items]
