# Dashboard and analytics specification

**State:** Proposed; G0 does not change chart or data contracts.

## Dashboard tabs

### 1. Overview

Show snapshot state (live/cached/partial/stale/failed), source scope and observation date. Summary cards use explicit definitions: unique persisted keyword records, observed candidate count, actually usable source count, classified/unassigned counts, last completed Fetch. Hide unavailable values rather than substituting zero.

**Top five** is conditional. Name one supported ranking basis, such as observed Wikimedia approximate pageviews or a versioned/evaluated trend method. Show source, unit, observation date, cohort size, quality flags and concise “why shown” evidence. Do not call a rank “hot” without a growth baseline. If fewer than five supported items exist, show count and reason; if none, explain why.

### 2. Analysis

Shared filters: source, scope/market, period, metric, algorithm family and result state. Never combine incompatible units or populations.

| Analysis question | Visualization | Metric/minimum-data rule | Interpretation |
| --- | --- | --- | --- |
| Keyword counts by source | Sorted horizontal bars | Distinct keyword IDs under documented dedup scope; source and period stated | Coverage in this sample, not market share |
| Algorithm effectiveness | Evaluation table first; grouped bars only for comparable labeled metrics | Same holdout, target, horizon, baseline and version; sample and uncertainty shown | No winner before comparable evaluation; current state `NOT_YET_EVALUATED` |
| Source effectiveness | Outcome table or stacked bars | Requests/records accepted/rejected/partial/errors with denominator and period | Operational yield, not business value or trend quality |
| Multi-source coverage | Source-by-keyword matrix/UpSet only when >=2 compatible sources exist | Shared canonicalization and comparable observation window; keep metrics separate | Sample overlap, not validation by itself |
| Keyword change over time | Line chart with points and visible gaps | Algorithm-defined support; same source/scope/metric/unit/window; no imputation | Missing/censored differs from zero; disclose baseline and window |

Avoid pie charts. Heatmaps are suitable for source/cohort intersections if readable. Scatter plots require defined axes/units and sufficient observations. Visual intensity must not imply unsupported confidence.

## Chart contract

Every chart states purpose/title, metric definition/unit/aggregation, source/population/scope, window/timezone, filters/cohort, minimum-data rule, uncertainty/quality limits, missing-data encoding, provider vs derived status, accessible labels and equivalent table/text summary.

## Trend and Top 5 eligibility

For each candidate preserve method/version, current and baseline windows, support count, scope fingerprint, missing/censored observations, numeric result and quality flags. One snapshot permits descriptive observed-popularity ordering only. Do not derive growth from absent historical days or treat top-list absence as zero. Do not compare Wikimedia `views_ceil` with SERP/YouTube `result_rank`. Confidence is not probability unless calibrated and validated.

## Current evidence

Backend exposes algorithm catalog/results and durable Wikimedia evidence; the Windows report records 1,848 results, where two ranking methods completed for 150 records each and skipped 4 each; other methods skipped for insufficient history or incompatible metrics. The sample result is approximate popularity/within-snapshot percentile, not a trend. These are dated audit facts, not static chart fixtures.
