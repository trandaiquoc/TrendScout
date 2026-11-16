# D6 YouTube LIVE Acceptance

**Reviewed:** 17 November 2026

**Status:** 84% complete. The Windows `QUOC` local LIVE application, approved policy consent, SQLite persistence, Source Center, Run History, and Data Quality were exercised. The attempted YouTube-only Fetch failed before provider HTTP because the owner-acceptance launcher admitted an out-of-schedule initial slot and the query planner rejected its timestamp. That bypass path has now been removed. No real YouTube response was obtained, so G4 is not passed and this report does not claim 100%. The next eligible refresh is 15:00 `Asia/Ho_Chi_Minh` on 17 November 2026. Vercel remains intentionally read-only.

## Gate status

| Gate | Weight | Status | Evidence |
|---|---:|---|---|
| G0 — Repository synchronization | 8% | PASS | `git fetch origin` found `origin/main` at `6def12b`; local commits `1d35603`, `de3bebb` and `23fbb9b` are ahead without rewriting or pushing. Untracked `.playwright-cli/` was preserved. |
| G1 — YouTube API contract | 12% | PASS (offline) | Fixed `search.list` and bounded `videos.list` request builders/parsers, source identity, validation, pagination binding and method-specific gateway routing are implemented and tested. No provider response was received. |
| G2 — Quota governance | 18% | PASS (offline) | Separate durable search and other-method pools, pre-request reservation, provider-day timezone handling and conservative failure accounting are implemented and tested. Actual Cloud project quota remains UNKNOWN. |
| G3 — Schedule, cache and Source Center | 12% | PASS | Local Source Center shows approved `d6-youtube-v2`, the actual refresh schedule, provider quota as UNKNOWN, and the next eligible time. Cache and schedule behavior remain covered by focused tests. |
| G4 — Genuine YouTube LIVE evidence | 16% | NOT PASSED — 0% | The accepted YouTube-only Fetch ended `PARTIAL`; its source is `FAILED` with `SOURCE_EXECUTION_FAILED`. The one-shot owner-acceptance path admitted an out-of-schedule initial attempt; query planning then rejected its timestamp before gateway dispatch. That bypass has been removed. Internal YouTube usage stayed at 0 and `last_provider_request_at` stayed null; no YouTube HTTP status, video IDs or provider response exist. The reservation was preserved. Next eligible: 08:00 UTC / 15:00 Vietnam time on 17 November. |
| G5 — SQLite and application flow | 16% | PASS | The real Windows run persisted its failed source outcome to SQLite; API readback, Source Center, Run History and Data Quality agree. Restarted FastAPI with the schedule-only code reports `initial_acceptance_available=false`, 08:00 UTC next eligibility, zero internal YouTube usage, and no provider request. The failed result has zero provider records and zero YouTube keyword rows. |
| G6 — Regression and browser QA | 12% | PASS | Current focused backend suites: 70 passed and 5 subtests passed; Frontend: 19 passed; focused Ruff and `git diff --check` passed. Windows browser checks covered consent, Vietnamese/English, light/dark, 390px layout, keyboard navigation, YouTube-only scope, Source Center, filtered Run History, details and Data Quality. The Firestore emulator waiver remains; its 16 setup failures were not rerun or claimed as passes. |
| G7 — Report and commits | 6% | PASS | Local code commits `1d35603`, `de3bebb` and `23fbb9b`, plus this report, are committed without pushing or deploying. |

**Weighted gate progress: 84%.** G4 contributes 0%; all other weighted gates pass. The goal remains incomplete until a genuine scheduled YouTube response is accepted. No provider retries, extra searches, resets, pushes, deployments or D7 work were performed.

> Sections below describing the 16 November preflight and earlier browser checks are retained as historical records. The “Current Windows QUOC LIVE acceptance” section at the end supersedes their statements about the current runtime, consent, host configuration, database, browser state and provider-call count.

## Public policy publication, verified 16 November 2026

The owner explicitly approved publication and confirmed that the owner details were in the ignored `.env.youtube-audit-owner` file. The final pages were generated from allowlisted owner fields, reviewed, committed and pushed as `c5ab577`. Both public URLs returned HTTP 200. Their deployed HTML matched the tracked files byte-for-byte; the privacy page carried policy version `d6-youtube-v2`, and the owner field matched the configured value. The comparison emitted only lengths/equality status; no `.env` values were printed.

- Privacy Policy: https://trend-scout-mu.vercel.app/dashboard/privacy.html
- Terms of Service: https://trend-scout-mu.vercel.app/dashboard/terms.html
- Dashboard: https://trend-scout-mu.vercel.app/dashboard/

The policy approval is now source-controlled against the exact approved version in `Backend/app/sources/youtube/policy.py`; a later version change will fail closed until separately approved. LIVE approval remains separate. Before commit `3b27393` deployed, the hosted API returned HTTP 200 with `policy_status` and `policy_version` absent. After deployment, it returned `policy_status=APPROVED`, `policy_version=d6-youtube-v2`, and `policy_agreement_required=true`. It still reports `environment=vercel`, `read_only=true`, `fetch_enabled=false`, and `source_selection_supported=false`; YouTube and Bing remain disabled.

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

The internal 200/month limits are ceilings, not expected consumption. Owner-provided Console screenshots identify project `trendscout-demo-youtube` and show 100 daily `search.list` calls, 10,000 daily other-method units and 0 usage at screenshot time; they do not independently prove that `YOUTUBE_API_KEY` belongs to that project or that the figures remain current. Provider quota remains `UNKNOWN`. The Windows hostname is `QUOC`. At the earlier Windows inspection, the process environment had no persistent `TREND_SCOUT_YOUTUBE_LIVE_HOST`, and YouTube LIVE approval was false. Policy publication approval is now version-controlled in code rather than supplied by a local environment flag. The isolated browser process temporarily designated host `QUOC` while keeping LIVE approval false. Credential resolution reported only `YOUTUBE_API_KEY=PRESENT` and `SERPAPI_API_KEY=PRESENT`; no credential value or `.env` content was read or printed. The `.env` ACL was tightened because its prior permissions were broad. The user's durable YouTube quota ledger was not inspected; no ledger or dataset was reset or modified by this continuation.

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
- Real-app browser checks passed for the historical policy gate and accessible surfaces listed above. The current hosted API still disables Fetch, so its live agreement/consent flow cannot be exercised. Public static policy pages and the deployed policy-status metadata were verified; no provider request was made.

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
- G0 remains PASS; `HEAD` and `origin/main` matched after code commit `3b27393`. G4 remains BLOCKED (0%), G6 remains PARTIAL (6/12), and total progress remains 78%.
- No YouTube requests were sent. Provider usage remains 0 `search.list`, 0 `videos.list`. The previously authorized maximum remains one `search.list` plus, only if necessary, one `videos.list`, without retries.
- Current unresolved evidence: API-key/project association and current Cloud API/quota state. Windows `QUOC` is the designated local runtime; Vercel remains read-only by design and is not the Fetch acceptance target.
- Implementation commit `3b27393` is pushed and its hosted API status was verified. Report commit `4aaf35c` records that deployment check. The public static policy pages remain deployed from `c5ab577`. No YouTube or Bing provider request was made.
- The owner directed that the legacy Firestore emulator tests be waived for this D6 acceptance. Their 16 setup failures were excluded from D6 scoring, left unchanged, and not claimed as passing. The suite's loopback/network isolation guard was not bypassed.
- Read-only Playwright QA of the hosted dashboard returned HTTP 200. The page showed the published policy and disabled Fetch. Source Center showed all four source checkboxes disabled; YouTube showed policy approved/versioned and agreement required, while the server key and LIVE host were not configured and provider quota remained unknown. The browser logged one favicon 404 and 403 responses for `/api/v1/collections` and `/api/v1/analytics/overview`.

## Windows QUOC LIVE preflight — 16 November 2026

- The owner clarified that D6 acceptance belongs on the Windows host `QUOC`, using the existing guarded local LIVE runtime and SQLite state. The hostname was verified. Vercel remains intentionally read-only; no hosted Fetch, policy republish, push or deploy was attempted.
- The backend credential resolver reports `YOUTUBE_API_KEY=PRESENT`; its value was never printed or written to this report. The required LIVE host/approval settings were absent from the process environment and were not set because the project/key and current quota gates remain unresolved.
- The pasted owner evidence identifies project `trendscout-demo-youtube`, confirms YouTube Data API v3 enablement, and reports `search.list` quota of 100 requests/day plus 10,000 units/day for other methods, with zero observed usage at screenshot time. That is owner-supplied evidence from that time, not a current quota reading. The project setting in the root `.env` does not match the owner-specified project; its configured value is intentionally omitted. This does not establish which project owns the key.
- No `gcloud` CLI or Application Default Credentials file is available on `QUOC`; the owner metadata file contains policy details but no Cloud login credential. The computer-use bridge could not connect to the Windows browser. Google documents `keys.lookupKey` as the way to find a key's parent project and requires OAuth plus `apikeys.keys.lookup` permission ([API Keys `lookupKey` reference](https://docs.cloud.google.com/api-keys/docs/reference/rest/v2/keys/lookupKey), accessed 16 November 2026). No Cloud API request or YouTube API request was made as a probe.
- Read-only inspection of the existing SQLite state found no YouTube schedule reservation, cache entry, checkpoint or internal YouTube quota reservation. The next scheduled opportunity was 23:00 `Asia/Ho_Chi_Minh`; no slot was reserved. Three snapshot blobs were already past their retention deadline. The normal runtime purges expired blobs at startup, so startup was withheld and those rows were left untouched.
- No local server or browser acceptance run was started. This continuation made zero YouTube provider calls and consumed zero YouTube quota. Current provider usage remains unknown; the owner screenshot's earlier zero does not establish current usage. D6 remains at 78%: G4 0%, G6 6/12. The authorized ceiling remains at most one `search.list` request and, only if needed, one `videos.list` request, with no retries.
- Required evidence for the next safe attempt: an authenticated Cloud Console session or sanitized official lookup result showing the API key's parent project matches the intended project, plus a current view of YouTube Data API v3 enablement and method quota/usage. Do not include or transmit the key string in evidence. After that, resolve the project-setting mismatch, set the host/approval only for the current process, and run acceptance on `QUOC` at an eligible slot.

## Current Windows QUOC LIVE acceptance — 17 November 2026

### Runtime and approval

- The actual Windows hostname was `QUOC`. The local FastAPI runtime resolved `YOUTUBE_API_KEY` as present without reading or printing the value. The YouTube adapter uses the API key independently of `GOOGLE_CLOUD_PROJECT`; this does not verify which Cloud project owns the key.
- The code-approved and published YouTube policy version is `d6-youtube-v2`. The local Source Center showed the policy as approved and published, and the actual Fetch consent dialog required that version and recorded the accepted agreement before the Fetch submission.
- The accepted Fetch body was exactly `{"sources":["youtube_video_search"]}`. No Google, Bing, Wikimedia or other source was selected for this request.

### One attempted application Fetch; no provider request

- The local API accepted Fetch ID `6588d73a0b77343cccd24d70c7aba103` with HTTP 202. Its terminal result was `PARTIAL`; `youtube_video_search` was `FAILED` with `SOURCE_EXECUTION_FAILED` and zero records.
- The local cause is the initial-acceptance planner mismatch: the owner-acceptance launcher admitted and reserved an `INITIAL_ACCEPTANCE` at 00:41:25 on 17 November Vietnam time, but the query planner accepted only 07:00, 15:00 or 23:00 schedule slots. The handler failed before gateway dispatch. This was an application error, not a YouTube provider error; there is no provider HTTP status or real video ID to report. The owner-acceptance flag no longer bypasses schedule eligibility.
- Evidence that no provider request was made: the YouTube internal quota snapshot remains 0/6 daily and 0/200 monthly for both methods, `last_provider_request_at` is null, the durable provider quota ledger has no YouTube usage keys, and the failed snapshot reports `provider_record_count=0`. Neither `search.list` nor `videos.list` reached the provider. No retry or second Fetch was sent.
- The initial-acceptance schedule reservation was preserved rather than reset. Its recorded attempt is `2026-11-16T17:41:25Z` (00:41:25 Vietnam time). The capability API and Source Center report the next eligible window as **2026-11-17 08:00 UTC / 15:00 Asia/Ho_Chi_Minh** after the eight-hour minimum spacing. The 07:00 slot is not eligible.
- Internal quota is application policy only. Provider quota remains `UNKNOWN`; the owner-supplied Cloud screenshot is historical and does not verify current quota or API-key ownership. A successful API response would prove service access, not project ownership.

### SQLite, UI and browser evidence

- The existing durable state was opened without reset or deletion. Its migration audit records the additive D6 YouTube policy change and confirms prior jobs, reservations and quota counters were preserved. The prior SerpApi and RSS usage remains intact.
- The live collection SQLite row records the Fetch job as processing `COMPLETED`, while its dataset outcome is `FAILED`; the source manifest preserves `SOURCE_EXECUTION_FAILED`, and there are zero YouTube LIVE keyword rows. Run History now exposes the dataset `outcome_status` separately from internal processing status, so this row displays `Thất bại` instead of a false success. The Vietnamese status filter sends the stable `FAILED` code and returns the YouTube row.
- Source Center displays `d6-youtube-v2`, the failed source, the schedule and 15:00 next eligibility. Data Quality displays the LIVE failure, zero received/saved/rejected and one quality note. Run History details show the same failed YouTube source and zero provider records.
- Windows browser QA also verified Vietnamese/English rendering, light/dark theme, 390×844 with no horizontal overflow, keyboard skip-navigation focus, consent version and YouTube-only selection. One non-blocking favicon request returned 404.

### Code and verification in this continuation

- Local code commits: `de3bebb` (`fix(d6): preserve live acceptance and report outcomes`) adds the narrowly scoped legacy ledger migration with quota/history preservation and separates Fetch outcome from processing completion in Run History; the browser filter uses stable status codes in both locales. Commit `23fbb9b` (`fix(youtube): enforce scheduled live fetch windows`) removes the out-of-schedule YouTube initial-acceptance path. The owner-acceptance launcher now only limits the query count to one and preserves expired blobs; normal schedule and approval gates still apply.
- Current focused backend regressions: **70 passed, 5 subtests passed**. Frontend Node tests: **19 passed, 0 failed**. Focused Ruff and `git diff --check` passed. The Firestore emulator suite was not run under the explicit D6 waiver; the earlier 16 setup failures are not represented as passes. Previously passing Wikimedia 18/18 results were preserved and not rerun.
- The normal next step is a user-triggered YouTube-only Fetch at or after the displayed eligible window, with consent and all backend gates still active. Do not reset the schedule or quota ledger to make it earlier. No `videos.list` request is needed unless a later successful `search.list` result proves otherwise. This acceptance remains at **84%**, not 100%, until genuine provider-backed evidence and the corresponding cache-only readback are recorded.
- No Git push, production deployment, state reset, data deletion, automatic Fetch, or D7 work occurred.
