# AI08 — Consolidated Acceptance

**Reviewed:** 2026-11-28 · **Branch:** `integration/demo-final` · **Recovery baseline:** `edfc5fb` · **Implementation parent:** `b453979`

**AI08 overall:** [100%] PASS — G1–G6 passed for the bounded synthetic, mocked-provider, and isolated-PostgreSQL acceptance scope. This is not evidence of live-model quality or Production readiness. SEC01 rotation confirmation and AI01H live G4 remain separate blockers.

## Acceptance gates

- **G1 — PASS.** One end-to-end PostgreSQL test runs a synthetic Wikimedia Fetch through durable keyword/evidence persistence and algorithm results, automatic classification and proposal creation, user-edited proposal acceptance, a second proposal rejection, targeted Preview/Apply, and product recommendation. Apply reuses the saved preview without another classification call. All operations use one disposable `ts_test_ai_demo_*` schema and a deterministic local provider double.
- **G2 — PASS.** Synthetic evaluator and golden-pipeline contracts passed: `test_algorithm_evaluation.py` and `test_golden_contract.py` reported **18 passed, 15 subtests passed**. Recommendation/classification schema and validator, qualitative research language, unsupported-claim rejection, and evidence-link cases were checked. These fixtures test contract behavior; they do not establish model or market accuracy.
- **G3 — PASS (shared-budget scope).** The consolidated journey used **4 of 40** daily reservations and completed its recommendation after classification and preview work. Apply made no inference request. AI02's unchanged benchmark and AI06's previously passing PostgreSQL cache, quota, cooldown, regeneration, and recovery evidence were reused rather than rerun. The benchmark reports 38 requests for 1,500 cold groups and zero requests for the full-cache case. The quota remains shared across tasks and has no reserved recommendation allotment; when the shared daily or rolling-minute cap is exhausted, recommendation work can defer. AI08 did not change that accepted quota policy.
- **G4 — PASS.** The journey binds `PUBLIC_DEMO` to an attested synthetic-only schema; every fixture keyword is `SYNTHETIC`, and the provider is a local deterministic double. Selected source-policy, profile-isolation, public-data projection, validator, and cache-safety checks reported **13 passed, 4 subtests passed**. No live/provider request, paid inference, quota reset, Production write, or deployment occurred. No raw prompt or credential was written to the report.
- **G5 — PASS.** Fresh install and rollback plus upgrade tests reported **4 passed**; the consolidated PostgreSQL journey also passed. The upgrade starts from migration 0012, applies only 0013/0014, verifies the complete version/name/checksum ledger and idempotency, preserves a pre-existing Fetch row and proposal, checks the new defaults/null fields and expiry index, and verifies `ON DELETE SET NULL`. A deliberately failing later migration leaves no partial objects. Test schemas are dropped; `trendscout` is not modified.
- **G6 — PASS.** The report and acceptance tests are delivered in a separate commit on `integration/demo-final`; the remote SHA is verified in the task handoff. Changed Python files passed Ruff and `compileall`; `git diff --check` passed.

## Verification

- `tests/test_ai08_consolidated_acceptance.py` and `tests/test_postgres_migrations.py`: **5 passed, 0 skipped**. PostgreSQL tests used the root `.env` unpooled URL without printing it and operated only on randomly named disposable schemas.
- `tests/test_algorithm_evaluation.py` and `tests/test_golden_contract.py`: **18 passed, 15 subtests passed**.
- Selected `tests/test_ai_engine.py` and `tests/test_ai_offline.py` validator, source-policy, profile, cache-safety, and recommendation cases: **13 passed, 4 subtests passed**.
- The previously accepted AI02 benchmark and AI06 PostgreSQL quota/cache/recovery runs were not repeated because AI08 made no runtime or algorithm changes.
- No AI08 live inference was attempted. No provider result or fixture is presented as evidence of actual trend or product quality.

## Limits

- All AI08 inference behavior is validated with a local mock and synthetic records. Real provider reachability, output quality, and Production readiness remain unverified.
- The shared AI request budget has no per-task reservation. The accepted 4-request flow completed, but a fully exhausted shared budget can defer both classification and recommendation work.
- PostgreSQL migrations 0013 and 0014 were applied only inside disposable test schemas; this does not authorize or imply a Production migration.
