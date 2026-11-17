# TrendScout — Windows 2 YouTube Cache-First and Four-Source Correction

**Status: Windows 2 LIVE, cache persistence, four-source and isolated headless browser acceptance complete.** This report records verified work for the post-D6 correction. It does not change or replace the completed D6 acceptance result.

## Scope and baseline

- Machine: Windows 2, hostname `DESKTOP-ICECOET`.
- Repository baseline: `b7591031fa2c61ea79cf2641fc740bafb5ff5c15`; `HEAD` matched `origin/main` before edits.
- Implementation commit: `ec2b1bd` (`fix(windows): add YouTube cache-first manual mode`). Local only; not pushed.
- The D6 YouTube acceptance report has no Git diff and remains unchanged.
- No database reset, deployment, or D7 work was performed.

## Reproduced cause

The two owner-reported Fetches explicitly selected Google Trends/Search, Wikimedia Pageviews, Bing Search, and YouTube Search. Their submitted source IDs reached Fetch admission; YouTube was not omitted by defaults or capabilities.

| Fetch | Google Trends/Search | Wikimedia | Bing Search | YouTube Search |
| --- | --- | --- | --- | --- |
| `c0df602b26da14c7ac6fd35828cd3ad0` (14:24, 17 Nov 2026) | LIVE/FRESH, 442 records | LIVE/FRESH, 1,000 records | UNAVAILABLE | UNAVAILABLE |
| `402a7128b38a43bef8d0b92be456e5f7` (14:26, 17 Nov 2026) | LIVE/CACHED, 442 records | LIVE/CACHED, 1,000 records | LIVE/FRESH, one saved-candidate verification record | UNAVAILABLE |

The Windows YouTube cache was missing usable evidence. Under the actual `NORMAL_GOOGLE_LIVE_V1` scope, the computed YouTube cache fingerprint was `84bf085bbe80496f193cf354c00c0a758bf83dae768efd8deb70e9777fae7fa8`; the corresponding local cache state was empty. The prior Linux cache was not present on Windows and was not assumed to transfer through Git. In NORMAL_DEMO, the provider refresh was outside its scheduled window, so the source returned an ineligible/unavailable result instead of identifying the cache miss. The UI also lacked a specific missing-cache explanation, and the Run History projection could drop unavailable source diagnostics.

SQLite integrity checks for the local collections database and normal Google LIVE state database returned `ok`. The collections Fetch jobs inspected before restarting were terminal; no queued or running work was found.

## Implementation committed

- Check scoped YouTube cache before deciding whether a scheduled refresh is eligible.
- Fresh cached evidence is delivered as `LIVE/CACHED`, with zero new provider requests and its original observation time preserved.
- Stale reusable evidence is returned as `STALE_CACHE` only within existing retention/provenance rules, with a stale warning and original observation time.
- Missing, expired, and invalid cache states have distinct stable diagnostics and report the next eligible refresh when known. A scheduled miss with no cache returns `UNAVAILABLE` and `NO_CACHE_AVAILABLE`; no video evidence is fabricated.
- Provider failures retain safe reusable cached evidence with stale status and sanitized provider diagnostics.
- Add explicit owner manual YouTube LIVE mode for the designated Windows 2 host. It is opt-in, local-only, user-triggered, one-query-per-Fetch, and rejected on Vercel. NORMAL_DEMO remains the default. Credentials remain server-side and the `d6-youtube-v2` approval and user-consent gate remain enforced.
- Preserve all four selected source outcomes through Fetch status, Run History, capabilities, and Source Center diagnostics. The Fetch view now reports selection, delivery, request count, observation/cache times, freshness, next refresh, and source error in English and Vietnamese.
- Bing's saved-Google-candidate dependency and source-level unavailable status are preserved; YouTube evidence is not promoted to keyword search volume.

## Verification completed

- Backend: `python -m unittest discover -s tests -v` — **930 passed, 2 skipped**. The two skips are Linux-only launcher compatibility cases on Windows. The Firestore emulator integration suite was not run; its existing waiver remains in force and is not counted as a pass.
- Frontend: `node --test tests/*.test.mjs` — **22 passed**.
- Ruff check on all changed Python files — passed.
- PowerShell parser check for `start.ps1` — passed.
- `git diff --check` — passed before commit.
- Focused regression coverage includes fresh/stale/missing/expired/invalid cache behavior, zero-request cache delivery, SQLite reopen persistence, unavailable-source manifest/diagnostics, the Windows host/default/Vercel manual-mode guards, and a frontend assertion that all four selected source IDs and the current YouTube policy version are submitted together.

## Windows 2 acceptance evidence - 17 Nov 2026

The prior implementation and regression work remain unchanged. This update closes the live runtime and browser acceptance gates using the authorized Windows 2 owner runtime. No database reset, push, deployment, Firebase migration, or D7 work was performed. D6 remains complete at 100%.

### A-C. YouTube LIVE/FRESH and durable SQLite evidence

- Started the guarded `OWNER_MANUAL_LIVE` runtime on loopback port 8001. Its capability response showed the approved `d6-youtube-v2` policy, designated Windows 2 host, and configured server-side key. Startup made no provider request.
- In the isolated Chrome DevTools browser context, selected YouTube Search only and submitted one user-triggered Fetch. Run ID: `e2c1602c4f207b87e4a5e34a45b96cf0`.
- The result was `PARTIAL`, `LIVE`, `FRESH`, through YouTube Data API v3 `search.list`, with exactly one provider request. The provider returned 50 accepted results; 47 unique records were retained and 3 were explicitly omitted by the configured storage limit. No records were rejected. Only the first page was requested; the continuation token was saved without another request.
- The recorded query was `technology software gadgets electronics trends`, scope `technology`, market filter `US`, language filter `en-US`. These filters do not establish US authorship, audience, or demand.
- After the owner runtime stopped, read-only checks of `C:\Users\tdquo\.local\state\trendscout\normal-google-live-v1\state.sqlite` returned `PRAGMA integrity_check = ok`. The scoped cache row `84bf085bbe80496f193cf354c00c0a758bf83dae768efd8deb70e9777fae7fa8` contains `origin=LIVE`, `data_mode=LIVE`, `delivery_state=FRESH`, `status=PARTIAL`, and 47 records. All 47 IDs are distinct and valid 11-character YouTube IDs; all 47 canonical watch URLs match their stored IDs. The fetch job and source checkpoint are durable in the same SQLite state database.
- `OWNER_MANUAL_LIVE` was stopped cleanly before starting `NORMAL_DEMO`.

### D-F. NORMAL_DEMO restart and zero-request cache reads

- First NORMAL_DEMO restart: run `d91afd4fc6b1b4b3cb8edeb59085fe2e` returned 47 records as `PARTIAL / LIVE / CACHED`, with `provider_request_count=0`. It preserved the original observation time `2026-11-17T08:59:12.469794Z`; cache delivery was `2026-11-17T09:02:41.782268Z`.
- Second NORMAL_DEMO restart: run `c1d26bc8187efd256ec90e1c19d70bb0` again returned 47 records as `PARTIAL / LIVE / CACHED`, with `provider_request_count=0` and the same source observation time. Cache delivery was `2026-11-17T09:03:43.668450Z`.
- The YouTube application ledger remained at one `search.list` request: 1/6 daily and 1/200 monthly after both restart reads. The last provider request timestamp stayed at the original LIVE request. No cache read made another YouTube call.

### G-H. Combined four-source Fetch, Run History, Data Quality, and quotas

The required combined Fetch completed outside the configured refresh window as run `3e08feafb3e907b676b550c8a25d91c7`. The selected source IDs were Wikimedia Pageviews, Google Search, Bing Search, and YouTube Search. The saved run and dataset both report `PARTIAL`, `LIVE`, with 1,490 records received, 1,368 keywords retained, 74 rejected records, and 9 quality notes.

| Source | Result | Evidence |
| --- | --- | --- |
| Bing Search | `SUCCESS / LIVE / CACHED` | 1 retained record; 0 provider requests; prior observation 14:26 ICT |
| Google Search | `PARTIAL / LIVE / CACHED` | 442 retained candidates; stale cache and `NOT_YET_ELIGIBLE`; 0 Trending Now calls; two candidate verification checks were blocked by admission |
| Wikimedia Pageviews | `SUCCESS / LIVE / CACHED` | 1,000 retained records; 0 provider requests; observation date 2026-11-15 |
| YouTube Search | `PARTIAL / LIVE / CACHED` | 50 provider results in the originating cache; 47 retained, 3 omitted; 0 requests in this combined run |

Run History details show all four per-source statuses, cache delivery, source observation times, counts, and the same run ID. Data Quality was reloaded after the combined run; its latest saved snapshot points to this same dataset and reports all four source statuses plus the matching 1,490 / 1,368 / 74 totals.

The Source Center showed YouTube at 1/6 daily and 1/200 monthly application requests, with provider quota still `UNKNOWN`. SerpApi local ledgers were unchanged by the combined Fetch: 7/9 daily and 15/244 monthly overall; Google 6/6 daily and Bing 1/3 daily. Source coverage shows no Google Trending Now request, no Bing request, and no paid SerpApi search request in this run. Two Google candidate verification checks were blocked by local admission (`serp_admission_blocked_count=2`).

The provider-account quota snapshot refreshed during the combined run at `2026-11-17T09:05:02Z` and showed 15/250 monthly searches, with 235 remaining and renewal on 2026-12-11. The previously stored provider snapshot was 14 and stale; the local monthly ledger was already 15 before and after this run. The snapshot update is recorded as refreshed telemetry and is not attributed to this Fetch as a new search. No additional local SerpApi allocation was consumed.

### I. Isolated headless Chrome DevTools browser QA

- A real `mcp__chrome_devtools__list_pages` call succeeded, confirming the Chrome DevTools MCP connection. The dashboard was opened in the isolated `trendscout-dashboard-verification` context using HeadlessChrome 157; no personal Chrome session or Windows desktop was controlled.
- Inspected the Overview, Source Center, Run History, and Data Quality routes. Verified the displayed source statuses, cache/request counts, quota panels, combined run detail, and quality totals. Theme and language toggles had already passed in the earlier browser QA; this continuation reused that evidence.
- At a 390 x 844 mobile viewport, `scrollWidth` and `clientWidth` both measured 390. Captured and saved the Run History viewport and full-page Data Quality screenshot:
  - [Run History - four-source run](windows-evidence/windows2-four-source-run-history-mobile-390.png)
  - [Data Quality - combined dataset](windows-evidence/windows2-four-source-data-quality-mobile-390.png)
- After reload, the JavaScript console contained no runtime exceptions. Chromium reported two non-fatal form/accessibility issues (missing autocomplete; a form field without an id/name). An earlier initial page load also requested `/favicon.ico`, which returned 404 `LOCAL_ROUTE_NOT_ALLOWED`; subsequent reload console output contained only the two issue warnings.
- Visual QA notes: long quality-note text wraps into a tall column at 390 px but does not create horizontal overflow. The Run History Google coverage detail contains a mis-encoded separator. The supplemental Data Quality timing panel still says collection time unavailable and "No run to assess yet," while the latest-data snapshot above it accurately reports the combined run and its source statuses. These UI observations are recorded; no unrelated UI refactor was made.

### Completion and remaining limits

All remaining Windows 2 acceptance gates A-J are complete. Existing implementation/regression commits and passing evidence were reused: backend 930 passed / 2 skipped, frontend 22 passed, Ruff passed, and PowerShell parser validation passed. No tests were rerun for this documentation-and-evidence-only update.

The Google Cloud project-specific YouTube quota remains unknown; the authorized run used one `search.list` call. The adapter's `US` market and `en-US` language filters do not prove where creators or viewers are located. YouTube records remain source evidence only and are not converted into keyword volume. The official YouTube quota references were checked 17 Nov 2026; provider limits can change:

- [YouTube Data API quota calculator](https://developers.google.com/youtube/data/determine_quota_cost?hl=en)
- [YouTube Data API `search.list` reference](https://developers.google.com/youtube/v3/docs/search/list?hl=en)

Before this report update, `git fetch origin` completed; local `main` was clean at `2c1308b57550c591a76679a99fc9cdc7c5d38d45`, one commit ahead of `origin/main` at `39b92351e8547e38e270138f9dd624961ea0ad34`. There was no incoming commit to synchronize, and nothing was pushed. This report and its two screenshots are the only additional acceptance artifacts.
