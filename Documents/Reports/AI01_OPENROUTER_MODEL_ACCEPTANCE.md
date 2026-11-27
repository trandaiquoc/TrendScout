# AI01 — OpenRouter model acceptance
As of 2026-11-27; branch `integration/demo-final`.
Progress: [83%] AI01 BLOCKED; G1/G2/G3/G5/G6 PASS, G4 BLOCKED.
G1 PASS — Existing lifecycle and failure-state contracts remain covered by focused tests.
G2 PASS — AI01C root `.env` key: direct `GET /key` and adapter both HTTP 200; quota metadata verified.
G3 PASS — Full `GET /models` catalog: 441 entries, 11 exact `:free` text-to-text models; all 11 model prices are zero.
Endpoint scan — All 11 endpoint routes returned metadata; 9 active (`status=0`), 2 inactive (`status=-2`); active endpoint prices, including auxiliary fields, were zero.
Five models advertise `structured_outputs`; two also match active endpoint, exact provider tag, and `safety_retention=false` in the ZDR list.
Eligible exact IDs: `liquid/lfm-3-1.7b:free` (Liquid, tag `liquid`, 262144 context/8192 output); `particle-ai/particle-1:free` (Particle AI, tag `particle-ai/us`, 1048576/943718).
The adapter verified both exact model IDs and endpoint/provider tags. Provider slugs were checked against `/providers`; routing kept the full endpoint slug.
Rejected 9: 3 structured-output models lack a matching ZDR endpoint; 2 endpoints are inactive; 4 active models lack structured-output support.
Configuration candidates only, not written or enabled: `TRENDSCOUT_AI_COLLECTION_MODEL=liquid/lfm-3-1.7b:free`; `TRENDSCOUT_AI_RECOMMENDATION_MODEL=particle-ai/particle-1:free`.
G4 BLOCKED — Four physical synthetic requests (two per candidate) returned HTTP 404; adapter mapped them to `AI_PROVIDER_UNAVAILABLE`; no completion succeeded.
Classification and recommendation both deferred; fixture accuracy and recommendation relevance are unmeasurable. Failed POST wall latency was 74–328 ms; task end-to-end 1.56–2.83 s; successful token usage 0.
Each physical attempt received one PostgreSQL quota reservation in two isolated schemas (2 each; 4 total); all failed. Both schemas were dropped; `trendscout` was untouched.
OpenRouter key usage was 0 before and after. No generation IDs were returned, so per-generation actual cost could not be verified; no inference completion was billed according to key usage.
G5 PASS — PostgreSQL quota reservation was shared within each isolated AIService run; failure attempts consumed quota. G6 PASS — `test_ai_engine.py`: 28 passed, 2 subtests passed; Ruff, compileall, diff check, and cleanup passed.
AI01C's 2 skips were Linux-only owner-host tests, skipped on this Windows host; they are unrelated to root ENV acceptance and are not required AI01D gates.
No inference retry was made after the four 404s; no source provider, Production ENV, or inference budget setting was changed.
References (checked 2026-11-27): [models](https://openrouter.ai/docs/api/api-reference/models/get-models), [model endpoints](https://openrouter.ai/docs/api/api-reference/endpoints/list-endpoints), [provider routing](https://openrouter.ai/docs/guides/routing/provider-selection), [generation cost metadata](https://openrouter.ai/docs/api/api-reference/generations/get-generation), [ZDR endpoint list](https://openrouter.ai/api/v1/endpoints/zdr).
