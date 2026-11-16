# D6 YouTube LIVE Acceptance

**Reviewed:** 16 November 2026

**Status:** offline implementation complete. The owner-approved Privacy Policy and Terms are publicly available and verified. Genuine LIVE acceptance remains blocked by project/key verification, current quota evidence, a designated LIVE host, and an eligible runtime. No YouTube request was sent.

## Gate status

| Gate | Weight | Status | Evidence |
|---|---:|---|---|
| G0 — Repository synchronization | 8% | PASS | `git fetch origin` completed on 16 November 2026. `HEAD` and `origin/main` match at `c5ab57724cd2090b2b807d4c700a5908648957cf`. No reset or overwrite was used; the pre-existing untracked `.playwright-cli/` directory was preserved. |
| G1 — YouTube API contract | 12% | PASS (offline) | Fixed `search.list` and bounded `videos.list` request builders/parsers, source identity, validation, pagination binding and method-specific gateway routing are implemented and tested. No provider response was received. |
| G2 — Quota governance | 18% | PASS (offline) | Separate durable search and other-method pools, pre-request reservation, provider-day timezone handling and conservative failure accounting are implemented and tested. Actual Cloud project quota remains UNKNOWN. |
| G3 — Schedule, cache and Source Center | 12% | PASS (offline) | Manual schedule eligibility, cache-first/stale delivery, focused expiry, and Vietnamese/English Source Center budget and status fields are implemented and tested. |
| G4 — Genuine YouTube LIVE evidence | 16% | BLOCKED — 0% | The owner authorized up to two requests total (at most one `search.list` and one `videos.list`, no retries); neither was used. Owner-approved policies are publicly verified, and code now records their approval separately from LIVE approval. The API key/project association and current project quota remain unverified. The hosted API reports `read_only=true`, `fetch_enabled=false`, and no selectable sources, so it cannot run the agreement/Fetch flow. No provider request was sent. |
| G5 — SQLite and application flow | 16% | PASS (offline) | Synthetic-only tests prove Fetch result persistence, API readback after SQLite reopen, source-selection combinations, cache-only delivery and exclusion from keyword scoring. No synthetic record is labeled LIVE. |
| G6 — Regression and browser QA | 12% | PARTIAL (6 points credited) | Focused backend suites passed, including all 18 Wikimedia gateway tests; frontend 19/19, changed-file Ruff, JS syntax and diff checks passed. The one full Backend run ended 904 passed and 16 failed because Firestore emulator setup was unavailable. The actual local-app browser run is documented below. A current hosted-site capability check confirmed read-only mode and disabled Fetch; the newly changed API fallback still needs deployment verification. |
| G7 — Report and local commits | 6% | PASS | The owner-approved policy pages were pushed in `c5ab577`; this report records the deployed public pages and current hosted API behavior. The present code/report changes remain local until committed. |

**Weighted gate progress: 78%.** G4 contributes 0%; G6 receives 6/12 points. The goal remains incomplete and is not 100% complete.

## Public policy publication, verified 16 November 2026

The owner explicitly approved publication and confirmed that the owner details were in the ignored `.env.youtube-audit-owner` file. The final pages were generated from allowlisted owner fields, reviewed, committed and pushed as `c5ab577`. Both public URLs returned HTTP 200. Their deployed HTML matched the tracked files byte-for-byte; the privacy page carried policy version `d6-youtube-v2`, and the owner field matched the configured value. The comparison emitted only lengths/equality status; no `.env` values were printed.

- Privacy Policy: https://trend-scout-mu.vercel.app/dashboard/privacy.html
- Terms of Service: https://trend-scout-mu.vercel.app/dashboard/terms.html
- Dashboard: https://trend-scout-mu.vercel.app/dashboard/

The policy approval is now source-controlled against the exact approved version in `Backend/app/sources/youtube/policy.py`; a later version change will fail closed until separately approved. LIVE approval remains separate. The current hosted API was queried read-only and returned HTTP 200 with `environment=vercel`, `read_only=true`, `fetch_enabled=false`, `source_selection_supported=false`, `policy_status` and `policy_version` absent, and YouTube/Bing disabled. The code change in this continuation fills those policy metadata fields, but does not enable Fetch or source selection.

## Repository and implementation

The implementation extends the existing local LIVE worker and keeps YouTube-specific behavior at the source boundary:

- `Backend/app/sources/youtube/` validates and normalizes YouTube search and video-list responses. `search.list` uses a fixed server-owned query, `type=video`, `part=snippet`, `regionCode=US`, `relevanceLanguage=en`, `order=relevance`, one page and at most 50 results.
- A deterministic, versioned planner selects no more than two curated scopes in an eligible slot. It prioritizes fashion, home/living, beauty, lifestyle and consumer technology, then rotates through the remaining registered scopes. A returned video is deduplicated by provider/video ID while retaining query evidence.
- `videos.list` support is bounded to `part=snippet`; it can request metadata by IDs or a `mostPopular` chart. It does not request statistics. The normal Fetch path does not make a second metadata call because `search.list` already returns the fields currently displayed. The `mostPopular` chart is not used as a general consumer-trend feed: YouTube says it now covers Trending Music, Movies and Gaming charts.
- `LivePageGateway` injects the key through `X-Goog-Api-Key`, not the URL, and reserves `search.list` and `videos.list` against separate internal pools before dispatch. Retries are disabled for the configured YouTube source limits.
- The user-triggered refresh schedule is 07:00, 15:00 and 23:00 `Asia/Ho_Chi_Minh`, with a ten-minute grace period, an eight-hour minimum interval and no missed-slot catch-up. At most two search requests are allowed in a scheduled batch.
- Fresh cache is returned without provider traffic. Eligible stale refresh failures preserve stale evidence and its original timestamps. The YouTube cache has an eight-hour freshness window and seven-day retention ceiling. Startup and hourly cleanup remove expired YouTube cache entries and expired YouTube checkpoints; shared snapshot blobs are deleted only when no remaining checkpoint references them.
- Fetch results retain YouTube videos as attributed source evidence. LIVE YouTube records are excluded from keyword extraction, collection scoring and composite metrics. Search position is not search volume or proof of national demand.
- Source Center exposes approval/configuration/host state, two separate local quota buckets, UNKNOWN provider quota, request and observation timestamps, cache status, next eligible time, errors and accepted record count. Vietnamese and English strings are present. The Fetch view only renders allowlisted YouTube watch links and text-safe metadata.

The original source/quota implementation is in `ac78355` (`feat(d6): add YouTube source and quota governance`). This continuation adds an independent policy-publication flag, blocks YouTube Fetch until the current policy is approved and the user's current version is accepted, and allowlists video fields returned to the public Fetch status API. It also requires the normal local LIVE composition for non-synthetic YouTube and distinguishes fresh provider evidence from cached/synthetic evidence in capabilities.

## API and policy references checked

Documentation was checked on 16 November 2026:

- [`search.list` reference](https://developers.google.com/youtube/data/reference/rest/v3/search/list): `maxResults` supports up to 50; `regionCode=US` restricts results to videos viewable in that region, and `relevanceLanguage=en` is a relevance preference. Neither establishes the residence of creators or viewers. The method has a separate documented daily bucket and each page request costs quota.
- [`videos.list` reference](https://developers.google.com/youtube/data/reference/rest/v3/videos/list): `part=snippet` exposes title/channel metadata; a call costs one unit; `maxResults` is not supported with the `id` filter. Statistics are deliberately not requested.
- [Quota calculator](https://developers.google.com/youtube/data/determine_quota_cost?hl=en), updated 12 November 2026: documented defaults are 100 `search.list` calls/day in its separate bucket and 10,000 units/day for other methods; requests including invalid ones cost at least one point, and daily quota resets at midnight Pacific Time. These are documentation defaults, **not verified project limits**.
- [YouTube Developer Policies](https://developers.google.com/youtube/terms/developer-policies): an API client must require privacy-policy agreement before users can use its features; applicable API data must be deleted or refreshed within 30 calendar days. The owner-approved privacy/terms pages are now published. The in-app agreement flow is implemented; a local operator-only deletion-request workflow is implemented and focused-test verified, with manual backup/log limits documented.
- [Getting started](https://developers.google.com/youtube/v3/getting-started?authuser=01) and [API key authentication](https://developers.google.com/youtube/data/guides/authentication): the standard project/API-key setup steps do not list publication of a privacy policy as a registration prerequisite. The [YouTube API Services Terms](https://developers.google.com/youtube/terms/api-services-terms-of-service) and Developer Policies do require a published privacy policy before the API Client provides API-backed features to users; Google may request additional information during review. **Registration can proceed without this policy; using YouTube through TrendScout cannot.**
- [Additional derived-metrics policy](https://developers.google.com/youtube/terms/derived-metrics-policy?hl=en): applicable additional analytics use requires accepting the policy amendment. TrendScout leaves YouTube keyword extraction and derived metrics disabled.
- [YouTube revision history](https://developers.google.com/youtube/data/revision_history): since July 2025, `mostPopular` reflects Trending Music, Movies and Gaming charts, so it is not used to claim broad consumer-product coverage.

## Quota, schedule and provider calls

| Internal pool | Application ceiling | Window convention | Current app-side reservation |
|---|---:|---|---:|
| `search.list` | 6 calls/day; 200 calls/UTC calendar month | Daily provider reset uses `America/Los_Angeles` with DST; internal monthly history is retained in UTC | 0 reservations made by this goal |
| Other methods, including `videos.list` | 6 units/day; 200 units/UTC calendar month | Same provider-day reset; separate UTC monthly ledger | 0 reservations made by this goal |

The internal 200/month limits are ceilings, not expected consumption. Owner-provided Console screenshots identify project `trendscout-demo-youtube` and show 100 daily `search.list` calls, 10,000 daily other-method units and 0 usage at screenshot time; they do not independently prove that `YOUTUBE_API_KEY` belongs to that project or that the figures remain current. Provider quota remains `UNKNOWN`. The Windows hostname is `QUOC`; the process environment still lacks a persistent `TREND_SCOUT_YOUTUBE_LIVE_HOST` and both YouTube approval flags remain false. The isolated browser process temporarily designated host `QUOC` while keeping both approvals false. Credential resolution reported only `YOUTUBE_API_KEY=PRESENT` and `SERPAPI_API_KEY=PRESENT`; no credential value or `.env` content was read or printed. The `.env` ACL was tightened because its prior permissions were broad. The user's durable YouTube quota ledger was not inspected; no ledger or dataset was reset or modified by this continuation.

**This goal sent 0 provider requests:** 0 `search.list`, 0 `videos.list`; there are no HTTP statuses, real video IDs or URLs to report, and no provider quota was spent by this goal. The two-request owner authorization is recorded but unused. No live request will be used as a probe for project/API enablement or policy compliance.

## Cache, SQLite and application evidence

The following local-app browser evidence predates owner policy approval; its policy-draft observations are historical and do not describe the current published static pages.

- The live source result is stored in the existing durable worker/checkpoint path and read through the Fetch status API. The SQLite reopen test reads back video ID/title from a separate temporary database and labels its record `SYNTHETIC`.
- Shared cache uses a YouTube-specific scope and grant. Retention cleanup only deletes the affected YouTube cache entry; a regression test confirms Google cache data remains. The ordinary collection/keyword table does not receive YouTube video titles as keywords; source snapshots retain bounded counts and provenance.
- No user database was reset, migrated by a live app run, or used for quota QA in this turn. Tests used isolated temporary SQLite state. Existing Google, Bing and Wikimedia data were not changed. No database row counts are claimed for this continuation.
- Browser QA used the actual FastAPI application and normal Google LIVE composition on `127.0.0.1:8123`, with temporary SQLite files and a transport guard that rejected every provider HTTP request. It did not use the standard shell launcher: that launcher performs a SerpApi account preflight at startup. The isolated settings kept Google, Bing, YouTube policy and YouTube LIVE approvals false, while the temporary child process designated host `QUOC`. The dashboard and Source Center loaded; Source Center showed YouTube, key `PRESENT`, host designated, policy draft, LIVE approval pending, provider quota unknown, and both local YouTube budgets at zero. Selecting YouTube changed only the isolated browser's source preference. Fetch was disabled; a same-origin YouTube Fetch POST returned `409 YOUTUBE_POLICY_NOT_APPROVED`, with no provider request. The Fetch agreement dialog was therefore inaccessible in the real app and its accept/cancel behavior remains verified only by the prior mock-browser run.
- The real app browser also loaded the draft Privacy Policy and Terms at `/dashboard/privacy.html` and `/dashboard/terms.html`; their relative links worked and external provider links used HTTPS. Vietnamese and English toggles worked, including the empty Wikimedia context after a small localization fix; light/dark toggles worked; at 390×844 there was no horizontal overflow. Keyboard Tab reached “Skip navigation”, and Enter moved focus to `<main>`. Run History loaded with no saved runs in the temporary store; Data Quality loaded with no evidence to display. The browser context and server were closed. The standard launcher is code-configured to enable Bing (`local_live_settings()` sets `bing_approved=True` and `account_api_approved=True`, covered by `test_google_local_launcher.py`); this isolated QA deliberately bypassed that launcher, so its disabled Bing state is not evidence that the standard product configuration disables Bing.
- The repository's standard `start.ps1` and `start.sh` launch the loopback demo; no deployed website URL was present in the repository or provided for QA. No live YouTube request, fresh YouTube result, or real YouTube Fetch/Run History/Data Quality record is claimed.
- Fetch API and dashboard source rendering use text nodes and validated YouTube watch URLs. LIVE source results remain separate from Google/Bing keyword records, and public Fetch output now strips unknown/raw video fields.

## Verification results

- Focused backend consent/API/capability tests after the final test-fixture update: **38 passed** (`test_d6_youtube_policy_consent`, `test_google_trends`, `test_d6_local_live_composition`, `test_d6_local_live_http`).
- Focused backend suites after the retention/deletion and Wikimedia repairs: **154 passed, 260 subtests passed**, including the YouTube adapter, consent, quota/scheduling, Google/Bing selection, deletion, retention and all 18 `test_d5_wikimedia_gateway` tests. The separate code-configured Bing launcher test passed **1/1**.
- Full Backend regression was run once: **904 passed, 16 failed, 1,039 subtests passed**. All 16 failures are in `emulator_tests/test_firestore_transactional.py`; they fail during setup because `FIRESTORE_EMULATOR_HOST` is unset. The suite was not rerun and no Firestore emulator was started. The four Wikimedia diagnostic failures from the previous run are fixed; the focused Wikimedia gateway suite passes 18/18.
- Frontend Node tests: **19 passed, 0 failed**.
- Focused Ruff over changed backend modules/tests: **passed**. Changed frontend modules passed Node syntax checks. `git diff --check`: **passed**.
- Real-app browser checks passed for the historical policy gate and accessible surfaces listed above. The current hosted API still disables Fetch, so its live agreement/consent flow cannot be exercised. Public static policy pages were deployed and verified; no provider request was made. The code change in this continuation has not yet been checked on the hosted API.

## G4 prerequisites recorded before owner publication (historical)

1. Sign into project `trendscout-demo-youtube`, verify YouTube Data API v3 is enabled, and check current `search.list`/other-method quotas. The owner screenshots are useful evidence, but the key/project relationship and current quota were not independently matched.
2. Persistently designate the one authorized LIVE host `QUOC`; the Windows process environment is currently not designated. Keep other hosts disabled.
3. The standard project/API-key registration process does not need to wait for the policy draft. Before any TrendScout YouTube feature is used, the owner must review the published Privacy Policy and Terms, verify operational retention/deletion facts, publish the final documents, then set `TREND_SCOUT_YOUTUBE_POLICY_APPROVED=true`. The in-app agreement flow is implemented. The local operator-only deletion workflow is implemented and tested; manual backups, external provider records and redirected console logs remain outside its reach.
4. Verify YouTube Data API v3 enablement and the actual project quotas in the correct Cloud project; designate one LIVE host and explicitly set `TREND_SCOUT_YOUTUBE_LIVE_APPROVED=true` plus `TREND_SCOUT_YOUTUBE_LIVE_HOST` there. The local API key value was not exposed.
5. At an eligible schedule slot, with cache unable to satisfy the check and no concurrent job, use the already granted authorization for at most one `search.list` request and, only if the result requires it, one `videos.list` request. Do not retry. Record real IDs/URLs, response status and quota reservation only after those preconditions pass.

No other owner quota approval is requested; the existing two-request authorization remains valid within the stated cap. No push, deployment, billing activation or D7 work is included.

## Handoff: completion, blockers and confirmations

### Completed in this continuation

- Added a separate policy-publication approval flag. The production API blocks YouTube Fetch until that flag is approved, and then requires the exact current policy version on Fetch submission and status polling.
- Added the bilingual, keyboard-accessible browser agreement dialog and Source Center publication/agreement statuses. Accepted policy version is stored in browser local storage; the key remains server-side.
- Allowlisted YouTube video metadata in public LIVE Fetch status and suppress invalid IDs/URLs, synthetic records, and unknown/raw fields. Capabilities report local LIVE verification only for fresh, non-empty provider-backed evidence.
- Added targeted YouTube checkpoint/cache expiry cleanup and a tested operator-only deletion workflow for the supported SQLite stores; unrelated source data and quota/schedule ledgers are preserved.
- Fixed the Wikimedia gateway diagnostic regression by passing the sanitized diagnostic through the superclass constructor; the full 18-test gateway suite now passes.
- Corrected the dashboard's no-data English localization and made the Skip navigation link focus the main region from the keyboard.
- Updated draft policy/terms wording, setup guidance, consent/API regression coverage and this report. No user database was reset or modified.

### Blockers recorded before owner publication (historical)

- The Privacy Policy and Terms remain draft. The owner must confirm the operational details (data retention, logs/backups and deletion handling), approve and publish them, then set `TREND_SCOUT_YOUTUBE_POLICY_APPROVED=true`. The standard Google project/API-key registration checklist does not require this publication; the API Client's user-facing YouTube features do.
- Sign into `trendscout-demo-youtube` and confirm YouTube Data API v3 enablement and current method quotas. Provider quota remains UNKNOWN until checked live.
- Designate one LIVE host and explicitly approve it with `TREND_SCOUT_YOUTUBE_LIVE_APPROVED=true` and `TREND_SCOUT_YOUTUBE_LIVE_HOST`. The existing API key value must stay private.
- Once those checks pass, use the already authorized maximum of two attempts (one `search.list` and, only if needed, one `videos.list`; no retries). No further quota authorization is requested now.

### Out-of-scope issues that delayed verification

- Full regression currently has 16 Firestore emulator setup failures because `FIRESTORE_EMULATOR_HOST` is unset. No emulator was started; the earlier four Wikimedia failures are repaired.
- The standard launcher performs a SerpApi account preflight. To avoid unrelated external requests, the real-app browser QA used a temporary isolated composition with paid-source approvals disabled and a provider egress guard. Its disabled Bing display is not the standard launcher configuration: the standard launcher enables Bing in code.

## Commits before policy publication (historical)

- Implementation: `ac78355` — `feat(d6): add YouTube source and quota governance`
- Wikimedia diagnostic repair: `cffdf7f` — `fix(wikimedia): preserve sanitized diagnostics`.
- YouTube retention, targeted deletion and runbook: `6dcbf19` — `feat(youtube): add targeted deletion workflow`.
- Draft policy pages, manual Pages workflow and browser accessibility/localization fixes: `3a4c9ac` — `docs(policy): prepare manual Pages publication`.
- This section predates policy publication and the `c5ab577` push.

## Current continuation update — 16 November 2026

- The owner approved public publication. Both policy URLs returned HTTP 200 and their served HTML matched the tracked pages byte-for-byte. The owner field matched `.env.youtube-audit-owner`; no values were printed. Public URLs are listed above.
- `Backend/app/sources/youtube/policy.py` now records approval for the exact published version. A new policy version fails closed until the approved version is updated. `GoogleLiveSettings` ignores the former local policy-approval variable. LIVE approval remains separate and fail-closed.
- The Vercel capability endpoint still reports `read_only=true`, `fetch_enabled=false`, and `source_selection_supported=false`; no sources are enabled, including Bing. The code change adds truthful YouTube policy status/version to the no-runtime response but does not enable public Fetch or provider traffic.
- G0 remains PASS at `c5ab57724cd2090b2b807d4c700a5908648957cf`; local HEAD equals `origin/main`. G4 remains BLOCKED (0%), G6 remains PARTIAL (6/12), and total progress remains 78%.
- No YouTube requests were sent. Provider usage remains 0 `search.list`, 0 `videos.list`. The previously authorized maximum remains one `search.list` plus, only if necessary, one `videos.list`, without retries.
- Current unresolved evidence: API-key/project association, current Cloud API/quota state, and a suitable hosted runtime with shared job/quota state. The Vercel deployment has no such runtime, so the existing hosted page cannot complete the Fetch/consent flow.
- Code and report changes in this continuation remain local until committed; the public static policy-page commit `c5ab577` is already pushed.
