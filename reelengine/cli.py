"""`reel` command line (docs/RUNBOOK.md documents every command).

Agents call these; `--json` gives machine-readable output wherever useful.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import io
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import db, secrets
from .config import ConfigError, live_ok_recorded, load_config
from .log import redact

LIVE_PHRASE = "GO LIVE"


def _out(obj: Any, as_json: bool, text: str | None = None) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))
    else:
        print(text if text is not None else json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def _conn(cfg):
    from .jobs import open_db

    return open_db(cfg)


def _local(cfg, iso: str | None) -> str:
    t = db.parse_ts(iso)
    return t.astimezone(cfg.tz).strftime("%Y-%m-%d %H:%M") if t else "—"


# ------------------------------------------------------------------ commands
def cmd_status(cfg, a) -> int:
    from . import tokens
    from .jobs import is_live, paused_reason

    conn = _conn(cfg)
    q = db.queue_counts(conn)
    nxt = conn.execute("SELECT id, scheduled_at FROM reels WHERE state='scheduled' AND is_demo=0 ORDER BY scheduled_at LIMIT 1").fetchone()
    last = conn.execute("SELECT kind, started_at, finished_at, status FROM ticks WHERE kind='tick' ORDER BY id DESC LIMIT 1").fetchone()
    st = {
        "mode": "live" if is_live(cfg) else "dry-run",
        "config_mode": cfg.mode,
        "live_ok_recorded": bool(live_ok_recorded(cfg)),
        "paused": paused_reason(cfg, conn),
        "queue": {k: v for k, v in q.items() if v},
        "next_post": {"reel": nxt["id"], "at": _local(cfg, nxt["scheduled_at"])} if nxt else None,
        "token": {"status": tokens.state(conn).get("status"), "days_left": tokens.days_left(conn)},
        "last_tick": dict(last) if last else None,
    }
    txt = (f"Mode: {st['mode'].upper()}{' (PAUSED: ' + st['paused'] + ')' if st['paused'] else ''}\n"
           f"Queue: {', '.join(f'{k} {v}' for k, v in st['queue'].items()) or 'empty'}\n"
           f"Next post: {('#' + str(nxt['id']) + ' at ' + st['next_post']['at']) if nxt else 'none scheduled'}\n"
           f"Token: {st['token']['status']} ({st['token']['days_left']} days left)\n"
           f"Last tick: {(last['status'] + ' at ' + _local(cfg, last['started_at'])) if last else 'never'}")
    _out(st, a.json, txt)
    return 0


def cmd_tick(cfg, a) -> int:
    from .jobs import Locked, tick

    try:
        s = tick(cfg, force_dry=a.dry_run, toast=None if not a.no_toast else False)
    except Locked as exc:
        _out({"status": "locked", "detail": str(exc)}, a.json, f"skipped: {exc}")
        return 0
    _out(s, a.json)
    return 0


def cmd_daily(cfg, a) -> int:
    from .jobs import Locked, daily

    try:
        _out(daily(cfg), a.json)
    except Locked as exc:
        print(f"skipped: {exc}")
    return 0


def cmd_weekly(cfg, a) -> int:
    from .jobs import Locked, weekly

    try:
        _out(weekly(cfg), a.json)
    except Locked as exc:
        print(f"skipped: {exc}")
    return 0


def cmd_process(cfg, a) -> int:
    from . import intake, pipeline
    from .jobs import engine_lock

    src = Path(a.file)
    if not src.is_file():
        print(f"not a file: {src}", file=sys.stderr)
        return 2
    with engine_lock(cfg):
        conn = _conn(cfg)
        rid = intake.take(cfg, conn, src, move=False)  # a file given by hand is copied, never moved
        if rid is None:
            print("duplicate: this exact file was already processed")
            return 1
        ctx = pipeline.Ctx.create(cfg, conn, toast=False)
        state = pipeline.process_reel(ctx, rid)
        row = db.get_reel(conn, rid)
        res = {"reel": rid, "state": state, "held_reason": row["held_reason"], "scheduled_at": _local(cfg, row["scheduled_at"]), "out": row["out_path"]}
    _out(res, a.json, f"reel #{rid}: {state}" + (f" ({row['held_reason']})" if row["held_reason"] else "") +
         (f", scheduled {res['scheduled_at']}" if row["scheduled_at"] else ""))
    return 0 if state in ("scheduled", "qa_passed") else 1


def _reel_detail(cfg, conn, rid: int) -> dict:
    row = db.get_reel(conn, rid)
    if row is None:
        raise SystemExit(f"no reel {rid}")
    d = {k: row[k] for k in row.keys() if not k.endswith("_json")}
    for k in ("probe_json", "plan_json", "caption_json", "qa_json", "features_json", "style_overrides_json"):
        d[k[:-5]] = db.jload(row[k])
    if row["transcript_path"] and Path(row["transcript_path"]).exists():
        tr = json.loads(Path(row["transcript_path"]).read_text(encoding="utf-8"))
        d["transcript"] = " ".join(w["text"] for w in tr["words"])
    d["metrics"] = [{"checkpoint_h": r["checkpoint_h"], "fetched_at": r["fetched_at"], **(db.jload(r["metrics_json"], {}) or {})}
                    for r in conn.execute("SELECT * FROM insights WHERE reel_id=? ORDER BY checkpoint_h", (rid,))]
    d["events"] = [dict(r) for r in conn.execute("SELECT ts, from_state, to_state, detail FROM events WHERE reel_id=? ORDER BY id", (rid,))]
    return d


def cmd_show(cfg, a) -> int:
    conn = _conn(cfg)
    d = _reel_detail(cfg, conn, a.id)
    if a.json:
        _out(d, True)
        return 0
    plan, cap, qa = d.get("plan") or {}, d.get("caption") or {}, d.get("qa") or {}
    lines = [f"Reel #{a.id}: {d['state']}" + (f" (held: {d['held_reason']})" if d.get("held_reason") else ""),
             f"source: {d['source_name']} · {d.get('src_duration_s') or 0:.1f} s → {d.get('duration_s') or 0:.1f} s · out: {d.get('out_path') or '—'}"]
    if plan:
        lines += [f"hook: {plan['hook']['on_screen']} · type {plan['hook_type']} · pillar {plan['pillar']} · {plan.get('cuts')} cuts",
                  f"kept segments {plan['keep_segments']} · risk flags: {plan.get('risk_flags') or 'none'}"]
    if d.get("transcript"):
        lines.append("transcript: " + d["transcript"][:600])
    if cap:
        lines.append("caption:\n" + cap["caption"])
    if qa:
        lines.append(f"QA: {'PASS' if qa['passed'] else 'HELD'} scores {qa.get('scores')}")
        lines += [f"  x {f}" for f in qa.get("hard_fails", [])]
    if d.get("scheduled_at"):
        lines.append(f"scheduled: {_local(cfg, d['scheduled_at'])} ({d.get('slot_arm')})")
    if d.get("media_id"):
        lines.append(f"media: {d['media_id']} {d.get('permalink') or ''} {'(dry-run)' if d['dry_run'] else ''}")
    if d["metrics"]:
        lines.append("metrics: " + "; ".join(f"{m['checkpoint_h']:g}h views {m.get('views')} reach {m.get('reach')}" for m in d["metrics"] if m.get("fetched_at")))
    print("\n".join(lines))
    return 0


def _parse_style(pairs: list[str]) -> dict:
    out: dict[str, Any] = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--style expects key=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k.strip()] = int(v) if re.fullmatch(r"-?\d+", v) else float(v) if re.fullmatch(r"-?\d+\.\d+", v) else v
    return out


def cmd_rerender(cfg, a) -> int:
    from . import pipeline
    from .jobs import engine_lock

    with engine_lock(cfg):
        conn = _conn(cfg)
        state = pipeline.rerender(pipeline.Ctx.create(cfg, conn, toast=False), a.id, _parse_style(a.style))
        row = db.get_reel(conn, a.id)
    _out({"reel": a.id, "state": state, "held_reason": row["held_reason"]}, a.json, f"reel #{a.id}: {state}" + (f" ({row['held_reason']})" if row["held_reason"] else ""))
    return 0


def cmd_held(cfg, a) -> int:
    conn = _conn(cfg)
    rows = [{"id": r["id"], "reason": r["held_reason"], "since": _local(cfg, r["updated_at"]), "source": r["source_name"]} for r in db.reels_in(conn, "held")]
    _out(rows, a.json, "\n".join(f"#{r['id']} ({r['since']}, {r['source']}): {r['reason']}" for r in rows) or "no held reels")
    return 0


def cmd_approve(cfg, a) -> int:
    from . import pipeline
    from .jobs import engine_lock

    with engine_lock(cfg):
        conn = _conn(cfg)
        state = pipeline.approve(pipeline.Ctx.create(cfg, conn, toast=False), a.id)
        row = db.get_reel(conn, a.id)
    _out({"reel": a.id, "state": state, "held_reason": row["held_reason"]}, a.json,
         f"reel #{a.id}: {state}" + (f" (still held: {row['held_reason']})" if state == "held" else ""))
    return 0 if state != "held" else 1


def cmd_pause(cfg, a) -> int:
    cfg.pause_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.pause_file.write_text(f"paused by {getpass.getuser()} via `reel pause` at {db.now_iso()}\n", encoding="utf-8")
    print("PAUSED: no publishing from the next tick on (processing continues). Undo with: reel resume")
    return 0


def cmd_resume(cfg, a) -> int:
    from . import tokens

    conn = _conn(cfg)
    if cfg.pause_file.exists():
        cfg.pause_file.unlink()
    if tokens.state(conn).get("status") == "auth_needed":
        print("Resumed, but Instagram still needs reconnecting: run  reel connect")
        return 1
    print("RESUMED: publishing continues at the next scheduled slot.")
    return 0


def _set_mode_in_config(cfg, mode: str) -> None:
    p = cfg.source
    text = p.read_text(encoding="utf-8")
    new, n = re.subn(r'(?m)^mode\s*=\s*"(dry-run|live)"', f'mode = "{mode}"', text, count=1)
    if n != 1:
        raise SystemExit("could not find `mode = ...` in config.toml")
    p.write_text(new, encoding="utf-8")


def cmd_mode(cfg, a) -> int:
    from . import doctor

    if a.mode == "dry-run":
        _set_mode_in_config(cfg, "dry-run")
        print("Mode: DRY-RUN (nothing will be posted).")
        return 0
    problems = []
    if not live_ok_recorded(cfg):
        problems.append("no LIVE-OK line in docs/DECISIONS.md (Yazeed's recorded go-live OK, e.g. `- 2026-10-05 LIVE-OK: Yazeed approved going live`)")
    if not secrets.has_secret(secrets.ACCESS_TOKEN):
        problems.append("no Instagram token in keyring (run `reel connect`)")
    conn = _conn(cfg)
    rep = doctor.run(cfg, conn, live=secrets.has_secret(secrets.ACCESS_TOKEN))
    fails = [c for c in rep["checks"] if c["status"] == "fail"]
    if fails:
        problems.append("reel doctor --live has failures: " + "; ".join(f"{c['check']}: {c['detail'][:80]}" for c in fails))
    if problems:
        print("Refusing to go live:\n  - " + "\n  - ".join(problems))
        return 2
    if not sys.stdin.isatty():
        print("Refusing: `reel mode live` must be confirmed interactively.")
        return 2
    typed = input(f"Type {LIVE_PHRASE} to let the engine post to Instagram: ").strip()
    if typed != LIVE_PHRASE:
        print("Not confirmed; mode unchanged.")
        return 1
    _set_mode_in_config(cfg, "live")
    n = conn.execute("SELECT COUNT(*) FROM reels WHERE state='published' AND dry_run=1 AND is_demo=0").fetchone()[0]
    print("Mode: LIVE. The next due reel will be posted." + (f"\n{n} reels were only rehearsed in dry-run; to post one for real: reel requeue <id>" if n else ""))
    return 0


def cmd_requeue(cfg, a) -> int:
    from . import scheduler

    conn = _conn(cfg)
    row = db.get_reel(conn, a.id)
    if row is None or row["state"] != "published" or row["dry_run"] != 1:
        state = row["state"] if row else "missing"
        print(f"Refusing: reel #{a.id} is {state}. Only a reel rehearsed in dry-run (published, dry_run=1) can be re-queued; "
              "a held reel goes through `reel approve` so QA runs again.")
        return 2
    try:
        db.transition(conn, a.id, "qa_passed", "re-queued after a dry-run rehearsal", expect_from="published",
                      media_id=None, permalink=None, published_at=None, dry_run=0,
                      container_id=None, container_created_at=None, upload_done_at=None, publish_requested_at=None)
    except db.TransitionError as exc:
        print(f"Refusing: {exc}")
        return 2
    res = scheduler.schedule_reel(conn, cfg, a.id)
    print(f"reel #{a.id} scheduled for {res.get('when_local')}")
    return 0


def _stats(cfg, conn, since: str | None) -> dict:
    q = "SELECT * FROM reels WHERE is_demo=0 AND state='published'" + (" AND published_at >= ?" if since else "")  # nosec B608 - constant SQL, value bound as a parameter
    reels = conn.execute(q + " ORDER BY published_at", (since,) if since else ()).fetchall()
    out = []
    for r in reels:
        m = conn.execute("SELECT metrics_json, checkpoint_h FROM insights WHERE reel_id=? AND fetched_at IS NOT NULL ORDER BY checkpoint_h DESC LIMIT 1", (r["id"],)).fetchone()
        met = db.jload(m["metrics_json"], {}) if m else {}
        f = db.jload(r["features_json"], {}) or {}
        out.append({"id": r["id"], "published_at": r["published_at"], "dry_run": bool(r["dry_run"]), "permalink": r["permalink"],
                    "duration_s": r["duration_s"], "hook_type": f.get("hook_type"), "pillar": f.get("pillar"), "slot": f.get("slot") or r["slot_arm"],
                    "duration_band": f.get("duration_band"), "cta_type": f.get("cta_type"), "hashtag_count": f.get("hashtag_count"),
                    "caption_style": f.get("caption_style"), "checkpoint_h": m["checkpoint_h"] if m else None,
                    "composite": r["composite_score"], **{k: met.get(k) for k in ("views", "reach", "likes", "comments", "shares", "saved", "ig_reels_avg_watch_time", "reels_skip_rate")}})
    acct = conn.execute("SELECT day, follower_count FROM account_insights ORDER BY day DESC LIMIT 1").fetchone()
    return {"since": since, "published": len(out), "live": sum(1 for x in out if not x["dry_run"]), "followers": acct["follower_count"] if acct else None, "reels": out}


def cmd_stats(cfg, a) -> int:
    conn = _conn(cfg)
    s = _stats(cfg, conn, a.since)
    txt = f"{s['published']} published ({s['live']} live) since {a.since or 'the start'}; followers {s['followers']}\n" + "\n".join(
        f"#{r['id']} {_local(cfg, r['published_at'])} {'DRY' if r['dry_run'] else 'LIVE'} {r['hook_type']} {r['duration_band']} views {r['views']} reach {r['reach']} composite {r['composite']}"
        for r in s["reels"])
    _out(s, a.json, txt)
    return 0


def cmd_export(cfg, a) -> int:
    conn = _conn(cfg)
    rows = _stats(cfg, conn, a.since)["reels"]
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    if a.out:
        Path(a.out).write_text(buf.getvalue(), encoding="utf-8-sig")
        print(f"wrote {len(rows)} rows to {a.out}")
    else:
        sys.stdout.write(buf.getvalue())
    return 0


def cmd_brief(cfg, a) -> int:
    from . import brief as bm
    from . import pipeline
    from .jobs import engine_lock

    conn = _conn(cfg)
    if a.now:
        with engine_lock(cfg):
            b = bm.make_brief(pipeline.Ctx.create(cfg, conn, toast=False))
    else:
        r = conn.execute("SELECT json FROM briefs ORDER BY id DESC LIMIT 1").fetchone()
        if not r:
            print("No brief yet. It is written every Saturday 20:00, or now with: reel brief --now")
            return 1
        b = db.jload(r["json"])
    _out(b, a.json, bm.brief_markdown(b))
    return 0


def cmd_review(cfg, a) -> int:
    from . import learner, pipeline
    from .jobs import engine_lock, review

    conn = _conn(cfg)
    if a.now:
        with engine_lock(cfg):
            _out(review(pipeline.Ctx.create(cfg, conn, toast=False)), a.json)
        return 0
    pb = learner.load_playbook(conn, cfg)
    r = conn.execute("SELECT * FROM playbooks ORDER BY version DESC LIMIT 1").fetchone()
    res = {"version": pb["version"], "summary": r["summary"] if r else None, "changes": db.jload(r["changes_json"], []) if r else [],
           "arm_stats": learner.arm_stats(conn, cfg, pb)}
    _out(res, a.json, learner.playbook_markdown(pb))
    return 0


def cmd_doctor(cfg, a) -> int:
    from . import doctor

    conn = _conn(cfg)
    rep = doctor.run(cfg, conn, live=a.live, probe_container=a.probe_container)
    if a.json:
        _out(rep, True)
    else:
        sym = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
        print("\n".join(f"{sym[c['status']]} {c['check']}: {c['detail']}" for c in rep["checks"]))
        print(f"\nOverall: {rep['status'].upper()}")
    return 1 if rep["status"] == "fail" else 0


def cmd_dashboard(cfg, a) -> int:
    from .dashboard import build as dbuild
    from .dashboard import server

    if a.demo:
        from .dashboard import demo

        ddir = cfg.dir("demo")
        dpath = ddir / "engine.sqlite"
        if dpath.exists():
            dpath.unlink()
        demo.seed_demo(dpath, cfg, ddir / "thumbs")
        dconn = db.connect(dpath)
        site = dbuild.build_dashboard(cfg, dconn, cfg.data_dir / "dashboard-demo", demo=True)
        if a.build_only:
            print(site)
            return 0
        api = None if a.read_only else server.make_api(cfg, dpath, site, demo=True)
        server.serve(cfg, site, port=a.port, open_browser=a.open, media_root=ddir, api=api)
        return 0
    conn = _conn(cfg)
    site = dbuild.build_dashboard(cfg, conn)
    if a.build_only:
        print(site)
        return 0
    api = None if a.read_only else server.make_api(cfg, cfg.db_path, site)
    server.serve(cfg, site, port=a.port, open_browser=a.open, api=api)
    return 0


def _read_token(stdin: bool) -> str:
    tok = sys.stdin.readline().strip() if stdin else getpass.getpass("Paste the Instagram long-lived token (hidden): ").strip()
    if not tok:
        raise SystemExit("empty token")
    return tok


def cmd_connect(cfg, a) -> int:
    from . import tokens
    from .graph import GraphError
    from .notify import notifier

    conn = _conn(cfg)
    token = _read_token(a.stdin)
    try:
        info = tokens.connect(cfg, conn, token, notifier(cfg, conn, toast=False))
    except GraphError as exc:
        print(f"Token rejected by Instagram: {redact(str(exc))[:200]}")
        return 2
    finally:
        token = ""  # noqa: F841  # nosec B105 - drops the reference, not a password
    print(f"Connected @{info['username']} ({info['account_type']}), user id {info['ig_user_id']}. Token stored in Windows Credential Manager.")
    print("Next: reel doctor --live")
    return 0


def cmd_refresh_token(cfg, a) -> int:
    from . import tokens
    from .notify import notifier

    conn = _conn(cfg)
    res = tokens.refresh_if_due(cfg, conn, notifier(cfg, conn, toast=False), force=True)
    _out(res, a.json)
    return 0 if res.get("refreshed") else 1


def cmd_secret(cfg, a) -> int:
    if a.name == secrets.ACCESS_TOKEN:
        print("Refusing: the Instagram token must be stored with `reel connect` (or scripts\\connect-instagram.ps1), "
              "which verifies it with Instagram and records its expiry.")
        return 2
    value = sys.stdin.readline() if a.stdin else getpass.getpass(f"Value for {a.name} (hidden): ")
    secrets.set_secret(a.name, value)
    print("stored")
    return 0


def cmd_order(cfg, a) -> int:
    conn = _conn(cfg)
    day = a.date or date.today().isoformat()
    datetime.strptime(day, "%Y-%m-%d")
    conn.execute("INSERT INTO orders(day, amount_sar, item, note, created_at) VALUES(?,?,?,?,?)", (day, a.amount, a.item, a.note, db.now_iso()))
    print(f"order recorded: {a.amount} SAR on {day}")
    return 0


# ------------------------------------------------------------------ calendar
def _occ_fields(a) -> dict:
    d = {}
    for k in ("name_ar", "name_en", "rule", "month", "day", "year", "duration_days", "category", "angle", "notes", "source_url", "checked_on", "keywords"):
        v = getattr(a, k, None)
        if v is not None:
            d[k] = v
    if getattr(a, "official", None) is not None:
        d["official"] = a.official == "yes"
    return d


def cmd_calendar(cfg, a) -> int:
    from . import occasions as oc

    # `seed` reports what IT added/refreshed, so skip the automatic seed that _conn() (open_db) runs first
    conn = db.connect(cfg.db_path) if a.action == "seed" else _conn(cfg)
    try:
        if a.action == "remind":
            return _calendar_remind(cfg, conn, a)
        if a.action == "seed":
            r = oc.seed(conn, cfg)
            print(f"added {r['added']} and refreshed {r['refreshed']} seed occasions (your edits and deletions are kept)")
            return 0
        if a.action == "list":
            rows = oc.all_occasions(conn, include_demo=False)
            _out(rows, a.json, "\n".join(
                f"{o['slug']:<26} {o['rule']:<9} {(str(o['year']) + '-') if o['year'] else ''}{o['month']:02d}-{o['day']:02d}"
                f"{' x' + str(o['duration_days']) + 'd' if o['duration_days'] > 1 else '':<6} {o['category']:<13} "
                f"{'official' if o['official'] else 'unofficial':<10} {o['name_en']} / {o['name_ar']}" for o in rows) or "no occasions (reel calendar seed)")
            return 0
        if a.action == "upcoming":
            items = oc.upcoming(conn, cfg, min_days=0, max_days=a.days, include_demo=False)
            _out([x.as_dict() for x in items], a.json, "\n".join(
                f"{x.start.isoformat()} (in {x.days_until} d){' ~' if x.approximate else ''}  {x.occasion['name_en']} / {x.occasion['name_ar']}"
                f"{'  [' + x.hijri + ']' if x.hijri else ''}  film by {x.film_by.isoformat() if x.film_by else '-'}" for x in items) or f"nothing in the next {a.days} days")
            return 0
        if a.action == "add":
            o = oc.add(conn, _occ_fields(a) | ({"slug": a.slug} if a.slug else {}))
            print(f"added {o['slug']}")
            return 0
        if a.action == "edit":
            if not a.slug:
                raise oc.OccasionError("give the id: reel calendar edit --slug <id> ...")
            o = oc.edit(conn, a.slug, _occ_fields(a))
            print(f"updated {o['slug']}")
            return 0
        if a.action == "delete":
            if not a.slug:
                raise oc.OccasionError("give the id: reel calendar delete --slug <id>")
            ok = oc.delete(conn, a.slug)
            print(f"deleted {a.slug}" if ok else f"no occasion {a.slug}")
            return 0 if ok else 1
    except oc.OccasionError as exc:
        print(f"calendar: {exc}", file=sys.stderr)
        return 2
    return 2


def _calendar_remind(cfg, conn, a) -> int:
    """`reel calendar remind [--dry-run]`: the daily occasion reminders now (dry-run: show, send/mark nothing)."""
    from . import occasions as oc
    from .pipeline import Ctx

    report: list = []
    if a.dry_run:
        oc.remind(conn, cfg, lambda *x, **k: None, dry_run=True, report=report)
    else:
        from .jobs import engine_lock

        with engine_lock(cfg):
            oc.remind(conn, cfg, Ctx.create(cfg, conn).notify, report=report)
    head = "would send" if a.dry_run else "sent"
    _out(report, a.json, "\n\n".join(f"[{head}] {r['title']}  (lead {r['lead']} d, ideas: {r['ideas_source']})\n{r['body']}" for r in report)
         or "no occasion reminder due today")
    return 0


# ------------------------------------------------------------------ accounts
def cmd_accounts(cfg, a) -> int:
    from . import accounts as ac

    conn = _conn(cfg)
    try:
        if a.action == "list":
            rows = ac.list_accounts(conn, include_demo=False)
            _out(rows, a.json, "\n".join(f"@{r['username']:<24} {r['role']:<10} {r['pseudonym']:<7} {r['posts']:>3} posts  source {r['source']}"
                                          for r in rows) or "no accounts attached (reel accounts attach <username> --role reference)")
            return 0
        if a.action == "attach":
            r = ac.attach(conn, a.username or "", a.role, notes=a.notes or "", niche=a.niche)
            print(f"attached @{r['username']} as {r['role']} ({r['pseudonym']})")
            return 0
        if a.action == "forget":
            ok = ac.forget(conn, a.username or "")
            if ok:
                conn.execute("VACUUM")
            print("forgotten: the account and all its stored posts were deleted" if ok else "not attached")
            return 0 if ok else 1
        if a.action == "import":
            if not a.file:
                raise ac.AccountError("give --file <csv or txt>")
            text = Path(a.file).read_text(encoding="utf-8-sig")
            rows = ac.parse_csv(text) if a.file.lower().endswith(".csv") else ac.parse_paste(text)
            res = ac.import_rows(conn, cfg, rows, default_username=a.username)
            _out(res, a.json, f"stored {res['stored']} posts for {', '.join('@' + u for u in res['accounts']) or 'nobody'}"
                 + ("".join(f"\n  ! {e}" for e in res["errors"])))
            return 0 if not res["errors"] else 1
        if a.action == "analyze":
            from .jobs import engine_lock

            with engine_lock(cfg):
                out = ac.analyze(cfg, conn)
            _out(out, a.json, out["summary"] + "\n" + "\n".join(f"- {i['idea']} ({i['hook_type']})" for i in out["ideas"]))
            return 0
        if a.action == "recommend":
            if not a.username:
                raise ac.AccountError("give the username: reel accounts recommend <username> [--niche \"...\"]")
            r = ac.recommend(cfg, conn, a.username, a.niche, use_brain=not a.no_brain)
            _out(r, a.json, (f"{r['note']}\n" if r["note"] else "") + f"{r['summary']} (source: {r['source']}, basis: {r['basis']})\n"
                 + "\n".join(f"- {i['topic']}: {i['hook_line']} [{i['format']}, {i['length']}, post {i['best_time']}]" for i in r["ideas"])
                 + "".join(f"\n* {o['start']} {o['name_en']} (in {o['days_until']} d): {o['angle']}" for o in r["occasions"]))
            return 0
        if a.action == "prune":
            _out(ac.prune(conn, cfg), a.json)
            return 0
        if a.action == "fetch":
            src = ac.BusinessDiscoverySource(cfg, conn)
            targets = [a.username] if a.username else [r["username"] for r in ac.list_accounts(conn, include_demo=False) if r["role"] != "own"]
            for u in targets:
                print(src.sync(u))
            return 0
    except ac.AccountError as exc:
        print(f"accounts: {exc}", file=sys.stderr)
        return 2
    return 2


# ------------------------------------------------------------------ schedule (macOS launchd / Windows tasks)
INSTALL_PHRASE = "INSTALL"
UNINSTALL_PHRASE = "UNINSTALL"


def _confirm(phrase: str) -> bool:
    """A person types the phrase in a terminal. No flag skips it (fact-check 2026-10-15): an agent or a
    script can't create or remove scheduled jobs on its own."""
    if not sys.stdin.isatty():
        print(f"Refusing: no terminal to confirm. Re-run it yourself in a terminal and type {phrase}.")
        return False
    return input(f"Type {phrase} to continue: ").strip() == phrase


def cmd_schedule(cfg, a) -> int:
    from . import macsched

    if a.action == "plist":
        out = Path(a.out) if a.out else cfg.data_dir / "launchd"
        paths = macsched.write_plists(cfg, out)
        print(f"Wrote {len(paths)} launchd plists to {out} (nothing installed):")
        for pth in paths:
            print(f"  {pth}")
        print("\nTo install them (only after Yazeed's OK), either run:\n  reel schedule install --mac\nor by hand:")
        print(f"  mkdir -p {macsched.agents_dir()}")
        for c in macsched.install_commands(paths):
            print("  " + " ".join(f'"{x}"' if " " in x else x for x in c))
        tzw = macsched.tz_warning(cfg)
        if tzw:
            print(f"\nWARNING: {tzw}")
        return 0
    if a.action == "status":
        if sys.platform == "win32" and not a.mac:
            from .winsched import tasks_status

            _out(tasks_status(), a.json)
            return 0
        st = macsched.status()
        tzw = macsched.tz_warning(cfg)
        if a.json:
            _out({"jobs": st, "tz_warning": tzw}, True)
        else:
            for label, s_ in st.items():
                if s_.get("exists"):
                    print(f"{label}: loaded; state={s_.get('state')}; last exit code={s_.get('last_exit_code')}; runs={s_.get('runs')}")
                else:
                    print(f"{label}: not loaded" + ("" if not s_.get("plist_installed") else " (plist present in ~/Library/LaunchAgents)"))
            if tzw:
                print(f"WARNING: {tzw}")
        return 0
    # install / uninstall
    if not a.mac:
        print("Pass --mac (Windows tasks are managed by scripts/register-tasks.ps1 / unregister-tasks.ps1).")
        return 2
    if sys.platform != "darwin":
        print("Refusing: --mac works only on macOS.")
        return 2
    if a.action == "install":
        print("This will:")
        print(f"  1. write 3 plists to {cfg.data_dir / 'launchd'}")
        print(f"  2. copy them to {macsched.agents_dir()}")
        print(f"  3. run `launchctl bootstrap {macsched.domain()} <plist>` for each (booting out an old copy first)")
        print("  Jobs: tick every 15 min (:00/:15/:30/:45), daily 03:30, weekly Saturday 20:00 (Mac's local clock).")
        print(f"  Mode stays {cfg.mode}; publishing still needs every live gate.")
        tzw = macsched.tz_warning(cfg)
        if tzw:
            print(f"WARNING: {tzw}")
        if not _confirm(INSTALL_PHRASE):
            print("Not installed.")
            return 1
        for line in macsched.install(cfg):
            print(line)
        print("Check with: reel schedule status --mac")
        return 0
    print(f"This will boot out the 3 Reel Engine launchd jobs and delete their plists from {macsched.agents_dir()}.")
    if not _confirm(UNINSTALL_PHRASE):
        print("Nothing changed.")
        return 1
    for line in macsched.uninstall():
        print(line)
    return 0


# ------------------------------------------------------------------ notify (desktop / Telegram)
TELEGRAM_TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,50}$")


def cmd_notify(cfg, a) -> int:
    from . import notify as nt

    if a.action == "setup-telegram":
        tok = sys.stdin.readline().strip() if a.stdin else getpass.getpass("Paste the Telegram bot token from @BotFather (hidden): ").strip()
        try:
            if not TELEGRAM_TOKEN_RE.match(tok):
                print("That does not look like a bot token (expected <digits>:<30-50 letters/digits/_/->). Nothing stored.")
                return 2
            secrets.set_secret(secrets.TELEGRAM_TOKEN, tok)
            print("Bot token stored in the OS credential store (Windows Credential Manager / macOS Keychain).")
            if a.verify:
                try:
                    me = nt.telegram_api(cfg, "getMe", token=tok)
                    print(f"getMe ok: bot @{me.get('result', {}).get('username')}")
                except RuntimeError as exc:
                    print(f"getMe failed: {exc}")
                    return 1
        finally:
            tok = ""  # noqa: F841  # nosec B105 - drops the reference, not a password
        print("\nNext:\n  1. In Telegram, open your bot and send it /start.")
        print("  2. Run: reel notify telegram-chat-id   (prints the chat ids that wrote to the bot)")
        print("  3. Add to config.toml:\n       [notify]\n       telegram = true\n       telegram_chat_ids = [\"<your chat id>\"]")
        print("  4. Run: reel notify test --channel telegram")
        return 0
    if a.action == "telegram-chat-id":
        tok = secrets.get_secret(secrets.TELEGRAM_TOKEN, required=False)
        if not tok:
            print("No bot token stored. Run: reel notify setup-telegram")
            return 2
        try:
            data = nt.telegram_api(cfg, "getUpdates", token=tok)
        except RuntimeError as exc:
            print(f"getUpdates failed: {exc}")
            return 1
        finally:
            tok = ""  # noqa: F841  # nosec B105 - drops the reference, not a password
        seen: dict[str, str] = {}
        for u in data.get("result", []):
            chat = (u.get("message") or u.get("my_chat_member") or u.get("channel_post") or {}).get("chat") or {}
            if "id" in chat:
                seen.setdefault(str(chat["id"]), chat.get("first_name") or chat.get("title") or "")
        if not seen:
            print("No chats yet. Send /start to the bot in Telegram, then run this again.")
            return 1
        print("Chats that wrote to the bot (put yours in [notify] telegram_chat_ids):")
        for cid, name in seen.items():
            print(f"  {cid}  {name}")
        return 0
    # test
    ch = a.channel
    desktop = ch in ("desktop", "all") and bool(cfg["notify"]["toast"])
    telegram = ch in ("telegram", "all")
    if telegram:
        state, detail = nt.telegram_state(cfg)
        if state != "ready":
            print(f"telegram: not enabled: {detail}")
            telegram = False
    conn = _conn(cfg)
    res = nt.notify(cfg, conn, "info", "Test notification", "If you can read this, this channel works.",
                    toast=desktop, telegram=telegram, kind="action")
    print("sent via: " + ", ".join(res["channels"]))
    wanted = {"dashboard"} | ({nt.desktop_channel() or "desktop"} if desktop else set()) | ({"telegram"} if telegram else set())
    missed = sorted(wanted - set(res["channels"]))
    if missed:
        print("failed: " + ", ".join(missed) + " (see data/logs/engine.log)")
    return 0 if not missed else 1


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reel", description="Reel Engine: autonomous Instagram Reels pipeline (dry-run by default).")
    p.add_argument("--config", help="path to config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn, help_: str, json_flag: bool = True) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        if json_flag:
            sp.add_argument("--json", action="store_true", help="machine-readable output")
        return sp

    add("status", cmd_status, "mode, pause, queue, next post, token, last tick")
    sp = add("tick", cmd_tick, "run one 15-minute tick now")
    sp.add_argument("--dry-run", action="store_true", help="never publish for real in this tick")
    sp.add_argument("--no-toast", action="store_true")
    add("daily", cmd_daily, "daily job: account insights, token health, backup, disk")
    add("weekly", cmd_weekly, "weekly job: trends, learning review, playbook, brief")
    sp = add("process", cmd_process, "run one video file through the pipeline now (copied, not moved)")
    sp.add_argument("file")
    sp = add("show", cmd_show, "everything about one reel")
    sp.add_argument("id", type=int)
    sp = add("rerender", cmd_rerender, "re-render a reel with caption style overrides, then re-run QA")
    sp.add_argument("id", type=int)
    sp.add_argument("--style", nargs="*", default=[], metavar="KEY=VALUE", help="e.g. font_size=110 margin_bottom=700 preset=big")
    add("held", cmd_held, "list held reels with reasons")
    sp = add("approve", cmd_approve, "re-run QA on a held reel; it is scheduled only if it passes")
    sp.add_argument("id", type=int)
    add("pause", cmd_pause, "kill switch: stop all publishing", json_flag=False)
    add("resume", cmd_resume, "undo pause", json_flag=False)
    sp = add("mode", cmd_mode, "dry-run | live (live needs LIVE-OK in DECISIONS.md, a token, a passing doctor and a typed confirmation)", json_flag=False)
    sp.add_argument("mode", choices=["dry-run", "live"])
    sp = add("requeue", cmd_requeue, "schedule a dry-run-rehearsed reel for a real post", json_flag=False)
    sp.add_argument("id", type=int)
    sp = add("stats", cmd_stats, "published reels and their latest metrics")
    sp.add_argument("--since", help="YYYY-MM-DD")
    sp = add("export", cmd_export, "CSV of published reels + metrics", json_flag=False)
    sp.add_argument("--since", help="YYYY-MM-DD")
    sp.add_argument("--out", help="file path (default: stdout)")
    sp = add("brief", cmd_brief, "show this week's filming brief (--now: write a new one)")
    sp.add_argument("--now", action="store_true")
    sp = add("review", cmd_review, "show the current playbook + learning stats (--now: run the weekly review)")
    sp.add_argument("--now", action="store_true")
    sp = add("doctor", cmd_doctor, "health checks (--live adds read-only Instagram checks)")
    sp.add_argument("--live", action="store_true")
    sp.add_argument("--probe-container", action="store_true", help="with --live: create an unpublished test container per candidate host")
    sp = add("dashboard", cmd_dashboard, "build and serve the dashboard on 127.0.0.1", json_flag=False)
    sp.add_argument("--open", action="store_true")
    sp.add_argument("--port", type=int)
    sp.add_argument("--demo", action="store_true", help="serve the clearly-labelled demo data instead")
    sp.add_argument("--build-only", action="store_true")
    sp.add_argument("--read-only", action="store_true", help="no upload button or edits (no POST routes)")
    sp = add("connect", cmd_connect, "store + verify the Instagram token (hidden input)", json_flag=False)
    sp.add_argument("--stdin", action="store_true")
    add("refresh-token", cmd_refresh_token, "refresh the 60-day token now")
    sp = add("secret", cmd_secret, "store a secret in Windows Credential Manager", json_flag=False)
    sp.add_argument("action", choices=["set"])
    sp.add_argument("name", choices=list(secrets.KNOWN_NAMES))
    sp.add_argument("--stdin", action="store_true")
    sp = add("calendar", cmd_calendar, "Saudi occasions calendar: list | upcoming | add | edit | delete | seed | remind")
    sp.add_argument("action", choices=["list", "upcoming", "add", "edit", "delete", "seed", "remind"])
    sp.add_argument("--dry-run", dest="dry_run", action="store_true", help="remind: show what would be sent; send and mark nothing")
    sp.add_argument("--slug", help="the occasion id (edit/delete; optional for add)")
    sp.add_argument("--name-ar", dest="name_ar")
    sp.add_argument("--name-en", dest="name_en")
    sp.add_argument("--rule", choices=["gregorian", "hijri", "once"])
    sp.add_argument("--month", type=int)
    sp.add_argument("--day", type=int)
    sp.add_argument("--year", type=int, help="only for --rule once")
    sp.add_argument("--duration-days", dest="duration_days", type=int)
    sp.add_argument("--category", choices=["national", "religious", "international", "tech", "career"])
    sp.add_argument("--official", choices=["yes", "no"])
    sp.add_argument("--angle")
    sp.add_argument("--notes")
    sp.add_argument("--source-url", dest="source_url")
    sp.add_argument("--checked-on", dest="checked_on", help="YYYY-MM-DD")
    sp.add_argument("--keywords", help="comma-separated extra words that tag a reel as about this day")
    sp.add_argument("--days", type=int, default=21, help="upcoming: how many days ahead (default 21)")
    sp = add("accounts", cmd_accounts, "attached Instagram accounts: list | attach | forget | import | analyze | prune | fetch | recommend")
    sp.add_argument("action", choices=["list", "attach", "forget", "import", "analyze", "prune", "fetch", "recommend"])
    sp.add_argument("--niche", help="attach/recommend: what the account is about, in your words (max 200 characters)")
    sp.add_argument("--no-brain", dest="no_brain", action="store_true", help="recommend: deterministic template only (no brain call)")
    sp.add_argument("username", nargs="?", help="Instagram username (with or without @)")
    sp.add_argument("--role", choices=["own", "reference", "competitor"], default="reference")
    sp.add_argument("--notes")
    sp.add_argument("--file", help="import: a .csv (header: username,permalink,posted_at,media_type,caption,likes,comments,views,duration_s) or a .txt of pasted lines")
    sp = add("order", cmd_order, "record a manual order (Money tab)", json_flag=False)
    sp.add_argument("amount", type=float)
    sp.add_argument("--item", default="")
    sp.add_argument("--note", default="")
    sp.add_argument("--date")
    sp = add("schedule", cmd_schedule, "launchd jobs on macOS: plist | install --mac | uninstall --mac | status [--mac]")
    sp.add_argument("action", choices=["plist", "install", "uninstall", "status"])
    sp.add_argument("--mac", action="store_true", help="act on macOS launchd (install/uninstall need it)")
    sp.add_argument("--out", help="plist: output folder (default data/launchd)")
    sp = add("notify", cmd_notify, "notifications: setup-telegram | telegram-chat-id | test", json_flag=False)
    sp.add_argument("action", choices=["setup-telegram", "telegram-chat-id", "test"])
    sp.add_argument("--stdin", action="store_true", help="setup-telegram: read the token from one stdin line")
    sp.add_argument("--verify", action="store_true", help="setup-telegram: call getMe once (network)")
    sp.add_argument("--channel", choices=["dashboard", "desktop", "telegram", "all"], default="all", help="test: which channel(s)")
    return p


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass  # nosec B110 - console without reconfigure()
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        return int(args.fn(cfg, args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
