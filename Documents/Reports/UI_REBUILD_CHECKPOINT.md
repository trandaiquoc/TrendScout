# Project Phoenix — checkpoint

Updated 2026-11-15 · local only · in progress

- Latest code commit: `c024a90`. Source library, research workspace and run history own their view state in separate modules; the backend API remains unchanged.
- Rebuilt shell, Dashboard masthead and Collections workspace; added resizable tree, accurate bulk selection, visible active filters, mobile filter sheet and contextual keyword details.
- Reorganized Analysis evidence, Source Center, Run History timeline, Algorithm Library, Data Quality, Product Research and Settings. Runtime statuses and filters use existing APIs.
- Failed source loads can be retried; unavailable analytics and algorithm results are explicit. Known source metrics and algorithm explanations are localized; source delivery and chart scope appear only from reported metadata. Lucian #031 attribution and MIT notice are included.
- Research editing labels the edit state and locks its linked keyword, matching the existing API contract; the form explains that the saved link is preserved.
- Verified FastAPI static serving through `start.ps1 -Synthetic` and isolated asset packaging. Chrome sampled nine routes on desktop/mobile with both VI/EN and light/dark combinations; this is not a complete accessibility audit.
- Chrome exercised isolated Collection CRUD, actual merge, search, bulk selection, mobile filters, keyword detail navigation/errors, run pagination/detail, research CRUD/search/edit/link preservation, scheduler policy denial and synthetic Fetch. A five-row stored fixture chart matched its evidence table including a zero. Mobile primary workflows currently show no page overflow.
- The broad non-emulator backend suite passed at the integration checkpoint; focused checks ran after subsequent frontend changes.
- Remaining: finish the UI-R1–UI-R12 acceptance audit, covering remaining detailed/error states, accessibility and responsive visual review; resolve any defects before claiming readiness.
- No live provider Fetch, paid calls, user database changes, push or deployment.
