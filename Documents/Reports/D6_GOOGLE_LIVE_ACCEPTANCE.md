# D6-GOOGLE LIVE acceptance

**Status:** OFFLINE ACCEPTANCE COMPLETE; GOOGLE LIVE ACCEPTANCE INCOMPLETE
**Review date:** 2026-11-16 (Asia/Bangkok)
**Push/deployment:** none

## Weighted acceptance gates

Progress is the sum of passed gate weights. A blocked LIVE gate does not pass because offline code or fixtures exist.

| Gate | Weight | PASS evidence | Status |
|---|---:|---|---|
| G0 Git and baseline | 10% | Fetched `origin/main`, reviewed local status/diffs and recent history, preserved local changes, identified prior Wikimedia PASS | PASS |
| G1 Google discovery contract | 16% | Strict Trending Now and RSS parsing; deterministic normalization; source-specific metrics, relationships, missing values, deduplication and tests | PASS |
| G2 quota and security | 18% | Shared SerpApi budget, bounded attempts, backend-only credentials, fail-closed approvals, cache/idempotency and security regressions | PASS |
| G3 LIVE discovery | 10% | One authorized SerpApi Google Trends Trending Now request was charged but returned `UPSTREAM_ERROR` with 0 records. The single local RSS reservation was consumed before HTTP by an approval-check failure; no RSS request reached Google. | FAIL — no usable LIVE discovery response |
| G4 Google SERP LIVE | 10% | No real Google discovery candidates were returned, so the deterministic planner selected none and no Google SERP search was attempted. | NOT RUN — no valid candidate |
| G5 SQLite and application flow | 16% | Failed Google snapshots and jobs were persisted and read back after restart; the snapshot contains 0 Google records and the keyword ledger contains 0 Google observations. Existing Wikimedia data and user research were preserved. | INCOMPLETE — no genuine Google observation to carry through the full flow |
| G6 Regression and UI | 12% | Focused Google tests, selected-source worker and Chrome UI paths; regressions/browser checks; Wikimedia acceptance preserved; states and locales/themes reviewed | PASS |
| G7 Report and local commits | 8% | This report contains evidence, blockers and D6-BING handoff; focused commits exist locally and are not pushed | PASS |
| **Total** | **100%** |  |  |

**Weighted progress: 64%.** G0, G1, G2, G6 and G7 passed (10% + 16% + 18% + 12% + 8%). G3 failed to return usable data, G4 could not run without real Google candidates, and live-backed G5 remains incomplete. The goal is not complete.

## Git baseline

- Local branch: `main`.
- `git fetch origin` completed on 2026-11-15.
- `git fetch origin` completed at goal resume on 2026-11-15. Fetched `origin/main`: `40b713c678c4a6cbec1745d8daf763f610a708e5`.
- Local `HEAD` at resume matched `origin/main`; no remote commits were missing and the worktree was clean.
- Acceptance code is committed locally as `cb1522a` (two local commits after the fetched `origin/main`). The report update is committed separately after that code SHA.
- The existing Windows Wikimedia G2-G7 acceptance was PASS on the baseline. Its evidence remains the authoritative prior acceptance; this goal does not repeat or overwrite that run.

## Provider documentation review

Reviewed 2026-11-15:

- SerpApi [Google Trends Trending Now](https://serpapi.com/google-trends-trending-now): engine `google_trends_trending_now`; `geo`, `hours`, and `hl` are supported. Its example schema includes query, start timestamp, active state, provider search-volume signal, percentage increase, categories and trend breakdown.
- Google Trends Help [Trending Now export and fields](https://support.google.com/trends/answer/3076011): the page supports 4-hour, 24-hour, 48-hour and 7-day windows; query groups can contain variants/related queries; search traffic is bucketed; the RSS export is offered as one export method.
- SerpApi [Account API](https://serpapi.com/account-api) and [pricing](https://serpapi.com/pricing): the account endpoint is documented as free of search-credit charge; account responses include usage/limit metadata; current public plan examples and counting rules are changeable. No account entitlement is inferred for this operator.
- Google Trends [BigQuery dataset](https://support.google.com/trends/answer/12764470): US top/rising queries cover 210 DMAs with daily and hourly datasets; this is recorded only as a possible follow-up evaluation, not a D6 implementation.

Documentation review does not prove credential access, provider approval, reachable live endpoints, account quota, or feed availability. No live provider request is authorized by this report.

Retry and error handling guidance reviewed 2026-11-16:

- SerpApi's [status and error code guide](https://serpapi.com/api-status-and-error-codes) distinguishes authentication/permission failures (401/403), rate or quota failures (429), and server-side errors (500/503). It also documents search-level errors that may result from proxy timeouts or internal errors. TrendScout's persisted `UPSTREAM_ERROR` is a sanitized internal status and this run retained neither the provider HTTP status nor the underlying exception class, so it cannot be mapped to one of those cases.
- Google Cloud's [API troubleshooting guidance](https://docs.cloud.google.com/monitoring/api/troubleshooting) and AWS's [retry with backoff guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/retry-backoff.html) recommend bounded backoff for identified transient failures and distinguish time-based throttling from exhausted request quotas. They do not justify blind retries when the failure class is unknown or when doing so would exceed an operator's attempt budget.
- Therefore no automatic or manual SerpApi retry was issued after this `UPSTREAM_ERROR`. The single RSS operation also was not repeated: its guard consumed the one local RSS reservation before an HTTP request, and there is no supported reservation-release operation. No ledger was reset or edited to manufacture availability.

## Implementation and verification

### Implemented offline

- **Source strategy:** SerpApi Trending Now is the preferred broad discovery path when approved because its documented request returns US-targeted trend candidates and provider trend context in one bounded request. The official RSS feed is a separately labeled fallback because it uses no SerpApi search credits, while its sample and fields are more limited. Google SERP remains a separate verification step, capped at two server-selected candidates and one page each. No automatic extra time windows, BigQuery adapter, or Wikimedia re-fetch was added; Wikimedia stays an independent source with its existing provenance.
- Added strict SerpApi Google Trends Trending Now and Google Trends RSS adapters. Their normalized records retain provider and transport identity, US targeting, language, applicable window, query groups, related queries, approximate traffic buckets, provider timestamps, fetch time, source URL, delivery state and quality flags. Missing provider fields stay missing; approximate buckets are not converted to exact search counts.
- Added deterministic canonical keyword identity across the two discovery transports, bounded candidate selection and a server-controlled Google SERP verification plan capped at two selected candidates per fresh batch. RSS discovery does not count as SERP verification.
- Added an account-aware SerpApi budget gate using the documented account endpoint, numeric-only quota snapshots, a pinned server-side credential, separate RSS and paid request pools, bounded retries and idempotent reservations. Approval settings default off. No credential is read by the frontend.
- Added a separately opted-in local Google runtime. The default `start.ps1` route remains the prior Wikimedia local runtime. The Google profile binds to loopback, uses one worker and does not start a provider Fetch automatically. Source approvals and the runtime approval must be supplied separately by an operator.
- Persisted Google source evidence and provider/fetch timestamps through the existing SQLite-backed result flow, with explicit source delivery state, bounded result/checkpoint sizes and visible storage limits. Added Google summaries to Source Center, Fetch results, Run History and keyword detail. The UI identifies cached results and shows verified-result counts independently from discovery candidates.
- Fixed the public synthetic discovery summary to show its actual data mode instead of the hard-coded `LIVE` label.
- Added local setup and operator-boundary notes in [Backend/README.md](../../Backend/README.md). No provider setting or credential was enabled as part of this change.

### Exact changed files

The local D6 change set relative to the fetched `origin/main` baseline is:

```text
Backend/README.md
Backend/app/api/capabilities.py
Backend/app/api/fetches.py
Backend/app/collections/service.py
Backend/app/collections/store.py
Backend/app/discovery/contracts.py
Backend/app/discovery/extraction.py
Backend/app/discovery/source_results.py
Backend/app/google_live_settings.py
Backend/app/jobs/distributed_worker.py
Backend/app/jobs/public_admission.py
Backend/app/jobs/shared_cache.py
Backend/app/jobs/source_checkpoints.py
Backend/app/main.py
Backend/app/normal_wikimedia_live_policy.py
Backend/app/orchestration/distributed_public_fetch.py
Backend/app/orchestration/normal_google_live_composition.py
Backend/app/sources/google_trends/__init__.py
Backend/app/sources/google_trends/budget.py
Backend/app/sources/google_trends/handler.py
Backend/app/sources/google_trends/rss.py
Backend/app/sources/google_trends/rss_gateway.py
Backend/app/sources/google_trends/rss_handler.py
Backend/app/sources/google_trends/trending_now.py
Backend/app/sources/google_trends/verification_plan.py
Backend/app/sources/google_trends/workflow.py
Backend/app/sources/live_gateway.py
Backend/app/sources/serpapi/account.py
Backend/app/sources/serpapi/pagination.py
Backend/app/storage/local_sqlite_snapshot_blobs.py
Backend/app/workers/d5_source_handlers.py
Backend/app/workers/guarded_attempt.py
Backend/scripts/run_google_live_local.py
Backend/tests/browser_g5_run_history_accessibility_offline.py
Backend/tests/browser_google_fetch_selection_offline.py
Backend/tests/test_d5_live_gateway.py
Backend/tests/test_d6_wikimedia_synthetic_e2e.py
Backend/tests/test_google_fetch_selection.py
Backend/tests/test_google_trends.py
Backend/tests/test_keyword_workbench_ui.py
Documents/Reports/D6_GOOGLE_LIVE_ACCEPTANCE.md
Frontend/public/app.js
Frontend/public/index.html
Frontend/public/modules/capabilities.js
Frontend/public/modules/fetch.js
Frontend/public/modules/i18n/en.js
Frontend/public/modules/i18n/vi.js
Frontend/public/modules/keyword-workbench.js
Frontend/public/modules/run-history-coverage.js
Frontend/public/modules/run-history.js
Frontend/public/styles.css
Frontend/tests/fetch-source-selection.test.mjs
Frontend/tests/run-history-coverage.test.mjs
start.ps1
```

### Verification evidence (offline only)

- `Backend\.venv\Scripts\python.exe -m unittest discover -s tests`: **829 tests passed** on 2026-11-15 (129.214 seconds). This includes Google parser, persistence identity, guarded gateway, quota, local composition, selected-source Google Fetch, Windows launcher default, existing Wikimedia, public admission, and end-to-end regression tests. All test transports are fixtures or mocks; this is not provider verification.
- New focused source-selection and startup tests: **3 passed**. Two exercise `D5SourceHandlers`, the guarded worker, and `DistributedPublicFetch`: Google-only Fetch issued one mocked RSS request and no Wikimedia request; combined Google/Wikimedia Fetch issued one mocked request to each source. The third confirms the Windows default launcher still starts the Wikimedia profile and Google requires `-GoogleLive`. Fixture results remained `SYNTHETIC`.
- `Backend\.venv\Scripts\python.exe tests\browser_google_fetch_selection_offline.py`: Chrome PASS for the real source selector, ALL (Google plus Wikimedia), Google-only Fetch, the RSS-limited and zero-SERP summary, and localized synthetic provenance. The browser and backend use mock responses; no provider endpoint is contacted.
- `Backend\.venv\Scripts\ruff.exe check` on `Backend/tests/test_google_fetch_selection.py` and `Backend/tests/browser_google_fetch_selection_offline.py`: passed.
- `Backend\.venv\Scripts\python.exe -m py_compile` on those two focused Python test files: passed.
- `node --test Frontend/tests/*.test.mjs`: **7 passed**, including three `PublicFetchController` source-selection cases for Google-only, Google plus Wikimedia, and the default all-sources request.
- `node --check Frontend/public/app.js`: passed.
- `Backend\.venv\Scripts\python.exe tests\browser_g5_run_history_accessibility_offline.py`: Chrome PASS for English/Vietnamese detail labels, accessibility region name, focus, Escape handling, and Google RSS provenance, cached delivery, timestamps and zero SERP verifications.
- `Backend\.venv\Scripts\python.exe tests\browser_workspace_offline.py`: Chrome PASS at 320, 390, 768, 1024, 1440 and 1920 px; mocked UI flows confirmed no provider Fetch.
- `git diff --check`: passed. Git reports expected LF-to-CRLF normalization warnings for this Windows workspace.

These checks verify offline contracts and rendering. They do not establish that SerpApi credentials work, RSS is reachable, quota is available, a Google response was received, or the data was persisted from Google LIVE.

### Corrections and focused checks after LIVE diagnostics

- The RSS `SOURCE_NOT_APPROVED` failure exposed an approval-object comparison race between the initial and final guard checks. The local approval provider now reuses a stable approval object for the bounded operation. This correction was committed as `bbb3862`; no RSS request was repeated after the correction.
- The failed Google snapshots also exposed a path that could reconcile old ledger rows when a source returned no usable records. Failed/empty snapshots now leave the existing keyword ledger and collection membership untouched. The same commit fixes the restrictive local budget policy check and records Google Trends HTTP status in coverage when one exists.
- A follow-up backend/frontend correction suppresses `FRESH`/`CACHED` delivery labels on failed Google data, distinguishes provider count from retained ledger count, and uses source-neutral Fetch messages for Google/mixed source runs. This was committed as `cb1522a`.
- Focused checks during this continuation: 44 backend tests and 9 frontend tests passed before the final delivery-label adjustment. After that adjustment, 3 targeted backend tests and 5 `run-history-coverage` frontend tests passed; Ruff on changed backend files, `node --check Frontend/public/app.js`, and `git diff --check` passed. The local Chrome UI was rechecked after server restart. The full 829-test suite was not rerun.
- These implementation corrections do not convert either failed LIVE attempt into a successful observation and were not followed by another provider request.

## LIVE status

### Runtime and preflight evidence

- `LocalCredentialProvider.status()` returned `SERPAPI_API_KEY=PRESENT`; the key itself was never printed. The Windows credential file exists at `%LOCALAPPDATA%\TrendScout\.env`.
- At goal resume, the parent PowerShell process did not have any of the five Google LIVE approval variables set. For the dedicated local server only, the five required flags were supplied in that child process, without changing user or machine environment variables: `TREND_SCOUT_GOOGLE_LOCAL_LIVE_APPROVED`, `TREND_SCOUT_GOOGLE_RSS_APPROVED`, `TREND_SCOUT_GOOGLE_TRENDS_APPROVED`, `TREND_SCOUT_GOOGLE_SERP_APPROVED`, and `TREND_SCOUT_SERPAPI_ACCOUNT_API_APPROVED`. Runtime capabilities reported local environment, `local_live_demo=true`, read-only false, Fetch enabled, Google workflow `local_only=true`, and credentials PRESENT. Server startup made no provider request. The current shell still has the approval variables unset, and the acceptance server was stopped after UI verification.
- The existing fixed-host Google RSS and SerpApi adapters, no-redirect/TLS/network guards, source approval checks, local request pools, and six-search cross-machine reserve were inspected. No conflicting active Google job was present before the bounded attempts. No Bing, YouTube, or Wikimedia request was issued during this acceptance.
- SerpApi Account API reads: **3**. Last observed pre-attempt snapshot at `2026-11-15T16:14:43.094124Z`: 250 remaining, monthly used 0, current-hour used 0 of 250. Post-attempt snapshot at `2026-11-15T16:15:48.003039Z`: 249 remaining, monthly used 1, current-hour used 1 of 250. An earlier preflight read at `2026-11-15T15:56:31.759788Z` also showed 250 remaining, monthly used 0, current-hour used 0. These are last-observed values, not a fresh quota lookup after server shutdown.
- Local SerpApi paid-search budget at the last runtime read: **1/3 used, 2/3 remaining**; the cross-machine reserve of 6 was preserved. Local Google RSS budget: **1/1 reserved/consumed, 0 remaining**. No quota ledger was reset. The provider account showed ample quota before the Trends request, so provider quota did not block that attempt.

### Google Trends LIVE attempts

| Path | Actual outcome | Evidence |
|---|---|---|
| Official Google Trends RSS (`US`) | **No HTTP request reached Google.** One local guarded operation failed `SOURCE_NOT_APPROVED` during its final approval check after its one local reservation had been consumed. | Intended fixed source URL: `https://trends.google.com/trending/rss?geo=US`; dataset created `2026-11-15T16:04:22.507635Z`; HTTP status/provider timestamp/records: none; accepted 0, rejected 0; no traffic buckets. Local RSS budget remaining 0/1. This is not an RSS LIVE success. |
| SerpApi Google Trends Trending Now (`US`) | **One paid LIVE search attempt; internal result `UPSTREAM_ERROR`; 0 records.** | Dataset created `2026-11-15T16:14:44.447128Z`; account quota moved 250→249. No provider HTTP status, exception subtype, provider timestamp, candidate, or response body was retained, so the underlying cause is unknown. No further attempt was made. |

Google keywords actually received from either provider path: **none (0)**. RSS returned no provider response; Trending Now returned no usable records. Approximate search traffic metrics: none received. RSS coverage limits and the distinction between approximate traffic and exact volume remain documented above; no metrics were fabricated.

### Google SERP LIVE

- Google SERP attempts: **0**. The deterministic planner received no real Google discovery candidates, so it selected no keyword. No Wikimedia keyword or invented candidate was substituted.
- Paid SerpApi usage for this acceptance: **1 search attempt** for Google Trends, **0** Google SERP searches, **1 of the authorized maximum 3 paid searches used**. The remaining 2 local paid slots were not spent. No Google SERP result count, cache state, rank, URL, or keyword evidence exists.

### SQLite, restart read-back, and UI

- The latest post-restart API dataset was `078397c238c9f3628d7ea4f2eefce30d`: status `FAILED`, mode `LIVE`, source `google_web_serp`, error `UPSTREAM_ERROR`, provider count 0, received 0, rejected 0, snapshot-retained keyword count 0. It retained the older ledger count (1,219; 1,216 eligible, 269 assigned, 947 unassigned) with `MIXED_OR_UNKNOWN` ledger coverage; this is not a Google observation. Google-source keyword listing total was 0.
- Run History read back both failed Google source jobs after a safe local-server restart. The RSS record retained `SOURCE_NOT_APPROVED`; the Trends record retained `UPSTREAM_ERROR`. Existing collection processing jobs completed against the prior 1,216-row ledger, not Google data. Failed Google delivery is now displayed as unavailable, not `FRESH` or `CACHED`.
- Browser checks covered Source Center, Run History/details, keyword listing and a retained Wikimedia keyword detail, Collections (61 collections), Data Quality, Analytics overview, and Product Research (1 existing record). The UI showed zero Google-source keywords/observations and the failed snapshot's unavailable delivery state. Product Research and prior Wikimedia evidence remained present.
- The trusted pre-run database backup was preserved. After restoring affected user-owned fields, direct row-hash comparison confirmed previous rows unchanged. Additions since that backup are two failed dataset snapshots, two fetch jobs, two membership reconciliation runs and 714 reconciliation items. The database integrity check returned `ok`; no database was deleted or reset. The corrective failed/empty-snapshot path now avoids further reconciliation of the old ledger.
- The local server was stopped after read-back and UI verification. Google records persisted/read back: **0**. Therefore the complete Google provider → observation → keyword → SQLite → application flow is **not verified** and G5 remains incomplete.

- Umbrella `GOOGLE_LIVE_VERIFIED`: **NOT_VERIFIED**. Offline contracts, runtime configuration, failed snapshots, quota evidence, and UI read-back do not substitute for actual Google records.

## Remaining Google LIVE acceptance

### Handoff

1. Do not blindly retry SerpApi `UPSTREAM_ERROR`: its underlying HTTP/exception class is unknown, and one authorized paid attempt was already consumed. Diagnose from safe retained provider metadata or a separately authorized diagnostic workflow before considering another bounded attempt.
2. The RSS code race is corrected, but the local 1/1 RSS reservation remains consumed; no supported release/refund exists. Do not edit/reset the ledger. Any further RSS LIVE request requires a new explicitly bounded session/authorization and an approved budget path that preserves the existing ledger.
3. A later permitted session should run Google RSS once only if its local budget admits the request, or use one available SerpApi Trends attempt only after the operator confirms retry is justified and account quota remains sufficient. Capture provider HTTP status, response/schema, timestamp, and quota before/after without expanding the configured limit.
4. Once real Google candidates are received, use the existing selective plan for at most two distinct candidates, recording exact provider/cache state and quota. Then verify the resulting records in SQLite and the existing UI views listed above.
5. Recompute G3, G4, and G5 only from genuine provider observations. Keep the weighted score below 100% until every mandatory gate has PASS evidence.

### D6-BING handoff

- Reuse the guarded attempt, durable quota reservation, approval, cache, checkpoint, SQLite and evidence-display boundaries where their contracts fit. Do not reuse Google engine parameters, quota semantics or provider response assumptions.
- Verify Bing's current official provider documentation, allowed scope, credentials, quota/cost model, pagination and caching before implementing a Bing LIVE adapter.
- Keep Bing source metrics separate, retain US localization as a filter rather than proof of US demand, and preserve the same distinctions between unavailable, zero, cached and fresh values.
- Require its own approved LIVE evidence and persisted SQLite read-back. Do not treat this Google report or its offline fixtures as D6-BING approval.

## Local commits

- Implementation and offline verification: `6f6e836` (`feat(google): add guarded Trends discovery workflow`).
- Initial offline acceptance and LIVE blocker report: `8cbaaa2` (`docs(google): record offline acceptance and LIVE blockers`).
- Selected-source offline integration checks: `b6a99cc` (`test(google): cover selected Fetch workflows offline`).
- Prior acceptance evidence update: `dd4e0bb` (`docs(google): update offline Fetch acceptance evidence`).
- Synthetic provenance fix and Chrome source-selection coverage: `377d45a` (`fix(google): verify Fetch UI provenance offline`).
- Preserve existing Wikimedia ledger on failed Google fetches and correct approval/budget/delivery handling: `bbb3862` (`fix(google): preserve existing ledger on failed fetches`).
- Correct failed Google delivery labels in the UI: `cb1522a` (`fix(ui): label failed Google delivery accurately`).
- This acceptance report is committed separately. Commits remain local; no push or deployment was performed.
