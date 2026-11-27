# AI06 — Product Recommendations

**Reviewed:** 2026-11-27 · **Branch:** `integration/demo-final` · **Requested baseline:** `b1bb1ec`

**Overall:** [83%] BLOCKED — G1–G4 and G6 PASS for the offline implementation contract; G5 is BLOCKED because the repository-root PostgreSQL credential failed authentication. PostgreSQL persistence, shared quota, and recovery were not verified against an isolated database. AI07 and AI08 were not started because they depend on this implementation gate.

## Acceptance gates

- **G1 — PASS.** `AIService.recommend()` accepts durable keyword IDs, enforces the existing maximum of 20 per request, and resolves only the selected IDs. It does not expand the request to the full dataset. Offline coverage confirms durable IDs flow into the recommendation context.
- **G2 — PASS (offline contract only).** The existing recommendation flow grounds product hypotheses in normalized keywords, active source evidence, current algorithm results, evidence scope, geography, and limitations. Its prompt asks for a reasoned Vietnam-to-US export research hypothesis and does not treat pageviews as sales, purchase intent, or market size. Product relevance quality was not measured against a live model.
- **G3 — PASS.** The existing validator requires product/category, related keyword IDs, evidence IDs tied to those keywords, rationale, consumer segment, seasonality, materials, manufacturing/export considerations, competition/IP/regulatory risks, uncertainty, and research steps. Unsupported numeric and commercial claims are rejected; valid qualitative research language remains permitted. Results are labeled `AI hypothesis` and include encoded Amazon US and Etsy research links.
- **G4 — PASS (in-memory contract).** Recommendation cache and job records now retain an input digest, evidence-based expiry, data profile and policy versions, source-policy digest, input/prompt/schema versions, and model/provider lineage. Cache reuse does not call inference. Legacy cache entries retain their result but report model/provider lineage as `NOT_RECORDED`; no historical metadata is fabricated. Regeneration bypasses cache through the existing job/quota path. Offline tests cover cache, regeneration, legacy lineage, and expiry inputs. PostgreSQL durability remains unverified under G5.
- **G5 — BLOCKED.** PostgreSQL is the quota and durable state authority. The root database credential failed authentication in the SEC01 connection check, and no alternate credential was used. PostgreSQL AI-engine tests were **not run**; they were not skipped. Shared PostgreSQL quota, cooldown, retry/recovery, cache persistence, and isolated-schema cleanup could not be verified in this run. No inference POST, quota reset, schema migration, or production database write was performed.
- **G6 — PASS (offline delivery).** `Backend/tests/test_ai_engine.py`: **58 passed, 6 subtests passed**. Ruff on changed Python files, `compileall`, `git diff --check`, and the Markdown secret-pattern scan passed. The report and code are included in the separate AI06 delivery commit; the PostgreSQL test limitation is recorded above.

## Implementation changes

- Captured the safe resolved model identifier from the OpenRouter completion response alongside the existing generation identifier.
- Added allowlisted requested model, verified provider name/tag, generation ID, and resolved model metadata to completed recommendation results and their existing atomic job/cache state.
- Persisted the recommendation input digest, result expiry, data profile, transfer/source policy, input contract, prompt, and schema versions without changing the PostgreSQL schema.
- Exposed persisted recommendation lineage through the existing job lookup response.
- Kept recommendation generation on the existing `AIService.recommend()` path. No Fetch or Algorithm jobs are rerun, and no source permission or quota limit was changed.

## Verification and limitations

All implementation tests used an offline fake provider. Their success establishes the local request/validation/state contract; it does not establish PostgreSQL connectivity, provider behavior, recommendation relevance quality, or Production LIVE readiness. AI01H remains separately blocked on its real-source LIVE gate. Existing migrations 0013 and 0014 were not applied.
