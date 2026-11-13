# TrendScout G2–G7 overnight implementation report

**Date:** 2026-11-14 (Asia/Bangkok)  
**State:** Local implementation checkpoint; not pushed or deployed  
**Overall acceptance:** **PARTIAL - approximately 86%**. G2 replay and most offline/local portions of G3-G7 are implemented. G4 analysis is table-first rather than chart-complete, G5 manual-run listing is bounded and awaits emulator verification, and scheduler operation remains off by default and limited to explicitly configured local synthetic Fetch. Full product acceptance is still outstanding.

## Git baseline and safety

- Resumed by fetching `origin` and inspecting status, staged/unstaged diffs, and recent history. `origin/main` was `8349a17` (`fix(ui): explain unavailable merge destinations`). Local checkpoints through `5e37027` (`feat(history): include persisted manual analysis runs`) were preserved; later verified scheduler and UI changes are staged for a separate local commit. No remote changes were pulled over local work.
- Changes in this report are committed locally after focused verification. No push or deploy was performed.
- No Pageviews Fetch, Google/Bing/YouTube API call, paid LLM call, or scheduled provider Fetch was made during this goal.
- The configured user database was backed up before G2 reconciliation to `%LOCALAPPDATA%\TrendScout\backups\overnight-g2-g7\collections-20261114-020943.sqlite`; SHA-256 `4662079f510ff754a81032a747dc260f2e846fac08177e8c8685f8219e1da0d7`. The backup's integrity check was `ok`, and its foreign-key check was empty. The baseline manifest with exact membership and user-decision state remains outside Git.
- The current database was inspected for read-only analytics. No database reset or D6 ledger change occurred. Research feature tests use temporary databases.

## Phase acceptance

| Phase | State | Implemented and evidence | Remaining acceptance gap |
|---|---|---|---|
| G2 — contextual classification and Collection quality | **PASS for the local replay scope** | Versioned `contextual-classifier-v1.2` policy is consumed by the local classifier; taxonomy is `topics-v1.4`; bounded official Wikipedia/Wikidata context lookups were used for uncached/stale saved keyword context; the 154-row reconciliation preserved all observations and manual decisions. | The store has only 154 eligible records, so this is acceptance for this actual local dataset, not a claim about future records. Six records remain without a Collection by design. |
| G3 — Source Center | **PASS for registry and research inventory UI** | Runtime source descriptions are backend-driven. The UI separates integration/configuration/approval/local-live/production status and states quota as unknown when it is not measured. The 81-entry catalog is explicitly research-only and locked, separate from runtime adapters. | Only the existing Wikimedia Pageviews source has persisted observations. Other candidates are not integrated, approved, or quota-verified. Inventory research was last audited 2026-11-11 and needs revalidation before integration. |
| G4 - Dashboard and analytics | **PARTIAL** | Overview shows a persisted comparable-cohort Top 5 chart. Analysis includes a daily observed/unavailable record-count chart and source-level evidence tables. Labels state the measure, cohort and limits; zero, unavailable and missing remain distinct. | The daily chart counts records, not demand or audience. Other requested analytics do not yet have dedicated charts. One-source coverage cannot establish cross-source comparison; unavailable filters remain disabled until compatible data exists. |
| G5 — Run History | **PARTIAL** | Search/status/mode filtering, pagination and details read persistent SQLite `fetch_jobs`, stored snapshots and the AnalysisStorage run ledger. A bounded registry contract returns up to 100 newest manual runs; a truncated-history flag is returned at the cap. Duration/source counts remain unavailable when not persisted. Offline fake and Firestore-stub contracts plus API tests pass. | Firestore emulator integration remains unverified in this environment; older-than-100 manual runs are not included in the merged search window. |
| G6 — algorithms and Data Quality | **PASS for the evaluated local snapshot; cross-product review partial** | API reports the actual 12-method registry. Persisted ledger has 154 observations and 1,848 keyword/algorithm results (154 × 12); incompatible methods are skipped with reasons. No effectiveness winner is selected without comparable labeled history. UI localizes names, statuses and reasons and exposes result/evidence detail. | The current single censored Wikimedia snapshot supports descriptive within-snapshot ranking only. It cannot validate growth, persistence, change points or algorithm effectiveness. Full keyboard/accessibility review of every detail state remains outstanding. |
| G7 - Product Research, AI settings and Scheduler | **PARTIAL** | Product Research persists researcher-entered hypotheses and evidence. AI Settings reports the actual local FastEmbed runtime; the LLM is unconfigured. Scheduler schedules and occurrences persist in private SQLite. Operator APIs list/create/enable/disable schedules; timezone/frequency, transactional leases, stable occurrence/run IDs, missed-run skipping, recovery and safe shutdown have offline/mock coverage. Runtime status reports disabled/running state. | Scheduler defaults off and rejects LIVE/cloud/remote configurations; no unattended Fetch ran. The UI has no schedule creation/edit controls. Product research does not independently discover competitors or validate commercial demand. |

## G2 classification and persisted data

The backup manifest records the before-state. Counts below distinguish classification results from Collection membership; no source observations were deleted or regenerated.

| Measure | Before | After |
|---|---:|---:|
| Keywords | 154 | 154 |
| Topic result counts | 125 classified, 11 ambiguous, 18 unknown | 148 classified, 4 ambiguous, 1 unknown, 1 ineligible |
| Collections | 39 | 53 |
| Membership rows | 180 | 211 |
| Unique keywords with a Collection | 125 | 148 |
| Manual/pinned memberships; REMOVE feedback; excluded keywords | 0; 0; 0 | 0; 0; 0 |
| Entity contexts | 150 | context refreshed only when absent/stale; classification used saved context after bounded lookups |
| Embedding cache | 517 vectors | 593 vectors; 76 inferred locally with installed FastEmbed model, no download |
| Algorithm observations / results | 154 / 1,848 | 154 / 1,848; classification replay did not recompute them |

The 25 mandatory titles were reviewed against persisted context and fixture cases. The replay results are:

| Title(s) | Result |
|---|---|
| Dancing with the Stars (American TV series) season 35 | Entertainment → Television |
| SS United States | Places → Ships |
| Children's Games (Bruegel) | Creative Works → Artwork |
| Joran van der Sloot | Person entity; Crime & Justice from saved criminal context; no occupation inferred |
| MediaWiki | Ambiguous: the saved project context identifies a Wikimedia project home page, not verified wiki software |
| Istimewa:Pencarian | `INELIGIBLE`: saved official page metadata identifies namespace `-1` |
| Wikimedia APIs | Technology → Internet Services |
| Tomb of the Unknown Soldier (Arlington National Cemetery) | Places → Monuments & Memorials |
| Home | Ambiguous: saved page context is a project home page, not a topic |
| Neatsville, Kentucky | Places → Settlements |
| Avengers: Doomsday | Entertainment → Movies |
| Usman Nurmagomedov; Ezra Frech; Peter Norman; Sonny Liston; Fernando Mendoza | People → Athletes from saved descriptions/occupation IDs |
| Archive of Our Own | Non-profit Organizations and Internet Services |
| Sofia Franklyn | People → Content Creators |
| Evolution of spiders | Science & Nature → Biology |
| General Sherman (arbre) | Science & Nature → Plants |
| Thank You/en | `UNKNOWN`: main-namespace page has no QID or description; suffix alone is not evidence of a translation page |
| Wikipedia | Media Organizations and Internet Services from saved “online encyclopedia” description |
| U.S. Route 101 | Places → Roads |
| Maura Higgins | People → Performers from saved occupation/context |
| Marsha M. Linehan | People → Health Professionals from saved psychologist context |

The taxonomy retained stable IDs and added evidenced leaves for settlements, monuments/memorials, crime/justice and Internet services. All 25 fixture cases stay unresolved without entity context except when verified namespace evidence determines ineligibility. The resolver preserves original title text and constrains project hosts to an allowlist. FastEmbed is local embedding/ranking support, not a generative LLM; the LLM provider remains unconfigured.

## G3–G7 implementation detail

- **Source Center:** `GET /api/v1/sources/library` returns the dated research catalog. Runtime capability descriptors are composed from backend source capabilities. UI status axes do not imply production enablement or quota knowledge. The catalog's 81 candidates remain non-integrated.
- **Dashboard:** `GET /api/v1/analytics/overview` uses the actual SQLite ledger. Current counts are 154 unique/eligible, 148 classified, 4 ambiguous, 1 unknown, 1 ineligible, 6 unclassified, one source, 154 observations and 1,848 algorithm results. Top 5 is computed from one latest source/project/date/metric/unit cohort and labels its values approximate/censored/partial as applicable. Current cohort is `wikimedia_pageviews_top_per_country`, `en.wikipedia`, 2026-11-11, 148 records. The ranking is observed volume rank, not growth. The Overview bar chart now uses those same persisted Top 5 values; its label says “Country filter” so US scope is not presented as author or reader nationality. The evidence table remains below the chart.
- **Run History:** `GET /api/v1/runs/history` and `GET /api/v1/runs/history/{fetch_id}` combine local persistent task/dataset rows with up to 100 newest `AnalysisStorage` manual runs. Eleven SQLite task rows were visible during read-only verification. The API indicates when manual history is unavailable or reaches its scan cap; missing duration/counts stay unavailable.
- **Algorithm intelligence:** all 12 registered algorithms were present, with result rows already persisted for all 154 × 12 pairs. The two compatible descriptive rank methods have 150 completed and 4 skipped results each; other methods are skipped when the one-snapshot metric lacks required history/compatibility. Dashboard labels now resolve through the backend algorithm registry and VI/EN names; statuses are translated. Effectiveness is `NOT_YET_EVALUATED`; no winner is fabricated.
- **Product Research:** new `/api/v1/research` GET/POST/PATCH endpoints persist researcher-entered product hypotheses, category/niche, competitors, market evidence, IP notes, risks, findings and status. “Validated” means validated by the researcher, not independently verified. No sales, demand, market size or opportunity score is inferred from pageviews.
- **Settings:** runtime inspection reported FastEmbed `0.9.1`, `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, CPU, 593 cached vectors at 384 dimensions, classifier `v1.2`, taxonomy `v1.4`, no LLM provider and scheduler disabled by default. No AI usage charts are shown.
- **Scheduler:** `TREND_SCOUT_SCHEDULER_ENABLED` defaults to `false`; schedule entries default disabled. When explicitly enabled, startup validates an operator token plus local/test synthetic mode with the existing public Fetch policy. The worker is backend-owned, uses a private SQLite schedule ledger and stable occurrence/run IDs, advances missed dates without a catch-up burst, reuses expired leases after restart, and shuts down before the run service. No scheduled Fetch ran during this goal. IANA timezone support uses pinned `tzdata==2026.6`; Python documents `tzdata` as the fallback for systems such as Windows without system IANA data ([Python `zoneinfo`](https://docs.python.org/3.12/library/zoneinfo.html), [tzdata on PyPI](https://pypi.org/project/tzdata/2026.6/)). Schedule create/list/enable/disable is operator-token protected; frontend schedule editing remains open.
- **Demo discovery:** synthetic fixture timestamps now remain behind the status-analysis clock, and a missing processing-grant callback correctly resolves to the empty-grant policy. This restores the demo's honest `INSUFFICIENT_EVIDENCE` response instead of masking it as `UNAVAILABLE`.

## Verification

- Scheduler, run idempotency, settings, and demo regression tests: **34 passed, 8 subtests passed**. Ruff passed on the changed backend modules. Python 3.12 project venv used.
- Full backend suite: **16 failed, 773 passed, 767 subtests passed**. All 16 failures are Firestore emulator tests: no Firebase CLI, Java runtime or `FIRESTORE_EMULATOR_HOST` was available, so the integration tests could not connect. The two demo discovery failures from the preceding run are fixed and now pass.
- Native Chrome offline workspace: passed at 320, 390, 768, 1024, 1440 and 1920 px for locale/theme persistence, all views, read-only API access and XSS handling. The Collection merge picker passed keyboard selection, review-step handoff and Escape dismissal in Vietnamese/English and light/dark in the isolated fixture; no merge was committed and no Fetch occurred.
- Fresh overview screenshots: 24 combinations at six widths, Vietnamese/English and light/dark are in `Documents/Reports/windows-evidence/overnight-g2-g7/`. Screenshots use an isolated synthetic browser fixture. Five earlier G2 evidence images are also retained in that directory.
- Offline Chrome visual/workspace checks passed at **320, 390, 768, 1024, 1440 and 1920 px**. Overview evidence covers VI/EN and light/dark; workspace checks cover all views, locale/theme persistence, read-only API behavior and XSS fixtures. These use synthetic/offline fixtures, not provider Fetch.
- `node --check` passed for `Frontend/public/app.js` and `Frontend/public/modules/capabilities.js`; Python compilation passed for changed backend modules; `git diff --check` passed (Git emitted only line-ending normalization warnings).
- Reviewed Lucian UX/UI Prompt's light/dark, keyboard/touch and reduced-motion guidance and its catalog. Applied the #031 custom listbox pattern to the Collection merge destination: clear selected state, themed popup, keyboard arrows/Home/End/Enter/Escape/typeahead, outside-click dismissal and no native blue highlight. The existing Overview segmented tabs remain unchanged. The implementation is adapted to the project's theme tokens and Collection data; no dependency was added. [Lucian UX/UI Prompt](https://lucianlabs.dev/en/uxui-prompt/introducing/), [catalog entry #031](https://github.com/Seokwoooo/lucian-uxui-prompt/blob/main/api/v1/en/e/031.txt).
- The screenshot run covered Overview only. The browser workflow exercised all routes, and the Analysis test verified the daily chart and accessible label in an isolated fixture, but it did not capture every route at every viewport. Keyboard-only workflows, complete modal/mobile-drawer journeys and a server restart acceptance pass remain incomplete.

## Remaining gates

1. Add frontend schedule creation/edit controls or explicitly accept the current operator API workflow. Scheduler is currently default-disabled and synthetic-only; no scheduled Fetch ran during this goal.
2. Add complete charts or explicitly accept the current accessible analysis tables; expand chart states only where comparable source data exists.
3. Provide a supported read-only interface to list Firestore-backed manual runs or clearly accept the local-ledger-only scope.
4. Finish native Chrome keyboard, mobile drawer, modal, pagination, reload and server-restart journeys; capture fresh evidence for every G2–G7 screen and both locales/themes.
5. Configure a Firestore emulator to run its 16 integration tests.
6. Revalidate the dated source research inventory before enabling or approving integrations. No external source approval, credential, quota or production capability is implied here.

No push or deployment was performed.
