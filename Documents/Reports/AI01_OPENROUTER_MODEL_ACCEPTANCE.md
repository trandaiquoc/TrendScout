# AI01 — OpenRouter model acceptance
As of 2026-11-27. Branch: `integration/demo-final`; no Production configuration changed.
Progress: [50%] AI01 BLOCKED; G1/G5/G6 PASS; G2/G3/G4 BLOCKED.
G1 PASS — Lifecycle statuses are verified by focused tests; no runtime lifecycle bug reproduced.
G2 BLOCKED — `.env` key was not printed; OpenRouter `GET /key` returned HTTP 401, so authentication and safe quota metadata are unavailable.
G3 BLOCKED — None of the six current Gemma/Nemotron `:free` variants meets every zero-price, active, JSON Schema, limits, and ZDR requirement.
G4 BLOCKED — No eligible authenticated model; guarded synthetic inference was correctly not attempted.
G5 PASS — PostgreSQL quota, jobs/idempotency, ALG07 inputs and fail-closed contracts passed against isolated test schemas.
G6 PASS — Required focused tests, scoped Ruff, compileall, diff check, schema cleanup, commit and remote verification passed; wider `app/ai` lint retains findings in untouched modules.
Lifecycle root cause: SQLite `CollectionStore` without AI correctly reports `AI_DISABLED`; the stale test expected Postgres incremental `MODEL_INFERENCE_DEFERRED`, which remains distinct and has its own passing no-provider regression.
The OpenRouter mock routed only paths ending `/models`, not `/models/{author}/{slug}/endpoints`; this caused false provider-unavailable failures.
After fixing the mock route, nonzero auxiliary pricing is `AI_PRICING_UNVERIFIED`/`PENDING` per contract; the old `AI_MODEL_UNAVAILABLE` expectation was stale.
Three other Smart Collections failures expected legacy embedding-era completion without an AI service; tests now assert `AI_DISABLED` and preserve scope/user state.
Fixes: corrected lifecycle assertions/mock routing; added model-vs-pricing status and storage-unavailable regression tests; no AI behavior change was needed.
Source manifest remains `SUCCESS` and the keyword is retained while collection processing independently reports `MODEL_UNAVAILABLE`; PostgreSQL lineage tests pass.
Provider quota: unavailable after 401; remaining quota was not inferred. Internal PostgreSQL limits remain 40/day and 20/minute.
Eligible-shape candidate: `nvidia/nemotron-3.5-super-vl:free` is active, zero-priced, structured-output capable (1M context/131072 output), but has no matching ZDR endpoint.
Other variants: `nvidia/nemotron-3.5-lightning:free`, `nvidia/nemotron-3-ultra-550b-a55b:free`, `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`, `google/gemma-4-26b-a4b-it:free`, and `google/gemma-4-31b-it:free`; none meets structured-output plus ZDR.
Selected exact model IDs: none. Classification and recommendation quality/latency/token usage remain unverified because no inference was eligible.
Physical inference requests: 0/6; no inference charge incurred. Only the requested OpenRouter metadata GETs were made; no source providers or OpenRouter completion endpoint were called.
Tests: 95 passed, 21 subtests passed, 0 skipped; PostgreSQL fixture tests used and dropped only their isolated `ts_test_*` schemas.
`compileall`, `git diff --check`, and fixture-owned PostgreSQL schema cleanup: PASS; no `trendscout` schema was written.
Code/test commit: `ed1567871b2f4359b408a30a74ef005fbf62a82d` (pushed and remote-verified before this report).
OpenRouter references (checked 2026-11-27): [key metadata](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key), [models](https://openrouter.ai/docs/api/api-reference/models/get-models), [model endpoints](https://openrouter.ai/docs/api/api-reference/endpoints/list-endpoints), [ZDR endpoints](https://openrouter.ai/docs/api/api-reference/endpoints/list-endpoints-zdr).
