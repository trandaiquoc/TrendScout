# AI01 — OpenRouter model acceptance

As of 2026-11-27; branch `integration/demo-final`.

## Latest status — AI01G

Progress: [100%] AI01 SYNTHETIC ACCEPTANCE PASS; G1–G6 PASS. STRICT_PRIVATE live-source and production acceptance remain unverified.

G1 PASS — Validator diagnostics identify stage, reason code, field path, expected constraint, and reference category without retaining response text. Positive research language and negative unsupported-claim fixtures pass.
G2 PASS — Root `.env` direct `/key` and adapter authentication had been verified HTTP 200; targeted model verification succeeded. No key value/hash is recorded.
G3 PASS — `particle-ai/particle-1:free` verified active, zero-priced, structured-output capable, with required request parameters and pinned `Particle AI` / `particle-ai/us` route for PUBLIC_DEMO synthetic data.
G4 PASS — Classification and recommendation each returned HTTP 200, passed application validation, used the expected synthetic keyword/evidence lineage, and resolved to Particle AI at `$0.00`.
Classification generation `gen-1795772868-eBefnTJLLTOlPvAxYxo2`: resolved model `particle-ai/particle-1-20261027:free`, 833 prompt tokens, 397 completion tokens, 2.956s generation time, total cost `$0.00`; expected “Synthetic camping cooking equipment” Collection assignment matched.
Recommendation generation `gen-1795772711-fRQSPznkR32QPHbN2wTu`: resolved model `particle-ai/particle-1-20261027:free`, 1,375 prompt tokens, 1,952 completion tokens, 9.547s generation time, total cost `$0.00`; keyword and evidence IDs matched.
Three AI01G physical POSTs were used: one HTTP 400 before generation and two valid HTTP 200 completions. The 400 root cause was a generated one-keyword classification schema with proposal `keyword_ids.minItems=2` and `maxItems=1`; the schema builder now omits proposal-specific bounds when proposals are impossible. No raw provider response was retained.
The recommendation validator's blanket term rejection was also narrowed: research/verification wording is accepted while unsupported numerical and factual commercial claims remain rejected. HTTP 429 diagnostics now retain sanitized `limit_source`, `Retry-After`, and normalized `X-RateLimit-Reset` information; no 429 occurred in AI01G.
G5 PASS — The new isolated PostgreSQL ledger carried forward all 11 preceding attempts; daily usage was 11 before AI01G and 14 after its three posts, below the 40/day internal limit. Prior attempts were older than the rolling minute window. No quota reset occurred.
G6 PASS — `test_ai_engine.py` + `test_ai_offline.py`: 63 passed, 18 subtests passed; isolated PostgreSQL tests: 4 passed, no skips; Ruff, `compileall`, and `git diff --check` passed. The owned synthetic schema was dropped; `trendscout` was untouched.
Only synthetic fixtures were sent. No source provider, paid route, or OpenRouter inference reset was used. AI02 batching/cache/resume tests remain passing.

## Historical status — AI01F

Progress: [83%] AI01 BLOCKED; G1/G2/G3/G5/G6 PASS, G4 BLOCKED.

G1 PASS — AI lifecycle and sanitized routing diagnostics remain covered by focused tests.
G2 PASS — Root `.env` credential direct `/key` and adapter authentication were previously verified HTTP 200. No key value or hash is recorded here.
G3 PASS — Targeted metadata recheck of `particle-ai/particle-1:free` found the exact free model, active endpoint, zero model/endpoint price, JSON Schema support, and required request parameters. Candidate is listed under `Particle AI` / `particle-ai/us` and published as zero-retention. The public-demo profile is limited to synthetic data and this pinned route.
G4 BLOCKED — Five physical AI01F inference POSTs were used. Classification returned HTTP 429 (`AI_RATE_LIMITED`; its limit source/reset were not recorded then and remain historically unknown); recommendation output failed the application output validator (`AI_INVALID_OUTPUT`). The recommendation generation metadata was later available: resolved model `particle-ai/particle-1-20261027:free`, provider `Particle AI`, 1,369 prompt tokens, 1,710 completion tokens, and reported total cost `$0.00`. That verifies cost only for this generation; it does not make either task pass. No raw model output was retained.
G5 PASS — All prior AI01D/E and AI01F attempts were carried forward in the isolated PostgreSQL quota ledger; after five AI01F POSTs, counters were 11/11 against daily/minute limits 40/20. No quota reset occurred.
G6 PASS — Focused unit tests, required isolated PostgreSQL tests, Ruff, `compileall`, and `git diff --check` passed. The disposable owned synthetic schema was dropped; `trendscout` was not touched.

Model discovery — 477 catalog entries were scanned previously; 11 exact `:free` text-model IDs were found. Targeted rechecks covered the prior candidates `liquid/lfm-3-1.7b:free` and `particle-ai/particle-1:free`; Particle AI was selected for its published zero-retention policy. No paid route or source provider was called.

AI01F profile — `PUBLIC_DEMO` permits `data_collection=allow` only for synthetic records in a disposable, marked, isolated test schema. It omits request-level `zdr` because this profile is not approved for private or live data. The default `STRICT_PRIVATE` policy remains `data_collection=deny` and `zdr=true`. Provider, endpoint, no-fallback, JSON Schema, and zero-price constraints remain pinned.

AI01F ended with G4 blocked and one of its six authorized requests unused. The later AI01G owner decision authorized a separate bounded window of up to six additional requests; AI01G used three and passed the synthetic acceptance gates.

Cleanup — All AI01F fixtures used synthetic keywords, evidence, and collections in disposable `ts_test_ai_demo_<hex>` PostgreSQL schemas. Owned schemas were dropped after each run; no production schema, source provider, OpenRouter paid route, or inference budget reset was used.

## Historical AI01E status

AI01E root cause — OpenRouter returned HTTP 404 with `failed_routing_step="Filter by Data Policy"` for both strict-policy candidates; this was not a `require_parameters` mismatch. Account/workspace policy could not be distinguished from endpoint filtering by public metadata.
AI01E fix — Added bounded, sanitized status/error/routing diagnostics and mapped policy-filter 404s to `AI_POLICY_DENIED` / `POLICY_DENIED`. No fallback, ZDR, JSON Schema, or zero-price protection was relaxed.

References checked 2026-11-27: [provider routing and data policy](https://openrouter.ai/docs/guides/routing/provider-selection), [chat completion request](https://openrouter.ai/docs/api/api-reference/chat/create-a-chat-completion), [error format](https://openrouter.ai/docs/api_reference/errors-and-debugging), [rate-limit diagnostics](https://openrouter.ai/blog/llm-api-rate-limits/), [generation metadata](https://openrouter.ai/docs/api/api-reference/generations/get-generation).
