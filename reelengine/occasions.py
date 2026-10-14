"""Saudi occasions calendar (feature 2026-10-14, see docs/DECISIONS_PENDING.md).

- The seed is `occasions.yaml` in the brand pack (falls back to the generic `brand/occasions.yaml`).
  It is copied into the `occasions` table once; afterwards the DB is the source of truth. Re-seeding
  only adds slugs the DB has never seen, so Yazeed's edits and deletions (tombstones) stick.
- Hijri-based days are converted with the Umm al-Qura calendar (`hijridate`, pinned). Religious dates
  are finally fixed by moon sighting, so every Hijri occurrence is flagged `approximate`.
- `upcoming()` feeds the weekly filming brief and the daily reminder (look-ahead 1-3 weeks);
  `window_for_reel()` lets the scheduler place a reel about an occasion just before / on its day.
- Nothing here talks to the network.
"""
from __future__ import annotations

import calendar as _cal
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml
from hijridate import Gregorian, Hijri

from . import db
from .config import PACKAGE_ROOT
from .log import get_logger

log = get_logger("occasions")

RULES = ("gregorian", "hijri", "once")
CATEGORIES = ("national", "religious", "international", "tech", "career")
CATEGORY_LABELS = {"national": "National", "religious": "Religious", "international": "International", "tech": "Tech", "career": "Career"}
HIJRI_MONTHS_EN = ("Muharram", "Safar", "Rabi al-Awwal", "Rabi al-Thani", "Jumada al-Ula", "Jumada al-Akhirah",
                   "Rajab", "Shaban", "Ramadan", "Shawwal", "Dhu al-Qadah", "Dhu al-Hijjah")
HIJRI_MONTHS_AR = ("محرم", "صفر", "ربيع الأول", "ربيع الآخر", "جمادى الأولى", "جمادى الآخرة",
                   "رجب", "شعبان", "رمضان", "شوال", "ذو القعدة", "ذو الحجة")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
FIELDS = ("slug", "name_ar", "name_en", "rule", "month", "day", "year", "duration_days", "category", "official",
          "angle", "notes", "source_url", "checked_on", "keywords")
MAX_TEXT = {"name_ar": 80, "name_en": 80, "angle": 400, "notes": 600, "source_url": 400}


class OccasionError(ValueError):
    """Invalid occasion input (shown to Yazeed as is)."""


@dataclass
class Occurrence:
    occasion: dict
    start: date
    end: date
    approximate: bool
    hijri: str = ""          # e.g. "1 Ramadan 1448"
    hijri_ar: str = ""
    days_until: int = 0
    film_by: date | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        o = self.occasion
        return {
            "slug": o["slug"], "name_ar": o["name_ar"], "name_en": o["name_en"], "category": o["category"],
            "official": bool(o["official"]), "angle": o.get("angle") or "", "notes": o.get("notes") or "",
            "source_url": o.get("source_url") or "", "checked_on": o.get("checked_on") or "",
            "start": self.start.isoformat(), "end": self.end.isoformat(), "approximate": self.approximate,
            "hijri": self.hijri, "hijri_ar": self.hijri_ar, "days_until": self.days_until,
            "film_by": self.film_by.isoformat() if self.film_by else None,
        }


# ------------------------------------------------------------------ seed
def seed_path(cfg) -> Path:
    own = cfg.brand_dir / "occasions.yaml"
    return own if own.exists() else PACKAGE_ROOT / "brand" / "occasions.yaml"


def load_seed(path: Path) -> list[dict]:
    items = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("occasions", []) or []
    out = []
    for raw in items:
        out.append(validate(dict(raw)))
    slugs = [o["slug"] for o in out]
    if len(slugs) != len(set(slugs)):
        raise OccasionError(f"duplicate slug in {path.name}")
    return out


def ensure_seeded(conn, cfg, path: Path | None = None) -> int:
    """Insert seed occasions whose slug the DB has never seen (deleted ones included). Returns the count added."""
    if not cfg.get("calendar.enabled", True):
        return 0
    p = path or seed_path(cfg)
    if not p.exists():
        return 0
    added = 0
    known = {r["slug"] for r in conn.execute("SELECT slug FROM occasions")}
    todo = [o for o in load_seed(p) if o["slug"] not in known]
    if not todo:
        return 0
    with db.tx(conn):
        for o in todo:
            _insert(conn, o, origin="seed")
            added += 1
    log.info("calendar: seeded %d occasions from %s", added, p.name)
    return added


# ------------------------------------------------------------------ validation
def _int(v: Any, name: str) -> int:
    if isinstance(v, bool):
        raise OccasionError(f"{name} must be a number")
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        raise OccasionError(f"{name} must be a number") from None


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:50]
    return s if len(s) >= 2 else ""


def validate(d: dict, *, partial: bool = False) -> dict:
    """Normalise and check one occasion dict. Raises OccasionError with a human message."""
    out: dict[str, Any] = {}
    for k in ("name_ar", "name_en", "angle", "notes", "source_url"):
        if k in d and d[k] is not None:
            v = str(d[k]).strip()
            if len(v) > MAX_TEXT[k]:
                raise OccasionError(f"{k} is too long (max {MAX_TEXT[k]} characters)")
            if any(ord(c) < 32 and c not in "\n\t" for c in v):
                raise OccasionError(f"{k} has control characters")
            out[k] = v
    if not partial or "name_en" in d or "name_ar" in d:
        if not out.get("name_en") and not partial:
            raise OccasionError("name_en is required")
        if not out.get("name_ar") and not partial:
            raise OccasionError("name_ar is required")
    if "slug" in d and d["slug"]:
        slug = str(d["slug"]).strip().lower()
        if not SLUG.match(slug):
            raise OccasionError("slug: lowercase letters, digits and dashes only (2-63)")
        out["slug"] = slug
    elif not partial:
        out["slug"] = slugify(out.get("name_en", ""))
        if not out["slug"]:
            raise OccasionError("give an English name (it makes the id)")
    if "rule" in d or not partial:
        rule = str(d.get("rule", "")).strip().lower()
        if rule not in RULES:
            raise OccasionError(f"rule must be one of {', '.join(RULES)}")
        out["rule"] = rule
    if "category" in d or not partial:
        cat = str(d.get("category", "")).strip().lower()
        if cat not in CATEGORIES:
            raise OccasionError(f"category must be one of {', '.join(CATEGORIES)}")
        out["category"] = cat
    for k in ("month", "day"):
        if k in d or not partial:
            out[k] = _int(d.get(k), k)
    if "year" in d and d["year"] not in (None, ""):
        out["year"] = _int(d["year"], "year")
    elif not partial:
        out["year"] = None
    if "duration_days" in d and d["duration_days"] not in (None, ""):
        out["duration_days"] = _int(d["duration_days"], "duration_days")
    elif not partial:
        out["duration_days"] = 1
    if "official" in d or not partial:
        out["official"] = _bool(d.get("official", False))
    if "checked_on" in d and d["checked_on"] not in (None, ""):
        c = str(d["checked_on"]).strip()
        try:
            date.fromisoformat(c)
        except ValueError:
            raise OccasionError("checked_on must be YYYY-MM-DD") from None
        out["checked_on"] = c
    if "source_url" in out and out["source_url"] and not re.match(r"^https://[^\s<>\"']+$", out["source_url"]):
        raise OccasionError("source_url must start with https:// (or leave it empty)")
    if "keywords" in d and d["keywords"] is not None:
        kw = d["keywords"]
        if isinstance(kw, str):
            kw = [x for x in re.split(r"[,،\n]", kw)]
        kw = [str(x).strip() for x in kw if str(x).strip()]
        if len(kw) > 12 or any(len(x) > 40 for x in kw):
            raise OccasionError("keywords: at most 12, each up to 40 characters")
        out["keywords"] = kw
    elif not partial:
        out["keywords"] = []
    return out


def _check_dates(o: dict) -> None:
    rule, m, dd = o["rule"], o["month"], o["day"]
    if not 1 <= m <= 12:
        raise OccasionError("month must be 1-12")
    if not 1 <= o.get("duration_days", 1) <= 60:
        raise OccasionError("duration_days must be 1-60")
    if rule == "hijri":
        if not 1 <= dd <= 30:
            raise OccasionError("Hijri day must be 1-30")
        if o.get("year"):
            raise OccasionError("a Hijri day repeats every year; leave year empty")
    elif rule == "gregorian":
        if o.get("year"):
            raise OccasionError("a fixed Gregorian day repeats every year; leave year empty (use 'once' for a dated event)")
        if not 1 <= dd <= _cal.monthrange(2028, m)[1]:  # a leap year allows 29 Feb
            raise OccasionError("day is out of range for that month")
    else:
        y = o.get("year")
        if not y or not 2000 <= y <= 2100:
            raise OccasionError("a one-off date needs a year (2000-2100)")
        try:
            date(y, m, dd)
        except ValueError:
            raise OccasionError("that date does not exist") from None


# ------------------------------------------------------------------ CRUD
def _insert(conn, o: dict, origin: str = "user", is_demo: bool = False) -> int:
    _check_dates(o)
    ts = db.now_iso()
    cur = conn.execute(
        "INSERT INTO occasions(slug, name_ar, name_en, rule, month, day, year, duration_days, category, official, angle, notes, "
        "source_url, checked_on, keywords_json, origin, deleted, is_demo, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
        (o["slug"], o["name_ar"], o["name_en"], o["rule"], o["month"], o["day"], o.get("year"), o.get("duration_days", 1),
         o["category"], int(bool(o["official"])), o.get("angle") or "", o.get("notes") or "", o.get("source_url") or "",
         o.get("checked_on") or "", db.jdump(o.get("keywords") or []), origin, int(is_demo), ts, ts),
    )
    return int(cur.lastrowid)


def add(conn, data: dict, is_demo: bool = False) -> dict:
    o = validate(data)
    row = conn.execute("SELECT deleted FROM occasions WHERE slug=?", (o["slug"],)).fetchone()
    if row is not None and not row["deleted"]:
        raise OccasionError(f"an occasion with id '{o['slug']}' already exists; edit it instead")
    with db.tx(conn):
        if row is not None:  # re-adding a deleted one replaces its tombstone
            conn.execute("DELETE FROM occasions WHERE slug=?", (o["slug"],))
        _insert(conn, o, origin="user", is_demo=is_demo)
    return get(conn, o["slug"])


def get(conn, slug: str) -> dict | None:
    r = conn.execute("SELECT * FROM occasions WHERE slug=? AND deleted=0", (str(slug),)).fetchone()
    return _row(r) if r else None


def _row(r) -> dict:
    d = dict(r)
    d["official"] = bool(d["official"])
    try:
        d["keywords"] = json.loads(d.get("keywords_json") or "[]")
    except ValueError:
        d["keywords"] = []
    return d


def edit(conn, slug: str, data: dict) -> dict:
    cur = get(conn, slug)
    if cur is None:
        raise OccasionError(f"no occasion '{slug}'")
    data = {k: v for k, v in data.items() if k in FIELDS and k != "slug"}
    upd = validate(data, partial=True)
    merged = {**cur, **upd}
    if merged["rule"] != "once":
        merged["year"] = None
    _check_dates(merged)
    sets = {k: merged[k] for k in ("name_ar", "name_en", "rule", "month", "day", "year", "duration_days", "category",
                                   "angle", "notes", "source_url", "checked_on")}
    sets["official"] = int(bool(merged["official"]))
    sets["keywords_json"] = db.jdump(merged.get("keywords") or [])
    sets["updated_at"] = db.now_iso()
    cols = ", ".join(f"{k}=?" for k in sets)
    with db.tx(conn):
        conn.execute(f"UPDATE occasions SET {cols} WHERE slug=? AND deleted=0", (*sets.values(), slug))  # nosec B608 - fixed column names
    return get(conn, slug)


def delete(conn, slug: str) -> bool:
    """Tombstone (so a seed day doesn't come back on reseed). Returns False if not found."""
    with db.tx(conn):
        cur = conn.execute("UPDATE occasions SET deleted=1, updated_at=? WHERE slug=? AND deleted=0", (db.now_iso(), str(slug)))
    return cur.rowcount > 0


def all_occasions(conn, include_demo: bool = True) -> list[dict]:
    q = "SELECT * FROM occasions WHERE deleted=0" + ("" if include_demo else " AND is_demo=0") + " ORDER BY month, day, slug"  # nosec B608 - constant clause
    try:
        return [_row(r) for r in conn.execute(q)]
    except Exception:  # noqa: BLE001 - an old DB without the table
        return []


# ------------------------------------------------------------------ dates
def hijri_label(d: date) -> tuple[str, str]:
    try:
        h = Gregorian(d.year, d.month, d.day).to_hijri()
    except (ValueError, OverflowError):
        return "", ""
    return f"{h.day} {HIJRI_MONTHS_EN[h.month - 1]} {h.year}", f"{h.day} {HIJRI_MONTHS_AR[h.month - 1]} {h.year}"


def occurrences_of(o: dict, start: date, end: date) -> list[tuple[date, bool]]:
    """(first day, approximate) of every occurrence of `o` that overlaps [start, end]."""
    dur = max(1, int(o.get("duration_days") or 1))
    out: list[tuple[date, bool]] = []
    if o["rule"] == "once":
        try:
            d = date(int(o["year"]), int(o["month"]), int(o["day"]))
        except (TypeError, ValueError):
            return []
        cand = [(d, False)]
    elif o["rule"] == "gregorian":
        cand = []
        for y in range(start.year - 1, end.year + 1):
            m, dd = int(o["month"]), int(o["day"])
            if m == 2 and dd == 29 and not _cal.isleap(y):
                dd = 28
            try:
                cand.append((date(y, m, dd), False))
            except ValueError:
                continue
    else:
        try:
            hy0 = Gregorian(start.year, start.month, start.day).to_hijri().year - 1
            hy1 = Gregorian(end.year, end.month, end.day).to_hijri().year + 1
        except (ValueError, OverflowError):  # outside the Umm al-Qura table (about 1924-2077)
            return []
        cand = []
        for hy in range(hy0, hy1 + 1):
            m, dd = int(o["month"]), int(o["day"])
            try:
                g = Hijri(hy, m, min(dd, Hijri(hy, m, 1).month_length())).to_gregorian()
            except (ValueError, OverflowError):
                continue
            cand.append((date(g.year, g.month, g.day), True))
    for d, approx in cand:
        last = d + timedelta(days=dur - 1)
        if last >= start and d <= end:
            out.append((d, approx))
    return sorted(out)


def between(occasions: Iterable[dict], start: date, end: date, today: date | None = None, film_lead_days: int = 3) -> list[Occurrence]:
    today = today or start
    res = []
    for o in occasions:
        for d, approx in occurrences_of(o, start, end):
            dur = max(1, int(o.get("duration_days") or 1))
            h_en, h_ar = hijri_label(d) if o["rule"] == "hijri" else ("", "")
            res.append(Occurrence(o, d, d + timedelta(days=dur - 1), approx, h_en, h_ar, (d - today).days,
                                  max(today, d - timedelta(days=film_lead_days))))
    res.sort(key=lambda x: (x.start, x.occasion["slug"]))
    return res


def local_today(cfg, now: datetime | None = None) -> date:
    return (now or db.utcnow()).astimezone(cfg.tz).date()


def upcoming(conn, cfg, today: date | None = None, min_days: int | None = None, max_days: int | None = None,
             include_demo: bool = True) -> list[Occurrence]:
    """Occasions starting in [today + min_days, today + max_days] (calendar.lookahead_* by default)."""
    today = today or local_today(cfg)
    lo = cfg.get("calendar.lookahead_min_days", 3) if min_days is None else min_days
    hi = cfg.get("calendar.lookahead_max_days", 21) if max_days is None else max_days
    occ = between(all_occasions(conn, include_demo), today + timedelta(days=lo), today + timedelta(days=hi), today,
                  int(cfg.get("calendar.film_lead_days", 3)))
    return [x for x in occ if lo <= x.days_until <= hi]


def brief_text(items: list[Occurrence]) -> str:
    """The occasion lines for the brief prompt (DATA, inside <data>)."""
    if not items:
        return "(none in the next weeks)"
    lines = []
    for x in items:
        o = x.occasion
        when = x.start.isoformat() + (f" to {x.end.isoformat()}" if x.end != x.start else "")
        lines.append(
            f"- id={o['slug']} · {o['name_en']} / {o['name_ar']} · {when} (in {x.days_until} days"
            + (", Hijri date, may move a day after moon sighting" if x.approximate else "") + ")"
            + f" · {'official Saudi occasion' if o['official'] else 'not an official occasion or holiday'} · {o['category']}"
            + (f" · angle: {o['angle']}" if o.get("angle") else "")
        )
    return "\n".join(lines)


# ------------------------------------------------------------------ reels <-> occasions
def _norm(s: str) -> str:
    from .gates import normalize

    return normalize(s or "")


def match_text(text: str, occasions: Iterable[dict]) -> list[dict]:
    """Occasions whose name (AR/EN) or a keyword appears in `text` (normalised like the gates)."""
    t = _norm(text)
    hits = []
    for o in occasions:
        keys = [o.get("name_ar") or "", o.get("name_en") or "", *(o.get("keywords") or [])]
        variants = []
        for k in keys:
            k = _norm(re.sub(r"\(.*?\)", "", k)).strip()
            if len(k) >= 3:
                variants.append(k)
                if k.startswith("ال") and len(k) >= 7:  # «لليوم الوطني», «باليوم الوطني»: the article merges with a prefix
                    variants.append(k[2:])
        if any(_contains(t, v) for v in variants):
            hits.append(o)
    return hits


def _contains(text: str, key: str) -> bool:
    """Arabic: substring (prefixes like و/ب/ل join the word). Latin: whole words only
    («leap of faith» must not tag LEAP, «eid» not inside other words)."""
    if re.search(r"[a-z]", key):
        return re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text) is not None
    return key in text


def reel_text(row) -> str:
    plan = db.jload(row["plan_json"], {}) if row["plan_json"] else {}
    cap = db.jload(row["caption_json"], {}) if row["caption_json"] else {}
    hook = plan.get("hook") if isinstance(plan.get("hook"), dict) else {}
    parts = [str(plan.get("topic") or ""), str(plan.get("title_card") or ""), str(hook.get("on_screen") or ""),
             str(hook.get("spoken") or ""), str(cap.get("caption") or "")]
    return " ".join(parts)


def window_for_reel(conn, cfg, row, now: datetime | None = None) -> dict | None:
    """If a reel is about an occasion coming up within the scheduler horizon, return
    {"slug", "start_utc", "end_utc"}: post from `post_window_days` before the day to the end of
    its last day (Asia/Riyadh). The occasion can be named explicitly in features_json.occasion."""
    if not cfg.get("calendar.enabled", True):
        return None
    try:
        occs = all_occasions(conn, include_demo=False)
    except Exception:  # noqa: BLE001
        return None
    if not occs:
        return None
    feats = db.jload(row["features_json"], {}) if row["features_json"] else {}
    tagged = feats.get("occasion") if isinstance(feats, dict) else None
    cands = [o for o in occs if o["slug"] == tagged] if tagged else match_text(reel_text(row), occs)
    if not cands:
        return None
    today = local_today(cfg, now)
    soon = between(cands, today, today + timedelta(days=14), today)
    if not soon:
        return None
    x = soon[0]
    pre = int(cfg.get("calendar.post_window_days", 2))
    start_local = datetime.combine(x.start - timedelta(days=pre), dtime(0, 0), tzinfo=cfg.tz)
    end_local = datetime.combine(x.end, dtime(23, 59), tzinfo=cfg.tz)
    return {"slug": x.occasion["slug"], "start_utc": start_local.astimezone(timezone.utc), "end_utc": end_local.astimezone(timezone.utc)}


# ------------------------------------------------------------------ reminders
def remind(conn, cfg, notify, today: date | None = None) -> list[str]:
    """Daily: one reminder per occasion occurrence when it enters the look-ahead window."""
    if not cfg.get("calendar.enabled", True) or not cfg.get("calendar.notify", True):
        return []
    sent = []
    for x in upcoming(conn, cfg, today, include_demo=False):
        key = f"occasion_reminded:{x.occasion['slug']}:{x.start.isoformat()}"
        if db.meta_get(conn, key):
            continue
        o = x.occasion
        notify("info", f"{o['name_en']} in {x.days_until} days ({x.start.isoformat()})",
               f"{o['name_ar']}. Film by {x.film_by.isoformat() if x.film_by else x.start.isoformat()}."
               + (f" Angle: {o['angle']}" if o.get("angle") else "")
               + (" Hijri date: re-check the official announcement." if x.approximate else ""), None)
        db.meta_set(conn, key, db.now_iso())
        sent.append(o["slug"])
    return sent


def month_grid(year: int, month: int, occ: list[Occurrence], today: date) -> dict:
    """A Sunday-first month grid for the dashboard."""
    by_day: dict[date, list[Occurrence]] = {}
    for x in occ:
        d = x.start
        while d <= x.end:
            by_day.setdefault(d, []).append(x)
            d += timedelta(days=1)
    cal = _cal.Calendar(firstweekday=6)
    weeks = []
    for wk in cal.monthdatescalendar(year, month):
        weeks.append([{"date": d, "in_month": d.month == month, "today": d == today, "items": by_day.get(d, [])} for d in wk])
    return {"year": year, "month": month, "label": date(year, month, 1).strftime("%B %Y"), "weeks": weeks,
            "hijri": hijri_label(date(year, month, 1))[0]}
