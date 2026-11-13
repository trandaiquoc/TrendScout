# G2 — Collections and Keyword Workbench

**Status:** Implemented and locally verified on Windows · **Repository baseline:** `f363edecf81452edc0b18c9757308a3c07bc5730` · **Provider calls:** None during this work

## Product decisions

- Collections is the single workspace for saved keywords. Legacy `#keywords` and `#results` links open it and retain their query.
- The main view prioritizes search, sorting, four useful totals, a Collection tree and a compact keyword table. Detailed filters and technical evidence are available after the user asks for them.
- “Chưa xếp vào Bộ sưu tập / Not in a Collection” describes eligible keywords without a Collection. It does not imply a taxonomy error. The internal label “AI cục bộ” was removed from the workbench; the interface describes automatic organization and user actions.
- A parent Collection displays the number of unique keywords in its descendants; a leaf displays its direct memberships. Missing counts display as unavailable. Request failures no longer become an empty result or a zero count, and delayed responses cannot replace newer data.
- Keyword details lead with source, observation date, observed metric, topic match, Collection placement and saved user decisions. Source payloads, analysis output and data limitations are collapsed. Wikimedia pageviews are identified as approximate observations, not search demand or purchase intent.
- Create, edit, merge and delete retain keyword records and user decisions where specified. Merge and delete show a confirmation first. Reclassification uses saved evidence and cached context; it does not start a provider fetch.

## Evidence and checks

- Read-only inspection of the existing local dataset returned 154 eligible keywords: 125 in Collections, 29 not in a Collection and 0 excluded. The topic-match totals were 125 identified, 11 needing review and 18 lacking context.
- Compared all 39 Collection counts with the corresponding server-filtered keyword totals: **39/39 matched**. Parent counts use unique keywords across descendants; keyword memberships may exist in more than one Collection.
- Injected a successful keyword response with no total into native Chrome: the page showed “Unavailable,” not “0” or “No stored keywords.” The real isolated API remained read-only in this check.
- Native Windows Chrome completed Collection create, edit, merge and delete against an isolated temporary database. Two synthetic keyword rows remained available after deleting the test Collection. The real local dataset was not changed.
- Native Windows Chrome screenshots and viewport checks are in [`windows-evidence/g2/`](../windows-evidence/g2/): 320, 390, 768, 1024, 1440 and 1920 px, keyword details, the mobile filter drawer, and English dark mode. No horizontal page overflow was found at those widths. Offline browser checks passed for 320/375 and 768/1024/1280/1440 px, including keyboard, locale/theme persistence and read-only API behavior.
- English and Vietnamese catalogs each contain 806 unique keys with exact key parity.
- Focused backend workbench/guard checks: **21 passed, 13 subtests passed**. Full backend suite: **749 passed, 768 subtests passed, 2 failed**. Both failures expect synthetic discovery status `INSUFFICIENT_EVIDENCE`, while the implementation returns `UNAVAILABLE`. Running those same tests against the unmodified baseline commit produced the same two failures; this G2 change does not touch synthetic discovery.
- JavaScript syntax and `git diff --check` passed. The latter reports only the repository's existing LF-to-CRLF working-copy warning for `Frontend/public/index.html`.

## Acceptance gates

| Gate | Result | Evidence |
|---|---|---|
| G2.1 Git synchronized; protect real data | PASS | Baseline matched `origin/main`; local dataset read only; no provider fetch or push |
| G2.2 One keyword workspace | PASS | Collections route and legacy redirects |
| G2.3 Persisted Collection hierarchy and counts | PASS | 39/39 filtered count comparison |
| G2.4 Explain placement and review states | PASS | Human-facing labels and separate “not in a Collection” state |
| G2.5 Searchable, sortable, responsive table | PASS | Native Chrome widths and offline workspace checks |
| G2.6 Server-side filters, pagination and counts | PASS | Store/API tests, 39 Collection comparisons and missing-total browser check |
| G2.7 Multi-select and bulk actions | PASS | API scope tests and browser workspace interaction checks |
| G2.8 Collection create/edit/merge/delete | PASS | Isolated native Chrome CRUD plus backend API tests |
| G2.9 Automatic assignment and manual decisions | PASS | Persistence, merge, and reclassification tests; cached-only reclassification |
| G2.10 Keyword details and evidence limits | PASS | Detail API/UI checks and native screenshot |
| G2.11 Vietnamese and English quality | PASS | Native Chrome locale check and 806-key parity |
| G2.12 Light and dark themes | PASS | Native Chrome theme persistence and screenshot evidence |
| G2.13 Desktop, tablet, mobile and keyboard use | PASS | Width matrix, no overflow, offline keyboard/mobile checks |
| G2.14 Persistence after reload | PASS | Backend persistence tests; isolated browser CRUD exercised live API writes |
| G2.15 No backend/provider regression | PASS | Full-suite results; the two failures reproduce unchanged on baseline |
| G2.16 Native Windows evidence and documentation | PASS | Windows Chrome screenshots and this report |

## UX reference

Reviewed the LucianLabs UXUI Prompt repository and its MIT license: [repository](https://github.com/Seokwoooo/lucian-uxui-prompt), [license](https://github.com/Seokwoooo/lucian-uxui-prompt/blob/main/LICENSE). The live site was unavailable during review. The workbench applies general interaction patterns such as progressive disclosure, contextual Collection actions, clear selected states and responsive navigation. No reference assets or code were copied.

## Scope and limitations

The workbench reflects the persisted local ledger and its source evidence. A zero count is valid when the server confirms no memberships for that Collection; missing or failed counts are shown as unavailable. Current pageview evidence is not a validated trend forecast. Synthetic discovery's two baseline test failures remain outside G2 scope and are documented above.
