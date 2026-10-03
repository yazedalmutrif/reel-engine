"""Offline learning simulation: does the real learner find the best arms?

Runs the REAL reelengine.learner and reelengine.scheduler code against throw-away in-memory
SQLite databases. A hidden ground truth gives some slot, hook_type and duration_band arms real
positive/negative effects on a latent reel quality; the simulated "world" turns that quality
into Instagram-like metrics (views, reach, likes, shares, saved, avg watch time, skip rate in
percent, follower growth), inserts them as insights rows at the 72 h checkpoint, and the learner
does the rest (composite vs rolling median, Beta bandits, weekly briefs, slot scheduling).

Usage (Windows):
  .venv\\Scripts\\python.exe scripts\\learning_sim.py                      # 200 seeds x 90 posts
  .venv\\Scripts\\python.exe scripts\\learning_sim.py --seeds 20 --no-docs # quick check
Results are reproducible for a given --seed (the master seed) and --seeds count.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
import time as _time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reelengine import db, learner, scheduler  # noqa: E402
from reelengine.config import load_config  # noqa: E402

RIYADH = ZoneInfo("Asia/Riyadh")
START = datetime(2026, 10, 1, 12, 0, tzinfo=RIYADH)   # the sim's day tick runs at local noon
CHECKPOINTS = (30, 60, 90)
BRIEF_SIZE = 7          # scripts per weekly filming brief
BUFFER = 2              # reels waiting in 'scheduled' before a new one is made
DIMS = learner.ARM_DIMENSIONS

# ---------------------------------------------------------------- hidden ground truth
# Effects on the latent reel quality q (reel-to-reel noise has sd 0.45 on the same scale).
MAIN = {
    "slot": {"weekday-13:30": -0.40, "weekday-18:30": 0.20, "weekday-21:30": 0.50, "weekday-22:30": 0.00,
             "weekend-13:30": -0.50, "weekend-18:30": -0.05, "weekend-21:30": 0.00, "weekend-22:30": 0.10},
    "hook_type": {"problem": 0.50, "bold_claim": 0.10, "list_number": 0.20, "story": 0.00,
                  "pov_before_after": -0.40, "question": -0.30},
    "duration_band": {"20-30": -0.20, "31-45": 0.15, "46-60": 0.40, "61-90": -0.45},
}
PRIOR_WRONG = {**MAIN, "duration_band": {"20-30": -0.20, "31-45": 0.40, "46-60": 0.00, "61-90": -0.45}}
NULL = {dim: {arm: 0.0 for arm in arms} for dim, arms in MAIN.items()}
SCENARIOS = {"main": MAIN, "prior_wrong": PRIOR_WRONG, "null": NULL}
REEL_NOISE_SD = 0.45
CTAS = ("send", "save", "none", "follow", "comment")
BANDS = {"20-30": (20, 30), "31-45": (31, 45), "46-60": (46, 60), "61-90": (61, 90)}


def true_best(truth: dict, dim: str) -> str | None:
    effects = truth[dim]
    top = max(effects.values())
    best = [a for a, e in effects.items() if e == top]
    return best[0] if len(best) == 1 else None


def _sig(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def world_outcome(q: float, duration: float, followers: float, rng: np.random.Generator) -> tuple[dict, int]:
    """Instagram-like metrics for a reel of latent quality q (NULL for missing data, like the API)."""
    reach = max(20, int(round(math.exp(math.log(250) + 0.8 * q + 0.5 * math.log1p(followers / 200) + rng.normal(0, 0.35)))))
    views = int(round(reach * math.exp(rng.normal(0.3, 0.1))))
    skip_pct = 100.0 * _sig(_logit(0.55) - 0.6 * q + rng.normal(0, 0.3))
    watch = _sig(_logit(0.40) + 0.6 * q + rng.normal(0, 0.3))

    def count(p: float, slope: float, sd: float) -> int:
        return int(rng.binomial(reach, _sig(_logit(p) + slope * q + rng.normal(0, sd))))

    metrics = {
        "views": views, "reach": reach, "likes": count(0.045, 0.5, 0.2), "comments": count(0.004, 0.3, 0.3),
        "shares": count(0.006, 0.8, 0.3), "saved": count(0.010, 0.6, 0.3),
        "ig_reels_avg_watch_time": None if rng.random() < 0.03 else round(watch * duration * 1000),
        "reels_skip_rate": None if rng.random() < 0.05 else round(skip_pct, 2),
    }
    gained = int(rng.binomial(reach, _sig(_logit(0.006) + 0.5 * q)))
    return metrics, gained


# ---------------------------------------------------------------- one simulated account
def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _snapshot(conn, cfg, pb) -> dict:
    stats = learner.arm_stats(conn, cfg, pb)
    snap = {}
    for dim in DIMS:
        rows = stats[dim]
        top = max(rows, key=lambda r: (r["mean"], -rows.index(r)))
        snap[dim] = {"top": top["arm"], "verdicts": {r["arm"]: r["verdict"] for r in rows},
                     "n": {r["arm"]: r["n"] for r in rows}}
    return snap


def run_one(job: tuple[str, int, int]) -> dict:
    """Simulate one account for `posts` posts with the real learner. Deterministic per seed."""
    scenario, seed, posts = job
    truth = SCENARIOS[scenario]
    with tempfile.TemporaryDirectory(prefix="reelsim-") as tmp:
        cfg = load_config().with_overrides({"paths": {"data_dir": tmp}})
        conn = db.connect(":memory:")
        pb = learner.load_playbook(conn, cfg)
        rng_learner = np.random.default_rng([seed, 1])
        rng_world = np.random.default_rng([seed, 2])
        followers, queue, records, pending = 60.0, [], {}, []
        snapshots: dict[int, dict] = {}
        published = 0
        for day in range(posts * 3):
            now = (START + timedelta(days=day)).astimezone(timezone.utc)
            for row in scheduler.due_reels(conn, now):
                db.transition(conn, row["id"], "publishing", expect_from="scheduled")
                db.transition(conn, row["id"], "published", expect_from="publishing",
                              published_at=row["scheduled_at"], media_id=f"sim{row['id']}")
                rec = records[row["id"]]
                metrics, gained = world_outcome(rec["q"], rec["duration"], followers, rng_world)
                followers += gained
                pending.append((db.parse_ts(row["scheduled_at"]) + timedelta(hours=72), row["id"], metrics))
                published += 1
            for item in [p for p in pending if p[0] <= now]:
                conn.execute("INSERT INTO insights(reel_id, media_id, checkpoint_h, due_at, fetched_at, metrics_json) "
                             "VALUES (?,?,?,?,?,?)", (item[1], f"sim{item[1]}", 72, _utc_iso(item[0]), _utc_iso(now), db.jdump(item[2])))
                pending.remove(item)
            conn.execute("INSERT INTO account_insights(day, fetched_at, follower_count) VALUES (?,?,?)",
                         ((START + timedelta(days=day)).date().isoformat(), _utc_iso(now),
                          int(followers) if followers >= 100 else None))
            learner.compute_rewards(conn, cfg, now=now)
            for cp in CHECKPOINTS:
                if published >= cp and cp not in snapshots and cp <= posts:
                    snapshots[cp] = _snapshot(conn, cfg, pb)
            if published >= posts:
                break
            waiting = conn.execute("SELECT COUNT(*) FROM reels WHERE state='scheduled'").fetchone()[0]
            while waiting < BUFFER and len(records) < posts:
                if not queue:
                    queue = learner.recommend_brief(conn, cfg, pb, BRIEF_SIZE, rng=rng_learner)
                records_item = queue.pop(0)
                rid = _make_reel(conn, records_item, rng_world, len(records))
                out = scheduler.schedule_reel(conn, cfg, rid, now=now, rng=rng_learner)
                arms = {"slot": out["arm"], "hook_type": records_item["hook_type"], "duration_band": records_item["duration_band"]}
                q = sum(truth[d][arms[d]] for d in DIMS) + float(rng_world.normal(0, REEL_NOISE_SD))
                records[rid] = {"arms": arms, "q": q, "duration": db.get_reel(conn, rid)["duration_s"],
                                "explore": {"slot": out["explore"], "hook_type": "hook_type" in records_item["explore_dims"],
                                            "duration_band": "duration_band" in records_item["explore_dims"]}}
                waiting += 1
        conn.close()
    ordered = [records[k] for k in sorted(records)][:posts]
    return {"scenario": scenario, "seed": seed, "picks": [r["arms"] for r in ordered],
            "explore": [r["explore"] for r in ordered], "snapshots": snapshots, "followers": int(followers),
            "published": published}


def _make_reel(conn, item: dict, rng: np.random.Generator, index: int) -> int:
    lo, hi = BANDS[item["duration_band"]]
    duration = round(float(rng.uniform(lo, hi)), 1)
    tags = int(rng.choice([3, 5]))
    plan = {"hook_type": item["hook_type"], "output_duration": duration, "pillar": "ai_tools",
            "topic": f"topic-{index}", "cuts": int(rng.integers(4, 14)), "output_word_count": int(duration * 2.3)}
    caption = {"caption": "نص تجريبي", "hashtags": [f"#tag{i}" for i in range(tags)],
               "cta_type": str(rng.choice(CTAS)), "caption_style": str(rng.choice(["natural_keywords", "short"]))}
    return db.insert_reel(conn, f"sim-{index}", f"sim{index}.mp4", "raw/sim.mp4", state="qa_passed",
                          duration_s=duration, plan_json=db.jdump(plan), caption_json=db.jdump(caption))


# ---------------------------------------------------------------- aggregation
def summarise(results: list[dict], truth: dict, posts: int) -> dict:
    windows = [(i, min(i + 10, posts)) for i in range(0, posts, 10)]
    out: dict = {"runs": len(results), "window_share": {}, "uniform": {}, "explore30": {}, "top_ok": {},
                 "regret": {}, "claims": {}}
    for dim in DIMS:
        arms = list(truth[dim])
        best = true_best(truth, dim)
        out["uniform"][dim] = 1.0 / len(arms)
        if best is not None:
            out["window_share"][dim] = [float(np.mean([sum(p[dim] == best for p in r["picks"][a:b]) / (b - a) for r in results]))
                                        for a, b in windows]
        shares = [sum(e[dim] for e in r["explore"][:30]) / min(30, len(r["explore"])) for r in results]
        out["explore30"][dim] = {"mean": float(np.mean(shares)), "min": float(np.min(shares))}
        out["top_ok"][dim] = {cp: float(np.mean([r["snapshots"][cp][dim]["top"] == best for r in results if cp in r["snapshots"]]))
                              for cp in CHECKPOINTS if cp <= posts} if best else {}
        # claims at the last checkpoint: 'better' verdicts for the true best vs for any other arm
        last = max(cp for cp in CHECKPOINTS if cp <= posts)
        v = [r["snapshots"][last][dim]["verdicts"] for r in results if last in r["snapshots"]]
        out["claims"][dim] = {
            "best_better": float(np.mean([vv.get(best) == "better" for vv in v])) if best else None,
            "false_better": float(np.mean([any(s == "better" for a, s in vv.items() if a != best) for vv in v])),
            "any_better": float(np.mean([any(s == "better" for s in vv.values()) for vv in v])),
            "any_claim": float(np.mean([any(s in ("better", "worse") for s in vv.values()) for vv in v])),
            "top_spread": max(np.bincount([arms.index(r["snapshots"][last][dim]["top"]) for r in results], minlength=len(arms))) / len(results),
        }
    gap = {dim: {a: max(truth[dim].values()) - e for a, e in truth[dim].items()} for dim in DIMS}
    uniform_gap = sum(float(np.mean(list(g.values()))) for g in gap.values())
    curves = np.array([np.cumsum([sum(gap[d][p[d]] for d in DIMS) for p in r["picks"]]) for r in results])
    mean_curve = curves.mean(axis=0)
    out["regret"] = {cp: {"learner": float(mean_curve[cp - 1]), "uniform": uniform_gap * cp} for cp in CHECKPOINTS if cp <= posts}
    out["regret_curve"] = mean_curve.tolist()
    out["uniform_gap"] = uniform_gap
    out["followers"] = float(np.mean([r["followers"] for r in results]))
    return out


def print_summary(name: str, s: dict, posts: int) -> None:
    print(f"\n== scenario {name}: {s['runs']} runs x {posts} posts ==")
    for dim in DIMS:
        ws = s["window_share"].get(dim)
        if ws:
            print(f"  best-arm share per 10 posts [{dim}] (uniform {s['uniform'][dim]:.0%}): " + " ".join(f"{x:.0%}" for x in ws))
        e = s["explore30"][dim]
        print(f"  explore share first 30 [{dim}]: mean {e['mean']:.0%}, min {e['min']:.0%}")
        if s["top_ok"][dim]:
            print(f"  P(top posterior = true best) [{dim}]: " + ", ".join(f"@{k}: {v:.0%}" for k, v in s["top_ok"][dim].items()))
        c = s["claims"][dim]
        best_better = "n/a" if c["best_better"] is None else f"{c['best_better']:.0%}"
        print(f"  claims @{max(s['regret'])} [{dim}]: best 'better' {best_better}, false 'better' {c['false_better']:.0%}, "
              f"any claim {c['any_claim']:.0%}, top-arm modal share {c['top_spread']:.0%}")
    for cp, r in s["regret"].items():
        less = f" ({1 - r['learner'] / r['uniform']:.0%} less)" if r["uniform"] > 0 else ""
        print(f"  cumulative regret @{cp}: learner {r['learner']:.1f} vs uniform {r['uniform']:.1f}{less}")


def run_scenario(name: str, seeds: list[int], posts: int, workers: int) -> tuple[dict, list[dict]]:
    jobs = [(name, s, posts) for s in seeds]
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(run_one, jobs, chunksize=max(1, len(jobs) // (workers * 4))))
    else:
        results = [run_one(j) for j in jobs]
    return summarise(results, SCENARIOS[name], posts), results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=20260929, help="master seed (run i uses seed + i)")
    ap.add_argument("--seeds", type=int, default=200, help="runs per scenario")
    ap.add_argument("--null-seeds", type=int, default=None, help="runs for the null / prior-wrong scenarios (default = --seeds)")
    ap.add_argument("--posts", type=int, default=90)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--no-docs", action="store_true", help="don't write docs/qa/learning-sim.md and the SVG charts")
    args = ap.parse_args(argv)
    if args.posts < 60:
        ap.error("--posts must be >= 60")
    extra = args.null_seeds if args.null_seeds is not None else args.seeds
    print(f"learning_sim: master seed {args.seed}, {args.seeds} runs x {args.posts} posts, {args.workers} workers")
    t0 = _time.perf_counter()
    summaries = {}
    for name, n in (("main", args.seeds), ("prior_wrong", extra), ("null", extra)):
        seeds = [args.seed + i for i in range(n)]
        summaries[name], _ = run_scenario(name, seeds, args.posts, args.workers)
        print_summary(name, summaries[name], args.posts)
    elapsed = _time.perf_counter() - t0
    print(f"\ndone in {elapsed:.0f} s")
    if not args.no_docs:
        write_docs(summaries, args, elapsed)
    return 0


# ---------------------------------------------------------------- charts (inline SVG, light + dark)
# Tokens from the dataviz reference palette: slot-1 blue per mode, neutral chrome, muted gray
# for the random baseline. Dark mode is its own set of steps, switched by prefers-color-scheme.
SVG_CSS = """
text { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.bg { fill: #fcfcfb; stroke: rgba(11,11,11,0.10); }
.t1 { fill: #0b0b0b; } .t2 { fill: #52514e; } .t3 { fill: #898781; }
.grid { stroke: #e1e0d9; stroke-width: 1; } .axis { stroke: #c3c2b7; stroke-width: 1; }
.s1 { stroke: #2a78d6; stroke-width: 2; fill: none; stroke-linejoin: round; stroke-linecap: round; }
.m1 { fill: #2a78d6; stroke: #fcfcfb; stroke-width: 2; }
.base { stroke: #898781; stroke-width: 2; fill: none; stroke-linecap: round; }
@media (prefers-color-scheme: dark) {
  .bg { fill: #1a1a19; stroke: rgba(255,255,255,0.10); }
  .t1 { fill: #ffffff; } .t2 { fill: #c3c2b7; }
  .grid { stroke: #2c2c2a; } .axis { stroke: #383835; }
  .s1 { stroke: #3987e5; } .m1 { fill: #3987e5; stroke: #1a1a19; }
}
"""
DIM_LABEL = {"slot": "Posting slot", "hook_type": "Hook type", "duration_band": "Duration band"}


def _pct1(v: float) -> str:
    """12.5%, 16.7%, 25% (one decimal only when needed)."""
    return f"{v * 100:.1f}".rstrip("0").rstrip(".") + "%"


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _svg(width: int, height: int, body: list[str], title: str, desc: str) -> str:
    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-labelledby="t d">',
        f"<title id=\"t\">{_esc(title)}</title><desc id=\"d\">{_esc(desc)}</desc>",
        f"<style>{SVG_CSS}</style>",
        f'<rect class="bg" x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="10"/>',
        *body, "</svg>", ""])


def _legend(x: float, y: float) -> list[str]:
    return [
        f'<line class="s1" x1="{x}" y1="{y}" x2="{x + 22}" y2="{y}"/><circle class="m1" cx="{x + 11}" cy="{y}" r="4"/>',
        f'<text class="t2" x="{x + 30}" y="{y + 4}" font-size="12">Learner (mean over runs)</text>',
        f'<line class="base" x1="{x + 190}" y1="{y}" x2="{x + 212}" y2="{y}"/>',
        f'<text class="t2" x="{x + 220}" y="{y + 4}" font-size="12">Picking at random</text>',
    ]


def share_chart(s: dict, runs: int, seed: int, posts: int) -> str:
    """Small multiples: best-arm pick share per 10-post window, one panel per dimension."""
    W, H, left, right, top, bottom, gap = 960, 400, 52, 20, 126, 52, 30
    pw = (W - left - right - 2 * gap) / 3
    ph = H - top - bottom
    body = [
        f'<text class="t1" x="{left}" y="34" font-size="18" font-weight="600">The learner moves its picks to the true best arm</text>',
        f'<text class="t2" x="{left}" y="56" font-size="13">Share of picks that went to the true-best arm, per 10 posts. '
        f'Mean of {runs} simulated accounts (master seed {seed}).</text>',
        *_legend(left, 80),
    ]
    for i, dim in enumerate(DIMS):
        x0 = left + i * (pw + gap)
        best = true_best(MAIN, dim)
        ws = s["window_share"][dim]
        uni = s["uniform"][dim]

        def X(post: float) -> float:
            return x0 + post / posts * pw

        def Y(v: float) -> float:
            return top + (1 - v) * ph

        body.append(f'<text class="t1" x="{x0}" y="{top - 14}" font-size="13" font-weight="600">{DIM_LABEL[dim]} '
                    f'<tspan class="t2" font-weight="400">best: {best}, {len(MAIN[dim])} arms</tspan></text>')
        for v in (0, 0.25, 0.5, 0.75, 1.0):
            body.append(f'<line class="grid" x1="{x0}" x2="{x0 + pw}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>')
            if i == 0:
                body.append(f'<text class="t3" x="{x0 - 8}" y="{Y(v) + 4:.1f}" font-size="11" text-anchor="end">{v:.0%}</text>')
        body.append(f'<line class="axis" x1="{x0}" x2="{x0 + pw}" y1="{Y(0):.1f}" y2="{Y(0):.1f}"/>')
        for t in range(0, posts + 1, 30):
            body.append(f'<text class="t3" x="{X(t):.1f}" y="{Y(0) + 18:.1f}" font-size="11" text-anchor="middle">{t}</text>')
        body.append(f'<line class="base" x1="{X(0):.1f}" x2="{X(posts):.1f}" y1="{Y(uni):.1f}" y2="{Y(uni):.1f}">'
                    f'<title>Random pick: {_pct1(uni)}</title></line>')
        body.append(f'<text class="t3" x="{X(posts):.1f}" y="{Y(uni) + 16:.1f}" font-size="11" text-anchor="end">random {_pct1(uni)}</text>')
        pts = [(X(10 * k + 5), Y(v)) for k, v in enumerate(ws)]
        body.append('<polyline class="s1" points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + '"/>')
        for k, ((x, y), v) in enumerate(zip(pts, ws)):
            body.append(f'<circle class="m1" cx="{x:.1f}" cy="{y:.1f}" r="4"><title>Posts {10 * k + 1}-{min(10 * k + 10, posts)}: '
                        f'{v:.0%} of picks on {best} (random {_pct1(uni)})</title></circle>')
        lx, ly = pts[-1]
        body.append(f'<text class="t1" x="{lx:.1f}" y="{ly - 10:.1f}" font-size="12" font-weight="600" text-anchor="end">{ws[-1]:.0%}</text>')
    body.append(f'<text class="t3" x="{W / 2}" y="{H - 14}" font-size="11" text-anchor="middle">Post number (10-post windows)</text>')
    desc = "; ".join(f"{DIM_LABEL[d]}: {s['window_share'][d][0]:.0%} in posts 1-10 to {s['window_share'][d][-1]:.0%} in the last window "
                     f"(random {s['uniform'][d]:.0%})" for d in DIMS)
    return _svg(W, H, body, "Best-arm pick share per 10 posts", desc)


def regret_chart(s: dict, runs: int, posts: int) -> str:
    """Cumulative regret of the learner vs picking at random (latent-quality units)."""
    W, H, left, right, top, bottom = 720, 370, 60, 110, 112, 52
    pw, ph = W - left - right, H - top - bottom
    curve = s["regret_curve"]
    uni = [s["uniform_gap"] * (i + 1) for i in range(len(curve))]
    ymax = max(uni) * 1.05

    def X(post: float) -> float:
        return left + post / posts * pw

    def Y(v: float) -> float:
        return top + (1 - v / ymax) * ph

    body = [
        f'<text class="t1" x="{left}" y="34" font-size="18" font-weight="600">Regret grows slower than random picking</text>',
        f'<text class="t2" x="{left}" y="56" font-size="13">Cumulative quality lost vs always picking the best arms, summed over '
        f'slot + hook + band. Mean of {runs} runs.</text>',
        *_legend(left, 80),
    ]
    step = 50 if ymax > 150 else 25
    for v in range(0, int(ymax) + 1, step):
        body.append(f'<line class="grid" x1="{left}" x2="{left + pw}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>')
        body.append(f'<text class="t3" x="{left - 8}" y="{Y(v) + 4:.1f}" font-size="11" text-anchor="end">{v}</text>')
    body.append(f'<line class="axis" x1="{left}" x2="{left + pw}" y1="{Y(0):.1f}" y2="{Y(0):.1f}"/>')
    for t in range(0, posts + 1, 30):
        body.append(f'<text class="t3" x="{X(t):.1f}" y="{Y(0) + 18:.1f}" font-size="11" text-anchor="middle">{t}</text>')
    body.append(f'<polyline class="base" points="{X(0):.1f},{Y(0):.1f} {X(posts):.1f},{Y(uni[-1]):.1f}"/>')
    body.append('<polyline class="s1" points="' + f"{X(0):.1f},{Y(0):.1f} " +
                " ".join(f"{X(i + 1):.1f},{Y(v):.1f}" for i, v in enumerate(curve)) + '"/>')
    for cp in CHECKPOINTS:
        if cp <= posts:
            v = curve[cp - 1]
            body.append(f'<circle class="m1" cx="{X(cp):.1f}" cy="{Y(v):.1f}" r="4"><title>After {cp} posts: learner {v:.0f}, '
                        f'random {uni[cp - 1]:.0f}</title></circle>')
    body.append(f'<text class="t1" x="{X(posts) + 10:.1f}" y="{Y(curve[-1]) + 4:.1f}" font-size="12" font-weight="600">Learner {curve[-1]:.0f}</text>')
    body.append(f'<text class="t2" x="{X(posts) + 10:.1f}" y="{Y(uni[-1]) + 4:.1f}" font-size="12">Random {uni[-1]:.0f}</text>')
    body.append(f'<text class="t3" x="{left + pw / 2:.1f}" y="{H - 14}" font-size="11" text-anchor="middle">Post number</text>')
    desc = f"After {posts} posts the learner's cumulative regret is {curve[-1]:.0f} vs {uni[-1]:.0f} for random picks."
    return _svg(W, H, body, "Cumulative regret vs random picking", desc)


# ---------------------------------------------------------------- report
def _pct(v: float | None) -> str:
    return "n/a" if v is None else _pct1(v) if v in (0.125, 1 / 6) else f"{v:.0%}"


def _truth_table() -> list[str]:
    lines = ["| Dimension | Arm | Effect on quality (main) | Effect (prior-wrong run) |", "|---|---|---|---|"]
    for dim in DIMS:
        best = true_best(MAIN, dim)
        for arm, eff in sorted(MAIN[dim].items(), key=lambda kv: -kv[1]):
            tag = " **(best)**" if arm == best else " (bad)" if eff <= -0.3 else ""
            alt = PRIOR_WRONG[dim][arm]
            lines.append(f"| {dim} | `{arm}`{tag} | {eff:+.2f} | {alt:+.2f} |")
    return lines


def build_report(summaries: dict, args: argparse.Namespace, elapsed: float) -> str:
    main_s, wrong_s, null_s = summaries["main"], summaries["prior_wrong"], summaries["null"]
    posts, n_main, n_extra = args.posts, main_s["runs"], null_s["runs"]
    cmd = f".venv\\Scripts\\python.exe scripts\\learning_sim.py --seed {args.seed} --seeds {args.seeds} --posts {posts}"
    if args.null_seeds is not None:
        cmd += f" --null-seeds {args.null_seeds}"
    windows = [f"{a + 1}-{min(a + 10, posts)}" for a in range(0, posts, 10)]
    last = max(cp for cp in CHECKPOINTS if cp <= posts)
    L = [
        "# Learning simulation: does the real learner learn?", "",
        f"Generated {datetime.now(RIYADH).date().isoformat()} by `scripts/learning_sim.py` "
        f"(master seed **{args.seed}**, {n_main} runs x {posts} posts for the main scenario, "
        f"{n_extra} runs each for the prior-wrong and no-effect scenarios, {elapsed:.0f} s).", "",
        "Reproduce exactly (Windows, from the project root):", "", "```powershell", cmd, "```", "",
        "## Headline", "",
    ]
    for dim in DIMS:
        ws = main_s["window_share"][dim]
        L.append(f"- **{DIM_LABEL[dim]}**: picks of the true-best arm go from {_pct(ws[0])} (posts 1-10) to "
                 f"{_pct(ws[-1])} (posts {windows[-1]}); random would be {_pct(main_s['uniform'][dim])}. "
                 f"The top posterior arm is the true best in {_pct(main_s['top_ok'][dim][last])} of runs at post {last}.")
    r = main_s["regret"][last]
    null_claims = ", ".join(f"{_pct(null_s['claims'][d]['any_better'])} ({d})" for d in DIMS)
    L += [
        f"- **Regret** after {last} posts: {r['learner']:.0f} vs {r['uniform']:.0f} for random picking "
        f"({1 - r['learner'] / r['uniform']:.0%} less).",
        f"- **Exploration** in the first 30 posts: at least {min(main_s['explore30'][d]['min'] for d in DIMS):.0%} in every run "
        f"and every dimension (rule: >= 25%).",
        f"- **No effects, no winner**: with every arm equal, a 'better' claim appears in "
        f"{null_claims} of runs at post {last}.", "",
        "![Best-arm pick share per 10 posts](learning-sim.svg)", "",
        "## Method", "",
        "- **Real code, fake world.** Each run makes a fresh in-memory SQLite DB, seeds playbook v1 with "
        "`learner.load_playbook`, and then only calls the production functions: `learner.recommend_brief` "
        f"(a {BRIEF_SIZE}-script filming brief whenever the previous one is used up), `scheduler.schedule_reel` "
        "(slot choice, all scheduler constraints), `scheduler.due_reels` + `db.transition` (publishing), and "
        "`learner.compute_rewards` once a day (composite vs rolling median, baseline of 5, Beta updates). "
        "Snapshots use `learner.arm_stats`. Nothing in the learner is re-implemented.",
        f"- **Timeline.** One tick per day at 12:00 Riyadh from {START.date().isoformat()}. A new reel is made when fewer than "
        f"{BUFFER} are waiting in `scheduled`; it follows the next brief item exactly (hook type + a duration drawn "
        "inside the band). Config defaults apply: max 1 post per day, 6 h gap, quiet 00:30-11:00, 20 min lead.",
        f"- **World.** Latent quality q = slot effect + hook effect + band effect + noise (sd {REEL_NOISE_SD}). "
        "From q: reach (log-normal, grows with followers), views, likes / comments / shares / saves (binomial on reach), "
        "average watch time (watch ratio x duration), skip rate in percent (lower when q is high), and follower growth. "
        "3% of reels have no watch time and 5% no skip rate (stored as NULL, like the API); follower_count is NULL "
        "under 100 followers (the account starts at 60).",
        "- **Insights** are written as `insights` rows at the 72 h checkpoint (fetched at the first daily tick "
        "after 72 h), and one `account_insights` row per day.",
        "- **Logged factors** (CTA, hashtag count, caption style) are random and have no effect.",
        "- **Metrics.** (1) share of picks on the true-best arm per 10-post window (mean over runs); "
        "(2) explore share of the first 30 posts; (3) P(top posterior arm = true best) at posts 30/60/90, where "
        "top = highest posterior mean alpha/(alpha+beta); (4) cumulative regret = sum over posts and dimensions of "
        "(best effect - chosen effect), vs the expected regret of uniform random picks; (5) claims: an arm's "
        "`verdict` from `arm_stats` ('better' needs n >= 3 and P(arm beats the pooled rest) >= 1 - 0.05/k).", "",
        "## Ground truth (hidden from the learner)", "", *_truth_table(), "",
        "## Results: main scenario", "",
        "### (1) Share of picks on the true-best arm, per 10 posts", "",
        "| Dimension | random | " + " | ".join(windows) + " |",
        "|---|---|" + "---|" * len(windows),
    ]
    for dim in DIMS:
        L.append(f"| {dim} | {_pct(main_s['uniform'][dim])} | " + " | ".join(_pct(v) for v in main_s["window_share"][dim]) + " |")
    L += ["", "### (2) Exploration in the first 30 posts", "",
          "| Dimension | mean explore share | lowest in any run |", "|---|---|---|"]
    L += [f"| {d} | {_pct(main_s['explore30'][d]['mean'])} | {_pct(main_s['explore30'][d]['min'])} |" for d in DIMS]
    L += ["", "Every arm is tried once first (coverage), then explore = a 25% coin flip plus a catch-up quota that "
          "keeps the running share >= 25%.", "",
          "### (3) P(top posterior arm = true best)", "",
          "| Dimension | " + " | ".join(f"post {cp}" for cp in CHECKPOINTS if cp <= posts) + " | if random |",
          "|---|" + "---|" * (len([c for c in CHECKPOINTS if c <= posts]) + 1)]
    for dim in DIMS:
        L.append(f"| {dim} | " + " | ".join(_pct(main_s["top_ok"][dim][cp]) for cp in CHECKPOINTS if cp <= posts) +
                 f" | {_pct(main_s['uniform'][dim])} |")
    L += ["", "### (4) Cumulative regret (quality units, all three dimensions)", "",
          "| After post | learner | random picks | reduction |", "|---|---|---|---|"]
    for cp, rr in main_s["regret"].items():
        L.append(f"| {cp} | {rr['learner']:.1f} | {rr['uniform']:.1f} | {1 - rr['learner'] / rr['uniform']:.0%} |")
    L += ["", "![Cumulative regret vs random picking](learning-sim-regret.svg)", "",
          f"### Claims at post {last}", "",
          "| Dimension | true best called 'better' | another arm called 'better' | any better/worse claim |", "|---|---|---|---|"]
    L += [f"| {d} | {_pct(main_s['claims'][d]['best_better'])} | {_pct(main_s['claims'][d]['false_better'])} | "
          f"{_pct(main_s['claims'][d]['any_claim'])} |" for d in DIMS]
    L += ["", "The bandit exploits long before it is allowed to *claim* anything: claims need strong evidence, "
          "exploitation doesn't.", "",
          "## Results: prior-wrong scenario", "",
          "The seed playbook gives 46-60 s a mild prior (Beta(2,1)). Here 31-45 s is truly best and 46-60 s is average, "
          "to check that the prior doesn't lock the learner in.", "",
          "| Dimension | " + " | ".join(windows) + f" | top = best @{last} |", "|---|" + "---|" * (len(windows) + 1)]
    for dim in DIMS:
        L.append(f"| {dim} | " + " | ".join(_pct(v) for v in wrong_s["window_share"][dim]) + f" | {_pct(wrong_s['top_ok'][dim][last])} |")
    rw = wrong_s["regret"][last]
    L += ["", f"Regret after {last}: {rw['learner']:.0f} vs {rw['uniform']:.0f} random ({1 - rw['learner'] / rw['uniform']:.0%} less).", "",
          "## (5) Sanity run: no true effects", "",
          f"Every arm has effect 0 ({n_extra} runs). A learner that hallucinates would call winners here.", "",
          "| Dimension | runs with a 'better' claim | any better/worse claim | most common top arm (share of runs) | if uniform |",
          "|---|---|---|---|---|"]
    L += [f"| {d} | {_pct(null_s['claims'][d]['any_better'])} | {_pct(null_s['claims'][d]['any_claim'])} | "
          f"{_pct(null_s['claims'][d]['top_spread'])} | {_pct(null_s['uniform'][d])} |" for d in DIMS]
    L += ["", "The claim rule allows a 5% family-wise false-claim rate per dimension (Bonferroni over its arms); the "
          "measured rate stays near or below that. The top posterior arm is spread across arms (the duration band leans "
          "to 46-60 s because of its prior). With no effects there is no regret to reduce, so that metric is omitted.", "",
          "## Honest limits", "",
          "- **The effects are assumptions.** Arm effects of up to +/-0.5 against reel noise of 0.45 are strong, clean "
          "signals. Real slot or hook effects are probably smaller and noisier; the samples needed grow with "
          "1/effect^2, so half-size effects need roughly 4x the posts for the same certainty.",
          "- **One quality drives every metric.** Here all metrics move together. On the real account they can disagree "
          "(long reels: lower watch ratio, more saves), which the composite weights then arbitrate.",
          "- **Additive, stationary world.** No hook x length interactions, no topic effects, no Instagram algorithm "
          "changes, no seasonality; follower growth is the only drift (and it inflates late wins a little).",
          "- **Perfect compliance.** Every brief item is filmed exactly as recommended, and insights always arrive. "
          "In real life reels are filmed late, skipped, or off-brief, and the 72 h data can lag.",
          "- **Scheduling limits exploitation.** With max 1 post per day, a weekday-only best slot can take at most 5 of "
          "7 days; in the sim the queue simply waits for weekdays, which slows posting.",
          "- **Binary reward.** 'Beat the median' ignores how much a reel won by; big hits and small wins count the same.",
          "- **Claims are conservative on purpose.** At 90 posts the learner already exploits the best arm, but it "
          "formally calls it 'better' in only about a third to a half of runs.",
          "", f"Charts: `docs/qa/learning-sim.svg`, `docs/qa/learning-sim-regret.svg` (light and dark aware).", ""]
    return "\n".join(L)


def write_docs(summaries: dict, args: argparse.Namespace, elapsed: float) -> None:
    out = ROOT / "docs" / "qa"
    out.mkdir(parents=True, exist_ok=True)
    main_s = summaries["main"]
    (out / "learning-sim.svg").write_text(share_chart(main_s, main_s["runs"], args.seed, args.posts), encoding="utf-8")
    (out / "learning-sim-regret.svg").write_text(regret_chart(main_s, main_s["runs"], args.posts), encoding="utf-8")
    (out / "learning-sim.md").write_text(build_report(summaries, args, elapsed), encoding="utf-8")
    print(f"wrote {out / 'learning-sim.md'} and 2 SVG charts")


if __name__ == "__main__":
    raise SystemExit(main())
