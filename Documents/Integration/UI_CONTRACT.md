# TrendScout UI integration contract

- **Status:** UI branch contract, implemented locally for review
- **Date:** 26 November 2026
- **Branch:** `feature/ui-overhaul`
**Scope:** Frontend behavior and the existing same-origin APIs it consumes. This document records observable contracts and UI requirements; it does not approve a permanent source, scoring model, runtime, or hosting choice.

## 1. Route and navigation contract

The browser uses clean paths. The application and Vercel configuration serve the single page at those paths; the FastAPI app serves its root page and retains backward redirects for old `/dashboard` URLs. The Vercel `installCommand` remains the Database Goal's existing `python3 -m pip install -e '.[legacy-firestore]'` command.

| Path | View | Behavior |
| --- | --- | --- |
| `/` | Overview | Default route; Overview tab |
| `/keywords` | Keywords | Opens the saved-keyword explorer and focuses its results heading |
| `/collections` | Collections | Collection tree, saved topics, suggestions, and keyword workbench |
| `/collections/{id}` | Collection detail | Selects a Collection in the workbench; can be reloaded |
| `/products` | Product ideas | Honest unavailable state for automated recommendations; links to saved evidence and manual Research |
| `/trends` | Trends | Overview's analysis tab; no per-trend detail route is claimed |
| `/sources` | Source Center | Current runtime source capabilities and the separate research inventory |
| `/sources/{source_id}` | Source detail | Opens the matching runtime source details; can be reloaded |
| `/research` | Research | User-authored product hypotheses and saved research notes |
| `/settings` | Settings | Runtime and existing settings surfaces |
| `/runs` | Run History | Fetch receipts, source outcomes, and supported recovery actions |
| `/algorithms` | Algorithms | Read-only registry and stored result summaries |
| `/quality` | Data Quality | Available coverage, freshness, and quality evidence |
| `/privacy`, `/terms` | Published policies | Aliases to the existing policy documents; policy text is not edited |

The eight primary navigation links are Overview, Keywords, Collections, Product Ideas, Trends, Sources, Research, and Settings. Run History, Algorithms, and Data Quality are grouped under Advanced. The legacy hash routes migrate to a clean path and preserve the query string. Server-level legacy redirects cover `/dashboard`, `/dashboard/`, and `/dashboard/{path}`. `/api/v1/dashboard/bootstrap` is an API name, not a user-facing page route.

Vercel route behavior follows its documented filesystem precedence and ordered redirect/rewrite rules. Verify deployment routing whenever the Vercel config changes: [Vercel project configuration](https://vercel.com/docs/project-configuration/vercel-json), [rewrites](https://vercel.com/docs/routing/rewrites) (reviewed 26 November 2026). Local tests do not prove a deployed Vercel configuration is active.

## 2. Data and source boundaries

- `GET /api/v1/capabilities` is the authority for runtime source descriptors and current operation status. The UI iterates the returned registry for source cards, Fetch controls, and overview source filtering. A source in a research library is not thereby an implemented or enabled runtime source.
- `GET /api/v1/sources/library` returns a separate candidate/research catalog. Its entries must remain labeled as candidates unless the runtime registry independently reports support.
- Source cards expose provider-reported adapter, configuration, approval, verification, enabled, latest-fetch, and production-readiness facts when present. Unknown source IDs use a neutral monogram fallback; source IDs and provider text are untrusted data.
- Wikimedia's current metric is an approximate upper-bound pageview measure over a country-scoped top list. Google/Bing result rank is position in a returned search sample. YouTube rank is position in a query-scoped video search sample. A `US` country, region, or localization filter does not establish US authorship, readership, customer demand, or market representativeness. Video counts and ranks are never presented as trending-keyword volume.
- The current source registry can grow beyond the four demo adapters. The present research library is a candidate inventory, not a claim of 81 connected sources.
- Source logos use centralized local marks and generic monograms. Source cards and Fetch outcomes do not recreate provider wordmarks. The pre-existing local YouTube SVG is used only in the linked YouTube evidence area; this branch did not download new logo assets. No matching license/provenance note was found under `Frontend/public/licenses`; confirm its origin and permitted reuse before expanding its use. Follow [YouTube API branding guidelines](https://developers.google.com/youtube/terms/branding-guidelines) (reviewed 26 November 2026); keep attribution and links to YouTube content where required.

## 3. Overview and keyword filters

`GET /api/v1/dashboard/bootstrap` returns `{item, analytics, keywords}` for the initial overview read. Its query follows the saved keyword contract below; the default first page is bounded. The frontend keeps one bootstrap request on initial load, then updates views from that response instead of preflighting separate dataset and analytics reads.

`GET /api/v1/datasets/latest` returns the most recent persisted dataset summary, or `{item: null}`. It does not prove that a historical series exists.

`GET /api/v1/datasets/latest/keywords` accepts:

| Field | Contract |
| --- | --- |
| `q` | Search text, up to 100 characters |
| `state` | `all`, `unassigned`, `ai`, `manual`, `mixed`, `needs_review`, `excluded`, or `non_topic` |
| `classification` | `all`, `CLASSIFIED`, `AMBIGUOUS`, `UNKNOWN`, or `INELIGIBLE` |
| `evidence` | `all`, `metric`, `algorithm`, or `missing` |
| `collection_ids` | Repeated IDs; at most 20; each non-empty and at most 64 characters |
| `source` | Source ID, up to 100 characters |
| `sort` | `recent`, `keyword`, `views`, or `rank` |
| `date_from`, `date_to` | ISO observation dates; an inverted range is rejected |
| `limit`, `offset` | Limit 1–100; offset 0–1,000,000 |
| `include_algorithm_results` | Boolean; disabled for the normal overview list |

The UI supports search, sorting, pagination, source, saved Collection, topic-category, classification, evidence, date, status, and selection/bulk actions within these limits. A category choice resolves to the ID of a saved Collection from the current taxonomy and is submitted as a Collection filter. The backend has no independent market-industry facet; the UI labels this as a topic category and says so in the filter scope note. Missing data stays unavailable; zero is preserved as zero.

## 4. Fetch, status, and recovery

### Submission

- `POST /api/v1/fetches` accepts only `sources` (optional non-empty unique source IDs) and `mode` (`LIVE` or `CACHE_ONLY`). It requires an `Idempotency-Key`. The server returns HTTP 202 with a stable fetch/job ID and status information when enabled.
- The UI does not expose provider IDs, credentials, or API tokens. Provider keys remain server-side. A source is selectable only if the runtime capabilities enable it for the current operation and applicable policy consent is satisfied.
- Cache-only is exposed only when the backend reports that it is available. Recovery/reconnect reuses the existing durable fetch ID and idempotency context. Reloading or an uncertain client timeout is not permission to start a new provider request.
- `POST /api/v1/fetches/history` accepts `days` from 1 to 2 and is currently a Wikimedia-only historical request. It is not an arbitrary date-range or all-source backfill.

### Status shape and UI rendering

`GET /api/v1/fetches/{fetch_id}` is no-store and returns a job status including fields such as `status`, `stage`, `selected_sources`, `sources`, `progress.completed_sources`, `progress.total_sources`, `terminal`, `recovery_available`, `created_at`, `updated_at`, and `finished_at` when available. In the distributed worker, `stage` currently mirrors the job state; it is not a save, Algorithms, or AI stage.

The frontend adapter validates the response shape before rendering. It uses only reported integer source counts to draw native progress; it ignores the provider's `percent` field. Per-source outcomes preserve `SUCCESS`, `PARTIAL`, `FAILED`, `UNAVAILABLE`, running/queued, and unknown states. `PARTIAL` is not upgraded to success. Record counts, error summaries, source names, and expandable diagnostics are rendered only when reported. The UI calculates elapsed time only if a real `started_at` timestamp is present; distributed status currently does not guarantee one. No API field currently reports separate persistence, algorithm, or AI progress stages, so those stages are not asserted by the UI.

Other public statuses and retry codes remain localized only when a known semantic mapping exists; unknown values are shown as unavailable or bounded detail instead of being silently converted. Source outcome `error_code` is retained in technical details. Status polling is bounded and continuation uses the same job rather than an extra provider submission.

## 5. Collections and proposal workflow

- `GET /api/v1/collections` returns `items`, `suggestions`, `mode`, `summary`, and a bounded `keyword_page` for the Collection workbench.
- `GET /api/v1/collections/keywords/{keyword_id}` is loaded on demand for keyword details and proposal evidence previews. Proposal evidence previews are capped at five saved keywords in the UI.
- Existing saved Collection suggestions can be reviewed, edited locally, accepted, or rejected. `POST /api/v1/collections/suggestions/{proposal_id}/accept` accepts an optional JSON object with `name` (1–80 non-blank characters) and `description` (up to 1,200 characters); unknown fields are rejected. `POST /api/v1/collections/suggestions/{proposal_id}/reject` records the explicit rejection action. Neither action silently accepts on view.
- Other implemented workbench actions include user-created Collection CRUD, merge review, bounded bulk membership changes, and `POST /api/v1/collections/reclassify` for the existing cached/reclassification workflow. These are not model-powered AI claims.
- Parallel AI contract v1.1-draft describes additional `/api/v1/ai/...` classification/proposal endpoints in the AI worktree. Those endpoints are absent from this UI branch's API/router and are not called here. Current suggestion provenance must be taken from the returned backend response, not inferred from an `AI` label or from the reference product.

## 6. Products and manual Research

No product-recommendation endpoint is present in this branch. `/products` therefore shows an explicit unavailable state, links to saved keyword evidence and manual Research, and does not invent product names, market evidence, AI rationale, images, demand, or export feasibility.

Existing Research APIs are:

- `GET /api/v1/research/bootstrap` for the initial compact keyword selector and up to 50 research records.
- `GET /api/v1/research` with `q` up to 100 characters, optional status (`DRAFT`, `RESEARCHING`, `NEEDS_REVIEW`, `VALIDATED`, `ARCHIVED`), `limit` 1–100 and `offset` up to 100,000.
- `POST /api/v1/research` to create a user-authored hypothesis and notes; text fields have individual backend bounds and an optional saved `keyword_id` must exist.
- `PATCH /api/v1/research/{research_id}` to update supported text/status fields.

Saved Research is a user-authored hypothesis/assessment. It is not provider measurement, a market fact, or an AI product recommendation. No automated product recommendation or regenerate action is advertised.

## 7. Trends, Algorithms, and Run History

- `/trends` reuses the analysis view and requests the current analytics projection (`GET /api/v1/analytics/overview`, or `analytics` in dashboard bootstrap). Historical charts render only returned observations/coverage; missing days are not filled with invented values. Source and time-range controls that have no supported endpoint/data remain disabled with an explanation.
- `GET /api/v1/algorithms` returns the read-only algorithm catalog. `GET /api/v1/algorithms/results` returns stored aggregate/sample results with `limit` 1–500 (default 100). The UI presents purpose, inputs, units, minimum samples, source compatibility, interpretation, limitations, and actual result state. A registered method is not presented as having run when its stored results are empty or skipped. No score is called a probability or validated accuracy without external evidence.
- `GET /api/v1/runs/history` supports `q`, status, mode, bounded `limit`, offset or cursor; cursor and offset cannot be combined. `GET /api/v1/runs/history/{fetch_id}` returns available per-run detail. The UI keeps run outcome separate from asynchronous Collection processing and source-level coverage. Recovery remains dependent on backend job state.
- No `Documents/Integration/ALGORITHMS_CONTRACT.md` was present in the Algorithms worktree during this review. The UI documents the current endpoints and rendered fields here; this does not approve the algorithm definitions or promise their completeness.

## 8. History and retention

The Run History UI states the Database Goal contract target, not runtime proof: keep rolling local calendar dates D−6 through D in `Asia/Ho_Chi_Minh`; a shorter provider content limit wins. Records without trustworthy collection timestamps or source lineage are to remain visible as unresolved/retained until reconciled. Collection text, user Research, settings, assignments, and operational ledgers have separate retention rules. The UI does not perform cleanup or claim cleanup is active.

The read-only Database contract dated 26 November 2026 says migrations 1–9 are present in the target schema but all 35 non-ledger business tables are empty; PostgreSQL is not cutover-ready. It also records a lineage gap for legacy derived/provider content. This is context from the Database worktree, not an implementation change in the UI branch.

## 9. Typed UI adapters and errors

The source-aware Fetch progress adapter (`Frontend/public/modules/fetch-progress.js`) accepts `unknown`, validates IDs, counters, enum-like states, timestamps and array/object boundaries, deduplicates source outcomes, rejects inconsistent counts, and ignores unknown fields. Rendering uses DOM text APIs, not provider markup. Unknown/unavailable/missing/partial/zero remain distinct.

All user-facing calls use the shared `Frontend/public/modules/api.js` client for bounded timeouts, in-flight GET coalescing, short response caching, mutation invalidation, same-origin credentials, and sanitized errors. Calls that can mutate data remain same-origin and use server-side policy validation. No frontend secret is required.

## 10. Verification boundary

Offline browser fixtures are synthetic test-only responses and are not shown as LIVE provider data. No production provider, Google, Wikimedia, Bing, YouTube, OpenRouter, or other paid/quota API calls are part of this UI verification. Backend integration, actual credentials, third-party quota, production routing, production retention execution, and AI recommendation quality remain unverified from this branch.
