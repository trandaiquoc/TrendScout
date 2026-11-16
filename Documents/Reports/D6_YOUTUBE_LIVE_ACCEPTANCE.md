# D6 YouTube LIVE Acceptance

**Reviewed:** 16 November 2026

**Status:** offline implementation complete; genuine LIVE acceptance remains blocked by owner-side policy publication, project quota/API evidence, and host designation. No YouTube request was sent.

## Gate status

| Gate | Weight | Status | Evidence |
|---|---:|---|---|
| G0 — Repository synchronization | 8% | PASS | At this continuation, `git fetch origin` completed and `HEAD` equals `origin/main` at `dcb4b59d7e4e31a7b72d33e7d79409f0d9163a59`; no reset or overwrite was used. |
| G1 — YouTube API contract | 12% | PASS (offline) | Fixed `search.list` and bounded `videos.list` request builders/parsers, source identity, validation, pagination binding and method-specific gateway routing are implemented and tested. No provider response was received. |
| G2 — Quota governance | 18% | PASS (offline) | Separate durable search and other-method pools, pre-request reservation, provider-day timezone handling and conservative failure accounting are implemented and tested. Actual Cloud project quota remains UNKNOWN. |
| G3 — Schedule, cache and Source Center | 12% | PASS (offline) | Manual schedule eligibility, cache-first/stale delivery, focused expiry, and Vietnamese/English Source Center budget and status fields are implemented and tested. |
| G4 — Genuine YouTube LIVE evidence | 16% | BLOCKED — 0% | The owner authorized up to two requests total (at most one `search.list` and one `videos.list`, no retries); neither was used. The policy pages remain draft, Google Cloud API/quota evidence and the designated host are unverified. The in-app versioned agreement flow is implemented and mock-browser verified, but is correctly disabled until the owner approves and publishes the policy. |
| G5 — SQLite and application flow | 16% | PASS (offline) | Synthetic-only tests prove Fetch result persistence, API readback after SQLite reopen, source-selection combinations, cache-only delivery and exclusion from keyword scoring. No synthetic record is labeled LIVE. |
| G6 — Regression and browser QA | 12% | PARTIAL | Frontend 19/19, focused D6 backend 38/38, changed-file Ruff, JS syntax and diff checks pass. The full Backend suite ran 900 tests with 4 unrelated Wikimedia gateway errors and 2 skipped; local mock-browser review passed without provider traffic. |
| G7 — Report and local commits | 6% | PASS | This report and the current consent-gate changes are included in a focused local commit. No push, deployment or billing change was made. |

**Weighted gate progress: 72%.** G4 contributes 0% and G6 is partial. The goal is blocked and not 100% complete.

## Repository and implementation

The implementation extends the existing local LIVE worker and keeps YouTube-specific behavior at the source boundary:

- `Backend/app/sources/youtube/` validates and normalizes YouTube search and video-list responses. `search.list` uses a fixed server-owned query, `type=video`, `part=snippet`, `regionCode=US`, `relevanceLanguage=en`, `order=relevance`, one page and at most 50 results.
- A deterministic, versioned planner selects no more than two curated scopes in an eligible slot. It prioritizes fashion, home/living, beauty, lifestyle and consumer technology, then rotates through the remaining registered scopes. A returned video is deduplicated by provider/video ID while retaining query evidence.
- `videos.list` support is bounded to `part=snippet`; it can request metadata by IDs or a `mostPopular` chart. It does not request statistics. The normal Fetch path does not make a second metadata call because `search.list` already returns the fields currently displayed. The `mostPopular` chart is not used as a general consumer-trend feed: YouTube says it now covers Trending Music, Movies and Gaming charts.
- `LivePageGateway` injects the key through `X-Goog-Api-Key`, not the URL, and reserves `search.list` and `videos.list` against separate internal pools before dispatch. Retries are disabled for the configured YouTube source limits.
- The user-triggered refresh schedule is 07:00, 15:00 and 23:00 `Asia/Ho_Chi_Minh`, with a ten-minute grace period, an eight-hour minimum interval and no missed-slot catch-up. At most two search requests are allowed in a scheduled batch.
- Fresh cache is returned without provider traffic. Eligible stale refresh failures preserve stale evidence and its original timestamps. The YouTube cache has an eight-hour freshness window and seven-day retention ceiling. Startup cleanup and an hourly local-LIVE sweeper remove expired YouTube cache entries only; if the app is stopped past expiry, cleanup runs at the next startup.
- Fetch results retain YouTube videos as attributed source evidence. LIVE YouTube records are excluded from keyword extraction, collection scoring and composite metrics. Search position is not search volume or proof of national demand.
- Source Center exposes approval/configuration/host state, two separate local quota buckets, UNKNOWN provider quota, request and observation timestamps, cache status, next eligible time, errors and accepted record count. Vietnamese and English strings are present. The Fetch view only renders allowlisted YouTube watch links and text-safe metadata.

The original source/quota implementation is in `ac78355` (`feat(d6): add YouTube source and quota governance`). This continuation adds an independent policy-publication flag, blocks YouTube Fetch until the current policy is approved and the user's current version is accepted, and allowlists video fields returned to the public Fetch status API. It also requires the normal local LIVE composition for non-synthetic YouTube and distinguishes fresh provider evidence from cached/synthetic evidence in capabilities.

## API and policy references checked

Documentation was checked on 16 November 2026:

- [`search.list` reference](https://developers.google.com/youtube/data/reference/rest/v3/search/list): `maxResults` supports up to 50; `regionCode=US` restricts results to videos viewable in that region, and `relevanceLanguage=en` is a relevance preference. Neither establishes the residence of creators or viewers. The method has a separate documented daily bucket and each page request costs quota.
- [`videos.list` reference](https://developers.google.com/youtube/data/reference/rest/v3/videos/list): `part=snippet` exposes title/channel metadata; a call costs one unit; `maxResults` is not supported with the `id` filter. Statistics are deliberately not requested.
- [Quota calculator](https://developers.google.com/youtube/data/determine_quota_cost?hl=en), updated 12 November 2026: documented defaults are 100 `search.list` calls/day in its separate bucket and 10,000 units/day for other methods; requests including invalid ones cost at least one point, and daily quota resets at midnight Pacific Time. These are documentation defaults, **not verified project limits**.
- [YouTube Developer Policies](https://developers.google.com/youtube/terms/developer-policies): an API client must require privacy-policy agreement before users can use its features; applicable API data must be deleted or refreshed within 30 calendar days. The current privacy/terms pages remain draft; the in-app agreement flow is implemented and mock-browser verified, while the deletion-request process is not yet implemented or verified.
- [Getting started](https://developers.google.com/youtube/v3/getting-started?authuser=01) and [API key authentication](https://developers.google.com/youtube/data/guides/authentication): the standard project/API-key setup steps do not list publication of a privacy policy as a registration prerequisite. The [YouTube API Services Terms](https://developers.google.com/youtube/terms/api-services-terms-of-service) and Developer Policies do require a published privacy policy before the API Client provides API-backed features to users; Google may request additional information during review. **Registration can proceed without this policy; using YouTube through TrendScout cannot.**
- [Additional derived-metrics policy](https://developers.google.com/youtube/terms/derived-metrics-policy?hl=en): applicable additional analytics use requires accepting the policy amendment. TrendScout leaves YouTube keyword extraction and derived metrics disabled.
- [YouTube revision history](https://developers.google.com/youtube/data/revision_history): since July 2025, `mostPopular` reflects Trending Music, Movies and Gaming charts, so it is not used to claim broad consumer-product coverage.

## Quota, schedule and provider calls

| Internal pool | Application ceiling | Window convention | Current app-side reservation |
|---|---:|---|---:|
| `search.list` | 6 calls/day; 200 calls/UTC calendar month | Daily provider reset uses `America/Los_Angeles` with DST; internal monthly history is retained in UTC | 0 reservations made by this goal |
| Other methods, including `videos.list` | 6 units/day; 200 units/UTC calendar month | Same provider-day reset; separate UTC monthly ledger | 0 reservations made by this goal |

The internal 200/month limits are ceilings, not expected consumption. Provider quota is `UNKNOWN`; the project-specific quota page was not accessible because the connected Cloud Console browser redirected to Google sign-in. The API key's value was never read or printed. The prior local credential-status check reported only `PRESENT`; the observed `TREND_SCOUT_YOUTUBE_LIVE_APPROVED` flag was false and no designated live host matched the process. No valid project/API-enable evidence was available. The user's durable YouTube quota ledger was not inspected; no ledger or dataset was reset or modified by this continuation.

**This goal sent 0 provider requests:** 0 `search.list`, 0 `videos.list`; there are no HTTP statuses, real video IDs or URLs to report, and no provider quota was spent by this goal. The two-request owner authorization is recorded but unused. No live request will be used as a probe for project/API enablement or policy compliance.

## Cache, SQLite and application evidence

- The live source result is stored in the existing durable worker/checkpoint path and read through the Fetch status API. The SQLite reopen test reads back video ID/title from a separate temporary database and labels its record `SYNTHETIC`.
- Shared cache uses a YouTube-specific scope and grant. Retention cleanup only deletes the affected YouTube cache entry; a regression test confirms Google cache data remains. The ordinary collection/keyword table does not receive YouTube video titles as keywords; source snapshots retain bounded counts and provenance.
- No user database was reset, migrated by a live app run, or used for quota QA in this turn. Tests used isolated temporary SQLite state. Existing Google, Bing and Wikimedia data were not changed. No database row counts are claimed for this continuation.
- Browser QA used an isolated local mock on `127.0.0.1:8765`, not the application backend: DRAFT disabled YouTube Fetch; APPROVED opened the accessible policy dialog; cancel produced zero POSTs; accepting with keyboard sent the expected version header only to the mock. Vietnamese/English Source Center status and a 390×844 viewport were checked with no horizontal overflow. All browser requests stayed on the local mock; there was no `/youtube/v3` traffic. The mock server and isolated tab were closed after QA.
- `start.sh` was not run. Its LIVE composition performs unrelated Google/Bing/SerpApi account preflight, so running it for this consent-only browser check could make external requests outside the two YouTube-call authorization. No real application Fetch, fresh YouTube result, Data Quality video record, or Run History video result is claimed.
- Fetch API and dashboard source rendering use text nodes and validated YouTube watch URLs. LIVE source results remain separate from Google/Bing keyword records, and public Fetch output now strips unknown/raw video fields.

## Verification results

- Focused backend consent/API/capability tests after the final test-fixture update: **38 passed** (`test_d6_youtube_policy_consent`, `test_google_trends`, `test_d6_local_live_composition`, `test_d6_local_live_http`).
- Full Backend regression: **900 tests run, 4 errors, 2 skipped**. The four errors are in unchanged `test_d5_wikimedia_gateway` cases and all fail because the expected sanitized diagnostic is `None`: `test_body_read_error_after_200_is_sanitized_and_remains_reserved`, `test_complete_invalid_json_is_malformed_not_transport_error`, `test_dispatch_and_content_decode_failures_have_distinct_sanitized_details`, and `test_remote_protocol_and_timeout_are_distinguished_from_json_errors`. This leaves G6 partial; no Wikimedia code was changed.
- Frontend Node tests: **19 passed, 0 failed**.
- Focused Ruff over changed backend modules/tests: **passed**. Changed frontend modules passed Node syntax checks. `git diff --check`: **passed**.
- Mock-browser checks passed for DRAFT blocking, accessible consent controls, cancel/no-submit behavior, exact version header submission to mock, bilingual Source Center policy state, and mobile viewport overflow. These are not live-provider or production-backend evidence.
- No provider request, real application Fetch, deploy or push was performed during these checks.

## Exact remaining G4 prerequisites

1. Sign into the correct Google Cloud project and verify YouTube Data API v3 is enabled and the project's actual `search.list`/other-method quotas. The currently open console session is at Google sign-in; the project behind `YOUTUBE_API_KEY` has not been independently matched to a Cloud project.
2. Designate the one allowed LIVE host and configure that host's process environment. The current owner switch is off and this process is not designated.
3. The standard project/API-key registration process does not need to wait for the policy draft. Before any TrendScout YouTube feature is used, the owner must review the published Privacy Policy and Terms, verify operational retention/deletion facts, publish the final documents, then set `TREND_SCOUT_YOUTUBE_POLICY_APPROVED=true`. The in-app agreement flow is implemented; the deletion-request process and all hosting/log/backup facts remain unverified.
4. Verify YouTube Data API v3 enablement and the actual project quotas in the correct Cloud project; designate one LIVE host and explicitly set `TREND_SCOUT_YOUTUBE_LIVE_APPROVED=true` plus `TREND_SCOUT_YOUTUBE_LIVE_HOST` there. The local API key value was not exposed.
5. At an eligible schedule slot, with cache unable to satisfy the check and no concurrent job, use the already granted authorization for at most one `search.list` request and, only if the result requires it, one `videos.list` request. Do not retry. Record real IDs/URLs, response status and quota reservation only after those preconditions pass.

No other owner quota approval is requested; the existing two-request authorization remains valid within the stated cap. No push, deployment, billing activation or D7 work is included.

## Handoff: completion, blockers and confirmations

### Completed in this continuation

- Added a separate policy-publication approval flag. The production API blocks YouTube Fetch until that flag is approved, and then requires the exact current policy version on Fetch submission and status polling.
- Added the bilingual, keyboard-accessible browser agreement dialog and Source Center publication/agreement statuses. Accepted policy version is stored locally; the key remains server-side.
- Allowlisted YouTube video metadata in public LIVE Fetch status and suppress invalid IDs/URLs, synthetic records, and unknown/raw fields. Capabilities report local LIVE verification only for fresh, non-empty provider-backed evidence.
- Updated draft policy/terms wording, setup guidance, consent/API regression coverage and this report. No user database was reset or modified.

### Blockers and owner confirmations required to resume G4

- The Privacy Policy and Terms remain draft. The owner must confirm the operational details (data retention, logs/backups and deletion handling), approve and publish them, then set `TREND_SCOUT_YOUTUBE_POLICY_APPROVED=true`. The standard Google project/API-key registration checklist does not require this publication; the API Client's user-facing YouTube features do.
- Sign into the correct Cloud project and confirm YouTube Data API v3 enablement and actual method quotas. Provider quota remains UNKNOWN.
- Designate one LIVE host and explicitly approve it with `TREND_SCOUT_YOUTUBE_LIVE_APPROVED=true` and `TREND_SCOUT_YOUTUBE_LIVE_HOST`. The existing API key value must stay private.
- Once those checks pass, use the already authorized maximum of two attempts (one `search.list` and, only if needed, one `videos.list`; no retries). No further quota authorization is requested now.

### Out-of-scope issues that delayed verification

- The full regression exposed four failures in unchanged Wikimedia gateway tests, all caused by a missing sanitized diagnostic (`diagnostic is None`). They make G6 partial; no Wikimedia code was changed.
- The normal `start.sh` path performs unrelated Google/Bing/SerpApi account preflight. To avoid unrelated external requests during YouTube consent QA, the browser check used a local mock instead, so no real app end-to-end or live Fetch could be claimed.

## Local commits

- Implementation: `ac78355` — `feat(d6): add YouTube source and quota governance`
- This continuation's policy consent, public-record projection, tests and updated report are committed locally; the new commit SHA is provided in the handoff. No push is included.
