# TrendScout Dashboard

The dashboard is a browser-native HTML, CSS, and JavaScript workspace served by FastAPI at **/dashboard/**. It has ten views: Overview, Sources, Runs, Explore Keywords, Algorithm Comparison, Data Quality, Refresh Schedule, Product Research, AI Usage, and Settings. It uses native ES modules; no frontend framework, npm install, or local bundling step is required.

## Run locally

From the repository root, **./setup.sh** and **./start.sh** start the backend and dashboard. For a manual backend setup, see the [Backend README](../Backend/README.md). Open **http://127.0.0.1:8000/dashboard/**.

The workspace defaults to Light and Vietnamese. Theme and locale preferences persist in this browser under **trendscout.theme** and **trendscout.locale**; these keys contain only **light/dark** and **vi/en**. No credential, API key, Run ID, or user data is written to browser storage.

## Frontend structure

- **public/index.html** contains the semantic workspace shell and all ten views.
- **public/styles.css** owns design tokens, Light/Dark themes, focus styles, and responsive layouts.
- **public/app.js** composes views and interactions. It contains no HTTP write flow.
- **public/modules/api.js** is the single API boundary. It issues same-origin GET requests with a bounded timeout and validates JSON/HTTP errors.
- **public/modules/format.js** formats dates and numbers for the selected locale.
- **public/modules/theme.js** and **public/modules/i18n.js** manage display preferences.
- **public/modules/i18n/vi.js** and **public/modules/i18n/en.js** are separate user-facing copy catalogs.

The browser safely renders API values as text and only links to validated HTTPS Wikimedia-family hosts. Missing values remain distinct from zero. Run, keyword, and detail reads preserve API pagination and source evidence; stale, synthetic, partial, and failed runs remain labeled.

## Fetch and source readiness

The prominent **Fetch new data / Thu thập dữ liệu mới** button is disabled. The dashboard has no authenticated operator session, so it does not send a create-run request or simulate progress. The existing POST /api/runs contract remains behind its operator Bearer token; this UI does not collect, store, or transmit that token. Vercel remains read-only and returns 405 for run creation.

Source status is deliberately limited to repository evidence:

- **Wikimedia Pageviews:** adapter implemented; live connectivity and account access have not been verified.
- **YouTube, Google Search, and Bing Search:** adapters/credentials are not configured; they are not called.

GET /api/config reports only read_only; it is not a source, credential, or authenticated-session capability check. No cross-source data is combined. Weekly refresh, AI usage, and product research screens disclose their unconfigured or illustrative state.

## Static assets and Vercel

Local FastAPI serves the files under public/ at /dashboard/. The Vercel build runs Backend/scripts/prepare_vercel.py, which packages every non-hidden file under Frontend/public into the function's generated app/dashboard_assets.py. The same authenticated dashboard route serves HTML, CSS, and nested JavaScript modules; assets are not moved to an unprotected CDN path. Read-only Basic authentication continues to protect the dashboard and its static modules.

## Checks

From the repository root:

    bash Backend/scripts/verify_offline_readiness.sh

This runs backend regressions, Golden seeds, JavaScript syntax checks, recursive Vercel asset parity, offline Chrome workspace/keyboard/mobile checks, synthetic pipeline integration, and bounded fake-storage workloads. It does not contact Wikimedia, YouTube, search providers, Firestore, or Vercel.

The focused screenshot review can be repeated with Chrome installed:

    cd Backend
    .venv/bin/python scripts/seccomp_exec.py --allow-unix-sockets -- .venv/bin/python tests/browser_d2_visual_offline.py

This creates compact WebP evidence under Documents/Reports/evidence/d2/. Those fixture screenshots show UI states only; they do not establish live source access, production authentication, or market validity. See the [Backend README](../Backend/README.md) for API, operator authorization, storage, and deployment constraints.
