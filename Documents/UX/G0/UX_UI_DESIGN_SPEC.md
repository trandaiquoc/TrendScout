# UX/UI product experience specification

**State:** Proposed design contract for G1+. Product owner approval and API review remain required before implementation.

## Product principles

1. Label every number as provider-reported, TrendScout-derived or unavailable.
2. Show source scope, metric unit, observation time/window and support status at the point of interpretation.
3. Distinguish missing, partial, stale, cached, live, failed, rejected and insufficient states.
4. High volume/rank alone does not establish a trend. Growth requires comparable history and a documented baseline.
5. User decisions take precedence over automatic suggestions.
6. Dense research tables remain scannable, keyboard-operable and usable on small screens.

## Dashboard

Header: title, snapshot state, freshness and explicit Fetch action (enabled only by backend readiness). Use two semantic tabs:

- **Overview:** snapshot date, persisted keyword count, observed candidate count, usable source count, eligible/classified/unassigned counts when known, and last completed Fetch. Top five appears only with five comparable observed records and a named ranking basis. Otherwise show “Not enough comparable evidence” with observed count.
- **Analysis:** shared source/date/scope filters; panels for source record counts, algorithm applicability/results, source outcomes, cross-source coverage and time series. Every chart includes purpose, unit, source, window, filter, minimum-data rule, interpretation and equivalent table.

“Hot” requires a versioned rule with metric, observation window, baseline, minimum comparable observations and validation. Without support, say “Insufficient history.” Never use fabricated sample charts. Pageviews, SERP ordinal rank and video result rank remain separate measures.

## Collections and keyword workbench

- Desktop: collapsible Collection tree/filter rail and main work area. Mobile: Collection tree in filter drawer.
- Main filters: All, Unclassified, Suggestions, Ambiguous/Unknown, Classified, Excluded; query, Collection path, source, observation window, metric availability, live/cache/partial provenance.
- Search covers durable ledger, not only current page; debounce locally and do not make provider requests.
- Toolbar: select visible page or all N matching rows; state exact scope; bulk action and destination; disable invalid combinations; announce results/errors.
- Table: checkbox, Keyword, Topic/Collection, Source, Evidence/metric, Last observed, Classification/assignment state, one Details action. Preserve evidence meaning on mobile.
- Detail drawer: original/canonical text, source records and query relationships, metric/unit, observation and collection times, scope, partial/cache/live provenance, classification evidence/version, assignment origin, user feedback/history.
- States: Classified; Suggested; Ambiguous; Unknown/insufficient context; Not in taxonomy; Needs review; Excluded. Unclassified is a filter/inbox, not a claim of impossibility. Retry only when new evidence or explicit user action justifies it.
- Keep per-row controls minimal. Put add/copy/move/remove/approve/reject/exclude/restore in batch toolbar or one row menu. Confirm destructive/broad actions with impact and count.

## Source Center

Two sections: **In use** lists only sources usable in the current runtime; **Source library** lists future candidates with lock and reason. Backend capability data is the source of truth.

Source detail: stable source/provider ID, family, adapter state, account/config state (never show secret), policy approval, local LIVE verification date, current health, production readiness, metric/unit/meaning, market/language filters and limits, collection/observation time, last Fetch, partial/truncation, cache state/age, quota state, retry window, attribution/retention note. Use “Unknown” where API has no value.

Quota states: known total+remaining; daily limit; rate-limited only/total unknown; unpublished; exhausted; temporary failure; cache-only fresh; cache stale; no cache. No percentage without a known denominator. When exhausted, obey admission policy; show prior cache with original timestamps and label. Cache is not live.

## Runs, algorithms and quality

- Runs list is chronological and backend-backed; show status, start/finish, sources, observed/retained counts, partial reason, classification and algorithm stage. Detail links to evidence. ID lookup is secondary.
- Algorithms: purpose and “When it can run”; status summary; method/version; input metric/unit and scope; history requirements; result value/unit/cohort, skipped/failed reason, evidence and limits. Keep `NOT_YET_EVALUATED` when no labeled evaluation exists.
- Quality: factual freshness, partial/truncated coverage, missing/invalid, duplicates and source errors with numerator, denominator and window. No unsupported aggregate quality/confidence score.
- Refresh: future backend-owned schedule with next run, queue/lease, outcome, pause/resume, budget and idempotency. It must not rely on a browser tab.
- AI usage exists only if real inference calls/tokens/cost are metered. Describe local embeddings separately from LLM usage.

## Product Research and Settings

Product Research stages are draft evidence, not commercial truth: signal; hypothesis; niche scope; market/competitor evidence; trademark/IP review; commercial relevance; human assessment; saved conclusion. Each metric stores source/method. “Not assessed” differs from “negative.” No sales or demand inference from pageviews, rank or one source.

Settings covers locale, theme, effective runtime mode, model actually present, purpose, status and device/runtime constraints. Provider credentials stay server-side; do not ask for them in browser.

## Interaction and accessibility

- Target WCAG 2.2 AA: visible keyboard focus, logical headings, semantic tabs/tables/dialogs, labels, keyboard selection, focus management, contrast, non-color status, announcements.
- Charts provide keyboard-accessible values and equivalent table/summary. Dates include timezone; numbers use locale-aware formatting without hiding uncertainty.
- Loading uses stable skeletons and honors `prefers-reduced-motion`. Empty search suggests which filters to clear and preserves query. Retry only when backend admission allows it.
- Explain “partial”, “cached”, “rank”, “unknown” and “insufficient history” inline; technical details may expand.
- Responsive target starts at 320px; no page overflow; toolbar wraps/stacks and tables have an accessible mobile presentation.

## Explicit non-goals

No G0 code/API/schema/data changes, new source integration, changed scoring, scheduler, provider calls, paid AI, commercial values, deployment or external account work.
