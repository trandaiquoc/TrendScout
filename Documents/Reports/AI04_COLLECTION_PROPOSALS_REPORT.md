# AI04 — New Collection Proposals

**Reviewed:** 2026-11-27 · **Branch:** `integration/demo-final` · **Baseline:** `6026b8d`

**Overall:** [100%] PASS after delivery verification. AI-generated Collections remain pending until a user accepts a proposal. Proposal review and acceptance use the existing Collection persistence boundary; no Frontend or Production ENV changes were made.

## Acceptance gates

- **G1 — PASS.** AI proposals include the complete candidate and selected keyword IDs, name, description, reason, heuristic score, uncertainty, source and active evidence references, model/job lineage, and expiry bounded by the earliest supporting evidence. Every selected keyword must be supported by at least one referenced active evidence record.
- **G2 — PASS.** Proposal creation checks normalized names and existing/pending labels, exact pending keyword sets, rejected selected/candidate keyword sets, and exact existing Collection membership sets in the serialized PostgreSQL transaction. A new or edited Collection context appearing during inference invalidates persistence. Rejecting stores both name and keyword-content suppression.
- **G3 — PASS.** `GET /api/v1/collections/suggestions/{proposal_id}` returns all proposed keyword IDs and a safe row for each keyword; the response is not capped at the workbench's five-keyword preview. `PATCH /api/v1/ai/proposals/{proposal_id}` edits name, description, and selected IDs, and selection can be changed within the original candidate set. The accepted response includes `reclassification_keyword_ids` for targeted follow-up.
- **G4 — PASS.** PostgreSQL acceptance revalidates current source policy, keyword eligibility, active evidence, expiry, and user-owned membership/pin state before atomically creating the Collection, selected memberships, and accepted status. Repeated or concurrent acceptance returns the same Collection ID. Rejection records name and content suppression. A forced membership insert failure verifies the entire accept transaction rolls back and leaves the proposal pending.
- **G5 — PASS.** Migration `0014_collection_proposal_lifecycle.sql` adds proposal evidence expiry and stable accepted Collection ID. SQLite adds compatible edit/expiry/accepted-ID columns without replacing existing state. Isolated PostgreSQL coverage includes active evidence expiry, proposal expiry, concurrent user edits, new Collection context during inference, pending/rejected content deduplication, concurrent acceptance, and transaction rollback. Accepting a Collection changes classification cache context; later classification is targeted and does not replay a dataset.
- **G6 — PASS.** Focused AI/Collection/API tests, isolated PostgreSQL proposal and membership tests, changed-file Ruff, `compileall`, and `git diff --check` passed. AI04 is committed and pushed separately to `integration/demo-final`; the remote SHA was verified at delivery.

## Verification

- `test_ai_engine.py`, `test_smart_collections.py`, and `test_collection_proposal_api.py`: **97 passed**, **13 subtests passed**. One upstream Starlette/httpx deprecation warning was reported.
- `test_postgres_ai_engine.py -k proposal`: **7 passed, 5 deselected, no skips**.
- `test_postgres_collection_memberships.py`: **1 passed, no skips**.
- Ruff on changed Python files: **PASS**. `compileall`: **PASS**. `git diff --check`: **PASS**.
- AI04 used mocked inference providers and consumed **0 live inference requests**. PostgreSQL tests used disposable `ts_test_*` schemas that were dropped during cleanup.

## Implementation and limitations

- PostgreSQL is the durable proposal workflow for Local and Vercel AI. The SQLite store now supports compatible proposal editing and repeat-accept behavior for local state.
- Proposal edits only select from their original candidate IDs. On edit, the store rebuilds evidence/source lineage from currently active allowed evidence and refreshes expiry. Acceptance refuses expired evidence, policy changes, or changed user state instead of silently accepting stale data.
- PostgreSQL migration `0014_collection_proposal_lifecycle.sql` must be applied to each target schema by the existing operator migration process before deploying code that reads the new fields. No Production schema was migrated here.
- Public-data source restrictions, server-side AI feature gating, and PUBLIC_DEMO isolation are unchanged. No provider or model IDs were hardcoded in Collection business logic. AI07 owns Frontend integration.
