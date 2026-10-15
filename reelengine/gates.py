"""Deterministic content gates: the facts gate (spec §2.6) and the legal/brand gate (§2.7).

These run on every text a viewer will see: the burned-in captions (the kept spoken
words), the hook card, the title card, and the Instagram caption with hashtags. Any
hard hit holds the reel. The brain critique in qa.py is an *additional* check; it
can never override a hard hit here, and the learner cannot edit these rules.

Rule files:
  brand/facts.yaml      verified facts + forbidden trap patterns (Accuracy Audit)
  brand/gates.yaml      engagement bait, guarantees, salary, fear, ads, reposts, service CTA
  brand/never_say.yaml  literal phrases from the brand copy's never-say list (generated)
  brand/gates_occasions.yaml  national/religious occasion rules (flag, Shahada, emblem, leaders,
                        religion, occasion + offer). Structured: match / near / window / severity.
                        The generic file always applies; a brand pack's copy can only add rules.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml

from .config import HARD_CAPTION_MAX, HARD_HASHTAG_CAP

_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")  # harakat, shadda, tatweel
_BIDI = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_LETTERS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})
_HASHTAG = re.compile(r"(?<![\w&])#[\w؀-ۿ_]+")
_WORD = re.compile(r"\S+")
_EMOJI_ANY = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF]")


def normalize(text: str, lower: bool = True) -> str:
    """Normalisation used on both texts and patterns so matching is spelling-tolerant."""
    t = unicodedata.normalize("NFKC", text or "")
    t = _BIDI.sub("", t)
    t = _DIACRITICS.sub("", t)
    t = t.translate(_AR_DIGITS).translate(_LETTERS)
    t = t.replace("ـ", "")
    t = re.sub(r"[ \t]+", " ", t)
    return t.lower() if lower else t


def normalize_pattern(pat: str) -> str:
    # letters only; never lowercase a regex (\S, \D, \W, \B would change meaning)
    return normalize(pat, lower=False)


@dataclass
class Hit:
    gate: str          # facts | legal | limits
    rule: str
    severity: str      # hard | soft
    reason: str
    field: str
    match: str

    def as_dict(self) -> dict:
        return asdict(self)

    def short(self) -> str:
        return f"{self.gate}:{self.rule} in {self.field}: '{self.match}' ({self.reason})"


@dataclass
class Brand:
    facts: dict
    gates: dict
    never_say: dict
    captions: dict
    hooks: dict
    voice: str
    styles: dict
    occasion_rules: dict = None  # type: ignore[assignment]


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


GENERIC_BRAND = Path(__file__).resolve().parents[1] / "brand"


def occasion_rules_path(brand_dir: Path) -> Path:
    own = Path(brand_dir) / "gates_occasions.yaml"
    return own if own.exists() else GENERIC_BRAND / "gates_occasions.yaml"


def load_occasion_rules(brand_dir: Path) -> dict:
    """The generic rules ALWAYS apply; a brand pack's gates_occasions.yaml can only ADD groups (new names),
    rules (new ids) and checklist lines, never replace or drop the generic ones."""
    base = GENERIC_BRAND / "gates_occasions.yaml"
    out = _yaml(base) if base.exists() else {}
    out = {"groups": dict(out.get("groups") or {}), "rules": list(out.get("rules") or []),
           "filming_checklist": list(out.get("filming_checklist") or []),
           "category_checklist": {str(k): list(v or []) for k, v in (out.get("category_checklist") or {}).items()},
           "occasion_checklist": {str(k): list(v or []) for k, v in (out.get("occasion_checklist") or {}).items()}}
    own = Path(brand_dir) / "gates_occasions.yaml"
    if own.exists() and own.resolve() != base.resolve():
        extra = _yaml(own)
        for k, v in (extra.get("groups") or {}).items():
            out["groups"].setdefault(k, v)
        ids = {r["id"] for r in out["rules"]}
        for r in extra.get("rules") or []:
            if not (isinstance(r, dict) and r.get("id") and r.get("match") and r.get("reason")):
                raise ValueError(f"{own}: every occasion rule needs id, match and reason")
            if r["id"] not in ids:
                out["rules"].append(r)
        out["filming_checklist"] += [c for c in (extra.get("filming_checklist") or []) if c not in out["filming_checklist"]]
        for key in ("category_checklist", "occasion_checklist"):  # v1.2: per-category / per-slug lines, add-only
            for k, lines in (extra.get(key) or {}).items():
                have = out[key].setdefault(str(k), [])
                have += [c for c in (lines or []) if isinstance(c, str) and c not in have]
    return out


@lru_cache(maxsize=4)
def _load_brand_cached(brand_dir: str, stamp: tuple) -> Brand:
    d = Path(brand_dir)
    return Brand(
        facts=_yaml(d / "facts.yaml"),
        gates=_yaml(d / "gates.yaml"),
        never_say=_yaml(d / "never_say.yaml") if (d / "never_say.yaml").exists() else {},
        captions=_yaml(d / "captions.yaml") if (d / "captions.yaml").exists() else {},
        hooks=_yaml(d / "hooks.yaml") if (d / "hooks.yaml").exists() else {},
        voice=(d / "voice.md").read_text(encoding="utf-8") if (d / "voice.md").exists() else "",
        styles=_yaml(d / "styles.yaml") if (d / "styles.yaml").exists() else {},
        occasion_rules=load_occasion_rules(d),
    )


def load_brand(brand_dir: Path) -> Brand:
    files = ("facts.yaml", "gates.yaml", "never_say.yaml", "captions.yaml", "hooks.yaml", "voice.md", "styles.yaml")
    stamp = tuple((f, (brand_dir / f).stat().st_mtime if (brand_dir / f).exists() else 0) for f in files)
    for orp in (GENERIC_BRAND / "gates_occasions.yaml", brand_dir / "gates_occasions.yaml"):
        stamp += ((str(orp), orp.stat().st_mtime if orp.exists() else 0),)
    return _load_brand_cached(str(brand_dir), stamp)


# ------------------------------------------------------------------ matching helpers
@lru_cache(maxsize=2048)
def _rx(pat: str) -> re.Pattern[str]:
    return re.compile(normalize_pattern(pat), re.IGNORECASE)


def _find(pats: Iterable[str], text: str) -> list[re.Match[str]]:
    out = []
    for p in pats:
        out.extend(_rx(p).finditer(text))
    return out


def _word_index(text: str, pos: int) -> int:
    return len(_WORD.findall(text[:pos]))


def _near(text: str, m: re.Match[str], pats: Iterable[str], window: int) -> bool:
    wi = _word_index(text, m.start())
    for c in _find(pats, text):
        if abs(_word_index(text, c.start()) - wi) <= window:
            return True
    return False


def check_facts(texts: dict[str, str], facts: dict) -> list[Hit]:
    """Forbidden trap patterns from facts.yaml (all hard)."""
    hits: list[Hit] = []
    for field, raw in texts.items():
        text = normalize(raw)
        if not text.strip():
            continue
        for rule in facts.get("forbidden", []):
            ctx = rule.get("context") or []
            if ctx and not _find(ctx, text):
                continue
            for m in _find(rule["patterns"], text):
                if rule.get("scope") == "window" and ctx and not _near(text, m, ctx, int(rule.get("window", 4))):
                    continue
                if rule.get("except_near") and _near(text, m, rule["except_near"], 4):
                    continue
                hits.append(Hit("facts", rule["id"], "hard", rule["reason"], field, m.group(0).strip()))
                break
        for req in facts.get("requires_titles", []) or []:
            if normalize(req["name"]) in text and normalize(req["title"]) not in text:
                hits.append(Hit("facts", req["id"], "hard", req["reason"], field, req["name"]))
    return hits


def check_legal(texts: dict[str, str], brand: Brand, service_enabled: bool) -> list[Hit]:
    """Engagement bait, guarantees, salary-as-fact, fear claims, third-party ads, reposts,
    the service CTA while it is off, and the literal never-say phrases."""
    hits: list[Hit] = []
    reasons = {
        "engagement_bait": "engagement bait (locked rule R7)",
        "guarantees": "guaranteed job/salary/income claim (locked rule R8)",
        "salary_as_fact": "salary figure stated as fact (vault salaries are estimates)",
        "fear_claims": "unsourced fear claim about AI and jobs",
        "ads_third_party": "third-party promotion needs a Mawthooq licence (SPEC §2.7)",
        "own_footage": "own footage only, no reposts (locked rule R6)",
        "service_cta": "paid-service CTA while cta.service_enabled = false",
    }
    for field, raw in texts.items():
        text = normalize(raw)
        if not text.strip():
            continue
        for cat, pats in (brand.gates or {}).items():
            if cat == "service_cta" and service_enabled:
                continue
            for m in _find(pats, text):
                hits.append(Hit("legal", cat, "hard", reasons.get(cat, cat), field, m.group(0).strip()))
                break
        ns = brand.never_say or {}
        for cat in ("jobs_money", "engagement_bait", "ads_mawthooq"):
            for phrase in ns.get(cat, []):
                if normalize(phrase) in text:
                    hits.append(Hit("legal", f"never_say.{cat}", "hard", "on the brand never-say list", field, phrase))
        for phrase in ns.get("fact_traps", []):
            np_ = normalize(phrase)
            if np_ in text:
                sev = "soft"  # the hard layer is facts.yaml (context-aware); literals go to the critique
                hits.append(Hit("facts", "never_say.fact_traps", sev, "on the never-say fact-trap list", field, phrase))
        for e in ns.get("banned_emoji", []):
            if e in raw:
                hits.append(Hit("legal", "banned_emoji", "hard", "banned emoji (brand voice)", field, e))
    return hits


SEVERITY = {"hold": "hard", "warn": "soft"}


def check_occasion_rules(texts: dict[str, str], rules: dict | None) -> list[Hit]:
    """gates_occasions.yaml: `match` (a group name or a regex), optionally only `near` another
    group within `window` words (0 = anywhere in the same text), optionally only in `fields`."""
    hits: list[Hit] = []
    if not rules:
        return hits
    groups = rules.get("groups") or {}
    for field, raw in texts.items():
        text = normalize(raw)
        if not text.strip():
            continue
        for r in rules.get("rules") or []:
            if r.get("fields") and field not in r["fields"]:
                continue
            pat = groups.get(r["match"], r["match"])
            near = groups.get(r["near"], r["near"]) if r.get("near") else None
            sev = SEVERITY.get(str(r.get("severity", "hold")), "hard")
            for m in _find([pat], text):
                if near is not None:
                    window = int(r.get("window", 8))
                    if window == 0:
                        if not _find([near], text):
                            continue
                    elif not _near(text, m, [near], window):
                        continue
                hits.append(Hit("legal", f"occasion.{r['id']}", sev, r["reason"], field, m.group(0).strip()))
                break
    return hits


def hashtags_of(caption: str) -> list[str]:
    return _HASHTAG.findall(caption or "")


def check_caption_limits(caption_json: dict, cfg, brand: Brand | None = None) -> list[Hit]:
    """Caption length, hashtag count (config cap AND the hard cap of 5), CTA type allowed,
    first line length, emoji count."""
    hits: list[Hit] = []
    cap = caption_json.get("caption") or ""
    max_chars = min(cfg["qa"]["caption_max_chars"], HARD_CAPTION_MAX)
    if len(cap) > max_chars:
        hits.append(Hit("limits", "caption_length", "hard", f"caption is {len(cap)} chars (max {max_chars})", "caption", str(len(cap))))
    if not cap.strip():
        hits.append(Hit("limits", "caption_empty", "hard", "caption is empty", "caption", ""))
    tags = hashtags_of(cap)
    tag_max = min(cfg["qa"]["hashtags_max"], HARD_HASHTAG_CAP)
    if len(tags) > tag_max:
        hits.append(Hit("limits", "hashtag_count", "hard", f"{len(tags)} hashtags (max {tag_max})", "caption", " ".join(tags)))
    if len(set(t.lower() for t in tags)) != len(tags):
        hits.append(Hit("limits", "hashtag_duplicate", "soft", "duplicate hashtag", "caption", " ".join(tags)))
    cta = caption_json.get("cta_type")
    allowed = list(cfg["cta"]["allowed"])
    if cfg["cta"]["service_enabled"]:
        allowed.append("service")
    if cta not in allowed:
        hits.append(Hit("limits", "cta_type", "hard", f"CTA type '{cta}' is not allowed ({', '.join(allowed)})", "caption", str(cta)))
    first = (caption_json.get("first_line") or cap.split("\n", 1)[0]).strip()
    fl_max = int(((brand.captions if brand else {}) or {}).get("first_line_max_chars", 125))
    if len(first) > fl_max:
        hits.append(Hit("limits", "first_line_length", "soft", f"first line is {len(first)} chars (target <= {fl_max})", "caption", first[:40]))
    if len(_EMOJI_ANY.findall(cap)) > 3:
        hits.append(Hit("limits", "emoji_count", "soft", "more than 3 emoji (brand voice)", "caption", ""))
    return hits


def run_content_gates(texts: dict[str, str], caption_json: dict | None, cfg, brand: Brand) -> list[Hit]:
    hits = check_facts(texts, brand.facts)
    hits += check_legal(texts, brand, bool(cfg["cta"]["service_enabled"]))
    hits += check_occasion_rules(texts, brand.occasion_rules)
    if caption_json is not None:
        hits += check_caption_limits(caption_json, cfg, brand)
    # de-duplicate (same rule + field)
    seen: set[tuple[str, str, str]] = set()
    out = []
    for h in hits:
        k = (h.gate, h.rule, h.field)
        if k not in seen:
            seen.add(k)
            out.append(h)
    return out


def facts_for_prompt(brand: Brand) -> str:
    """The verified facts + traps as plain text for the critique/caption/brief prompts."""
    f = brand.facts
    lines = ["VERIFIED FACTS about Yazeed (the ONLY facts about him that may be stated):"]
    for item in f.get("facts", []):
        lines.append(f"- [{item['id']}] {item['en']}" + (f" / {item['ar']}" if item.get("ar") else ""))
    lines.append("")
    lines.append("FORBIDDEN (known traps; never state these):")
    for r in f.get("forbidden", []):
        lines.append(f"- [{r['id']}] {r['reason']}")
    return "\n".join(lines)
