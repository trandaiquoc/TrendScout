# AI05 — AI Reclassification

**Reviewed:** 2026-11-27 · **Branch:** `integration/demo-final` · **Baseline:** `38f9c56`

**Overall:** [100%] PASS after delivery verification. AI reclassification supports a bounded user-selected scope and a separate Preview/Apply flow. No Frontend, Production ENV, or production database changes were made; no live AI inference was used.

## Acceptance gates

- **G1 — PASS.** The API accepts selected keyword IDs, active Collection IDs, and/or unassigned keywords as a union. Collection selectors match direct memberships; descendants are not added implicitly. A request resolves to at most 200 keywords, with no expansion outside its selectors. Oversized and empty scopes return machine-readable errors.
- **G2 — PASS.** The input and classification-cache digests include current Collection names, descriptions, versions and `auto_match`, current memberships, user assignment/pin state, removal feedback, active evidence references and expiry, and the resolved scope. The database write transaction rechecks the complete submitted scope and saved keyword/Collection snapshots before changing memberships.
- **G3 — PASS.** Preview records no Collection, proposal, membership, or reconciliation business changes. It labels unchanged memberships, AI assignment changes/removals, new assignments, new Collection proposals, no-Collection outcomes, user-protected records, and other skipped records. Apply uses the exact saved preview payload without another model call and rejects expired, changed, or inconsistent previews.
- **G4 — PASS.** Apply skips user-assigned, pinned, and excluded records; revalidates user membership and removal feedback; and reconciles only eligible AI memberships. Rollback uses the existing targeted reconciliation API, preserves user-owned rows, and refuses a superseded run. PostgreSQL coverage exercises concurrent Apply, replay, a forced membership-write failure with retry, a scope change after Preview, and rollback.
- **G5 — PASS.** The existing bounded classification batches, shared AI quota, cache, durable job, retry, and resume mechanisms are reused. Job status exposes processed/total progress, preview/apply summaries, retry/deferred state, sanitized error codes, and whether Apply or rollback is available. Apply makes no inference request. Reclassification does not rerun Fetch or Algorithms.
- **G6 — PASS.** Focused Python and isolated PostgreSQL checks pass; Ruff, compile checks, and diff validation pass. AI05 is committed and pushed separately; the final delivery note records the verified branch and remote SHA.

## Verification

- `tests/test_ai_engine.py`: **57 passed**, **6 subtests passed**.
- `tests/test_postgres_ai_engine.py` and `tests/test_postgres_collection_memberships.py`: **15 passed** in the full focused run. After the final evidence-snapshot changes, the three key PostgreSQL cases for 21-evidence context, scoped Preview/Apply/rollback, and stale scope rejection were rerun: **3 passed**.
- Ruff on changed Python files: **PASS**. `compileall`: **PASS**. `git diff --check`: **PASS**.
- Providers were mocked; AI03–AI05 used **0 live inference requests**. PostgreSQL checks used disposable `ts_test_*` schemas and dropped them during cleanup. The root `.env` was read only to provide the test connection; Production ENV and database schemas were not modified.

## Implementation and limits

- The API is exposed through `POST /api/v1/ai/classifications/reclassify` with `preview=true`, `POST /api/v1/ai/classifications/previews/{preview_job_id}/apply`, the existing job status/resume endpoints, and the existing reconciliation rollback endpoint.
- The maximum resolved scope is 200 keywords per reclassification job. A larger Collection scope is rejected with a bounded-scope error; callers can select a narrower set and submit another request.
- `Documents/Integration/AI_CONTRACT.md` is updated to version 1.7-draft. No schema migration was required. No UI was changed.
