# Decisions pending (v1.1 branch `feature/calendar-upload-accounts`, 2026-10-14; v1.2 branch `feature/v1.2-days-reminders-mac`, 2026-10-15)

The real decision log (`docs/DECISIONS.md`, D-001 onwards) is private. These entries are written in the same style; on merge, give them the next free D-numbers and move them there. Owner: Yazeed. Built by tech-lead and team; legal rulings by legal-compliance; facts by researcher (all checked 2026-10-14).

## PD-1: Saudi occasions calendar lives in the DB, seeded from YAML
- **Decision:** a new `occasions` table (schema v2) holds every day: names AR/EN, rule (`gregorian` every year, `hijri` every year, `once`), month/day/year, duration, category, official yes/no, content angle, notes, source URL, checked date, match words. The seed is `occasions.yaml` in the brand pack, falling back to the generic `brand/occasions.yaml` (Saudi occasions aren't personal, so they ship in the public repo). Seeding only adds slugs the DB has never seen; deleting a seed day leaves a tombstone so a reseed doesn't bring it back.
- **Why:** Yazeed edits from the dashboard and the CLI, so the DB must be the source of truth; YAML keeps the sourced seed reviewable.
- **Every seeded day has a source and the date checked.** Primary-source coverage is thin for the holiday status (several are secondary sources, marked in `notes`).

## PD-2: Hijri dates from Umm al-Qura via `hijridate==2.6.0`
- **Decision:** convert Hijri days with `hijridate` (the maintained successor of `hijri-converter`, by the same author, Umm al-Qura based, range 1343-1500 AH), pinned. No hard-coded Gregorian guesses. Every Hijri occurrence is flagged approximate (±1 day) because Ramadan and the Eids are finally fixed by the moon-sighting announcement.
- **Check:** 1 Ramadan 1448 = 2027-02-08, 1 Shawwal 1448 = 2027-03-09, 10 Dhu al-Hijjah 1448 = 2027-05-16 (matches the researcher's 2027 projections).

## PD-3: Look-ahead 3-21 days; the scheduler gets an occasion window
- **Decision:** the weekly brief gets the occasions starting 3-21 days ahead (`[calendar] lookahead_*`), may write at most two occasion scripts (`occasion` field, validated against the upcoming ids and counted by the validator), and carries the occasion filming checklist. The daily job sends one reminder per occurrence. When a QA-passed reel is about an occasion (its id in `features_json.occasion`, or its name/match words in the topic, hook, title or caption), the scheduler picks the bandit's preferred slot inside `[day - post_window_days, last day]`; every normal constraint still applies, and with no valid slot in the window it falls back to the normal choice (logged as `occasion_missed`).
- **Why:** an occasion reel posted after the day is wasted; the bandit still chooses among the slots inside the window, so learning isn't bypassed.

## PD-4: Occasion legal rules are additive (`brand/gates_occasions.yaml`)
- **Decision:** a structured rule file (groups, `match`, optional `near` + `window`, `fields`, `hold`/`warn`) checked by `gates.check_occasion_rules` inside `run_content_gates`: occasion + discount/offer, flag/Shahada/leaders + commercial words, the royal emblem, religion + mockery, an altered flag, occasion giveaways (hold); a leader mention, Quran recitation in speech, the Shahada as text, the National Day identity (warn). Nothing in `gates.yaml` changed; the learner can't touch either file. The generic file always applies: a brand pack's own `gates_occasions.yaml` can only add groups, rules and checklist lines. The commercial-word group avoids the bare word «كود» (code) so tech reels about coding aren't held. A filming checklist covers what text can't show.
- **Source:** legal-compliance ruling 2026-10-14 (Flag Law M/3 1393H art. 14-16 and 20, read on a secondary mirror; Ministry of Culture flag guide; Ministry of Commerce ban on commercial use of the flag/Shahada, emblem and leaders, 2022-09-16; Audiovisual Media Law art. 5; the MoC discount licence). Not legal advice.

## PD-5: Dashboard write API for uploads and edits
- **Decision:** the server stays a static-site server, plus a small `WriteApi` (POST `/api/upload`, `/api/calendar`, `/api/accounts`; GET `/api/session`, `/api/uploads`) when `[dashboard] allow_writes = true` (default; `--read-only` turns it off). CSP changes from `connect-src 'none'` to `connect-src 'self'` so the page's own `app.js` can call it; everything else in the CSP is unchanged (`form-action 'none'`, no inline script). Same-origin `Origin`, `Sec-Fetch-Site`, a per-process CSRF token in a custom header, Content-Length caps, no chunked bodies.
- **Upload path:** raw body (not multipart) streamed to `.upload-<hex>.part` in the inbox, signature + type + size + disk checks, sha256 duplicate check, then `os.replace` into the inbox. From there the existing intake does stability, dedupe and `data/raw`. Uploads never schedule or publish; dry-run is untouched. Demo mode never accepts uploads.
- **Why not call `intake.take()` directly:** the spec says "feed the existing intake path"; dropping into the inbox keeps one path, one set of tests, and the engine lock rules (the server never holds the engine lock).

## PD-6: Account analysis, official API only, manual import by default
- **Decision:** `accounts`, `account_media`, `account_analyses` tables; config `[accounts]` (own/reference/competitor lists, retention, discovery mode); CLI `reel accounts …`; an Accounts tab. **Business Discovery is not available with Instagram Login** (Meta docs, checked 2026-10-14), so `discovery = "off"` by default and data comes from manual import (pasted lines or CSV; links stored as text, never fetched). A `BusinessDiscoverySource` adapter (graph.facebook.com, own keyring token `fb_discovery_token`, minimal fields) is ready behind `discovery = "facebook_login"`, runs only from `reel accounts fetch`, and has only been tested against a mock.
- **Brain:** task `accounts` (no tools, schema-validated, inside the existing 60-call cap), run by the weekly job before the brief when posts exist. Input is pseudonymised (ACC_nn, @user, no links); output ideas are checked for @handles, links and copied captions. Ideas feed the brief as patterns only.
- **Privacy:** legal-compliance ruling 2026-10-14: creators' public posts are personal data; basis = public source + legitimate interest; minimisation; captions 90 days, rows 180 days; `forget` (secure_delete, WAL checkpoint, VACUUM); retention counted from first import; pseudonyms never reused; captions escaped inside `<data>`; pseudonymise before the transfer to Anthropic (PDPL art. 29). `docs/PRIVACY.md`.

## PD-7: Copy guard
- **Decision:** any reel caption/hook/title, brief script or analysis idea sharing ≥ 60% of its 5-word phrases with a stored reference/competitor caption is held (hard) or rejected. Protects copyright and the own-content rule R6 now that other accounts' captions are stored.

## PD-8: Small fixes found on the way
- `brand.example/episodes.yaml` E-4 had an unquoted `: ` and didn't parse, so `make_brief` failed with the example pack; quoted.
- `scripts/set-secret.ps1` accepts `fb_discovery_token`.

# v1.2 (branch `feature/v1.2-days-reminders-mac`, 2026-10-15)
Built by tech-lead and team; dates by researcher; rulings by legal-compliance (2026-10-15); UI by designer.

## PD-9: The full Saudi days calendar (42 seed days)
- **Decision:** `brand/occasions.yaml` grows from 21 to 42 days in the existing schema and categories: national 4 (Founding Day, Flag Day, Saudi Green Initiative Day, National Day), religious 7 (Hijri New Year, start of Ramadan, last ten nights, Eid al-Fitr, Hajj season, Arafah, Eid al-Adha), international 9, career 9, tech 13 (incl. one-off events FII10, GEW 2026, Black Hat MEA 2026, CS Ed Week 2026, Safer Internet Day 2027, Girls in ICT Day 2027, GAIN 2027, Programmers' Day 2027). 7 are official (royal order / Council of Ministers / public holiday). Every day has a source and `checked_on` (2026-10-15, or 2026-10-14 for 3 days not re-checked); "secondary" in notes marks days not confirmed on a primary page.
- **Left out:** sensitive days per legal-compliance (Mawlid, Ashura, Isra and Mi'raj, Mid-Sha'ban, Valentine's, Christmas, Halloween, Pride); unsourced (Father's Day, Doctors' Day, a Saudi Engineers' Day, Labour Day as a Saudi occasion, Saudi-specific Teacher/Arabic/Children's days, the 2026/27 school calendar). LEAP 2027 was removed: its date is unconfirmed (LEAP 2026 moved to Aug-Sep). Mother's Day (21 Mar) is in, renamed «يوم الأم في العالم العربي», with a note that it is religiously contested and to ask before filming.
- **Keywords are specific phrases** («يوم التعليم», not «التعليم»), so ordinary reels aren't pulled into a day's posting window.

## PD-10: Re-seed refreshes untouched seed days
- **Decision:** `reel calendar seed` (and every job start) still adds only new slugs and never revives a deleted one, and now also refreshes seed rows Yazeed never edited (`origin = seed`, `updated_at = created_at`) when the seed content changed. Edited rows and tombstones are never touched. **Why:** corrected dates and sources should reach the DB without overwriting anything Yazeed changed. Rows removed from the seed (e.g. `leap-2027` in an existing v1.1 DB) stay until deleted by hand.

## PD-11: Reminders at 14/7/3/0 days with ready-to-film ideas
- **Decision:** `[calendar] remind_days = [14, 7, 3, 0]`. The due lead is the smallest lead ≥ days left; it is sent once (`occasion_reminder:<slug>:<start>:<lead>` in `meta`), and sending it marks every larger lead, so a lead missed while the computer was off is skipped, never sent late. A v1.1 reminder (`occasion_reminded:<slug>:<start>`) counts for every advance lead, so upgrading doesn't announce the same day twice (the day-of reminder still goes out). Each reminder: names AR/EN, date (+ Hijri and ±1 day), days left, official or not, angle, 2-3 ideas (title, hook line, 3 beats, shot note, length) and the legal checklist (`category_checklist` + per-day `occasion_checklist` in `brand/gates_occasions.yaml`, additive).
- **Ideas:** brain task `occasion_ideas` (no tools, schema + gates + copy guard + no handles/links), at most one call per run for the whole window, cached per occurrence in `occasion_ideas` and reused by later leads and the dashboard. Skipped when fewer than `brain_reserve_calls` (10) calls are left in the 24 h cap; then Saudi-dialect templates (tested against the gates for all 42 days), upgraded to brain ideas on a later run.

## PD-12: Notification channels by platform; Telegram finished but off
- **Decision:** `[notify] toast = true` now means "desktop notification on this OS" (Windows toast unchanged; macOS via `osascript` with the script on stdin and text as argv). Telegram (Bot API) is implemented under the legal-compliance PDPL ruling: token in the credential store, `telegram_chat_ids` allow-list, `telegram_kinds` from a fixed set (default occasion, action, error), title-only for non-occasion kinds, refuse-on-handle/URL/path/secret, `protect_content`, no link previews. **Status:** Yazeed skipped Telegram on 2026-10-15: no token, no chat id; `doctor` shows "not configured (optional)".

## PD-13: macOS support
- **Scheduler:** launchd LaunchAgents generated with `plistlib` (`local.reelengine.tick|daily|weekly`, same times as Windows), `reel schedule plist|install|uninstall|status --mac`. Written to `data/launchd/`; **not loaded** (needs Yazeed's OK).
- **Transcription:** ctranslate2 has no Metal/MPS backend (CTranslate2 docs; verified locally: 0 CUDA devices, CPU types int8/int8_float32/float32), so macOS goes straight to CPU int8.
- **ffmpeg:** Martin Riedl's static, signed macOS arm64 build, release 9.0.2 (ffmpeg.zip 28,395,699 B, sha256 c8ed4c4e…d924; ffprobe.zip 28,317,701 B, sha256 fcbe8395…7ea6), pinned in `scripts/setup-mac.sh`. GPLv3. FriBidi is linked inside its libass (no `--enable-libfribidi` flag), so `doctor` and the setup script check FriBidi functionally (libass's "Shaper: FriBidi" line) on all platforms. evermeet.cx was rejected (Intel only).
- **claude CLI:** after PATH, also `~/.local/bin`, `~/.claude/local`, Homebrew paths, and the newest Claude desktop app bundle (`~/Library/Application Support/Claude/claude-code/<ver>/…/claude`).
- **Config:** `config.mac.example.toml` (dry-run, inbox `~/Movies/Reel Inbox`, vault writes off, CPU int8, the macOS ffmpeg paths).

## PD-14: Link account and "What to post"
- **Decision:** DB schema v3 adds `accounts.niche`, `account_recommendations` (cascade + explicit delete on `forget`, pruned after `metrics_retention_days`) and `occasion_ideas`. Brain task `recommend` (no tools, `review_model`, pseudonymised input: ACC_nn, stats/posts only if imported, niche with @mentions and links replaced, playbook rules and best slots, trend titles, up to 4 fitting occasions in the next 60 days scored by niche keywords + category); validated by the copy guard, the gates on hooks, no handles/links, known occasion ids, an HH:MM best time. Template plan without the brain. `basis = description` when no reel numbers, with the note "Based on your description; add reel numbers for sharper advice." Dashboard: a native `<dialog>` (header + Accounts tab) → POST `link` starts one background job, the page polls `recommend_status`, then opens the card. Plans are never sent as notifications.

## PD-15: Small fixes found on the way
- Six tests depended on the wall clock (quiet hours 00:30-11:00) and failed in the morning; they now inject `now`, and `jobs._published_today` follows the injected `now`. Checked at 7 times of day.
- `tests/conftest.py` now blocks real Claude CLI calls (the CLI is found on the Mac; one early test run made one real call with only example-pack data before the guard existed) and any call to api.telegram.org.
- New hold rule `occasion.pride_content` (legal-compliance 2026-10-15: Anti-Cybercrime Law art. 6(1), Audiovisual Media Law art. 5), tested not to catch «فخور» or «ميمز».

## Open (✍️ for Yazeed)
- **v1.2:** OK to load the launchd jobs (`reel schedule install --mac`); OK to download Whisper large-v3 (3.09 GB) for clip processing on the Mac; whether to set up Telegram later; push and PR/release of this branch; re-run the suite on the Windows PC.
- Push this branch and open a PR: not done (needs your OK).
- Which accounts to attach (own username; reference/competitor lists).
- Whether to switch on Business Discovery (Facebook Login + a Page + App Review question); a privacy-policy URL for the Meta app if so.
- Re-run the suite on the Windows PC (the 49 private gate tests included).
