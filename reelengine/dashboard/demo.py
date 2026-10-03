"""Demo data for reviewing the dashboard layout (spec §12, "30 fake reels, clearly marked").

seed_demo() writes a SEPARATE SQLite file (never data/engine.sqlite) full of obviously
fake data: every reel has is_demo=1, thumbnails say DEMO, and the site built from it
carries a DEMO banner on every page. The Arabic hooks and captions are generic tips
about AI tools and tech careers; nothing in here is a claim about Yazeed.

Dates are generated relative to "now", so a fresh seed always looks current.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .. import db

SEED = 20260929
CHECKPOINTS = (1, 6, 24, 72, 168, 672)

# title_card, on-screen hook (<= 7 words), spoken hook, pillar, hook_type
CONTENT = [
    ("٣ أدوات AI توفر عليك ساعات مذاكرة", "٣ أدوات توفر عليك ساعات", "عندك اختبار الأسبوع الجاي؟ هذي ثلاث أدوات بتختصر عليك نص الوقت", "ai_tools", "list_number"),
    ("أكبر غلطة في الـCV", "لا تكتب سيرتك كذا", "أغلب السير الذاتية اللي أشوفها فيها نفس الغلطة… وهي بسيطة", "cv_method", "problem"),
    ("الـAI ما بياخذ وظيفتك", "الـAI ما بياخذ وظيفتك", "الذكاء الاصطناعي ما راح ياخذ وظيفتك… اللي يعرف يستخدمه هو اللي بياخذها", "ai_tools", "bold_claim"),
    ("جهّز لمقابلتك التقنية في أسبوع", "مقابلة تقنية؟ أسبوع يكفي", "عندك مقابلة تقنية بعد أسبوع؟ هذي خطة يوم بيوم", "job_hunt", "list_number"),
    ("ما عندك خبرة؟ سوّ مشروع", "بدون خبرة؟ ابدأ من هنا", "كل الوظائف تبي خبرة… طيب من وين أجيب الخبرة؟", "build_proof", "question"),
    ("بروفايل LinkedIn قبل وبعد", "قبل وبعد: LinkedIn", "هذا بروفايل ما يجيب ولا رسالة… وهذا نفسه بعد ثلاث تعديلات", "job_hunt", "pov_before_after"),
    ("Claude ولا ChatGPT؟", "متى تستخدم كل واحد؟", "Claude ولا ChatGPT؟ السؤال الصح: وش المهمة اللي عندك؟", "ai_tools", "question"),
    ("برومبت يلخص محاضرتك", "لخّص محاضرتك في دقيقة", "حط هالبرومبت وخله يطلع لك ملخص مرتب من محاضرة ساعتين", "ai_tools", "bold_claim"),
    ("ليش الـATS يرفض سيرتك", "سيرتك ما توصل لأحد", "ممكن سيرتك ما وصلت لإنسان أصلاً… خلني أوريك ليش", "cv_method", "problem"),
    ("٣ مشاريع تعبّي GitHub", "GitHub فاضي؟", "GitHub حقك فاضي؟ هذي ثلاث مشاريع صغيرة تسويها في ويكند", "build_proof", "list_number"),
    ("محلل بيانات ولا مهندس بيانات؟", "وش الفرق بينهم؟", "محلل بيانات ومهندس بيانات… تحسبهم نفس الشي؟", "job_hunt", "question"),
    ("POV: أول يوم في التدريب", "أول يوم تدريب", "أول يوم في التدريب التعاوني… وش المفروض تسوي؟", "job_hunt", "pov_before_after"),
    ("لا تقول «أنا طموح» في المقابلة", "لا تقولها في المقابلة", "إذا قالوا عرّف بنفسك… لا تبدأ بـ«أنا شخص طموح»", "job_hunt", "problem"),
    ("رتّب أسبوعك بالـAI", "أسبوعك مرتب في دقيقتين", "أعطه مهامك وخله يرتب لك الأسبوع كامل", "ai_tools", "bold_claim"),
    ("٣ أسئلة آخر المقابلة", "اسألهم هالثلاث أسئلة", "لما يقولون عندك أي سؤال؟ لا تقول لا… اسأل هذي", "job_hunt", "list_number"),
    ("خلّ Copilot يشرح لك الكود", "لا تنسخ الكود… افهمه", "بدل ما تنسخ الكود وتمشي… خله يشرح لك كل سطر", "ai_tools", "bold_claim"),
    ("رسالة LinkedIn لمسؤول التوظيف", "رسالة تجيب رد", "أغلب الرسايل لمسؤولين التوظيف تنقرأ وتنترك… هذي صيغة تجيب رد", "job_hunt", "problem"),
    ("CV صفحة وحدة", "من صفحتين لصفحة", "سيرتك صفحتين وأنت خريج؟ خلنا نختصرها", "cv_method", "pov_before_after"),
    ("تبي تتعلم بايثون؟ ابدأ هنا", "بايثون من الصفر", "تبي تتعلم بايثون وما تدري من وين تبدأ؟", "build_proof", "question"),
    ("الـAI يغلط… تأكد", "لا تصدقه على طول", "الذكاء الاصطناعي يقول كلام واثق… وأحياناً يكون غلط", "ai_tools", "bold_claim"),
    ("بعد التخرج مباشرة: ٣ خطوات", "تخرجت؟ سوّ هذا", "تخرجت وما تدري وش الخطوة الجاية؟ ابدأ بهذي الثلاث", "job_hunt", "list_number"),
    # scheduled x2, qa_passed, rendered
    ("حوّل ملاحظاتك لبطاقات مراجعة", "بطاقات مراجعة بثواني", "عندك ملاحظات كثير؟ خل الـAI يحولها بطاقات مراجعة", "ai_tools", "list_number"),
    ("طريقة STAR بدقيقة", "جاوب أي سؤال سلوكي", "أسئلة المقابلة السلوكية لها طريقة وحدة تريحك: STAR", "job_hunt", "question"),
    ("وريهم شغلك مو شهادتك", "الشهادة لحالها ما تكفي", "الشهادة مهمة… بس اللي يفرقك هو الشغل اللي تقدر توريه", "build_proof", "bold_claim"),
    ("كيف تكتب إنجاز في الـCV", "إنجاز مو مهمة", "لا تكتب وش كانت مهامك… اكتب وش أنجزت", "cv_method", "problem"),
]
HELD_FACTS = ("دروس من هاكاثون AgentX", "الفائز في AgentX", "خلني أقولك وش تعلمت من هاكاثون AgentX", "build_proof", "story")
HELD_SCORE = ("نصايح عامة للمقابلات", "نصايح للمقابلة", "اليوم بعطيكم نصايح عامة للمقابلات", "job_hunt", "list_number")
FAILED = ("الـAI في البحث عن وظيفة", "دوّر وظيفة بذكاء", "كيف تستخدم الذكاء الاصطناعي وأنت تدور وظيفة بدون ما تغش", "job_hunt", "question")

CTA_TEXT = {
    "send": "أرسله لخويك اللي يحتاجه",
    "save": "احفظه عشان ترجع له",
    "follow": "تابع عشان يوصلك الجزء الثاني",
    "comment": "علّق بسؤالك وأرد عليك",
    "none": "",
}
HASHTAGS = {
    "ai_tools": ["#الذكاء_الاصطناعي", "#AI", "#ChatGPT", "#أدوات_ذكاء_اصطناعي", "#تقنية"],
    "job_hunt": ["#وظائف", "#مقابلة_عمل", "#خريجين", "#LinkedIn", "#تطوير_مهني"],
    "build_proof": ["#برمجة", "#مشاريع", "#GitHub", "#بايثون", "#تعلم_البرمجة"],
    "cv_method": ["#سيرة_ذاتية", "#CV", "#وظائف", "#خريجين", "#ATS"],
}
BANDS = {"20-30": (21, 29), "31-45": (32, 44), "46-60": (47, 59), "61-90": (62, 80)}
HOOK_Q = {"list_number": 1.3, "problem": 1.15, "bold_claim": 1.0, "question": 0.9, "pov_before_after": 0.95, "story": 0.9}
BAND_Q = {"20-30": 0.9, "31-45": 1.25, "46-60": 1.05, "61-90": 0.8}
TIME_Q = {"21:30": 1.2, "22:30": 1.05, "18:30": 1.0, "13:30": 0.75}


class DemoError(Exception):
    pass


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _f(h: float) -> float:
    """Share of a reel's lifetime views reached h hours after posting."""
    return 1 - math.exp(-max(h, 0) / 40.0)


def _thumb(path: Path, rid: int, pillar: str, fonts_dir: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    w, h = 360, 640
    tint = {"ai_tools": (70, 38, 120), "job_hunt": (34, 52, 96), "build_proof": (24, 70, 66), "cv_method": (92, 62, 24)}.get(pillar, (60, 40, 90))
    t = np.linspace(0, 1, h)[:, None, None]
    top = np.array(tint, dtype=float)[None, None, :]
    bottom = np.array([15, 15, 18], dtype=float)[None, None, :]
    arr = (top * (1 - t) + bottom * t) * np.ones((1, w, 1))
    img = Image.fromarray(arr.clip(0, 255).astype("uint8"), "RGB")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([28, 470, 332, 482], radius=6, fill=(181, 112, 255))
    d.rounded_rectangle([28, 496, 240, 506], radius=5, fill=(90, 90, 104))
    d.ellipse([130, 170, 230, 270], outline=(181, 112, 255), width=5)
    d.polygon([(166, 196), (166, 244), (206, 220)], fill=(181, 112, 255))

    def font(size: int, bold: bool = True):
        try:
            f = ImageFont.truetype(str(fonts_dir / "Inter[opsz,wght].ttf"), size)
            if bold:
                try:
                    f.set_variation_by_axes([32, 800])
                except Exception:  # noqa: BLE001  # nosec B110 - variation support depends on the FreeType build
                    pass
            return f
        except OSError:
            return ImageFont.load_default()

    d.rounded_rectangle([24, 24, 134, 64], radius=10, fill=(250, 178, 25))
    d.text((79, 44), "DEMO", fill=(20, 20, 22), font=font(24), anchor="mm")
    d.text((28, 540), f"#{rid}", fill=(242, 242, 245), font=font(40), anchor="lm")
    d.text((28, 590), "fake reel · layout review", fill=(170, 170, 182), font=font(18, bold=False), anchor="lm")
    img.save(path)


def _playbook_v1(cfg) -> dict:
    p = cfg.brand_dir / "playbook_v1.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "summary": "Starting playbook", "arms": {}, "experiments": [], "hypotheses": []}


def _prepare_path(db_path: Path, cfg) -> None:
    real = cfg.db_path.resolve()
    if db_path.resolve() == real:
        raise DemoError(f"refusing to seed demo data into the real database {real}")
    if db_path.exists():
        import sqlite3

        try:
            c = sqlite3.connect(str(db_path))
            row = c.execute("SELECT value FROM meta WHERE key='demo'").fetchone()
            c.close()
        except sqlite3.DatabaseError:
            row = None
        if not row or row[0] != "1":
            raise DemoError(f"{db_path} exists and is not a demo database; not touching it")
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()


def seed_demo(db_path: Path, cfg, thumbs_dir: Path, now: datetime | None = None) -> None:
    """Create a fresh demo DB at db_path (replacing an older demo DB) plus PNG thumbnails."""
    db_path = Path(db_path)
    thumbs_dir = Path(thumbs_dir)
    _prepare_path(db_path, cfg)
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(SEED)  # nosec B311 - fake demo data, not security
    rng = np.random.default_rng(SEED)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(second=0, microsecond=0)
    tz = cfg.tz
    today = now.astimezone(tz).date()
    weights = cfg.get("learning.weights", {}) or {}

    def at(d: date, hhmm: str) -> datetime:
        hh, mm = (int(x) for x in hhmm.split(":"))
        return datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).astimezone(timezone.utc)

    def slot_for(d: date, hhmm: str) -> str:
        return f"{'weekend' if d.weekday() in (4, 5) else 'weekday'}-{hhmm}"

    conn = db.connect(db_path)
    reels: list[dict] = []
    events: list[tuple] = []
    insights: list[tuple] = []

    def make_reel(rid: int, content: tuple | None, state: str, *, band: str | None = None, pub: datetime | None = None,
                  sched: datetime | None = None, created: datetime, cta: str = "save", n_tags: int = 5,
                  caption_style: str = "natural_keywords", qa_pass: bool = True, scores: dict | None = None,
                  held_reason: str | None = None, extra: dict | None = None) -> dict:
        r: dict = {
            "id": rid, "sha256": hashlib.sha256(f"demo-{SEED}-{rid}".encode()).hexdigest(),
            "source_name": f"IMG_{4700 + rid * 7}.MOV", "raw_path": f"data/raw/demo_{rid}.mov", "state": state,
            "held_reason": held_reason, "created_at": _iso(created), "updated_at": _iso(pub or min(created + timedelta(minutes=45), now - timedelta(minutes=2))),
            "is_demo": 1, "dry_run": 0, "retries": 0,
        }
        if content is None:
            return r
        title, on_screen, spoken, pillar, hook_type = content
        band = band or rnd.choice(list(BANDS))
        dur = round(rnd.uniform(*BANDS[band]) + rnd.random(), 1)
        src = round(dur * rnd.uniform(1.3, 1.75), 1)
        cuts = int(dur / rnd.uniform(3.2, 5.5))
        tags = HASHTAGS[pillar][:n_tags]
        first = spoken if len(spoken) < 90 else spoken[:88] + "…"
        body = "في هالمقطع أشرح الفكرة خطوة بخطوة، بأمثلة تقدر تطبقها اليوم."
        cta_t = CTA_TEXT.get(cta, "")
        caption = "\n\n".join(x for x in (first, body, cta_t, " ".join(tags)) if x)
        r.update({
            "probe_json": db.jdump({"duration": src, "width": 1080, "height": 1920, "rotation": 0, "fps": 30, "vcodec": "hevc",
                                    "pix_fmt": "yuv420p10le", "is_hdr": False, "has_audio": True, "audio_rate": 48000,
                                    "audio_channels": 1, "size_bytes": int(src * 2_100_000)}),
            "src_duration_s": src,
        })
        if state in ("new", "transcribed"):
            return r
        plan = {
            "keep_segments": list(range(1, cuts + 2)), "drop_word_ids": [], "intervals": [], "output_duration": dur, "cuts": cuts,
            "hook": {"spoken": spoken, "on_screen": on_screen}, "title_card": title, "pillar": pillar, "topic": pillar,
            "hook_type": hook_type, "cover_t_src": 1.2, "cover_t_out": 0.8, "risk_flags": [], "caption_fixes": [],
            "notes": "Demo plan: dropped one retake and two long pauses.",
        }
        if extra and extra.get("risk_flags"):
            plan["risk_flags"] = extra["risk_flags"]
        feats = {
            "pillar": pillar, "topic": pillar, "hook_type": hook_type, "duration_band": band, "duration_s": dur,
            "wpm": rnd.randint(128, 172), "cuts_per_10s": round(cuts / dur * 10, 2), "caption_len": len(caption),
            "hashtags": tags, "hashtag_count": len(tags), "cta_type": cta, "caption_style": caption_style,
            "cover_style": "face_text",
        }
        r.update({
            "plan_json": db.jdump(plan), "duration_s": dur, "features_json": db.jdump(feats), "playbook_version": 1,
            "transcript_path": f"data/work/{rid}/words.json",
        })
        if state == "planned":
            return r
        r["out_path"] = f"data/out/{rid}.mp4"
        r["thumb_path"] = f"{rid}.png"
        r["caption_json"] = db.jdump({"caption": caption, "first_line": first, "body": body, "cta_type": cta, "cta_text": cta_t,
                                      "hashtags": tags, "keywords": ["AI", "الذكاء الاصطناعي", "وظائف"], "caption_style": caption_style})
        sc = scores or {"hook": rnd.choice([7, 8, 8, 9]), "clarity": rnd.choice([8, 8, 9]), "value": rnd.choice([7, 8, 9]), "accuracy": 9}
        checks = [
            {"name": "video_specs", "ok": True, "hard": True, "detail": "1080x1920, 30 fps, H.264 High, yuv420p"},
            {"name": "duration_band", "ok": True, "hard": True, "detail": f"{dur:.1f} s is inside 5-90 s"},
            {"name": "loudness", "ok": True, "hard": True, "detail": f"{-14 + rnd.uniform(-0.4, 0.4):.1f} LUFS (target -14)"},
            {"name": "black_or_frozen_frames", "ok": True, "hard": True, "detail": "none found"},
            {"name": "captions_present", "ok": True, "hard": True, "detail": "burned-in captions on 97% of speech"},
            {"name": "facts_gate", "ok": True, "hard": True, "detail": "no claims about Yazeed outside brand/facts.yaml"},
            {"name": "legal_gate", "ok": True, "hard": True, "detail": "no paid promotion, no service CTA"},
            {"name": "hashtag_count", "ok": True, "hard": True, "detail": f"{len(tags)} of max 5"},
            {"name": "caption_length", "ok": True, "hard": True, "detail": f"{len(caption)} of max 2200 chars"},
        ]
        if extra and extra.get("checks"):
            names = {c["name"]: c for c in extra["checks"]}
            checks = [names.get(c["name"], c) for c in checks] + [c for c in extra["checks"] if c["name"] not in {x["name"] for x in checks}]
        r["qa_json"] = db.jdump({"passed": qa_pass, "hard_fails": [c["name"] for c in checks if not c["ok"] and c["hard"]],
                                 "checks": checks, "scores": sc, "critique": {"summary": "demo critique"}, "at": _iso(created + timedelta(hours=1))})
        if sched:
            r["scheduled_at"] = _iso(sched)
            r["slot_arm"] = slot_for(sched.astimezone(tz).date(), sched.astimezone(tz).strftime("%H:%M"))
            feats.update(slot=r["slot_arm"], weekday=sched.astimezone(tz).strftime("%a"), hour=sched.astimezone(tz).hour)
            r["features_json"] = db.jdump(feats)
        if pub:
            r["published_at"] = _iso(pub)
            r["media_id"] = f"demo-{rid:04d}"
        r["_band"], r["_hook"], r["_dur"], r["_pillar"] = band, hook_type, dur, pillar
        return r

    # ---------------------------------------------------------------- published reels
    offsets = [43, 41, 39, 37, 35, 33, 31, 29, 27, 25, 23, 21, 19, 17, 15, 13, 11, 9, 7, 4, 1]
    ctas = ["save", "save", "send", "save", "send", "follow", "send", "save", "send", "comment", "send", "save",
            "send", "none", "send", "save", "send", "send", "save", "send", "send"]
    rid = 0
    views_final: dict[int, float] = {}
    for k, off in enumerate(offsets):
        rid += 1
        d = today - timedelta(days=off)
        hhmm = rnd.choices(["21:30", "18:30", "22:30", "13:30"], weights=[0.4, 0.25, 0.2, 0.15])[0]
        pub = at(d, hhmm)
        c = CONTENT[k]
        band = rnd.choices(list(BANDS), weights=[0.2, 0.35, 0.3, 0.15])[0]
        r = make_reel(rid, c, "published", band=band, pub=pub, sched=pub, created=pub - timedelta(hours=rnd.uniform(20, 60)),
                      cta=ctas[k], n_tags=3 if k % 4 == 1 else 5, caption_style="short" if k % 3 == 2 else "natural_keywords")
        q = HOOK_Q[c[4]] * BAND_Q[band] * TIME_Q[hhmm] * float(rng.lognormal(0, 0.3))
        growth = 1 + 0.11 * k
        vf = 380 * growth * q
        views_final[rid] = vf
        watch = float(np.clip({"20-30": 0.66, "31-45": 0.58, "46-60": 0.47, "61-90": 0.36}[band] * (0.85 + 0.3 * (q / (q + 1))) + rng.normal(0, 0.04), 0.22, 0.85))
        share_q = (1.35 if ctas[k] == "send" else 1.0) * float(rng.lognormal(0, 0.25))
        save_q = (1.3 if ctas[k] == "save" else 1.0) * float(rng.lognormal(0, 0.25))
        r["_m"] = {"q": q, "watch": watch, "share_q": share_q, "save_q": save_q}
        for cp in CHECKPOINTS:
            due = pub + timedelta(hours=cp)
            if due > now:
                insights.append((rid, r["media_id"], cp, _iso(due), None, 0, None, None, None))
                continue
            frac = min(1.0, _f(cp) * float(rng.uniform(0.96, 1.04)))
            views = round(vf * frac)
            reach = round(views * float(rng.uniform(0.64, 0.74)))
            likes = round(reach * 0.045 * (0.8 + 0.4 * q / (q + 1)))
            comments = round(reach * 0.0045)
            shares = round(reach * 0.011 * share_q)
            saved = round(reach * 0.017 * save_q)
            w_now = watch * (1.08 - 0.08 * frac)
            awt = int(w_now * r["_dur"] * 1000)
            m = {
                "views": views, "reach": reach, "likes": likes, "comments": comments, "shares": shares, "saved": saved,
                "reposts": round(shares * 0.1), "total_interactions": likes + comments + shares + saved,
                "ig_reels_avg_watch_time": awt, "ig_reels_video_view_total_time": awt * views,
                "reels_skip_rate": round(float(np.clip(58 - 45 * (w_now - 0.45) + rng.normal(0, 3), 22, 75)), 1),
                "crossposted_views": None, "facebook_views": None,
            }
            insights.append((rid, r["media_id"], cp, _iso(due), _iso(due + timedelta(minutes=rnd.randint(2, 14))), 1,
                             db.jdump(m), db.jdump({"demo": True}), None))
        reels.append(r)

    # composite score at the 72 h checkpoint (1.0 = account median), then bandit wins
    ins72 = {i[0]: json.loads(i[6]) for i in insights if i[2] == 72 and i[6]}
    rows72 = {}
    for r in reels:
        m = ins72.get(r["id"])
        if not m:
            continue
        rows72[r["id"]] = {
            "watch_ratio": m["ig_reels_avg_watch_time"] / 1000 / r["_dur"],
            "skip_rate": 100 - m["reels_skip_rate"],
            "shares_per_reach": m["shares"] / m["reach"],
            "saves_per_reach": m["saved"] / m["reach"],
            "likes_per_reach": m["likes"] / m["reach"],
            "views_per_follower": m["views"] / max(60 + 20 * r["id"], 1),
        }
    meds = {k: float(np.median([v[k] for v in rows72.values()])) for k in next(iter(rows72.values()))} if rows72 else {}
    for r in reels:
        v = rows72.get(r["id"])
        if not v:
            continue
        comp = sum(float(weights.get(k, 1 / 6)) * (v[k] / meds[k] if meds[k] else 1) for k in v)
        r["composite_score"] = round(comp, 3)
        r["rewarded_at"] = _iso(datetime.fromisoformat(r["published_at"]) + timedelta(hours=72, minutes=10))

    # ---------------------------------------------------------------- the rest of the queue
    def add(content, state, **kw):
        nonlocal rid
        rid += 1
        r = make_reel(rid, content, state, **kw)
        reels.append(r)
        return r

    tomorrow = today + timedelta(days=1)
    s1 = at(tomorrow, "21:30")
    s2 = at(tomorrow + timedelta(days=1), "18:30")
    add(CONTENT[21], "scheduled", band="31-45", sched=s1, created=now - timedelta(hours=30), cta="send")
    add(CONTENT[22], "scheduled", band="46-60", sched=s2, created=now - timedelta(hours=26), cta="save")
    add(CONTENT[23], "qa_passed", band="31-45", created=now - timedelta(hours=5), cta="send")
    add(CONTENT[24], "rendered", band="31-45", created=now - timedelta(hours=2), cta="save")
    add(("", "", "", "job_hunt", "question"), "transcribed", created=now - timedelta(minutes=50))
    add(None, "new", created=now - timedelta(minutes=12))
    held1 = add(HELD_FACTS, "held", band="46-60", created=now - timedelta(hours=20), cta="follow", qa_pass=False,
                held_reason="Facts gate: 'الفائز' next to AgentX (Siyaq placed 3rd)",
                scores={"hook": 8, "clarity": 8, "value": 7, "accuracy": 3},
                extra={"risk_flags": ["award claim about Yazeed"],
                       "checks": [{"name": "facts_gate", "ok": False, "hard": True,
                                   "detail": "on-screen text says 'الفائز في AgentX'; brand/facts.yaml says 3rd place (المركز الثالث)"}]})
    held2 = add(HELD_SCORE, "held", band="61-90", created=now - timedelta(hours=9), cta="save", qa_pass=False,
                held_reason="Critique score 6/10 (hook)",
                scores={"hook": 6, "clarity": 7, "value": 6, "accuracy": 9},
                extra={"checks": [{"name": "critique_score", "ok": False, "hard": True,
                                   "detail": "hook 6/10 is below the pass mark of 7: the opening line is generic and names no problem"}]})
    failed = add(FAILED, "planned", band="31-45", created=now - timedelta(hours=7), cta="send")
    failed.update(state="failed", failed_from="planned", retries=2, last_error="render: ffmpeg exited with code 1 (libass: caption font not found)",
                  next_attempt_at=_iso(now + timedelta(minutes=40)), updated_at=_iso(now - timedelta(minutes=20)))
    for r in reels:
        if r["state"] == "transcribed":
            r.pop("plan_json", None)

    # events (state history)
    flow = ["new", "transcribed", "planned", "rendered", "qa_passed", "scheduled", "publishing", "published"]
    for r in reels:
        created = datetime.fromisoformat(r["created_at"])
        target = r["state"]
        if target == "held":
            path = flow[:4] + ["held"]
        elif target == "failed":
            path = flow[:3] + ["failed"]
        else:
            path = flow[: flow.index(target) + 1]
        end = datetime.fromisoformat(r["published_at"]) if r.get("published_at") else (datetime.fromisoformat(r["updated_at"]))
        span = max((end - created).total_seconds(), 600)
        prev = None
        for i, st in enumerate(path):
            if st == "published":
                ts = end
            elif st == "publishing" and r.get("published_at"):
                ts = end - timedelta(minutes=3)
            else:
                ts = created + timedelta(seconds=span * min(i / max(len(path) - 1, 1), 0.3 if st in ("scheduled",) else 1) if st != "new" else 0)
                if st in ("transcribed", "planned", "rendered", "qa_passed"):
                    ts = created + timedelta(minutes=[0, 6, 9, 17, 19][flow.index(st)] + 2)
            detail = {"new": f"intake {r['source_name']}", "transcribed": "faster-whisper large-v3 (cuda)", "planned": "edit plan ok",
                      "rendered": "render ok", "qa_passed": "all gates passed", "scheduled": f"slot {r.get('slot_arm') or ''}".strip(),
                      "publishing": "container created", "published": "read back from /me/media",
                      "held": r.get("held_reason") or "", "failed": r.get("last_error") or ""}[st]
            events.append((r["id"], _iso(ts), prev, st, detail))
            prev = st
        if target == "failed":
            events.append((r["id"], _iso(now - timedelta(minutes=20)), None, None, "retry 2 of 4 scheduled"))

    # ---------------------------------------------------------------- write reels, events, insights
    cols = sorted({k for r in reels for k in r if not k.startswith("_")})
    with db.tx(conn):
        for r in reels:
            vals = [r.get(c) for c in cols]
            conn.execute(f"INSERT INTO reels({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})", vals)  # nosec B608 - fixed demo columns
        conn.executemany("INSERT INTO events(reel_id, ts, from_state, to_state, detail) VALUES(?,?,?,?,?)", events)
        conn.executemany(
            "INSERT INTO insights(reel_id, media_id, checkpoint_h, due_at, fetched_at, attempts, metrics_json, raw_json, error) VALUES(?,?,?,?,?,?,?,?,?)",
            insights,
        )

        # ------------------------------------------------------------ account insights (daily)
        first = today - timedelta(days=45)
        pubs = [(datetime.fromisoformat(r["published_at"]), views_final[r["id"]]) for r in reels if r.get("published_at")]
        ndays = (today - first).days
        for i in range(ndays + 1):
            d = first + timedelta(days=i)
            day_start = at(d, "00:00")
            day_end = day_start + timedelta(days=1)
            v = 25.0 + sum(vf * (_f((min(day_end, now) - p).total_seconds() / 3600) - _f((day_start - p).total_seconds() / 3600))
                           for p, vf in pubs if p < min(day_end, now))
            fol = int(round(860 * (i / ndays) ** 1.7 + rnd.uniform(-4, 4))) if i else 0
            m = {"reach": round(v * 0.68), "views": round(v), "accounts_engaged": round(v * 0.05),
                 "profile_links_taps": int(rng.poisson(max(v * 0.0016, 0.05))), "likes": round(v * 0.03),
                 "comments": round(v * 0.003), "shares": round(v * 0.008), "saves": round(v * 0.012)}
            conn.execute(
                "INSERT INTO account_insights(day, fetched_at, follower_count, metrics_json, raw_json) VALUES(?,?,?,?,?)",
                (d.isoformat(), _iso(at(d, "03:31") + timedelta(days=1)) if d < today else _iso(now), fol if fol >= 100 else None, db.jdump(m), None),
            )

        # ------------------------------------------------------------ playbooks v1 -> v3
        pb1 = _playbook_v1(cfg)
        last_sat = today - timedelta(days=(today.weekday() - 5) % 7)
        v1_at = at(first, "09:00")
        v2_at = at(last_sat - timedelta(days=14), "20:10")
        v3_at = at(last_sat, "20:10")
        ch2 = [
            {"field": "defaults.cta_type", "before": "save", "after": "send",
             "evidence": {"n": 8, "effect_size": 0.41, "metric": "shares_per_reach"}, "confidence": 0.72, "status": "applied",
             "reason": "Reels that asked viewers to send them to a friend got more shares per reach (n=8 vs 6)."},
            {"field": "caption_style.first_line_max_chars", "before": 125, "after": 90,
             "evidence": {"n": 10, "effect_size": 0.18, "metric": "reach"}, "confidence": 0.55, "status": "applied",
             "reason": "Shorter first lines kept slightly more non-follower reach."},
            {"field": "defaults.hashtag_count", "before": 5, "after": 3,
             "evidence": {"n": 2, "effect_size": 0.35, "metric": "reach"}, "confidence": 0.2, "status": "rejected",
             "reason": "n=2 < 3: only two 3-hashtag reels so far, keep logging."},
        ]
        ch3 = [
            {"field": "defaults.target_band", "before": "46-60", "after": "31-45",
             "evidence": {"n": 7, "effect_size": 0.38, "metric": "watch_ratio"}, "confidence": 0.74, "status": "applied",
             "reason": "31-45 s reels held a higher share of their length than 46-60 s reels (n=7 vs 6)."},
            {"field": "hooks.priority", "before": ["problem", "bold_claim", "list_number"], "after": ["list_number", "problem", "bold_claim"],
             "evidence": {"n": 6, "effect_size": 0.29, "metric": "reels_skip_rate"}, "confidence": 0.61, "status": "applied",
             "reason": "Number/list hooks had the lowest skip rate, so the brief leads with them."},
            {"field": "slots.weekday-13:30", "before": "normal", "after": "explore only",
             "evidence": {"n": 4, "effect_size": -0.31, "metric": "composite"}, "confidence": 0.58, "status": "applied",
             "reason": "Weekday 13:30 posts scored below the median 3 times out of 4."},
            {"field": "topics.cv_method.share", "before": 0.25, "after": 0.35,
             "evidence": {"n": 2, "effect_size": 0.44, "metric": "composite"}, "confidence": 0.25, "status": "rejected",
             "reason": "n=2 < 3 per arm: promising, but too few CV-method reels to move the mix."},
        ]
        pb2 = dict(pb1, version=2, created_at=v2_at.date().isoformat(), summary="First review: the send CTA and shorter first lines are in; the 3-hashtag test needs more reels.")
        pb2["defaults"] = dict(pb1.get("defaults", {}), cta_type="send")
        pb3 = dict(pb2, version=3, created_at=v3_at.date().isoformat(), summary="31-45 s is the new default length, list hooks move up, weekday 13:30 becomes explore-only.")
        pb3["defaults"] = dict(pb2["defaults"], target_band="31-45")
        conn.executemany(
            "INSERT INTO playbooks(version, created_at, json, changes_json, summary, path) VALUES(?,?,?,?,?,?)",
            [
                (1, _iso(v1_at), db.jdump(pb1), db.jdump([]), pb1.get("summary", "Starting playbook"), "data/playbooks/v1.json"),
                (2, _iso(v2_at), db.jdump(pb2), db.jdump(ch2), pb2["summary"], "data/playbooks/v2.json"),
                (3, _iso(v3_at), db.jdump(pb3), db.jdump(ch3), pb3["summary"], "data/playbooks/v3.json"),
            ],
        )
        for r in reels:
            if r.get("published_at"):
                p = datetime.fromisoformat(r["published_at"])
                ver = 3 if p >= v3_at else (2 if p >= v2_at else 1)
                conn.execute("UPDATE reels SET playbook_version=? WHERE id=?", (ver, r["id"]))
            elif r.get("plan_json"):
                conn.execute("UPDATE reels SET playbook_version=3 WHERE id=?", (r["id"],))

        # ------------------------------------------------------------ bandit arms + decisions
        arms = pb1.get("arms", {})
        priors = pb1.get("priors", {})
        stats: dict[tuple[str, str], list[int]] = {}
        for r in reels:
            if r.get("composite_score") is None:
                continue
            f = json.loads(r["features_json"])
            win = int(r["composite_score"] > 1.0)
            for dim, arm in (("slot", f.get("slot")), ("hook_type", f.get("hook_type")), ("duration_band", f.get("duration_band"))):
                s = stats.setdefault((dim, arm), [0, 0])
                s[0] += 1
                s[1] += win
        for dim, lst in arms.items():
            for a in lst:
                n, wins = stats.get((dim, a["id"]), [0, 0])
                p = priors.get(dim, {}).get(a["id"], {})
                conn.execute(
                    "INSERT INTO bandit_arms(dimension, arm, alpha, beta, n, wins, updated_at) VALUES(?,?,?,?,?,?,?)",
                    (dim, a["id"], p.get("alpha", 1) + wins, p.get("beta", 1) + n - wins, n, wins, _iso(v3_at)),
                )
        for r in reels:
            if not r.get("scheduled_at"):
                continue
            f = json.loads(r["features_json"])
            ts = datetime.fromisoformat(r["scheduled_at"]) - timedelta(hours=rnd.uniform(3, 20))
            for dim in ("slot", "hook_type", "duration_band"):
                explore = int(rnd.random() < 0.3)
                conn.execute(
                    "INSERT INTO decisions(ts, reel_id, dimension, arm, explore, detail_json) VALUES(?,?,?,?,?,?)",
                    (_iso(ts), r["id"], dim, f.get(dim), explore, db.jdump({"policy": "thompson", "sampled": round(rnd.uniform(0.3, 0.8), 3)})),
                )

        # ------------------------------------------------------------ experiments
        hyps = {h["id"]: h for h in pb1.get("hypotheses", [])}
        exp_rows = []
        results = {"E6": ("concluded", "Send beat save on shares per reach (+41%, n=8 vs 6). Adopted in v2."),
                   "E7": ("parked", None), "E8": ("next", None)}
        for e in pb1.get("experiments", []):
            st, res = results.get(e["id"], (e.get("status"), None))
            h = hyps.get(e.get("hypothesis"), {})
            exp_rows.append((e["id"], h.get("text", e.get("hypothesis", "")), e.get("dimension"), db.jdump([]), st,
                             _iso(v1_at) if st != "next" else None, _iso(v2_at) if st == "concluded" else None, res))
        exp_rows.append(("E9", "Cover style: face + text vs text only.", "cover_style", db.jdump(["face_text", "text_only"]), "dropped",
                         _iso(v1_at), _iso(v2_at), "Dropped: every cover so far used face + text, so there is nothing to compare yet."))
        conn.executemany("INSERT INTO experiments(id, hypothesis, dimension, arms_json, status, started_at, ended_at, result) VALUES(?,?,?,?,?,?,?,?)", exp_rows)

        # ------------------------------------------------------------ brief
        week_start = today - timedelta(days=(today.weekday() + 1) % 7)  # Sunday
        scripts = [
            {"n": 1, "title": "٣ طرق تتدرب فيها على المقابلة لحالك", "pillar": "job_hunt", "hook_type": "list_number", "duration_band": "31-45",
             "target_length_s": 40, "hook_line": "ثلاث طرق تتدرب فيها على المقابلة… وأنت في غرفتك",
             "on_screen": "تدرّب على المقابلة لحالك",
             "beats": ["سجّل نفسك وأنت تجاوب، وشوف وين تتلعثم", "خل الـAI يسألك أسئلة على نفس الوصف الوظيفي", "جاوب بصوت عالي بطريقة STAR"],
             "cta": "أرسله لخويك اللي عنده مقابلة", "cta_type": "send",
             "shot_note": "Desk, window light from the left, phone at eye level, one step back so hands show.", "experiment": "E1"},
            {"n": 2, "title": "خليت الـAI يكتب الـCover Letter؟", "pillar": "ai_tools", "hook_type": "question", "duration_band": "31-45",
             "target_length_s": 38, "hook_line": "تخلي الـAI يكتب لك الـCover Letter؟ طيب انتبه لهالشي",
             "on_screen": "الـAI والـCover Letter",
             "beats": ["أعطه الوصف الوظيفي وإنجازاتك الحقيقية بس", "اطلب منه يختصرها لربع صفحة", "عدّل النبرة لين تصير تشبهك أنت"],
             "cta": "احفظه لين تقدّم", "cta_type": "save",
             "shot_note": "Standing, plain wall, natural light; show the laptop screen for 2 s in beat 1.", "experiment": "E2"},
            {"n": 3, "title": "عنوانك في LinkedIn يضيع عليك فرص", "pillar": "job_hunt", "hook_type": "problem", "duration_band": "46-60",
             "target_length_s": 50, "hook_line": "عنوانك في LinkedIn مكتوب فيه «طالب»؟ هذا يضيع عليك فرص",
             "on_screen": "غيّر عنوانك في LinkedIn",
             "beats": ["العنوان هو أول شي يطلع في البحث", "اكتب المجال والمهارة بدل «طالب»", "مثال قبل وبعد على الشاشة"],
             "cta": "تابع عشان الجزء الثاني عن الـAbout", "cta_type": "follow",
             "shot_note": "Seated, over-the-shoulder B-roll of the profile edit screen (blur any real names).", "experiment": "E1"},
            {"n": 4, "title": "مشروع ويكند يفرق في سيرتك", "pillar": "build_proof", "hook_type": "bold_claim", "duration_band": "31-45",
             "target_length_s": 42, "hook_line": "مشروع واحد تسويه في ويكند يفرق في سيرتك أكثر من شهادة",
             "on_screen": "مشروع ويكند > شهادة",
             "beats": ["اختر مشكلة صغيرة تعرفها", "ابنها بأبسط أداة وارفعها على GitHub", "اكتب سطرين في سيرتك: وش المشكلة ووش النتيجة"],
             "cta": "أرسله لأحد يدور خبرة", "cta_type": "send",
             "shot_note": "Desk with the laptop open, warm lamp behind, medium close-up.", "experiment": "E2"},
            {"n": 5, "title": "٣ أوامر AI تختصر عليك Excel", "pillar": "ai_tools", "hook_type": "list_number", "duration_band": "20-30",
             "target_length_s": 28, "hook_line": "ثلاث أوامر تخلي الـAI يسوي شغل Excel عنك",
             "on_screen": "Excel بدون صداع",
             "beats": ["اطلب منه المعادلة بالعربي", "خله يشرح لك الخطأ في الجدول", "اطلب منه ملخص للأرقام في ثلاث نقاط"],
             "cta": "احفظه للمشروع الجاي", "cta_type": "save",
             "shot_note": "Screen recording for beats 1-3 with your face in a corner; keep it under 30 s.", "experiment": "E2"},
            {"n": 6, "title": "قبل وبعد: نقطة وحدة في الـCV", "pillar": "cv_method", "hook_type": "pov_before_after", "duration_band": "31-45",
             "target_length_s": 36, "hook_line": "هذي نقطة من سيرة خريج… وهذي نفس النقطة بعد دقيقة تعديل",
             "on_screen": "نقطة وحدة… قبل وبعد",
             "beats": ["اقرأ النقطة قبل التعديل", "ابدأ بفعل وحط رقم أو نتيجة", "اقرأ النقطة بعد التعديل"],
             "cta": "علّق بنقطة من سيرتك وأعدلها لك", "cta_type": "comment",
             "shot_note": "Hold a printed CV page to camera for the before/after; bright, even light.", "experiment": "E1"},
        ]
        brief = {"week_start": week_start.isoformat(), "generated_at": _iso(v3_at + timedelta(minutes=25)), "scripts": scripts,
                 "notes": "Demo brief: list hooks lead on skip rate, so two of six scripts use them; script 5 keeps the 20-30 s band explored."}
        conn.execute("INSERT INTO briefs(week_start, created_at, json, path) VALUES(?,?,?,?)",
                     (week_start.isoformat(), _iso(v3_at + timedelta(minutes=25)), db.jdump(brief), None))

        # ------------------------------------------------------------ ticks (last ~3 days, every 15 min)
        start = now - timedelta(days=3)
        start = start.replace(minute=(start.minute // 15) * 15)
        t = start
        errs = {37: "brain: edit_plan timed out after 300 s (retry scheduled)", 121: "insights: HTTP 500 from graph.instagram.com (retry in 15 min)",
                190: "intake: file locked by OneDrive sync, will retry", 262: "render: ffmpeg exited with code 1 (libass: caption font not found)"}
        long_ticks = {36, 58, 120, 150, 204, 261}
        paused = {95, 96, 97}
        i = 0
        tick_rows = []
        while t <= now - timedelta(minutes=1):
            if i in errs:
                st, dur, err = "error", rnd.uniform(20, 320), errs[i]
            elif i in paused:
                st, dur, err = "paused", rnd.uniform(0.5, 1.5), None
            elif i - 1 in long_ticks and rnd.random() < 0.7:
                st, dur, err = "locked", 0.3, None
            elif i in long_ticks:
                st, dur, err = "ok", rnd.uniform(180, 420), None
            else:
                st, dur, err = "ok", float(np.clip(rng.normal(7, 2.2), 2.5, 16)), None
            summ = {"intake": 0, "processed": 1 if i in long_ticks else 0, "published": 0, "insights": rnd.randint(0, 3)}
            tick_rows.append(("tick", _iso(t + timedelta(seconds=4)), _iso(t + timedelta(seconds=4 + dur)), st, db.jdump(summ), err))
            t += timedelta(minutes=15)
            i += 1
        for back in range(0, 4):
            d = today - timedelta(days=back)
            dt = at(d, "03:30")
            if dt < now:
                tick_rows.append(("daily", _iso(dt), _iso(dt + timedelta(seconds=rnd.uniform(40, 90))), "ok", db.jdump({"backup": True}), None))
        tick_rows.append(("weekly", _iso(at(last_sat, "20:00")), _iso(at(last_sat, "20:00") + timedelta(minutes=11)), "ok", db.jdump({"playbook": 3}), None))
        conn.executemany("INSERT INTO ticks(kind, started_at, finished_at, status, summary_json, error) VALUES(?,?,?,?,?,?)", tick_rows)

        # ------------------------------------------------------------ brain calls (7 days)
        calls = []
        for k in range(46):
            ts = now - timedelta(hours=rnd.uniform(0, 7 * 24))
            task = rnd.choices(["edit_plan", "caption", "critique", "learner", "brief", "trends"], weights=[10, 10, 10, 1, 1, 1])[0]
            ok = 0 if k in (5, 29) else 1
            err = None if ok else ("timeout after 300 s" if k == 5 else "schema validation failed after 3 attempts")
            calls.append((_iso(ts), task, "sonnet", rnd.randint(1, 30), round(rnd.uniform(18, 140) if ok else 300.0, 1), 1 if ok else 3, ok, err))
        conn.executemany("INSERT INTO brain_calls(ts, task, model, reel_id, duration_s, attempts, ok, error) VALUES(?,?,?,?,?,?,?,?)", calls)

        # ------------------------------------------------------------ notifications
        last_pub = max((r for r in reels if r.get("published_at")), key=lambda r: r["published_at"])
        notes = [
            (_iso(now - timedelta(minutes=20)), "error", "Render failed, retrying", "ffmpeg exited with code 1", failed["id"]),
            (_iso(now - timedelta(hours=9)), "action", "Reel held: critique score 6/10 (hook)", "Refilm the opening line or approve a rewrite.", held2["id"]),
            (_iso(now - timedelta(hours=20)), "action", "Reel held by the facts gate", "'الفائز' next to AgentX; Siyaq placed 3rd.", held1["id"]),
            (last_pub["published_at"], "info", "Reel posted", "Published and read back from /me/media.", last_pub["id"]),
            (_iso(v3_at + timedelta(minutes=25)), "info", "Filming brief ready", f"6 scripts for the week of {week_start.isoformat()}.", None),
            (_iso(v3_at + timedelta(minutes=12)), "info", "Playbook v3 written", "3 changes applied, 1 rejected (n=2 < 3).", None),
            (_iso(now - timedelta(days=3, hours=2)), "info", "Token refreshed", "Valid for another 60 days.", None),
        ]
        conn.executemany("INSERT INTO notifications(ts, level, title, body, reel_id, channels_json) VALUES(?,?,?,?,?,?)",
                         [(a, b, c, d, e, db.jdump(["toast", "dashboard"])) for a, b, c, d, e in notes])

        # ------------------------------------------------------------ token, API usage, backups, orders, meta
        conn.execute(
            "UPDATE token_state SET status='ok', issued_at=?, expires_at=?, last_refresh_at=?, last_check_at=?, ig_user_id=?, username=?, account_type=?, detail=? WHERE id=1",
            (_iso(now - timedelta(days=20)), _iso(now + timedelta(days=40)), _iso(now - timedelta(days=20)), _iso(now - timedelta(hours=2)),
             "demo", "demo_account", "CREATOR", "demo token state (no real token)"),
        )
        usage = []
        for k in range(40):
            ts = now - timedelta(hours=k * 1.8)
            usage.append((_iso(ts), "/me/content_publishing_limit" if k % 5 == 0 else "/{media-id}/insights", 200,
                          round(rnd.uniform(1.5, 9.5), 1), 1 if k % 5 == 0 else None, 100 if k % 5 == 0 else None, None))
        conn.executemany("INSERT INTO api_usage(ts, endpoint, http_status, app_usage_pct, quota_usage, quota_total, detail) VALUES(?,?,?,?,?,?,?)", usage)
        for back in range(7):
            dt = at(today - timedelta(days=back), "03:31")
            if dt < now:
                conn.execute("INSERT INTO backups(ts, path, size_bytes, onedrive_copy, ok) VALUES(?,?,?,?,?)",
                             (_iso(dt), f"data/backups/engine-{(today - timedelta(days=back)).isoformat()}.sqlite",
                              1_350_000 - back * 21_000, "Reel Engine Backups (demo)", 1))
        conn.execute("INSERT INTO orders(day, amount_sar, item, note, created_at, is_demo) VALUES(?,?,?,?,?,1)",
                     ((today - timedelta(days=3)).isoformat(), 0.0, "Test order (demo)", "Layout check only, not a sale", _iso(now - timedelta(days=3))))
        db.meta_set(conn, "demo", "1")
        db.meta_set(conn, "demo_seeded_at", _iso(now))
    conn.close()

    # thumbnails (only for reels that reached the render stage)
    for r in reels:
        if r.get("thumb_path"):
            _thumb(thumbs_dir / r["thumb_path"], r["id"], r.get("_pillar") or "", cfg.fonts_dir)
