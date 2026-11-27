# AI01 — OpenRouter model acceptance

As of 2026-11-27; branch `integration/demo-final`.

## Latest status — AI01F

Progress: [83%] AI01 BLOCKED; G1/G2/G3/G5/G6 PASS, G4 BLOCKED.

G1 PASS — AI lifecycle and sanitized routing diagnostics remain covered by focused tests.
G2 PASS — Root `.env` credential direct `/key` and adapter authentication were previously verified HTTP 200. No key value or hash is recorded here.
G3 PASS — Targeted metadata recheck of `particle-ai/particle-1:free` found the exact free model, active endpoint, zero model/endpoint price, JSON Schema support, and required request parameters. Candidate is listed under `Particle AI` / `particle-ai/us` and published as zero-retention. The public-demo profile is limited to synthetic data and this pinned route.
G4 BLOCKED — Five physical AI01F inference POSTs were used. Classification returned HTTP 429 (`AI_RATE_LIMITED`); recommendation output failed the application output validator (`AI_INVALID_OUTPUT`). The recommendation generation metadata was later available: resolved model `particle-ai/particle-1-20261027:free`, provider `Particle AI`, 1,369 prompt tokens, 1,710 completion tokens, and reported total cost `$0.00`. That verifies cost only for this generation; it does not make either task pass. No raw model output was retained.
G5 PASS — All prior AI01D/E and AI01F attempts were carried forward in the isolated PostgreSQL quota ledger; after five AI01F POSTs, counters were 11/11 against daily/minute limits 40/20. No quota reset occurred.
G6 PASS — Focused unit tests, required isolated PostgreSQL tests, Ruff, `compileall`, and `git diff --check` passed. The disposable owned synthetic schema was dropped; `trendscout` was not touched.

Model discovery — 477 catalog entries were scanned previously; 11 exact `:free` text-model IDs were found. Targeted rechecks covered the prior candidates `liquid/lfm-3-1.7b:free` and `particle-ai/particle-1:free`; Particle AI was selected for its published zero-retention policy. No paid route or source provider was called.

AI01F profile — `PUBLIC_DEMO` permits `data_collection=allow` only for synthetic records in a disposable, marked, isolated test schema. It omits request-level `zdr` because this profile is not approved for private or live data. The default `STRICT_PRIVATE` policy remains `data_collection=deny` and `zdr=true`. Provider, endpoint, no-fallback, JSON Schema, and zero-price constraints remain pinned.

Owner decision required — G4 needs successful valid classification and recommendation outputs. The final run had one of six additional AI01F POSTs unused; one request cannot satisfy both task gates, and retries are not justified after the observed rate limit and invalid output. Keep AI03–AI05 paused until Owner decides whether to authorize a new, explicitly bounded smoke window or leave G4 blocked.

Cleanup — All AI01F fixtures used synthetic keywords, evidence, and collections in disposable `ts_test_ai_demo_<hex>` PostgreSQL schemas. Owned schemas were dropped after each run; no production schema, source provider, OpenRouter paid route, or inference budget reset was used.

## Historical AI01E status

AI01E root cause — OpenRouter returned HTTP 404 with `failed_routing_step="Filter by Data Policy"` for both strict-policy candidates; this was not a `require_parameters` mismatch. Account/workspace policy could not be distinguished from endpoint filtering by public metadata.
AI01E fix — Added bounded, sanitized status/error/routing diagnostics and mapped policy-filter 404s to `AI_POLICY_DENIED` / `POLICY_DENIED`. No fallback, ZDR, JSON Schema, or zero-price protection was relaxed.

References checked 2026-11-27: [provider routing and data policy](https://openrouter.ai/docs/guides/routing/provider-selection), [chat completion request](https://openrouter.ai/docs/api/api-reference/chat/create-a-chat-completion), [error format](https://openrouter.ai/docs/api_reference/errors-and-debugging), [generation metadata](https://openrouter.ai/docs/api/api-reference/generations/get-generation).
