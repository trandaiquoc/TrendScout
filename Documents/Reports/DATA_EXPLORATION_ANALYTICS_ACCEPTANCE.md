# TrendScout data exploration and analytics — implementation status

**Status:** in progress; not a final acceptance pass
**Reviewed:** 2026-11-13
**Scope:** persisted local keyword Collections and the mandatory trend-algorithm-engine addendum. No deployment, push, or provider Fetch was performed for this review.

## Implemented in this change set

- Automatic local embedding assignment operates over every eligible persisted keyword without filtering by the browser's current page. Existing membership pins and explicit user removals/rejections are respected. Collection create/edit reclassifies unresolved persisted keywords from cached embeddings. Multiple strong memberships can be stored; ambiguous matches remain for review. Collection creation remains `SUGGEST` by default and AI matching for existing Collections is enabled by default.
- Manual Collection workflow includes a form for context and positive/negative examples, per-Collection AI matching controls, inbox selection, bulk add, approve/reject actions, and membership remove/copy/move controls. Assignment origin remains visible as user or local AI.
- The local Fetch processing path now stores dated source observations and versioned algorithm results in the existing private local SQLite store. It runs the registered catalog across all eligible persisted keywords, not just rows awaiting Collection assignment. An input digest prevents recalculation when observations and algorithm versions are unchanged. The result endpoint returns per-method status totals and a bounded page of result evidence.
- The Algorithms page reads the backend registry, filters by family/source/availability, shows source-capability badges, output contracts, requirements, limitations, and representative persisted results. It does not claim an algorithm winner or a cross-metric comparison.
- The persisted dataset explorer restores from the private SQLite ledger on startup/reload, supports full-ledger search and state/Collection/source/sort filtering, and performs page or all-filtered bulk actions server-side. It exposes assignment provenance and an explicit excluded state. The search endpoint returns results outside the first 100 visible rows.
- Fetch snapshot metadata distinguishes the latest provider batch from the full durable ledger and warns when historical coverage is unknown. The browser labels the recorded latest batch (1 row) separately from the stored ledger (214 rows); it does not imply the one-row observation refreshed or covers all historical rows.
- The multilingual local-embedding acceptance replay uses the installed model and a temporary SQLite database. It creates a blank “Smart Home” Collection, places the robot-vacuum keyword after 104 unrelated persisted records, checks an unrelated Mars keyword, rejects an incorrect membership, reopens the store, repeats Fetch, and confirms cached reclassification. This does not modify the user's existing database.

## Algorithm decision matrix

| Family / candidate | Decision | Current evidence and boundary |
|---|---|---|
| Wikimedia legacy score | Reuse existing | Preserved as a versioned compatibility method; its heuristic score is not an evaluated probability. |
| Growth ratio and rolling median | Reuse existing | Require compatible observed history. “Not in top list” remains missing/censored rather than zero. |
| Observed volume rank and percentile | Implement | Execute within one persisted Wikimedia snapshot cohort. Outputs are conditional on the observed top list and carry censoring/approximation flags. |
| Relative source rank | Implement contract | Supports query-scoped ordinal `result_rank` sources with matching provider/query scope. Wikimedia volume data is incompatible; configured search providers are not live. |
| Rolling mean and relative change | Implement | Require complete compatible daily history and positive baseline. No fabricated output for gaps or censored Wikimedia history. |
| Theil–Sen slope | Implement | Median pairwise slope; complete equally spaced numeric series only. No inferential interval. |
| Kendall monotonic direction | Implement | Kendall tau-b calculated for complete series; descriptive only, no significance test. |
| Robust Z / MAD and EWMA | Reuse existing | Retained with their warmup and coverage checks; current censored Wikimedia top-list snapshots are incompatible by default. |
| Rank delta, persistence, top-N entry/reappearance | Defer | Reliable snapshot-level rank history and explicit coverage of absent entries are not yet persisted. A missing top-list row cannot establish a zero rank or a first appearance. |
| CUSUM / Page–Hinkley | Defer | The current source has no validated in-control target or sufficiently long comparable series for calibrated shift monitoring. NIST's control-chart procedures depend on a process reference and monitoring assumptions. |
| PELT change points | Defer | No suitable long, complete time series is available in this source ledger; penalty and minimum-segment choices would be arbitrary without one. |
| Event-stream burst detection | Not applicable | Wikimedia provides daily aggregates rather than event arrival times needed for a Kleinberg-style burst stream. |
| STL seasonality / recurrence | Defer | There are not enough comparable seasonal cycles or retained daily histories to estimate recurring components. |
| Multi-source agreement / rank consensus | Defer | Search and video adapters are offline-only and use different metrics/scopes; there is no supported live, independently comparable pair. Metrics are not summed. |
| Algorithm backtest / winner selection | Not yet evaluated | No trustworthy historical labels or compatible labeled backtest are available. Descriptive outputs are not compared on a shared scale. |

## Scientific references reviewed

- [NIST Engineering Statistics Handbook: EWMA charts](https://itl.nist.gov/div898/handbook/pmc/section3/pmc314.htm) and [CUSUM charts](https://www.itl.nist.gov/div898/handbook/pmc/section3/pmc323.htm): historical process assumptions and detection behavior inform the explicit calibration/history gates.
- [SciPy `theilslopes`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.theilslopes.html) and [SciPy `kendalltau`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.kendalltau.html): basis for the robust slope and rank-direction methods; this implementation emits descriptive values only.
- [ruptures PELT guide](https://github.com/deepcharles/ruptures/blob/master/docs/user-guide/detection/pelt.md): offline change-point segmentation requires suitable series, penalty, and segment constraints.
- [statsmodels STL documentation](https://www.statsmodels.org/stable/generated/statsmodels.tsa.seasonal.STL.html): seasonal decomposition needs multiple comparable cycles.
- [scikit-learn `TimeSeriesSplit`](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html): time-ordered evaluation avoids training on future observations.
- [Kleinberg, “Bursty and Hierarchical Structure in Streams”](https://dl.acm.org/doi/10.1145/775047.775061): event-stream burst model; the current daily aggregate source does not meet that input contract.

## Verification recorded

- Local model assignment acceptance replay: **PASS** — 106 persisted candidates; the late robot-vacuum row was assigned, Mars remained unassigned, rejection survived repeat processing and store reopen, a later Collection classified cached embeddings, and default collection creation mode was `SUGGEST`.
- Algorithm engine numerical and compatibility tests: **PASS** — complete-series growth and direction methods, ties in snapshot popularity, censored-source rejection, catalog capabilities, and existing Wikimedia strategies.
- Persisted full-ledger processing: **PASS** — 212 eligible keywords produced rows for all 12 registered methods; observed volume and percentile ran on the snapshot cohort; replay executed zero unchanged keywords and made no additional embedding calls.
- Backend suite: **PASS** — 714 tests, 2 skipped, including the registry/results API, partial-snapshot compatibility, persisted dataset search, full-filter bulk selection, exclusion durability, and loopback-guarded endpoint coverage.
- Native Chromium inspection after server restart: the dashboard restored the persisted ledger and Collections from same-origin GET requests; searching “Maren Morris” returned the row found after the initial 100 alphabetical rows. The startup request log contains no Wikimedia/provider request. The search table exposes its AI Collection provenance, and the overview shows latest batch = 1, ledger = 214, and prior coverage unknown. Screenshots: [ledger overview](evidence/data-exploration/native-chrome-ledger-overview.png) and [persisted search result](evidence/data-exploration/native-chrome-persisted-search.png).
- Existing user database was inspected strictly read-only: 214 keyword rows, 141 membership rows, one Collection, and zero `keyword_decisions` rows. The latest recorded snapshot row is `COMPLETED` with provider/retained batch count 1; ledger coverage remains mixed/unknown. This session did not invoke Fetch or write user memberships, keyword decisions, or keyword rows.

## Remaining acceptance gates

This report is not an end-to-end project PASS. The dataset explorer and native-Chrome restart/search review are implemented and verified, but the parent goal's remaining project-wide acceptance checks are not complete. Existing user Collection records were intentionally preserved and not reconciled; the observed 141 memberships may predate this workflow, and the zero decision rows mean this inspection cannot establish prior user intent. A separately reviewed migration/reconciliation is still needed before those legacy memberships can be treated as reconciled. The Wikimedia source remains one provider with censored top-list data, and algorithm evaluation remains `NOT_YET_EVALUATED` because compatible labeled historical data is unavailable. Do not infer whole-ledger freshness from the latest one-row snapshot.
