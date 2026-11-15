# D6-GOOGLE LIVE acceptance

**Status:** OFFLINE ACCEPTANCE COMPLETE; LIVE ACCEPTANCE BLOCKED EXTERNAL
**Review date:** 2026-11-15 (Asia/Bangkok)
**Push/deployment:** none

## Weighted acceptance gates

Progress is the sum of passed gate weights. A blocked LIVE gate does not pass because offline code or fixtures exist.

| Gate | Weight | PASS evidence | Status |
|---|---:|---|---|
| G0 Git and baseline | 10% | Fetched `origin/main`, reviewed local status/diffs and recent history, preserved local changes, identified prior Wikimedia PASS | PASS |
| G1 Google discovery contract | 16% | Strict Trending Now and RSS parsing; deterministic normalization; source-specific metrics, relationships, missing values, deduplication and tests | PASS |
| G2 quota and security | 18% | Shared SerpApi budget, bounded attempts, backend-only credentials, fail-closed approvals, cache/idempotency and security regressions | PASS |
| G3 LIVE discovery | 10% | Authorized, real RSS and/or SerpApi Trending Now response with dated, bounded evidence; provider mode reported accurately | BLOCKED_EXTERNAL pending use approval |
| G4 Google SERP LIVE | 10% | Authorized real Google SERP response with persisted run and quota/freshness evidence | BLOCKED_EXTERNAL pending credential and approval |
| G5 SQLite and application flow | 16% | Genuine source evidence persisted/read back and exercised through API, keyword, dashboard, collections and research workflow without data loss or hidden truncation | BLOCKED_EXTERNAL pending authorized Google evidence |
| G6 Regression and UI | 12% | Focused Google and relevant regression/browser checks; Wikimedia acceptance preserved; states and locales/themes reviewed | PASS |
| G7 Report and local commits | 8% | This report contains evidence, blockers and D6-BING handoff; focused commits exist locally and are not pushed | PASS |
| **Total** | **100%** |  |  |

**Weighted progress: 64%.** G0, G1, G2, G6 and G7 passed (10% + 16% + 18% + 12% + 8%). G3, G4 and live-backed G5 remain BLOCKED_EXTERNAL; the mandatory LIVE work is not complete.

## Git baseline

- Local branch: `main`.
- `git fetch origin` completed on 2026-11-15.
- Local `HEAD` and fetched `origin/main`: `46a0590e07c09f57a481aa4cfb952db6d9fd6fd0`.
- Worktree and index were clean before implementation; no missing remote commits or user changes were present.
- The existing Windows Wikimedia G2-G7 acceptance was PASS on the baseline. Its evidence remains the authoritative prior acceptance; this goal does not repeat or overwrite that run.

## Provider documentation review

Reviewed 2026-11-15:

- SerpApi [Google Trends Trending Now](https://serpapi.com/google-trends-trending-now): engine `google_trends_trending_now`; `geo`, `hours`, and `hl` are supported. Its example schema includes query, start timestamp, active state, provider search-volume signal, percentage increase, categories and trend breakdown.
- Google Trends Help [Trending Now export and fields](https://support.google.com/trends/answer/3076011): the page supports 4-hour, 24-hour, 48-hour and 7-day windows; query groups can contain variants/related queries; search traffic is bucketed; the RSS export is offered as one export method.
- SerpApi [Account API](https://serpapi.com/account-api) and [pricing](https://serpapi.com/pricing): the account endpoint is documented as free of search-credit charge; account responses include usage/limit metadata; current public plan examples and counting rules are changeable. No account entitlement is inferred for this operator.
- Google Trends [BigQuery dataset](https://support.google.com/trends/answer/12764470): US top/rising queries cover 210 DMAs with daily and hourly datasets; this is recorded only as a possible follow-up evaluation, not a D6 implementation.

Documentation review does not prove credential access, provider approval, reachable live endpoints, account quota, or feed availability. No live provider request is authorized by this report.

## Implementation and verification

### Implemented offline

- Added strict SerpApi Google Trends Trending Now and Google Trends RSS adapters. Their normalized records retain provider and transport identity, US targeting, language, applicable window, query groups, related queries, approximate traffic buckets, provider timestamps, fetch time, source URL, delivery state and quality flags. Missing provider fields stay missing; approximate buckets are not converted to exact search counts.
- Added deterministic canonical keyword identity across the two discovery transports, bounded candidate selection and a server-controlled Google SERP verification plan capped at two selected candidates per fresh batch. RSS discovery does not count as SERP verification.
- Added an account-aware SerpApi budget gate using the documented account endpoint, numeric-only quota snapshots, a pinned server-side credential, separate RSS and paid request pools, bounded retries and idempotent reservations. Approval settings default off. No credential is read by the frontend.
- Added a separately opted-in local Google runtime. The default `start.ps1` route remains the prior Wikimedia local runtime. The Google profile binds to loopback, uses one worker and does not start a provider Fetch automatically. Source approvals and the runtime approval must be supplied separately by an operator.
- Persisted Google source evidence and provider/fetch timestamps through the existing SQLite-backed result flow, with explicit source delivery state, bounded result/checkpoint sizes and visible storage limits. Added Google summaries to Source Center, Fetch results, Run History and keyword detail. The UI identifies cached results and shows verified-result counts independently from discovery candidates.
- Added local setup and operator-boundary notes in [Backend/README.md](../../Backend/README.md). No provider setting or credential was enabled as part of this change.

### Verification evidence (offline only)

- `Backend\.venv\Scripts\python.exe -m unittest discover -s tests`: **826 tests passed** on 2026-11-15 (116.689 seconds). This includes Google parser, persistence identity, guarded gateway, quota, local composition, existing Wikimedia, public admission, and end-to-end regression tests. All test transports are fixtures or mocks; this is not provider verification.
- `Backend\.venv\Scripts\ruff.exe check` over changed Python implementation and test files: passed.
- `Backend\.venv\Scripts\python.exe -m compileall -q app scripts`: passed.
- `node --test Frontend/tests/run-history-coverage.test.mjs`: **4 passed**.
- `node --check` over `Frontend/public/**/*.js`: passed.
- `Backend\.venv\Scripts\python.exe tests\browser_g5_run_history_accessibility_offline.py`: Chrome PASS for English/Vietnamese detail labels, accessibility region name, focus, Escape handling, and Google RSS provenance, cached delivery, timestamps and zero SERP verifications.
- `Backend\.venv\Scripts\python.exe tests\browser_workspace_offline.py`: Chrome PASS at 320, 390, 768, 1024, 1440 and 1920 px; mocked UI flows confirmed no provider Fetch.
- `git diff --check`: passed. Git reports expected LF-to-CRLF normalization warnings for this Windows workspace.

These checks verify offline contracts and rendering. They do not establish that SerpApi credentials work, RSS is reachable, quota is available, a Google response was received, or the data was persisted from Google LIVE.

## LIVE status

- Google discovery offline: OFFLINE_VERIFIED; fixtures and mocks only.
- Google discovery live: BLOCKED_EXTERNAL pending explicit use and local-runtime approval for the selected discovery path.
- Google SERP offline: existing adapter and dynamic candidate planning are OFFLINE_VERIFIED; no provider SERP request was made.
- Google SERP live: BLOCKED_EXTERNAL pending an approved SerpApi credential, account budget, and Google SERP use approval.
- LIVE Google request count: **0**.
- SerpApi account/quota request count: **0**; no account snapshot or available-credit evidence exists in this run.
- Google records received from provider: **0**; Google LIVE SQLite read-back: **not performed**.
- Umbrella `GOOGLE_LIVE_VERIFIED`: NOT_VERIFIED. This must remain false until authorized provider requests, actual source evidence, quota evidence and persisted read-back are reviewed.

## Remaining Google LIVE acceptance

### Handoff

1. Before any LIVE request, obtain explicit operator approval for the selected Google path and local runtime, confirm permitted usage/retention, and provision an approved server-side credential for paid SerpApi paths. Do not set the opt-in flags until those conditions are met.
2. Verify the SerpApi account endpoint and remaining quota from the approved credential, then run one bounded Google Trends discovery. Capture provider-reported mode, provider timestamp, fetch timestamp, delivery state, source scope, numeric quota before/after, request count and quality flags.
3. If Google SERP verification is approved, use only the server-selected candidate plan, capped at two candidates per fresh batch, and record exact charged attempts and persisted verification evidence. Do not label RSS-only candidates verified.
4. Read the same records back from SQLite and inspect Source Center, Fetch result, Run History, keyword detail, Collections and Research views. Check that approximate buckets remain approximate, `0` remains distinct from missing, cached data retains its original provider timestamp, and storage omissions are visible.
5. Recompute G3, G4 and G5 only from those real observations. Keep the weighted score below 100% until every mandatory gate has PASS evidence.

### D6-BING handoff

- Reuse the guarded attempt, durable quota reservation, approval, cache, checkpoint, SQLite and evidence-display boundaries where their contracts fit. Do not reuse Google engine parameters, quota semantics or provider response assumptions.
- Verify Bing's current official provider documentation, allowed scope, credentials, quota/cost model, pagination and caching before implementing a Bing LIVE adapter.
- Keep Bing source metrics separate, retain US localization as a filter rather than proof of US demand, and preserve the same distinctions between unavailable, zero, cached and fresh values.
- Require its own approved LIVE evidence and persisted SQLite read-back. Do not treat this Google report or its offline fixtures as D6-BING approval.

## Local commits

- Implementation and offline verification: `6f6e836` (`feat(google): add guarded Trends discovery workflow`).
- This acceptance report is committed separately. Commits remain local; no push or deployment was performed.
