# G1 Implementation Report

**Status:** Complete
**Scope:** Application shell, information architecture, dashboard tabs, design foundation, runtime source status, and Windows browser acceptance.
**Commit message:** `feat(ui): implement G1 application shell and design foundation` (created locally; no push)

## Git baseline

Before implementation, `main`, local `HEAD`, and `origin/main` were aligned at `50e9eb8d5e96db7c8052d83a0a0918caa9b17372` (`docs(ux): add comprehensive G0 UX audit and design blueprint`). `git fetch origin` completed; ahead/behind was `0/0`, the working tree was clean, and required G0 commits `37368e3`, `fb3c43b`, and `d74e062` were present. No user changes were overwritten.

## Delivered

- Reorganized the sidebar into five primary destinations and three supported Advanced destinations. Removed navigation and unused panels for the duplicate Keywords route, Refresh schedule, and AI usage.
- Added semantic Overview and Analysis tabs with ARIA state, arrow-key/Home/End handling, native button activation, route hashes, and query-string preservation.
- Kept the current keyword explorer in Overview. Legacy `#keywords` and `#results` links still open Overview without losing query parameters.
- Added desktop sidebar collapse and mobile drawer focus management, focus trapping, Escape handling, and focus return.
- Added an evidence-based Overview summary for provider records, stored keywords, eligible keywords, runtime-enabled sources, and localized last Fetch time. Missing values remain unavailable.
- Limited the Wikimedia evidence list to five rows and retained its explanatory empty state when current evidence is absent.
- Added an Analysis layout and filter frame. It displays a retained keyword count only for a single-source snapshot with that count; unsupported algorithm evaluation, source effectiveness, multi-source coverage, and time-series views explain the missing evidence.
- Made all four source cards display runtime capability descriptors: adapter, configuration, approval, historical local verification, current-operation enablement, unreported current health, and production readiness. Historical local verification is not represented as production readiness.
- Added shared spacing, radius, typography, content-width, and motion tokens while preserving the existing warm light palette and dark theme.
- Added and verified Vietnamese and English copy for G1 UI and source status labels. Snapshot metrics and context rerender when locale changes.

## Acceptance gates

| Gate | Result | Evidence |
| --- | --- | --- |
| G1.1 Git synchronization and local-change preservation | PASS | Baseline and final commit recorded; no reset, rewrite, or push |
| G1.2 Design tokens | PASS | Shared tokens used by shell, spacing, tabs, cards, and analysis layout |
| G1.3 Application shell | PASS | Desktop and mobile Chrome interactions and screenshots |
| G1.4 Navigation and routes | PASS | Offline browser route checks; legacy aliases and invalid route fallback |
| G1.5 Two semantic Dashboard tabs | PASS | ARIA and keyboard checks in Windows Chrome |
| G1.6 No fabricated chart or metrics | PASS | Empty chart without new evidence; Analysis requires supporting snapshot data |
| G1.7 Runtime source status | PASS | Actual `/api/v1/capabilities` response and Sources screenshot |
| G1.8 VI/EN localization and key parity | PASS | 697 keys in each catalog; runtime locale switching checked |
| G1.9 Light and dark themes | PASS | Windows Chrome toggle and fresh dark screenshot |
| G1.10 Responsive layout | PASS | 320, 390, 768, 1024, 1440, and 1920 px checks; no page overflow |
| G1.11 Accessibility and keyboard navigation | PASS | Tabs, navigation, focus return, dialog Escape, and reduced-motion CSS checks |
| G1.12 Fetch, Collections, and existing data behavior | PASS | Offline regression suite and live local read-only route checks; no Fetch call |
| G1.13 Existing tests | PASS | 737 passed, 760 subtests passed |
| G1.14 Real Windows browser acceptance | PASS | Native Chrome 157.0.8103.51; interactions and fresh evidence under `Documents/Reports/windows-evidence/g1/` |
| G1.15 No backend/schema/data scope change | PASS | Changes are frontend, tests, and documentation/evidence only |
| G1.16 Documentation | PASS | Five G1 handoff documents in this folder |

## Validation

- `Backend/.venv/Scripts/python.exe -m pytest Backend/tests -q` — **737 passed, 760 subtests passed**.
- Offline Chrome workspace browser checks — PASS at 768, 1024, 1280, and 1440 px.
- Offline Chrome keyboard/dialog/mobile checks — PASS at 320 and 375 px emulation widths.
- Native Windows Chrome against the local runtime — PASS at 320, 390, 768, 1440, and 1920 px; the offline workspace checks additionally covered 1024 px.
- Runtime browser network observation — no external requests and no `/api/v1/fetches` request. Only local application reads were used; no provider Fetch was triggered.
- Ruff — PASS for changed Python test files.
- `node --check` — PASS for changed frontend JavaScript modules.
- Vietnamese/English catalog parity — PASS, 697 keys in each.
- `git diff --check` — PASS.

## Scope boundaries and next phase

No backend API, database schema, stored user data, AI engine, algorithm, scheduler, deployment, or dependency changed. G1 does not move the durable keyword ledger: the existing explorer remains in Overview pending the complete G2 Collections workbench. G3 remains responsible for the broader Source Center; G4 for validated analytics and chart definitions; G5 for run history; and later goals for the remaining product workflows. No G2 work was started.
