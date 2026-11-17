# TrendScout — Windows 2 YouTube Cache-First and Four-Source Correction

**Status: implementation committed; live Windows acceptance is pending.** This report records verified work for the post-D6 correction. It does not change or replace the completed D6 acceptance result.

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

## Live runtime state and remaining acceptance

The existing server on `127.0.0.1:8000` remains in NORMAL_DEMO. The guarded owner manual runtime was started separately on loopback `127.0.0.1:8001`; its capability response reported `OWNER_MANUAL_LIVE`, `d6-youtube-v2` approved, designated host, and server-side YouTube key presence. Startup logs show no provider request. No YouTube search request has been made by this agent, so no genuine Windows YouTube cache has yet been created and no YouTube quota has been consumed by this run.

Browser interaction remains pending. The user requested a hidden Chrome workflow and asked the agent not to control the Windows desktop. Although the owner reported Chrome MCP connected on port 9223, this session still exposes no Chrome DevTools MCP tool; the browser provider reports `browser access host unavailable`, `127.0.0.1:9223` has no listener, and the shell cannot resolve the suggested `chrome` hostname. The plugin directory search did not find a Chrome DevTools MCP connector, and available headless browser modules were not usable. No login, policy-consent click, or Fetch UI action was attempted after that instruction. The manual server is idle and awaits owner interaction.

The official Chrome DevTools MCP documentation supports attaching to a running Chrome through a configurable `--browser-url`, including a custom loopback port such as `http://127.0.0.1:9223`. This requires the owner to start Chrome with remote debugging enabled. Upstream warns that any local application able to reach that debugging port can control the browser; use a separate Chrome profile and keep the listener on loopback. No Chrome launch flags or MCP configuration were changed here. See [Chrome DevTools MCP connection guidance](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/advanced-usage.md) and [Chrome DevTools configuration](https://developer.chrome.com/docs/devtools/agents/get-started/configuration/), accessed 17 Nov 2026.

Consequently, these gates remain unverified: one real YouTube-only `search.list` response and SQLite persistence; Source Center/Run History/Data Quality evidence for that response; two NORMAL_DEMO cache-only reads across application restarts with unchanged provider usage and observation time; a combined four-source Fetch; and responsive/light-dark/English-Vietnamese browser QA. No claim of LIVE/CACHED YouTube data is made.

The official YouTube documentation accessed 17 Nov 2026 describes a default `search.list` bucket of 100 calls/day, with each call costing one quota unit, and documents `regionCode` and `relevanceLanguage`. Project-specific quota is unknown until checked in Google Cloud Console; no billing was enabled. The adapter is configured for `regionCode=US` and `relevanceLanguage=en`. These provider settings do not establish US authorship, audience, or demand.

- [YouTube Data API quota calculator](https://developers.google.com/youtube/data/determine_quota_cost?hl=en)
- [YouTube Data API `search.list` reference](https://developers.google.com/youtube/data/reference/rest/v3/search/list?hl=en)

To finish live acceptance, use the owner's browser session to complete policy consent and one YouTube-only Fetch against `http://127.0.0.1:8001/dashboard/`, then verify the persisted cache and NORMAL_DEMO readbacks, run the combined four-source Fetch without an unnecessary SerpAPI call, complete browser QA, and update this report with observed evidence before the final local documentation commit.
