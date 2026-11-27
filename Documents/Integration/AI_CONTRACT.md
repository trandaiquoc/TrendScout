# TrendScout AI Integration Contract

**Version:** 1.5-draft
**Status:** AI input contract, profile isolation, server-side `PUBLIC_DATA` transfer boundary, and AI03 automatic Collection matching are implemented on `integration/demo-final`; AI01G synthetic acceptance passed on 2026-11-27. AI01H real-source acceptance is tracked separately in `Documents/Reports/AI01H_PUBLIC_DATA_ACCEPTANCE.md`. Production activation remains disabled by default.
**Owner:** AI goal
**Updated:** 2026-11-27

The AI boundary is implemented in this branch. The Database goal owns the general PostgreSQL schema and migrations. No Documents/Integration/DB_CONTRACT.md existed at the baseline, so Database review is still needed before treating the persistence mapping as stable. AI03 adds migration `0013_fetch_job_ai_work.sql` for durable Fetch-to-AI batch linkage; it is versioned and operator-applied, never applied by an application request.

## 1. Responsibilities and boundaries

- Source adapters, ingestion, normalization, market-signal calculations, and statistical algorithms remain outside the AI service.
- AI classifies eligible normalized keywords against existing Collections, proposes new Collections for user review, supports user-triggered reclassification, and creates qualitative Vietnam-to-US export product hypotheses.
- AI does not fetch source data, calculate search volume, create evidence, create a Collection without user acceptance, or claim a product will sell or be profitable.
- Provider input is built server-side from exact canonical keyword groups and retains all durable source keyword IDs. Groups do not cross data modes or resolved entity identities; unresolved records group only with other unresolved records, and conflicting known entities remain separate. Categories, per-source geography/freshness, active evidence references, source-specific algorithm results, and Collection/user constraints are bounded and deterministically ordered. Request bodies accept IDs and bounded research constraints only. Raw source payloads, credentials, and YouTube data are excluded.
- For LIVE data in `PUBLIC_DATA` or `STRICT_PRIVATE`, a source must appear in both `TRENDSCOUT_AI_ALLOWED_SOURCES` and the independent `TRENDSCOUT_AI_TRANSFER_APPROVED_SOURCES`, and its current source-policy registry entry must allow the task and fields. Both environment lists default to empty. Fetch, storage, derived-metric, AI-transfer, and provider-retention permissions remain independent. YouTube is denied even if configured. See `Documents/Integration/AI_SOURCE_POLICY_MATRIX.md`.
- Expired or expiry-unknown evidence is omitted. AI cache/result lifetime is bounded by the earliest active evidence expiry; Fetch replay does not extend source expiry.
- Source geography and observation time remain evidence metadata. English text or a US-targeted product hypothesis does not establish US authorship, readership, or demand.
- An AI failure does not discard persisted keywords, evidence, existing Collection assignments, or algorithm results. Resuming a Fetch's stored AI batches does not run Fetch, source adapters, or algorithms.
- When the server-side AI feature switch is enabled for `PUBLIC_DATA` or `STRICT_PRIVATE`, automatic Collection matching runs after Fetch through the existing Collection processing handoff. Source Fetch becomes terminal before inference; local processing continues after its response, while Vercel uses the subsequent retained-checkpoint Collection recovery request. This AI execution is represented by PostgreSQL-backed AI jobs, not a Vercel fire-and-forget task.
- Automatic Fetch classification considers current active Collections and only writes to those with `auto_match=true`. The model's score is a heuristic compared with the configured assignment threshold. Unmatched keywords remain unassigned; model-produced new-Collection proposals are ignored by this automatic path and are handled by the proposal workflow separately.

## 2. OpenRouter and free-only admission

The adapter uses https://openrouter.ai/api/v1 and a server-side OPENROUTER_API_KEY. Classification and recommendation model IDs are separate configuration; business logic does not select a model. Before every inference attempt, the adapter rechecks the exact model's `:free` catalog entry, zero model and endpoint pricing, active endpoint, required structured-output support, provider slug, context/output limits, and profile-specific provider policy. STRICT_PRIVATE additionally rechecks the current ZDR endpoint list.

An inference is admitted only when:

1. The configured ID ends in :free and appears in the current model catalog.
2. Every listed model and selected-endpoint price field is present, numeric, and zero. Unknown or nonzero fields leave the job PENDING.
3. The selected endpoint is active and supports the request's JSON Schema output, temperature, and token parameters.
4. STRICT_PRIVATE requires a currently ZDR-listed endpoint and sends data_collection="deny", zdr=true. PUBLIC_DATA and PUBLIC_DEMO pin the reviewed Particle AI provider endpoint, send data_collection="allow", and do not require request-level ZDR. PUBLIC_DEMO is local/test-only synthetic data; PUBLIC_DATA is eligible for local and Vercel use only with a verified source grant and explicit source configuration.
5. All profiles pin the endpoint and set allow_fallbacks=false, require_parameters=true, and max_price.prompt=0 / completion=0. No web-search plugin, tools, paid fallback, BYOK, credits, or key switching are used. PUBLIC_DATA does not permit public prompt/completion exposure; OpenRouter's current default API logging excludes prompt/completion text, and the selected Particle AI policy currently says no training and zero retention. Review these provider policies again before activation.
6. A shared PostgreSQL quota reservation commits before the chat-completion request.

### Data profiles

- `PUBLIC_DATA` is the runtime default profile proposed for TrendScout's public-source workflow and supports local/Vercel composition over the existing PostgreSQL Collection store. It accepts only source IDs present in the source-policy registry, the server allowlist, and the separate transfer-approval list. The checked-in policy currently allows only `wikimedia_pageviews_top_per_country` for live AI input. The AI feature remains disabled and both source lists remain empty by default.
- PUBLIC_DATA sends only the source registry's field projection. It omits user removal feedback, existing Collection membership context, unapproved Collection names/descriptions, and raw provider payloads. Collection names/descriptions are eligible only when their durable IDs appear in server-side `TRENDSCOUT_AI_PUBLIC_DATA_COLLECTION_IDS`; the runtime screens those texts for private/confidential markers and common PII patterns. Non-empty `research_constraints` are rejected in PUBLIC_DATA because no server-side public classification exists for request-supplied constraints.
- `STRICT_PRIVATE` is an opt-in private-data profile. It preserves the independent source allowlist, transfer approval, YouTube prohibition, and ZDR routing. An invalid profile fails closed; it does not switch profiles. PublicData source permissions never override STRICT_PRIVATE's ZDR requirements.
- `PUBLIC_DEMO` requires an explicit server-side `TRENDSCOUT_AI_DATA_PROFILE=PUBLIC_DEMO`, local/test environment, and a distinct `ts_test_ai_demo_<hex>` schema configured through `TRENDSCOUT_AI_PUBLIC_DEMO_SCHEMA`. Vercel rejects this profile.
- PUBLIC_DEMO AI routes are bound to a separate PostgreSQL store scoped to `data_mode=SYNTHETIC`; the store must contain the server-written `ai_synthetic_provenance` marker. The schema cannot equal the primary schema. Client request models have no profile selector and reject extra fields.
- Only synthetic fixtures may be written to this profile. It cannot read the primary schema's keywords or Collections. Source IDs on fixture rows identify the data contract and do not enable source adapters or grant transfer of real provider data. A runtime check rejects LIVE rows.
- The selected public-demo model is `particle-ai/particle-1:free`, pinned to `particle-ai/us`. OpenRouter's provider directory currently labels Particle AI as no-training and zero-retention; model pricing/endpoint metadata is verified separately for each inference. No public prompt/completion exposure is accepted for this profile.
- Profile, profile version, transfer-policy version, source approvals, source field policy, and approved public Collection IDs are included in classification/recommendation digests. Cache and idempotency identity cannot cross STRICT_PRIVATE, PUBLIC_DATA, and PUBLIC_DEMO.
- PUBLIC_DATA eligibility comes only from an explicit source license/policy review and does not approve unrelated records, user research, credentials, private Collection context, or confidential business constraints. PUBLIC_DEMO's provider-policy choice applies only to synthetic fixtures and grants no live-source permission.

No application-level model fallback is implemented. Classification and recommendation may use different configured model IDs; the checked-in example pins the same reviewed free candidate for both while keeping AI disabled. Synthetic smoke acceptance establishes the structured contract only, not broad model quality.

The internal budget is **40 physical inference requests per UTC day** and **20 per rolling minute**, shared through PostgreSQL. Failed requests and user retries consume reservations. Cache hits do not call the provider. The limit applies to model inference; metadata lookups are not counted as inference.

A provider HTTP 429 records a shared PostgreSQL `provider_retry_at` across classification and recommendation. Every AI task remains deferred until the normalized provider Retry-After/reset time; if the provider supplied no usable reset, the runtime waits one full rolling minute. Repeated submissions still consume internal reservations only when a physical inference is admitted.

OpenRouter currently publishes a free-variant baseline of 50 requests/day and 20/minute; failed attempts count against the daily limit. These account limits can change. The internal daily cap leaves headroom and does not rely on separate per-model quotas. [Free model limits](https://openrouter.ai/collections/free-models/) · [Pricing and plan limits](https://openrouter.ai/pricing)

The ZDR list is mutable and endpoint-specific. OpenRouter's November 22, 2026 incident update says it removed 125 endpoints from ZDR eligibility; this supports checking the current endpoint list at each inference admission. ZDR controls provider retention; prompts still travel to and are processed by that provider, and OpenRouter describes legal/abuse-screening exceptions. Review the current OpenRouter and model-provider policies before enabling sources. [Incident update](https://openrouter.ai/blog/announcements/data-processing-incidents-on-inference-providers/) · [ZDR endpoint API](https://openrouter.ai/docs/api/api-reference/endpoints/list-endpoints-zdr) · [Privacy policy](https://openrouter.ai/privacy)

## 3. Persistence boundary

AI runtime requires PostgreSQL Neon and does not use SQLite, Firestore, Redis, in-memory quota counters, or a local model as its authority.

- Collection assignments, proposals, review, and rollback use the existing PostgresCollectionStore business tables.
- `fetch_jobs.ai_jobs` stores the stable AI job ID and keyword IDs for each automatic Fetch batch. Migration `0013_fetch_job_ai_work.sql` adds this JSONB field; the Local SQLite store has a corresponding additive column migration.
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

Classification jobs with `INVALID_OUTPUT` can be explicitly resumed after the output validator rejects a provider response. The same durable job ID is used, while current input, evidence, Collection context, and user state are reloaded and revalidated.

Fetch processing exposes separate source-ingestion, dataset-materialization, algorithm, and AI-classification states on the dataset snapshot. A Fetch's Collection job response includes `ai_job_ids` and per-batch AI status summaries. On a deferred result, all batch IDs and keyword IDs are already stored in `fetch_jobs.ai_jobs`.

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

Resumes only PENDING, DEFERRED, MODEL_UNAVAILABLE, INVALID_OUTPUT, or expired RUNNING jobs from stored IDs and current stored context. It does not call source adapters.

### GET /api/v1/collections/jobs/{fetch_id}

Returns the persisted Collection-processing status plus stable `ai_job_ids` and per-batch status, progress, retry time, and sanitized error code.

### POST /api/v1/collections/jobs/{fetch_id}/resume

Resumes only that Fetch's persisted AI batches. It reloads the current classification context and user state, then reuses completed batches and resumes deferred, invalid-output, or expired-running jobs. It never re-runs source Fetch or algorithms. When all batches complete, it marks the dataset's AI status completed and refreshes the Fetch job summary.

## 6. Collection classification and proposal workflow

### POST /api/v1/ai/classifications/reclassify

Requires Idempotency-Key with 8–128 printable ASCII characters.

    {
      "keyword_ids": ["keyword-id-1", "keyword-id-2"],
      "preview": false
    }

The request has no text, evidence, source, Collection, model, URL, or credential fields. The service groups exact canonical phrases within each data mode and resolved entity identity, passes one logical keyword input with its durable source IDs, categories, active evidence IDs, and eligible Collection context, and fans a validated result back to eligible durable IDs. Unresolved records do not merge with resolved identities; conflicting known entities remain separate. Ineligible, excluded, user-assigned, user-pinned, disallowed-source, unsupported-mode, evidence-expired, and YouTube rows are not sent. Removed Collection IDs remain part of the server-side write guard and are never included in PUBLIC_DATA prompts. PUBLIC_DATA sends only fields allowed by the source policy matrix; Collection names/descriptions require the server-side public Collection ID allowlist and a content screen.

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

For automatic Fetch classification (`automatic=true`), model-proposed new Collections are not persisted. Keywords below threshold or without a matching eligible Collection remain unassigned until the separate proposal workflow is used.

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

Requires Idempotency-Key. keyword_ids accepts 1–20 IDs; research_constraints is optional and at most 500 characters; regenerate is a boolean. In PUBLIC_DATA, non-empty request-supplied research constraints are rejected because they have no server-side public classification. The server groups exact canonical phrases within each data mode and resolved entity identity and loads eligible normalized keywords, categories, active source evidence, and current algorithm results with active evidence lineage. Existing Collection membership context is omitted from PUBLIC_DATA. Unresolved records do not merge with resolved identities; conflicting known entities remain separate. The profile keeps metric/unit, source role/platform, geography, window, freshness, conflicting/insufficient status, and source limitations separate. It supplies no composite score and no raw provider payload. If current evidence is insufficient, the bounded input says so explicitly.

Each recommendation is labeled AI hypothesis and includes a product name/category, US export target, related keyword/evidence IDs, rationale, potential segment, seasonality, possible materials, manufacturing/export considerations, competition/IP/regulatory risks, uncertainty, research steps, and encoded marketplace search links. A link creates a search URL; it does not mean the marketplace was searched.

The request JSON Schema is built for the exact keyword, Collection, and evidence IDs in each batch, with matching required fields, string/list bounds, and reference enums. The application validator additionally checks evidence-to-keyword lineage and rejects unsupported numerical or factual commercial claims. Qualitative research wording such as “research consumer demand,” “verify certification requirements,” and “compare competitor prices” is allowed when framed as a research step or question. Rejections expose only machine-readable stage, reason, field path, expected constraint, and reference category; raw model output is not stored or logged.

Identical business-input digests reuse the stored recommendation. Manual regeneration bypasses that cache and consumes a new request reservation; use a new idempotency key for a new action.

## 8. Environment

Repository-root `.env.example` is the only local environment template. The checked-in defaults keep AI disabled, select PUBLIC_DATA as the proposed operating profile, leave source allowlist/transfer approval and public Collection context blank, and set classification and recommendation model IDs to the reviewed free candidate. Vercel must use equivalent server-side environment settings; this change does not modify Production ENV.

| Variable | Purpose |
| --- | --- |
| OPENROUTER_API_KEY | Server-side key for current key metadata and inference. |
| TRENDSCOUT_AI_ENABLED | Explicit feature switch; defaults to false. |
| TRENDSCOUT_AI_COLLECTION_MODEL | Exact currently verified :free model ID for classification; the checked-in example uses the reviewed Particle AI candidate while AI remains disabled. |
| TRENDSCOUT_AI_RECOMMENDATION_MODEL | Exact currently verified :free recommendation model ID; the checked-in example uses the same reviewed Particle AI candidate. |
| TRENDSCOUT_AI_ALLOWED_SOURCES | Comma-separated allowlist of Wikimedia, Google, and Bing source IDs; defaults to empty. YouTube is always denied. |
| TRENDSCOUT_AI_TRANSFER_APPROVED_SOURCES | Separate comma-separated approval for transferring those source records/evidence to the configured AI provider; defaults to empty and never grants extraction permission. YouTube is always denied. |
| TRENDSCOUT_AI_DATA_PROFILE | Server-side `PUBLIC_DATA` runtime default; `STRICT_PRIVATE` is an explicit private-data profile requiring deny/ZDR; `PUBLIC_DEMO` is restricted to local/test synthetic data. Never a request-body field. |
| TRENDSCOUT_AI_PUBLIC_DEMO_SCHEMA | Dedicated local/test schema matching `ts_test_ai_demo_<hex>`; it must be distinct from the primary schema and carry the server-set synthetic provenance marker. |
| TRENDSCOUT_AI_PUBLIC_DATA_COLLECTION_IDS | Server-side IDs of Collections reviewed and classified public for AI context. Defaults to empty; unlisted Collection text is omitted. |

## 9. Provider references

Provider and source policies were reviewed on **2026-11-27**. The Wikimedia Pageviews Analytics API states its data is CC0. Google/Bing search and Google Trends transfers remain denied because the current retrieval paths do not establish the required permission; YouTube and Wikimedia entity text remain denied. OpenRouter's provider directory currently labels Particle AI no-training/zero-retention; Particle's current Terms and Privacy Policy describe standard synchronous inference as zero-retention and no-training. OpenRouter says prompt/completion text is not logged by default, though request metadata may be retained. See the dated source matrix for direct policy references and restrictions. The AI01G synthetic smoke completed one expected classification and one recommendation with valid references; generation metadata confirmed Particle AI, the resolved free model, and zero cost for both. This is a narrow contract smoke, not a task-quality benchmark. PUBLIC_DATA real-source acceptance and production activation are tracked separately; provider/source policies can change.

- [Models API and pricing fields](https://openrouter.ai/docs/api/api-reference/models/get-models)
- [Per-model provider endpoints](https://openrouter.ai/docs/api/api-reference/endpoints/list-endpoints)
- [ZDR endpoint list](https://openrouter.ai/docs/api/api-reference/endpoints/list-endpoints-zdr)
- [Provider routing controls](https://openrouter.ai/docs/guides/routing/provider-selection)
- [Rate-limit source and reset metadata](https://openrouter.ai/blog/llm-api-rate-limits/)
- [Structured-output request mode](https://openrouter.ai/docs/guides/features/structured-outputs)
- [Free model list and limits](https://openrouter.ai/collections/free-models/)
- [OpenRouter privacy policy](https://openrouter.ai/privacy)
