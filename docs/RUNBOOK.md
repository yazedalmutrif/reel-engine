# Runbook

All commands run from the project folder in PowerShell; `reel` means `.venv\Scripts\reel.exe`. Add `--json` for machine-readable output (agents: video-editor, growth-analyst, social-director). Exit codes: 0 ok, 1 something needs attention, 2 refused or error.

## Commands
| Command | What it does |
|---|---|
| `reel status [--json]` | Mode (LIVE / DRY-RUN / PAUSED), queue by state, next post, token days left, last tick |
| `reel tick [--dry-run] [--no-toast] [--json]` | Run one 15-minute tick now: intake → process → publish due → insights → rewards → dashboard. `--dry-run` never posts for real, even in live mode |
| `reel daily` / `reel weekly` | The scheduled jobs, run by hand |
| `reel process <file> [--json]` | Run one video through the pipeline now (the file is **copied**, not moved). Ends at `scheduled` or `held` |
| `reel show <id> [--json]` | State, QA results and scores, transcript, edit plan, caption, schedule, metrics, events |
| `reel rerender <id> [--style key=value ...]` | Re-render with caption overrides (`font_size=110`, `margin_bottom=700`, `max_words=3`, `preset=big` from `brand/styles.yaml`), then re-run QA |
| `reel held [--json]` | Held reels with their reasons |
| `reel approve <id>` | Re-run QA on a held reel (new critique). Scheduled only if it passes |
| `reel pause` / `reel resume` | Kill switch. Publishing stops from the next tick; processing continues |
| `reel mode dry-run` | Back to rehearsal mode (no confirmation needed) |
| `reel mode live` | Refuses unless: a LIVE-OK line is in `docs/DECISIONS.md`, a token is in keyring, and `doctor --live` has no FAIL. Then you type `GO LIVE` |
| `reel requeue <id>` | Schedule a reel that was only rehearsed in dry-run for a real post (refused for reels really on Instagram) |
| `reel stats [--since YYYY-MM-DD] [--json]` | Published reels with their latest metrics, features and composite score |
| `reel export --since YYYY-MM-DD [--out file.csv]` | The same as CSV |
| `reel brief [--now] [--json]` | This week's filming brief (`--now` writes a new one: 1 brain call) |
| `reel review [--now] [--json]` | Current playbook plus bandit stats (`--now` runs the weekly learning review: 1 brain call) |
| `reel doctor [--live] [--probe-container] [--json]` | Health checks. `--live` adds read-only Instagram checks (token, account type, quota, metric support, skip-rate unit); `--probe-container` also settles the container-host VERIFY with an unpublished test container |
| `reel dashboard [--open] [--port N] [--build-only]` | Build and serve the dashboard on 127.0.0.1 (default port 8765) |
| `reel dashboard --demo --open` | The same with clearly labelled demo data (separate DB) |
| `reel connect [--stdin]` | Hidden-input token setup, verified with `/me`, stored in keyring |
| `reel refresh-token [--json]` | Refresh the 60-day token now (the weekly job does this automatically) |
| `reel secret set <name> --stdin` | Store a secret (used by `scripts/set-secret.ps1`) |
| `reel order <amount> [--item ...] [--note ...] [--date ...]` | Record a manual order (Money tab) |

## Reel states
`new → transcribed → planned → rendered → qa_passed → scheduled → publishing → published`, plus `held` (needs a human; the reason is shown) and `failed` (retried with backoff; after 4 failures it becomes held).

## Common situations
| Situation | Do this |
|---|---|
| A reel is held | `reel show <id>`: read `held_reason` and the QA checks. For caption/layout problems: `reel rerender <id> --style ...`. For a content fact problem: re-film, or if the fact is true and verified, add it to `brand/facts.yaml` and `reel approve <id>`. For a critique score below 7: re-film per the fixes |
| "Instagram needs reconnecting" (auth_needed) | Publishing is paused automatically. Generate a new token in the Meta dashboard → `scripts\connect-instagram.ps1` → `reel doctor --live` → `reel resume` |
| Too long after editing (> 90 s) | Film a shorter take, or split it into two clips |
| Clip rejected (no audio, < 3 s) | Re-film; the engine needs your voice track |
| Posted something wrong | Delete it in the Instagram app (the API can't edit or delete Reels). Then `reel pause` and investigate with `reel show <id>` |
| Brand copy corrected in the vault | `.venv\Scripts\python.exe scripts\build_brand.py` |
| Nothing happens | `reel status` → `reel doctor` → `Get-ScheduledTaskInfo -TaskName 'Brain - Reel Engine Tick'`; logs are in `data\logs\engine.log` |
| Restore a backup | Stop the tasks (below), copy `data\backups\engine-<stamp>.sqlite` over `data\engine.sqlite`, start the tasks |

## Scheduled tasks (Windows Task Scheduler)
| Task | When (Asia/Riyadh) | Runs | Stops a run after |
|---|---|---|---|
| Brain - Reel Engine Tick | every 15 min | `.venv\Scripts\pythonw.exe -m reelengine.cli tick` | 14 min |
| Brain - Reel Engine Daily | daily 03:30 | `... -m reelengine.cli daily` | 1 h |
| Brain - Reel Engine Weekly | Saturday 20:00 | `... -m reelengine.cli weekly` | 2 h |

Working folder: the project root. Settings: wake to run, start when available, no overlapping runs, runs on battery. The tasks run as Yazeed only while he's signed in (a locked screen is fine). The optional `-RunWhetherLoggedOn` uses a stored password, not S4U: S4U can't decrypt Credential Manager, and OneDrive syncs the inbox only while he's signed in. "Allow wake timers" must be enabled; the scripts never change it.
- Preview: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-tasks.ps1 -WhatIf`
- Register or update: the same without `-WhatIf` (safe to re-run)
- Run now: `Start-ScheduledTask -TaskName 'Brain - Reel Engine Tick'`
- Status: `Get-ScheduledTaskInfo -TaskName 'Brain - Reel Engine Tick'` (LastTaskResult 0 = ok), or `reel doctor`
- Pause all jobs: `Get-ScheduledTask -TaskName 'Brain - Reel Engine *' | Disable-ScheduledTask` (undo with `Enable-ScheduledTask`)
- Remove: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\unregister-tasks.ps1 [-WhatIf] [-Force]`
- After a Windows password change (only if registered with `-RunWhetherLoggedOn`): run `register-tasks.ps1 -RunWhetherLoggedOn` again.

## Secrets (Windows Credential Manager, keyring service "ReelEngine")
- Connect Instagram: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\connect-instagram.ps1`
- Any secret (`ig_access_token | ig_app_secret | telegram_bot_token`): `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\set-secret.ps1 -Name <name>`. The input is hidden and passed on stdin only; the script prints only "stored" or the error.
- Never paste a token into a command line, a file, `config.toml` or a chat. Tests use the service `ReelEngine-Test`.

## Notifications
Every notification is a DB row (shown on the dashboard, with tokens masked) plus a Windows toast when `[notify].toast = true`. Toasts appear as "Windows PowerShell"; Focus/Do Not Disturb can hide them, so the dashboard is the reliable place to look. Telegram is v2 and stays off.

## Setup and repair
`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 [-SkipModel] [-WhatIf]`. Safe to re-run. Exit 0 = ok, 1 = a step failed, 2 = setup done but `reel doctor` reported FAIL.

## Tests
- Fast suite: `.venv\Scripts\python.exe -m pytest -q`
- End-to-end with the synthetic clip (GPU + ffmpeg, no subscription use): `.venv\Scripts\python.exe -m pytest -m e2e tests\test_e2e.py -k fake`
- End-to-end with the real brain (3 Claude calls): `.venv\Scripts\python.exe -m pytest -m "e2e and brain" tests\test_e2e.py`
- Caption rendering QA frames: `.venv\Scripts\python.exe scripts\caption_qa.py`
- Learning simulation: `.venv\Scripts\python.exe scripts\learning_sim.py --seed 20260929 --seeds 200 --posts 90`
- Security: `.venv\Scripts\python.exe -m pip_audit -r requirements.txt` and `.venv\Scripts\python.exe -m bandit -r reelengine -q`
