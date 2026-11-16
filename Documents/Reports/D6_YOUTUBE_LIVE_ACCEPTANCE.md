# D6 YouTube LIVE Acceptance

**Reviewed:** 16 November 2026

**Status:** implementation and offline acceptance complete; genuine LIVE acceptance blocked by owner-side configuration and policy prerequisites. No YouTube request was sent.

## Gate status

| Gate | Weight | Status | Evidence |
|---|---:|---|---|
| G0 — Repository synchronization | 8% | PASS | `git fetch origin` completed. Before implementation, local `HEAD` and `origin/main` both pointed to `7fac98dbcfa3871ab77d03e65d039d85c75584ee`; no reset or overwrite was used. |
| G1 — YouTube API contract | 12% | PASS (offline) | Fixed `search.list` and bounded `videos.list` request builders/parsers, source identity, validation, pagination binding and method-specific gateway routing are implemented and tested. No provider response was received. |
| G2 — Quota governance | 18% | PASS (offline) | Separate durable search and other-method pools, pre-request reservation, provider-day timezone handling and conservative failure accounting are implemented and tested. Actual Cloud project quota remains UNKNOWN. |
| G3 — Schedule, cache and Source Center | 12% | PASS (offline) | Manual schedule eligibility, cache-first/stale delivery, focused expiry, and Vietnamese/English Source Center budget and status fields are implemented and tested. |
| G4 — Genuine YouTube LIVE evidence | 16% | BLOCKED — 0% | The owner authorized up to two requests total (at most one `search.list` and one `videos.list`, no retries). That authorization is unused. API enablement/project quota and designated host are unverified; privacy policy is draft and the required agreement flow is absent. |
| G5 — SQLite and application flow | 16% | PASS (offline) | Synthetic-only tests prove Fetch result persistence, API readback after SQLite reopen, source-selection combinations, cache-only delivery and exclusion from keyword scoring. No synthetic record is labeled LIVE. |
| G6 — Regression and browser QA | 12% | PASS (offline) | 231 backend tests (2 skipped), 17 frontend tests, focused Ruff, Python compile, diff checks and local browser review passed. Browser review intentionally made no YouTube request. |
| G7 — Report and local commits | 6% | PASS | This report and the implementation have focused local commits. No push, deployment or billing change was made. |

**Weighted gate progress: 84%.** G4 contributes 0%; the goal is not 100% complete.

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

The repository base matched `origin/main` at the reference commit above before these changes. The focused implementation commit is `ac78355` (`feat(d6): add YouTube source and quota governance`).

## API and policy references checked

Documentation was checked on 16 November 2026:

- [`search.list` reference](https://developers.google.com/youtube/data/reference/rest/v3/search/list): `maxResults` supports up to 50; `regionCode=US` restricts results to videos viewable in that region, and `relevanceLanguage=en` is a relevance preference. Neither establishes the residence of creators or viewers. The method has a separate documented daily bucket and each page request costs quota.
- [`videos.list` reference](https://developers.google.com/youtube/data/reference/rest/v3/videos/list): `part=snippet` exposes title/channel metadata; a call costs one unit; `maxResults` is not supported with the `id` filter. Statistics are deliberately not requested.
- [Quota calculator](https://developers.google.com/youtube/data/determine_quota_cost?hl=en), updated 12 November 2026: documented defaults are 100 `search.list` calls/day in its separate bucket and 10,000 units/day for other methods; requests including invalid ones cost at least one point, and daily quota resets at midnight Pacific Time. These are documentation defaults, **not verified project limits**.
- [YouTube Developer Policies](https://developers.google.com/youtube/terms/developer-policies): an API client must require privacy-policy agreement before users can use its features; applicable API data must be deleted or refreshed within 30 calendar days. The current privacy/terms pages are still marked draft, and this agreement flow and the deletion-request process are not implemented or verified.
- [Additional derived-metrics policy](https://developers.google.com/youtube/terms/derived-metrics-policy?hl=en): applicable additional analytics use requires accepting the policy amendment. TrendScout leaves YouTube keyword extraction and derived metrics disabled.
- [YouTube revision history](https://developers.google.com/youtube/data/revision_history): since July 2025, `mostPopular` reflects Trending Music, Movies and Gaming charts, so it is not used to claim broad consumer-product coverage.

## Quota, schedule and provider calls

| Internal pool | Application ceiling | Window convention | Current app-side reservation |
|---|---:|---|---:|
| `search.list` | 6 calls/day; 200 calls/UTC calendar month | Daily provider reset uses `America/Los_Angeles` with DST; internal monthly history is retained in UTC | 0 in this goal's isolated QA state |
| Other methods, including `videos.list` | 6 units/day; 200 units/UTC calendar month | Same provider-day reset; separate UTC monthly ledger | 0 in this goal's isolated QA state |

The internal 200/month limits are ceilings, not expected consumption. Provider quota is `UNKNOWN`; the project-specific quota page was not accessible because the connected Cloud Console browser redirected to Google sign-in. The API key's value was never read or printed. A local credential-status check reported only `PRESENT`; `TREND_SCOUT_YOUTUBE_LIVE_APPROVED` was false and the configured live host did not match this process. No valid project/API-enable evidence was available.

**This goal sent 0 provider requests:** 0 `search.list`, 0 `videos.list`; there are no HTTP statuses, video IDs or URLs to report, and no quota was spent by this goal. The two-request owner authorization is recorded but unused. No live request will be used as a probe for project/API enablement or policy compliance.

## Cache, SQLite and application evidence

- The live source result is stored in the existing durable worker/checkpoint path and read through the Fetch status API. The SQLite reopen test reads back video ID/title from a separate temporary database and labels its record `SYNTHETIC`.
- Shared cache uses a YouTube-specific scope and grant. Retention cleanup only deletes the affected YouTube cache entry; a regression test confirms Google cache data remains. The ordinary collection/keyword table does not receive YouTube video titles as keywords; source snapshots retain bounded counts and provenance.
- No user database was reset, migrated by a live app run, or used for quota QA in this turn. Tests used isolated temporary SQLite state. The durable user's YouTube ledger was not read; actual project consumption therefore remains unknown. Existing Google, Bing and Wikimedia data were not changed.
- Source Center and Fetch evidence were inspected in local browser QA at a 390px viewport (document width 390px). The YouTube brand asset loaded at 300×67; console had no errors/warnings, and captured network activity had no `/youtube/v3` request because no Fetch action was clicked. This verifies the no-live UI state only. No genuine video detail, Data Quality record or Run History item is claimed.
- Fetch API and dashboard source rendering use text nodes and validated YouTube watch URLs. LIVE source results remain separate from Google/Bing keyword records.

## Verification results

- Backend regression: **231 passed, 2 skipped** (`unittest`, 4.797 seconds). Two non-failing diagnostics appeared during the suite: an asyncio slow-task notice and a temporary-directory `ResourceWarning`.
- Focused Ruff over changed backend modules/tests: **passed**.
- `python -m compileall -q app tests`: **passed**.
- Frontend Node tests: **17 passed, 0 failed**.
- `git diff --check` and staged diff check: **passed**.
- No provider request, Fetch button click, deploy or push was performed during these checks.

## Exact remaining G4 prerequisites

1. Sign into the correct Google Cloud project and verify YouTube Data API v3 is enabled and the project's actual `search.list`/other-method quotas. The currently open console session is at Google sign-in; the project behind `YOUTUBE_API_KEY` has not been independently matched to a Cloud project.
2. Designate the one allowed LIVE host and configure that host's process environment. The current owner switch is off and this process is not designated.
3. Approve final privacy/terms and retention facts, implement and verify the required user agreement and deletion-request path, then explicitly enable the source. Current policy evidence is **DRAFT — NOT READY TO SUBMIT**.
4. At an eligible schedule slot, with cache unable to satisfy the check and no concurrent job, use the already granted authorization for at most one `search.list` request and, only if the result requires it, one `videos.list` request. Do not retry. Record real IDs/URLs, response status and quota reservation only after those preconditions pass.

No other owner quota approval is requested; the existing two-request authorization remains valid within the stated cap. No push, deployment, billing activation or D7 work is included.

## Local commits

- Implementation: `ac78355` — `feat(d6): add YouTube source and quota governance`
- Acceptance report: committed separately after this report was added; the final report commit SHA is provided in the handoff.
