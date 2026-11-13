# TrendScout UX redesign and live start acceptance

**Date:** 2026-11-13
**Scope:** Discover experience and local Wikimedia LIVE launcher. No push or deployment performed.

## Git baseline

- Starting `HEAD` and fetched `origin/main`: `0efd9ef6b7c648a6dde6f8080b16e812863b6ee7`.
- The working tree was clean before this task. The remote was fetched and no upstream changes needed merging.
- The feature implementation commit is recorded below. A report-only follow-up commit stores this hash in the report.

## Research reviewed

- [Lucian Labs UX/UI prompt](https://lucianlabs.dev/uxui-prompt/) for hierarchy, clarity, and polished interaction patterns.
- [The Block Laundromat](https://the-block-laundromat.vercel.app/) for the owner’s light visual atmosphere. Its identity and assets were not copied.
- [Shopify custom explorations](https://help.shopify.com/en/manual/reports-and-analytics/shopify-reports/report-types/custom-reports/create-custom-explorations) for filterable, self-serve data exploration.
- [W3C WCAG 2.2](https://www.w3.org/TR/WCAG22/) as the accessibility reference.

## What changed

- `bash start.sh` now runs the normal local Wikimedia LIVE composition, binds only to loopback, and opens `http://127.0.0.1:8000/dashboard/` by default. Set `TREND_SCOUT_DEMO_PORT` only to choose a different local port. A clear error reports occupied ports. `bash start.sh --synthetic` retains the offline fixture runtime.
- Discover is the landing page and keeps one prominent Fetch action with the summaries, ranked source evidence, source scope and candidate explorer together. Search, category filters, evidence sorting and candidate details are available without navigating to technical views.
- Four destinations remain in primary navigation; runs, algorithm, quality, refresh, usage and keyword internals are grouped under Advanced.
- The dashboard uses a warm neutral visual palette, soft cards and responsive candidate rows. Vietnamese and English, light and dark themes remain available. Existing Advanced pages remain reachable.
- The public Wikimedia display uses provider evidence and explicit `INSUFFICIENT_DATA` status. It does not create a trend curve or infer consumer demand from Wikimedia pageviews. Source-specific boundaries and activation gates were left intact; no Google, Bing or YouTube LIVE work was started.
- The Discover summary separates the latest Fetch completion time from the provider evidence’s collection time, and makes source storage omissions and candidate result limits visible.

## Startup and live browser evidence

The standard launch command is:

```bash
bash start.sh
```

The normal launcher was started on its default port. `GET /api/health` returned HTTP 200 and capabilities reported `data_mode=LIVE`, local live demo enabled, and Wikimedia public Fetch enabled. It made no provider request during startup. A run of the user-facing Fetch action in native Linux Chrome completed as `LIVE · CACHED` from the persistent local Wikimedia cache.

The rendered result showed 1,000 provider-accepted records, 212 locally stored records, and 788 omitted by the existing storage budget; 717 candidates were extracted and 100 returned to the browser. Evidence covered observation date 2026-11-11 and Wikimedia `US` / `all-access`. The cache’s source-data time (11:12 local on 2026-11-13) is distinguished from this Fetch completion time (12:21 local). This verification therefore confirms the normal LIVE composition, cache path and UI; it does not claim a fresh provider request during this run.

Port-conflict handling was verified with both the running app and a dummy loopback listener. The launcher exited with a clear occupied-port message. The explicit synthetic launcher was also exercised on a non-default local port and reported synthetic mode.

## Browser evidence and accessibility

- Native Linux Chrome exercised the actual local dashboard and Fetch flow. Candidate search returned the expected `home` row; sorting by view evidence worked. At widths 1440, 1280, 1024, 768, 375 and 320 px, the document and body had no horizontal overflow.
- The responsive workspace, bilingual copy, themes, keyboard navigation, source capability gates, sanitization, and browser Fetch paths passed the project’s focused browser acceptance scripts using the documented seccomp wrapper. The Wikimedia synthetic browser E2E passed in a route-less loopback network namespace.
- Lighthouse audit: Accessibility 100, Best Practices 100, SEO 100 and Agentic 100; 36 checks passed and 0 failed.
- Screenshot evidence (WebP) is in [`evidence/ux-redesign/`](evidence/ux-redesign/):
  - [`discover-1440-light-vi.webp`](evidence/ux-redesign/discover-1440-light-vi.webp)
  - [`discover-1440-dark-en.webp`](evidence/ux-redesign/discover-1440-dark-en.webp)
  - [`discover-1280-light-vi.webp`](evidence/ux-redesign/discover-1280-light-vi.webp)
  - [`discover-1024-light-vi.webp`](evidence/ux-redesign/discover-1024-light-vi.webp)
  - [`discover-768-light-vi.webp`](evidence/ux-redesign/discover-768-light-vi.webp)
  - [`discover-375-light-vi.webp`](evidence/ux-redesign/discover-375-light-vi.webp) and [`discover-375-viewport-light-vi.webp`](evidence/ux-redesign/discover-375-viewport-light-vi.webp)
  - [`discover-320-light-vi.webp`](evidence/ux-redesign/discover-320-light-vi.webp) and [`discover-320-viewport-light-vi.webp`](evidence/ux-redesign/discover-320-viewport-light-vi.webp)

## Validation run

- `node --check` on the edited ES modules — passed.
- `bash -n start.sh demo.sh` — passed.
- Focused dashboard contract/capability unit tests — 7 passed, including a provider-count versus analysis-count contract.
- Offline native-Chrome regression scripts passed, including workspace, keyboard, public Fetch, distributed Fetch, pipeline, and loopback-only synthetic Wikimedia flows. The offline browser scripts require `Backend/scripts/seccomp_exec.py`; a plain shell or a route-less namespace alone does not satisfy their socket-blocking precondition.
- Live Chrome Fetch/search/sort, visible status, locale/theme and six responsive widths were verified after the final UI copy and Fetch-timestamp edits.
- `git diff --check` — passed before commit.

## Data limits and known constraints

- Wikimedia Pageviews measures page activity, not a representative sample of U.S. consumers, search volume, social popularity or purchase intent. `US` plus `all-access` describes the provider query scope, not user identity or demand.
- The local storage budget can omit accepted provider records; this run omitted 788. The browser only receives the returned candidate subset (100 of 717 extracted), and search/filtering operates over that returned subset.
- The saved source records were cached, so their 11:12 source fetch time is older than the 12:21 UI Fetch completion. `LIVE · CACHED` communicates this distinction.
- Provider records did not meet the trend baseline rules, so the candidates remain `INSUFFICIENT_DATA`. No score or trend curve is asserted for these results.
- This local launcher requires the repository’s configured Python 3.12 virtual environment and existing backend dependencies. It requires no API key or authorization ID for the normal Wikimedia Fetch path.

## G1–G8 acceptance

| Gate | Result | Evidence |
|---|---|---|
| G1 — Git synchronized safely | PASS | Fetched `origin`; starting HEAD matched the known baseline and `origin/main`; no overwrite or merge needed. |
| G2 — `start.sh` | PASS | Default normal Wikimedia LIVE starts on loopback port 8000; capability and UI Fetch enabled; occupied-port error verified; `--synthetic` remains available. |
| G3 — Main UX | PASS | Discover is the default page with one Fetch action, summary, source evidence and searchable candidates; technical views are under Advanced. |
| G4 — Visual design | PASS | Responsive light/dark and VI/EN experience implemented; visual evidence saved; Lighthouse Accessibility 100. |
| G5 — Data exploration | PASS | Search, categories, evidence sort and detail links work; source scope, timestamps, storage omissions and candidate cap are shown; no unvalidated trend label is asserted. |
| G6 — Source architecture | PASS | Existing provider boundaries and scoring rules remain; Wikimedia alone is live-enabled; no unrelated provider activation. |
| G7 — Browser and regression | PASS | Native Linux Chrome Fetch/search/sort and six responsive widths verified; 7 focused unit tests pass; focused offline browser suites pass with the repository’s isolation tools. |
| G8 — Handoff | PASS | Startup docs, screenshot evidence and this report are included; a local commit is recorded below; no push/deployment. |

## Local commit

- Feature implementation commit: `a3d132cfa94cbca6987a87700c910bb574a421b2` (`feat(ui): redesign discover and live start`).
- The report hash is recorded by a separate local documentation commit. No remote push was made.
