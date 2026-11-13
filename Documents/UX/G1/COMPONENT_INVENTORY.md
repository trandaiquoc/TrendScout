# G1 Component Inventory

| Component | Location | G1 behavior and state |
| --- | --- | --- |
| Application shell | `Frontend/public/index.html`, `app.js`, `styles.css` | Sticky topbar, breadcrumb, backend connection status, light/dark control, VI/EN control, bounded workspace content, brand/policy footer |
| Primary sidebar | Same | Five primary destinations, active route, icon labels, desktop collapse/expand, persisted collapse preference |
| Advanced navigation | Same | Runs, Algorithms, and Quality grouped under a native disclosure |
| Mobile navigation drawer | Same | Scrim, inert background panel, focus placement and containment, Escape close, focus return |
| Dashboard tab list | Same | Overview and Analysis tabs with semantic roles, roving focus, route hash, arrow/Home/End navigation, and selected state |
| Overview metrics | Same | Provider records, stored keyword count, eligible keyword count, enabled source count, and last Fetch time; localized values and explicit unavailable states |
| Wikimedia evidence list | Same | Maximum five evidence-backed rows; empty message when the current response has no eligible observations |
| Existing keyword explorer | Same | Remains in Overview with its current search, filters, table, pagination, selection, and detail workflows |
| Analysis filter frame | Same | Source and observation-window controls are visibly disabled until the data supports filtering |
| Analysis cards | Same | Single-source retained snapshot count only when supported; explicit insufficient-evidence states for algorithm results, source effectiveness, multi-source coverage, and history |
| Source capability card | `Frontend/public/modules/capabilities.js` plus source cards in `index.html` | API-derived adapter, configuration, approval, historical local verification, current-operation enablement, health reporting, and production readiness |
| Collections | Existing section and API flow | Route and workflows preserved; no ledger relocation in G1 |
| Run lookup | Existing section and API flow | Manual Run ID lookup remains available under Advanced; no fictional run list |
| Algorithms and Quality | Existing sections and API flows | Remain available under Advanced |
| Research and Settings | Existing sections | Remain primary destinations |
| Loading, empty, notice, and dialog patterns | Existing shared CSS and markup | Loading skeleton on Analysis evidence; localized empty/unsupported state; existing notices and native dialogs retain focus behavior |

## Component state notes

- Source capability state comes from `GET /api/v1/capabilities`. If it is unavailable, values show unknown/unavailable rather than retaining a hard-coded verification claim.
- `LOCAL_LIVE_VERIFIED` is presented as historical local evidence. It is distinct from current operation enablement, current health, and production readiness.
- Filter controls remain disabled when there is no supported source/window contract for the current Analysis data.
- Existing controls retain their shared hover, active, focus-visible, and disabled patterns. Loading treatment is used only where an asynchronous evidence state exists.
