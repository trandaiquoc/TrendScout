# D6.2.3 Wikimedia synthetic end-to-end and dashboard validation

**Validation date:** 2026-11-13
**Runtime:** Native Windows, Python 3.12.10, HTTPX 0.28.1, native Chrome
**Status:** Synthetic path verified. Wikimedia LIVE is not verified.
**`WIKIMEDIA_LIVE_SMOKE_VERIFIED`:** remains unset.

## Result

The real TrendScout API, admission, distributed worker, checkpoint journal, discovery functions and dashboard assets process a deterministic Wikimedia-shaped fixture end to end. The temporary test application uses `data_mode=SYNTHETIC` and a temporary SQLite database. The only source with a gateway or transport is Wikimedia, and that gateway refuses anything except `httpx.MockTransport`. Google, Bing and YouTube return `UNAVAILABLE` without gateway construction, credential resolution or HTTP clients.

The fixture produced 10 accepted normalized records from 14 article rows, with 4 invalid rows rejected. Discovery produced 36 deterministic candidates. Each candidate remains unscored and insufficiently evidenced because there is no comparable baseline. A real Windows Chrome browser completed one Fetch against the actual loopback API and rendered the result in the actual dashboard.

**Important isolation exception:** the first combined regression run included the pre-existing `test_controlled_network_constructor_opens_no_request` test from `test_d5_wikimedia_gateway.py`. That test called `WikimediaTopPageGateway.controlled_network()`, which constructed one real `httpx.AsyncHTTPTransport`; it did not send an HTTP request, resolve a provider, or access credentials, and the gateway was closed in test teardown. The D6.2.3 instructions prohibit even constructing that transport. Therefore G2 is recorded as **FAIL**, despite zero real provider requests. Later regression runs excluded that test. This exception cannot be undone by a rerun.

## Git baseline and scope

- Ran `git fetch origin` before changes.
- Starting worktree was clean; local `HEAD` and `origin/main` both matched `3a985afa55263491e0584d60482f0f654148bf87` (`docs(d6): review Wikimedia re-authorization feasibility`).
- No reset, pull overwrite, push, canonical ledger access, live quota change, original approval change or D6.3 work occurred.
- Code changes are limited to synthetic unavailable-source mode, preserving discovery version metadata, and displaying the source observation date in dashboard evidence. The D6 LIVE runtime, quota policy, approval lifecycle and local process/security controls were not changed.

## Synthetic composition and fixture

`Backend/tests/d6_wikimedia_synthetic_support.py` builds the application through the existing `create_app()` and complete four-source registry contract. It uses an explicit `test` profile, `run_mode=synthetic`, enabled synthetic Fetch, and `LocalSQLiteAtomicStateStore(data_mode="SYNTHETIC")` inside each test's temporary directory. Machine `.env`, cloud configuration and provider credentials are not loaded.

The Wikimedia source is composed from `WikimediaTopPageGateway.offline_fixture()`, `D5SourceHandlers`, `DistributedFetchWorker`, `PublicAdmission`, `DistributedPublicFetch` and `SourceCheckpointJournal`. Its transport accepts exactly one `GET` to the fixed Wikimedia-shaped US/all-access endpoint for 2026-11-10; any other host, method, path, query or second request raises an assertion. The in-memory approval-shaped object is a test fixture only and is not read from or written to an approval register. Other source handlers declare `SYNTHETIC` and return `UNAVAILABLE` without gateways. No processing grant is passed to the synthetic facade.

The checked-in fixture `Backend/tests/fixtures/d6_2_3_wikimedia_response.json` uses a fixed observation date of 2026-11-10 and fixed fetch time 2026-11-12T12:00:00Z. It contains US, `all-access`, `en.wikipedia` rows for Linen Pants, Vintage Denim, Fashion Design, Kitchen Organizer, Interior Design, Furniture Design, Robotics and Artificial Intelligence. Its view values are invented fixture values, not actual Wikimedia or market data.

| Fixture handling | Count | Notes |
| --- | ---: | --- |
| Valid records accepted | 10 | Eight representative article titles, one accepted duplicate Linen Pants row, and one untrusted HTML-shaped article string. |
| Invalid article rows rejected | 4 | Empty article, malformed project, rank 0, and negative `views_ceil`. |
| Duplicate article rows | 2 accepted | The current source parser preserves both rows; it does not reject or deduplicate same-page duplicate rows. |
| Wrong country/access/date cases | Rejected | Separate parser checks verify CA, `desktop`, and a mismatched date are refused. |

Normalized records retain source, project, article forms, country/access, observation date, fetched time, `views_ceil`, provider rank, source URL and `TOP_LIST_ONLY` / `VIEWS_APPROXIMATE` quality flags. Discovery now also returns parser, extraction, taxonomy, classification and assessment algorithm versions from their existing contracts. The fixture-only raw-body sentinel is absent from status output, checkpoints and the temporary SQLite state.

## API, worker and checkpoint evidence

The acceptance test uses the actual HTTP endpoints in order:

1. Application startup and capabilities read: zero Wikimedia mock calls.
2. `POST /api/v1/fetches` with `{}` and one `Idempotency-Key`: HTTP 202.
3. The API's existing background task invokes the distributed worker. The worker runs the Wikimedia handler once, checkpoints normalized records, and completes the job as `PARTIAL` because four input rows were rejected and the other three sources are unavailable.
4. `GET /api/v1/fetches/{fetch_id}` returns terminal status, source results and discovery output. A second status read returns identical results and makes no source request.
5. Replaying the same idempotency key returns the same job with `Idempotent-Replay: true`. Calling the worker again after completion claims no job and does not repeat the fixture request.

Each isolated end-to-end API run observed exactly **1 Wikimedia MockTransport request**, **0 Google/Bing/YouTube requests**, and **0 real provider requests**. The browser run independently observed one Fetch POST, two status polls, and exactly one Wikimedia MockTransport request. These counts are per temporary end-to-end run; the permanent temporary-database tests do not share counters or state.

The retained Wikimedia checkpoint is `SYNTHETIC`, complete, and expires under its explicit five-minute synthetic checkpoint grant. Admission and checkpoints use only the temporary test database. The canonical `%LOCALAPPDATA%\TrendScout\local\d6-smoke\ledger.sqlite` was not opened or modified.

## Discovery provenance and metric semantics

`discover_fetch_results()`, `extract_candidates()`, `prioritize_for_research()` and `assess_candidate()` run on records returned after API admission and worker execution; this is not a direct discovery-only test. The result has 36 candidates, all in `SYNTHETIC` mode, with deterministic IDs and existing taxonomy categories where supported. Fashion and Home & Living examples classify to `fashion` and `home_living`; Robotics classifies to `technology`.

Each candidate score is `null`. Assessments remain `INSUFFICIENT_EVIDENCE` because comparable temporal coverage and baseline are absent. Result limitations include `SYNTHETIC_ONLY`, `NOT_LIVE_VERIFIED`, `NO_BASELINE` and `COVERAGE_LIMITED`. There are no growth, acceleration, search-volume, sales, purchase-intent or product-demand claims. `views_ceil` remains only the Wikimedia source metric and is not copied into candidate evidence or used for scoring.

A separate test supplies a LIVE-shaped record without a valid extraction grant and confirms `DISCOVERY_PROCESSING_BLOCKED` with zero candidates. Synthetic handlers are all declared SYNTHETIC; the synthetic facade has no processing-grant resolver, and cannot relabel those records LIVE.

## Dashboard and browser validation

`Backend/tests/browser_wikimedia_synthetic_e2e_offline.py` launches the actual `create_app()` on loopback, serves the real `Frontend/public` dashboard files, and uses native Windows Chrome. The browser talks to the real synthetic API, whose Fetch triggers the worker and MockTransport fixture. No mock dashboard or API response substitution is used.

Chrome verified:

- Fetch is enabled from synthetic capabilities without token or provider ID fields; rapid double-click results in one POST with `{}` and an idempotency key.
- Status polling reaches terminal state; Wikimedia is shown as SYNTHETIC/PARTIAL and Google, Bing and YouTube as UNAVAILABLE.
- Discovery candidates and evidence render with source, US country scope, all-access project scope, observation date `Nov 10, 2026`, fetched time and SYNTHETIC provenance.
- Search, Fashion and Home & Living filters work; light/dark and English/Vietnamese controls remain available; the dashboard responds at a 375 px viewport.
- Assessment remains insufficient; the discovery panel does not label Pageviews as Search Volume.
- The untrusted HTML-shaped article creates no HTML element, the raw fixture marker is not rendered, no provider credential is sent or stored, and Fetch calls stay same-origin.
- Reload does not create another job or another Wikimedia request.

The dashboard evidence line was updated to display the observation date separately from fetched time. It still displays source-provided evidence using text nodes, not HTML.

## Verification

Native Windows Python 3.12.10 results:

- **67 focused offline unit tests passed** across Wikimedia gateway/parser/cleaning, distributed discovery, offline worker composition, D6 dashboard capabilities, D6 local HTTP, the new D6.2.3 API flow, and public Fetch API tests. The forbidden `test_controlled_network_constructor_opens_no_request` was excluded from this final safe run.
- **4 new D6.2.3 tests passed**: full API/worker/checkpoint/replay, wrong scope/date rejection, LIVE extraction-grant blocking, and synthetic-mode enforcement.
- **Native Chrome browser E2E passed** with one job, two status polls, one Wikimedia MockTransport request and zero browser assertions failing.
- The edited `Frontend/public/app.js` passed Node's ES module syntax check. New/changed Python files passed `py_compile`.
- Ruff was unavailable in the installed environment; no dependency was installed.

## Acceptance gates

| Gate | Result | Evidence |
| --- | --- | --- |
| G1 Git baseline | PASS | Clean synchronized starting tree at `3a985afa55263491e0584d60482f0f654148bf87`; final documentation commit recorded below. |
| G2 Offline isolation | **FAIL** | No provider request was sent and end-to-end runs used only MockTransport, but an earlier regression run constructed a real Wikimedia `AsyncHTTPTransport` through the pre-existing controlled-network constructor test. |
| G3 Wikimedia normalization | PASS | 10 accepted, 4 rejected; scope/date checks reject invalid envelopes; duplicate rows follow existing parser behavior. |
| G4 API/worker end-to-end | PASS | Capabilities → HTTP 202 Fetch → admission → worker → temporary checkpoint → terminal status → discovery → status replay. |
| G5 Provenance and discovery | PASS | 36 SYNTHETIC candidates; source/provider/scope/date/fetched time/version metadata retained; no score or baseline claim; LIVE without grant is blocked. |
| G6 Dashboard/browser | PASS | Native Windows Chrome used actual dashboard assets and actual API responses; Fetch, polling, reload, filters, evidence, localization/theme and mobile viewport checks passed. |
| G7 Metric semantics and security | PASS | Pageviews remains `views_ceil`; no search-volume/demand claims; raw marker, credential, or injected HTML was not exposed. |
| G8 Regression and handoff | PASS after local commit | Focused tests and browser E2E passed; report and D6 pointer committed locally, with no push. |

Because G2 failed its strict no-real-transport requirement, this report does not claim 100% acceptance. The transport constructor test made no request and did not perform DNS lookup, but that does not satisfy the stricter D6.2.3 rule.

## Remaining LIVE blockers and next goal

D6.2 remains incomplete. The original Wikimedia one-request reservation remains consumed, its approval remains expired, and no new authorization or quota was created. The D6.2.1 launcher summary still needs its separately scoped diagnostic-field update before it can expose those diagnostics. No Wikimedia LIVE connectivity, credentials, current schema, or provider verification has been established here.

Recommended next goal: resolve the G2 test-isolation issue by making the existing “constructor opens no request” regression test patch or substitute the transport factory so it cannot instantiate a real HTTP transport, then rerun the isolated offline gates. Keep all live attempts blocked unless the owner separately authorizes them. This report does not start that work.

**Local commit:** `ba87e1c6ceee25ddaf4ac4cd1042475aba979c53`
**Push:** none.
