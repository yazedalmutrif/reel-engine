"""Build the static dashboard site (spec §8) from the SQLite store.

build_dashboard() is read-only on the DB, renders every page with Jinja2 into a
temporary directory next to the target, then swaps it into place, so a half-written
site is never served. It runs on every 15-minute tick and must stay fast (< 2 s for
500 reels), so all data is loaded with a handful of queries and processed in memory.

No secrets are read here: token_state only holds dates and status, and the keyring is
never touched. Pages contain no inline styles or scripts (strict CSP, see CSP below).
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import statistics
import subprocess  # nosec B404 - fixed nvidia-smi argv, no shell
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from jinja2 import Environment, FileSystemBytecodeCache, FileSystemLoader, select_autoescape
from markupsafe import Markup

from . import charts as C
from ..log import get_logger

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"

CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self' data:; font-src 'self'; media-src 'self'; "
    "script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
DEMO_BANNER = "DEMO DATA: fake reels for layout review, nothing here is real"

PAGES = (
    ("index", "Overview"),
    ("pipeline", "Pipeline"),
    ("reels", "Reels"),
    ("learning", "Learning"),
    ("brief", "Brief"),
    ("calendar", "Calendar"),
    ("accounts", "Accounts"),
    ("health", "Health"),
    ("money", "Money"),
)

# brand/fonts file -> served name (the server's filename regex has no brackets or commas)
FONT_FILES = {
    "Inter[opsz,wght].ttf": "Inter-var.ttf",
    "JetBrainsMono[wght].ttf": "JetBrainsMono-var.ttf",
    "IBMPlexSansArabic-Regular.ttf": "IBMPlexSansArabic-Regular.ttf",
    "IBMPlexSansArabic-Medium.ttf": "IBMPlexSansArabic-Medium.ttf",
    "IBMPlexSansArabic-SemiBold.ttf": "IBMPlexSansArabic-SemiBold.ttf",
    "IBMPlexSansArabic-Bold.ttf": "IBMPlexSansArabic-Bold.ttf",
    "OFL-inter.txt": "OFL-inter.txt",
    "OFL-jetbrainsmono.txt": "OFL-jetbrainsmono.txt",
    "OFL-ibmplexsansarabic.txt": "OFL-ibmplexsansarabic.txt",
}

STATE_LABELS = {
    "new": "New",
    "transcribed": "Transcribed",
    "planned": "Planned",
    "rendered": "Rendered",
    "qa_passed": "QA passed",
    "scheduled": "Scheduled",
    "publishing": "Publishing",
    "published": "Published",
    "held": "Held",
    "failed": "Failed",
}
FLOW_STATES = ("new", "transcribed", "planned", "rendered", "qa_passed", "scheduled", "publishing", "published")
QUEUE_STATES = ("new", "transcribed", "planned", "rendered", "qa_passed", "scheduled", "publishing")
LANE_HINTS = {
    "new": "Clips just moved in from the OneDrive inbox.",
    "transcribed": "Speech turned into timed words.",
    "planned": "Edit plan written: cuts, hook, title card.",
    "rendered": "Video rendered with burned-in captions.",
    "qa_passed": "Passed every QA gate; waiting for a slot.",
    "scheduled": "Has a posting slot.",
    "publishing": "Uploading to Instagram right now.",
    "published": "Live on the account (latest 10).",
    "held": "Stopped by a gate. Needs your eyes.",
    "failed": "A stage crashed after its retries.",
}
DIM_LABELS = {"slot": "Posting slot", "hook_type": "Hook type", "duration_band": "Length band"}
DIM_HINTS = {
    "slot": "When to post (Asia/Riyadh; weekend = Fri-Sat).",
    "hook_type": "How the first 3 seconds open.",
    "duration_band": "Final length after the edit.",
}
EXP_COLUMNS = (
    ("running", "Running", "Bandit is testing it now"),
    ("logging", "Logging", "Recorded on every reel, judged at review"),
    ("next", "Next", "Starts when a running one ends"),
    ("concluded", "Concluded", "Answered, with evidence"),
    ("parked", "Parked", "On hold or dropped"),
)
FACTOR_LABELS = {"cta_type": "CTA type", "hashtag_count": "Hashtag count", "caption_style": "Caption style"}
LEVEL_LABELS = {"natural_keywords": "Natural", "short": "Short"}
PILLAR_SHORT = {"ai_tools": "AI tools", "job_hunt": "Job hunt", "build_proof": "Build proof", "cv_method": "CV method"}
DIM_NAMES = {
    "slot": "Slot", "hook_type": "Hook type", "duration_band": "Length band", "caption_style": "Caption style",
    "hashtag_count": "Hashtag count", "cta_type": "CTA type", "format": "Format", "cover_style": "Cover style",
}
CP_LABELS = {1: "1h", 6: "6h", 24: "24h", 72: "3d", 168: "7d", 672: "28d"}
CURVES = (
    ("views", "Views", C.fmt_compact),
    ("reach", "Reach (accounts)", C.fmt_compact),
    ("shares", "Shares", C.fmt_compact),
    ("saved", "Saves", C.fmt_compact),
    ("watch_s", "Avg watch time (s)", lambda v: f"{v:.0f}s" if v >= 10 or float(v).is_integer() else f"{v:.1f}s"),
    ("skip_pct", "Skip rate (%)", lambda v: f"{v:.0f}%"),
)
_AR = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-\ufeff]")
_GPU_TTL_S = 30 * 60


# =============================================================================== helpers
def _j(s: Any, default: Any) -> Any:
    """Parse a JSON column; fall back to `default` on NULL, bad JSON or the wrong type."""
    if s is None or s == "":
        return default
    try:
        v = json.loads(s)
    except (TypeError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def _ts(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, dict) and "value" in v:
        return _num(v["value"])
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        return str(v.get("text") or v.get("on_screen") or v.get("spoken") or "")
    return str(v)


def is_ar(s: Any) -> bool:
    return bool(s) and bool(_AR.search(str(s)))


def _median(vals: list[float]) -> float | None:
    return statistics.median(vals) if vals else None


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _humanize(s: str) -> str:
    s = str(s).replace("_", " ").replace("-", " ").strip()
    return s[:1].upper() + s[1:] if s else s


def _slot_label(arm: str) -> str:
    m = re.match(r"^(weekday|weekend)-(\d{1,2}:\d{2})$", str(arm))
    return f"{m.group(1).capitalize()} {m.group(2)}" if m else _humanize(arm)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """SELECT helper that tolerates a missing table (a fresh or older DB)."""
    try:
        cur = conn.execute(sql, params)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc) or "no such column" in str(exc):
            return []
        raise
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class Clock:
    """Formats times in the engine time zone (Asia/Riyadh), ISO style."""

    def __init__(self, tz, now: datetime):
        self.tz = tz
        self.now = now

    def local(self, dt: datetime | None) -> datetime | None:
        return dt.astimezone(self.tz) if dt else None

    def dt(self, dt: datetime | None) -> str:
        return self.local(dt).strftime("%Y-%m-%d %H:%M") if dt else "–"

    def date(self, dt: datetime | None) -> str:
        return self.local(dt).strftime("%Y-%m-%d") if dt else "–"

    def short(self, dt: datetime | None) -> str:
        return self.local(dt).strftime("%m-%d") if dt else "–"

    def dow(self, dt: datetime | None) -> str:
        return self.local(dt).strftime("%a") if dt else ""

    def iso(self, dt: datetime | None) -> str:
        return self.local(dt).isoformat(timespec="minutes") if dt else ""

    def rel(self, dt: datetime | None) -> str:
        if not dt:
            return ""
        secs = (dt - self.now).total_seconds()
        a = abs(secs)
        if a < 90:
            return "now"
        if a < 3600:
            txt = f"{round(a / 60)} min"
        elif a < 86400 * 2:
            txt = f"{round(a / 3600)} h"
        else:
            txt = f"{round(a / 86400)} d"
        return f"in {txt}" if secs > 0 else f"{txt} ago"


def fmt_secs(v: float | None) -> str:
    v = C.num_or_none(v)
    if v is None:
        return "–"
    if v < 60:
        return f"{v:.0f} s" if v >= 10 else f"{v:.1f} s"
    return f"{int(v // 60)} min {int(v % 60):02d} s"


def fmt_bytes(v: float | None) -> str:
    v = C.num_or_none(v)
    if v is None:
        return "–"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.0f} {unit}" if unit in ("B", "KB") else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"


def fmt_value(v: Any) -> str:
    """Compact display of a playbook value (before/after)."""
    if v is None:
        return "–"
    if isinstance(v, (list, tuple)):
        return ", ".join(fmt_value(x) for x in v) or "–"
    if isinstance(v, dict):
        return ", ".join(f"{k}: {fmt_value(x)}" for k, x in v.items()) or "–"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


# ============================================================================ metrics
def derive(m: dict, duration_s: float | None, skip_is_percent: bool = True) -> dict:
    """Raw Instagram insight values -> the numbers the dashboard shows."""
    g = lambda k: _num(m.get(k))  # noqa: E731
    views, reach, shares, saved, likes = g("views"), g("reach"), g("shares"), g("saved"), g("likes")
    awt = g("ig_reels_avg_watch_time")
    watch_s = awt / 1000 if awt is not None else None
    skip = g("reels_skip_rate")
    if skip is not None and not skip_is_percent:
        skip *= 100
    return {
        "views": views,
        "reach": reach,
        "likes": likes,
        "comments": g("comments"),
        "shares": shares,
        "saved": saved,
        "total_interactions": g("total_interactions"),
        "watch_s": watch_s,
        "watch_ratio": (watch_s / duration_s) if (watch_s is not None and duration_s) else None,
        "skip_pct": skip,
        "shares_per_reach": (shares / reach) if (shares is not None and reach) else None,
        "saves_per_reach": (saved / reach) if (saved is not None and reach) else None,
        "total_time_s": (g("ig_reels_video_view_total_time") or 0) / 1000 if g("ig_reels_video_view_total_time") is not None else None,
    }


# ================================================================================ GPU
def gpu_info(cache_file: Path | None) -> dict:
    """nvidia-smi query with a short timeout, cached for 30 min (file) so ticks stay fast."""
    now = time.time()
    if cache_file and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - float(cached.get("at", 0)) < _GPU_TTL_S:
                return cached
        except (OSError, ValueError):
            pass
    info: dict = {"at": now, "ok": False}
    exe = shutil.which("nvidia-smi")
    if exe:
        try:
            out = subprocess.run(  # nosec B603 - fixed argv
                [exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            line = (out.stdout or "").strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                info.update(ok=True, name=parts[0], util=_num(parts[1]), mem_used=_num(parts[2]), mem_total=_num(parts[3]), temp=_num(parts[4]))
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    if cache_file:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(info), encoding="utf-8")
        except OSError:
            pass
    return info


# ============================================================================= collect
class Collector:
    """Loads everything once and shapes it for the templates."""

    def __init__(self, cfg, conn: sqlite3.Connection, demo: bool, now: datetime):
        self.cfg = cfg
        self.conn = conn
        self.demo = demo
        self.clock = Clock(cfg.tz, now)
        self.now = now
        self.skip_pct = bool(cfg.get("instagram.skip_rate_is_percent", True))
        self.min_n = int(cfg.get("learning.min_n_per_arm", 3))
        self.window = int(cfg.get("learning.rolling_window", 20))
        cps = cfg.get("instagram.insights_checkpoints_h", [1, 6, 24, 72, 168, 672])
        self.checkpoints = [float(c) for c in cps]

    # ------------------------------------------------------------------ playbook
    def playbooks(self) -> list[dict]:
        out = []
        for r in _rows(self.conn, "SELECT version, created_at, json, changes_json, summary, path FROM playbooks ORDER BY version"):
            changes = _j(r["changes_json"], [])
            changes = [c for c in changes if isinstance(c, dict)]
            for c in changes:
                ev = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
                c["_n"] = ev.get("n")
                eff = ev.get("effect_size", ev.get("effect"))
                c["_effect"] = _num(eff)
                c["_metric"] = ev.get("metric") or ""
                c["_status"] = str(c.get("status") or "applied").lower()
                conf = c.get("confidence")
                c["_conf"] = (f"{_num(conf) * 100:.0f}%" if _num(conf) is not None and _num(conf) <= 1 else _humanize(conf)) if conf is not None else "–"
                c["_before"] = fmt_value(c.get("before"))
                c["_after"] = fmt_value(c.get("after"))
            out.append(
                {
                    "version": r["version"],
                    "created": _ts(r["created_at"]),
                    "json": _j(r["json"], {}),
                    "changes": changes,
                    "summary": r["summary"] or "",
                    "applied": sum(1 for c in changes if c["_status"] == "applied"),
                    "rejected": sum(1 for c in changes if c["_status"] == "rejected"),
                }
            )
        return out

    def base_playbook(self, pbs: list[dict]) -> dict:
        if pbs and pbs[-1]["json"]:
            return pbs[-1]["json"]
        f = self.cfg.brand_dir / "playbook_v1.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # ------------------------------------------------------------------ reels
    def reels(self, pb: dict) -> list[dict]:
        where = "" if self.demo else " WHERE is_demo=0"
        rows = _rows(self.conn, "SELECT * FROM reels" + where + " ORDER BY id")  # nosec B608 - constant clause
        ids = {r["id"] for r in rows}
        topics = {}
        for t in pb.get("topics", []) or []:
            if isinstance(t, dict) and t.get("id"):
                lab = str(t.get("label") or t["id"])
                m = re.search(r"\(([^)]+)\)", lab)
                topics[t["id"]] = PILLAR_SHORT.get(t["id"]) or (_humanize(m.group(1)) if m else _humanize(t["id"]))
        hooks = {a.get("id"): a.get("label") for a in (pb.get("arms", {}) or {}).get("hook_type", []) if isinstance(a, dict)}

        ins: dict[int, dict[float, dict]] = defaultdict(dict)
        for r in _rows(self.conn, "SELECT reel_id, checkpoint_h, fetched_at, metrics_json FROM insights WHERE fetched_at IS NOT NULL AND metrics_json IS NOT NULL"):
            if r["reel_id"] in ids:
                ins[r["reel_id"]][float(r["checkpoint_h"])] = _j(r["metrics_json"], {})
        events: dict[int, list[dict]] = defaultdict(list)
        for e in _rows(self.conn, "SELECT reel_id, ts, from_state, to_state, detail FROM events WHERE reel_id IS NOT NULL ORDER BY id"):
            if e["reel_id"] in ids:
                e["t"] = _ts(e["ts"])
                events[e["reel_id"]].append(e)

        out = []
        for r in rows:
            plan = _j(r.get("plan_json"), {})
            cap = _j(r.get("caption_json"), {})
            qa = _j(r.get("qa_json"), {})
            feat = _j(r.get("features_json"), {})
            probe = _j(r.get("probe_json"), {})
            hook = plan.get("hook") if isinstance(plan.get("hook"), dict) else {}
            title = _text(plan.get("title_card")) or _text(hook.get("on_screen")) or (r.get("source_name") or "") or f"Reel #{r['id']}"
            dur = _num(r.get("duration_s")) or _num(plan.get("output_duration")) or _num(feat.get("duration_s"))
            pillar = feat.get("pillar") or plan.get("pillar")
            hook_type = feat.get("hook_type") or plan.get("hook_type")
            curves = {}
            for cp, m in sorted(ins.get(r["id"], {}).items()):
                curves[cp] = derive(m, dur, self.skip_pct)
            latest_cp = max(curves) if curves else None
            thumb = Path(str(r["thumb_path"])).name if r.get("thumb_path") else None
            media = Path(str(r["out_path"])).name if r.get("out_path") else f"{r['id']}.mp4"
            hashtags = cap.get("hashtags") if isinstance(cap.get("hashtags"), list) else (feat.get("hashtags") or [])
            reel = {
                "id": r["id"],
                "state": r["state"],
                "state_label": STATE_LABELS.get(r["state"], _humanize(r["state"])),
                "title": title,
                "source_name": r.get("source_name") or "",
                "held_reason": r.get("held_reason") or "",
                "last_error": r.get("last_error") or "",
                "failed_from": r.get("failed_from") or "",
                "retries": r.get("retries") or 0,
                "next_attempt": _ts(r.get("next_attempt_at")),
                "created": _ts(r.get("created_at")),
                "updated": _ts(r.get("updated_at")),
                "scheduled": _ts(r.get("scheduled_at")),
                "published": _ts(r.get("published_at")),
                "duration": dur,
                "src_duration": _num(r.get("src_duration_s")) or _num(probe.get("duration")),
                "thumb": thumb if thumb and re.fullmatch(r"[A-Za-z0-9_-]{1,64}\.png", thumb) else None,
                "media": media if re.fullmatch(r"[A-Za-z0-9_-]{1,64}\.mp4", media) else None,
                "pillar": pillar,
                "pillar_label": topics.get(pillar, _humanize(pillar)) if pillar else "–",
                "hook_type": hook_type,
                "hook_label": hooks.get(hook_type) or (_humanize(hook_type) if hook_type else "–"),
                "band": feat.get("duration_band") or "",
                "slot": feat.get("slot") or r.get("slot_arm") or "",
                "dry_run": bool(r.get("dry_run")),
                "permalink": r.get("permalink") if str(r.get("permalink") or "").startswith("https://") else None,
                "media_id": r.get("media_id") or "",
                "composite": _num(r.get("composite_score")),
                "playbook_version": r.get("playbook_version"),
                "plan": plan,
                "hook": hook,
                "caption": cap,
                "hashtags": [str(h) for h in hashtags if h],
                "qa": qa,
                "features": feat,
                "probe": probe,
                "curves": curves,
                "latest_cp": latest_cp,
                "m": curves.get(latest_cp) if latest_cp is not None else derive({}, dur, self.skip_pct),
                "events": events.get(r["id"], []),
                "is_demo": bool(r.get("is_demo")),
            }
            out.append(reel)
        return out

    # ------------------------------------------------------------------ baselines
    def baselines(self, published: list[dict]) -> dict[int, dict[float, dict[str, float | None]]]:
        """Per reel: median of each curve metric at each checkpoint over the previous N published reels."""
        out: dict[int, dict] = {}
        chron = sorted(published, key=lambda r: r["published"] or self.now)
        for k, reel in enumerate(chron):
            prev = chron[max(0, k - self.window):k]
            per = {}
            for cp in self.checkpoints:
                per[cp] = {}
                for key, _, _ in CURVES:
                    vals = [p["curves"][cp][key] for p in prev if cp in p["curves"] and p["curves"][cp].get(key) is not None]
                    per[cp][key] = _median(vals) if len(vals) >= 3 else None
            out[reel["id"]] = per
        return out

    # ------------------------------------------------------------------ account
    def account(self) -> list[dict]:
        rows = _rows(self.conn, "SELECT day, follower_count, metrics_json FROM account_insights ORDER BY day")
        return [{"day": r["day"], "followers": _num(r["follower_count"]), "m": _j(r["metrics_json"], {})} for r in rows]

    def token(self) -> dict:
        r = _rows(self.conn, "SELECT status, issued_at, expires_at, last_refresh_at, last_check_at, username, account_type, detail FROM token_state WHERE id=1")
        t = r[0] if r else {"status": "missing"}
        exp = _ts(t.get("expires_at"))
        days_left = (exp - self.now).total_seconds() / 86400 if exp else None
        return {
            "status": (t.get("status") or "missing"),
            "expires": exp,
            "days_left": days_left,
            "last_refresh": _ts(t.get("last_refresh_at")),
            "last_check": _ts(t.get("last_check_at")),
            "username": t.get("username") or "",
            "account_type": t.get("account_type") or "",
            "detail": t.get("detail") or "",
        }


def _mode(cfg, token: dict) -> dict:
    mode = cfg["engine"]["mode"]
    paused = cfg.pause_file.exists()
    auth = token.get("status") == "auth_needed"
    if auth:
        badge = {"key": "auth", "text": "AUTH NEEDED", "icon": "!", "sev": "critical"}
    elif paused:
        badge = {"key": "paused", "text": "PAUSED", "icon": "❚❚", "sev": "warning"}
    elif mode == "live":
        badge = {"key": "live", "text": "LIVE", "icon": "●", "sev": "good"}
    else:
        badge = {"key": "dry", "text": "DRY-RUN", "icon": "○", "sev": "accent"}
    return {"mode": mode, "paused": paused, "auth": auth, "badge": badge}


# ============================================================================ context
def collect(cfg, conn: sqlite3.Connection, demo: bool, now: datetime) -> dict:
    col = Collector(cfg, conn, demo, now)
    ck = col.clock
    pbs = col.playbooks()
    pb = col.base_playbook(pbs)
    reels = col.reels(pb)
    by_id = {r["id"]: r for r in reels}
    published = sorted([r for r in reels if r["state"] == "published"], key=lambda r: r["published"] or now, reverse=True)
    base = col.baselines(published)
    token = col.token()
    mode = _mode(cfg, token)
    acct = col.account()

    ctx: dict[str, Any] = {
        "demo": demo,
        "demo_banner": DEMO_BANNER,
        "csp": CSP,
        "pages": PAGES,
        "mode": mode,
        "updated": ck.dt(now),
        "updated_iso": ck.iso(now),
        "tz_name": cfg["engine"]["timezone"],
        "clock": ck,
        "now": now,
        "C": C,
    }

    # ---------------------------------------------------------------- overview
    trial_min = int(cfg.get("learning.trial_reels_min_followers", 1000))
    fol = [a for a in acct if a["followers"] is not None]
    followers = fol[-1]["followers"] if fol else None
    fol_delta = None
    if fol:
        last_day = datetime.fromisoformat(fol[-1]["day"]).date()
        ref = [a for a in fol if datetime.fromisoformat(a["day"]).date() <= last_day - timedelta(days=7)]
        if ref:
            fol_delta = followers - ref[-1]["followers"]
    fol_series = acct[-60:]
    followers_chart = C.dual(
        C.line_chart, "fol",
        "Followers per day",
        "Follower count from the daily account check. Days under 100 followers have no data (Instagram does not report them).",
        [a["day"][5:] for a in fol_series],
        [{"name": "Followers", "values": [a["followers"] for a in fol_series], "color": C.ACCENT, "emphasis": True, "area": True}],
        wide=560, narrow=326, height=220, y_fmt=C.fmt_compact, tip_labels=[a["day"] for a in fol_series],
    ) if fol else Markup("")

    today = ck.local(now).date()
    views_7 = views_prev = None
    daily_views = {a["day"]: _num(a["m"].get("views")) for a in acct if _num(a["m"].get("views")) is not None}
    views_source = "account"
    if daily_views:
        d7 = [(today - timedelta(days=i)).isoformat() for i in range(7)]
        p7 = [(today - timedelta(days=i)).isoformat() for i in range(7, 14)]
        views_7 = sum(daily_views.get(d, 0) for d in d7)
        views_prev = sum(daily_views.get(d, 0) for d in p7) if any(d in daily_views for d in p7) else None
    else:
        recent = [r for r in published if r["published"] and r["published"] >= now - timedelta(days=7)]
        if recent:
            views_7 = sum(r["m"].get("views") or 0 for r in recent)
            views_source = "reels"
    spark_views = [daily_views.get((today - timedelta(days=i)).isoformat()) for i in range(13, -1, -1)] if daily_views else []

    last10 = published[:10]
    watch = [r["m"]["watch_ratio"] for r in last10 if r["m"].get("watch_ratio") is not None]
    sh = sum(r["m"].get("shares") or 0 for r in last10 if r["m"].get("reach"))
    rc = sum(r["m"].get("reach") or 0 for r in last10 if r["m"].get("reach"))
    upcoming = sorted([r for r in reels if r["state"] == "scheduled" and r["scheduled"]], key=lambda r: r["scheduled"])
    next_post = upcoming[0] if upcoming else None
    queue = [r for r in reels if r["state"] in QUEUE_STATES]
    q_ready = sum(1 for r in queue if r["state"] in ("qa_passed", "scheduled"))

    recent12 = list(reversed(published[:12]))
    views_chart = C.dual(
        C.column_chart, "vpr",
        "Views per recent reel",
        "Views at the latest insight checkpoint for the last 12 published reels, oldest on the left.",
        [{"label": f"#{r['id']}", "value": r["m"].get("views"), "tip": f"Reel #{r['id']} · {ck.date(r['published'])}: {C.fmt_int(r['m'].get('views'))} views"} for r in recent12],
        wide=560, narrow=326, height=220,
    ) if any(r["m"].get("views") is not None for r in recent12) else Markup("")

    # alerts
    alerts: list[dict] = []
    if mode["auth"]:
        alerts.append({"sev": "critical", "title": "Instagram needs you to reconnect", "body": "The access token was rejected or expired, so nothing can publish. Run `reel connect` (docs/SETUP.md).", "href": "health.html"})
    if token["status"] in ("missing", "unknown"):
        alerts.append({"sev": "info", "title": "Instagram is not connected yet", "body": "Dry-run works without it. To post for real, connect once with `reel connect` (docs/SETUP.md).", "href": "health.html"})
    if token["days_left"] is not None and token["status"] != "auth_needed" and token["days_left"] < 10:
        alerts.append({"sev": "warning", "title": f"Token expires in {max(token['days_left'], 0):.0f} days", "body": "The weekly run refreshes it; if this stays low, check the Health tab.", "href": "health.html"})
    if mode["paused"]:
        alerts.append({"sev": "warning", "title": "Publishing is paused", "body": "The kill switch (data/PAUSE) is on. Run `reel resume` to continue posting.", "href": None})
    for r in reels:
        if r["state"] == "held":
            alerts.append({"sev": "serious", "title": f"Reel #{r['id']} held", "body": r["held_reason"] or "Held by QA.", "href": f"reels/{r['id']}.html", "ar_title": r["title"]})
    for r in reels:
        if r["state"] == "failed":
            alerts.append({"sev": "critical", "title": f"Reel #{r['id']} failed", "body": r["last_error"] or "A stage failed.", "href": f"reels/{r['id']}.html", "ar_title": r["title"]})
    tick_rows = _rows(conn, "SELECT id, kind, started_at, finished_at, status, error, summary_json FROM ticks WHERE started_at >= ? ORDER BY started_at", ((now - timedelta(days=3)).isoformat(timespec="seconds"),))
    errs24 = [t for t in tick_rows if t["status"] == "error" and (_ts(t["started_at"]) or now) >= now - timedelta(hours=24)]
    if errs24:
        alerts.append({"sev": "warning", "title": f"{len(errs24)} tick error{'s' if len(errs24) != 1 else ''} in the last 24 h", "body": (errs24[-1]["error"] or "")[:180], "href": "health.html"})

    notes = _rows(conn, "SELECT ts, level, title, body, reel_id FROM notifications ORDER BY ts DESC LIMIT 6")
    for n in notes:
        n["t"] = _ts(n["ts"])

    ctx["ov"] = {
        "followers": followers,
        "fol_delta": fol_delta,
        "fol_spark": C.sparkline([a["followers"] for a in acct[-14:]], label="Followers, last 14 days") if len(fol) >= 2 else Markup(""),
        "views_7": views_7,
        "views_prev": views_prev,
        "views_source": views_source,
        "views_spark": C.sparkline(spark_views, label="Views per day, last 14 days") if spark_views else Markup(""),
        "watch": _mean(watch),
        "watch_n": len(watch),
        "spr": (sh / rc) if rc else None,
        "spr_n": sum(1 for r in last10 if r["m"].get("reach")),
        "next_post": next_post,
        "queue": len(queue),
        "q_ready": q_ready,
        "q_working": len(queue) - q_ready,
        "trial_min": trial_min,
        "trial_ok": followers is not None and followers >= trial_min,
        "trial_meter": C.meter("Followers towards Trial Reels", (followers or 0) / trial_min, "accent"),
        "followers_chart": followers_chart,
        "fol_table": fol_series,
        "views_chart": views_chart,
        "views_table": recent12,
        "alerts": alerts,
        "notes": notes,
        "published_n": len(published),
    }

    # ---------------------------------------------------------------- pipeline
    lanes = []
    for s in FLOW_STATES + ("held", "failed"):
        items = [r for r in reels if r["state"] == s]
        if s == "published":
            items = published[:10]
        elif s == "scheduled":
            items = sorted(items, key=lambda r: r["scheduled"] or now)
        else:
            items = sorted(items, key=lambda r: r["updated"] or now, reverse=True)
        total = sum(1 for r in reels if r["state"] == s)
        lanes.append({"state": s, "label": STATE_LABELS[s], "hint": LANE_HINTS[s], "cards": items, "total": total})
    ctx["lanes"] = lanes
    ctx["attention"] = [l for l in lanes if l["state"] in ("held", "failed")]
    ctx["flow"] = [l for l in lanes if l["state"] not in ("held", "failed", "published")]
    ctx["pub_lane"] = next(l for l in lanes if l["state"] == "published")

    # ---------------------------------------------------------------- reels table + detail
    def sort_key(r):
        t = r["published"] or r["scheduled"] or r["created"] or now
        return (0 if r["state"] != "published" else 1, -t.timestamp())

    ctx["reels"] = sorted(reels, key=sort_key)
    ctx["reel_count"] = len(reels)

    media_dir = (cfg.data_dir / "demo" / "out") if demo else (cfg.data_dir / "out")
    details = []
    for r in reels:
        per = base.get(r["id"], {})
        charts = []
        has_base = False
        if r["curves"]:
            x_cps = [cp for cp in col.checkpoints]
            for key, label, fmt in CURVES:
                mine = [r["curves"].get(cp, {}).get(key) for cp in x_cps]
                med = [per.get(cp, {}).get(key) for cp in x_cps]
                if all(v is None for v in mine):
                    continue
                has_base = has_base or any(v is not None for v in med)
                series = [{"name": "This reel", "values": mine, "color": C.ACCENT, "emphasis": True}]
                if any(v is not None for v in med):
                    series.append({"name": "Account median", "values": med, "color": C.BASELINE, "emphasis": False, "end_label": False})
                charts.append({
                    "label": label,
                    "svg": C.line_chart(
                        f"cv-{key}", label,
                        f"{label} for reel {r['id']} at each insight checkpoint, against the median of the previous {col.window} reels.",
                        [CP_LABELS.get(int(cp), f"{cp:g}h") for cp in x_cps], series,
                        width=340, height=170, y_fmt=fmt, zero_based=(key != "skip_pct"), max_x_labels=6,
                        tip_labels=[f"+{CP_LABELS.get(int(cp), f'{cp:g}h')}" for cp in x_cps],
                    ),
                    "rows": [(CP_LABELS.get(int(cp), f"{cp:g}h"), fmt(a) if a is not None else "–", fmt(b) if b is not None else "–") for cp, a, b in zip(x_cps, mine, med)],
                })
        exists = bool(r["media"]) and (media_dir / r["media"]).is_file()
        scores = r["qa"].get("scores") if isinstance(r["qa"].get("scores"), dict) else {}
        min_score = int(cfg.get("qa.min_score", 7))
        score_rows = []
        for k in ("hook", "clarity", "value", "accuracy"):
            v = _num(scores.get(k))
            if v is not None:
                score_rows.append({"name": k.capitalize(), "v": v, "ok": v >= min_score, "meter": C.meter(f"{k} score {v:g} of 10", v / 10, "good" if v >= min_score else "critical", width=160)})
        checks = [c for c in (r["qa"].get("checks") or []) if isinstance(c, dict)]
        details.append({"r": r, "charts": charts, "has_base": has_base, "video_ok": exists, "scores": score_rows, "checks": checks, "min_score": min_score})
    ctx["details"] = details

    # ---------------------------------------------------------------- learning
    latest = pbs[-1] if pbs else None
    ctx["pbs"] = list(reversed(pbs))
    ctx["latest_pb"] = latest
    ctx["latest_changes"] = sorted(latest["changes"], key=lambda c: 0 if c["_status"] == "applied" else 1) if latest else []

    arm_rows = _rows(conn, "SELECT dimension, arm, alpha, beta, n, wins, updated_at FROM bandit_arms")
    by_dim: dict[str, list[dict]] = defaultdict(list)
    priors_only = False
    if arm_rows:
        for a in arm_rows:
            by_dim[a["dimension"]].append(a)
    else:
        priors = pb.get("priors", {}) or {}
        for dim, arms in (pb.get("arms", {}) or {}).items():
            for a in arms:
                if isinstance(a, dict) and a.get("id"):
                    p = (priors.get(dim, {}) or {}).get(a["id"], {})
                    by_dim[dim].append({"dimension": dim, "arm": a["id"], "alpha": p.get("alpha", 1), "beta": p.get("beta", 1), "n": 0, "wins": 0})
        priors_only = bool(by_dim)
    order = {dim: [a.get("id") for a in arms if isinstance(a, dict)] for dim, arms in (pb.get("arms", {}) or {}).items()}
    hook_names = {a.get("id"): a.get("label") for a in (pb.get("arms", {}) or {}).get("hook_type", []) if isinstance(a, dict)}
    rng = np.random.default_rng(20260929)  # fixed seed: the same DB always renders the same intervals
    bandits = []
    dims = [d for d in ("slot", "hook_type", "duration_band") if d in by_dim] + sorted(d for d in by_dim if d not in ("slot", "hook_type", "duration_band"))
    for dim in dims:
        arms = by_dim[dim]
        ordr = order.get(dim, [])
        arms.sort(key=lambda a: (ordr.index(a["arm"]) if a["arm"] in ordr else 999, str(a["arm"])))
        A = np.array([max(float(a["alpha"] or 1), 1e-3) for a in arms])
        B = np.array([max(float(a["beta"] or 1), 1e-3) for a in arms])
        samples = rng.beta(A[:, None], B[:, None], size=(len(arms), 4000))
        lo, hi = np.quantile(samples, [0.05, 0.95], axis=1)
        best = np.bincount(np.argmax(samples, axis=0), minlength=len(arms)) / samples.shape[1]
        means = A / (A + B)
        # a "leader" needs the same minimum evidence the learner uses for claims
        tried = [i for i, a in enumerate(arms) if (a["n"] or 0) >= col.min_n]
        lead = max(tried, key=lambda i: means[i]) if tried else None
        rows = []
        for i, a in enumerate(arms):
            label = _slot_label(a["arm"]) if dim == "slot" else (hook_names.get(a["arm"]) if dim == "hook_type" and hook_names.get(a["arm"]) else (f"{a['arm']} s" if dim == "duration_band" else _humanize(a["arm"])))
            rows.append({
                "label": label, "arm": a["arm"], "mean": float(means[i]), "lo": float(lo[i]), "hi": float(hi[i]),
                "n": int(a["n"] or 0), "wins": int(a["wins"] or 0), "p_best": float(best[i]), "lead": i == lead,
            })
        total_n = sum(r["n"] for r in rows)
        lw = 132 if dim == "hook_type" else 104
        bandits.append({
            "dim": dim, "label": DIM_LABELS.get(dim, _humanize(dim)), "hint": DIM_HINTS.get(dim, ""), "rows": rows, "n": total_n,
            "lead": rows[lead] if lead is not None else None,
            "svg": C.interval_rows(f"arm-{dim}", f"{DIM_LABELS.get(dim, dim)}: posterior chance to beat the median",
                                    "Each row is an arm: the bar is the 90% credible interval, the dot the posterior mean. The 50% line is no signal.",
                                    rows, width=340, label_w=lw),
        })
    ctx["bandits"] = bandits
    ctx["priors_only"] = priors_only
    decs = _rows(conn, "SELECT ts, reel_id, dimension, arm, explore FROM decisions ORDER BY ts DESC LIMIT 60")
    ctx["explore"] = {"n": len(decs), "share": (sum(1 for d in decs if d["explore"]) / len(decs)) if decs else None,
                      "min": float(cfg.get("learning.explore_min", 0.25)), "until": int(cfg.get("learning.explore_until_posts", 30))}

    # experiments board
    exps = _rows(conn, "SELECT id, hypothesis, dimension, arms_json, status, started_at, ended_at, result FROM experiments ORDER BY id")
    hyps = {h.get("id"): h for h in pb.get("hypotheses", []) or [] if isinstance(h, dict)}
    if not exps:
        for e in pb.get("experiments", []) or []:
            if isinstance(e, dict):
                h = hyps.get(e.get("hypothesis"), {})
                exps.append({"id": e.get("id"), "hypothesis": h.get("text") or e.get("hypothesis") or "", "dimension": e.get("dimension"), "status": e.get("status"), "started_at": None, "ended_at": None, "result": None, "metric": h.get("metric")})
    for e in exps:
        hid = e.get("hypothesis")
        if hid in hyps:
            e["metric"] = hyps[hid].get("metric")
            e["hypothesis"] = hyps[hid].get("text") or hid
        e.setdefault("metric", None)
        e["started"] = _ts(e.get("started_at"))
        e["ended"] = _ts(e.get("ended_at"))
        st = str(e.get("status") or "").lower()
        e["col"] = "parked" if st in ("parked", "dropped") else st
        e["dropped"] = st == "dropped"
    board = [{"key": k, "label": lab, "hint": hint, "cards": [e for e in exps if e["col"] == k]} for k, lab, hint in EXP_COLUMNS]
    ctx["board"] = board
    ctx["exp_count"] = len(exps)

    # logged factors
    scored = [r for r in published if r["composite"] is not None]
    ref = _median([r["composite"] for r in scored])
    factors = []
    lf = pb.get("logged_factors", {}) or {}
    for fac in ("cta_type", "hashtag_count", "caption_style"):
        groups: dict[str, list[float]] = defaultdict(list)
        for r in scored:
            f = r["features"]
            if fac == "hashtag_count":
                v = f.get("hashtag_count")
                if v is None:
                    v = len(f.get("hashtags") or r["hashtags"] or [])
            else:
                v = f.get(fac) or (r["caption"].get(fac) if fac in ("cta_type", "caption_style") else None)
            if v is None or v == "":
                continue
            groups[str(v)].append(r["composite"])
        levels = [str(x) for x in (lf.get(fac) or [])]
        keys = [k for k in levels if k in groups] + sorted(k for k in groups if k not in levels)
        rows = []
        for k in keys:
            vals = groups[k]
            lab = f"{k} tags" if fac == "hashtag_count" else LEVEL_LABELS.get(k, _humanize(k))
            rows.append({"label": lab, "values": vals, "mean": _mean(vals), "n": len(vals), "enough": len(vals) >= col.min_n})
        factors.append({
            "key": fac, "label": FACTOR_LABELS[fac], "rows": rows, "levels": levels,
            "svg": C.strip_plot(f"lf-{fac}", f"{FACTOR_LABELS[fac]} vs composite score",
                                "Each gray dot is one published reel; the violet tick is the level mean (gray when n is below the minimum). The vertical line is the median of all reels.",
                                rows, width=340, label_w=104, ref=ref, ref_label="All-reel median") if rows else Markup(""),
        })
    ctx["factors"] = factors
    ctx["scored_n"] = len(scored)
    ctx["min_n"] = col.min_n

    # ---------------------------------------------------------------- brief
    br = _rows(conn, "SELECT week_start, created_at, json FROM briefs ORDER BY week_start DESC, created_at DESC LIMIT 1")
    brief = None
    if br:
        bj = _j(br[0]["json"], {})
        scripts = [s for s in (bj.get("scripts") or []) if isinstance(s, dict)]
        for s in scripts:
            s["beats"] = [str(b) for b in (s.get("beats") or [])][:6]
        brief = {"week_start": bj.get("week_start") or br[0]["week_start"], "generated": _ts(bj.get("generated_at") or br[0]["created_at"]), "scripts": scripts, "notes": bj.get("notes") or ""}
    ctx["brief"] = brief
    ctx["pillar_names"] = {r["pillar"]: r["pillar_label"] for r in reels if r["pillar"]}
    for t in pb.get("topics", []) or []:
        if isinstance(t, dict) and t.get("id") and t["id"] not in ctx["pillar_names"]:
            m = re.search(r"\(([^)]+)\)", str(t.get("label") or ""))
            ctx["pillar_names"][t["id"]] = PILLAR_SHORT.get(t["id"]) or (_humanize(m.group(1)) if m else _humanize(t["id"]))
    ctx["hook_names"] = hook_names

    # ---------------------------------------------------------------- health
    tz = cfg.tz
    ticks = [t for t in tick_rows if t["kind"] == "tick"]
    for t in tick_rows:
        t["start"] = _ts(t["started_at"])
        t["end"] = _ts(t["finished_at"])
        t["dur"] = (t["end"] - t["start"]).total_seconds() if t["start"] and t["end"] else None
    days = []
    for back in (2, 1, 0):
        d = today - timedelta(days=back)
        cells = []
        for slot in range(96):
            start_local = datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(minutes=15 * slot)
            cells.append({"status": None, "future": start_local > now.astimezone(tz), "tip": f"{d.isoformat()} {start_local:%H:%M} · no tick"})
        days.append({"label": d.isoformat(), "date": d, "cells": cells})
    rank = {"error": 4, "paused": 3, "locked": 2, "skipped": 1, "ok": 0}
    day_idx = {dd["date"]: dd for dd in days}
    for t in ticks:
        if not t["start"]:
            continue
        loc = t["start"].astimezone(tz)
        dd = day_idx.get(loc.date())
        if not dd:
            continue
        slot = (loc.hour * 60 + loc.minute) // 15
        cell = dd["cells"][slot]
        st = t["status"] or "ok"
        if cell["status"] is None or rank.get(st, 0) > rank.get(cell["status"], 0):
            cell["status"] = st
            cell["tip"] = f"{loc:%Y-%m-%d %H:%M} · {st}" + (f" · {fmt_secs(t['dur'])}" if t["dur"] is not None else "") + (f" · {t['error'][:120]}" if t.get("error") else "")
    ctx["tick_strip"] = C.dual(C.status_strip, "ticks", "Tick history, last 3 days",
                               "One cell per 15-minute tick: green ok, red error, gray skipped or locked, amber paused, dark no tick.",
                               days, wide=1160, narrow=326, label_w=92) if ticks else Markup("")
    dur_ticks = [t for t in ticks if t["dur"] is not None]
    marks = [{"i": i, "value": t["dur"], "color": C.STATUS["critical"], "tip": f"{ck.dt(t['start'])} error: {(t['error'] or '')[:120]}"} for i, t in enumerate(dur_ticks) if t["status"] == "error"]
    ctx["dur_chart"] = C.dual(C.line_chart, "tdur", "Tick duration", "Seconds each 15-minute tick took over the last 3 days; red dots are ticks that ended in an error.",
                              [ck.local(t["start"]).strftime("%m-%d %H:%M") for t in dur_ticks],
                              [{"name": "Duration", "values": [t["dur"] for t in dur_ticks], "color": C.ACCENT, "emphasis": True, "end_label": False}],
                              wide=1160, narrow=326, height=180, y_fmt=lambda v: f"{v:.0f}s", markers=marks) if len(dur_ticks) >= 2 else Markup("")
    ok_n = sum(1 for t in ticks if t["status"] == "ok")
    ctx["tick_stats"] = {
        "n": len(ticks), "ok": ok_n, "err": sum(1 for t in ticks if t["status"] == "error"),
        "other": len(ticks) - ok_n - sum(1 for t in ticks if t["status"] == "error"),
        "last": ticks[-1] if ticks else None,
        "median": _median([t["dur"] for t in dur_ticks]),
        "p95": (sorted(t["dur"] for t in dur_ticks)[int(0.95 * (len(dur_ticks) - 1))] if dur_ticks else None),
        "last_daily": next((t for t in reversed(tick_rows) if t["kind"] == "daily"), None) or _last_kind(conn, "daily"),
        "last_weekly": next((t for t in reversed(tick_rows) if t["kind"] == "weekly"), None) or _last_kind(conn, "weekly"),
    }
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
    errors = []
    for t in _rows(conn, "SELECT kind, started_at, error FROM ticks WHERE status='error' AND started_at >= ? ORDER BY started_at DESC LIMIT 20", (week_ago,)):
        errors.append({"t": _ts(t["started_at"]), "src": f"{t['kind']}", "msg": t["error"] or "error"})
    calls = _rows(conn, "SELECT ts, task, model, reel_id, duration_s, attempts, ok, error FROM brain_calls WHERE ts >= ? ORDER BY ts DESC", (week_ago,))
    for c in calls:
        if not c["ok"]:
            errors.append({"t": _ts(c["ts"]), "src": f"brain · {c['task']}", "msg": c["error"] or "failed"})
    for n in _rows(conn, "SELECT ts, title, body FROM notifications WHERE level='error' AND ts >= ? ORDER BY ts DESC LIMIT 10", (week_ago,)):
        errors.append({"t": _ts(n["ts"]), "src": "notification", "msg": f"{n['title']}: {n['body'] or ''}".strip(": ")})
    errors.sort(key=lambda e: e["t"] or now, reverse=True)
    ctx["errors"] = errors[:12]
    by_task: dict[str, dict] = defaultdict(lambda: {"n": 0, "fail": 0, "dur": []})
    for c in calls:
        b = by_task[c["task"]]
        b["n"] += 1
        b["fail"] += 0 if c["ok"] else 1
        if _num(c["duration_s"]) is not None:
            b["dur"].append(_num(c["duration_s"]))
    ctx["brain"] = {
        "n": len(calls), "fail": sum(1 for c in calls if not c["ok"]),
        "tasks": sorted(({"task": k, "n": v["n"], "fail": v["fail"], "avg": _mean(v["dur"])} for k, v in by_task.items()), key=lambda x: -x["n"]),
        "avg": _mean([_num(c["duration_s"]) for c in calls if _num(c["duration_s"]) is not None]),
    }
    ctx["token"] = token
    tdl = token["days_left"]
    ctx["token_meter"] = C.meter("Days left on the 60-day token", (tdl or 0) / 60, "good" if (tdl or 0) > 14 else ("warning" if (tdl or 0) > 7 else "critical")) if tdl is not None else Markup("")
    usage = _rows(conn, "SELECT ts, endpoint, http_status, app_usage_pct, quota_usage, quota_total FROM api_usage ORDER BY ts DESC LIMIT 200")
    q = next((u for u in usage if u["quota_total"]), None)
    appu = next((u for u in usage if u["app_usage_pct"] is not None), None)
    ctx["quota"] = None
    if q:
        frac = (q["quota_usage"] or 0) / q["quota_total"]
        ctx["quota"] = {"used": q["quota_usage"] or 0, "total": q["quota_total"], "t": _ts(q["ts"]), "meter": C.meter("Publishing quota used", frac, "good" if frac < 0.6 else ("warning" if frac < 0.9 else "critical"))}
    ctx["app_usage"] = None
    if appu:
        p = float(appu["app_usage_pct"])
        lim = float(cfg.get("instagram.app_usage_backoff_pct", 75))
        ctx["app_usage"] = {"pct": p, "limit": lim, "t": _ts(appu["ts"]), "meter": C.meter("App usage", p / 100, "good" if p < lim * 0.6 else ("warning" if p < lim else "critical"))}
    ctx["api_calls_24h"] = sum(1 for u in usage if (_ts(u["ts"]) or now) >= now - timedelta(hours=24))
    try:
        du = shutil.disk_usage(cfg.data_dir if cfg.data_dir.exists() else cfg.root)
        free_gb = du.free / 1024 ** 3
        min_gb = float(cfg.get("backup.min_free_gb", 20))
        ctx["disk"] = {"free_gb": free_gb, "total_gb": du.total / 1024 ** 3, "min_gb": min_gb, "ok": free_gb >= min_gb,
                       "meter": C.meter("Disk used", 1 - du.free / du.total, "good" if free_gb >= min_gb * 2 else ("warning" if free_gb >= min_gb else "critical"))}
    except OSError:
        ctx["disk"] = None
    ctx["gpu"] = gpu_info(cfg.data_dir / "cache" / "gpu.json" if cfg.data_dir.exists() else None)
    bk = _rows(conn, "SELECT ts, path, size_bytes, onedrive_copy, ok FROM backups ORDER BY ts DESC LIMIT 7")
    for b in bk:
        b["t"] = _ts(b["ts"])
        b["name"] = Path(str(b["path"])).name
    ctx["backups"] = bk

    # ---------------------------------------------------------------- money
    ctx["service_enabled"] = bool(cfg.get("cta.service_enabled", False))
    taps = [(a["day"], _num(a["m"].get("profile_links_taps"))) for a in acct[-28:]]
    taps = [(d, v) for d, v in taps if v is not None]
    ctx["taps_total"] = sum(v for _, v in taps) if taps else None
    ctx["taps_7"] = sum(v for d, v in taps if d >= (today - timedelta(days=6)).isoformat()) if taps else None
    ctx["taps_chart"] = C.dual(C.column_chart, "taps", "Link taps per day", "Taps on the profile link per day, last 28 days (account insights).",
                               [{"label": d[5:], "value": v, "tip": f"{d}: {C.fmt_int(v)} link taps"} for d, v in taps],
                               wide=1160, narrow=326, height=200, y_fmt=C.fmt_int) if taps else Markup("")
    ctx["taps"] = taps
    orders = _rows(conn, "SELECT day, amount_sar, item, note, created_at, is_demo FROM orders" + ("" if demo else " WHERE is_demo=0") + " ORDER BY day DESC, id DESC")  # nosec B608 - constant clause
    ctx["orders"] = orders
    ctx["orders_total"] = sum(_num(o["amount_sar"]) or 0 for o in orders)

    collect_calendar(ctx, cfg, conn, demo, today)
    collect_accounts(ctx, cfg, conn, demo)
    from .api import resolve_uploads

    ctx["uploads"] = resolve_uploads(conn, cfg.inbox)
    ctx["upload_max_mb"] = int(cfg.get("upload.max_mb", 2048))
    ctx["upload_exts"] = ", ".join(e for e in cfg["intake"]["extensions"] if e in (".mp4", ".mov", ".m4v"))
    return ctx


log = get_logger("dashboard.build")
CATEGORY_LABELS = {"national": "National", "religious": "Religious", "international": "International", "tech": "Tech", "career": "Career"}
RULE_LABELS = {"gregorian": "Every year (Gregorian)", "hijri": "Every year (Hijri, Umm al-Qura)", "once": "One-off date"}


def _no_scheme(url: str) -> str:
    """Source links are shown as copyable text: the static site carries no external URLs."""
    return re.sub(r"^https?://", "", str(url or ""))


def collect_calendar(ctx: dict, cfg, conn: sqlite3.Connection, demo: bool, today) -> None:
    from .. import occasions as oc

    occs = oc.all_occasions(conn, include_demo=demo) if _table_exists(conn, "occasions") else []
    if not demo:
        occs = [o for o in occs if not o.get("is_demo")]
    lo, hi = int(cfg.get("calendar.lookahead_min_days", 3)), int(cfg.get("calendar.lookahead_max_days", 21))
    lead = int(cfg.get("calendar.film_lead_days", 3))

    def view(x) -> dict:
        d = x.as_dict()
        d["source_text"] = _no_scheme(d["source_url"])
        d["cat_label"] = CATEGORY_LABELS.get(d["category"], d["category"])
        return d

    soon = [view(x) for x in oc.between(occs, today, today + timedelta(days=hi), today, lead)]
    months = []
    y, m = today.year, today.month
    for _ in range(3):
        first = today.replace(year=y, month=m, day=1)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        grid = oc.month_grid(y, m, oc.between(occs, first - timedelta(days=7), last + timedelta(days=7), today, lead), today)
        agenda = []
        for wk in grid["weeks"]:
            for cell in wk:
                items = [{"slug": x.occasion["slug"], "name_en": x.occasion["name_en"], "name_ar": x.occasion["name_ar"],
                          "category": x.occasion["category"], "first": x.start == cell["date"], "hijri": x.hijri} for x in cell["items"]]
                cell["items"] = items
                cont = [it for it in items if not it["first"]]
                cell["cont"] = cont[0]["category"] if cont and not any(it["first"] for it in items) else ""
                if cell["in_month"]:
                    for it in items:
                        if it["first"]:
                            agenda.append({**it, "date": cell["date"].isoformat(), "label": cell["date"].strftime("%a %d"), "today": cell["today"]})
        grid["agenda"] = agenda
        months.append(grid)
        m += 1
        if m == 13:
            y, m = y + 1, 1
    rows = []
    year_ahead: dict = {}
    for x in oc.between(occs, today, today + timedelta(days=400), today, lead):  # sorted by date: keep the first
        year_ahead.setdefault(x.occasion["slug"], x)
    for o in occs:
        nxt = year_ahead.get(o["slug"])
        rows.append({**o, "source_text": _no_scheme(o.get("source_url")), "cat_label": CATEGORY_LABELS.get(o["category"], o["category"]),
                     "rule_label": RULE_LABELS.get(o["rule"], o["rule"]),
                     "date_text": (f"{o['year']}-{o['month']:02d}-{o['day']:02d}" if o["rule"] == "once"
                                   else (f"{o['day']} {oc.HIJRI_MONTHS_EN[o['month'] - 1]}" if o["rule"] == "hijri"
                                         else datetime(2028, o["month"], o["day"]).strftime("%d %b"))),
                     "next": nxt.start.isoformat() if nxt else None, "next_days": nxt.days_until if nxt else None,
                     "keywords_text": ", ".join(o.get("keywords") or []),
                     "json": json.dumps({k: o.get(k) for k in ("slug", "name_ar", "name_en", "rule", "month", "day", "year", "duration_days",
                                                               "category", "official", "angle", "notes", "source_url", "checked_on")}
                                        | {"keywords": ", ".join(o.get("keywords") or []), "source_url": _no_scheme(o.get("source_url"))},
                                        ensure_ascii=False)})
    rows.sort(key=lambda r: (r["next"] or "9999", r["slug"]))
    ctx["calendar"] = {
        "soon": soon, "window": [x for x in soon if lo <= x["days_until"] <= hi], "months": months, "rows": rows,
        "lo": lo, "hi": hi, "lead": lead, "categories": CATEGORY_LABELS, "rules": RULE_LABELS,
        "weekdays": ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"),
    }
    collect_coming_up(ctx, cfg, conn, demo, today)


def collect_coming_up(ctx: dict, cfg, conn: sqlite3.Connection, demo: bool, today) -> None:
    """v1.2 "Coming up": occasions inside the reminder window with the next reminder, ideas and checklist.
    Read-only: cached ideas, or template ideas made on the fly; never a brain call during a build."""
    from .. import occasions as oc

    items = []
    try:
        rows = oc.upcoming_reminders(conn, cfg, today, include_demo=demo) if _table_exists(conn, "occasions") else []
    except Exception as exc:  # noqa: BLE001 - the dashboard must build even if this part can't
        log.warning("coming-up collector failed: %s", exc)
        rows = []
    for x in rows:
        items.append({
            "slug": x["slug"], "name_ar": x["name_ar"], "name_en": x["name_en"], "category": x["category"],
            "cat_label": CATEGORY_LABELS.get(x["category"], x["category"]), "official": x["official"],
            "start": x["start"], "end": x["end"], "days_until": x["days_until"], "approximate": x["approximate"],
            "hijri": x["hijri"], "hijri_ar": x["hijri_ar"], "film_by": x["film_by"], "angle": x["angle"],
            "checked_on": x["checked_on"], "source_text": _no_scheme(x["source_url"]),
            "next_lead": x["next_lead"], "ideas_source": x["ideas_source"],
            "ideas": [{"title": i.get("title", ""), "hook_line": i.get("hook_line", ""), "beats": list(i.get("beats") or []),
                       "shot_note": i.get("shot_note", ""), "length_s": i.get("length_s")} for i in x["ideas"]],
            "checklist": list(x["checklist"]),
        })
    ctx["coming_up"] = items


def collect_accounts(ctx: dict, cfg, conn: sqlite3.Connection, demo: bool) -> None:
    from .. import accounts as ac

    if not _table_exists(conn, "accounts"):
        ctx["accounts"] = {"rows": [], "analysis": None, "by_acc": {}, "stats": {}, "recs": {}}
        return
    rows = ac.list_accounts(conn, include_demo=demo)
    if not demo:
        rows = [r for r in rows if not r.get("is_demo")]
    for r in rows:
        r["last_import_t"] = _ts(r.get("last_import"))
        r["added_t"] = _ts(r.get("added_at"))
    an = ac.latest_analysis(conn)
    by_acc = {a["account"]: a for a in (an or {}).get("accounts", [])} if an else {}
    stats = {s["account"]: s for s in ((an or {}).get("stats", {}) or {}).get("stats", [])} if an else {}
    recs: dict = {}
    for r in rows:
        r["analysis"] = by_acc.get(r["pseudonym"])
        r["stats"] = stats.get(r["pseudonym"])
        r["niche"] = r.get("niche") or ""
        try:
            rec = ac.latest_recommendation(conn, r["username"], cfg=cfg)
        except Exception as exc:  # noqa: BLE001 - an old DB or a bad row must not break the build
            log.warning("recommendation for the accounts page unavailable: %s", exc)
            rec = None
        if rec:
            rec["created"] = _ts(rec["created"])
            recs[r["username"]] = rec
    ctx["accounts"] = {
        "rows": rows, "analysis": an, "created": _ts(an["created_at"]) if an else None,
        "own_analysis": by_acc.get("OWN"),
        "retention_days": int(cfg.get("accounts.retention_days", 90)),
        "metrics_retention_days": int(cfg.get("accounts.metrics_retention_days", 180)),
        "discovery": cfg.get("accounts.discovery", "off"),
        "csv_columns": ",".join(ac.CSV_COLUMNS),
        "recs": recs,
    }


def _last_kind(conn: sqlite3.Connection, kind: str) -> dict | None:
    r = _rows(conn, "SELECT kind, started_at, finished_at, status, error FROM ticks WHERE kind=? ORDER BY started_at DESC LIMIT 1", (kind,))
    if not r:
        return None
    t = r[0]
    t["start"] = _ts(t["started_at"])
    t["end"] = _ts(t["finished_at"])
    t["dur"] = (t["end"] - t["start"]).total_seconds() if t["start"] and t["end"] else None
    return t


# ============================================================================ render
_ENV: Environment | None = None


def _env(cache_dir: Path | None = None) -> Environment:
    """Jinja environment; compiled templates are cached on disk because every tick is a new process."""
    global _ENV
    if _ENV is None:
        bcc = None
        if cache_dir is not None:
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                bcc = FileSystemBytecodeCache(str(cache_dir))
            except OSError:
                bcc = None
        env = Environment(
            loader=FileSystemLoader(str(TEMPLATES)),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
            auto_reload=False,
            bytecode_cache=bcc,
        )
        env.filters.update(
            {
                "int": C.fmt_int,
                "compact": C.fmt_compact,
                "pct": C.fmt_pct,
                "secs": fmt_secs,
                "bytes": fmt_bytes,
                "val": fmt_value,
                "human": _humanize,
                "dimlabel": lambda d: DIM_NAMES.get(str(d), _humanize(d)),
                "slot": _slot_label,
                "langattrs": lambda s: Markup(' lang="ar" dir="rtl"') if is_ar(s) else Markup(' dir="auto"'),
            }
        )
        env.globals.update(is_ar=is_ar, state_labels=STATE_LABELS)
        _ENV = env
    return _ENV


ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="8" fill="#141416"/>'
    '<rect x="9" y="6" width="14" height="20" rx="3" fill="none" stroke="#B570FF" stroke-width="2.4"/>'
    '<path d="M14 12.5v7l5.5-3.5z" fill="#B570FF"/></svg>'
)


def _write_assets(site: Path, cfg) -> None:
    assets = site / "assets"
    (assets / "fonts").mkdir(parents=True, exist_ok=True)
    for f in STATIC.iterdir():
        if f.is_file() and f.suffix in (".css", ".js"):
            shutil.copyfile(f, assets / f.name)
    (assets / "icon.svg").write_text(ICON_SVG, encoding="utf-8")
    fonts = cfg.fonts_dir
    for src, dst in FONT_FILES.items():
        p = fonts / src
        if p.is_file():
            shutil.copyfile(p, assets / "fonts" / dst)


def render_site(ctx: dict, site: Path, cache_dir: Path | None = None) -> None:
    env = _env(cache_dir)
    (site / "reels").mkdir(parents=True, exist_ok=True)
    for name, label in PAGES:
        tpl = env.get_template(f"{name}.html")
        html = tpl.render(ctx, page=name, page_label=label, root="")
        (site / f"{name}.html").write_text(html, encoding="utf-8", newline="\n")
    tpl = env.get_template("reel.html")
    for d in ctx["details"]:
        html = tpl.render(ctx, page="reels", page_label=f"Reel #{d['r']['id']}", root="../", d=d, r=d["r"])
        (site / "reels" / f"{d['r']['id']}.html").write_text(html, encoding="utf-8", newline="\n")


def _retry(fn, *args, attempts: int = 40, delay: float = 0.05):
    """Windows: a file the server is streaming can block a rename for a moment."""
    for i in range(attempts):
        try:
            return fn(*args)
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)
    return None


def _swap(tmp: Path, out: Path) -> None:
    """Replace `out` with the freshly built `tmp` directory."""
    if not out.exists():
        os.replace(tmp, out)
        return
    old = out.parent / f".{out.name}.old-{os.getpid()}-{secrets.token_hex(3)}"
    try:
        _retry(os.replace, out, old)
    except PermissionError:
        # fall back to replacing file by file (each os.replace is atomic), then prune stale files
        _merge_into(tmp, out)
        shutil.rmtree(tmp, ignore_errors=True)
        return
    try:
        os.replace(tmp, out)
    except OSError:
        os.replace(old, out)  # put the previous site back rather than leave nothing
        raise
    shutil.rmtree(old, ignore_errors=True)


def _merge_into(tmp: Path, out: Path) -> None:
    fresh = set()
    for src in tmp.rglob("*"):
        rel = src.relative_to(tmp)
        dst = out / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        fresh.add(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        _retry(os.replace, src, dst)
    for old in out.rglob("*"):
        if old.is_file() and old.relative_to(out) not in fresh:
            try:
                old.unlink()
            except OSError:
                pass


def _clean_leftovers(out: Path) -> None:
    """Remove temp dirs left by a crashed build (only our own name pattern)."""
    for p in out.parent.glob(f".{out.name}.*"):
        if p.is_dir() and (".building-" in p.name or ".old-" in p.name):
            try:
                if time.time() - p.stat().st_mtime > 600:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass


def build_dashboard(cfg, conn: sqlite3.Connection, out_dir: Path | None = None, demo: bool = False, *, now: datetime | None = None) -> Path:
    """Regenerate the static site. Returns the site directory.

    out_dir defaults to data/dashboard/ (or data/dashboard-demo/ when demo=True).
    Read-only on the DB. Built in a temp dir next to out_dir, then swapped in.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    out = Path(out_dir) if out_dir else cfg.data_dir / ("dashboard-demo" if demo else "dashboard")
    out.parent.mkdir(parents=True, exist_ok=True)
    _clean_leftovers(out)
    ctx = collect(cfg, conn, demo, now)
    tmp = out.parent / f".{out.name}.building-{os.getpid()}-{secrets.token_hex(3)}"
    tmp.mkdir(parents=True)
    try:
        _write_assets(tmp, cfg)
        render_site(ctx, tmp, cache_dir=out.parent / "cache" / "jinja")
        _swap(tmp, out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return out
