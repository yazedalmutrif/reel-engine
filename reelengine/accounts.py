"""Attached Instagram accounts and their analysis (feature 2026-10-14, docs/DECISIONS_PENDING.md).

Non-negotiables (SECURITY.md, API_NOTES.md):
- Official API only. No scraping, no password logins, no unofficial libraries. The engine never fetches
  an instagram.com page: a pasted permalink is stored as text, nothing more.
- Business Discovery (other accounts' public posts) exists only on the Instagram API with *Facebook*
  Login (graph.facebook.com, a Page-linked account, a Facebook-Login token). The engine's Instagram-Login
  path can't use it, so `accounts.discovery = "off"` by default and the data comes from manual import
  (pasted lines or a CSV). The `BusinessDiscoverySource` adapter is ready for the day the auth path exists.
- PDPL minimisation (legal-compliance ruling 2026-10-14): only the fields in `account_media`; never profile
  pictures, bios, locations, comments/commenters or follower lists. Caption text is wiped after
  `retention_days` (90), whole post rows deleted after `metrics_retention_days` (180), `forget` deletes an
  account at once, with secure_delete on. Before anything goes to the brain (Anthropic, US), usernames,
  @mentions and URLs are replaced by pseudonyms (ACC_01, @user); the mapping never leaves the PC.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from . import db
from .log import get_logger, redact

log = get_logger("accounts")

ROLES = ("own", "reference", "competitor")
USERNAME = re.compile(r"^[a-z0-9._]{1,30}$")
PERMALINK = re.compile(r"^https://(?:www\.)?instagram\.com/(?:[A-Za-z0-9._]{1,30}/)?(reel|reels|p|tv)/([A-Za-z0-9_-]{5,40})/?(?:\?[^\s]*)?$")
MEDIA_TYPES = ("REELS", "VIDEO", "IMAGE", "CAROUSEL_ALBUM")
MAX_IMPORT_ROWS = 500
MAX_CAPTION = 2200
CSV_COLUMNS = ("username", "permalink", "posted_at", "media_type", "caption", "likes", "comments", "views", "duration_s")
# the only fields ever requested from Business Discovery (no profile_picture_url, biography, website, ...)
DISCOVERY_MEDIA_FIELDS = "id,caption,like_count,comments_count,view_count,timestamp,media_type,media_product_type,permalink"
DISCOVERY_SECRET = "fb_discovery_token"  # nosec B105 - keyring entry name
COPY_THRESHOLD = 0.6
SHINGLE = 5


class AccountError(ValueError):
    pass


# ------------------------------------------------------------------ accounts
def norm_username(u: str) -> str:
    u = (u or "").strip()
    m = re.match(r"^https://(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})/?$", u)
    if m:
        u = m.group(1)
    u = u.lstrip("@").lower()
    if not USERNAME.match(u):
        raise AccountError("username: 1-30 letters, digits, dots or underscores")
    return u


def _next_pseudonym(conn) -> str:
    n = 1
    for r in conn.execute("SELECT pseudonym FROM accounts"):
        m = re.match(r"^ACC_(\d+)$", r["pseudonym"] or "")
        if m:
            n = max(n, int(m.group(1)) + 1)
    return f"ACC_{n:02d}"


def attach(conn, username: str, role: str, source: str = "manual", notes: str = "", is_demo: bool = False) -> dict:
    u = norm_username(username)
    role = (role or "").strip().lower()
    if role not in ROLES:
        raise AccountError(f"role must be one of {', '.join(ROLES)}")
    if len(notes or "") > 300:
        raise AccountError("notes: max 300 characters")
    row = conn.execute("SELECT id FROM accounts WHERE username=?", (u,)).fetchone()
    with db.tx(conn):
        if role == "own":
            conn.execute("UPDATE accounts SET role='reference' WHERE role='own' AND username<>?", (u,))
        if row:
            conn.execute("UPDATE accounts SET role=?, notes=? WHERE id=?", (role, notes or "", row["id"]))
        else:
            conn.execute("INSERT INTO accounts(username, pseudonym, role, source, notes, added_at, is_demo) VALUES(?,?,?,?,?,?,?)",
                         (u, _next_pseudonym(conn), role, source, notes or "", db.now_iso(), int(is_demo)))
    return get(conn, u)


def get(conn, username: str) -> dict | None:
    r = conn.execute("SELECT * FROM accounts WHERE username=?", (norm_username(username),)).fetchone()
    return dict(r) if r else None


def list_accounts(conn, include_demo: bool = True) -> list[dict]:
    q = ("SELECT a.*, (SELECT COUNT(*) FROM account_media m WHERE m.account_id=a.id) AS posts, "
         "(SELECT MAX(fetched_at) FROM account_media m WHERE m.account_id=a.id) AS last_import FROM accounts a"
         + ("" if include_demo else " WHERE a.is_demo=0") + " ORDER BY CASE role WHEN 'own' THEN 0 WHEN 'reference' THEN 1 ELSE 2 END, username")  # nosec B608 - constant clause
    try:
        return [dict(r) for r in conn.execute(q)]
    except Exception:  # noqa: BLE001 - an old DB without the table
        return []


def forget(conn, username: str) -> bool:
    """Delete an account and every row about it now (secure_delete, then VACUUM by the caller if wanted)."""
    u = norm_username(username)
    conn.execute("PRAGMA secure_delete=ON")
    with db.tx(conn):
        row = conn.execute("SELECT id FROM accounts WHERE username=?", (u,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM account_media WHERE account_id=?", (row["id"],))
        conn.execute("DELETE FROM accounts WHERE id=?", (row["id"],))
    log.info("accounts: forgot one account and its posts")
    return True


def sync_config(conn, cfg) -> list[str]:
    """Attach the accounts listed in config.toml ([accounts] own / reference / competitor)."""
    if not cfg.get("accounts.enabled", True):
        return []
    added = []
    wanted = [(cfg.get("accounts.own", ""), "own")] + [(u, "reference") for u in cfg.get("accounts.reference", [])] + \
             [(u, "competitor") for u in cfg.get("accounts.competitor", [])]
    for u, role in wanted:
        if not u:
            continue
        cur = get(conn, u)
        if cur is None or cur["role"] != role:
            attach(conn, u, role, source="config")
            added.append(norm_username(u))
    return added


# ------------------------------------------------------------------ import
def _int_or_none(v: Any, name: str) -> int | None:
    s = str(v if v is not None else "").strip().replace(",", "").replace("٬", "")
    if s == "":
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kKmM]?)", s)
    if not m:
        raise AccountError(f"{name}: not a number ({s[:20]!r})")
    n = float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6}[m.group(2).lower()]
    return int(n)


def _float_or_none(v: Any, name: str) -> float | None:
    s = str(v if v is not None else "").strip()
    if s == "":
        return None
    try:
        f = float(s)
    except ValueError:
        raise AccountError(f"{name}: not a number") from None
    if not 0 < f <= 3600:
        raise AccountError(f"{name}: 0-3600 seconds")
    return f


def _ts(v: Any, tz) -> str | None:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        s2 = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s.replace("Z", "+00:00").replace("/", "-"))  # Graph sends +0000
        dt = datetime.fromisoformat(s2)
    except ValueError:
        raise AccountError(f"posted_at: use YYYY-MM-DD or YYYY-MM-DD HH:MM ({s[:20]!r})") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def norm_permalink(url: str) -> tuple[str, str]:
    """(canonical permalink without tracking query, media key). Raises AccountError."""
    url = (url or "").strip()
    if re.match(r"^(www\.)?instagram\.com/", url, re.I):
        url = "https://" + url
    m = PERMALINK.match(url)
    if not m:
        raise AccountError("permalink must be an https://www.instagram.com/reel/... or /p/... link")
    kind = "reel" if m.group(1) in ("reel", "reels") else m.group(1)
    return f"https://www.instagram.com/{kind}/{m.group(2)}/", f"code:{m.group(2)}"


def clean_caption(text: str | None) -> str | None:
    if text is None:
        return None
    t = str(text).replace("\r", "").strip()
    t = "".join(c for c in t if c in "\n\t" or ord(c) >= 32)
    return t[:MAX_CAPTION] or None


def _store(conn, account_id: int, rec: dict, source: str) -> bool:
    cur = conn.execute(
        "INSERT INTO account_media(account_id, media_key, permalink, posted_at, media_type, caption, likes, comments, views, duration_s, source, fetched_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id, media_key) DO UPDATE SET "
        "permalink=COALESCE(excluded.permalink, permalink), posted_at=COALESCE(excluded.posted_at, posted_at), "
        "media_type=COALESCE(excluded.media_type, media_type), caption=COALESCE(excluded.caption, caption), "
        "likes=COALESCE(excluded.likes, likes), comments=COALESCE(excluded.comments, comments), views=COALESCE(excluded.views, views), "
        "duration_s=COALESCE(excluded.duration_s, duration_s), source=excluded.source, fetched_at=excluded.fetched_at",
        (account_id, rec["media_key"], rec.get("permalink"), rec.get("posted_at"), rec.get("media_type"), rec.get("caption"),
         rec.get("likes"), rec.get("comments"), rec.get("views"), rec.get("duration_s"), source, db.now_iso()),
    )
    return cur.rowcount > 0


def _record(row: dict, tz) -> dict:
    rec: dict[str, Any] = {}
    if row.get("permalink"):
        rec["permalink"], rec["media_key"] = norm_permalink(row["permalink"])
    rec["posted_at"] = _ts(row.get("posted_at"), tz)
    mt = str(row.get("media_type") or "").strip().upper()
    if mt and mt not in MEDIA_TYPES:
        raise AccountError(f"media_type must be one of {', '.join(MEDIA_TYPES)}")
    rec["media_type"] = mt or ("REELS" if (row.get("permalink") or "").find("/reel") > 0 else None)
    rec["caption"] = clean_caption(row.get("caption"))
    rec["likes"] = _int_or_none(row.get("likes"), "likes")
    rec["comments"] = _int_or_none(row.get("comments"), "comments")
    rec["views"] = _int_or_none(row.get("views"), "views")
    rec["duration_s"] = _float_or_none(row.get("duration_s"), "duration_s")
    if "media_key" not in rec:
        basis = f"{rec['posted_at'] or ''}|{rec['caption'] or ''}"
        if basis == "|":
            raise AccountError("each post needs a permalink, or at least a date and a caption")
        rec["media_key"] = "manual:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
    return rec


def import_rows(conn, cfg, rows: Iterable[dict], default_username: str | None = None, source: str = "manual") -> dict:
    """Validate and store rows (dicts with CSV_COLUMNS). Unknown usernames are attached as 'reference'."""
    res: dict[str, Any] = {"stored": 0, "errors": [], "accounts": set()}
    tz = cfg.tz
    batch = []
    for i, row in enumerate(rows, 1):
        if i > MAX_IMPORT_ROWS:
            res["errors"].append(f"stopped after {MAX_IMPORT_ROWS} rows")
            break
        try:
            u = norm_username(row.get("username") or default_username or "")
            batch.append((u, _record(row, tz)))
        except AccountError as exc:
            res["errors"].append(f"row {i}: {exc}")
    cap = int(cfg.get("accounts.max_media_per_account", 30))
    for u, rec in batch:
        acc = get(conn, u) or attach(conn, u, "reference")
        with db.tx(conn):
            if _store(conn, acc["id"], rec, source):
                res["stored"] += 1
        res["accounts"].add(u)
    for u in res["accounts"]:
        _trim(conn, get(conn, u)["id"], cap)
    res["accounts"] = sorted(res["accounts"])
    return res


def _trim(conn, account_id: int, keep: int) -> None:
    """Keep only the newest `keep` posts of an account (minimisation)."""
    conn.execute("DELETE FROM account_media WHERE account_id=? AND id NOT IN (SELECT id FROM account_media WHERE account_id=? "
                 "ORDER BY COALESCE(posted_at, fetched_at) DESC, id DESC LIMIT ?)", (account_id, account_id, keep))


def parse_csv(text: str) -> list[dict]:
    rdr = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not rdr.fieldnames:
        raise AccountError("the CSV is empty")
    cols = [c.strip().lower() for c in rdr.fieldnames]
    unknown = set(cols) - set(CSV_COLUMNS)
    if unknown:
        raise AccountError(f"unknown CSV columns: {', '.join(sorted(unknown))} (allowed: {', '.join(CSV_COLUMNS)})")
    if "permalink" not in cols and not {"posted_at", "caption"} <= set(cols):
        raise AccountError("the CSV needs a permalink column, or posted_at + caption")
    out = []
    for r in rdr:
        out.append({k.strip().lower(): (v or "") for k, v in r.items() if k})
    return out


def parse_paste(text: str) -> list[dict]:
    """Pasted lines: a CSV with a header row, or one post per line:
    URL | YYYY-MM-DD HH:MM | likes | comments | views | seconds | caption   (only the URL is required)."""
    text = (text or "").strip()
    if not text:
        return []
    first = text.splitlines()[0].lower()
    if "," in first and ("permalink" in first or "caption" in first):
        return parse_csv(text)
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"\s*\|\s*|\t", line)]
        keys = ("permalink", "posted_at", "likes", "comments", "views", "duration_s", "caption")
        rec = dict(zip(keys, parts[:6] + ([" | ".join(parts[6:])] if len(parts) > 6 else [])))
        out.append(rec)
    return out


# ------------------------------------------------------------------ retention
def prune(conn, cfg, now: datetime | None = None) -> dict:
    """Wipe caption text after retention_days and delete post rows after metrics_retention_days.
    The own account's rows follow the same clock (the engine's own reels live in `reels`, untouched)."""
    now = now or db.utcnow()
    cap_cut = (now - timedelta(days=int(cfg.get("accounts.retention_days", 90)))).isoformat(timespec="seconds")
    row_cut = (now - timedelta(days=int(cfg.get("accounts.metrics_retention_days", 180)))).isoformat(timespec="seconds")
    conn.execute("PRAGMA secure_delete=ON")
    with db.tx(conn):
        wiped = conn.execute("UPDATE account_media SET caption=NULL WHERE caption IS NOT NULL AND fetched_at < ?", (cap_cut,)).rowcount
        deleted = conn.execute("DELETE FROM account_media WHERE fetched_at < ?", (row_cut,)).rowcount
        old_an = conn.execute("DELETE FROM account_analyses WHERE created_at < ?", (row_cut,)).rowcount
    return {"captions_wiped": wiped, "posts_deleted": deleted, "analyses_deleted": old_an}


# ------------------------------------------------------------------ pseudonymise + stats
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_MENTION = re.compile(r"(?<![\w@])@[A-Za-z0-9._]{1,30}")


def pseudonymise(text: str | None, names: dict[str, str]) -> str:
    """Replace known usernames with their pseudonym, any other @mention with @user, and drop URLs."""
    t = _URL.sub("[link]", text or "")

    def repl(m: re.Match) -> str:
        u = m.group(0)[1:].lower()
        return names.get(u, "@user")

    t = _MENTION.sub(repl, t)
    for u, p in names.items():
        t = re.sub(rf"(?<![\w.]){re.escape(u)}(?![\w])", p, t, flags=re.I)
    return t


def _hour_local(iso: str | None, tz) -> int | None:
    t = db.parse_ts(iso) if iso else None
    return t.astimezone(tz).hour if t else None


def _engagement(m: dict) -> float | None:
    parts = [m.get("likes"), m.get("comments")]
    if all(p is None for p in parts):
        return None
    return float(sum(p or 0 for p in parts))


def account_stats(conn, cfg, include_demo: bool = True) -> list[dict]:
    """Deterministic per-account numbers (no text): the brain only interprets them."""
    tz = cfg.tz
    out = []
    for a in list_accounts(conn, include_demo):
        rows = [dict(r) for r in conn.execute("SELECT * FROM account_media WHERE account_id=? ORDER BY posted_at DESC", (a["id"],))]
        hours = Counter(h for r in rows if (h := _hour_local(r["posted_at"], tz)) is not None)
        days = Counter(db.parse_ts(r["posted_at"]).astimezone(tz).strftime("%a") for r in rows if r["posted_at"])
        lens = [r["duration_s"] for r in rows if r["duration_s"]]
        views = [r["views"] for r in rows if r["views"] is not None]
        eng = [e for r in rows if (e := _engagement(r)) is not None]
        ranked = sorted(rows, key=lambda r: (r["views"] if r["views"] is not None else -1, _engagement(r) or -1), reverse=True)
        out.append({
            "account": a["pseudonym"], "username": a["username"], "role": a["role"], "posts": len(rows),
            "types": dict(Counter(r["media_type"] or "unknown" for r in rows)),
            "median_length_s": round(statistics.median(lens), 1) if lens else None,
            "median_views": statistics.median(views) if views else None,
            "median_engagement": statistics.median(eng) if eng else None,
            "top_hours": [f"{h:02d}:00" for h, _ in hours.most_common(3)],
            "top_days": [d for d, _ in days.most_common(3)],
            "top_posts": [{"post": f"{a['pseudonym']}-P{rows.index(r) + 1}", "views": r["views"], "likes": r["likes"],
                           "comments": r["comments"], "length_s": r["duration_s"], "posted": r["posted_at"]} for r in ranked[:3]],
        })
    return out


def own_stats(conn, cfg, include_demo: bool = False) -> dict:
    """The owner's published reels from the engine's own DB (hook type, length, slot, views)."""
    where = "" if include_demo else " AND r.is_demo=0"
    rows = conn.execute(
        "SELECT r.features_json, r.duration_s, r.published_at, (SELECT metrics_json FROM insights i WHERE i.reel_id=r.id AND i.fetched_at IS NOT NULL "
        "ORDER BY checkpoint_h DESC LIMIT 1) AS m FROM reels r WHERE r.state='published'" + where).fetchall()  # nosec B608 - constant clause
    hooks = Counter()
    perf: dict[str, list[float]] = {}
    for r in rows:
        f = db.jload(r["features_json"], {}) or {}
        m = db.jload(r["m"], {}) or {}
        ht = f.get("hook_type") or "unknown"
        hooks[ht] += 1
        if isinstance(m.get("views"), (int, float)):
            perf.setdefault(ht, []).append(float(m["views"]))
    return {"posts": len(rows), "hook_types": dict(hooks),
            "median_views_by_hook": {k: statistics.median(v) for k, v in perf.items() if v}}


def brain_input(conn, cfg, include_demo: bool = True, per_account: int = 15) -> dict:
    """Pseudonymised posts + stats for the brain. No username, @mention or URL leaves the PC."""
    accts = list_accounts(conn, include_demo)
    names = {a["username"]: a["pseudonym"] for a in accts}
    posts = []
    for a in accts:
        rows = conn.execute("SELECT * FROM account_media WHERE account_id=? ORDER BY posted_at DESC LIMIT ?", (a["id"], per_account)).fetchall()
        for k, r in enumerate(rows, 1):
            posts.append({"post": f"{a['pseudonym']}-P{k}", "account": a["pseudonym"], "role": a["role"], "type": r["media_type"],
                          "posted_riyadh": db.parse_ts(r["posted_at"]).astimezone(cfg.tz).strftime("%a %H:%M") if r["posted_at"] else None,
                          "length_s": r["duration_s"], "views": r["views"], "likes": r["likes"], "comments": r["comments"],
                          "caption": pseudonymise(r["caption"], names)[:400] if r["caption"] else None})
    stats = account_stats(conn, cfg, include_demo)
    for s in stats:
        s.pop("username", None)
    return {"accounts": [{"account": a["pseudonym"], "role": a["role"]} for a in accts], "stats": stats, "posts": posts,
            "own": own_stats(conn, cfg, include_demo)}


# ------------------------------------------------------------------ copy guard
def _shingles(text: str) -> set[tuple[str, ...]]:
    from .gates import normalize

    words = re.findall(r"[\w؀-ۿ]+", normalize(text or ""))
    return {tuple(words[i:i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def copy_overlap(text: str, captions: Iterable[str]) -> float:
    a = _shingles(text)
    if len(a) < 2:
        return 0.0
    best = 0.0
    for c in captions:
        b = _shingles(c)
        if b:
            best = max(best, len(a & b) / len(a))
    return best


def stored_captions(conn) -> list[str]:
    try:
        return [r[0] for r in conn.execute(
            "SELECT m.caption FROM account_media m JOIN accounts a ON a.id=m.account_id WHERE a.role<>'own' AND m.caption IS NOT NULL")]
    except Exception:  # noqa: BLE001 - old DB
        return []


def copy_hits(conn, texts: dict[str, str]):
    """Hard hits for generated text that copies a stored reference/competitor caption (>= 60% of its 5-grams)."""
    from .gates import Hit

    caps = stored_captions(conn)
    if not caps:
        return []
    hits = []
    for field, t in texts.items():
        ov = copy_overlap(t, caps)
        if ov >= COPY_THRESHOLD:
            hits.append(Hit("legal", "copy_guard", "hard", f"{ov:.0%} of its 5-word phrases copy another account's caption (own content only, R6)",
                            field, (t or "")[:40]))
    return hits


# ------------------------------------------------------------------ analysis
def analyze(cfg, conn, include_demo: bool = False) -> dict:
    """One brain call (task 'accounts', no tools, schema-validated, counted in the daily cap)."""
    from . import brain

    inp = brain_input(conn, cfg, include_demo)
    if not inp["posts"] and not inp["own"]["posts"]:
        raise AccountError("nothing to analyse yet: attach accounts and import some posts first")
    known = {a["account"] for a in inp["accounts"]} | {"OWN"}
    caps = stored_captions(conn)

    def validate(out: dict) -> list[str]:
        errs = []
        for a in out["accounts"]:
            if a["account"] not in known:
                errs.append(f"unknown account {a['account']} (use the ACC_ ids given)")
        for i, idea in enumerate(out["ideas"], 1):
            if copy_overlap(idea["idea"], caps) >= COPY_THRESHOLD:
                errs.append(f"idea {i} copies a caption; write an original angle")
            if _MENTION.search(idea["idea"]) or _URL.search(idea["idea"]):
                errs.append(f"idea {i}: no @handles or links")
        return errs

    ctx = {"input_json": json.dumps(inp, ensure_ascii=False, indent=1, default=str)}
    out = brain.ask(cfg, conn, "accounts", ctx, model=cfg["brain"]["review_model"], validate=validate)
    conn.execute("INSERT INTO account_analyses(created_at, json, stats_json) VALUES(?,?,?)",
                 (db.now_iso(), db.jdump(out), db.jdump({"stats": inp["stats"], "own": inp["own"]})))
    return out


def latest_analysis(conn) -> dict | None:
    try:
        r = conn.execute("SELECT created_at, json, stats_json FROM account_analyses ORDER BY id DESC LIMIT 1").fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not r:
        return None
    return {"created_at": r["created_at"], **(db.jload(r["json"], {}) or {}), "stats": db.jload(r["stats_json"], {}) or {}}


def brief_text(conn) -> str:
    """Ideas from the latest analysis for the filming brief (pseudonymous, DATA)."""
    a = latest_analysis(conn)
    if not a or not a.get("ideas"):
        return "(no account analysis yet)"
    lines = [f"- {i['idea']} (hook type {i['hook_type']}; why: {i['why']})" for i in a["ideas"]]
    if a.get("summary"):
        lines.insert(0, f"Summary: {a['summary']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ Business Discovery adapter
class DiscoveryUnavailable(AccountError):
    pass


class BusinessDiscoverySource:
    """Official Business Discovery (Instagram API with Facebook Login, graph.facebook.com).

    Unavailable on the engine's Instagram-Login path (verified in Meta's docs 2026-10-14, API_NOTES.md).
    Needs: accounts.discovery = "facebook_login", accounts.discovery_ig_user_id, and a Facebook-Login token
    in keyring (`fb_discovery_token`). Requests only the minimal fields; only Business/Creator accounts
    are returned by Meta. Never runs on a schedule: `reel accounts fetch` only."""

    def __init__(self, cfg, conn, token_getter: Callable[[], str] | None = None, session=None):
        if cfg.get("accounts.discovery", "off") != "facebook_login":
            raise DiscoveryUnavailable("Business Discovery is off: it needs the Instagram API with Facebook Login (see docs/API_NOTES.md). Use manual import.")
        if not cfg.get("accounts.discovery_ig_user_id"):
            raise DiscoveryUnavailable("accounts.discovery_ig_user_id is empty")
        from . import secrets
        from .graph import GraphClient

        self.cfg = cfg
        self.conn = conn
        getter = token_getter or (lambda: secrets.get_secret(DISCOVERY_SECRET))
        self.client = GraphClient(cfg, conn, getter, session=session)

    def fetch(self, username: str, limit: int | None = None) -> dict:
        u = norm_username(username)
        n = int(limit or self.cfg.get("accounts.max_media_per_account", 30))
        host = self.cfg["accounts"]["discovery_host"].rstrip("/")
        url = f"{host}/{self.cfg['instagram']['api_version']}/{self.cfg['accounts']['discovery_ig_user_id']}"
        fields = f"business_discovery.username({u}){{username,followers_count,media_count,media.limit({n}){{{DISCOVERY_MEDIA_FIELDS}}}}}"
        payload = self.client.request("GET", url, params={"fields": fields})
        bd = payload.get("business_discovery") or {}
        return {"username": u, "followers": bd.get("followers_count"), "media_count": bd.get("media_count"),
                "media": ((bd.get("media") or {}).get("data")) or []}

    def sync(self, username: str) -> dict:
        from .graph import GraphError

        acc = get(self.conn, username)
        if acc is None:
            raise AccountError(f"@{norm_username(username)} is not attached")
        try:
            d = self.fetch(username)
        except GraphError as exc:
            self.conn.execute("UPDATE accounts SET last_error=? WHERE id=?", (redact(str(exc))[:300], acc["id"]))
            raise
        rows = []
        for m in d["media"]:
            rows.append({"permalink": m.get("permalink") or "", "posted_at": m.get("timestamp") or "",
                         "media_type": "REELS" if m.get("media_product_type") == "REELS" else (m.get("media_type") or ""),
                         "caption": m.get("caption"), "likes": m.get("like_count"), "comments": m.get("comments_count"),
                         "views": m.get("view_count"), "_id": m.get("id")})
        stored = 0
        tz = self.cfg.tz
        for r in rows:
            try:
                rec = _record({k: v for k, v in r.items() if k != "_id" and (k != "permalink" or v)}, tz)
            except AccountError:
                continue
            if r.get("_id"):
                rec["media_key"] = f"id:{r['_id']}"
            with db.tx(self.conn):
                stored += int(_store(self.conn, acc["id"], rec, "api"))
        _trim(self.conn, acc["id"], int(self.cfg.get("accounts.max_media_per_account", 30)))
        self.conn.execute("UPDATE accounts SET last_fetched_at=?, last_error=NULL, followers=?, media_count=?, source='api' WHERE id=?",
                          (db.now_iso(), d["followers"], d["media_count"], acc["id"]))
        return {"username": d["username"], "stored": stored}
