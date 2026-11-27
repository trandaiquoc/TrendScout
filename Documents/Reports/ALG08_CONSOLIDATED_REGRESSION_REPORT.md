# ALG08 Consolidated Algorithm Regression

Date: 2026-11-27
Branch: `integration/demo-final`
Baseline: `9fe76e22eabc9402d1d65ee1a26cc5b06ab7770e`
`origin/main` at verification start: `5122e6f0a717f72f2933bea56978c2b66f42139a`

**Result:** ALG08 algorithm and integration regression gates pass. The registry still reports `NOT_YET_EVALUATED`; this is not a predictive-accuracy, production, or end-to-end LIVE acceptance.

## Scope and prior phases

The task baseline records ALG01–ALG07 as PASS. The repository contains the ALG03 compatibility report and the current AI input contract; it has no separate ALG01/02/04/05/06/07 reports. This report therefore uses the current registry, implementation, and regression tests as evidence and does not invent unrecorded phase metrics. No product or algorithm code changed in ALG08.

## Registered algorithm inventory

Catalog snapshot: 13 registered IDs; five persisted source adapters, one legacy-only path, one method without a durable query cohort, and six without a compatible uncensored daily-series adapter under current source/retention limits. All 13 remain registered and have an implementation or explicitly documented legacy path; “registered” does not mean runtime executed or LIVE verified.

| Algorithm | Contract and runtime status |
|---|---|
| `ewma@v0.2` | Complete uncensored daily numeric history; at least 7 prior values plus current. No compatible source; six prior days fit retention. Registered/implemented, not dispatched from current persisted input. |
| `google-trends-increase-rank@v1` | Rank provider-reported increase percentages within one matching US/English 24-hour snapshot; minimum cohort 2. Approximate volume buckets are not inputs. Persisted adapter exists; LIVE derived-metric policy currently gates execution. |
| `growth-ratio@v0.1` | Wikimedia current `views_ceil` divided by the median of at least 3 observed prior days; dimensionless and conditional on the censored top-list. Persisted adapter available. |
| `kendall-trend@v2` | Kendall tau-b over complete, equally spaced prior daily values plus current; windows 7/14/30, matching source scope and unit. No compatible uncensored adapter/retained history. |
| `median-baseline@v0.1` | Median of at least 3 observed prior Wikimedia `views_ceil` values; output preserves `approx_pageviews_upper_bound`. Persisted adapter available. |
| `observed-percentile-rank@v4` | Same-date Wikimedia snapshot cohort, minimum 2; percentile is within the observed top-list, not the market population. Persisted adapter available; censored membership remains explicit. |
| `observed-volume-rank@v4` | Same-date Wikimedia snapshot cohort, minimum 2; ranks approximate observed values and ties deterministically. Persisted adapter available; not a market-wide rank. |
| `relative-change@v1` | `(current - preceding-window mean) / mean`; positive baseline and complete uncensored 7/14/30-day prior series required. No compatible source/retained history. |
| `relative-source-rank@v2` | Ordinal SERP rank is comparable only inside one provider/query/snapshot cohort, minimum 2. No durable cohort adapter; YouTube remains excluded. |
| `robust-z-mad@v0.2` | Robust standardized deviation requires at least 14 prior uncensored daily values and nonzero MAD. No compatible source; retention supplies six prior days. |
| `rolling-mean-baseline@v1` | Mean of every preceding day in a 7/14/30-day window with matching scope/unit. No compatible uncensored source/retained history. |
| `theil-sen-slope@v1` | Median pairwise daily slope over complete equally spaced prior observations plus current; windows 7/14/30, capped at 31 observations. No compatible source/retained history. |
| `wikimedia-trend@v0.1` | Existing versioned 0–100 descriptive heuristic on Wikimedia dataset input, with top-list/missing-coverage limits. The catalog requires 3 observed baseline values within the seven-date baseline; the legacy path is not persisted in `algorithm_results`, and partial history remains visible. |

Source compatibility remains source-specific: Wikimedia `views_ceil` is an approximate, censored country-scoped daily top-list; Google Trends provides a one-day snapshot and provider-reported metrics, not a complete daily series; Google/Bing SERP positions are not search popularity; YouTube is policy restricted. The catalog marks Google/Bing search adapters offline/pending and Google Trends live execution policy gated. Tests in this run used fixtures, not LIVE provider calls.

## Consolidated contracts

- Keyword identity applies Unicode NFKC, HTML entity decoding, case folding, whitespace normalization, and typographic quote/dash equivalence while preserving meaningful punctuation. Exact cross-source duplicates retain durable IDs and source evidence; related phrases stay separate, and known conflicting entity IDs are not merged.
- Wikimedia observed rank/percentile uses deterministic same-snapshot cohorts. Assessment `sample-presence-v2` uses 3 current plus 4 baseline days, exact source/scope/parser/mode, complete observable coverage, and minimum multi-day support. Missing, partial, expired, censored, or unobservable coverage is insufficient rather than zero. Geography remains `US_COUNTRY_SCOPED`, `US_FILTER_ONLY`, `GLOBAL`, or `UNKNOWN` from evidence.
- Multi-source profiles retain metric/unit, source/platform/role, independent evidence groups, geography, freshness, and limitations separately. Verification evidence is not a trend-demand signal; only comparable contracts can conflict/corroborate. No composite score is synthesized.
- PostgreSQL algorithm results preserve version, source/scope, input digest, observations, and evidence lineage. Active evidence expiry and the rolling seven-day window suppress stale algorithm results. Incremental observations, unchanged digest reuse, retention boundary/expiry, concurrent access, idempotency, and metadata-only dataset reuse were covered by the selected PostgreSQL/regression cases.
- ALG07 AI inputs remain task-specific. Classification groups canonical phrases by mode and entity identity without algorithm history; recommendations receive bounded source-specific algorithm/evidence profiles. Expired evidence, raw provider payloads, and unapproved source transfers are excluded. Durable IDs, user assignment/pin protections, input digests, and deterministic ordering are preserved. Fake providers were used only in tests; no inference ran.

## Verification

The consolidated `pytest.exe` run covered `test_algorithm_engine.py`, `test_algorithm_evaluation.py`, `test_discovery_d5.py`, `test_distributed_discovery.py`, `test_keyword_identity.py`, `test_multi_source_profile.py`, `test_source_lineage.py`, `test_postgres_collection_store.py`, `test_postgres_collection_reads.py`, `test_postgres_retention.py`, `test_retention_policy.py`, `test_postgres_ai_engine.py`, and the algorithm/cache cases in `test_smart_collections.py` (`test_full_record_input_and_cache_reuse`, `test_analysis_cache_invalidates_for_observation_result_and_registry_changes`, `test_repeated_observation_avoids_writes_but_changed_evidence_is_saved`, and both digest compatibility cases).

- Consolidated result: **109 passed, 41 subtests passed, 0 failed, 0 skipped** in 107.23 seconds.
- Additional focused checks: **21 AIServiceTests passed**, **1 cache-only collection reuse test passed**, and **2 metadata-only manifest/dataset reuse tests passed**; these used fake/offline paths only.
- Known AI lifecycle probe: 8 passed, 1 failed, 35 deselected; the failure and exact assertion are listed under Deferred AI01 findings.
- PostgreSQL URL came from `.env` `DATABASE_URL_UNPOOLED` and was supplied only as `TRENDSCOUT_TEST_POSTGRES_URL`. PostgreSQL tests used fixture-owned `ts_test_*` schemas; fixtures drop them. No production schema, migration, or provider was used.
- No Python source file changed in ALG08. Ruff on the ALG07 AI-input/PostgreSQL contract files passed. A broader read-only Ruff check over unmodified algorithm and test modules reported existing findings; they were left untouched as out of scope. `compileall` passed.

## Deferred AI01 findings

- DB07 records four Smart Collections AI/model lifecycle expectation failures but does not include their test names or assertion details. ALG08's focused lifecycle recheck covered nine related cases: eight passed, one currently reproduces below. The other three historical failures were not reproduced in that selected set; no details are inferred for them.
- Current reproducible failure: `test_smart_collections.py::SmartCollectionsTests::test_deferred_fetch_model_keeps_raw_keyword_and_skips_embedding` expects `fetch_jobs.error_code=MODEL_INFERENCE_DEFERRED`; actual is `AI_DISABLED`. Both report `MODEL_UNAVAILABLE`, retain the keyword, and skip the legacy embed call. Defer the lifecycle/status contract to AI01; not changed here.
- ALG07's OpenRouter mock assertion: `test_ai_engine.py::OpenRouterAdapterTests::test_paid_price_or_auxiliary_price_fails_closed` expects `AI_MODEL_UNAVAILABLE` for an active endpoint with auxiliary `image=0.01` pricing. The adapter classifies any nonzero/unverified endpoint price as `AI_PRICING_UNVERIFIED`. This mock-only expectation/contract needs AI01 review; no real OpenRouter request was made.

## Acceptance gates

| Gate | Status | Evidence |
|---|---|---|
| G1 — Algorithm contracts | PASS | Registry count, versions, mathematical requirements, source dispatch, and insufficient-history states above; algorithm engine/evaluation tests pass. |
| G2 — Keyword and ranking | PASS | Identity, extraction, deterministic ranks, 3+4 assessment, missing/partial/censored coverage tests pass. |
| G3 — Multi-source analysis | PASS | Geography, unit compatibility, independence, conflict, and evidence limitation tests pass. |
| G4 — PostgreSQL and retention | PASS | PostgreSQL persistence/lineage/incremental, cache reuse, expiry, idempotency, concurrency, and metadata-only tests pass with no skips. |
| G5 — AI input integration | PASS | Focused AIService and PostgreSQL AI tests pass for bounded payloads, transfer policy, active evidence, digests, and user-state protection; deferred lifecycle assertion is outside this gate. |
| G6 — Verification and delivery | PASS | Consolidated tests, PostgreSQL no-skip result, Ruff on the AI-input/PostgreSQL contract files, compileall, diff check, report, and remote SHA verified. No ALG08 Python file changed. |

Predictive accuracy remains unverified: catalog evaluation is `NOT_YET_EVALUATED`; synthetic/unit tests establish implementation contracts only. The run establishes neither real-world calibration nor production/LIVE acceptance.
