# D6 YouTube LIVE Acceptance

**Reviewed:** 17 November 2026

**Status:** 100% complete for the authorized D6 final handoff. The owner confirmed that the former Linux host `QUOC` was shut down and converted to Windows 2, that no Linux LIVE runtime remains, and that `OWNER_MANUAL_LIVE` stopped after Linux acceptance. The current verified Windows hostname is `DESKTOP-ICECOET`. The prior Linux SQLite/quota ledger was not found in the inspected Windows paths, so historical provider and cache evidence remains recorded in this report. Provider quota remains `UNKNOWN`; empty Windows state is not treated as zero historical usage.

## Current final handoff gate status

| Gate | Weight | Status | Evidence |
|---|---:|---|---|
| G0 — Repository synchronization | 8% | PASS | `git fetch origin` completed with the pre-change `HEAD` equal to `origin/main` at `b8c289b97f9a5d2fd67c43eee3a298d4f04c84a7`. The Windows code/test commit and report commit are local; no push occurred. |
| G1 — YouTube API contract | 12% | PASS | The focused YouTube suite passed 52 tests on Windows. The original Linux acceptance still documents two genuine bounded `search.list` responses, canonical video records and provider timestamps. No provider call was repeated for this handoff. |
| G2 — Quota governance | 18% | PASS | The owner confirmed that the former Linux runtime is stopped and the accepted D6 runtime has moved to Windows 2. A local process scan found no matching TrendScout/Python/Uvicorn worker. The `OWNER_MANUAL_LIVE` path remains Linux-only and its Windows rejection test passed. Provider quota remains `UNKNOWN`, and the old usage ledger is not available in the inspected Windows state. |
| G3 — Schedule, cache and Source Center | 12% | PASS | Windows code and focused tests confirm the default YouTube query mode remains `NORMAL_DEMO`, with schedule and cache guards intact. The app was not launched; no startup Fetch or provider traffic occurred. |
| G4 — Genuine YouTube LIVE evidence | 16% | PASS | The retained Linux evidence records two genuine `search.list` requests, 50 accepted provider records, 47 stored records, 3 size-cap omissions and two `LIVE/CACHED` deliveries including after restart. This evidence was preserved without another provider request. |
| G5 — SQLite and application flow | 16% | PASS | The prior Linux SQLite/application evidence remains documented. Current Windows Google, Wikimedia and Collections databases pass `integrity_check`; their contents do not include the old Linux YouTube records or quota history. This absence is recorded without fabricating a migration. |
| G6 — Regression and browser QA | 12% | PASS | Focused backend regressions: 150 passed, 3 Linux-only tests skipped; frontend Node tests: 20 passed; all 14 public JavaScript files passed syntax checks; five offline Chrome E2E suites passed. The Firestore emulator waiver remains in force and is not counted as a pass. |
| G7 — Report and commits | 6% | PASS | The code/test fix and this report update are separate local commits. No push, deployment, state reset, data deletion, paid service change or D7 work occurred. |

**Weighted handoff progress: 100%.** G2 completion relies on the owner's confirmation that the Linux runtime stopped and the accepted D6 handoff moved to Windows 2. Provider quota remains unknown, and the prior Linux state is preserved here as report evidence rather than represented as migrated Windows data.

## Historical gate status — prior Linux-session snapshot

| Gate | Weight | Status | Evidence |
|---|---:|---|---|
| G0 — Repository synchronization | 8% | PASS | `git fetch origin` established the requested baseline; `origin/main` remains `2c3ec8a`. The clean local branch is two focused commits ahead (implementation and report); no reset, rewrite or push occurred. |
| G1 — YouTube API contract | 12% | PASS | Focused offline contract tests pass, and two genuine `search.list` responses were parsed with real IDs, canonical URLs, bounded results and provider timestamps. No `videos.list` call was needed. |
| G2 — Quota governance | 18% | PARTIAL (9/18 awarded) | Durable method-level accounting recorded both physical requests and remained intact. Manual mode skipped only the normal internal ceilings, with concurrency and retry controls preserved. Provider quota remains UNKNOWN, and this Linux session could not verify Windows `QUOC` inactivity or cross-machine exclusivity. |
| G3 — Schedule, cache and Source Center | 12% | PASS | NORMAL_DEMO schedule and defaults are preserved. Manual Fetch bypasses schedule spacing only when explicitly activated. Fresh/cache behavior and Source Center mode, host, quota and policy status were verified. |
| G4 — Genuine YouTube LIVE evidence | 16% | PASS | Two successful provider `search.list` calls were physically made. The corrected product-focused request returned 50 accepted records, 47 stored records and 3 documented storage-cap omissions as LIVE/FRESH with US/en-US request parameters. Two later Fetches, including one after restart, returned LIVE/CACHED without new provider requests. |
| G5 — SQLite and application flow | 16% | PASS | Four user-triggered Fetch manifests were persisted. Both SQLite databases pass integrity checks. Source Center, Run History and Data Quality show matching source counts, partial status, timestamps, cache delivery and extraction-disabled evidence. |
| G6 — Regression and browser QA | 12% | PASS | Focused backend tests: 185 passed; frontend tests: 20 passed; Ruff, JavaScript syntax checks and `git diff --check` passed. Linux browser checks covered consent, language, themes, mobile layout, keyboard navigation, Source Center, Run History and Data Quality. The Firestore waiver remains in force; its 16 setup failures are not claimed as passes. |
| G7 — Report and commits | 6% | PASS | The implementation and this acceptance update are separate focused local commits. No push, deployment, reset, billing change or D7 work occurred. |

**Weighted gate progress: 91%.** G2 contributes 9/18 because the single-host requirement cannot be fully verified without access to Windows `QUOC`; all other weighted gates pass. G4 is complete with genuine provider evidence. The goal remains incomplete until Windows inactivity is independently established. The local manual process has been stopped, and actual usage history is preserved.

> The detailed sections below preserve earlier Linux and Windows sessions as historical evidence. The latest dated section at the end of this report is the current Windows 2 handoff snapshot; it supersedes earlier claims about the current host, app/database state and overall progress. The genuine Linux provider and cache records remain the evidence for the accepted provider calls.

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

## Linux D6 acceptance update — 17 November 2026

### Host, mode and authorization

- The actual Linux hostname was `trandaiquoc`. The credential resolver reported `YOUTUBE_API_KEY=PRESENT`; the secret value was never read into output, logs, screenshots, tests or this report. No gcloud, ADC or OAuth credential was used.
- Linux was designated as the sole authorized LIVE host for this acceptance. The explicit process flag `--youtube-owner-manual-live` enabled the development mode for the local FastAPI runtime. The mode is Linux-only, process-local, user-triggered and unavailable on Vercel or through a public bypass endpoint. Standard `bash start.sh` still selects NORMAL_DEMO by default.
- Windows `QUOC` was not accessible for process inspection from this Linux session, and the browser computer-use bridge was unavailable. Therefore its current YouTube LIVE state is **unknown**. This report does not claim Windows stopped, and Git does not provide a cross-machine quota lock. Manual mode is disabled locally and the authorized Linux server was stopped after acceptance; do not re-enable a second host until Windows inactivity is established.
- The previous failure root cause was that the Windows owner-acceptance launcher admitted an out-of-schedule attempt while the query planner rejected its scheduled timestamp before dispatch. The new design separates query selection from schedule eligibility: NORMAL_DEMO keeps the 07:00, 15:00 and 23:00 Vietnam schedule, eight-hour spacing, cache-first behavior and 6/day plus 200/month internal budgets; OWNER_MANUAL_LIVE selects a registered product query using actual time and bypasses only those internal schedule and budget gates.
- Manual mode retains durable request accounting, provider error reporting, user consent, host checks, credential handling, concurrency limits and disabled automatic retries. Each request remained user-triggered and bounded to one query. The one-shot fresh-cache bypass required the explicit `--youtube-owner-force-fresh-test` action. SerpApi account/quota preflight was skipped in this mode; only YouTube was selected. No SerpApi provider traffic occurred.
- The real browser recorded consent to published policy version `d6-youtube-v2`. The accepted request selected only `youtube_video_search`; no Google, Bing, Wikimedia or other provider was selected.

### Genuine provider calls and cache acceptance

- There were **2 physical YouTube API requests**: 2 `search.list`, 0 `videos.list`, with no retries and no provider errors. The first request was Fetch `82b8c8a02fb5bb29eb135b16f7fc6826` at `2026-11-17T02:11:10.762693Z`, query `business finance companies markets trends`, taxonomy scope `business_finance`. It succeeded but was outside the intended first-priority manual product scopes; this exposed and prompted the planner correction. Fifty provider results were accepted, 47 stored, and 3 omitted by the existing storage-size limit.
- The corrected fresh request was Fetch `43861c07756a8deae5614ea108b38fc7` at `2026-11-17T02:12:40.772968Z`, query `beauty skincare cosmetics haircare trends`, scope `beauty_personal_care`, market filter `US`, language filter `en-US`. It returned 50 records from `search.list`; the application stored 47 and reported 3 storage-cap omissions. Dataset outcome is `PARTIAL` because evidence is bounded and keyword extraction is disabled, with no provider error.
- Example retained provider records include `5sYq09Wc8sM` ([canonical video URL](https://www.youtube.com/watch?v=5sYq09Wc8sM), published `2026-11-13T12:30:14Z`), `sU-LcLySvPY`, and `U0C2jSct2Lo`. Their source observation time is `2026-11-17T02:12:41.397682Z`. A valid response verifies that this API key can access `search.list`; it does not establish the key parent project or current Cloud quota.
- A same-query Fetch `62a2dbc2a335c424218cc805cdff6ea3` returned `LIVE/CACHED`, 47 records, with original observation time preserved and cache delivery at `2026-11-17T02:13:03.784194Z`. After stopping and restarting the backend in explicit manual mode without force-fresh, Fetch `8ac46fecd350b4a5c94b0dafbae0c9b3` also returned `LIVE/CACHED` with the same 47 records, original observation time and delivery time `2026-11-17T02:16:52.963245Z`. Neither cache delivery made a provider request; the IDs matched and no duplicate provider evidence was introduced.
- The durable ledger recorded two `search.list` requests in its provider-day bucket `youtube:day:2026-11-16` and two in `youtube:month:2026-11`, with zero other-method usage. NORMAL_DEMO ceilings remain 6/day and 200/month; manual mode did not erase or reset history. Provider quota remains `UNKNOWN`. The owner-observed Cloud limits and historical screenshot are not a current remaining-balance reading. Official YouTube documentation says `regionCode=US` limits results to videos viewable in the United States, while language is a relevance preference; these parameters do not prove creator or viewer residence ([search.list reference](https://developers.google.com/youtube/v3/docs/search/list), [quota calculator](https://developers.google.com/youtube/v3/determine_quota_cost), checked 17 November 2026).

### Persistence, application evidence and verification

- SQLite integrity checks returned `ok` for both `normal-google-live-v1/state.sqlite` and `collections-v1/collections.sqlite`. Four Fetch manifests remain persisted: two fresh and two cached. The post-restart cached Fetch preserved the original source observation time and the later cache delivery time. No database was reset, deleted or synchronized between Windows and Linux.
- Source Center showed `OWNER_MANUAL_LIVE`, host designation and local approval, policy `d6-youtube-v2`, provider quota `UNKNOWN`, and local ledger usage of two search requests. Run History showed the two real fresh attempts and two cached deliveries. Its details distinguish 50 provider records, 47 stored source records and 3 size-limit omissions, and show the query, taxonomy, US/en-US filters, method, original timestamp and cache delivery timestamp.
- Data Quality reports the source observation time, 47 stored records, 3 storage omissions, partial outcome and zero retained keywords. YouTube remains evidence-only; keyword extraction, scoring and derived metrics remain disabled. Do not treat these search results as search volume, purchasing intent, a validated trend, or proof of US creator/audience location.
- Real Linux browser QA covered dashboard, Source Center, YouTube-only selection, cancellation before submission, acceptance and persisted policy version, LIVE/FRESH, LIVE/CACHED, Run History, Data Quality, local quota metrics and provider quota UNKNOWN. Vietnamese/English, light/dark, keyboard skip-navigation and 390×844 mobile layout were checked; the mobile page had no horizontal overflow. The actual Fetch used the accepted agreement.
- Focused validation: **185 backend tests passed** across planner, admission/quota, worker/cache, launcher, composition, local LIVE HTTP, policy consent, source adapters and provider allocation contracts; **20 frontend Node tests passed**. Ruff, JavaScript syntax checks and `git diff --check` passed. NORMAL_DEMO scheduling/default and the explicit manual fresh behavior are covered by tests. The Google/Bing and Wikimedia focused regressions passed; Wikimedia was 18/18. The Firestore emulator suite was not run under the existing explicit waiver, so its 16 setup failures are not counted as passes. No full repository suite is claimed.
- The focused implementation commit is `3be11d7e4a20cb4c3154224b5818351f3ab7edfe` (`fix(youtube): add owner manual live mode`). The report is committed separately. No push, deploy, policy republish, billing change, state reset, or D7 work occurred.

## Windows 2 final handoff update — 17 November 2026

### Host, ownership and runtime state

- `socket.gethostname()` on the current Windows machine returned `DESKTOP-ICECOET`. The owner confirmed that the former Linux host `QUOC` was powered down and converted to Windows 2, that no Linux LIVE runtime remains, and that Linux `OWNER_MANUAL_LIVE` stopped after the original acceptance. No remote inspection of the former host was attempted; the cross-host status here is based on that owner confirmation.
- The local process scan found no matching TrendScout, Python or Uvicorn process. Before any launch, process-scope `TREND_SCOUT_YOUTUBE_LIVE_APPROVED` and `TREND_SCOUT_YOUTUBE_LIVE_HOST` were absent. `start.ps1` derives the current Windows hostname and scopes those values to the explicitly launched process, restoring the prior values when it exits. This handoff did not invoke `start.ps1` or start a server.
- The default YouTube query mode remains `NORMAL_DEMO` with its scheduled/cache controls. `OWNER_MANUAL_LIVE` remains Linux-only; the Windows launcher guard rejects that mode. No automatic YouTube startup Fetch occurred, and no YouTube or SerpApi provider request was made during this handoff.
- Vercel was left read-only. No deployment, push, database migration, reset, deletion or D7 work was performed.

### Credentials and local data inspection

- The credential resolver reported only `YOUTUBE_API_KEY=PRESENT` and `SERPAPI_API_KEY=PRESENT`. Credential values and `.env` contents were never printed or added to this report. Provider quota remains `UNKNOWN`.
- The Windows state directory contains `normal-google-live-v1/state.sqlite` (20 KiB; integrity `ok`; five `atomic_state` rows, including an empty `normal-youtube-search-schedule-v1` object; zero snapshot blobs) and `normal-wikimedia-live-v1/state.sqlite` (20 KiB; integrity `ok`; one empty state object; zero snapshot blobs). The schedule object contains no old quota usage or accepted YouTube evidence.
- `%LOCALAPPDATA%\TrendScout\collections-v1\collections.sqlite` is 167,936 bytes and passes `integrity_check`. It has 18 tables; only `settings` has a row. `fetch_jobs`, `dataset_snapshots`, `keywords` and the other data tables are empty. The known `local\d6-smoke` and user-profile `collections-v1` paths were absent.
- No prior Linux YouTube database/quota ledger or matching `.bak`/`.backup` file was found in the inspected user state, `%LOCALAPPDATA%\TrendScout` or repository/document roots. This is limited to those inspected locations; the old Linux records remain supported by the historical report sections above. The empty Windows state is not evidence that historical provider usage was zero.
- Ordinary read-only SQLite opens caused SQLite to leave zero-byte `-wal` and 32 KiB `-shm` sidecars beside the Google and Collections databases. Later immutable read-only checks left them unchanged; they remain in place. No SQL writes, migrations, resets or deletions were run.

### Windows verification and local code change

- Backend focused tests: **150 passed, 3 skipped** (the skips are Linux-only `OWNER_MANUAL_LIVE` cases). This included 52 YouTube tests, the Windows launcher guard, credential/policy checks, local LIVE composition and routes, Fetch, Google, workspace UI, source catalog and read-only Vercel checks. `pip check` reported no broken requirements.
- Frontend Node tests: **20 passed**. All **14** `Frontend/public/**/*.js` files passed `node --check`.
- Five offline Chrome suites passed: workspace at 320/390/768/1024/1440/1920 px, keyboard accessibility, pipeline, public Fetch and distributed synthetic Fetch. The distributed Fetch-to-keyword-detail flow was rerun after the final assertion cleanup. These suites used synthetic/offline data and made no provider calls.
- Firestore emulator tests were not run under the explicit waiver and are not counted as passing; no full backend suite is claimed. The local app was not started, because the handoff did not need a runtime launch or provider preflight.
- Code/test changes are committed locally as `99353827b5b01ac04bbf419826442a24bd603014` (`fix(d6): align Windows handoff checks and evidence details`). The public Fetch candidate-detail action now opens without a persisted run ID, and stale policy/launcher/browser assertions now match the published `d6-youtube-v2` policy and current Windows behavior. The report is committed separately after this update. No push was made.
