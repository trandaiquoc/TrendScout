# Project Phoenix — checkpoint

Updated 2026-11-15 · local only · in progress

- Latest code commit: `824645e`. Source library, research workspace and run history own their view state in separate modules; the backend API remains unchanged.
- Rebuilt shell, Dashboard masthead and Collections workspace; Collections now uses a tree, results table and contextual evidence inspector on wide screens, with the inspector below results at narrower widths. The inspector preserves date and Collection membership details; the wide table gives more room to keyword, metric and classification.
- Collections retains its resizable tree, accurate bulk selection, visible active filters and mobile filter sheet; Collection dialogs now scroll within the viewport on short screens. Inspector Tab order follows the page, Escape clears the context and restores focus.
- Reorganized Analysis evidence, Source Center, Run History timeline, Algorithm Library, Data Quality, Product Research and Settings. Runtime statuses and filters use existing APIs.
- Failed source loads can be retried; unavailable analytics and algorithm results are explicit. Known source metrics and algorithm explanations are localized; source delivery and chart scope appear only from reported metadata. Lucian #031 attribution and MIT notice are included.
- Research editing labels the edit state and locks its linked keyword, matching the existing API contract; the form explains that the saved link is preserved.
- Verified FastAPI static serving through `start.ps1 -Synthetic` and isolated asset packaging. Chrome sampled nine routes on desktop/mobile with both VI/EN and light/dark combinations; this is not a complete accessibility audit.
- Chrome exercised isolated Collection CRUD, actual merge, search, bulk selection, mobile filters, keyword detail navigation/errors, run pagination/detail, research CRUD/search/edit/link preservation, scheduler policy denial and synthetic Fetch. A five-row stored fixture chart matched its evidence table including a zero. The current Collection workbench measures 649px for results plus a 210px inspector at 1440px, and its mobile workflows show no page overflow.
- Final offline Chrome workspace suite passed at 320, 390, 768, 1024, 1440 and 1920px with both stylesheets loaded, checking all views, locale/theme, Source Center, Collection CRUD/bulk/merge, Research fixtures, errors and no Fetch.
- Focused post-change Chrome checks passed for real FastAPI with temporary SQLite, keyboard create/edit/delete and inspector focus behavior, plus responsive Collection and cross-route mobile workflows. The broad non-emulator backend suite passed at the integration checkpoint.
- UI-R1–UI-R12 acceptance audit is complete. Keyboard focus and responsive flows were checked; a full screen-reader/WCAG audit was not part of this pass. No blocking UI issue is known.
- Status: `READY_FOR_OWNER_REVIEW`; next step is owner review, not another rebuild wave.
- No live provider Fetch, paid calls, user database changes, push or deployment.
