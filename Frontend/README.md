# TrendScout Dashboard

The dashboard is a browser-native HTML, CSS, and JavaScript workspace served by FastAPI at **/dashboard/**. Discover is the default screen and brings together Fetch, source scope, live summary, Wikimedia pageview evidence, and the bounded searchable candidate table. Sources, Product Research, and Settings are primary navigation; Runs, keyword detail, algorithms, quality, refresh, and usage views are under Advanced. It uses native ES modules; no frontend framework, npm install, or local bundling step is required.

## Run locally

From the repository root, run `..\start.ps1` to start the normal multi-source LIVE composition on loopback with one worker and local storage. It makes Wikimedia, Google Trends/Google Search, Bing Search and YouTube Search available when their credentials are configured. Google/Bing use `SERPAPI_API_KEY`; YouTube uses `YOUTUBE_API_KEY` from the root `.env` file. Startup does not contact providers; collection starts only after Fetch is selected. Server-owned quotas, the YouTube schedule and policy consent remain in force. Set `TREND_SCOUT_DEMO_PORT` to choose another port. This launcher selects LIVE, not synthetic fixtures. See the [Backend README](../Backend/README.md) for dependencies and the runtime boundary.

The workspace defaults to Light and Vietnamese. Theme and locale preferences persist in this browser under **trendscout.theme** and **trendscout.locale**; these keys contain only **light/dark** and **vi/en**. No credential, API key, Run ID, or user data is written to browser storage.

For reloads, the browser also keeps a versioned copy of the public capabilities response. It can render the last known Fetch readiness immediately while a background check runs; stale status is labelled, and the server still validates policy, approval, and quota on every Fetch. This cache contains no credentials, Fetch IDs, run results, or private user data. Reloading never submits a Fetch; Run History offers an explicit **Continue Fetch** action for a recovered durable job and resubmits its stored source scope so the server can join the queued job and reuse its checkpoints.

To use a different port, set `$env:TREND_SCOUT_DEMO_PORT = '8123'` before running `.\start.ps1`. Startup does not fetch provider data. Choose **Collections / Bộ sưu tập** to search and manage saved keywords. **Chưa xếp vào Bộ sưu tập / Not in a Collection** is the place for eligible keywords that have not been organized. Search, sorting, filters and paging read the local database only. **Organize saved keywords again / Sắp xếp lại từ khóa từ dữ liệu đã lưu** uses evidence already saved. Create, edit, merge and delete operate on the local Collections database; merge and delete require confirmation.

Collection counts show keywords placed directly in a Collection; parent counts include unique keywords in subcollections. Missing counts display as unavailable, not zero. Keyword details keep source records, analysis results and data limitations collapsed until opened. Wikimedia pageviews are approximate observed pageviews; they do not measure search volume or purchase intent.

## Frontend structure

- **public/index.html** contains the semantic workspace shell and all ten views.
- **public/styles.css** supplies the shared foundations; **public/phoenix.css** supplies the rebuilt shell and workspace layouts, Light/Dark presentation, and responsive refinements.
- **public/app.js** composes shared views and capability state; **public/modules/source-library.js**, **research-workspace.js**, and **run-history.js** own their respective screen interactions and request state.
- **public/modules/api.js** is the single HTTP boundary. It issues same-origin GET/POST requests with a bounded timeout and validates JSON/HTTP errors. Fetch submission and checkpoint-recovery POSTs use the Vercel function deadline; only the idempotent Fetch controller retries them with the same request key.
- **public/modules/capabilities.js** loads server-owned source and Fetch readiness and stores the last-known public response for immediate, stale-labelled rendering after reload.
- **public/modules/fetch.js** owns the anonymous, idempotent one-click Fetch, bounded status polling, automatic same-key recovery after transient errors or lease expiry, and explicit recovery of a durable job. Vercel Shared LIVE starts the first bounded source slice immediately after enqueue acknowledgement and advances after an acknowledged slice is confirmed queued; an uncertain continuation still polls the existing job before retrying.
- **public/modules/format.js** formats dates and numbers for the selected locale.
- **public/modules/theme.js** and **public/modules/i18n.js** manage display preferences.
- **public/modules/i18n/vi.js** and **public/modules/i18n/en.js** are separate user-facing copy catalogs.

The browser safely renders API values as text and only links to validated HTTPS Wikimedia-family hosts. Missing values remain distinct from zero. Run, keyword, and detail reads preserve API pagination and source evidence; stale, synthetic, partial, and failed runs remain labeled. Synthetic fixtures are not selected by the Windows launcher.

## Fetch and source readiness

The local dashboard has no account login or registration. It reads `GET /api/v1/capabilities` anonymously. The Windows launcher runs local multi-source LIVE mode; Fetch uses the bounded `POST /api/v1/fetches` contract and backend-selected source scope. Fixture-based synthetic behavior is separate from this launcher. The browser creates an idempotency key in memory and never stores provider credentials. The current Vercel Production project has SSO Deployment Protection enabled for all deployment types; Shared LIVE Fetch relies on that edge gate, while provider credentials remain in Vercel environment variables. Keep SSO protection enabled for the Production deployment.

Vercel SSO Deployment Protection gates the Production dashboard and API before requests reach the application. Private `/api/runs/...` reads also remain behind HTTP Basic and the configured Vercel OIDC/Firestore identity; a missing `TREND_SCOUT_READ_PASSWORD` blocks those private reads. Local development does not use the Production SSO gate.

The legacy `POST /api/runs` route remains separate and still requires its operator Bearer token; its process-backed RunService is not started in Vercel Functions. The bounded `POST /api/v1/fetches` route accepts an optional source selection; the backend enforces the allowed source set, query plan, request budget, and runtime mode. Vercel Functions are stateless, so Shared LIVE Fetch, its history, and Collections use request-scoped Firestore identity and the existing shared database. Collections keeps its SQLite query model in a bounded working copy that is restored from and committed to Firestore. On Vercel, the first Fetch POST admits the durable job and returns its ID without starting provider work; each same-key continuation runs at most one bounded source slice. Terminal Collections processing and snapshot persistence finish before the final slice response. Expired work resumes from durable source checkpoints. A terminal Fetch whose Collections snapshot is missing can be replayed from retained LIVE checkpoints through `POST /api/v1/fetches/{fetch_id}/collections`; that path does not run provider adapters. When `TREND_SCOUT_VERCEL_SHARED_LIVE_ENABLED=true`, the current owner-approved Vercel setup supports Wikimedia Pageviews, Google Trends/Search, Bing Search, and YouTube Search. Google and Bing manual refreshes bypass only scheduled clock windows and still honor shared eight-hour source spacing, durable admission, and provider quota accounting. SerpApi account usage above the local source ledger is reserved in the shared monthly pool as unattributed usage, preserving the separate Google and Bing allocations. Selecting Bing also admits Google first, whose completed same-job checkpoint can supply candidates before Collections processing; candidate records retain Google provenance. Google SERP verification remains limited to its existing two selected candidates per batch. YouTube retains up to 50 normalized records when they fit the configured 3.5 MB result/checkpoint cap, and oversized checkpoint records use the existing snapshot blob store. The YouTube consent policy remains `d6-youtube-v2`; the user accepts or revokes it in Settings, and the browser sends only its policy version with YouTube Fetch requests. The flag defaults to off. The local synthetic demo's Google, Bing, and YouTube fixtures use MockTransport and do not call those providers.

Source status is deliberately limited to runtime capability and returned source evidence:

- **Wikimedia Pageviews:** normal local LIVE composition is available; individual Fetch requests can fail or return partial/cached data. The dashboard distinguishes LIVE from cached delivery.
- **YouTube, Google Search, and Bing Search:** the Windows LIVE profile enables these when the corresponding credentials are present and runtime gates pass. Capability readiness does not verify that a provider will accept a request; quota, schedule, consent and provider errors can still prevent or limit a Fetch.

Capability data distinguishes adapter implementation from approval, configuration, live verification, and source limitations. No cross-source data is combined. Local search/filter operates only on the API-returned candidate subset (maximum 100); the dashboard identifies this bound when extraction returns more candidates. Weekly refresh, AI usage, and product research screens disclose their unconfigured or illustrative state.

## Static assets and Vercel

Local FastAPI serves the files under Frontend/public at /dashboard/. The Vercel build runs Backend/scripts/prepare_vercel.py and stages the same non-hidden files under Backend/public/dashboard. Vercel FastAPI static CDN is enabled and excludes those files from the Python Function bundle; Backend/vercel.json redirects / to /dashboard/. The FastAPI mount remains available for local runs and as the configured application path. Private run data stays behind its protected API boundary.

## Checks

From the repository root, run:

    .\verify_offline_readiness.ps1

This Windows-native script runs backend regressions, Golden seeds, JavaScript syntax checks, offline Chrome workspace/keyboard/mobile checks, synthetic pipeline integration, and bounded fake-storage workloads. It does not contact Wikimedia, YouTube, search providers, Firestore, or Vercel. See the [Backend README](../Backend/README.md) for API, legacy operator authorization, public Fetch limits, storage, and deployment constraints.
