"""Weekly trends + filming brief (spec §5 "Trends", §7)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import brain, db, learner, vault
from .gates import facts_for_prompt, run_content_gates
from .log import get_logger

log = get_logger("brief")


def week_start(cfg, today: date | None = None) -> date:
    """The coming Saudi week starts on Sunday."""
    d = today or db.utcnow().astimezone(cfg.tz).date()
    return d + timedelta(days=(6 - d.weekday()) % 7)  # Mon=0 .. Sun=6


def run_trends(ctx) -> dict:
    cfg, conn = ctx.cfg, ctx.conn
    today = db.utcnow().astimezone(cfg.tz).date().isoformat()
    res = brain.ask(cfg, conn, "trends", {"today": today}, tools=brain.READ_ONLY_WEB_TOOLS, model=cfg["brain"]["review_model"],
                    timeout=cfg["brain"]["trends_timeout_s"])
    items = [i for i in res["items"] if i["published_date"] >= (date.fromisoformat(today) - timedelta(days=30)).isoformat()]
    res["items"] = items
    conn.execute("INSERT INTO trends(week_start, created_at, json) VALUES(?,?,?)", (week_start(cfg).isoformat(), db.now_iso(), db.jdump(res)))
    return res


def latest_trends(conn) -> dict:
    r = conn.execute("SELECT json FROM trends ORDER BY id DESC LIMIT 1").fetchone()
    return db.jload(r["json"], {}) if r else {}


def _cta_lines(ctx) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for c in (ctx.brand.captions or {}).get("ctas", []):
        if c.get("condition") or c["cta_type"] not in ctx.cfg["cta"]["allowed"]:
            continue
        out.setdefault(c["cta_type"], []).append(c["text"])
    return out


def make_brief(ctx, k: int = 6) -> dict:
    cfg, conn = ctx.cfg, ctx.conn
    pb = learner.load_playbook(conn, cfg)
    slots = learner.recommend_brief(conn, cfg, pb, k)
    hooks = [h for h in (ctx.brand.hooks or {}).get("hooks", []) if not h.get("blocked")]
    eps_all = (ctx.brand_episodes if hasattr(ctx, "brand_episodes") else None) or _episodes(cfg)
    eps = [e for e in eps_all if not e.get("blocked")]
    trends = latest_trends(conn).get("items", [])
    ctas = _cta_lines(ctx)

    def validate(b: dict) -> list[str]:
        errs = []
        if len(b["scripts"]) != len(slots):
            errs.append(f"write exactly {len(slots)} scripts")
        for i, (s, want) in enumerate(zip(b["scripts"], slots), 1):
            if s["hook_type"] != want["hook_type"] or s["duration_band"] != want["duration_band"]:
                errs.append(f"script {i} must use hook_type={want['hook_type']} and duration_band={want['duration_band']}")
            lo, hi = (int(x) for x in s["duration_band"].split("-"))
            if not lo <= s["target_length_s"] <= hi:
                errs.append(f"script {i}: target_length_s outside its band")
            if len(s["on_screen"].split()) > 7:
                errs.append(f"script {i}: on_screen over 7 words")
            if s["cta_type"] != "none" and s["cta"].strip() not in [x.strip() for x in ctas.get(s["cta_type"], [])]:
                errs.append(f"script {i}: cta must be copied from the {s['cta_type']} list")
            text = " ".join([s["hook_line"], s["on_screen"], *s["beats"], s["cta"], s["title"]])
            for h in run_content_gates({"script": text}, None, cfg, ctx.brand):
                if h.severity == "hard":
                    errs.append(f"script {i}: {h.reason} ('{h.match}')")
            errs += [f"script {i}: {e}" for e in source_problems(text, s.get("sources", []), trends)]
        return errs

    ctx_prompt = {
        "slots": slots,
        "ctas": ctas,
        "trends": "\n".join(f"- {t['title']} ({t['source_name']}, {t['published_date']}): {t['summary']} -> {t['episode_idea']} [{t['source_url']}]" for t in trends) or "(none this week)",
        "hooks": "\n".join(f"- [{h['hook_type']}] {h['spoken']} / on screen: {h['on_screen']}" + (" (re-check facts on the official page)" if h.get("recheck_weekly") else "") for h in hooks),
        "episodes": "\n".join(f"- [{e['topic']}] {e['text']}" for e in eps),
        "rules": "\n".join(f"- {r['id']}: {r['text']} ({r['evidence']})" for r in pb["rules"]),
        "facts": facts_for_prompt(ctx.brand),
    }
    out = brain.ask(cfg, conn, "brief", ctx_prompt, model=cfg["brain"]["review_model"], validate=validate)
    ws = week_start(cfg)
    brief = {"week_start": ws.isoformat(), "generated_at": db.now_iso(), "playbook_version": pb["version"], "scripts": [
        {"n": i, **s, "explore": slots[i - 1]["explore"]} for i, s in enumerate(out["scripts"], 1)], "notes": out["notes"]}
    path = write_brief_note(cfg, brief)
    conn.execute("INSERT INTO briefs(week_start, created_at, json, path) VALUES(?,?,?,?)", (ws.isoformat(), db.now_iso(), db.jdump(brief), str(path)))
    ctx.notify("info", "This week's filming brief is ready", f"{len(brief['scripts'])} scripts for the week of {ws.isoformat()}", None)
    return brief


# outside facts that change (programmes, platforms, requirements): each named one needs a
# trends link or an explicit "re-check on the official site" note (fact-check #23)
NEEDS_SOURCE = {
    "LinkedIn": ("linkedin", "لينكد"), "Jadarat": ("jadarat", "جدارات"), "Taqat": ("taqat", "طاقات"), "Tamheer": ("tamheer", "تمهير"),
    "Sabbar": ("sabbar", "صبار"), "Bayt": ("bayt", "بيت.كوم", "بيت كوم"), "Glassdoor": ("glassdoor", "جلاسدور"),
    "stc TIP": ("stc tip", "tip"), "Aramco": ("aramco", "ارامكو"), "PIF": ("\\bpif\\b", "صندوق الاستثمارات"), "HRDF": ("hrdf", "هدف"),
}


def source_problems(text: str, sources: list[str], trends: list[dict]) -> list[str]:
    import re

    from .gates import normalize

    t = normalize(text)
    joined = normalize(" ".join(sources))
    trend_urls = {x.get("source_url", "") for x in trends}
    errs = []
    for name, keys in NEEDS_SOURCE.items():
        if any(re.search(normalize(k, lower=False) if k.startswith("\\b") else re.escape(normalize(k)), t) for k in keys):
            ok = "re-check" in joined and normalize(name) in joined or any(u and u in " ".join(sources) for u in trend_urls)
            if not ok:
                errs.append(f"names {name}: add a trends link or 're-check on the official site before filming: {name}' to sources")
    for src in sources:
        for url in re.findall(r"https?://\S+", src):
            if url.rstrip(").,") not in trend_urls:
                errs.append(f"source link {url} is not from the trends list (you can't browse: use 're-check on the official site before filming: <name>')")
    return errs


def _episodes(cfg) -> list[dict]:
    import yaml

    p = cfg.brand_dir / "episodes.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("episodes", []) if p.exists() else []


def brief_markdown(brief: dict) -> str:
    lines = [f"Week of **{brief['week_start']}** · playbook v{brief['playbook_version']} · {len(brief['scripts'])} scripts", ""]
    for s in brief["scripts"]:
        lines += [
            f"## {s['n']}. {s['title']}",
            f"- **Hook (say it word for word):** {s['hook_line']}",
            f"- **On screen:** {s['on_screen']}",
            f"- **Beats:** 1) {s['beats'][0]} · 2) {s['beats'][1]} · 3) {s['beats'][2]}",
            f"- **CTA:** {s['cta'] or '—'}",
            f"- **Length:** ~{s['target_length_s']} s ({s['duration_band']} s) · **Pillar:** {s['pillar']} · **Hook type:** {s['hook_type']}",
            f"- **Shot:** {s['shot_note']}",
            f"- **Experiment:** {s['experiment']}" + (" (exploration)" if s.get("explore") else ""),
        ]
        if s.get("sources"):
            lines.append("- **Re-check before filming:** " + " · ".join(s["sources"]))
        lines.append("")
    if brief.get("notes"):
        lines += ["**Notes:** " + brief["notes"], ""]
    return "\n".join(lines)


def write_brief_note(cfg, brief: dict):
    rel = f"Filming Briefs/{brief['week_start']} Filming Brief.md"
    return vault.write_block(cfg, rel, "brief", brief_markdown(brief), title=f"Filming Brief {brief['week_start']}",
                             frontmatter={"type": "filming-brief", "date": brief["week_start"], "tags": ["social-media", "reels", "filming-brief"],
                                          "generated_by": "Reel Engine"},
                             intro="Film these, then drop the clips in the OneDrive `Reel Inbox`. Everything else is automatic.")
