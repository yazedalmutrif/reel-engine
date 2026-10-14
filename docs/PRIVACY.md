# Privacy note: attached accounts (added 2026-10-14)

Not legal advice. Ruling by legal-compliance (2026-10-14), based on the PDPL text on laws.boe.gov.sa and Meta's Platform Terms.

**What is collected.** For each Instagram account Yazeed attaches (his own, plus reference or competitor accounts he chooses): the username, a role, and for up to `max_media_per_account` (30) recent public posts: the permalink, posting time, media type, caption text, like/comment/view counts and length. Nothing else. Never: profile pictures or any image or video of other people, bios, contact details, locations or geotags, comments or commenters, follower lists, and no inferred religion, tribe, health or politics. Only public Business or Creator accounts, added one by one; no bulk discovery and no personal accounts.

**Where it comes from.** Manual import (what Yazeed can see and copies in), or, only if he switches it on, Meta's official Business Discovery API. No scraping, no logins as anyone, no unofficial tools.

**Why (legal basis).** Creator accounts' usernames and captions are personal data under the PDPL (art. 1), even when public. The basis is collection from a public source (art. 10(2)) for a legitimate interest (art. 6(4) / 10(7) as amended): learning which public content formats work, for Yazeed's own channel. **Legitimate-interest assessment:** the purpose (benchmarking public posts) is narrow; the data is non-sensitive, already public and minimal; no profiles of people are built, nothing is published or shared, and data expires on a short clock; the people concerned are professional accounts publishing to promote themselves, so a reasonable expectation of such benchmarking exists. The interests of the account holders are not overridden. ✍️ The Implementing Regulation article for this assessment was not verified.

**Who sees it.** It stays in the engine's SQLite file on Yazeed's PC (not in the vault, not in the repo; git-ignored). For the weekly analysis, the engine sends caption text and numbers to Anthropic (USA) through the Claude CLI **after pseudonymisation**: usernames become `ACC_01`…, other @mentions become `@user`, links are removed, and the mapping never leaves the PC. Sending identifiable personal data abroad would be a transfer under PDPL art. 29; pseudonymising first keeps the residual risk low. Keep Claude's model-training setting off. A lawyer is needed before this becomes a service for other people or before identifiable data is sent abroad.

**How long.** Caption text is wiped after 90 days (`retention_days`); whole post rows are deleted after 180 days (`metrics_retention_days`); analyses after 180 days. The engine's own reels (Yazeed's own account) are kept as before.

**Deletion.** `reel accounts forget <username>` (or Remove on the Accounts tab) deletes the account and every stored post at once, with SQLite `secure_delete` and a `VACUUM`. `reel accounts prune` applies the retention clock now. Anyone who asks to be removed: run `forget` and don't re-attach them.

**Meta app privacy policy.** If Business Discovery is switched on, the Meta app needs a public privacy-policy URL saying the above (what, why, retention, deletion on request) (Platform Terms §4). ✍️ Not written yet.
