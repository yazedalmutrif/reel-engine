"""Filming ideas for upcoming occasions (v1.2, 2026-10-15).

One generation per occurrence (slug + first day), cached in `occasion_ideas`, so the 14/7/3/0-day
reminders and the dashboard all show the same ideas.

- Brain path: ONE call per `occasions.remind()` run for every occurrence that still needs ideas
  (task `occasion_ideas`, no tools, schema-validated, counted in the daily cap through brain.ask).
  It is skipped when `calendar.remind_ideas` is off or fewer than `calendar.brain_reserve_calls`
  calls are left in the 24 h cap, so reel processing is never starved.
- Template path: deterministic Saudi-dialect templates per category, filled with the occasion's
  names. Used whenever the brain is skipped or fails. Template rows are cached but not final: a later
  run with the brain available upgrades them (still at most one brain call per run).
- The legal checklist comes from brand/gates_occasions.yaml (`category_checklist` + optional per-slug
  `occasion_checklist`), read fresh every time so a refined rule shows up at once.
- Nothing here talks to the network except through brain.ask.
"""
from __future__ import annotations

import json
import re
from typing import Iterable

from . import db
from .gates import Brand, facts_for_prompt, run_content_gates
from .log import get_logger

log = get_logger("ideas")

MAX_HOOK_WORDS = 14
MAX_PER_CALL = 20  # occasions per brain call (schema maxItems); the rest get templates this run
_URL = re.compile(r"https?://|www\.", re.I)
_MENTION = re.compile(r"(?<![\w@])@[A-Za-z0-9._]{1,30}")

# ------------------------------------------------------------------ templates
# {ar} = the Arabic name, {en} = the English name. Every template must pass run_content_gates with the
# example brand for every seed occasion (tests/test_reminders.py checks all of them). No offers, prizes,
# jokes, flags, emblems or leader images; own footage only.
_SHOT = "Face in the central square, window light, quiet room; own footage only, no logos or other people's clips."
_SHOT_NATIONAL = ("Face in the central square, window light; no flag, Shahada, emblem or leader image in frame "
                  "(or follow the checklist exactly); no product, price or brand anywhere.")
_SHOT_RELIGIOUS = "Face in the central square, calm light, modest look; no music under any verse, no jokes, nothing promotional."

TEMPLATES: dict[str, list[dict]] = {
    "national": [
        {"title": "{en}: tech then and now", "hook_line": "بمناسبة {ar}: وش تغير في التقنية عندنا؟",
         "beats": ["قارن شي واحد بسيط: كيف كنا نسويه قبل وكيف نسويه اليوم",
                   "اذكر مثال واحد تعرفه بنفسك، بدون أرقام ما تأكدت منها",
                   "اختم بجملة فخر هادية عن اللي يبنونه شبابنا"],
         "shot_note": _SHOT_NATIONAL, "length_s": 35},
        {"title": "{en}: 3 skills the market asks for", "hook_line": "{ar}: ثلاث مهارات تقنية يطلبها سوق العمل عندنا",
         "beats": ["المهارة الأولى ومثال واحد عملي عليها",
                   "المهارة الثانية ووين تتعلمها مجانا",
                   "المهارة الثالثة وكيف تبينها في سيرتك الذاتية (CV)"],
         "shot_note": _SHOT_NATIONAL, "length_s": 45},
        {"title": "{en}: what it means to a tech grad", "hook_line": "وش يعني لك {ar} لو كنت خريج تقنية؟",
         "beats": ["جوابك أنت بكلامك (✍️)",
                   "مشروع أو مبادرة وطنية تقنية تعرفها، بمعلومة متأكد منها",
                   "خطوة وحدة يقدر الخريج يسويها هالأسبوع"],
         "shot_note": _SHOT_NATIONAL, "length_s": 30},
    ],
    "religious": [
        {"title": "{en}: a calm plan for the days ahead", "hook_line": "{ar} قرب، رتب أسبوعك بثلاث خطوات بسيطة",
         "beats": ["حدد هدف واحد واضح للأيام الجاية",
                   "خل وقت التعلم قصير وثابت، ربع ساعة تكفي",
                   "خفف الجوال وقت الراحة والعائلة"],
         "shot_note": _SHOT_RELIGIOUS, "length_s": 30},
        {"title": "{en}: a short greeting and a light plan", "hook_line": "تهنئة قصيرة بمناسبة {ar}، وخطة خفيفة للي بعده",
         "beats": ["التهنئة بكلامك أنت (✍️)",
                   "شي واحد بتكمله بعد المناسبة: دورة، مشروع أو تقديم",
                   "تذكير هادي إن الراحة جزء من الخطة"],
         "shot_note": _SHOT_RELIGIOUS, "length_s": 25},
        {"title": "{en}: keep your routine gently", "hook_line": "كيف تحافظ على روتين التعلم في {ar} بدون ضغط؟",
         "beats": ["اختر وقت ثابت يناسب يومك في هالأيام",
                   "مهمة صغيرة وحدة كل يوم بدل خطة كبيرة",
                   "راجع أسبوعك في آخره وعدل بهدوء"],
         "shot_note": _SHOT_RELIGIOUS, "length_s": 35},
    ],
    "international": [
        {"title": "{en}: the tech and careers angle", "hook_line": "{ar}: وش علاقته بالتقنية والوظايف عندنا؟",
         "beats": ["اربط اليوم بمجال تقني واحد بجملة وحدة",
                   "مثال من السعودية متأكد منه، بدون أسماء أشخاص",
                   "خطوة عملية للخريج يبدأ فيها"],
         "shot_note": _SHOT, "length_s": 35},
        {"title": "{en}: 3 practical ideas", "hook_line": "ثلاث أفكار عملية بمناسبة {ar}",
         "beats": ["الفكرة الأولى ومثال قصير",
                   "الفكرة الثانية وكيف تطبقها هالأسبوع",
                   "الفكرة الثالثة وأداة مجانية تساعدك فيها"],
         "shot_note": _SHOT, "length_s": 40},
        {"title": "{en}: start here as a grad", "hook_line": "لو تبي تستفيد من {ar} كخريج، ابدأ من هنا",
         "beats": ["وش المشكلة اللي يواجهها الخريج في هالموضوع",
                   "الحل البسيط اللي جربته أو تعرفه (✍️)",
                   "وين يتعلم أكثر من مصدر رسمي"],
         "shot_note": _SHOT, "length_s": 30},
    ],
    "tech": [
        {"title": "{en}: what matters for a grad", "hook_line": "{ar}: وش الشي اللي يهمك كخريج تقنية؟",
         "beats": ["لخص الموضوع بجملة وحدة واضحة",
                   "وش المهارات أو الوظايف المرتبطة فيه، من مصدر رسمي",
                   "خطوة وحدة تبدأ فيها اليوم"],
         "shot_note": _SHOT + " Re-check the official page before filming.", "length_s": 40},
        {"title": "{en}: 3 related skills", "hook_line": "ثلاث مهارات تقنية مرتبطة بـ{ar}",
         "beats": ["المهارة الأولى ومثال عملي",
                   "المهارة الثانية ووين تتعلمها مجانا",
                   "المهارة الثالثة وكيف تبينها في مشروع صغير"],
         "shot_note": _SHOT, "length_s": 45},
        {"title": "{en}: your first step today", "hook_line": "أول خطوة تسويها اليوم بمناسبة {ar}",
         "beats": ["الخطوة بالتفصيل في أقل من دقيقة",
                   "الغلطة اللي لازم تتجنبها فيها",
                   "وش تسوي بعدها عشان تكمل"],
         "shot_note": _SHOT + " Screen recording of your own screen only, nothing internal.", "length_s": 30},
    ],
    "career": [
        {"title": "{en}: what a grad should know", "hook_line": "{ar}: وش يحتاج الخريج يعرف؟",
         "beats": ["المعلومة الأهم بجملة وحدة",
                   "مثال واقعي بدون أسماء أشخاص",
                   "خطوة عملية تسويها هالأسبوع"],
         "shot_note": _SHOT, "length_s": 30},
        {"title": "{en}: 3 quick tips", "hook_line": "ثلاث نصايح سريعة بمناسبة {ar}",
         "beats": ["النصيحة الأولى وكيف تطبقها",
                   "النصيحة الثانية ومثال قصير",
                   "النصيحة الثالثة وكيف تبينها في سيرتك الذاتية (CV)"],
         "shot_note": _SHOT, "length_s": 40},
        {"title": "{en}: a common grad mistake", "hook_line": "غلطة يسويها كثير من الخريجين، و{ar} فرصة تصلحها",
         "beats": ["وش الغلطة بالضبط",
                   "ليش تضرك في التقديم أو الشغل",
                   "التصحيح بخطوة وحدة واضحة"],
         "shot_note": _SHOT, "length_s": 35},
    ],
}
_SHORT_HOOK = "فكرة مقطع بمناسبة {ar}"  # used when a filled hook would pass the word limit (long official names)


def _fill(t: str, o: dict) -> str:
    return t.format(ar=o.get("name_ar") or o.get("name_en") or "", en=o.get("name_en") or o.get("name_ar") or "")


def template_ideas(occasion: dict, n: int = 3) -> list[dict]:
    """Deterministic ideas for an occasion (category templates filled with its names)."""
    out = []
    for t in TEMPLATES.get(occasion.get("category") or "", TEMPLATES["international"])[: max(1, n)]:
        hook = _fill(t["hook_line"], occasion)
        if len(hook.split()) > MAX_HOOK_WORDS:
            hook = _fill(_SHORT_HOOK, occasion)
        out.append({"title": _fill(t["title"], occasion)[:120], "hook_line": hook, "beats": list(t["beats"]),
                    "shot_note": t["shot_note"], "length_s": int(t["length_s"])})
    return out


def checklist_for(brand: Brand | None, occasion: dict) -> list[str]:
    rules = (brand.occasion_rules if brand else None) or {}
    lines = list((rules.get("category_checklist") or {}).get(occasion.get("category") or "", []) or [])
    lines += [c for c in ((rules.get("occasion_checklist") or {}).get(occasion.get("slug") or "", []) or []) if c not in lines]
    return lines


# ------------------------------------------------------------------ checks shared by brain + tests
def idea_text(idea: dict) -> str:
    return " ".join([str(idea.get("title") or ""), str(idea.get("hook_line") or ""), *[str(b) for b in idea.get("beats") or []],
                     str(idea.get("shot_note") or "")])


def idea_problems(idea: dict, cfg, brand: Brand, captions: Iterable[str] = ()) -> list[str]:
    """Why an idea can't be shown (empty = fine). Same rules for the brain and the templates."""
    from .accounts import COPY_THRESHOLD, copy_overlap

    errs = []
    if len(idea.get("beats") or []) != 3:
        errs.append("exactly 3 beats")
    if len(str(idea.get("hook_line") or "").split()) > MAX_HOOK_WORDS:
        errs.append(f"hook line over {MAX_HOOK_WORDS} words")
    text = idea_text(idea)
    if _URL.search(text) or _MENTION.search(text):
        errs.append("no @handles or links")
    for h in run_content_gates({"script": text}, None, cfg, brand):
        if h.severity == "hard":
            errs.append(f"{h.reason} ('{h.match}')")
    caps = list(captions)
    if caps and max(copy_overlap(text, caps), copy_overlap(str(idea.get("hook_line") or ""), caps)) >= COPY_THRESHOLD:
        errs.append("copies another account's caption; write an original idea")
    return errs


# ------------------------------------------------------------------ cache
def _key(occ) -> tuple[str, str]:
    return occ.occasion["slug"], occ.start.isoformat()


def cached(conn, occ) -> dict | None:
    """The cached generation for an occurrence, or None (also None on an old DB without the table)."""
    try:
        r = conn.execute("SELECT source, json, created_at FROM occasion_ideas WHERE slug=? AND start=?", _key(occ)).fetchone()
    except Exception:  # noqa: BLE001 - DB from before v3
        return None
    if not r:
        return None
    try:
        data = json.loads(r["json"]) or {}
    except ValueError:
        return None
    return {"source": r["source"], "ideas": data.get("ideas") or [], "created_at": r["created_at"]}


def _store(conn, occ, source: str, ideas: list[dict]) -> None:
    slug, start = _key(occ)
    conn.execute("INSERT INTO occasion_ideas(slug, start, created_at, source, json) VALUES(?,?,?,?,?) "
                 "ON CONFLICT(slug, start) DO UPDATE SET created_at=excluded.created_at, source=excluded.source, json=excluded.json",
                 (slug, start, db.now_iso(), source, db.jdump({"ideas": ideas})))


def peek(conn, cfg, brand: Brand | None, occ, n: int | None = None) -> dict:
    """Read-only: the cached ideas, else template ideas made on the fly (no brain call, nothing written)."""
    n = int(n or cfg.get("calendar.ideas_per_reminder", 3))
    c = cached(conn, occ)
    if c and c["ideas"]:
        return {"source": c["source"], "ideas": c["ideas"][:n], "checklist": checklist_for(brand, occ.occasion)}
    return {"source": "template", "ideas": template_ideas(occ.occasion, n), "checklist": checklist_for(brand, occ.occasion)}


# ------------------------------------------------------------------ brain
def brain_allowed(conn, cfg) -> tuple[bool, str]:
    if not cfg.get("calendar.remind_ideas", True):
        return False, "calendar.remind_ideas = false"
    from . import brain

    left = int(cfg["brain"]["max_calls_per_day"]) - brain.calls_today(conn)
    reserve = int(cfg.get("calendar.brain_reserve_calls", 10))
    if left < reserve:
        return False, f"only {left} brain calls left in the 24 h cap (reserve {reserve})"
    return True, ""


def _brain_input(occs: list, n: int) -> list[dict]:
    out = []
    for x in occs:
        o = x.occasion
        out.append({"id": o["slug"], "start": x.start.isoformat(), "name_ar": o["name_ar"], "name_en": o["name_en"],
                    "category": o["category"], "official": bool(o["official"]),
                    "hijri_date_may_move": bool(x.approximate), "days_until": x.days_until,
                    "angle": (o.get("angle") or "")[:300], "keywords": list(o.get("keywords") or [])[:8], "ideas_wanted": n})
    return out


def brain_ideas(conn, cfg, brand: Brand, occs: list, n: int) -> dict[tuple[str, str], list[dict]]:
    """ONE brain call for all `occs`. Raises brain.BrainError (incl. BrainBudgetExceeded) on failure."""
    from . import brain
    from .accounts import stored_captions

    caps = stored_captions(conn)
    want = {_key(x) for x in occs}

    def validate(out: dict) -> list[str]:
        errs = []
        seen = set()
        for item in out["occasions"]:
            k = (item["id"], item["start"])
            if k not in want:
                errs.append(f"unknown occasion id/start {item['id']} {item['start']} (use the ids and starts given)")
                continue
            seen.add(k)
            if len(item["ideas"]) != n:
                errs.append(f"{item['id']}: write exactly {n} ideas")
            for i, idea in enumerate(item["ideas"], 1):
                errs += [f"{item['id']} idea {i}: {e}" for e in idea_problems(idea, cfg, brand, caps)]
        for slug, start in sorted(want - seen):
            errs.append(f"missing ideas for {slug} {start}")
        return errs

    ctx = {"n": n, "voice": brand.voice or "(no voice guide)", "facts": facts_for_prompt(brand),
           "occasions_json": json.dumps(_brain_input(occs, n), ensure_ascii=False, indent=1).replace("<", "‹").replace(">", "›")}
    out = brain.ask(cfg, conn, "occasion_ideas", ctx, model=cfg["brain"]["model"], validate=validate)
    return {(i["id"], i["start"]): [{k: idea[k] for k in ("title", "hook_line", "beats", "shot_note", "length_s")} for idea in i["ideas"]]
            for i in out["occasions"]}


def ensure_ideas(conn, cfg, brand: Brand, occs: list, n: int | None = None, *, allow_brain: bool = True) -> dict[tuple[str, str], dict]:
    """Ideas for every occurrence in `occs`, cached. At most ONE brain call (for every occurrence that has
    no ideas yet or only template ideas); on any brain problem the templates are used and cached as
    'template' so a later run can upgrade them."""
    from . import brain

    n = int(n or cfg.get("calendar.ideas_per_reminder", 3))
    have = {_key(x): cached(conn, x) for x in occs}
    need = [x for x in occs if not have[_key(x)] or have[_key(x)]["source"] != "brain" or len(have[_key(x)]["ideas"]) < n]
    got: dict[tuple[str, str], list[dict]] = {}
    if need and allow_brain:
        ok, why = brain_allowed(conn, cfg)
        if ok:
            try:
                got = brain_ideas(conn, cfg, brand, need[:MAX_PER_CALL], n)
            except brain.BrainError as exc:  # no CLI, cap, schema/validation failures, timeouts
                log.warning("occasion ideas: brain unavailable, using templates (%s)", str(exc)[:200])
        else:
            log.info("occasion ideas: brain skipped (%s)", why)
    for x in need:
        k = _key(x)
        if k in got:
            _store(conn, x, "brain", got[k])
            have[k] = {"source": "brain", "ideas": got[k]}
        elif not have[k] or len(have[k]["ideas"]) < n:
            ideas = template_ideas(x.occasion, n)
            _store(conn, x, "template", ideas)
            have[k] = {"source": "template", "ideas": ideas}
    return {k: {"source": v["source"], "ideas": v["ideas"][:n], "checklist": checklist_for(brand, x.occasion)}
            for x in occs for k, v in [(_key(x), have[_key(x)])]}


def ideas_for(conn, cfg, brand: Brand, occurrence, n: int | None = None, *, allow_brain: bool = True) -> dict:
    """{"source": "brain"|"template", "ideas": [...], "checklist": [...]} for one occurrence (cached)."""
    return ensure_ideas(conn, cfg, brand, [occurrence], n, allow_brain=allow_brain)[_key(occurrence)]


def as_text(ideas: list[dict]) -> str:
    lines = []
    for i, idea in enumerate(ideas, 1):
        b = idea.get("beats") or []
        lines += [f"{i}. {idea.get('title', '')}",
                  f"   Hook: {idea.get('hook_line', '')}",
                  *[f"   {k}) {beat}" for k, beat in enumerate(b, 1)],
                  f"   Shot: {idea.get('shot_note', '')}",
                  f"   Length: ~{idea.get('length_s', '')} s"]
    return "\n".join(lines)

