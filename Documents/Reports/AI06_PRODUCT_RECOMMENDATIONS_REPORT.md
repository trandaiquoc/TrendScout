# AI06 — Product Recommendations

**Reviewed:** 2026-11-28 · **Branch:** `integration/demo-final` · **Recovery baseline:** `edfc5fb`

**Overall:** [100%] PASS — G1–G6 passed for the bounded mock/offline and isolated-PostgreSQL acceptance scope. This does not establish real-model recommendation quality or Production LIVE readiness.

## Acceptance gates

- **G1 — PASS.** `AIService.recommend()` accepts durable keyword IDs, enforces the existing maximum of 20 per request, and resolves only the selected IDs. It does not expand the request to the full dataset. Offline coverage confirms durable IDs flow into the recommendation context.
- **G2 — PASS (offline contract only).** The existing recommendation flow grounds product hypotheses in normalized keywords, active source evidence, current algorithm results, evidence scope, geography, and limitations. Its prompt asks for a reasoned Vietnam-to-US export research hypothesis and does not treat pageviews as sales, purchase intent, or market size. Product relevance quality was not measured against a live model.
- **G3 — PASS.** The existing validator requires product/category, related keyword IDs, evidence IDs tied to those keywords, rationale, consumer segment, seasonality, materials, manufacturing/export considerations, competition/IP/regulatory risks, uncertainty, and research steps. Unsupported numeric and commercial claims are rejected; valid qualitative research language remains permitted. Results are labeled `AI hypothesis` and include encoded Amazon US and Etsy research links.
- **G4 — PASS (in-memory contract).** Recommendation cache and job records now retain an input digest, evidence-based expiry, data profile and policy versions, source-policy digest, input/prompt/schema versions, and model/provider lineage. Cache reuse does not call inference. Legacy cache entries retain their result but report model/provider lineage as `NOT_RECORDED`; no historical metadata is fabricated. Regeneration bypasses cache through the existing job/quota path. Offline tests cover cache, regeneration, legacy lineage, and expiry inputs. PostgreSQL durability remains unverified under G5.
- **G5 — PASS.** Using the owner-updated root `.env`, the PostgreSQL AI-engine module ran against disposable schemas: **14 passed, 0 skipped**. Focused reruns after adding cache/recovery assertions: **2 passed, 0 skipped**. Coverage verified recommendation/result/job/cache persistence across service instances; a cache hit without a second provider call; regeneration making a second reservation; shared quota enforcement across service instances; cross-task cooldown enforcement; and reservation recovery after the recorded cooldown expired. The AI test schemas were dropped. Two `ts_test_db06_*` schemas were present but were outside this module's naming scope and were left untouched. No query or write targeted the `trendscout` production schema.
- **G6 — PASS.** `Backend/tests/test_ai_engine.py`: **58 passed, 6 subtests passed**; `Backend/tests/test_postgres_ai_engine.py`: **14 passed, 0 skipped**, plus the two focused modified-test reruns (**2 passed, 0 skipped**). Ruff, `compileall`, `git diff --check`, and the Markdown secret-pattern scan passed. The G5 acceptance update and report are delivered in a separate AI06 commit.

## Implementation changes

- Captured the safe resolved model identifier from the OpenRouter completion response alongside the existing generation identifier.
- Added allowlisted requested model, verified provider name/tag, generation ID, and resolved model metadata to completed recommendation results and their existing atomic job/cache state.
- Persisted the recommendation input digest, result expiry, data profile, transfer/source policy, input contract, prompt, and schema versions without changing the PostgreSQL schema.
- Exposed persisted recommendation lineage through the existing job lookup response.
- Kept recommendation generation on the existing `AIService.recommend()` path. No Fetch or Algorithm jobs are rerun, and no source permission or quota limit was changed.

## Verification and limitations

Recommendation generation used an offline fake provider. These checks establish the bounded request, validation, PostgreSQL persistence, cache, quota, and cooldown recovery contracts; they do not establish real-model recommendation relevance or Production LIVE readiness. AI01H remains separately blocked because the shared application attempt/cooldown ledger is absent. Existing migrations 0013 and 0014 were not applied.
