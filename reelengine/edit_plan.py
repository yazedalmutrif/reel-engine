"""Edit plan (spec §4.4): numbered transcript segments -> brain decides what to keep ->
deterministic tightening -> source intervals + output-timeline words.

Tightening (D-008 in docs/DECISIONS.md):
- words kept = words of kept segments, minus the brain's drop_word_ids, minus pure
  hesitation sounds (إمم / آآ / um / uh);
- a silence of more than gap_threshold_s (0.45 s) between two kept words is cut down to
  gap_target_s (0.12 s total, split evenly around the cut);
- wherever content was removed (a dropped segment or word), each side of the cut keeps
  pad_s (80 ms) of padding so word onsets/tails are never clipped;
- overlapping intervals are merged; every interval boundary is a cut (zoom alternates).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from .gates import normalize

_FILLER = re.compile(r"^(ا*م{2,}|ا+م+|ام+|اه+|ا{2,}|اا+ه*|ه+م+|um+|uh+|erm+|hmm+|ah+|eh+)$")
_PUNCT = re.compile(r"[\s.,،؛;:!?؟…\"'«»()\-]+")


def is_filler(text: str) -> bool:
    t = _PUNCT.sub("", normalize(text))
    return bool(t) and bool(_FILLER.match(t))


@dataclass
class Phrase:
    id: int
    word_ids: list[int]
    start: float
    end: float
    text: str


def build_phrases(words: Sequence[dict], gap_s: float = 0.35) -> list[Phrase]:
    """Split the transcript into numbered segments at pauses > gap_s, whisper segment
    changes, or sentence-final punctuation."""
    phrases: list[Phrase] = []
    cur: list[dict] = []

    def flush() -> None:
        if cur:
            phrases.append(Phrase(len(phrases), [w["i"] for w in cur], cur[0]["start"], cur[-1]["end"], " ".join(w["text"] for w in cur)))

    for w in words:
        if cur:
            prev = cur[-1]
            if w["start"] - prev["end"] > gap_s or w.get("seg") != prev.get("seg") or re.search(r"[.!?؟]$", prev["text"]):
                flush()
                cur = []
        cur.append(w)
    flush()
    return phrases


def phrases_for_prompt(phrases: Sequence[Phrase], words: Sequence[dict]) -> str:
    by_id = {w["i"]: w for w in words}
    lines = []
    for p in phrases:
        toks = " ".join(f"{by_id[i]['text']}⟨{i}⟩" for i in p.word_ids)
        lines.append(f"[S{p.id} {p.start:.2f}-{p.end:.2f}s] {toks}")
    return "\n".join(lines)


def validate_plan(plan: dict, phrases: Sequence[Phrase], words: Sequence[dict], playbook: dict) -> list[str]:
    """Extra checks the JSON schema can't express (the brain gets these back on retry)."""
    errs = []
    ids = {p.id for p in phrases}
    wids = {w["i"] for w in words}
    bad = [s for s in plan["keep_segments"] if s not in ids]
    if bad:
        errs.append(f"keep_segments has unknown ids {bad}")
    if [w for w in plan["drop_word_ids"] if w not in wids]:
        errs.append("drop_word_ids has unknown word ids")
    if len(plan["hook"]["on_screen"].split()) > 7:
        errs.append("hook.on_screen must be 7 words or fewer")
    if len(plan["title_card"].split()) > 7:
        errs.append("title_card must be 7 words or fewer")
    for fx in plan["caption_fixes"]:
        if fx["word_id"] not in wids:
            errs.append(f"caption_fixes word_id {fx['word_id']} unknown")
    if plan.get("cover_word_id") is not None and plan["cover_word_id"] not in wids:
        errs.append("cover_word_id unknown")
    arms = {a["id"] for a in playbook["arms"]["hook_type"]}
    if plan["hook_type"] not in arms:
        errs.append(f"hook_type must be one of {sorted(arms)}")
    return errs


def tighten(
    words: Sequence[dict],
    phrases: Sequence[Phrase],
    keep_segments: Sequence[int],
    drop_word_ids: Sequence[int],
    *,
    gap_threshold: float = 0.45,
    gap_target: float = 0.12,
    pad: float = 0.08,
    src_duration: float | None = None,
) -> dict[str, Any]:
    """Return {intervals:[[s,e],...], kept_word_ids:[...], output_duration, cuts, dropped_fillers}."""
    keep_set = set()
    for p in phrases:
        if p.id in set(keep_segments):
            keep_set.update(p.word_ids)
    drop = set(drop_word_ids)
    fillers = [w["i"] for w in words if w["i"] in keep_set and is_filler(w["text"])]
    kept = [w for w in words if w["i"] in keep_set and w["i"] not in drop and w["i"] not in set(fillers)]
    if not kept:
        return {"intervals": [], "kept_word_ids": [], "output_duration": 0.0, "cuts": 0, "dropped_fillers": fillers}

    end_limit = src_duration if src_duration else kept[-1]["end"] + pad
    intervals: list[list[float]] = []
    cur_s = max(0.0, kept[0]["start"] - pad)
    prev = kept[0]
    for w in kept[1:]:
        removed_between = w["i"] != prev["i"] + 1  # content (words) was removed here
        gap = w["start"] - prev["end"]
        if removed_between:
            intervals.append([cur_s, prev["end"] + pad])
            cur_s = max(0.0, w["start"] - pad)
        elif gap > gap_threshold:
            half = gap_target / 2
            intervals.append([cur_s, prev["end"] + half])
            cur_s = w["start"] - half
        prev = w
    intervals.append([cur_s, min(end_limit, prev["end"] + pad)])

    merged: list[list[float]] = []
    for s, e in intervals:
        s, e = round(max(0.0, s), 3), round(min(end_limit, e), 3)
        if e <= s:
            continue
        if merged and s <= merged[-1][1] + 1e-3:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    total = round(sum(e - s for s, e in merged), 3)
    return {
        "intervals": merged,
        "kept_word_ids": [w["i"] for w in kept],
        "output_duration": total,
        "cuts": max(0, len(merged) - 1),
        "dropped_fillers": fillers,
    }


def map_time(t: float, intervals: Sequence[Sequence[float]]) -> float | None:
    """Source time -> output time (None if t falls in a removed part)."""
    off = 0.0
    for s, e in intervals:
        if s - 1e-6 <= t <= e + 1e-6:
            return round(off + (t - s), 3)
        off += e - s
    return None


def output_words(words: Sequence[dict], kept_ids: Sequence[int], intervals: Sequence[Sequence[float]], fixes: Sequence[dict] = ()) -> list[dict]:
    """Kept words on the OUTPUT timeline, with caption fixes applied. A fix may replace
    one word with several (e.g. 'سوسيفي' -> 'سوّ CV'): time is split by length."""
    by_id = {w["i"]: w for w in words}
    fix = {f["word_id"]: f["text"].strip() for f in fixes if f.get("text", "").strip()}
    out: list[dict] = []
    prev_interval = None
    for i in kept_ids:
        w = by_id[i]
        s, e = map_time(w["start"], intervals), map_time(w["end"], intervals)
        if s is None or e is None:
            continue
        iv = next(k for k, (a, b) in enumerate(intervals) if a - 1e-6 <= w["start"] <= b + 1e-6)
        brk = prev_interval is not None and iv != prev_interval
        prev_interval = iv
        parts = fix.get(i, w["text"]).split()
        total_len = sum(len(p) for p in parts) or 1
        t = s
        for k, part in enumerate(parts):
            d = (e - s) * len(part) / total_len
            out.append({"i": i, "text": part, "raw": w["text"], "start": round(t, 3), "end": round(t + d, 3), "prob": w.get("prob", 1.0), "break_before": brk and k == 0})
            t += d
    return out


def words_per_minute(n_words: int, duration: float) -> float | None:
    return round(n_words / duration * 60, 1) if duration > 0 else None
