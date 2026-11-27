# AI07 — UI Integration Acceptance

**Reviewed:** 2026-11-28 · **Branch:** `integration/demo-final` · **Requested recovery baseline:** `edfc5fb` · **Implementation parent:** `54faf229`

**Overall:** G1–G6 PASS. Browser checks used isolated API mocks. They verify the UI and repository contracts only; they do not establish Production readiness or successful provider inference.

## Acceptance gates

- **G1 — PASS.** Fetch stays independent from dataset materialization, algorithms, and AI classification. The saved collection-job UI displays source status, all three processing stages, processed/eligible, pending, deferred, policy-restricted, unavailable, rate-limited, and retry time separately. In the browser mock, one Fetch produced one submit, one status poll, and one job read. Frontend recovery tests passed for reattaching to the retained job, reusing its source scope and idempotency key, and bounded continuation. No retry or inference loop was triggered by opening a page.
- **G2 — PASS.** Automatic Fetch classification keeps valid model proposals in the existing pending-proposal workflow instead of discarding them. This reuses the original result; it does not issue a second inference, auto-accept a proposal, or create a Collection. Review loads every supporting keyword, its source and observation time, evidence references, reason, and uncertainty. The mocked browser flow edited and accepted a proposal, showed six supporting keyword records, exposed the targeted reclassification action, and rejected a second proposal. The focused isolated-PostgreSQL regression verified acceptance when the proposal came from automatic Fetch.
- **G3 — PASS.** Preview supports explicit selected keywords, one selected Collection, and unassigned keywords. Requests over 200 selected keywords are rejected without truncation or a request. Preview renders the unchanged, new, changed, removed, proposed, unassigned, user-protected, and skipped counts. Apply calls the saved-preview endpoint and performs no additional classification request. Deferred work exposes a manual resume action; stale-input, quota, and rate-limit responses are mapped to safe UI messages. Rollback was exercised against the mocked reconciliation endpoint.
- **G4 — PASS.** Product hypotheses are reachable from Keywords, Trends, and Collections. Keywords and Collections carry selected saved keyword IDs into Products; Trends opens the saved-keyword search. Opening Products or Settings sends no recommendation request. Search asks for at most 20 saved keywords; a 21-item mock proved the 21st selection is refused, the first 20 remain selected, and no item is silently omitted. The explicit request exposes the regenerate control, cache/lineage, evidence scope, related keywords, rationale, market framing, uncertainty, materials, manufacturing/export considerations, risks, research steps, and allowlisted marketplace research links. Resume, cache reuse, deliberate regeneration, and a mocked provider 429 with existing results retained were verified.
- **G5 — PASS.** English and Vietnamese, light and dark themes, and keyboard access were checked. The first Tab reaches the skip-navigation link; visible controls had accessible names. At 375px, Products, Keywords, and Trends had zero document-level horizontal overflow; the keyword table stayed within its scroll container. Error states retained existing recommendation content and did not expose raw provider error codes.
- **G6 — PASS.** The report, focused backend regressions, frontend suite, syntax checks, and mocked browser acceptance are complete. Delivery is a separate AI07 commit/push on `integration/demo-final`; the remote SHA is verified in the task handoff.

## Changes

- Preserved valid automatic Fetch proposals in the pending proposal workflow and exposed safe source-policy and provider retry metadata in AI capabilities.
- Added a separate Fetch/AI processing summary and expanded the Collection proposal, saved-preview reclassification, and product-hypothesis UI.
- Connected Keywords, Trends, and Collections to Products with explicit saved keyword IDs and scope limits.
- Opened containing disclosure panels when a targeted proposal starts a preview, so the result and its actions remain visible.
- Updated `Documents/Integration/AI_CONTRACT.md` and `Frontend/README.md` for the implemented behavior.

## Verification

- `Backend/tests/test_ai_engine.py -k automatic_fetch`: **1 passed**.
- `Backend/tests/test_ai_engine.py -k capabilities_exposes_only_effective_source_policy_and_retry_time`: **1 passed**.
- Isolated PostgreSQL `test_postgres_ai_proposal_acceptance_and_targeted_reclassification`: **1 passed**; disposable test schema only.
- Ruff and `compileall` on changed Backend files: **PASS**.
- `Frontend`: `node --test tests/*.test.mjs`: **71 passed, 0 failed**.
- `node --check` on changed frontend JavaScript modules and locale files: **PASS**.
- Playwright on the local static UI with every `/api/` request intercepted by fixtures: **PASS** for Fetch/job stages, proposal review/accept/reject, all reclassification scopes and error paths, 201-keyword rejection, product selection at the 20-keyword limit, cache/resume/regenerate, VI/EN, theme, keyboard, and responsive checks. The page recorded zero JavaScript runtime errors and no external resource requests. Mocked 409/429 responses were used to exercise error handling; no provider inference request was sent.
- `git diff --check`: **PASS**.

## Limitations

The Fetch, AI capability, quota, cooldown, classification, and recommendation responses used in browser checks were fixtures. These checks do not attest provider capacity, application quota, cooldown, Production behavior, or live inference quality. No Production schema, deployment, paid service, quota reset, or `main` branch was used.

The local static test server does not supply SPA history fallback for a direct reload of a nested route. Browser checks navigated through the application's links or reset to `/`; this is a test-server limitation and was not treated as a successful backend response.
