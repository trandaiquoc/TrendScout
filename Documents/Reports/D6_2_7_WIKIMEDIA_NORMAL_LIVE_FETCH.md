# D6.2.7 — Wikimedia normal LIVE Fetch end-to-end

Date: 2026-11-13 (Asia/Ho_Chi_Minh)

Status: **G1–G8 PASS for the local Linux demo.** This verifies one bounded Wikimedia source and one local runtime; it is not production or multi-host acceptance.

## Result

The dashboard Fetch action now uses a separate `NORMAL_WIKIMEDIA_LIVE_V1` local runtime. It reads the latest available cached scope first, then requests Wikimedia's US / all-access top-page pageviews when needed. The request uses the descriptive `TrendScout/0.1 (https://github.com/trandaiquoc/TrendScout)` User-Agent. The backend persists the versioned operational state separately from the retired D6 smoke ledger. No lifetime request cap or per-Fetch approval is used. Google, Bing, and YouTube remain unavailable.

Two deliberate Wikimedia GETs were made in this goal. The first returned HTTP 200, but the HTTP client failed while decoding the response body; the result was rejected and no records were published. Diagnostics were inspected before changing the request to send `Accept-Encoding: identity`. The second GET returned HTTP 200 and a complete 86,201-byte response. Parsing and validation accepted 1,000 provider entries with zero rejected; the local per-source storage bound persisted 212 normalized entries and reported 788 omitted by that bound. The successful observation date was **2026-11-11**; the fetch timestamp is recorded per entry. No other provider was contacted.

The successful sample is Wikimedia's country-scoped US, all-access daily top-page list. Country attribution and pageview ranks describe the provider's buckets and sample, not authorship, total US demand, search volume, purchase intent, or complete market coverage. The configured `views_ceil` values are approximate pageviews. No one-day trend baseline was available, so scores remain null/`INSUFFICIENT_EVIDENCE`.

## Dashboard verification

On native Linux Chrome at `http://127.0.0.1:8001/dashboard/`, the real Fetch action completed through the local API, worker, Wikimedia parser, normalized storage and discovery. The dashboard displayed `Wikimedia · LIVE · PARTIAL · 212`; opening a result showed LIVE provenance, article, project, provider rank, approximate views, observation date, fetch timestamp and source URL. English and Vietnamese labels rendered. Search for `home` narrowed 717 extracted candidates to one, and the category filter remained usable.

A second dashboard Fetch was served as `CACHED` from persistent local cache, with no third provider GET. Cache delivery is shown separately from original LIVE provenance. The browser's observed network requests were to loopback only; Wikimedia was contacted by the backend, not by browser JavaScript. Dashboard state included partial storage coverage and did not invent a volume score.

## Gates

| Gate | Result | Evidence |
| --- | --- | --- |
| G1 Git synchronized safely | PASS | Starting worktree was clean; `HEAD` matched `origin/main` at `c65506d14fb71ff131435c5c71d11ecbbcf33c42`. |
| G2 Normal Wikimedia LIVE runtime | PASS | New versioned local policy/composition serves the dashboard without the retired D6 activation path. |
| G3 No experimental lifetime cap | PASS | New SQLite namespace and policy have bounded daily/monthly operational budgets, no lifetime cap, and are separate from historical smoke receipts. |
| G4 Rate, cache, errors, duplicates | PASS | One request at a time, persistent one-second pacing, bounded attempts/date fallback, Retry-After handling, cache, same-key admission and duplicate protections; offline tests cover response/error/cache paths. |
| G5 Actual provider GET and validation | PASS | 2 total intentional Wikimedia GETs: one HTTP 200 body decode failure before the transport fix; one HTTP 200 complete response, 1,000 accepted, 0 rejected, 212 stored, 788 omitted by local storage bound. |
| G6 API/worker/discovery/dashboard | PASS | Native Chrome real Fetch rendered LIVE normalized evidence, provenance, search/filter, bilingual UI, then persistent CACHED delivery without another GET. |
| G7 Focused regressions/security | PASS | 96 focused unittest cases passed; Ruff, Python compilation, JavaScript syntax and whitespace checks passed. Local-only guard and source allowlist remain active. Historical D6 receipts were not opened or modified. |
| G8 Docs, launcher, local commit | PASS | This report, D6 roll-up, and module launcher are included. Local commit is recorded in the handoff below; nothing was pushed or deployed. |

## Offline validation

The final focused unittest run passed 96 cases across the new local composition and dashboard/API integration plus existing Wikimedia gateway, source checkpoint, worker/admission, public-fetch, local HTTP and strict offline/synthetic regression cases. Test transports are mocked; the two live GETs above are separate recorded provider verification. `pytest` is not installed; this repository's focused suites use `unittest`.

The launcher is `cd Backend && .venv/bin/python -m scripts.run_wikimedia_live_local`. It binds only to loopback and allows one worker. Its persistent operational state is stored under the current user's `~/.local/state/trendscout/normal-wikimedia-live-v1/` directory.

## Provider references checked 2026-11-13

- [Wikimedia Analytics API access policy](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/access-policy.html)
- [AQS troubleshooting](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/troubleshooting.html)
- [Wikimedia Foundation User-Agent policy](https://foundation.wikimedia.org/wiki/Policy%3AWikimedia_Foundation_User-Agent_Policy/en)
- [Country data semantics](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/concepts/country-data.html)

The documentation lookup confirms the published endpoint and policy context; it does not establish continuing availability, future quota behavior, or any provider guarantee. This report records the observed request outcome on the stated date.

## Handoff

- Git baseline: `c65506d14fb71ff131435c5c71d11ecbbcf33c42`
- Changed files: see the local D6.2.7 commit.
- Provider calls: 2 Wikimedia GETs; HTTP statuses 200 and 200; first body decode failed, second parsed completely.
- Accepted / rejected / stored / omitted: 1,000 / 0 / 212 / 788.
- Observation date: `2026-11-11`.
- Cache: second dashboard Fetch returned `CACHED`; no additional provider request.
- Dashboard: native Linux Chrome local E2E PASS.
- Tests: 96 focused unittests PASS; changed-file Ruff, compilation, JS syntax and `git diff --check` PASS.
- Commit: `feat(d6): enable normal wikimedia live fetch`; local only, no push or deployment.
