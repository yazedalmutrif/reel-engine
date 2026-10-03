# Brain (Claude CLI) calls made during the build

Ledger kept by the tech lead. Each real-brain run also recorded its calls in the `brain_calls` table of its own (temporary) DB. The latest ones are in `data/engine.sqlite` and in `docs/qa/e2e/real-brain-summary.json`.

| # | When (UTC) | Task | Result |
|---|---|---|---|
| 1–2 | 2026-09-29 | CLI format test (tiny schema) | ok (learned the `structured_output` field); #2 reran it to capture output |
| 3 | 2026-09-29 | edit_plan on the synthetic transcript | ok, 23.6 s. Evidence: `docs/qa/e2e/brain-edit-plan-2026-09-29.json` (kept [1, 3, 6, 7], «سوسيفي» → «سوّ CV», «برفقك» → «بروفايلك») |
| 4–6 | 2026-09-29/30 | e2e real brain, run 1: edit_plan, caption, critique | ok; the reel was **held** by the critique (the "send" CTA was judged bait-like) → prompt fix D-013 |
| 7–9 | 2026-09-29/30 | e2e run 2: 3 calls | ok; **held** (stacked CTAs: spoken «احفظ» + a caption «أرسله») → spoken-CTA detection |
| 10–12 | 2026-09-29/30 | e2e run 3: 3 calls | ok; **held** (mixed masculine/feminine address) → D-014 |
| 13–15 | 2026-09-29/30 | e2e run 4: 3 calls | ok; **passed** QA (hook 7, clarity 8, value 7, accuracy 9) |
| 16 | 2026-09-30 01:33 | weekly: trends (WebSearch/WebFetch) | ok, 275 s, 6 dated items |
| 17–19 | 2026-09-30 | weekly: learner review | the CLI rejected the `$schema` key locally 3 times (no model call); fixed in `brain.py` |
| 20 | 2026-09-30 | weekly: brief | ok, 181 s, 6 scripts (later found to need fixes: fact-check #5, #6, #23) |
| 21 | 2026-09-30 01:41 | `reel review --now` (learner) | ok, 12.7 s, v1 → v2 (no rule changes; n = 0) |
| 22–23 | 2026-09-30 | `reel brief --now` after the fact-check fixes | 1 task, 2 attempts, 260 s: attempt 1 rejected by the new source check (HRDF without a source), attempt 2 passed every gate |

Totals: **23 CLI runs, 20 of which reached the model** (19 tasks; before #22 it was 21 runs / 18 model calls) (#17–19 failed locally before any model call). The e2e run that failed at collection (a syntax error) made no calls. The fake-brain e2e runs make no calls.
