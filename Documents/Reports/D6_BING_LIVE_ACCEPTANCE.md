# D6-BING LIVE + Source Center Fetch Budget — Acceptance Report

**Status:** Partial implementation; D6-BING is not accepted as a live end-to-end feature.
**Assessment date:** 2026-11-16 (Asia/Ho_Chi_Minh)
**Starting repository revision:** `0dd40f296f0ddf2693f44112e78ee914ef29f335`
**Implementation commit:** `c4aff09e62e7005d0950b545841e901d930d82b6`

## Summary

This work adds a deterministic, one-candidate Bing verification plan contract, permits that registered dynamic context through the existing SerpApi pagination guard, exposes the provider-reported renewal date in sanitized account metadata, and adds browser-local default Fetch source preferences plus a read-only SerpApi quota panel to Source Center. Focused offline tests and browser layout checks pass.

The requested full workflow is not implemented or verified. The Bing plan is not wired to a persisted Google candidate query, Bing LIVE collector, durable Bing records, application API or Dashboard. The shared Google/Bing monthly allocation, protected reserve, cross-machine coordination, time slots, paid-source cache-first admission, and server-side per-source budgets are not enforced by this change. The sole approved Bing attempt was not made: the local database had no eligible Google candidate and this checkout is not the designated Windows paid-fetch host. No Bing provider evidence or new paid searches were produced.

## Repository and implementation inspected

Startup synchronization:

- Fetched `origin`; `HEAD` equaled `origin/main` at the supplied reference revision before changes, so no sync merge was required.
- Read root `AGENTS.md` and `Frontend/AGENTS.md`, inspected the source catalog, query plan, LIVE gateway, SerpApi client/adapter/pagination/account and Google budget/runtime paths, source capability API and frontend Source Center modules, tests, and `Documents/Reports/D6_GOOGLE_LIVE_ACCEPTANCE.md`.

Implementation files changed:

- `Backend/app/sources/bing_search/verification_plan.py` — deterministic candidate selection contract limited to one saved LIVE Google Trends observation; normalized deduplication; rejects malformed/naive/future timestamps, non-Google records, and recently verified identities; retains source provenance and selection reason.
- `Backend/app/sources/serpapi/pagination.py` — validates the registered dynamic Bing query context before pagination.
- `Backend/app/sources/serpapi/account.py` — validates and exposes the provider's optional ISO renewal date in public quota metadata.
- `Frontend/public/modules/capabilities.js`, `Frontend/public/app.js`, `Frontend/public/index.html`, styles and English/Vietnamese strings — Source Center default Fetch checkboxes saved in browser local storage and intersected with server-enabled sources; read-only provider status, usage, reserve, renewal, check timestamp and allocation fields. Bing allocation is displayed as unavailable because no Bing allocation ledger is implemented.
- Focused Python, frontend and offline browser regressions cover these contracts and the visible controls.

## SerpApi account and budget evidence

The official SerpApi Account API was read without issuing a search. Latest observed snapshot: **2026-11-16T02:20:35.116996Z**.

| Provider-reported field | Snapshot |
| --- | ---: |
| Account status | Active (account response parsed successfully) |
| Monthly plan limit | 250 searches |
| Monthly remaining | 246 searches |
| Total remaining | 246 searches |
| Month-to-date usage | 4 searches |
| Current-hour usage | 0 searches |
| Hourly limit | 250 searches |
| Provider-reported next renewal | 2026-12-11 |
| Bing requests made by this goal | 0 |
| SerpApi searches consumed by this goal | 0 |

The prior Google acceptance report also described 4 used and 246 remaining, consistent with the provider snapshot. However, this Linux checkout has no `normal-google-live-v1/state.sqlite` reservation ledger, so current local Google reservation history cannot be read or reconciled here. The local collections DB has no Google or Bing keyword rows. Therefore account usage is provider-reported evidence; it does not prove the contents of either machine's local ledger.

The target 122 Google / 122 Bing / 6 shared reserve split remains a target only. This checkout does not enforce those allocations or report verifiable per-source usage/reservations. Given the account snapshot, the arithmetic ceiling after retaining six reserve searches is at most 240 new searches for the account, but that is not authorization, allocation policy, or a locally enforced available balance. Do not treat Google/Bing as separate 250-search pools. No billing was activated.

The user authorized at most one new Bing paid-search attempt. That approval is not yet actionable on this checkout: the available SQLite dataset contains no saved Google/Bing candidate rows and no designated paid-fetch Windows host is connected. No retry or seed campaign was run. Since no eligible query/host was available, the approval remains unused.

## Provider and discovery strategy

The bounded contract selects from saved LIVE Google Trends observations and labels the Bing result as SERP verification evidence. SerpApi's Bing Search documentation supports the existing adapter request shape (`engine=bing`, `q`, `mkt=en-US`, `first`) and organic-result response. A SERP position or result count is not Bing search volume or proof of US customer demand.

Current source documentation was checked on 2026-11-16:

- [SerpApi Bing Search API](https://serpapi.com/bing-search-api)
- [SerpApi Account API](https://serpapi.com/account-api)
- [SerpApi Legal](https://serpapi.com/legal)
- [Microsoft Bing Search APIs retirement notice](https://learn.microsoft.com/en-us/bing/search-apis/bing-web-search/overview) — Microsoft's Bing Search APIs were retired on 2025-08-11; no direct Microsoft Bing Search/News API integration was added.
- [Microsoft Advertising Ad Insight reference](https://learn.microsoft.com/en-us/advertising/ad-insight-service/ad-insight-service-reference?view=bingads-13) and [OAuth quick start](https://learn.microsoft.com/en-us/advertising/guides/authentication-oauth-quick-start?view=bingads-13) — possible future keyword-statistics route, but it requires Microsoft Advertising access and OAuth/developer credentials and was outside this goal's constraints.

No undocumented scraping or News feed ingestion was implemented. SerpApi provider access terms and retention conditions remain applicable to any later LIVE work; confirm the then-current terms before production use.

## SQLite, API and Dashboard baseline

Read-only inspection of `~/.local/state/trendscout/collections-v1/collections.sqlite` found:

- SQLite integrity check: `ok`.
- 214 LIVE Wikimedia keywords, 21 collections and 13 dataset snapshots.
- No saved Google or Bing keyword rows/candidates in this local DB.
- No Bing archive-backed provider response was found in the inspected local data.
- No application process was running during this inspection.
- No database file, classification, research record or existing run was changed.

The earlier Google acceptance document reports 603 Google discovery records and the earlier Wikimedia baseline of 1,219 keyword records. Those numbers differ from this checkout's current SQLite contents (zero Google rows and 214 Wikimedia rows). The scope, host and/or DB may differ; the discrepancy is unresolved and this report does not overwrite either count or claim preservation/readback for the absent records.

No real Bing-only, Google+Bing, or ALL-source Fetch was submitted. No Bing record was read back from SQLite or the API, and Dashboard run history, keyword detail, collection/classification, evidence links, and restart behavior were not verified for Bing. Source Center controls were verified in the offline browser harness only. Browser-local preference is not a server identity or authorization mechanism; server policy must continue to control paid access.

## Fetch/cache, scheduling and cross-machine limitations

The existing SerpApi adapters and pagination tests exercise bounded request construction, result parsing, page handling, error classification and provenance. The present goal change does not connect Bing to the application's cache-first collection admission or prove that a Bing Fetch outside an eligible slot makes zero provider requests. It also does not implement the requested Asia/Ho_Chi_Minh 07:00/15:00/23:00 eligibility slots, eight-hour source cooldown, no-catch-up behavior, or per-source refresh state. Those conditions remain unverified.

The two Windows computers' local SQLite databases cannot provide atomic cross-machine quota reservations. This work adds no coordination service. A designated active paid-fetch host is required until a safe shared coordination design is explicitly approved; this Linux checkout did not identify or connect that host. Preserve the six-search reserve and fail closed if account quota or coordination is uncertain.

## Verification results

All checks below were offline; there was no paid Bing request:

- Bing saved-candidate contract: **3 passed**.
- Google Trends/runtime/account tests: **19 passed**.
- SerpApi adapter and offline transport tests: **23 passed**.
- Frontend Node tests: **13 passed**.
- Offline Chrome browser workspace: **PASS** at 320, 390, 768, 1024, 1440 and 1920 px; Source Center defaults and provider panel rendered. The harness explicitly did not submit Fetch.
- Ruff, Python compileall, JavaScript syntax checks and `git diff --check`: **PASS**.

These results do not establish a live Bing provider response or end-to-end SQLite/API/Dashboard acceptance.

## Gate status

Progress is based only on complete gate passes:

| Gate | Weight | Status | Evidence / reason |
| --- | ---: | --- | --- |
| G0 Git sync, baseline, scope | 8% | PASS | Synced to origin/main; inspected repo guidance and read-only local baseline. Count discrepancies recorded. |
| G1 Bing bounded discovery contract | 12% | PARTIAL | Contract and offline tests exist, but candidate selection is not wired to persisted rows/runtime. No credit. |
| G2 Shared Google+Bing quota policy | 18% | NOT PASS | Provider account snapshot is read-only; shared allocations/reserve and local usage enforcement are absent. |
| G3 Source Center controls and cache | 12% | PARTIAL | Browser-local defaults and read-only quota panel render; server budget controls and cache-first Bing flow are not integrated. No credit. |
| G4 Real Bing provider LIVE evidence | 16% | NOT PASS | No request, archive evidence, Bing result URLs or provider timestamps. |
| G5 SQLite + real application flow | 16% | NOT PASS | No Bing collection worker or end-to-end Fetch/readback. |
| G6 Focused regression / browser QA | 12% | PASS | Focused offline regressions and six-width browser harness passed. |
| G7 Report and local commits | 6% | PASS after report commit | Implementation commit `c4aff09e62e7005d0950b545841e901d930d82b6`; report commit is recorded in the final handoff. |

**Earned progress after G7: 26%.** D6-BING is not complete or accepted as LIVE.

## Remaining work and blockers

1. Connect the saved-candidate selector to actual persisted LIVE Google observations and add a Bing collector through the guarded SerpApi adapter, with explicit operator policy, single-attempt approval and source identity.
2. Implement and migrate a shared provider-level quota policy that preserves prior Google reservations, enforces 122/122 allocations and six reserve, and records auditable local usage. Fail closed where safe migration or cross-machine control cannot be proven.
3. Add scheduled-slot eligibility, per-source cooldown and cache-first SQLite delivery. Avoid autonomous paid scheduling.
4. On the designated paid-fetch Windows host, verify credentials and policy, recover/check any existing archive, confirm an eligible persisted candidate and quota preflight, then—only with the still-valid explicit approval—perform at most one Bing request. Record original response metadata, organic evidence and URLs without treating SERP data as volume.
5. Verify Bing-only and multi-source Fetch through API, SQLite, Dashboard, collections and restart; reconcile database host/scope against the earlier Google/Wikimedia acceptance counts.

No push, deployment, billing activation or secret-bearing change was made.
