# Proposed acceptance matrix for G1–G7

Future work packages derived from G0; not started or approved implementation. G0 remains documentation-only.

| Goal | Scope | Dependencies | Acceptance criteria | Test plan |
| --- | --- | --- | --- | --- |
| G1 — Shell and information architecture | Navigation, two-tab Dashboard shell, routes/redirects, tokens and accessibility baseline | Product decision to place global ledger in Collections; preserve vanilla frontend boundary | No orphaned routes; semantic keyboard tabs; responsive nav; VI/EN and themes; no Fetch on route/reload | Route checks; browser keyboard/mobile/theme/locale; contrast/accessibility scan |
| G2 — Collections keyword workbench | Durable search, Collection tree, Unclassified triage, evidence detail and batch toolbar | G1; confirm existing API state semantics | Search covers persisted ledger; all-filter count exact; no default assignment; preserve pins/feedback/exclusion; clear partial/error states | API/browser cases for missing vs zero, page/all selection, ambiguous/unknown, assignment, reject, exclude/restore, reload persistence |
| G3 — Source Center | Dynamic active capabilities and locked 81-candidate library | G1; current catalog truth and product/legal review | Only usable sources in “In use”; offline differs from LIVE; local historical verification differs from current health/production; no invented quota; cache age shown | Descriptor fixtures for status axes; offline/disabled/live and quota/cache matrix; VI/EN/accessibility |
| G4 — Dashboard evidence/charts | Overview/Analysis, Top 5 eligibility and accessible visualizations | G1/G3; approve metric definitions/support rules | Chart states unit/source/window/cohort; no incompatible comparisons; missing != zero; unsupported Top 5 is explained | Permitted fixtures for partial/missing/stale/incompatible scope/ties/<5; chart-table parity |
| G5 — Run history/diagnostics | Chronological list/detail; ID lookup secondary | Backend list contract or verified history source; G1 | Find run without ID; partial/failure/source coverage preserved; stale not fresh | Pagination/status contract; completed/partial/failed/empty; restart persistence; no accidental Fetch |
| G6 — Algorithms/data quality | Plain-language applicability/evidence and factual quality checks | G2/G4; backend outputs remain source of truth | Skip reason/support visible; `NOT_YET_EVALUATED` honest; no unsupported aggregate score | COMPLETED/SKIPPED/FAILED; baseline insufficient; invalid/missing/duplicate/stale/partial; accessibility |
| G7 — Research, settings and operations | Research workflow/records, actual model/runtime, scheduler/usage only when backed | G1–G6; separate product/legal/provider/ops approval | No fictional commercial/AI/quota facts; no browser secrets; scheduler server-owned; usage only when measured | Contract tests for unavailable/configured/instrumented states; negative tests for fabricated values/provider/browser credential handling |

## Cross-goal gates

- Preserve API/schema/data contracts unless separately approved; version changes and provide migration/recovery plan.
- No live provider request in automated tests; use explicit mock transport/fixtures, label replay/synthetic data.
- Search/filter makes no provider request. Fetch is explicit and backend-admitted.
- Capture fresh browser evidence only for implemented work; identify environment/date; do not reuse old screenshots as new.
- Check locale parity, keyboard flow, responsive behavior, no page overflow, reduced motion and chart alternatives.
- Reconcile docs with code/reports; record unresolved questions rather than silent defaults.
