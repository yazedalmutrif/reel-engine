# Decisions pending (branch `feature/calendar-upload-accounts`, 2026-10-14)

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

## Open (✍️ for Yazeed)
- Push this branch and open a PR: not done (needs your OK).
- Which accounts to attach (own username; reference/competitor lists).
- Whether to switch on Business Discovery (Facebook Login + a Page + App Review question); a privacy-policy URL for the Meta app if so.
- Re-run the suite on the Windows PC (the 49 private gate tests included).
