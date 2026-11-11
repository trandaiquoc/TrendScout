# TrendScout Dashboard

The dashboard is a browser-native HTML, CSS, and JavaScript workspace served by FastAPI at **/dashboard/**. It has ten views: Overview, Sources, Runs, Explore Keywords, Algorithm Comparison, Data Quality, Refresh Schedule, Product Research, AI Usage, and Settings. It uses native ES modules; no frontend framework, npm install, or local bundling step is required.

## Run locally

From the repository root, `bash demo.sh` starts the loopback synthetic presentation with ephemeral storage and no account or credential. Open **http://127.0.0.1:8000/dashboard/**. For dependencies, an alternate port or the separately configured legacy cloud runtime, see the [Backend README](../Backend/README.md).

The workspace defaults to Light and Vietnamese. Theme and locale preferences persist in this browser under **trendscout.theme** and **trendscout.locale**; these keys contain only **light/dark** and **vi/en**. No credential, API key, Run ID, or user data is written to browser storage.

## Frontend structure

- **public/index.html** contains the semantic workspace shell and all ten views.
- **public/styles.css** owns design tokens, Light/Dark themes, focus styles, and responsive layouts.
- **public/app.js** composes views, capability state, and interactions.
- **public/modules/api.js** is the single HTTP boundary. It issues same-origin GET/POST requests with a bounded timeout and validates JSON/HTTP errors. POST requests are never retried automatically.
- **public/modules/capabilities.js** loads server-owned source and Fetch readiness.
- **public/modules/fetch.js** owns the anonymous, idempotent one-click Fetch and bounded status polling.
- **public/modules/format.js** formats dates and numbers for the selected locale.
- **public/modules/theme.js** and **public/modules/i18n.js** manage display preferences.
- **public/modules/i18n/vi.js** and **public/modules/i18n/en.js** are separate user-facing copy catalogs.

The browser safely renders API values as text and only links to validated HTTPS Wikimedia-family hosts. Missing values remain distinct from zero. Run, keyword, and detail reads preserve API pagination and source evidence; stale, synthetic, partial, and failed runs remain labeled.

## Fetch and source readiness

The dashboard has no login, logout, registration, account, or credential entry flow. It reads `GET /api/v1/capabilities` anonymously. Fetch uses the separate `POST /api/v1/fetches` contract only when the backend explicitly enables the offline synthetic runtime; the feature flag defaults off and production/Vercel cannot enable it. In an enabled local/test fixture run, the button executes the injected fixture, reports its real backend state, and reads the resulting run. Otherwise it stays disabled with the backend's reason. The browser creates an idempotency key in memory, does not retry POST automatically, and never stores or sends operator/provider credentials.

The Vercel application serves dashboard HTML, CSS, and JavaScript modules anonymously. Private `/api/runs/...` reads remain behind HTTP Basic and the configured Vercel OIDC/Firestore identity; a missing `TREND_SCOUT_READ_PASSWORD` blocks those private reads, not dashboard rendering. Vercel Deployment Protection is a separate platform gate and may still require authentication before a request reaches the application.

The legacy `POST /api/runs` route remains separate and still requires its operator Bearer token. The public Fetch route accepts only `{}` and the backend selects fixed sources, query plan, request budget, and synthetic mode. The demo runs one fixture query each through Google, Bing and YouTube adapters using MockTransport; it reports incomplete scope and shows their candidate evidence separately from the Wikimedia score table. Vercel remains read-only and returns 405 for Fetch/run creation. Live Wikimedia and paid/provider APIs are not called by this flow.

Source status is deliberately limited to repository evidence:

- **Wikimedia Pageviews:** adapter implemented; live connectivity and account access have not been verified.
- **YouTube, Google Search, and Bing Search:** adapters are tested with offline fixtures. Live credentials, approval and live connectivity are not verified.

Capability data distinguishes adapter implementation from approval, configuration, live verification, and source limitations. No cross-source data is combined. Weekly refresh, AI usage, and product research screens disclose their unconfigured or illustrative state.

## Static assets and Vercel

Local FastAPI serves the files under public/ at /dashboard/. The Vercel build runs Backend/scripts/prepare_vercel.py, which packages every non-hidden file under Frontend/public into the function's generated app/dashboard_assets.py. The same application route serves the public HTML, CSS, and nested JavaScript modules. Private run data stays behind its API boundary; assets are not moved to an unrelated CDN path.

## Checks

From the repository root:

    bash Backend/scripts/verify_offline_readiness.sh

This runs backend regressions, Golden seeds, JavaScript syntax checks, recursive Vercel asset parity, offline Chrome workspace/keyboard/mobile checks, synthetic pipeline integration, and bounded fake-storage workloads. It does not contact Wikimedia, YouTube, search providers, Firestore, or Vercel.

The focused screenshot review can be repeated with Chrome installed:

    cd Backend
    .venv/bin/python scripts/seccomp_exec.py --allow-unix-sockets -- .venv/bin/python tests/browser_d2_visual_offline.py

This creates compact WebP evidence under Documents/Reports/evidence/d2/. Those fixture screenshots show UI states only; they do not establish live source access, production abuse resistance, or market validity. See the [Backend README](../Backend/README.md) for API, legacy operator authorization, public Fetch limits, storage, and deployment constraints.
