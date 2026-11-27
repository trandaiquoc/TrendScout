# AI03 — Automatic Collection Matching

**Reviewed:** 2026-11-27 · **Branch:** `integration/demo-final` · **Baseline:** `4f405bb`

**Overall:** [100%] PASS after delivery verification. Automatic Fetch classification is wired for Local and Vercel PostgreSQL compositions when the server-side AI feature switch is explicitly enabled. The checked-in default remains disabled; no Production ENV or deployment was changed.

## Acceptance gates

- **G1 — PASS.** `PUBLIC_DATA` and `STRICT_PRIVATE` compositions attach the existing `AIService` to `CollectionService` only when `TRENDSCOUT_AI_ENABLED=true`. Fetch reaches terminal source status before AI inference. Local continues Collection processing after returning the source response. Vercel's existing post-terminal retained-checkpoint recovery request starts Collection processing; no Vercel fire-and-forget task was added. Before inference, the complete batch list and IDs are persisted in `fetch_jobs.ai_jobs`. `GET /api/v1/collections/jobs/{fetch_id}` exposes per-batch status, and `POST /api/v1/collections/jobs/{fetch_id}/resume` resumes stored AI work only.
- **G2 — PASS.** Classification loads the current active Collection IDs, names, descriptions, versions, and `auto_match` flags through the existing AI context builder. Existing canonical grouping, cache reuse, durable-ID fan-out, and the 0.82 assignment threshold remain in force. Only active `auto_match=true` Collections can receive an automatic assignment. Automatic Fetch ignores model-produced new-Collection proposals; keywords below threshold or without a match remain unassigned for the separate proposal workflow.
- **G3 — PASS.** Existing classification filters exclude user-assigned, pinned, removed, excluded, expired-evidence, and policy-restricted keywords. PostgreSQL revalidates keyword state, active evidence, Collection state/version, and user overrides in the write transaction. The new PostgreSQL race test inserts a user pin after inference but before persistence and verifies the user's membership survives with no AI assignment.
- **G4 — PASS.** A provider error leaves retained keywords, evidence, existing memberships, and algorithm results intact. Every batch ID and keyword-ID list is recorded before the first inference. Explicit resume reuses completed AI jobs, resumes deferred or expired-running jobs, and does not rerun Fetch or algorithms. `INVALID_OUTPUT` jobs can be resumed through the existing `AIService`; stale user input returns a conflict instead of creating an assignment.
- **G5 — PASS.** PostgreSQL `fetch_jobs.ai_jobs` is added by versioned migration `0013_fetch_job_ai_work.sql`; Local SQLite has an additive compatibility column and persists distinct source-ingestion, materialization, algorithm, and AI statuses. Isolated PostgreSQL tests cover durable batch linkage/resume, algorithm non-replay, Local/Vercel feature-switch composition, existing AI persistence/quota behavior, and a concurrent user pin. All created `ts_test_*` schemas were dropped. The `trendscout` business schema and production quota were not modified.
- **G6 — PASS.** Focused tests, changed-file Ruff, `compileall`, and `git diff --check` passed. The AI03 change was committed and pushed separately to `integration/demo-final`; the remote branch SHA was checked against the local commit at delivery.

## Verification

- `test_ai_engine.py` and `test_smart_collections.py`: **96 passed**, **13 subtests passed**.
- Focused Vercel Fetch/Collection recovery tests: **3 passed**.
- PostgreSQL AI engine tests: **5 passed, no skips**; PostgreSQL Collection concurrency and durable resume tests: **2 passed, no skips**; PostgreSQL concurrent user-pin revalidation: **1 passed, no skips**.
- Shared Local/Vercel PostgreSQL composition profile test: **1 passed**.
- Ruff on changed Python files: **PASS**. `compileall`: **PASS**. `git diff --check`: **PASS**.
- All inference providers in AI03 tests were mocks. AI03 consumed **0 live inference requests**.

## Implementation and limitations

- The AI engine, source-policy registry, input contract, cached classification, and PostgreSQL write protections are reused. The AI model remains configuration-driven; automatic matching uses the configured classification model and the existing shared quota.
- The Fetch job stores deterministic AI job IDs with the exact durable keyword IDs for each batch. AI job status remains in the shared PostgreSQL AI state store. The new resume API does not replay source adapters, data materialization, or algorithms.
- Automatic matching remains off until an operator enables the server-side feature flag. Public-data source allowlists, transfer approval, Collection text classification, provider zero-price checks, and public/private input guards continue to fail closed. The runtime migration must be applied to each target PostgreSQL schema by the existing operator migration process before code using migration 0013 can run; no production schema was migrated here.
- PublicData live transfer remains limited to `wikimedia_pageviews_top_per_country`; the Google Trends/Search, Bing, YouTube, and Wikimedia entity-text restrictions from the AI01H source-policy matrix are unchanged. No LIVE inference was performed, and no Frontend files were changed.
