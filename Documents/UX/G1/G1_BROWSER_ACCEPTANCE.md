# G1 Windows Browser Acceptance

**Result:** PASS
**Capture date:** 2026-11-13 (local environment)
**Browser:** Native Windows Google Chrome 157.0.8103.51
**Application:** Local `start.ps1 -Port 8000`, normal Wikimedia local runtime. Startup made no automatic Fetch.

## Interactions exercised

- Loaded the real local dashboard and read its local health, capability, and latest-snapshot endpoints.
- Verified the five Overview metrics, the single currently enabled source count, compact localized last-Fetch time, and evidence-backed empty Top 5 state.
- Collapsed and expanded the desktop sidebar.
- Selected Analysis and verified the semantic tab state and query-string preservation; navigated with an arrow key.
- Opened Sources and checked the actual capability response, including Wikimedia historical local LIVE verification, operation enablement, health not reported, and not-production-ready status.
- Opened Collections and confirmed its existing route remains available.
- Opened legacy `#keywords` with a query parameter and confirmed the Overview explorer remains available and the query remains intact.
- Switched light/dark themes and Vietnamese/English. Confirmed snapshot summary copy rerenders in English.
- At 390 px, opened and closed the mobile drawer, verified focus moved into it, then verified Escape returned focus to the menu button.
- At 320 px, selected Analysis and checked the page fit.
- Checked horizontal overflow at 320, 390, 768, 1024, 1440, and 1920 px. Native runtime capture measured exact 320 and 390 px mobile visual viewports. Offline Chrome additionally covered 768, 1024, 1280, and 1440 px.
- Observed browser network events: no external request and no `/api/v1/fetches` request. No provider Fetch was triggered.

## Fresh screenshots

All PNGs below were captured for G1 and are stored in `Documents/Reports/windows-evidence/g1/`.

| Screenshot | View |
| --- | --- |
| `g1-desktop-overview-1440.png` | Light Vietnamese Overview with actual local runtime snapshot and source count |
| `g1-desktop-analysis-1440.png` | Analysis tab with real single-source retained count and explained empty states |
| `g1-sources-capabilities-1440.png` | Runtime capability descriptors and separated source status axes |
| `g1-collections-1440.png` | Existing Collections destination |
| `g1-desktop-dark-en-1440.png` | Dark English Overview and localized snapshot summary |
| `g1-mobile-overview-390.png` | 390 px mobile Overview and drawer focus return |
| `g1-mobile-analysis-320.png` | 320 px mobile Analysis |
| `g1-tablet-overview-768.png` | 768 px Overview |
| `g1-wide-overview-1920.png` | 1920 px Overview |

## Supporting checks

- `Backend/tests/browser_workspace_offline.py` — PASS at 768, 1024, 1280, and 1440 px with mocked local responses; covers routes, locale/theme, capability status axes, query preservation, XSS-safe rendering, and no Fetch submission.
- `Backend/tests/browser_keyboard_offline.py` — PASS for keyboard navigation, mobile drawer, dialog Escape, tab arrows, locale/theme, and 320/375 px emulation.
- Full backend suite — 737 passed, 760 subtests passed.
- Native Windows runtime acceptance is separate from the offline fixture checks; no screenshot is reused from G0.
