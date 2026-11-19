# TrendScout Dashboard

The dashboard is a browser-native HTML, CSS, and JavaScript workspace served by FastAPI at **/dashboard/**. Discover is the default screen and brings together Fetch, source scope, live summary, Wikimedia pageview evidence, and the bounded searchable candidate table. Sources, Product Research, and Settings are primary navigation; Runs, keyword detail, algorithms, quality, refresh, and usage views are under Advanced. It uses native ES modules; no frontend framework, npm install, or local bundling step is required.

## Run locally

From the repository root, run `..\start.ps1` to start the normal multi-source LIVE composition on loopback with one worker and local storage. It makes Wikimedia, Google Trends/Google Search, Bing Search and YouTube Search available when their credentials are configured. Google/Bing use `SERPAPI_API_KEY`; YouTube uses `YOUTUBE_API_KEY` from the root `.env` file. Startup does not contact providers; collection starts only after Fetch is selected. Server-owned quotas, the YouTube schedule and policy consent remain in force. Set `TREND_SCOUT_DEMO_PORT` to choose another port. This launcher selects LIVE, not synthetic fixtures. See the [Backend README](../Backend/README.md) for dependencies and the runtime boundary.

The workspace defaults to Light and Vietnamese. Theme and locale preferences persist in this browser under **trendscout.theme** and **trendscout.locale**; these keys contain only **light/dark** and **vi/en**. No credential, API key, Run ID, or user data is written to browser storage.

To use a different port, set `$env:TREND_SCOUT_DEMO_PORT = '8123'` before running `.\start.ps1`. Startup does not fetch provider data. Choose **Collections / Bộ sưu tập** to search and manage saved keywords. **Chưa xếp vào Bộ sưu tập / Not in a Collection** is the place for eligible keywords that have not been organized. Search, sorting, filters and paging read the local database only. **Organize saved keywords again / Sắp xếp lại từ khóa từ dữ liệu đã lưu** uses evidence already saved. Create, edit, merge and delete operate on the local Collections database; merge and delete require confirmation.

Collection counts show keywords placed directly in a Collection; parent counts include unique keywords in subcollections. Missing counts display as unavailable, not zero. Keyword details keep source records, analysis results and data limitations collapsed until opened. Wikimedia pageviews are approximate observed pageviews; they do not measure search volume or purchase intent.

## Frontend structure

- **public/index.html** contains the semantic workspace shell and all ten views.
- **public/styles.css** supplies the shared foundations; **public/phoenix.css** supplies the rebuilt shell and workspace layouts, Light/Dark presentation, and responsive refinements.
- **public/app.js** composes shared views and capability state; **public/modules/source-library.js**, **research-workspace.js**, and **run-history.js** own their respective screen interactions and request state.
- **public/modules/api.js** is the single HTTP boundary. It issues same-origin GET/POST requests with a bounded timeout and validates JSON/HTTP errors. POST requests are never retried automatically.
- **public/modules/capabilities.js** loads server-owned source and Fetch readiness.
- **public/modules/fetch.js** owns the anonymous, idempotent one-click Fetch and bounded status polling.
- **public/modules/format.js** formats dates and numbers for the selected locale.
- **public/modules/theme.js** and **public/modules/i18n.js** manage display preferences.
- **public/modules/i18n/vi.js** and **public/modules/i18n/en.js** are separate user-facing copy catalogs.

The browser safely renders API values as text and only links to validated HTTPS Wikimedia-family hosts. Missing values remain distinct from zero. Run, keyword, and detail reads preserve API pagination and source evidence; stale, synthetic, partial, and failed runs remain labeled. Synthetic fixtures are not selected by the Windows launcher.

## Fetch and source readiness

The dashboard has no login, logout, registration, account, or credential entry flow. It reads `GET /api/v1/capabilities` anonymously. The Windows launcher runs local multi-source LIVE mode; Fetch uses the bounded `POST /api/v1/fetches` contract and backend-selected source scope. Fixture-based synthetic behavior is separate from this launcher. The browser creates an idempotency key in memory, does not retry POST automatically, and never stores or sends operator/provider credentials.

The Vercel application serves dashboard HTML, CSS, and JavaScript modules anonymously. Private `/api/runs/...` reads remain behind HTTP Basic and the configured Vercel OIDC/Firestore identity; a missing `TREND_SCOUT_READ_PASSWORD` blocks those private reads, not dashboard rendering. Vercel Deployment Protection is a separate platform gate and may still require authentication before a request reaches the application.

The legacy `POST /api/runs` route remains separate and still requires its operator Bearer token. The bounded `POST /api/v1/fetches` route accepts an optional source selection; the backend enforces the allowed source set, query plan, request budget, and runtime mode. In Vercel, SQLite and legacy run creation remain read-only. When `TREND_SCOUT_VERCEL_SHARED_LIVE_ENABLED=true`, only the Fetch route uses request-scoped Firestore Shared LIVE storage and Wikimedia is the only selectable provider; the flag defaults to off. The Settings view labels the SQLite store as read-only while making this Firestore Fetch path clear. The local synthetic demo's Google, Bing, and YouTube fixtures use MockTransport and do not call those providers.

Source status is deliberately limited to runtime capability and returned source evidence:

- **Wikimedia Pageviews:** normal local LIVE composition is available; individual Fetch requests can fail or return partial/cached data. The dashboard distinguishes LIVE from cached delivery.
- **YouTube, Google Search, and Bing Search:** the Windows LIVE profile enables these when the corresponding credentials are present and runtime gates pass. Capability readiness does not verify that a provider will accept a request; quota, schedule, consent and provider errors can still prevent or limit a Fetch.

Capability data distinguishes adapter implementation from approval, configuration, live verification, and source limitations. No cross-source data is combined. Local search/filter operates only on the API-returned candidate subset (maximum 100); the dashboard identifies this bound when extraction returns more candidates. Weekly refresh, AI usage, and product research screens disclose their unconfigured or illustrative state.

## Static assets and Vercel

Local FastAPI serves the files under public/ at /dashboard/. The Vercel build runs Backend/scripts/prepare_vercel.py, which packages every non-hidden file under Frontend/public into the function's generated app/dashboard_assets.py. The same application route serves the public HTML, CSS, and nested JavaScript modules. Private run data stays behind its API boundary; assets are not moved to an unrelated CDN path.

## Checks

From the repository root, run:

    .\verify_offline_readiness.ps1

This Windows-native script runs backend regressions, Golden seeds, JavaScript syntax checks, offline Chrome workspace/keyboard/mobile checks, synthetic pipeline integration, and bounded fake-storage workloads. It does not contact Wikimedia, YouTube, search providers, Firestore, or Vercel. See the [Backend README](../Backend/README.md) for API, legacy operator authorization, public Fetch limits, storage, and deployment constraints.
