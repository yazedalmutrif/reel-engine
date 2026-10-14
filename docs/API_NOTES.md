# Instagram API notes (verified 2026-09-29 by researcher from Meta developer docs)

These are the facts the publisher and insights code must follow. Items marked **VERIFY** can't be settled from the docs. Make them configurable and confirm them on the first live connect (`reel doctor --live`).

## Path
- **Instagram API with Instagram Login**, host `graph.instagram.com`. No Facebook Page is needed. The account must be a professional account (Business or Creator).
  Source: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/
- Meta app type **Business**, with the Instagram product added. It stays in **Development mode / Standard Access**. The owner's IG account is added as an **Instagram Tester** (App roles → Roles). No App Review and no Business Verification are needed for a self-owned account.
  Source: https://developers.facebook.com/docs/instagram-platform/app-review/
- **VERIFY:** the exact dashboard menu labels for the tester invite.

## Scopes (least privilege)
`instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_insights`. Add `instagram_business_manage_comments` only if comment features are ever enabled (off in v1). The old `business_*` scope names were deprecated 2025-01-27.

## API version
`v26.0` (released 2026-07-29), set as `api_version` in config.toml.

## Tokens
- A short-lived token comes from the OAuth code exchange (`POST https://api.instagram.com/oauth/access_token`) and lasts 1 h.
- A long-lived token lasts 60 days: `GET https://graph.instagram.com/access_token?grant_type=ig_exchange_token&client_secret=…&access_token=…`
- The **dashboard "Generate token"** (Instagram → API setup) returns a long-lived token directly. This is the setup path for Yazeed (no redirect server needed).
- Refresh: `GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token=<LL>`. The token must be **≥ 24 h old and not expired**, and each refresh gives another 60 days. The engine refreshes when the token is older than 7 days and older than the last refresh + 7 days (weekly), and stores `issued_at`/`expires_at` (not the token) in the DB.
- A refresh failure or error code 190 → state `auth_needed`, publishing pauses, Yazeed is notified with the re-connect command. No silent retry loops.
- **VERIFY:** what invalidates a token (a password change, app removal); treat it as possible.
- Required secrets in keyring: `ig_access_token` (plus `ig_app_secret` only if the exchange flow is used). `ig_user_id` is not secret and goes in the DB/config.

## Publishing a Reel: resumable upload (no public URL needed)
1. `POST https://graph.instagram.com/v26.0/{ig_user_id}/media` with `media_type=REELS`, `upload_type=resumable`, `caption`, `share_to_feed=true`, `thumb_offset=<ms>`, optional `cover_url`, `audio_name`, `collaborators` (≤3), `location_id`, `user_tags`, `is_ai_generated` (false: real footage; the param was added 2026-06-22), `trial_params` (Trial Reels need 1,000+ followers, so off in v1). The response has a container `id` and an upload `uri`.
   - **VERIFY:** Meta's resumable sample uses host `graph.facebook.com` for container creation. With an Instagram-Login token the container call is expected on `graph.instagram.com`. Make the host configurable and test both on the first live connect.
2. `POST https://rupload.facebook.com/ig-api-upload/v26.0/{container_id}` with headers `Authorization: OAuth <token>`, `offset: 0`, `file_size: <bytes>`, and the raw bytes as the body. It can resume from an offset.
3. Poll `GET /{container_id}?fields=status_code,status` about once a minute for up to about 5 min. Values: `IN_PROGRESS`, `FINISHED`, `ERROR`, `EXPIRED` (not published within 24 h), `PUBLISHED`. A timeout means re-poll next tick; only after 24 h does it count as expired and need a new container.
4. `POST /{ig_user_id}/media_publish` with `creation_id=<container_id>` returns the media id. Then read back `GET /{media_id}?fields=id,permalink,timestamp,media_type,media_product_type`.
- **Idempotency:** persist `container_id` before the upload and `publish_requested_at` before media_publish. On a restart in the `publishing` state, first check `GET /{container_id}?fields=status_code`. If it is `PUBLISHED`, find the media in `GET /{ig_user_id}/media?fields=id,caption,timestamp` (match by caption hash or time) and mark it published. **Never create a second container for a reel that has a live one.**
- Flow A (video_url on a public URL) is not used, so nothing is hosted publicly.
Sources: https://developers.facebook.com/docs/instagram-platform/content-publishing/ · …/content-publishing/resumable-uploads/ · …/instagram-graph-api/reference/ig-user/media/

## Reel specs (API level)
MP4 with **moov at the front** (`-movflags +faststart`), no edit lists; H.264 or HEVC, progressive, **closed GOP**, 4:2:0; 23–60 fps; AAC ≤ 48 kHz, 128 kbps; video VBR ≤ 25 Mbps; ≤ 300 MB; 3 s – 15 min; max 1920 px horizontal; 9:16 recommended. **Engine target: 1080×1920, 30 fps, ≤ 90 s** (Reels-tab eligibility of 5–90 s appears only in secondary sources, so stay inside it; the minimum target is 5 s).
- Caption ≤ 2,200 chars. **Hashtags ≤ 5** (Instagram cap since 2025-12-18, per the ranking research).

## Quota
Meta's docs conflict (100/24 h on the overview page vs "currently 50" on the reference page). **Never hardcode it.** Before each publish, call `GET /{ig_user_id}/content_publishing_limit?fields=quota_usage,config` and refuse if usage ≥ total − 2. Our own cap (`max_posts_per_day`, start at 1) is far below both. Watch the `X-App-Usage` header and back off above 75%.

## Insights
- Media (Reel): `views, reach, likes, comments, shares, saved, reposts, total_interactions, ig_reels_avg_watch_time (ms), ig_reels_video_view_total_time, reels_skip_rate, crossposted_views, facebook_views`. **Deprecated, don't use:** `impressions, plays, clips_replays_count, ig_reels_aggregated_all_plays_count`. Request the metrics individually or in small groups, so one unsupported metric doesn't fail the whole call, and store the raw JSON.
- Account: `follower_count, reach, accounts_engaged, profile_links_taps, views, likes, comments, shares, saves, replies, reposts, total_interactions, follower_demographics, engaged_audience_demographics, follows_and_unfollows`.
- Data can lag **up to 48 h**. `follower_count` isn't available under 100 followers. Empty data comes back as an empty set, not 0 (store NULL, not 0).
- The learner must use `reels_skip_rate` (a direct retention signal) and `ig_reels_avg_watch_time / duration`.
Sources: https://developers.facebook.com/documentation/instagram-platform/reference/instagram-media/insights · …/api-reference/instagram-user/insights

## Limits of API-published Reels
No access to Instagram's music library (audio must be in the file). No editing after publish via the API. No alt_text for Reels. No official statement that API posts get less reach (folklore; tested as a hypothesis). The Developer Policy bans excessive-rate scripts and fake engagement; official publishing within quota is the low-risk category. Access can be reduced after 30 days of app inactivity, so the daily insights job keeps it active.

## Other accounts: Business Discovery (verified 2026-10-14 by researcher from Meta developer docs)
- **Not available on the engine's path.** Business Discovery (another Business/Creator account's public profile counts and media) is documented only for the **Instagram API with Facebook Login** (host `graph.facebook.com`, a Facebook User or Page token, an Instagram professional account linked to a Facebook Page). The Instagram-Login overview lists no Business Discovery (and says hashtag search is not supported there). The docs don't literally say "not supported on graph.instagram.com"; one test call with an Instagram-Login token would settle it, but plan on it failing. **VERIFY** before relying on it.
  Sources: https://developers.facebook.com/documentation/instagram-platform/overview.md · https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login · https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/business-discovery · https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery
- Call: `GET https://graph.facebook.com/{version}/{your-ig-user-id}?fields=business_discovery.username(<name>){followers_count,media_count,media{...}}`. Permissions: `instagram_basic`, `instagram_manage_insights`, `pages_read_engagement` (plus `ads_read`/`ads_management` if the Page role comes through Business Manager). Media fields shown in the docs: `id`, `comments_count`, `like_count`, `view_count` ("includes both paid and organic"). **VERIFY:** `caption`, `timestamp`, `permalink`, `media_product_type` (the engine requests them; drop any that error), whether a hidden like count comes back, and rate limits (the overview says Business Discovery is outside the standard Business Use Case limits).
- Age-gated accounts return nothing; reading a returned media object directly fails with a permissions error; the media edge pages with cursors only.
- **Access level VERIFY:** Advanced Access (App Review + Business Verification) is "needed for accounts you don't own or manage"; the Business Discovery pages don't say whether Standard Access in Development mode is enough. Test in Development mode first.
- **What switching it on takes:** add Facebook Login for Business to the Meta app, link the account to a Facebook Page, get a Facebook-Login token with the permissions above, store it with `scripts\set-secret.ps1 -Name fb_discovery_token`, set `[accounts] discovery = "facebook_login"` and `discovery_ig_user_id`. Publishing keeps using the Instagram-Login token: two tokens, two hosts.
- **Meta Platform Terms** (https://developers.facebook.com/terms/, checked 2026-10-14): delete platform data when it is no longer needed or when Meta or the user asks (§3.d); no building or enhancing user profiles without consent (§3.a); a privacy policy that explains deletion (§4). The engine's retention and `forget` follow this; see `docs/PRIVACY.md`.
- Until then: **manual import** (Yazeed pastes links, captions and numbers he can see, or a CSV). No scraping, no password logins, no unofficial libraries.
