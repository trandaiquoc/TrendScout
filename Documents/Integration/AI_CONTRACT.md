# TrendScout AI Integration Contract

**Version:** 1.4-draft
**Status:** AI input contract and profile isolation implemented on `integration/demo-final`; AI01G synthetic classification and recommendation acceptance passed on 2026-11-27. STRICT_PRIVATE LIVE, real-source processing, and production acceptance remain unverified.
**Owner:** AI goal
**Updated:** 2026-11-27

The AI boundary is implemented in this branch. The Database goal owns PostgreSQL schema and migrations. No Documents/Integration/DB_CONTRACT.md existed at the baseline, so Database review is still needed before treating the persistence mapping as stable. The AI goal does not apply or propose a migration.

## 1. Responsibilities and boundaries

- Source adapters, ingestion, normalization, market-signal calculations, and statistical algorithms remain outside the AI service.
- AI classifies eligible normalized keywords against existing Collections, proposes new Collections for user review, supports user-triggered reclassification, and creates qualitative Vietnam-to-US export product hypotheses.
- AI does not fetch source data, calculate search volume, create evidence, create a Collection without user acceptance, or claim a product will sell or be profitable.
- Provider input is built server-side from exact canonical keyword groups and retains all durable source keyword IDs. Groups do not cross data modes or resolved entity identities; unresolved records group only with other unresolved records, and conflicting known entities remain separate. Categories, per-source geography/freshness, active evidence references, source-specific algorithm results, and Collection/user constraints are bounded and deterministically ordered. Request bodies accept IDs and bounded research constraints only. Raw source payloads, credentials, and YouTube data are excluded.
- A source must appear in both `TRENDSCOUT_AI_ALLOWED_SOURCES` and the independent `TRENDSCOUT_AI_TRANSFER_APPROVED_SOURCES`. Both default to empty. The second setting is explicit approval for transfer/processing by the configured AI provider; extraction and derived-metric grants do not imply that permission. YouTube is denied even if configured.
- Expired or expiry-unknown evidence is omitted. AI cache/result lifetime is bounded by the earliest active evidence expiry; Fetch replay does not extend source expiry.
- Source geography and observation time remain evidence metadata. English text or a US-targeted product hypothesis does not establish US authorship, readership, or demand.
- An AI failure does not discard persisted keywords or algorithm results. Resuming an AI job reads stored records; it does not run Fetch or call source adapters.

## 2. OpenRouter and free-only admission

The adapter uses https://openrouter.ai/api/v1 and a server-side OPENROUTER_API_KEY. Model IDs are configuration; the business logic is provider-independent. Before every inference attempt, the adapter rechecks the exact model's pricing and available endpoints, required structured-output support, provider slug, context/output limits, and current ZDR endpoint list.

An inference is admitted only when:

1. The configured ID ends in :free and appears in the current model catalog.
2. Every listed model and selected-endpoint price field is present, numeric, and zero. Unknown or nonzero fields leave the job PENDING.
3. The selected endpoint is active and supports the request's JSON Schema output, temperature, and token parameters.
4. STRICT_PRIVATE (default) requires a currently ZDR-listed endpoint and sends data_collection="deny", zdr=true. PUBLIC_DEMO is local/test only, pins the reviewed Particle AI endpoint, sends data_collection="allow", and omits a request-level ZDR requirement.
5. Both profiles pin the endpoint, set allow_fallbacks=false, require_parameters=true, and max_price.prompt=0 / completion=0. No web-search plugin, tools, paid fallback, BYOK, credits, or key switching are used.
6. A shared PostgreSQL quota reservation commits before the chat-completion request.

### Data profiles

- `STRICT_PRIVATE` is the default and preserves the independent source allowlist, transfer approval, YouTube prohibition, and ZDR routing. Local/Vercel composition continues to use its existing primary Collection store.
- `PUBLIC_DEMO` requires an explicit server-side `TRENDSCOUT_AI_DATA_PROFILE=PUBLIC_DEMO`, local/test environment, and a distinct `ts_test_ai_demo_<hex>` schema configured through `TRENDSCOUT_AI_PUBLIC_DEMO_SCHEMA`. Vercel rejects this profile.
- PUBLIC_DEMO AI routes are bound to a separate PostgreSQL store scoped to `data_mode=SYNTHETIC`; the store must contain the server-written `ai_synthetic_provenance` marker. The schema cannot equal the primary schema. Client request models have no profile selector and reject extra fields.
- Only synthetic fixtures may be written to this profile. It cannot read the primary schema's keywords or Collections. Source IDs on fixture rows identify the data contract and do not enable source adapters or grant transfer of real provider data. A runtime check rejects LIVE rows.
- The selected public-demo model is `particle-ai/particle-1:free`, pinned to `particle-ai/us`. OpenRouter's provider directory currently labels Particle AI as no-training and zero-retention; model pricing/endpoint metadata is verified separately for each inference. No public prompt/completion exposure is accepted for this profile.
- Profile and profile-version are included in classification/recommendation digests. Cache and idempotency identity cannot cross STRICT_PRIVATE and PUBLIC_DEMO.
- Provider retention/training permission in PUBLIC_DEMO applies only to synthetic inputs. It does not approve real source data, production records, user research, credentials, or internal information.

No application-level model fallback is implemented. Classification and recommendation may use different configured model IDs; the checked-in example pins the same reviewed free candidate for both while keeping AI disabled. Synthetic smoke acceptance establishes the structured contract only, not broad model quality.

The internal budget is **40 physical inference requests per UTC day** and **20 per rolling minute**, shared through PostgreSQL. Failed requests and user retries consume reservations. Cache hits do not call the provider. The limit applies to model inference; metadata lookups are not counted as inference.

OpenRouter currently publishes a free-variant baseline of 50 requests/day and 20/minute; failed attempts count against the daily limit. These account limits can change. The internal daily cap leaves headroom and does not rely on separate per-model quotas. [Free model limits](https://openrouter.ai/collections/free-models/) · [Pricing and plan limits](https://openrouter.ai/pricing)

The ZDR list is mutable and endpoint-specific. OpenRouter's November 22, 2026 incident update says it removed 125 endpoints from ZDR eligibility; this supports checking the current endpoint list at each inference admission. ZDR controls provider retention; prompts still travel to and are processed by that provider, and OpenRouter describes legal/abuse-screening exceptions. Review the current OpenRouter and model-provider policies before enabling sources. [Incident update](https://openrouter.ai/blog/announcements/data-processing-incidents-on-inference-providers/) · [ZDR endpoint API](https://openrouter.ai/docs/api/api-reference/endpoints/list-endpoints-zdr) · [Privacy policy](https://openrouter.ai/privacy)

## 3. Persistence boundary

AI runtime requires PostgreSQL Neon and does not use SQLite, Firestore, Redis, in-memory quota counters, or a local model as its authority.

- Collection assignments, proposals, review, and rollback use the existing PostgresCollectionStore business tables.
- Shared attempts, quota, jobs, idempotency records, model verification metadata, classification cache, and recommendation cache use AtomicStateStore through PostgresAtomicStateStore, under scope trendscout-ai-engine-v1.
- The current atomic state store reads/writes coordination_fields, coordination_entries, coordination_checkpoints, source_payload_blobs, and source_payload_references. This requires existing migrations 0003_coordination.sql, 0004_source_payloads.sql, and 0008_payload_references.sql, as well as the existing business schema. The Database goal must confirm this compatibility boundary.
- The atomic-state port must serialize concurrent quota reservations across hosts. Local test doubles are not authoritative.
- PostgreSQL configuration failure blocks writes and inference. An ambiguous transaction outcome blocks the guarded external request.
- Job and cache retention are bounded. Expired caches may trigger recomputation; old results are not presented as fresh.
- The proposal acceptance transaction creates the Collection and selected memberships together. Rejection records name suppression. Reclassification membership changes are scoped to submitted keywords and can be rolled back through the existing reconciliation route.

## 4. Job statuses and errors

Job/result statuses:

| Status | Meaning |
| --- | --- |
| PENDING | Pricing or an eligible endpoint could not be verified; no inference was admitted. |
| RUNNING | The job has a live lease and is being processed. |
| COMPLETED | Validated output and resulting business records were saved, or a preview completed. |
| DEFERRED | Shared quota, rate limit, provider availability, or persistence temporarily blocked work. |
| MODEL_UNAVAILABLE | AI is disabled, the key is unavailable, or the selected model/endpoint cannot be used. |
| POLICY_DENIED | Source allowlist or provider policy requirements are not met. |
| INVALID_OUTPUT | JSON, schema, ID, evidence, or cross-reference validation failed. |
| FAILED | Non-retryable request or persistence failure. |

Public errors are sanitized: AI_DISABLED, AI_KEY_MISSING, AI_MODEL_UNAVAILABLE, AI_PRICING_UNVERIFIED, AI_POLICY_DENIED, AI_QUOTA_EXHAUSTED, AI_RATE_LIMITED, AI_INVALID_OUTPUT, AI_KEYWORD_NOT_FOUND, AI_COLLECTION_NOT_FOUND, AI_IDEMPOTENCY_REQUIRED, AI_JOB_NOT_RESUMABLE, AI_INPUT_CHANGED, and AI_STORAGE_UNAVAILABLE. Structured-output validation failures may include a safe machine-readable stage, reason, field path, expected constraint, and missing/unknown reference category. Rate-limit diagnostics may include a bounded `limit_source` category and normalized `Retry-After` / `X-RateLimit-Reset` times. No automatic retry is performed after HTTP 429. Provider text, prompt contents, credentials, and raw responses are not included.

retry_at is returned only when a provider Retry-After or supported reset header gives a valid timestamp within the bounded retry window. Otherwise it is null.

## 5. Capabilities and job API

### GET /api/v1/ai/capabilities

Returns feature state, configured model IDs, last metadata verification, internal quota, and safe provider quota counters when GET /api/v1/key succeeds. Key labels, hashes, and key values are never returned.

    {
      "enabled": false,
      "storage": "AVAILABLE",
      "models": {"classification": null, "recommendation": null},
      "metadata_verification": {
        "classification": {"status": "NOT_CONFIGURED", "verified_at": null},
        "recommendation": {"status": "NOT_CONFIGURED", "verified_at": null}
      },
      "quota": {"daily_used": 0, "daily_limit": 40, "minute_used": 0, "minute_limit": 20},
      "provider_quota": null,
      "error_code": "AI_DISABLED"
    }

OpenRouter documents GET /api/v1/key as an authenticated route returning fields such as is_free_tier, limit, limit_remaining, usage, and reset window. The application returns only a safe subset. [Current API key metadata](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key)

### GET /api/v1/ai/jobs/{job_id}

Returns job_id, task, status, timestamps, processed/attempt/token counts, sanitized error code, and retry timestamp.

### POST /api/v1/ai/jobs/{job_id}/resume

Resumes only PENDING, DEFERRED, MODEL_UNAVAILABLE, or expired RUNNING jobs from stored IDs and current stored context. It does not call source adapters.

## 6. Collection classification and proposal workflow

### POST /api/v1/ai/classifications/reclassify

Requires Idempotency-Key with 8–128 printable ASCII characters.

    {
      "keyword_ids": ["keyword-id-1", "keyword-id-2"],
      "preview": false
    }

The request has no text, evidence, source, Collection, model, URL, or credential fields. The service groups exact canonical phrases within each data mode and resolved entity identity, passes one logical keyword input with its durable source IDs, categories, active evidence IDs, and current Collection context, and fans a validated result back to eligible durable IDs. Unresolved records do not merge with resolved identities; conflicting known entities remain separate. Ineligible, excluded, user-assigned, user-pinned, disallowed-source, unsupported-mode, evidence-expired, and YouTube rows are not sent. Removed Collection IDs are unioned across grouped source records so a result cannot override a user's removal on any member.

Classification is batched. Initial batch size is bounded by model limits and token estimates, then adjusted using recent latency and structured-output validity. Only cache misses are sent. Cache identity includes the model, prompt/schema/input-contract/source-policy versions, canonical group and durable IDs, source/data-mode/categories, active evidence content/expiry, Collections and versions, and per-keyword override state. The Postgres write transaction rechecks keyword, active evidence lineage/expiry, categories, override, data mode, and Collection state before applying changes.

    {
      "job_id": "ai_<32 hex characters>",
      "status": "COMPLETED",
      "processed": 2,
      "assigned": [
        {"keyword_id": "keyword-id-1", "collection_id": "collection-id", "score": 0.86, "reason": "..."}
      ],
      "unassigned_keyword_ids": ["keyword-id-2"],
      "proposal_ids": ["prop_<32 hex characters>"],
      "skipped_keyword_ids": [],
      "membership_reconciliation_id": "reconcile-<UUID>",
      "retry_at": null,
      "error_code": null
    }

The score is a model self-assessment used with an initial 0.82 assignment threshold; it is not a probability, calibrated confidence, or validated accuracy. Only active Collections with auto_match=true can receive automatic assignments. A matching Collection listed in a user's removal feedback is excluded.

Setting preview=true returns proposed assignments and proposal drafts with mode="PREVIEW". It creates no Collection, proposal record, assignment, or reconciliation run. The validated AI result may still be cached and the preview job/quota usage is recorded.

### Proposal lifecycle

AI creates PENDING proposals only. A proposal contains a name, description, support keyword IDs, reason, score, uncertainty, evidence IDs, source IDs, model ID, and job ID. No Collection is created until user acceptance.

- PATCH /api/v1/ai/proposals/{proposal_id} edits the pending proposal name, description, or selected keyword IDs; every selected keyword and current evidence reference is revalidated.
- POST /api/v1/collections/suggestions/{proposal_id}/accept atomically creates the Collection and selected memberships.
- POST /api/v1/collections/suggestions/{proposal_id}/reject records rejection/suppression feedback.
- POST /api/v1/collections/reconciliation/{run_id}/rollback restores earlier AI memberships when the latest applicable targeted reconciliation has not been superseded.

User membership, pinned keywords, exclusion, removal feedback, proposal-name suppression, and current source policy are checked at persistence/acceptance time.

## 7. Export product recommendations

### POST /api/v1/ai/recommendations

Requires Idempotency-Key. keyword_ids accepts 1–20 IDs; research_constraints is optional and at most 500 characters; regenerate is a boolean. The server groups exact canonical phrases within each data mode and resolved entity identity and loads eligible normalized keywords, categories, Collections, active source evidence, and current algorithm results with active evidence lineage. Unresolved records do not merge with resolved identities; conflicting known entities remain separate. The profile keeps metric/unit, source role/platform, independent evidence groups, geography, window, freshness, conflicting/insufficient status, and source limitations separate. It supplies no composite score and no raw provider payload. If current evidence is insufficient, the bounded input says so explicitly.

Each recommendation is labeled AI hypothesis and includes a product name/category, US export target, related keyword/evidence IDs, rationale, potential segment, seasonality, possible materials, manufacturing/export considerations, competition/IP/regulatory risks, uncertainty, research steps, and encoded marketplace search links. A link creates a search URL; it does not mean the marketplace was searched.

The request JSON Schema is built for the exact keyword, Collection, and evidence IDs in each batch, with matching required fields, string/list bounds, and reference enums. The application validator additionally checks evidence-to-keyword lineage and rejects unsupported numerical or factual commercial claims. Qualitative research wording such as “research consumer demand,” “verify certification requirements,” and “compare competitor prices” is allowed when framed as a research step or question. Rejections expose only machine-readable stage, reason, field path, expected constraint, and reference category; raw model output is not stored or logged.

Identical business-input digests reuse the stored recommendation. Manual regeneration bypasses that cache and consumes a new request reservation; use a new idempotency key for a new action.

## 8. Environment

Repository-root `.env.example` is the only local environment template. The checked-in defaults keep AI disabled, STRICT_PRIVATE selected, source allowlist/transfer approval blank, and model IDs set only to the reviewed free candidate.

| Variable | Purpose |
| --- | --- |
| OPENROUTER_API_KEY | Server-side key for current key metadata and inference. |
| TRENDSCOUT_AI_ENABLED | Explicit feature switch; defaults to false. |
| TRENDSCOUT_AI_COLLECTION_MODEL | Exact currently verified :free model ID for classification; the checked-in example uses the reviewed Particle AI candidate while AI remains disabled. |
| TRENDSCOUT_AI_RECOMMENDATION_MODEL | Exact currently verified :free recommendation model ID; the checked-in example uses the same reviewed Particle AI candidate. |
| TRENDSCOUT_AI_ALLOWED_SOURCES | Comma-separated allowlist of Wikimedia, Google, and Bing source IDs; defaults to empty. YouTube is always denied. |
| TRENDSCOUT_AI_TRANSFER_APPROVED_SOURCES | Separate comma-separated approval for transferring those source records/evidence to the configured AI provider; defaults to empty and never grants extraction permission. YouTube is always denied. |
| TRENDSCOUT_AI_DATA_PROFILE | Server-side `STRICT_PRIVATE` default; `PUBLIC_DEMO` is restricted to local/test synthetic data. Never a request-body field. |
| TRENDSCOUT_AI_PUBLIC_DEMO_SCHEMA | Dedicated local/test schema matching `ts_test_ai_demo_<hex>`; it must be distinct from the primary schema and carry the server-set synthetic provenance marker. |

## 9. Provider references

Verified against OpenRouter's public model, endpoint, ZDR, routing, and provider policy metadata on **2026-11-27**. The AI01G synthetic smoke completed one expected classification and one recommendation with valid references; generation metadata confirmed Particle AI, the resolved free model, and zero cost for both. This is a narrow contract smoke, not a task-quality benchmark. STRICT_PRIVATE live-source use and production acceptance remain unverified. Provider policies can change.

- [Models API and pricing fields](https://openrouter.ai/docs/api/api-reference/models/get-models)
- [Per-model provider endpoints](https://openrouter.ai/docs/api/api-reference/endpoints/list-endpoints)
- [ZDR endpoint list](https://openrouter.ai/docs/api/api-reference/endpoints/list-endpoints-zdr)
- [Provider routing controls](https://openrouter.ai/docs/guides/routing/provider-selection)
- [Rate-limit source and reset metadata](https://openrouter.ai/blog/llm-api-rate-limits/)
- [Structured-output request mode](https://openrouter.ai/docs/guides/features/structured-outputs)
- [Free model list and limits](https://openrouter.ai/collections/free-models/)
- [OpenRouter privacy policy](https://openrouter.ai/privacy)
