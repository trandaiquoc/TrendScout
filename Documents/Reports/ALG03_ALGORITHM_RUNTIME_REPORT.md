# ALG03 — Algorithm runtime integration

Status: verified implementation and compatibility inventory for baseline `50af984`.
No provider or OpenRouter requests are part of this verification.

## Source compatibility matrix

| Input profile | Metric, unit, geography and time shape | Algorithm compatibility | Runtime and policy result |
|---|---|---|---|
| Wikimedia Pageviews | `views_ceil`, `approx_pageviews_upper_bound`; US country-scoped, daily top-list snapshots; censored membership; current snapshot plus six prior days under the seven-day retention contract. | Wikimedia Trend, Growth Ratio, Median Baseline, Observed Volume Rank, Observed Percentile Rank. | Four methods persist via the Wikimedia input projection. Legacy Trend remains on its versioned dataset path. No absent page is treated as zero. |
| Google Trends Trending Now | `search_volume` is a provider-reported approximate bucket; `increase_percentage` is provider-reported; US region, English filter; 24-hour cluster snapshot (RSS may omit metrics). Stored as `google_web_serp` with `data_source=GOOGLE_TRENDS`. | None of the 12: bucket and percentage are not a complete daily numeric series, and the cluster is not a comparable result-rank cohort. | Source ID is shared with Google Search, but this record profile is separate from SERP rank. No algorithm is dispatched for it. |
| Google Search / SERP | `result_rank`, `ordinal result position`; Google `gl=us` / `hl=en` localization; query-scoped result sample, not search volume or a daily series. | Relative Source Rank is mathematically valid only within a same-provider, same-query, same-snapshot cohort. | Current catalog is offline/mock only and the Collection store has no durable full SERP rank cohort. No persisted algorithm path. |
| Bing / SERP | `result_rank`, `ordinal result position`; `mkt=en-US` localization; query-scoped sample, not search volume or a daily series. | Relative Source Rank only with a persisted, comparable single-query cohort. | Current Fetch stores candidate verification evidence, not a comparable result cohort. Adapter is offline/mock only; no persisted algorithm path. |
| YouTube Search | `result_rank`, `ordinal result position`; query-scoped video results; US `regionCode` indicates viewability, not author/viewer residence. | No derived algorithm under the current source policy. | Keyword extraction and derived metrics are disabled; Relative Source Rank explicitly excludes this source. |

Google Trends and Google Search share the `google_web_serp` persistence source ID. The
`data_source` discriminator is required to interpret Google records; the source catalog's
`result_rank` contract describes SERP records, not the Trends cluster metrics.

## Algorithm inventory

| Algorithm | Input contract and availability |
|---|---|
| Wikimedia Trend `v0.1` | `views_ceil` → legacy `trend_score` / `points_0_100`; Wikimedia only, seven-day descriptive heuristic; legacy dataset path, not persisted in `algorithm_results`. |
| Growth Ratio `v0.1` | Wikimedia approximate pageviews → current / median of at least three observed prior days; dimensionless, persisted when valid. |
| Median Baseline `v0.1` | Wikimedia approximate pageviews → conditional median of at least three observed prior days; same input unit, persisted. |
| Robust Z-MAD `v0.2` | Complete uncensored daily numeric history, minimum 14 days and nonzero MAD; no compatible source and exceeds seven-day retention. |
| EWMA `v0.2` | Complete uncensored daily numeric history, minimum seven prior days plus current; current Wikimedia projection has six prior days and is censored. |
| Rolling Mean Baseline `v1` | Complete uncensored daily numeric series at 7/14/30 days; no current source supplies this input. |
| Relative Change `v1` | Same complete daily series windows; positive mean baseline required; no current compatible source. |
| Theil-Sen Slope `v1` | Equally spaced complete daily series including current; no current compatible source. |
| Kendall Trend `v2` | Complete daily series including current; no current compatible source. |
| Observed Volume Rank `v4` | Wikimedia approximate values in the same dated, country/access/project snapshot cohort; persisted, explicitly top-list conditional. |
| Observed Percentile Rank `v4` | Same Wikimedia snapshot cohort and censoring; persisted within-sample percentile only. |
| Relative Source Rank `v2` | Ordinal positions within one provider/query/snapshot cohort; no such durable cohort exists. YouTube is policy restricted. |

## Runtime and PostgreSQL contract

The collection runner selects persisted algorithms from each registered input
projection and algorithm metadata (source, metric/unit, source capabilities, censoring,
history depth, and runtime cohort requirements). Current projection:
`wikimedia_pageviews_observation_projection`. Future source support requires its own
normalized persisted projection; the shared runner does not infer compatibility from
matching labels alone.

PostgreSQL `algorithm_results` stores algorithm ID/version, result status and value,
metric/unit, serialized source/scope/window, observation references, input digest and
update time. `algorithm_observations` stores the source-scoped inputs and digests.
No schema change is planned.

## Verification

- Focused algorithm/Collection tests: 24 passed, 10 subtests passed.
- PostgreSQL isolated-schema tests: 2 passed, 0 skipped; the two generated `ts_test_*` schemas were dropped.
- Focused Ruff checks and `git diff --check`: passed.
- Broader Smart Collections suite: 4 failures are AI/model lifecycle expectation mismatches; no AI code was changed.
