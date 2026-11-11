# TrendScout Demo Online — Query Plan and Contracts

- **Contract proposal version:** 1.0
- **Status:** D0 technical proposal for D2–D6; not owner-approved.
- **Checked:** 2026-11-11 (ICT). No live source calls were made.

## Proposed default query plan

| Field | Proposed value |
|---|---|
| query_plan_id | demo-us-en |
| version | 1 |
| source provenance | Curated demo seeds informed by repository taxonomy topics-v1.0 and the TrendScout US-market objective; not derived from a measured representative sample |
| taxonomy scope | All active topic parent categories in topics-v1.0 plus one open discovery seed; unclassified remains a valid output state |
| market / language | US / en-US application intent; each provider receives only the parameters it documents |
| time window | Rolling seven days for proposed YouTube publication filtering and Google/Bing result filters. Wikimedia uses its daily observation date and current seven-day fallback/history behavior. Exact start/end timestamps must be captured per fetch. |
| query count | 14 canonical queries per query-based source; one initial page per query; no implicit pagination |
| budget proposal | At most 14 provider calls per source per fetch before retries. One in-flight request per provider. Total first-page calls: 14 YouTube requests, 14 Google-provider searches, and 14 Bing-provider searches if all were separately approved. Retries consume request budget. |
| cost / quota | YouTube search.list: current docs list 100 calls/day in a separate bucket, one quota unit per page. SerpApi: one credit per successful result page; two SERP engines cost up to 28 successful credits per complete fetch. Recheck project/account quota and current price immediately before any future approval. No package or billing approved. |
| result limits | YouTube maxResults=50; search providers return their own page sizes (commonly 10) and offset/cursor rules. One page only in the default proposal. Provider limits and changes must be reflected in descriptors. |
| pagination | YouTube nextPageToken; Google SerpApi start offset; Bing SerpApi first offset. Validate all cursors against the source and query fingerprint. Stop at one page by default. |
| deterministic identity | Plan fingerprint is SHA-256 of canonical JSON containing plan ID/version, seed provenance, taxonomy version/scope, canonical queries, source provider IDs, market/language, time-filter rule, page cap, and dedup version. Exclude secrets, fetch time, response data, and account identifiers. |
| deduplication | Deduplicate an entity within a source/provider/metric/window/market scope using its provider ID or canonical source URL. Preserve a many-to-many query-evidence relation so the same entity returned by different queries is linked to each query, not discarded. Cross-source records are never merged by title alone. |
| representativeness | This is a curated demo seed list, not a sample design. It does not represent all Internet topics, US demand, or query frequency. Fashion and Home & Living are priority regression/fixture areas only, not filters or weighting rules. |

### Canonical seed list

Queries are stored as lowercase Unicode strings with single spaces. The exact strings are stable within version 1; changing text, query parameters, taxonomy version, or ordering requires a new plan version.

| Order | Scope label | Canonical query |
|---:|---|---|
| 1 | fashion | fashion clothing footwear accessories trends |
| 2 | home_living (legacy UI label: home_goods) | home living kitchen furniture decor trends |
| 3 | technology | technology software gadgets electronics trends |
| 4 | gaming | gaming video games hardware esports trends |
| 5 | beauty_personal_care | beauty skincare cosmetics haircare trends |
| 6 | sports | sports athletes teams competitions equipment trends |
| 7 | food_beverage | food beverage cooking trends |
| 8 | health_fitness | health fitness wellness trends |
| 9 | travel_lifestyle | travel destinations lifestyle trends |
| 10 | business_finance | business finance companies markets trends |
| 11 | news_politics | news politics elections public policy current events |
| 12 | entertainment | entertainment film music television anime trends |
| 13 | celebrity_public_figures | creators celebrities public figures trends |
| 14 | open_discovery | emerging topics new public conversations |

These seeds are not approved market research. They can reflect the wording and taxonomy assumptions of their authors. Open discovery is a single broad query and is not a substitute for full-market coverage.

### Provider mapping proposal

| Provider | Market/language/time mapping | Request count and page cap |
|---|---|---|
| Wikimedia AQS | Endpoint has country=US and access=all-access; no query/language filter. Request current daily top list plus up to seven days of history using the existing adapter. | Not part of the 14-query count; source is a curated daily feed. Existing adapter caps lookback/history parameters. |
| YouTube Data API | search.list with q, type=video, regionCode=US, relevanceLanguage=en, publishedAfter derived from a captured UTC cutoff, maxResults<=50. | 14 first-page calls maximum; use only nextPageToken if an approved budget explicitly permits more pages. |
| SerpApi Google candidate | engine=google, q, gl=us, hl=en, google_domain=google.com, tbs=qdr:w. API key is never part of fingerprints or logs. | 14 first-page searches; start offset remains 0. |
| SerpApi Bing candidate | engine=bing, q, mkt=en-US, one reviewed recent-date filter. API key is never part of fingerprints or logs. | 14 first-page searches; first offset remains 1. |

## Data contract — provider-neutral layer

The internal data contract describes what a source adapter returns. It is separate from the HTTP API contract used by the browser.

### Capability descriptor

Each source descriptor is versioned and records:

- source_id, provider_id, capability_version, adapter_status, data_mode_supported, default_enabled;
- endpoint/method template and provider-specific parameter allowlist;
- authentication mode and server-side credential reference name only;
- supported market/language/time filters and what those values mean;
- query_based or curated_feed, page/cursor semantics, documented response caps;
- freshness/time basis, metric descriptors and comparability scope;
- quota bucket and cost unit when verified;
- retryable/permanent/quota/auth error classification;
- storage, refresh, attribution, and redistribution constraints;
- provenance fields and user-visible label/disclaimer.

Do not include raw API keys, tokens, full authorization headers, or credentials in a capability descriptor.

### Fetch request

An internal fetch request binds source_id, provider_id, data_mode, query_plan_id, query_plan_version, query_fingerprint, canonical query(s), market, language, exact time cutoff/window, maximum pages/results, request budget, deadline, idempotency key, and cancellation signal. User input may select an approved plan or an approved plan subset; it may not supply a URL, arbitrary provider parameters, Run ID, token, or API key.

### Page/batch and canonical observation

A source page records source_id, provider_id, data_mode, fetched_at, source_data_as_of, query plan/version/fingerprint, market, language, metric descriptor, requested page/cursor, response page/cursor, requested/returned count, completion state, safe source URL, quota usage, and sanitized error details.

A canonical observation records source/provider identity, source record ID or canonical URL, entity identity, query-evidence links, observation time/window and time basis, fetched_at, market/language/source geography (separate fields), metric name/value/unit/meaning, raw-versus-derived classification, data_mode, source URL, quality flags, and canonicalization version.

The data contract must keep provider-reported metrics separate from TrendScout-derived metrics. No cross-source score is part of D1.

### Progress, summary, quota, and error semantics

The fetch summary reports source/query/page totals and the following traceable values where available:

- source_id, provider_id, data_mode, fetched_at, source_data_as_of;
- query_plan_version, query_fingerprint, market, language;
- metric_name, metric_unit, source_url;
- requested_page_count, completed_page_count, returned_count, accepted_count, rejected_count, duplicate_count;
- pagination_state, quota_usage, stop_reason, sanitized errors, completeness, and status.

Counts are scoped and non-negative. Missing counts are null/unknown, not zero. Error objects contain a stable error code, HTTP status, retryability, safe attempt count, optional Retry-After seconds, and a redacted safe message; they never store request URLs containing secrets or raw provider exceptions.

### Modes, readiness, and mutually exclusive result state

Keep source readiness, data mode, and fetch outcome in separate fields:

- readiness: IMPLEMENTED, NOT_CONFIGURED, PENDING_APPROVAL;
- data_mode: LIVE, CACHE, REPLAY, SYNTHETIC;
- fetch outcome: SUCCESS, PARTIAL, FAILED, RATE_LIMITED, QUOTA_EXHAUSTED, TIMEOUT, INSUFFICIENT_DATA, CANCELLED.

If the source is not configured or not approved, there is no fetch outcome and no observation. CACHE/REPLAY/SYNTHETIC results must never be relabeled LIVE. A single source attempt has exactly one terminal outcome. PARTIAL is a terminal outcome with explicit completed/failed page counts and errors; it cannot also be SUCCESS or FAILED. SUCCESS with zero returned rows is allowed only when the provider contract explicitly says the query succeeded with no results; missing or censored records are not zero.

Suggested state transitions:

    NOT_CONFIGURED / PENDING_APPROVAL -> no request
    READY -> QUEUED -> FETCHING -> SUCCESS | PARTIAL | FAILED
    FETCHING -> RATE_LIMITED | QUOTA_EXHAUSTED | TIMEOUT | CANCELLED

Run-level status is derived from source outcomes using one documented rule; never overwrite a failed refresh with an older result while reporting a new SUCCESS.

## Future HTTP API contract — not implemented in D0 or D1

Keep transport API and source/data contracts independent.

| Method and route | Proposed behavior |
|---|---|
| POST /api/v1/fetches | One-click Fetch uses the server default query plan. Body selects an approved query_plan_id/version and optional allowed source IDs; no query text, URL, token, Run ID, or credential. Requires authenticated demo session, CSRF protection when cookie-authenticated, idempotency key, allowlist, rate limit, and cost budget. Returns 202 with fetch_id and QUEUED status. |
| GET /api/v1/fetches/{fetch_id} | Authenticated progress polling returns safe progress, per-source/page counts, data_mode, source timestamps, completeness, terminal status, and sanitized errors. It does not trigger another fetch. |
| GET /api/v1/query-plans/{plan_id} | Optional read-only metadata for plan/version and configured sources; exposes no credential values. |

The existing POST /api/runs contract and authentication are preserved during D1. It must not be removed or made anonymous to achieve one-click UI behavior; the new session model requires a separate approval and implementation phase.

### Server-side paid-request protection

- Authenticated demo session or explicit server-side allowlist; never trust a role, tenant, source enablement, or quota claim supplied by the browser.
- CSRF token and SameSite/HttpOnly/Secure session cookie when cookie authentication is selected; validate Origin/Host on state-changing routes.
- Per-session and global request limits, one active fetch per demo instance, bounded provider concurrency, per-source page/query caps, bounded retries, an absolute deadline, and a cost/quota budget checked before every attempt.
- Idempotency key binds authenticated actor, plan/version, source set, canonical request fingerprint, and freshness window. Replays return the existing fetch and do not spend quota twice.
- Fixed provider host allowlist, HTTPS, redirects disabled or revalidated, proxy disabled unless explicitly approved, no arbitrary URL fetch, and strict response size/schema limits.
- Redact API keys from query parameters, request URLs, exceptions, traces, and logs. Provider credentials remain server-side and are never returned in API responses or frontend bundles.
- No scheduler, cleanup worker, AI provider, paid provider, or cloud worker is enabled by this contract.

## Acceptance matrix

| ID | D0 acceptance | Evidence / result |
|---|---|---|
| D0-A | Git baseline and instructions verified | Local main and origin/main resolve to e4764a89865c24600b2ca6ede47f05b694d7a900; clean worktree; both handoff commits exist and are ancestors; root and Frontend AGENTS.md read |
| D0-B | Required local baseline rerun | Offline readiness runner passed: 371/371 tests, Golden 7/7 under seeds 1 and 983, pip/syntax/bundle/browser/benchmark checks passed |
| D0-C | Actual ownership audited | Settings, composition, main/API, source registry/Wikimedia adapter, algorithms, storage boundaries, tests, frontend request flow and packaging reviewed |
| D0-D | Four-source capability matrix | This document; future providers remain disabled/pending |
| D0-E | Versioned default query plan | demo-us-en v1 proposal with 14 canonical queries, all taxonomy topics plus open discovery, fingerprint/budget/dedup |
| D0-F | D2–D6 data and HTTP contracts | This document; HTTP/data contracts kept separate; one-click API is proposal only |
| D0-G | Approvals, security, rollback | Listed in D0 report; no provider, paid plan, identity, or storage approval inferred |
