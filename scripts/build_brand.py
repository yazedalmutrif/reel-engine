"""Regenerate the brand files from the vault note "Reels Brand Copy" (idempotent).

Reads (never writes) the vault note and writes:
  brand/voice.md         <- §5 Voice guide (verbatim) + §9 Never-say list (verbatim)
  brand/hooks.yaml       <- §6 Hook bank (30 hooks), each mapped to a learner hook_type arm
  brand/captions.yaml    <- §7 templates, organic CTAs, DRAFT service CTAs, hashtag pool
  brand/never_say.yaml   <- §9 quoted phrases by category (used by the legal/content gate)

Usage: .venv\\Scripts\\python.exe scripts\\build_brand.py [--note "<path to note>"]
(default: $REELENGINE_BRAND_NOTE, else the path written in the git-ignored file brand_note.local.txt)
Run it again whenever the note changes (e.g. after the fact-check corrections).
Hand-maintained rules that must NOT be regenerated live in brand/facts.yaml and
brand/gates.yaml.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def default_note() -> str | None:
    """The brand-copy note: $REELENGINE_BRAND_NOTE, else the path in the git-ignored file brand_note.local.txt."""
    env = os.environ.get("REELENGINE_BRAND_NOTE", "").strip()
    if env:
        return env
    local = ROOT / "brand_note.local.txt"
    if local.exists():
        return local.read_text(encoding="utf-8").strip() or None
    return None

# Brand-copy hook types (8) -> learner arms (6).
TYPE_TO_ARM = {
    "question": "question",
    "bold claim": "bold_claim",
    "myth-bust": "bold_claim",
    "number / list": "list_number",
    "mistake": "problem",
    "story": "story",
    "pov": "pov_before_after",
    "before / after": "pov_before_after",
}
PILLAR_TO_TOPIC = {"1": "ai_tools", "2": "job_hunt", "3": "build_proof", "4": "cv_method"}
GOAL_TO_CTA = {"follow": "follow", "save": "save", "share": "send", "comment": "comment"}
# phrases quoted in §9 that are the *allowed* wording, never forbidden
ALLOW = {"تقديرات", "سويت خرائط وظائف لأصحابي", "أبني أداة…", "أبني أداة", "فريقنا", "وصل للنهائيات"}


def section(text: str, num: str) -> str:
    m = re.search(rf"^## {re.escape(num)}\. .*?$(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not m:
        raise SystemExit(f"section {num} not found in the note")
    return m.group(0).strip()


def subsection(sec: str, title_start: str) -> str:
    m = re.search(rf"^### {re.escape(title_start)}.*?$(.*?)(?=^### |\Z)", sec, re.S | re.M)
    return m.group(1) if m else ""


def table_rows(block: str) -> list[list[str]]:
    rows = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|") or re.match(r"^\|\s*-", line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(cells)
    return rows[1:] if rows else []  # drop header


def unquote(s: str) -> str:
    """Drop markdown bold and wrapping ASCII quotes; guillemets are part of the text."""
    s = re.sub(r"^\*\*|\*\*$", "", s.strip()).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s


def expand_slashes(phrase: str) -> list[str]:
    """'فزت/فزنا بهاكاثون' -> ['فزت بهاكاثون', 'فزنا بهاكاثون'] (per-token alternatives)."""
    out = [""]
    for tok in phrase.split(" "):
        alts = tok.split("/") if "/" in tok else [tok]
        out = [f"{o} {a}".strip() for o in out for a in alts]
    return out


def quotes(s: str) -> list[str]:
    guil = re.findall(r"«([^»]+)»", s)
    rest = re.sub(r"«[^»]+»", " ", s)  # ASCII quotes only outside guillemets (no nested pieces)
    found = guil + re.findall(r"\"([^\"]*[؀-ۿ][^\"]*)\"", rest)
    out: list[str] = []
    for q in found:
        q = q.replace("…", "").replace("\"", "").strip(" .،")
        if q and q not in ALLOW:
            out.extend(expand_slashes(q))
    uniq = list(dict.fromkeys(out))
    # drop phrases contained in a longer phrase of the same list (e.g. a nested quote)
    return [q for q in uniq if not any(q != o and q in o for o in uniq)]


def build_hooks(sec6: str) -> list[dict]:
    hooks = []
    for cells in table_rows(sec6):
        if len(cells) < 6 or not cells[0].isdigit():
            continue
        n, typ, spoken, on_screen, pillar, facts = cells[:6]
        arm = TYPE_TO_ARM.get(typ.strip().lower())
        if arm is None:
            raise SystemExit(f"unknown hook type {typ!r} (row {n}); update TYPE_TO_ARM")
        hooks.append(
            {
                "id": f"H{int(n):02d}",
                "brand_type": typ.strip(),
                "hook_type": arm,
                "spoken": unquote(spoken),
                "on_screen": unquote(on_screen),
                "on_screen_words": len(unquote(on_screen).split()),
                "topic": PILLAR_TO_TOPIC.get(pillar.strip(), pillar.strip()),
                "facts_from": facts,
                "recheck_weekly": "⚠️" in facts and "Film only after" not in facts,
                "blocked": _blocked(facts)[0],
                "blocked_reason": _blocked(facts)[1],
                "needs_confirmation": "✍️" in facts,
            }
        )
    return hooks


def _blocked(cell: str) -> tuple[bool, str | None]:
    """Rows gated with '⚠️ Film only after …' are never used automatically."""
    m = re.search(r"Film only after ([^.;(]+)", cell)
    return (True, "film only after " + m.group(1).strip()) if m else (False, None)


def build_episodes(sec4: str) -> list[dict]:
    """§4 pillar episode tables -> ideas for the weekly brief (blocked rows flagged)."""
    eps = []
    for m in re.finditer(r"^### Pillar (\d): (.*?)$(.*?)(?=^### |\Z)", sec4, re.S | re.M):
        pillar, title, body = m.group(1), m.group(2).strip(), m.group(3)
        for cells in table_rows(body):
            if len(cells) < 3:
                continue
            n, text, draws = cells[0], cells[1], cells[2]
            blocked, reason = _blocked(text + " " + draws)
            eps.append(
                {
                    "id": f"P{pillar}-{n}",
                    "topic": PILLAR_TO_TOPIC.get(pillar, pillar),
                    "pillar_title": title,
                    "text": text,
                    "draws_on": draws,
                    "draft": n.upper() == "DRAFT",
                    "blocked": blocked or n.upper() == "DRAFT",
                    "blocked_reason": reason or ("paid-service DRAFT (cta.service_enabled = false)" if n.upper() == "DRAFT" else None),
                    "recheck_weekly": "⚠️" in (text + draws) and not blocked,
                    "needs_confirmation": "✍️" in (text + draws),
                }
            )
    return eps


def build_captions(sec7: str) -> dict:
    templates = {}
    for m in re.finditer(r"^### Template ([A-Z]): (.*?)$\s*```\s*(.*?)```", sec7, re.S | re.M):
        templates[m.group(1)] = {"name": m.group(2).strip(), "skeleton": m.group(3).strip()}
    organic = []
    for cells in table_rows(subsection(sec7, "CTA lines")):
        if len(cells) >= 3 and cells[0].isdigit():
            line = re.sub(r"\*\(.*?\)\*", "", cells[2]).strip()
            organic.append(
                {
                    "id": f"C{int(cells[0]):02d}",
                    "cta_type": GOAL_TO_CTA[cells[1].strip().lower()],
                    "text": line,
                    "condition": (re.search(r"\*\((.*?)\)\*", cells[2]) or [None, None])[1],
                }
            )
    drafts = []
    for cells in table_rows(subsection(sec7, "DRAFT: paid service")):
        if len(cells) >= 2 and re.match(r"D\d", cells[0]):
            drafts.append({"id": cells[0], "cta_type": "service", "draft": True, "text": re.sub(r"^\*\*DRAFT:\*\*\s*", "", cells[1]).strip()})
    pool_line = re.search(r"\*\*Hashtag pool\*\*.*?:(.*)$", sec7, re.M)
    pool = re.findall(r"#[\w\u0600-\u06FF_]+", pool_line.group(1)) if pool_line else []
    rules = re.search(r"\*\*Pipeline rules for every caption:\*\*(.*)$", sec7, re.M)
    return {
        "templates": templates,
        "ctas": organic,
        "service_ctas_draft": drafts,
        "hashtag_pool": pool,
        "pipeline_rules": rules.group(1).strip() if rules else "",
        "first_line_max_chars": 125,
        "hashtags_min": 3,
        "hashtags_max": 5,
    }


def build_never_say(sec9: str) -> dict:
    cats: dict[str, list[str]] = {}
    traps = []
    for cells in table_rows(subsection(sec9, "Fact traps")):
        if cells:
            traps += quotes(cells[0])
    cats["fact_traps"] = traps
    for key, title in (("jobs_money", "Jobs, money and results"), ("engagement_bait", "Engagement bait"), ("ads_mawthooq", "Ads and Mawthooq"), ("tone", "Tone")):
        block = subsection(sec9, title)
        phrases: list[str] = []
        for para in re.split(r"\n(?=- )|\n\n", block):
            # only take quotes before an "allowed wording" marker
            para = re.split(r"label them|Allowed:|Say instead|✅", para)[0]
            phrases += quotes(para)
        cats[key] = sorted(set(phrases), key=phrases.index)
    emoji_line = subsection(sec9, "Tone")
    cats["banned_emoji"] = re.findall(r"[\U0001F300-\U0001FAFF]", emoji_line)
    return cats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--note", default=default_note())
    a = ap.parse_args()
    if not a.note:
        ap.error("give the brand-copy note with --note <path> (or set REELENGINE_BRAND_NOTE, or put the path in brand_note.local.txt)")
    note = Path(a.note)
    text = note.read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    head = f"# GENERATED by scripts/build_brand.py from '{note.name}' (sha256 {digest}) on {date.today().isoformat()}. Do not hand-edit; edit the note and rerun.\n"
    brand = ROOT / "brand"

    sec5, sec6, sec7, sec9 = section(text, "5"), section(text, "6"), section(text, "7"), section(text, "9")
    (brand / "voice.md").write_text(
        f"<!-- GENERATED by scripts/build_brand.py from '{note.name}' (sha256 {digest}). Edit the note and rerun. -->\n"
        f"# Brand voice (for the caption, critique and brief prompts)\n\n{sec5}\n\n{sec9}\n",
        encoding="utf-8",
    )
    hooks = build_hooks(sec6)
    dump = lambda obj: yaml.safe_dump(obj, allow_unicode=True, sort_keys=False, width=1000)  # noqa: E731
    mapping = {k: v for k, v in TYPE_TO_ARM.items()}
    (brand / "hooks.yaml").write_text(head + dump({"type_to_arm": mapping, "hooks": hooks}), encoding="utf-8")
    caps = build_captions(sec7)
    (brand / "captions.yaml").write_text(head + dump(caps), encoding="utf-8")
    episodes = build_episodes(section(text, "4"))
    (brand / "episodes.yaml").write_text(head + dump({"episodes": episodes}), encoding="utf-8")
    print(f"episodes: {len(episodes)} ({sum(e['blocked'] for e in episodes)} blocked: {[e['id'] for e in episodes if e['blocked']]})")
    print(f"hooks blocked: {[h['id'] for h in hooks if h['blocked']]}")
    never = build_never_say(sec9)
    (brand / "never_say.yaml").write_text(head + dump(never), encoding="utf-8")

    by_arm: dict[str, int] = {}
    for h in hooks:
        by_arm[h["hook_type"]] = by_arm.get(h["hook_type"], 0) + 1
    print(f"hooks: {len(hooks)} {by_arm}")
    print(f"ctas: {len(caps['ctas'])} organic, {len(caps['service_ctas_draft'])} DRAFT service; templates {list(caps['templates'])}; pool {len(caps['hashtag_pool'])}")
    print("never_say: " + ", ".join(f"{k}={len(v)}" for k, v in never.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
