# G0 final report — UX/UI audit and product experience blueprint

**Goal:** G0 — TrendScout UX/UI audit & product experience blueprint
**Audit date:** 2026-11-13
**Scope:** Read-only repository audit and eleven requested documents under `Documents/UX/G0/`. No G1 implementation started.

## Result

Git snapshot: branch `main`; current GitHub `origin/main` at `d74e062129ffc06ae9edba3e1a0a5631e6f31170`. The initial fetch showed the local baseline three commits ahead; a subsequent fetch observed the remote branch at that same commit. No G0 commit has been pushed.

The G0 deliverables document current screens and route/API truth; proposed information architecture; Collection/keyword workbench; two-tab Dashboard and chart eligibility; full 81-source research inventory versus four runtime descriptors; quota/cache states; Lucian Labs references/license; VI/EN glossary/audit; design-system guidance; and future G1–G7 acceptance/test plan.

## Key findings and decisions

- Frontend is vanilla HTML/CSS/JS and runs locally with persisted data. The current Windows report is dated evidence; its counts are not product fixtures or universal values.
- Overview currently contains the durable keyword explorer. The Advanced Keywords link aliases to Overview, leaving an orphaned panel and unreachable branch. Proposed single home for full ledger is Collections.
- Dashboard should have Overview and Analysis tabs. “Top five hot keywords” is conditional on comparable support and a named ranking basis. One snapshot supports descriptive popularity, not growth or algorithm superiority.
- Runtime catalog has Wikimedia (local LIVE historically verified, not production-ready), YouTube, Google and Bing through SerpApi (offline-tested only; not configured, approved or live-verified). The strategic inventory has 81 candidates, not 81 integrations.
- Source UI should use backend capability data. Quota/cache displays use sourced provider/runtime facts; unknown stays unknown and saved timestamps remain unchanged.
- Local classification uses CPU FastEmbed 0.9.1 and `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`. Local embeddings are active; Settings’ LLM provider is disabled/unconfigured. No cloud LLM usage is evidenced.
- Product Research is a future workflow; scheduler and AI usage are not current verified capabilities. Keep them proposed/unavailable until backed by real contracts and measurements.
- VI/EN catalogs have matching 614-key sets. This proves key parity only; hard-coded English metadata/status literals and full rendered-copy quality remain future audit work.
- Lucian repository currently describes 400 entries and has an MIT license. Adapt interaction patterns only; retain attribution/license and discard demo content/data.

## Acceptance gates

| Gate | Verdict | Evidence / limitation |
| --- | --- | --- |
| G0.1 Git baseline | PASS | At initial audit, local HEAD `d74e062` was 3 commits ahead of `origin/main` `aca450a`, 0 behind; working tree/staging were clean. A later final-review fetch showed GitHub `origin/main` now at `d74e062`, matching the preserved baseline. No push was issued from this G0 task; local analytics history was not rewritten. |
| G0.2 Existing screens/flows | PASS | Static audit covers shell, 11 `data-view` screens, privacy/terms, Fetch/run/Collection/algorithm flows. Existing Windows screenshots were referenced, not recaptured. |
| G0.3 UI/backend/API truth | PASS | API routers/capabilities compared with UI; mismatches and placeholder states documented. |
| G0.4 New IA/navigation | PASS | Sitemap, journeys and route migration recorded. |
| G0.5 Collections/Unclassified/keyword design | PASS | States, table, evidence drawer, batch workflow and user-decision preservation specified. |
| G0.6 Source/quota/cache matrix | PASS | All 81 inventory candidates included with repo status. Inventory/provider metadata dated 2026-11-11; revalidate before integrations. |
| G0.7 Dashboard/Top 5/analytics | PASS | Two tabs, chart contracts, minimum-data rules and single-snapshot limits specified. |
| G0.8 Lucian research/license | PASS | Repository, catalog, MIT license and selected source links/adaptation rules documented. Recheck at adoption. |
| G0.9 VI/EN, AI, Product Research, advanced pages | PASS | Locale key sets compared; model/runtime facts sourced to current reports; all advanced screens reviewed. Exhaustive in-browser linguistic review remains future work. |
| G0.10 Design system and G1–G7 plan | PASS | Tokens/components/accessibility guidance and staged acceptance/test matrix created. |
| G0.11 No silent contradictions | PASS | Conflicts are explicitly separated: Wikimedia status axes; stale Keywords route; disabled pages; missing roadmap docs. Proposed future state is labeled proposal. |
| G0.12 No out-of-scope code/data changes | PASS | Only `Documents/UX/G0/` is in scope. Final Git review confirms only requested documents before local docs commit. |

## Open decisions / blockers for later goals

1. Approve Collections as the home for taxonomy and the global durable keyword ledger.
2. Define “hot”/Top 5 metric, minimum support and validation. Current evidence cannot establish growth or a winner.
3. Reconcile `approval_status=PENDING_LIVE_APPROVAL` with Wikimedia local historical verification without changing enablement policy.
4. Confirm the 81-entry future library and assign a data/policy steward. Review rights, quota, retention, geography, freshness and attribution before source integration.
5. Decide whether Runs gets a list API or another verified history contract; current UI requires ID entry.
6. Decide whether disabled Refresh/Usage pages remain as roadmap placeholders or are hidden in a later goal.
7. Obtain absent roadmap documents if authoritative reconciliation is needed.
8. Recheck source provider policies/quota; inventory research was reviewed 2026-11-11.

## G1 recommendation

Start with **G1 — Shell and information architecture** in [the acceptance matrix](ACCEPTANCE_MATRIX_G1_G7.md). Resolve open item 1, move the explorer to Collections while preserving a safe old-route redirect, establish semantic Dashboard tabs, and align Source Center to runtime capabilities. Do not include live-source, scheduler, commercial-research or scoring changes in G1.

## Deliverables

1. [CURRENT_STATE_AUDIT.md](CURRENT_STATE_AUDIT.md)
2. [INFORMATION_ARCHITECTURE.md](INFORMATION_ARCHITECTURE.md)
3. [UX_UI_DESIGN_SPEC.md](UX_UI_DESIGN_SPEC.md)
4. [SOURCE_CATALOG_AND_QUOTA_MATRIX.md](SOURCE_CATALOG_AND_QUOTA_MATRIX.md)
5. [COLLECTION_AND_KEYWORD_UX.md](COLLECTION_AND_KEYWORD_UX.md)
6. [DASHBOARD_AND_ANALYTICS_SPEC.md](DASHBOARD_AND_ANALYTICS_SPEC.md)
7. [LUCIAN_COMPONENT_RESEARCH.md](LUCIAN_COMPONENT_RESEARCH.md)
8. [I18N_GLOSSARY_AND_AUDIT.md](I18N_GLOSSARY_AND_AUDIT.md)
9. [DESIGN_SYSTEM_SPEC.md](DESIGN_SYSTEM_SPEC.md)
10. [ACCEPTANCE_MATRIX_G1_G7.md](ACCEPTANCE_MATRIX_G1_G7.md)
11. This report.
