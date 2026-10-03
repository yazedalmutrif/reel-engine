"""Scheduled jobs (spec §6): tick (every 15 min), daily (03:30), weekly (Sat 20:00).

Each job holds a single-instance lock (data/engine.lock, an OS file lock released
automatically if the process dies) and records a `ticks` row.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from . import db, insights, intake, learner, pipeline, publisher, scheduler, tokens, vault
from .config import live_ok_recorded
from .graph import GraphError
from .log import get_logger, redact, setup_logging
from .pipeline import Ctx

log = get_logger("jobs")
LATE_GRACE_MIN = 30  # a reel more than this late (PC asleep at the slot) is rescheduled, not posted late


class Locked(Exception):
    pass


@contextmanager
def engine_lock(cfg) -> Iterator[None]:
    path = cfg.data_dir / "engine.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")  # noqa: SIM115
    try:
        if sys.platform == "win32":
            import msvcrt

            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise Locked("another Reel Engine job is running") from None
        else:
            import fcntl

            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Locked("another Reel Engine job is running") from None
        yield
    finally:
        try:
            if sys.platform == "win32":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        f.close()


def open_db(cfg) -> sqlite3.Connection:
    setup_logging(cfg.dir("logs"))
    return db.connect(cfg.db_path)


def _start(conn, kind: str) -> int:
    return int(conn.execute("INSERT INTO ticks(kind, started_at, status) VALUES(?,?,?)", (kind, db.now_iso(), "running")).lastrowid)


def _finish(conn, tid: int, status: str, summary: dict, error: str | None = None) -> None:
    conn.execute("UPDATE ticks SET finished_at=?, status=?, summary_json=?, error=? WHERE id=?",
                 (db.now_iso(), status, db.jdump(summary), redact(error)[:1000] if error else None, tid))


def is_live(cfg) -> bool:
    return cfg.mode == "live" and live_ok_recorded(cfg) is not None


def paused_reason(cfg, conn) -> str | None:
    if cfg.pause_file.exists():
        return (cfg.pause_file.read_text(encoding="utf-8").splitlines() or ["paused"])[0]
    if tokens.state(conn).get("status") == "auth_needed" and is_live(cfg):
        return "auth_needed"
    return None


def build_dashboard(ctx: Ctx) -> str:
    try:
        from .dashboard.build import build_dashboard as _b

        return str(_b(ctx.cfg, ctx.conn))
    except Exception as exc:  # noqa: BLE001 - the dashboard must never break a tick
        log.warning("dashboard build failed: %s", exc)
        return f"failed: {exc}"


# ------------------------------------------------------------------ publish
def _published_today(ctx: Ctx) -> int:
    start = datetime.now(ctx.cfg.tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start.astimezone(db.utcnow().tzinfo).isoformat(timespec="seconds")
    return ctx.conn.execute("SELECT COUNT(*) FROM reels WHERE is_demo=0 AND state='published' AND published_at >= ?",
                            (start_utc,)).fetchone()[0]


def make_publisher(ctx: Ctx, force_dry: bool = False):
    if force_dry or not is_live(ctx.cfg):
        return publisher.DryRunPublisher(ctx.cfg, ctx.conn)
    uid = tokens.ig_user_id(ctx.cfg, ctx.conn)
    if not uid:
        raise GraphError("auth", "no ig_user_id: run `reel connect`")
    return publisher.GraphPublisher(ctx.cfg, ctx.conn, tokens.client(ctx.cfg, ctx.conn), uid)


def publish_due(ctx: Ctx, force_dry: bool = False, now: datetime | None = None, limit: int = 1) -> dict:
    cfg, conn = ctx.cfg, ctx.conn
    reason = paused_reason(cfg, conn)
    if reason:
        return {"skipped": f"paused: {reason}"}
    now = now or db.utcnow()
    due = [r["id"] for r in scheduler.due_reels(conn, now)]
    due += [r["id"] for r in conn.execute(
        "SELECT id FROM reels WHERE state='publishing' AND is_demo=0 AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id",
        (now.isoformat(timespec="seconds"),))]
    due = [rid for rid in dict.fromkeys(due)
           if not (db.get_reel(conn, rid)["next_attempt_at"] and db.parse_ts(db.get_reel(conn, rid)["next_attempt_at"]) > now)]
    out: dict[str, Any] = {"due": len(due), "published": [], "outcomes": {}}
    if not due:
        return out
    try:
        pub = make_publisher(ctx, force_dry)
    except GraphError as exc:
        publisher.handle_publish_error(cfg, conn, due[0], exc, ctx.notify)
        out["error"] = str(exc)
        return out
    for rid in due[:limit]:
        row = db.get_reel(conn, rid)
        if row["state"] == "scheduled":
            slot = db.parse_ts(row["scheduled_at"]) or now
            late_min = (now - slot).total_seconds() / 60
            quiet = scheduler.is_quiet(now.astimezone(cfg.tz).time(), cfg)
            if late_min > LATE_GRACE_MIN or quiet:
                # the PC was asleep/off at the slot: posting late would break quiet hours and
                # teach the slot bandit the wrong time, so pick a fresh slot instead
                why = "quiet hours" if quiet else f"missed its slot by {late_min:.0f} min"
                db.transition(conn, rid, "qa_passed", f"{why}; rescheduling")
                scheduler.schedule_reel(conn, cfg, rid, now=now)
                out["outcomes"][rid] = f"rescheduled ({why})"
                continue
        if row["state"] == "scheduled" and _published_today(ctx) >= cfg["schedule"]["max_posts_per_day"]:
            db.transition(conn, rid, "qa_passed", "daily cap reached at publish time; rescheduling")
            scheduler.schedule_reel(conn, cfg, rid, now=now)
            out["outcomes"][rid] = "rescheduled (daily cap)"
            continue
        if cfg.pause_file.exists():
            out["outcomes"][rid] = "paused"
            break
        try:
            res = pub.publish(rid)
        except (publisher.PublishDeferred, publisher.PublishHeld, GraphError) as exc:
            out["outcomes"][rid] = publisher.handle_publish_error(cfg, conn, rid, exc, ctx.notify)
            continue
        out["published"].append(rid)
        out["outcomes"][rid] = "dry-run" if res.get("dry_run") else "published"
        after_publish(ctx, rid)
    return out


def after_publish(ctx: Ctx, rid: int) -> None:
    row = db.get_reel(ctx.conn, rid)
    cap = (db.jload(row["caption_json"], {}) or {}).get("first_line", "")
    plan = db.jload(row["plan_json"], {}) or {}
    if not row["dry_run"]:
        insights.schedule_checkpoints(ctx.cfg, ctx.conn, rid, row["media_id"], row["published_at"])
    when = db.parse_ts(row["published_at"]).astimezone(ctx.cfg.tz).strftime("%Y-%m-%d %H:%M")
    tag = "DRY-RUN" if row["dry_run"] else "LIVE"
    link = row["permalink"] or "—"
    try:
        vault.append_log_line(ctx.cfg, f"- {when} · {tag} · #{rid} · {plan.get('hook', {}).get('on_screen', '')} · {row['duration_s'] or 0:.0f} s · {plan.get('hook_type', '')} · {link} · {cap[:80]}")
    except Exception as exc:  # noqa: BLE001
        log.warning("vault log failed: %s", exc)
    ctx.notify("info", "Reel published" if not row["dry_run"] else "Reel rehearsed (dry-run)", f"#{rid}: {plan.get('hook', {}).get('on_screen', '')} {link}", rid)


# ------------------------------------------------------------------ jobs
def tick(cfg, force_dry: bool = False, toast: bool | None = None) -> dict:
    with engine_lock(cfg):
        conn = open_db(cfg)
        ctx = Ctx.create(cfg, conn, toast)
        tid = _start(conn, "tick")
        summary: dict[str, Any] = {"mode": "live" if is_live(cfg) else "dry-run"}
        t0 = time.monotonic()
        deadline = t0 + cfg["engine"]["tick_budget_minutes"] * 60
        try:
            summary["intake"] = intake.scan(cfg, conn)
            summary["retried"] = pipeline.retry_failed(ctx)
            processed = {}
            for r in db.reels_in(conn, *pipeline.PRE_PUBLISH):
                if time.monotonic() > deadline:
                    summary["budget"] = "time budget reached; continuing next tick"
                    break
                processed[r["id"]] = pipeline.process_reel(ctx, r["id"], deadline)
            summary["processed"] = processed
            summary["publish"] = publish_due(ctx, force_dry)
            if is_live(cfg) and not paused_reason(cfg, conn):
                try:
                    summary["insights"] = insights.collect_due(cfg, conn, tokens.client(cfg, conn))
                except GraphError as exc:
                    if exc.kind == "auth":
                        tokens.mark_auth_needed(cfg, conn, str(exc), ctx.notify)
                    summary["insights"] = {"error": exc.kind}
            summary["rewards"] = learner.compute_rewards(conn, cfg)
            summary["dashboard"] = build_dashboard(ctx)
            _finish(conn, tid, "ok", summary)
        except BaseException as exc:
            _finish(conn, tid, "error", summary, f"{type(exc).__name__}: {exc}")
            if not isinstance(exc, Exception):
                raise
            ctx.notify("error", "Tick failed", redact(str(exc))[:300], None)
            log.exception("tick failed")
        summary["seconds"] = round(time.monotonic() - t0, 1)
        return summary


def backup_db(cfg, conn) -> dict:
    bdir = cfg.dir("backups")
    stamp = db.utcnow().astimezone(cfg.tz).strftime("%Y%m%d-%H%M")
    dest = bdir / f"engine-{stamp}.sqlite"
    target = sqlite3.connect(str(dest))
    with target:
        conn.backup(target)
    target.close()
    ok = sqlite3.connect(str(dest)).execute("PRAGMA quick_check").fetchone()[0] == "ok"
    copies = sorted(bdir.glob("engine-*.sqlite"))
    for old in copies[: max(0, len(copies) - cfg["backup"]["keep"])]:
        old.unlink(missing_ok=True)
    od = None
    odir = Path(cfg["backup"]["onedrive_copy_dir"])
    try:  # the DB holds no secrets (tokens are in keyring), so a cloud copy is fine
        odir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dest, odir / "engine-latest.sqlite")
        od = str(odir / "engine-latest.sqlite")
    except OSError as exc:
        log.warning("OneDrive backup copy failed: %s", exc)
    conn.execute("INSERT INTO backups(ts, path, size_bytes, onedrive_copy, ok) VALUES(?,?,?,?,?)",
                 (db.now_iso(), str(dest), dest.stat().st_size, od, int(ok)))
    return {"path": str(dest), "ok": ok, "onedrive": od}


def daily(cfg, toast: bool | None = None) -> dict:
    with engine_lock(cfg):
        conn = open_db(cfg)
        ctx = Ctx.create(cfg, conn, toast)
        tid = _start(conn, "daily")
        s: dict[str, Any] = {}
        try:
            if is_live(cfg) and tokens.state(conn).get("status") == "ok":
                try:
                    s["account"] = insights.collect_account(cfg, conn, tokens.client(cfg, conn), tokens.ig_user_id(cfg, conn))
                except GraphError as exc:
                    if exc.kind == "auth":
                        tokens.mark_auth_needed(cfg, conn, str(exc), ctx.notify)
                    s["account"] = {"error": exc.kind}
            s["token"] = tokens.health(cfg, conn, ctx.notify)
            s["backup"] = backup_db(cfg, conn)
            free_gb = shutil.disk_usage(cfg.data_dir).free / 1e9
            s["disk_free_gb"] = round(free_gb, 1)
            if free_gb < cfg["backup"]["min_free_gb"]:
                ctx.notify("warn", "Disk space low", f"{free_gb:.0f} GB free on the engine drive", None)
            s["pruned"] = prune_work(cfg, conn)
            s["dashboard"] = build_dashboard(ctx)
            _finish(conn, tid, "ok", s)
        except Exception as exc:  # noqa: BLE001
            _finish(conn, tid, "error", s, str(exc))
            ctx.notify("error", "Daily job failed", redact(str(exc))[:300], None)
            log.exception("daily failed")
        return s


def prune_work(cfg, conn, keep_days: int = 14) -> int:
    """Delete intermediate work folders of reels published more than keep_days ago
    (the raw clip and the final mp4 are kept)."""
    cutoff = (db.utcnow() - timedelta(days=keep_days)).isoformat(timespec="seconds")
    n = 0
    for r in conn.execute("SELECT id FROM reels WHERE state='published' AND published_at < ?", (cutoff,)):
        d = cfg.data_dir / "work" / f"{r['id']:05d}" / "render"
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


def review(ctx: Ctx) -> dict:
    """Weekly learning review: stats -> brain -> validated playbook vN+1 -> vault note."""
    from . import brain

    cfg, conn = ctx.cfg, ctx.conn
    learner.compute_rewards(conn, cfg)
    pb = learner.load_playbook(conn, cfg)
    inputs = learner.review_inputs(conn, cfg, pb)
    rv = brain.ask(cfg, conn, "learner", {"inputs": inputs, "inputs_json": json.dumps(inputs, ensure_ascii=False, indent=1, default=str)},
                   model=cfg["brain"]["review_model"])
    new = learner.apply_review(conn, cfg, pb, rv)
    conn.execute("INSERT INTO reviews(created_at, from_version, to_version, json) VALUES(?,?,?,?)",
                 (db.now_iso(), pb["version"], new["version"], db.jdump(rv)))
    md = learner.playbook_markdown(new, learner.playbook_diff(pb, new))
    today = db.utcnow().astimezone(cfg.tz).date().isoformat()
    fm = {"type": "playbook", "version": new["version"], "date": today, "tags": ["social-media", "reels", "playbook"], "generated_by": "Reel Engine"}
    vault.write_block(cfg, "Reels Playbook.md", "playbook", md, title="Reels Playbook", frontmatter=fm,
                      intro="The engine's current playbook. Versions and diffs are on the dashboard (Learning tab).")
    vault.write_block(cfg, f"Growth Reviews/{today} Growth Review.md", "review",
                      f"**Summary:** {rv.get('summary', '')}\n\n" + md,
                      title=f"Growth Review {today}", frontmatter={**fm, "type": "growth-review"})
    ctx.notify("info", f"Playbook v{new['version']} written", rv.get("summary", "")[:200], None)
    return {"from": pb["version"], "to": new["version"], "summary": rv.get("summary")}


def weekly(cfg, toast: bool | None = None) -> dict:
    from . import brief as briefmod

    with engine_lock(cfg):
        conn = open_db(cfg)
        ctx = Ctx.create(cfg, conn, toast)
        tid = _start(conn, "weekly")
        s: dict[str, Any] = {}
        steps = [
            ("trends", lambda: {"items": len(briefmod.run_trends(ctx).get("items", []))}),
            ("review", lambda: review(ctx)),
            ("brief", lambda: {"scripts": len(briefmod.make_brief(ctx)["scripts"])}),
            ("token_refresh", lambda: tokens.refresh_if_due(cfg, conn, ctx.notify) if is_live(cfg) else {"skipped": "dry-run"}),
            ("dashboard", lambda: build_dashboard(ctx)),
        ]
        errors = []
        for name, fn in steps:  # one failing step doesn't stop the others
            try:
                s[name] = fn()
            except Exception as exc:  # noqa: BLE001
                log.exception("weekly step %s failed", name)
                s[name] = {"error": redact(str(exc))[:300]}
                errors.append(name)
        _finish(conn, tid, "error" if errors else "ok", s, ", ".join(errors) or None)
        if errors:
            ctx.notify("error", "Weekly job had errors", ", ".join(errors), None)
        return s
