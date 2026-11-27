# AI01 — OpenRouter model acceptance

As of 2026-11-27; branch `integration/demo-final`.
Progress: [67%] AI01 BLOCKED; G1/G2/G5/G6 PASS, G3/G4 BLOCKED.

G1 PASS — AI lifecycle, failure-state, and sanitized-diagnostic contracts pass focused tests.
G2 PASS — Root `.env` key direct `/key` and adapter checks previously returned HTTP 200; current usage was 0 before and after AI01E.
G3 BLOCKED — The prior 441-model scan found 11 exact free text models and two candidates meeting zero-price, active, structured-output, and ZDR metadata checks.
Candidates: `liquid/lfm-3-1.7b:free` (tag `liquid`) and `particle-ai/particle-1:free` (tag `particle-ai/us`).
Targeted endpoint metadata recheck: both remain active, zero-priced, support `temperature`, `max_tokens`, and JSON Schema; `/endpoints/zdr` lists both with `safety_retention=false`.
The endpoint metadata for neither candidate exposes a data-collection policy; ZDR metadata alone does not establish compliance with `data_collection=deny`.
AI01D's four synthetic inference attempts returned HTTP 404; AI01E used the only two remaining attempts, one classification and one recommendation.
Both AI01E responses returned HTTP 404, `error.code=404`, `failed_routing_step="Filter by Data Policy"`, and a routing funnel beginning with one `Initial Endpoints` candidate.
No request ID was returned. No generation ID or completion was created; per-generation token usage and cost are unavailable.
OpenRouter key usage was `$0.00` before and after both attempts; no source provider or paid route was called.
Root cause — OpenRouter's Data Policy filter rejected the selected endpoints for the request's `data_collection="deny"`; this is not a `require_parameters` mismatch.
Official documentation says `deny` routes only to providers that do not collect user data and can also be enforced account-wide; public endpoint metadata and `/key` do not identify whether the exclusion is endpoint policy or account/workspace privacy settings.
AI01E fix — Preserve strict routing and expose only bounded HTTP/error/routing diagnostics; routing-policy 404s now map to `AI_POLICY_DENIED` / `POLICY_DENIED`, preventing deferred retries.
No fallback, ZDR, JSON Schema, or zero-price protection was relaxed. No model selection, root `.env`, or durable allowlist setting was changed.
Synthetic-only smoke used a process-local config and Wikimedia-labeled synthetic fixture; no root `.env` or durable allowlist changed. Its isolated PostgreSQL schema carried forward four prior reservations, recorded exactly two new reservations (six total), and was dropped; `trendscout` was untouched.
G4 BLOCKED — Neither task completed, so structured-output validity, actual provider/model, and zero per-generation cost cannot be verified.
G5 PASS — PostgreSQL quota guard accounted for the four previous attempts plus exactly two AI01E attempts; provider usage remained zero.
G6 PASS — `test_ai_engine.py`: 35 passed, 2 subtests passed; Ruff, targeted `compileall`, and `git diff --check` passed; isolated schema cleanup passed.
Owner action — Verify OpenRouter account/workspace Data Collection privacy settings and approved providers; select endpoints proven compatible with `deny`, ZDR, JSON Schema, and zero price. Do not relax these request protections to clear the filter.

References checked 2026-11-27: [provider routing and data policy](https://openrouter.ai/docs/guides/routing/provider-selection), [chat completion request](https://openrouter.ai/docs/api/api-reference/chat/create-a-chat-completion), [error format](https://openrouter.ai/docs/api_reference/errors-and-debugging), [generation metadata](https://openrouter.ai/docs/api/api-reference/generations/get-generation).
