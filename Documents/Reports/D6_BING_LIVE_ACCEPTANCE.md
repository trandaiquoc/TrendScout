# D6-BING LIVE + Source Center Fetch Budget — Acceptance Report

**Status:** Accepted for the local LIVE demo; one real Bing request and its cache-only replay were verified.
**Assessment date:** 2026-11-16 (Asia/Ho_Chi_Minh)
**Repository:** `/mnt/Data/Mine/TrendScout`
**Goal-start synchronized `origin/main`:** `96d0084f4e16d5d6166f751bedd46e7bd3cfc8d5`
**Implementation commit:** `19c342f9530db50b7f701a86ea5aca160fd1cbee` (`feat(d6): add live bing verification`)
**Report commit:** the commit containing this report; its hash is included in the final handoff.

## Scope and synchronization

- Fetched `origin` at goal start; `HEAD` equaled `origin/main`, with no commit gap to merge. The user pushed implementation commit `19c342f` during this goal; this report commit remains local for the user to push with the rest of the pending work.
- Read the repository instructions and inspected the source catalog, query plan, SerpApi client/pagination/account APIs, controlled HTTP gateway, Google and Bing handlers, public admission/quota state, local credential provider, collection persistence, capabilities API, dashboard modules, tests and the D6 Google acceptance report.
- Completed D6-BING only. No D6-YOUTUBE, D7, agent-initiated push, deployment or billing activation.
- Local acceptance data is in `~/.local/state/trendscout/collections-v1/collections.sqlite`; the separate paid-source quota/schedule state is in `~/.local/state/trendscout/normal-google-live-v1/state.sqlite`.

## Implementation

- Added `BingSavedCandidateHandler` and a deterministic plan that reads persisted LIVE Google Trends observations, prefers eligible Fashion/Home & Living signals, deduplicates normalized identities, skips recently verified candidates, and selects at most one candidate per Fetch.
- The controlled Bing request uses SerpApi `engine=bing`, `q=<saved candidate>`, `mkt=en-US`, `first=1`, one page, bounded HTTPS transport, one attempt and no retry. Newly admitted Bing provider requests now set `no_cache=true`; an existing local verification is served from SQLite first and never reaches the gateway.
- Validated and normalized organic result URLs, titles, snippets, ranks and timestamps. Bing evidence is attached to the original Google candidate under `provider_id=serpapi_bing` and `verification_mode=BING_SERP`; it does not create a fake Bing keyword observation or alter the Google observation timestamp or user decision.
- Added persistent user-triggered refresh eligibility at **07:00, 15:00 and 23:00 Asia/Ho_Chi_Minh**, a ten-minute slot grace, at least eight hours between attempts, no catch-up, and a one-shot first-run acceptance exception. The paid-source schedules are independent.
- Added one SerpApi account ceiling shared by Google and Bing, with Google/Bing allocations, daily caps, a six-search reserve, Account API preflight, persisted provider renewal-cycle reconciliation and fail-closed migration. Browser source selection is a preference; the server policy remains authoritative.
- Both `./start.sh` and `start.ps1` start the same loopback web application. The launch scripts do not source credentials or initiate Fetch. `LocalCredentialProvider` reads `SERPAPI_API_KEY` from the repository-root `.env` file on Linux and Windows; `.env.example` contains only a placeholder. No secret was committed.
- Added Source Center quota/schedule/latest Bing coverage display, source-only Fetch selection, bilingual detail rendering and honest local LIVE / cached delivery status. Added Data Quality snapshot timestamp persistence for sources whose evidence is stored outside ordinary keyword rows.

## Official provider documentation checked 2026-11-16

- [SerpApi Bing Search API](https://serpapi.com/bing-search-api) documents `engine=bing`, `q`, the `mkt` market code, the `first` offset, organic results, and `no_cache=true` to force a new provider fetch instead of using SerpApi's one-hour cache.
- [SerpApi Account API](https://serpapi.com/account-api) reports plan limit, month-to-date use, remaining searches, hourly use and renewal date. SerpApi documents Account API calls as free and not counted against search quota.
- Microsoft announced that the [Bing Search APIs were retired on 2025-08-11](https://learn.microsoft.com/lifecycle/announcements/bing-search-api-retirement); no Bing News or unofficial-feed scraper was added.
- Microsoft's [Ad Insight API](https://learn.microsoft.com/en-us/advertising/ad-insight-service/ad-insight-service-reference?view=bingads-13) can provide historical keyword performance and ideas. [Production API setup](https://learn.microsoft.com/en-us/advertising/guides/get-started?view=bingads-13) requires a Microsoft Advertising account, OAuth user access and a developer token. It remains a future option outside this goal's no-new-account/OAuth scope.

## Shared SerpApi quota evidence

The account has a 250-search plan with provider-reported renewal date `2026-12-11`. Account usage is shared by Google and Bing.

| Evidence | Provider usage | Remaining | Timestamp |
| --- | ---: | ---: | --- |
| Account API preflight for the Bing attempt | 6 / 250 | 244 | `2026-11-16T06:14:13.418937Z` |
| Read-only Account API follow-up | 7 / 250 | 243 | `2026-11-16T06:34:01.454291Z` |

The follow-up increased month-to-date provider use by exactly one search, matching the single Bing LIVE attempt. The read-only Account API itself did not use a search. At acceptance close, the persisted local ledger showed:

| Pool | Daily used / limit | Cycle used / limit | Remaining in cycle |
| --- | ---: | ---: | ---: |
| Shared SerpApi | 3 / 9 | 7 / 244 | 237 |
| Google allocation | 2 / 6 | 6 / 122 | 116 |
| Bing allocation | 1 / 3 | 1 / 122 | 121 |
| Protected shared reserve | — | 6 | 6 protected |

The account snapshot last persisted by the app is the pre-search `6/250` snapshot. After restart the UI marks it **STALE** rather than presenting it as current; the next admitted paid Fetch performs a fresh Account API preflight. Account use and the app ledger are not separate 250-search pools.

**Cross-machine limit:** each computer has its own SQLite reservation state. Git synchronization does not create atomic quota coordination across the two machines. Keep one designated active paid-fetch host; do not interpret the local ledger as protection against a simultaneous request from another computer.

## Real Bing LIVE acceptance

Exactly one newly authorized Bing search was sent. No Google Trends request or retry was made for this Bing acceptance.

- Saved candidate: `fitness`, source `google_web_serp`, selection reason `PREFERRED_INDUSTRY_SIGNAL`.
- Original Google observation: `2026-11-15T07:10:00Z`; unchanged after Bing verification.
- Bing request: `q=fitness`, `mkt=en-US`, language `en-US`, first offset `1`, one page.
- SerpApi provider-created time: `2026-11-16T06:14:13Z`.
- Locally captured result time: `2026-11-16T06:14:19.466473Z`.
- Delivery: `FRESH`; source status `SUCCESS`; provider reported 10 results, 10 accepted, 0 rejected; provider cache state recorded as `UNKNOWN`.
- Source run/dataset: `ba4006f345a57853fad234bec790426e`; overall dataset `COMPLETED`.
- The broad `fitness` results included Orlando-localized businesses. `mkt=en-US` is a market parameter, not proof of US customer interest. SERP rank and result count are not search volume or commercial demand.

| Bing result position | Result | Canonical URL |
| ---: | --- | --- |
| 1 | LA Fitness, Orlando | <https://www.lafitness.com/Pages/ClubHome.aspx?clubid=1058> |
| 2 | Planet Fitness, Orlando | <https://www.planetfitness.com/clubs/fl/orlando> |
| 3 | Amped Fitness | <https://ampedfitness.com/> |
| 4 | Fitness.com | <https://www.fitness.com/en> |
| 5 | Crunch Fitness, East Colonial | <https://www.crunch.com/locations/east-colonial> |
| 6 | Physical fitness, Wikipedia | <https://en.wikipedia.org/wiki/Physical_fitness> |
| 7 | LA Fitness, Orlando East Colonial | <https://www.lafitness.com/Pages/clubhome.aspx?clubid=289> |
| 8 | Anytime Fitness, Orlando | <https://www.anytimefitness.com/locations/orlando-florida-3285> |
| 9 | CLUB4 Fitness, Orlando | <https://www.club4fitness.com/location/orlando/> |
| 10 | Planet Fitness | <https://www.planetfitness.com/> |

## Cache-only replay

A second Bing-only Fetch used the saved local result:

- Source status `SUCCESS`, delivery `CACHED`, local cache state `FRESH` (under one hour).
- `provider_request_count=0`; the account allocation remained Bing `1/122` and shared `7/244`.
- Original `cache_fetched_at` remained `2026-11-16T06:14:19.466473Z`; no new provider timestamp was fabricated.
- Source run/dataset: `0b46d09c4d33f434567a6eadb3ff6eba`; the overall dataset was `PARTIAL` because the Fetch selected only Bing, while the Bing source itself succeeded.

## SQLite, API, Dashboard and preservation

- SQLite `PRAGMA integrity_check`: `ok`.
- Before and after the two Bing Fetches: 583 LIVE Google rows and 1,035 LIVE Wikimedia rows (1,020 distinct normalized Wikimedia identities); no row counts changed.
- No Bing keyword row was created. One existing Google candidate retains the Bing SERP evidence, including 10 results. Its Google `observed_at` stayed `2026-11-15T07:10:00Z`; no user decision was present or changed.
- API keyword detail returned the Bing verification with provider identity, query, delivery, counts, timestamps and 10 result URLs. Dashboard Collections detail rendered all 10 results and the “rank is not search volume” limitation.
- Source Center was verified in Vietnamese and English: Bing-only saved Fetch selection, shared/local quotas, reserve, stale provider snapshot, Bing query/count, cached delivery, provider cache uncertainty and next schedule. On a 500 CSS-pixel Chrome viewport, document width was 485 pixels with no horizontal overflow.
- Run History displayed the LIVE and CACHED Bing runs separately. Data Quality showed the latest Bing source as successful and cached. The old cached dataset snapshot's aggregate `fetched_at` had been NULL even though its source manifest contained `cache_fetched_at`; that exact field was backfilled from the stored original timestamp without changing keyword rows, evidence or decisions. The implementation also persists valid source-level fetch timestamps for future snapshots.
- The earlier Google acceptance document reports 603 Google and 1,219 Wikimedia observations, while this local LIVE database currently contains 583 Google and 1,035 Wikimedia rows. The host/database scope discrepancy is unresolved; the measured local counts above are the evidence for this acceptance.

## Regression and review

- Focused backend regression covering shared admission, worker, LIVE gateway, cache, Google, Bing, credentials, source selection, migration and refresh schedules: **167 passed, 2 skipped**.
- Additional Wikimedia, dashboard capabilities, settings and source catalog regressions: **30 passed**.
- Frontend Node tests: **16 passed**.
- Python compileall, `bash -n start.sh`, `git diff --check`: **passed**.
- Focused Ruff lint for newly added Bing/schedule modules and tests: **passed**. A whole-repository Ruff scan reported 1,509 findings across the existing repository, including files outside this goal; no repository-wide clean lint claim is made.
- Multi-perspective review covered code quality, boundaries, security and acceptance completeness. The review fixes include retaining the last account snapshot after restart, preserving Bing coverage on cached delivery, forcing provider refresh for newly admitted Bing attempts, and surfacing source timestamps in Data Quality.

## Acceptance gates

| Gate | Weight | Status | Evidence |
| --- | ---: | --- | --- |
| G0 Git sync, baseline, scope | 8% | PASS | Synced to `origin/main`, inspected repository and recorded actual local DB baseline. |
| G1 Bing bounded discovery contract | 12% | PASS | Saved-candidate selection, deterministic deduplication, one query/one page and preserved provenance. |
| G2 Shared Google+Bing quota policy | 18% | PASS | Shared 244 ceiling, 122/122 source allocations, six reserve, persisted source usage and Account API reconciliation. Cross-machine limitation is explicitly documented. |
| G3 Source Center controls and cache | 12% | PASS | Per-source selection, quota/schedule status and verified zero-request cached delivery in English/Vietnamese. |
| G4 Real Bing provider LIVE evidence | 16% | PASS | One provider search; Account API use increased by exactly one; 10 organic results preserved with timestamps and URLs. |
| G5 SQLite + real application flow | 16% | PASS | SQLite, API, Collections detail, Source Center, Run History and Data Quality verified; existing Google/Wikimedia counts and decisions preserved. |
| G6 Focused regression / browser QA | 12% | PASS | 167+30 backend tests, 16 frontend tests, compile/shell/diff checks and responsive browser verification. |
| G7 Report and local commits | 6% | PASS after this report commit | Implementation commit above; report commit is identified in the final handoff. |

**Earned progress after G7: 100%.**

## User-requested Bing test reset

After reaching 100%, the user asked for the local Bing test to be reset so they can click Fetch themselves: first Bing-only click should force one new LIVE provider request, and the second should use the local cache. The reset is complete. It removed only this acceptance candidate's `bing_verification` and `bing_verification_history` and reset only the Bing refresh-schedule key to initial eligibility. It preserved the SerpApi quota ledger (Bing remains 1/3 daily and 1/122 cycle; shared usage remains 3/9 daily and 7/244 cycle), the Google candidate and observation, all other evidence, user decisions, Google/Wikimedia rows, and all dataset/fetch history. Post-reset SQLite integrity is `ok`. No agent-initiated provider request was made after acceptance. The adapter uses `no_cache=true` for the user's next admitted Bing attempt, so that first click can make a fresh provider request; the second click should then be served from local cache.

The user pushed the implementation commit. The report commit is local; the agent did not push it, deploy, activate billing or commit a secret-bearing file.
