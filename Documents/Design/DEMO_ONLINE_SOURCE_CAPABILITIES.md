# TrendScout Demo Online — Source Capability Matrix

- **Status:** D0 proposal updated through D4 offline adapters; no provider access, pricing, storage right, or live readiness is owner-approved.
- **Matrix version:** 1.2
- **Checked:** 2026-11-11 (ICT). Official documentation was reviewed; no provider endpoint was called.

This document separates TrendScout's target US/en-US market from the population measured by a provider. Search-result localization does not establish the author's, viewer's, or audience's residence. Provider records are evidence within the returned sample, not a census of the Internet.

## Capability summary

| TrendScout source ID | Provider ID | Proposed interface | Data shape | Query or feed | Current repository state | UI publication state |
|---|---|---|---|---|---|---|
| wikimedia_pageviews_top_per_country | wikimedia_aqs | Wikimedia Analytics API (AQS) Pageviews REST API | Daily top-list entries with project, article, rank, and views_ceil | Provider-curated top list; no query parameter in the current adapter | Implemented adapter, lazy registry factory, MockTransport/fixture tests; live API was not called in this audit | May be labeled Wikimedia Pageviews, US top-list scope, approximate pageviews; never search volume, unique audience, or complete topic coverage |
| youtube_video_search | youtube_data_api_v3 | YouTube Data API v3 search.list | Query-scoped video search resources and metadata; no view-count field | Query-based video search; not a curated all-topic trending feed | Adapter implemented and verified with synthetic fixtures/MockTransport only; not registered for production or Public Fetch | PENDING_APPROVAL; do not publish a derived TrendScout score from YouTube data |
| google_web_serp | serpapi_google | SerpApi Google Search Engine Results API candidate | Query-scoped organic result sample, rank, title, URL, snippets where returned | Query-based search result sample | D4 adapter uses synthetic MockTransport only; no account, real key, approval, or live call | PENDING_APPROVAL; label as Google search-result sample, never Google search volume or Google Trends |
| bing_web_serp | serpapi_bing | SerpApi Bing Search Engine Results API candidate | Query-scoped organic result sample, rank, title, URL, snippets where returned | Query-based search result sample | D4 adapter uses synthetic MockTransport only; no account, real key, approval, or live call | PENDING_APPROVAL; label as Bing search-result sample, never search volume or an official Microsoft Bing API |

## Wikimedia Analytics API

| Capability | D0 finding |
|---|---|
| Provider / endpoint | Wikimedia Foundation Analytics Query Service (AQS), Pageviews API. Existing code issues GET to https://wikimedia.org/api/rest_v1/metrics/pageviews/top-per-country/US/all-access/{YYYY}/{MM}/{DD}. The AQS reference describes page-view analytics and most-viewed-pages endpoints. |
| Data and metric | The adapter maps provider views_ceil into a daily page observation. It is an approximate upper-bound pageview metric for the returned top list. Pageviews are requests for page content, not unique people, search volume, purchase intent, or demand. |
| Market / language | The endpoint's country path is US and access is all-access. It does not set a topic or language filter; project/article identifiers can span Wikimedia projects. Country-scoped data has privacy protections and is not equivalent to reader identity or a complete US market measure. |
| Pagination / maximum | The current endpoint/adapter has no cursor loop. It accepts one response list per day. Provider top-list censorship and any provider-side limits mean absence from a returned list is not zero or evidence of no interest. The exact maximum was not established from the reviewed reference. |
| Freshness / time | The observation date is in the request path; fetched_at is recorded separately. The current adapter looks back up to seven days for an available current snapshot, then requests up to seven historical days. The provider does not provide a freshness SLA in the reviewed documentation. |
| Authentication / quota | No API credential is used by the current adapter. Wikimedia requires an informative User-Agent with contact information and asks clients to honor throttling instructions, avoid harmful request spikes/concurrency, and follow applicable content licenses. No fixed request quota or per-call charge was confirmed. |
| Retry / failures | Current adapter treats 404 as day unavailable, uses bounded retries for request errors, 429, and 5xx, parses Retry-After as seconds or HTTP-date, and stops when the requested wait exceeds its configured cap. Tests use MockTransport. |
| Storage / redistribution | Public API access does not waive content-license or reuse requirements. Confirm license and attribution duties for the exact returned data and displayed article metadata before broader redistribution. No provider live permission or cloud storage test was performed. |
| Provenance / UI | Preserve source ID, provider ID, endpoint, country/access scope, observation date, fetched_at, response status, request count, counts, rejected records, and source URL when available. UI must say “Wikimedia Pageviews” and explain top-list censorship and approximation. |

Official references, accessed 2026-11-11:

- [Wikimedia Analytics API — Page view analytics](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html)
- [Wikimedia Analytics API — country data and pageview privacy limits](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/concepts/country-data.html)
- [Wikimedia Analytics API — pageview definition](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/concepts/page-views.html)
- [Wikimedia Foundation API Usage Guidelines](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_API_Usage_Guidelines)
- [Wikimedia Foundation User-Agent Policy](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy)

## YouTube Data API v3

| Capability | D0 finding |
|---|---|
| Provider / endpoint | Google YouTube Data API v3, GET https://www.googleapis.com/youtube/v3/search. D3B implements only search.list; possible follow-up videos.list statistics are out of scope. Do not use HTML scraping. |
| Data and metric | search.list returns videos matching a query and search ordering. The adapter records result membership/rank and provider metadata; it does not request view counts. A result count or rank is not search volume, unique viewers, audience geography, or a cross-source trend metric. |
| Market / language | Candidate parameters: regionCode=US, relevanceLanguage=en, type=video. regionCode means videos that can be viewed in a country; it does not prove the viewer or creator is US-based. Relevance language does not exclude other languages. |
| Query / feed distinction | search.list(q=...) is query-based. videos.list(chart=mostPopular) is not an all-topic Trending feed: since 2025-07-21, the chart includes Trending Music, Movies, and Gaming charts. It is not selected as the general feed. |
| Pagination / maximum | maxResults accepts 0–50; follow nextPageToken and stop at an explicit page budget. The API may return fewer than requested; pageInfo.totalResults is approximate and must not drive pagination. Each requested page is another API call. |
| Freshness / time | publishedAfter/publishedBefore filter video creation time in RFC3339; they do not measure view velocity. Search-index delay can make new uploads incomplete. |
| Authentication / quota | Public data requires an enabled Google Cloud project and API key; OAuth is needed for user-authorized/private data. The official quota guide documents a separate `search.list` bucket with a default allocation of 100 calls/day and one quota unit per call; each page request is another call. Actual project allocation must be checked in its console and may differ. No project or key was configured or checked. |
| D3B request | Fixed HTTPS endpoint, `part=snippet`, `type=video`, one canonical query from `demo-us-en` v1, `regionCode=US`, `relevanceLanguage=en`, one UTC `publishedAfter` cutoff per batch, `maxResults=50`, `order=relevance`, and provider `pageToken` when allowed by the execution cap. The credential boundary uses the `X-Goog-Api-Key` header; it is never included in the query URL. |
| D3B status | Offline-only implementation. `adapter_status=IMPLEMENTED_OFFLINE_VERIFIED`; `readiness=PENDING_APPROVAL`; `approval_status=PENDING_APPROVAL`; `configuration_status=NOT_CONFIGURED`; `live_verification_status=NOT_VERIFIED`; `default_enabled=false`; supported data mode is `SYNTHETIC` only. MockTransport is enforced and the adapter is absent from the live registry and Public Fetch allowlist. |
| Retry / failures | The implementation distinguishes invalid credentials, quota exhaustion, permission denial, rate limiting, transient 5xx, timeout, malformed payload, oversized response, and request-budget/deadline/cancellation stops. Retries are bounded by the shared HTTP policy and request budget. No live behavior or Retry-After guarantee was verified for this project. |
| Terms / storage | YouTube policy generally requires deleting or refreshing non-authorized API data within 30 days. Additional derived metrics and longer statistical storage require an audited analytics use case and explicit approval under the policy amendment effective 2026-06-01. The policy also restricts combining/aggregating YouTube API data with other sources. No cross-source score is designed or authorized here. |
| Provenance / UI | D3B preserves source/provider, plan/version/fingerprint, query fingerprint and scope, region/language filters, exact publication cutoff, fetched_at, requested page/rank, response status, validated video ID, canonical URL, and query evidence across deduplicated results. `result_rank` means ordinal position in this query response only. Do not display trend scores until legal/policy review and approval establish a permitted use. |

The adapter stores no raw provider response, makes no storage writes, and exposes no live factory. Its 14 canonical queries are curated demo seeds, not representative coverage of the US market. The default page cap remains one page per query; a returned cursor is marked `MORE_AVAILABLE`/`PAGE_CAP_REACHED`, and the batch is not called complete.

Official references, accessed 2026-11-11:

- [YouTube Search: list](https://developers.google.com/youtube/data/reference/rest/v3/search/list)
- [YouTube Videos: list](https://developers.google.com/youtube/data/reference/rest/v3/videos/list)
- [YouTube Data API quota calculator](https://developers.google.com/youtube/data/determine_quota_cost)
- [YouTube Data API revision history](https://developers.google.com/youtube/data/revision_history)
- [Google API key best practices](https://docs.cloud.google.com/docs/authentication/api-keys-best-practices)
- [Google API key use](https://docs.cloud.google.com/docs/authentication/api-keys-use)
- [YouTube API Services Developer Policies](https://developers.google.com/youtube/terms/developer-policies)
- [YouTube derived metrics and data storage policy](https://developers.google.com/youtube/terms/derived-metrics-policy)

## Google Search results through a provider candidate

| Capability | D0 finding |
|---|---|
| Provider / endpoint | Candidate provider is SerpApi, not an approved Google API. Its Google engine endpoint is GET https://serpapi.com/search.json with engine=google. Google's Custom Search JSON API is closed to new customers and existing customers are directed to transition by 2027-01-01; it is not the default integration proposal. |
| Data and metric | Query-specific organic result sample, rank, URL, title, and optional snippet. The total-results field is provider/search-engine reported and not search volume. Search results are neither an independent demand measure nor a representative Internet census. |
| Market / language / window | D4 uses gl=us, hl=en, google_domain=google.com. The candidate `tbs=qdr:w` is not sent: the reviewed provider reference describes `tbs` generally but does not establish the exact semantics for this use case. No seven-day freshness claim follows. Search location can change results; gl/hl do not establish author or reader residence. |
| Authentication / pagination | SerpApi requires an API key and its documented request examples place api_key in the query string; URLs and access logs therefore require strict redaction. Google result pagination uses start offsets (commonly 10 results per page); each successful SerpApi search is one search credit. |
| Maximum / freshness | Pagination is offset-based, not a stable cursor. Search engines can change ordering, result availability, and accessible depth; do not claim complete result coverage. Capture fetched_at and returned page/offset; no fixed freshness guarantee was verified. |
| Quota / cost | SerpApi pricing page, accessed 2026-11-11, listed a Free plan of 250 searches/month, Starter at USD 25/month for 1,000, and Developer at USD 75/month for 5,000. Only successful searches count; each additional result page is another search. These prices/limits can change and are not approved purchases. |
| Retry / failures | Provider documents 401 invalid key, 403 account permission, 429 hourly throughput or account credits exhausted, and 500/503 server errors. The reviewed docs do not promise Retry-After. Treat credit exhaustion as terminal for that run, not a transient retry. Bound 5xx retries and redact query-string keys. |
| Terms / retention | Provider FAQ states search result files are retained for 31 days; ZeroTrace is a separate mode whose availability depends on account/plan. Provider terms distinguish legal protection by plan and disclaim accuracy. Terms do not settle TrendScout's rights to store or redistribute returned engine content. Provider/legal/privacy review and an approved retention mode are required. |
| Current availability risk | SerpApi status page accessed 2026-11-11 showed ongoing Google Search reports involving irrelevant/fewer results and advanced-operator timeouts as of 2026-11-09. This is an additional candidate reliability risk, not proof that all queries fail. |
| Provenance / UI | Preserve provider ID, engine, query plan/version/fingerprint, gl/hl/time filter, start offset, fetched_at, result rank and URL, provider response status, and cache/live mode. Do not label result counts as Google searches or search volume. |

References, accessed 2026-11-11:

- [Google Custom Search JSON API transition notice](https://developers.google.com/custom-search/v1/overview)
- [SerpApi Google Search API](https://serpapi.com/search-api)
- [SerpApi pricing](https://serpapi.com/pricing)
- [SerpApi status and error codes](https://serpapi.com/api-status-and-error-codes)
- [SerpApi FAQ: query counts and result retention](https://serpapi.com/faq)
- [SerpApi terms and legal documents](https://serpapi.com/legal)
- [SerpApi status](https://status.serpapi.com/)

## Bing Search results through a provider candidate

| Capability | D0 finding |
|---|---|
| Provider / endpoint | Candidate provider is SerpApi, not Microsoft. SerpApi documents GET https://serpapi.com/search.json with engine=bing. Microsoft's traditional Bing Search APIs retired on 2025-08-11. Grounding with Bing Search / Foundry Web Search is an agent grounding product and does not provide the same structured organic SERP contract required here. |
| Data and metric | Query-specific organic result sample, rank, URL, title, and optional snippet. Counts and rank are not search volume, search frequency, or a direct measure of US audience. |
| Market / language / window | D4 uses mkt=en-US. The provider describes `filters`, including date-range examples, but D4 sends no time filter pending verified semantics and approved use. Market routing does not prove the population represented. |
| Authentication / pagination | SerpApi API key is required and examples place it in the query string; redact URLs. `first` defaults to 1 and selects an organic result offset; provider examples use variable next offsets. D4 extracts and validates the numeric offset from metadata, constructs a new fixed-host request, and never follows the returned link. |
| Maximum / freshness | Result depth and ordering are controlled by Bing/provider and can change. The reviewed API docs do not establish a complete result count or freshness SLA. Do not claim complete coverage. |
| Quota / cost | The same SerpApi account/search-credit plans apply; one successful page request uses one search credit. Current account quota is unknown because no account was created or checked. |
| Retry / failures | Apply the SerpApi HTTP handling described above: 401/403 fail closed, 429 is throughput/credits, 500/503 are bounded transient failures. No Retry-After promise was confirmed. |
| Terms / retention | SerpApi terms/retention and plan-dependent ZeroTrace requirements apply. Microsoft Grounding terms do not grant a structured SERP API or substitute for provider approval. Review redistribution, retention, privacy, and plan terms before use. |
| Provenance / UI | Preserve provider ID as serpapi, engine=bing, query plan/version/fingerprint, mkt, exact filter, first offset, fetched_at, cache/live state, source URL and response status. Label as a SerpApi-provided Bing search-result sample. |

References, accessed 2026-11-11:

- [Microsoft Bing Search APIs retirement](https://learn.microsoft.com/en-us/lifecycle/announcements/bing-search-api-retirement)
- [Microsoft Foundry Web Search](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/web-search)
- [SerpApi Bing Search API](https://serpapi.com/bing-search-api)
- [SerpApi status and error codes](https://serpapi.com/api-status-and-error-codes)
- [SerpApi pricing](https://serpapi.com/pricing)
- [SerpApi legal documents](https://serpapi.com/legal)

## Cross-source limits and approval register

- Query-result membership/rank, YouTube results, and Wikimedia pageviews are different metrics and populations. Do not sum or normalize them into one score in D1.
- An English query or US region parameter does not establish US authorship, viewer residence, readership, or demand.
- Google and Bing result pages are samples returned for specific queries at fetch time. The 14-query demo seed list in the query-plan proposal is deliberately curated and is not representative of the market.
- Wikimedia top-list absence is censored; missing, unavailable, rejected, partial, and explicit zero remain separate.
- YouTube derived metrics, long-term storage, aggregation, and cross-source display require a policy/compliance review and any applicable approval before use.
- SerpApi is a provider candidate only. Account creation, paid plans, credentials, and result storage/redistribution require owner approval.
- YouTube, Google and Bing have offline-only query adapters, but remain unconfigured, unapproved, unverified live, and absent from production registration/Public Fetch. No live source claim is authorized by this document.

## D4 offline boundary and catalog ownership

Google and Bing use a shared SerpApi client, typed query evidence, canonical URL deduplication, a process-local shared request/cost reservation pool, bounded HTTP policy and fixed server-owned `demo-us-en` v1. The adapter accepts only `httpx.MockTransport`; it does not read real credentials or register a live factory. It preserves cache status when provider metadata reports `Cached`, provider creation time when valid, and collection time independently. The local pool is not distributed admission or a production cost guarantee. Results retain query and page evidence, while rank is an ordinal result position, not demand. A page cap, invalid next offset, duplicate page, quota, error or rejected item is surfaced as incomplete/partial.

Both capabilities remain `readiness=PENDING_APPROVAL`, `approval_status=PENDING_APPROVAL`, `configuration_status=NOT_CONFIGURED`, `live_verification_status=NOT_VERIFIED`, `default_enabled=false`, and `supported_data_modes=[SYNTHETIC]`. Their `adapter_status=IMPLEMENTED_OFFLINE_VERIFIED` reflects synthetic tests only. The runtime catalog contains four supported contracts; the separate [strategic US inventory](US_PLATFORM_SOURCE_INVENTORY.md) describes future candidates and is not included in the query-plan fingerprint or Public Fetch allowlist. Live integration requires the [D5 four-source blueprint](D5_FOUR_SOURCE_LIVE_INTEGRATION_BLUEPRINT.md), a suitable distributed execution environment, approvals and individual live verification.
