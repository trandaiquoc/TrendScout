# TrendScout AI Integration Contract

**Version:** 1.1-draft
**Status:** Implemented and merged into local main at owner request; regression gates, PostgreSQL cutover, and model quality remain unverified
**Owner:** AI goal
**Updated:** 2026-11-26

The AI boundary is implemented in this branch. The Database goal owns PostgreSQL schema and migrations. No Documents/Integration/DB_CONTRACT.md existed at the baseline, so Database review is still needed before treating the persistence mapping as stable. The AI goal does not apply or propose a migration.

## 1. Responsibilities and boundaries

- Source adapters, ingestion, normalization, market-signal calculations, and statistical algorithms remain outside the AI service.
- AI classifies eligible normalized keywords against existing Collections, proposes new Collections for user review, supports user-triggered reclassification, and creates qualitative Vietnam-to-US export product hypotheses.
- AI does not fetch source data, calculate search volume, create evidence, create a Collection without user acceptance, or claim a product will sell or be profitable.
- Provider input is built server-side from database IDs, normalized text, selected Collection context, source IDs, and permitted evidence references. Request bodies accept IDs and bounded research constraints only. Raw source payloads, credentials, and YouTube data are excluded.
- The supported source allowlist is Wikimedia, Google Search/Trends, and Bing. It defaults to empty. YouTube is denied even if configured.
- Source geography and observation time remain evidence metadata. English text or a US-targeted product hypothesis does not establish US authorship, readership, or demand.
- An AI failure does not discard persisted keywords or algorithm results. Resuming an AI job reads stored records; it does not run Fetch or call source adapters.

## 2. OpenRouter and free-only admission

The adapter uses https://openrouter.ai/api/v1 and a server-side OPENROUTER_API_KEY. Model IDs are configuration; the business logic is provider-independent. Before every inference attempt, the adapter rechecks the exact model's pricing and available endpoints, required structured-output support, provider slug, context/output limits, and current ZDR endpoint list.

An inference is admitted only when:

1. The configured ID ends in :free and appears in the current model catalog.
2. Every listed model and selected-endpoint price field is present, numeric, and zero. Unknown or nonzero fields leave the job PENDING.
3. The selected endpoint is active, supports JSON Schema output, and is currently listed as ZDR eligible.
4. The request pins the endpoint's provider slug, sets allow_fallbacks=false, require_parameters=true, data_collection="deny", zdr=true, and max_price.prompt=0 / completion=0.
5. No web-search plugin, tools, paid model fallback, BYOK, credit top-up, or key switching is used.
6. A shared PostgreSQL quota reservation commits before the chat-completion request.

No application-level model fallback is implemented. Classification and recommendation may use different configured model IDs, but both are blank until exact-model task quality is reviewed.

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

Public errors are sanitized: AI_DISABLED, AI_KEY_MISSING, AI_MODEL_UNAVAILABLE, AI_PRICING_UNVERIFIED, AI_POLICY_DENIED, AI_QUOTA_EXHAUSTED, AI_RATE_LIMITED, AI_INVALID_OUTPUT, AI_KEYWORD_NOT_FOUND, AI_COLLECTION_NOT_FOUND, AI_IDEMPOTENCY_REQUIRED, AI_JOB_NOT_RESUMABLE, AI_INPUT_CHANGED, and AI_STORAGE_UNAVAILABLE. Provider text, prompt contents, credentials, and raw responses are not included.

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

The request has no text, evidence, source, Collection, model, URL, or credential fields. The service deduplicates IDs and loads eligible normalized text, current Collection IDs/names/descriptions/versions, allowed evidence references, and override state. Ineligible, excluded, user-assigned, user-pinned, disallowed-source, and YouTube rows are not sent.

Classification is batched. Initial batch size is bounded by model limits and token estimates, then adjusted using recent latency and structured-output validity. Only cache misses are sent. Cache identity includes the model, prompt/schema/policy versions, normalized keyword, source, evidence references, Collections and versions, and override state. The Postgres write transaction rechecks keyword/evidence/override and Collection state before applying changes.

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

Requires Idempotency-Key. keyword_ids accepts 1–20 IDs; research_constraints is optional and at most 500 characters; regenerate is a boolean. The server loads eligible normalized keywords, Collections, bounded algorithm results, freshness/scope, and allowed evidence references.

Each recommendation is labeled AI hypothesis and includes a product name/category, US export target, related keyword/evidence IDs, rationale, potential segment, seasonality, possible materials, manufacturing/export considerations, competition/IP/regulatory risks, uncertainty, research steps, and encoded marketplace search links. A link creates a search URL; it does not mean the marketplace was searched.

Narrative output is screened for unsupported metrics and commercial claims. Evidence IDs must come from the server-supplied context and support the selected keywords. Identical business-input digests reuse the stored recommendation. Manual regeneration bypasses that cache and consumes a new request reservation; use a new idempotency key for a new action.

## 8. Environment

Backend/.env.example shows the supported variable names. The checked-in defaults keep AI disabled and models/source allowlist blank.

| Variable | Purpose |
| --- | --- |
| OPENROUTER_API_KEY | Server-side key for current key metadata and inference. |
| TRENDSCOUT_AI_ENABLED | Explicit feature switch; defaults to false. |
| TRENDSCOUT_AI_COLLECTION_MODEL | Exact currently verified :free model ID for classification; unset pending quality review. |
| TRENDSCOUT_AI_RECOMMENDATION_MODEL | Exact currently verified :free recommendation model ID; may equal the classifier model. |
| TRENDSCOUT_AI_ALLOWED_SOURCES | Comma-separated allowlist of Wikimedia, Google, and Bing source IDs; defaults to empty. YouTube is always denied. |

## 9. Provider references

Verified against OpenRouter's public model, endpoint, ZDR, and routing metadata on **2026-11-26**. This verifies catalog metadata only; it does not prove that credentials can call inference, establish task quality, or guarantee that provider policies remain unchanged.

- [Models API and pricing fields](https://openrouter.ai/docs/api/api-reference/models/get-models)
- [Per-model provider endpoints](https://openrouter.ai/docs/api/api-reference/endpoints/list-endpoints)
- [ZDR endpoint list](https://openrouter.ai/docs/api/api-reference/endpoints/list-endpoints-zdr)
- [Provider routing controls](https://openrouter.ai/docs/guides/routing/provider-selection)
- [Structured-output request mode](https://openrouter.ai/docs/guides/features/structured-outputs)
- [Free model list and limits](https://openrouter.ai/collections/free-models/)
- [OpenRouter privacy policy](https://openrouter.ai/privacy)
