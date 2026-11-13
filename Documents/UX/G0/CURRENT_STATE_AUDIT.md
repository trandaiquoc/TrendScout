# Current-state UX/UI audit

**Audit date:** 2026-11-13 · **Status:** G0 read-only audit. This is a proposal document, not approval to change the application.

## Scope and evidence

Reviewed `Frontend/public/index.html`, `app.js`, `styles.css`, locale modules, backend API routers/source catalog, current Windows acceptance report, source/business-analysis references, and the existing screenshots listed in that report. No app code, API, schema, database, provider, Fetch, scheduler, model, dependency, or secret was changed or invoked. The [current Windows acceptance report](../../Reports/DATA_EXPLORATION_ANALYTICS_ACCEPTANCE.md) records 154 persisted eligible keyword rows, 125 classified, 11 ambiguous, 18 unknown, 29 unassigned, 39 Collections, and 1,848 algorithm rows. These are dated local observations, not live metrics or blueprint fixtures.

Expected roadmap files `01_ROADMAP_DEMO_DA_NGUON_TRUC_TUYEN.md` and `02_HO_SO_NGU_CANH_TOAN_DU_AN.md` were not found. Available working references include `Documents/Design/US_TREND_KEYWORD_DISCOVERY_BUSINESS_ANALYSIS.md`, `US_PLATFORM_SOURCE_INVENTORY.md`, D5/D6 and Demo Online documents, and current acceptance reports. `Documents/REQUIREMENTS_TECH_BA_DEV.md` and the DOCX are legacy/working references; the DOCX labels version 0.1 as a discussion draft and unconfirmed technology/capabilities as proposals.

## Application and shell

- Frontend is static HTML, CSS and browser ES modules; no framework was found. Vietnamese and light theme are defaults; preferences load from local storage. English and dark theme are supported.
- Shell: responsive sidebar, mobile navigation scrim/toggle, breadcrumbs, connection status, locale/theme toggles, policy links, Wikimedia status and demo marker.
- Recent UX acceptance records native Linux Chrome evidence. Current Windows acceptance records fresh Windows desktop/mobile screenshots and interactions. This audit did not reopen the app or recapture screenshots.
- CSS already has light/dark tokens for surfaces, ink, accent, status colors, borders, focus and elevation. Preserve the established warm-neutral identity.

## Screen inventory

| Screen / route | Implemented behavior and data contract | UX issue or boundary |
| --- | --- | --- |
| Overview | Fetch/readiness card; four snapshot metrics; Wikimedia top-page horizontal bars; scope/freshness/limitation copy; persisted dataset keyword table with query, state, Collection, source, sort, paging and bulk actions. Reads `/api/v1/capabilities`, `/api/v1/datasets/latest`, `/api/v1/datasets/latest/keywords`; Fetch status uses `/api/v1/fetches/{id}`. | Combines summary, source evidence and a large explorer. Separate summary and analysis in proposed two-tab Dashboard. The chart is observed pageview ordering, not trend growth. |
| Collections | Collection search, creation mode, organize/reclassify action, suggestions, Collection cards, unassigned inbox, selection/assignment, review and bulk actions. Backend supports list/inbox/classification detail, CRUD, merge, memberships, bulk, organize and rollback. | Collection search only filters Collection cards; keyword search is elsewhere. Separate classified, suggested, ambiguous/unknown, unclassified, excluded and user-review states. No mandatory accept workflow. |
| Sources | Four hand-authored cards: Wikimedia, YouTube, Google Search and Bing Search. Backend provides `/api/v1/capabilities`. | Hardcoded copy can disagree with dynamic metadata. Wikimedia card says “not verified” while backend/current Windows report say `LOCAL_LIVE_VERIFIED`. No account-backed quota display. Separate usable sources from locked candidates. |
| Runs | Requires manually typing a run ID; calls `/api/runs/{id}`, `/keywords` and detail routes. | Chronological history should be the normal view; ID lookup should be secondary. |
| Keywords (advanced) | HTML `data-view="keywords"` panel and navigation link remain. | `showView()` maps `keywords`/`results` to `overview` and replaces the hash. The hidden Keywords panel and its load branch are unreachable. Retire duplicate route after moving the explorer to Collections. |
| Algorithms | Reads `/api/v1/algorithms`, `/api/v1/algorithms/results` and capabilities; shows family/source/status filters, applicability, summaries and evidence. | Explain in user language; keep technical detail expandable. Current evaluation is `NOT_YET_EVALUATED`; do not imply a winner. |
| Quality | Static/partly placeholder coverage labels such as `NOT_MEASURED`; no complete quality-score contract. | Show factual freshness, partial coverage, rejected/duplicate/missing counts only when evidence exists. Unknown is not zero. |
| Refresh / schedule | Disabled page; interval/next/lease/budget not configured or measured; controls disabled. | Do not promise browser timers. Future schedule must be backend-owned and job-backed. |
| Product Research | Unverified example and workflow preview; copy says no API. | Future workflow only. No fabricated sales, market size, margin, competition, IP or purchase-intent values. |
| Usage / AI usage | Disabled; usage unmeasured, cost unknown, budget not configured. | No usage dashboard until real metering exists. No cloud LLM claim. |
| Settings | Theme and locale controls; AI/provider panel disabled/unconfigured and says credentials are server-side. | Limit to preferences and truthful runtime/model state. Never ask for provider secrets in browser. |
| Privacy and Terms | Static policy pages; sidebar labels policy draft/local/not public. | Preserve draft status; do not imply legal/compliance approval. |

## Critical consistency findings

1. **Wikimedia status mismatch:** backend says adapter `IMPLEMENTED`, local LIVE verified, production not ready, while approval remains `PENDING_LIVE_APPROVAL`; UI hardcodes “not verified.” Present implementation, historical local verification, current health and production readiness independently. Do not change enablement based only on display.
2. **Navigation mismatch:** Overview owns the durable keyword explorer; Keywords aliases to Overview despite an orphaned panel.
3. **Source metadata duplication:** Sources hardcodes state while Algorithms reads runtime capability data. A later UI should use one backend-owned descriptor.
4. **Operational placeholders:** Quality, Refresh, Usage and Product Research contain disabled/unmeasured states. Navigation presence is not proof of capability.
5. **Provider truth:** Wikimedia US-country top list is censored and approximate. Search/YouTube rank is query-scoped ordinal position, not volume, view count or purchase intent. US filters do not prove US authorship/viewership.
6. **No scheduler, paid AI or live non-Wikimedia source** is evidenced by current acceptance.

## Recommendations

- Dashboard gets Overview and Analysis tabs with shared filters and explicit snapshot scope.
- Collections becomes the keyword workspace: Collection hierarchy, Unclassified review and durable ledger table. Keep batch actions in a selection toolbar.
- Build Source Center from capability API, with a separate research library for the 81 candidates.
- Turn Runs into chronological history; keep ID lookup secondary until a list contract exists.
- Keep Product Research, scheduling, usage and unverified providers visibly proposed/unavailable.
- Translate technical status tokens to VI/EN labels; explain with details.

## Evidence links

- [Current Windows acceptance](../../Reports/DATA_EXPLORATION_ANALYTICS_ACCEPTANCE.md)
- [UX redesign acceptance](../../Reports/UX_REDESIGN_AND_LIVE_START_ACCEPTANCE.md)
- [Business analysis](../../Design/US_TREND_KEYWORD_DISCOVERY_BUSINESS_ANALYSIS.md)
- [Frontend instructions](../../../Frontend/AGENTS.md)

## AI capability clarification

Local Collection matching/classification uses CPU FastEmbed 0.9.1 with `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, per the current local-AI acceptance report. This embedding path is active. The disabled Settings panel refers to an LLM/provider integration; no cloud LLM or paid AI usage is evidenced. UI copy should distinguish local embeddings from an unconfigured LLM provider.
