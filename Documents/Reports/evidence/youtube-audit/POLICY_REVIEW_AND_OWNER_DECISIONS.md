# YouTube audit: policy review and owner decisions

Reviewed 16 November 2026. Status: **DRAFT — NOT READY TO SUBMIT**. The local source now contains a YouTube Data API v3 path and user-facing Source Center/evidence components, but YouTube is disabled by default and no live provider request has been made. This review does not establish that the client is compliant or that YouTube has approved any derived metrics.

## What is implemented

- The anonymous dashboard links to draft Privacy Policy and Terms pages. Source Center exposes YouTube's server configuration, approval state, separate local budgets and refresh schedule. The Fetch result can display query-scoped video evidence with an official YouTube logo. Browser local storage holds theme, language and selected source preferences.
- The guarded LIVE runtime, user-triggered schedule gate, SQLite cache and retention ceiling are implemented locally. YouTube defaults to disabled; zero real YouTube API requests have been made. The cache is configured for at most seven days, but complete deletion of copies in logs, crash dumps, swap and backups is not verified.
- A required privacy-policy acceptance flow is still absent. Before YouTube API features are enabled for users, approve the final text and implement and verify that flow for the anonymous dashboard.

## Owner decisions required

The owner supplied identity, public contact email, proposed date, HTTPS URLs, hosting description and intended use in `.env.youtube-audit-owner` (Git-ignored). The owner confirmed review of the policies and selected an acknowledgment target of two business days and deletion within seven calendar days where applicable. An earlier approved design called for API records to remain in process memory only for 120 seconds and not be persisted to SQLite. The D6 request now calls for SQLite cache-first delivery, and the implementation uses a seven-day cache; this is a material retention change that needs owner review before enabling the source. The request-handling targets remain unimplemented, the optional governing-law field is empty, and the actual effective date and project log retention remain unresolved. Do not put API keys in the file or imply that a form submission or API key grants any approval.

The owner-provided public Privacy URL returned HTTP 200 on 12 November 2026 and served the earlier draft with placeholders. The connected Vercel project identifies the provided public domain and the project drain listing returned no drains; the active plan, any other log destinations and actual runtime log settings remain unverified. Vercel's [runtime log documentation](https://vercel.com/docs/logs/runtime) describes plan-dependent retention. The generic in-memory Fetch registry uses a 3,600-second TTL; the synthetic distributed demo and proposed local smoke admission each configure 600-second receipts. Cleanup and other quota-state lifetimes must be assessed separately.

## Retention and deletion — CURRENT LOCAL IMPLEMENTATION / OWNER REVIEW REQUIRED

The D6 implementation stores normalized YouTube video metadata in the local SQLite source cache for no more than seven days, serves it with original observation timestamps and labels cached delivery, and removes an expired or rejected YouTube cache entry during runtime composition startup. This is more restrictive than the provider's current general maximum of 30 calendar days for applicable non-authorized API data. It supersedes the earlier volatile-only design for this local implementation but has not yet been accepted as a published privacy commitment. The cache does not prove erasure of OS swap, crash dumps, host logs, exception traces, transport buffers or backups. Verify every persistent and transient copy, including failure and restart paths, before enabling live use.

The owner chose an acknowledgment target of two business days and deletion as soon as possible and within **7 calendar days** when YouTube's user-data rule applies. The current YouTube flow uses an API key and public video metadata rather than a user's YouTube OAuth data, but the operator still needs an applicable request-handling policy. No user-facing request intake, identity verification, suppression or end-to-end deletion proof exists. The general 30-day cache limit does not replace the applicable seven-day deletion deadline for related user data. Deleting a local copy does not delete data held by YouTube.

### Proposed manual handling procedure — not yet operationally verified

1. The operator checks the published contact inbox, records receipt time and the applicable deadline, and acknowledges within the chosen target of two business days. Never request a password or API key.
2. Verify control of the reply address and request only the minimum information required to identify the request and the relevant data. Determine whether the client actually holds related data and whether YouTube's user-data rule applies.
3. Suspend any relevant collection that could recreate data while handling the request. Inventory application memory, databases, logs, backups, exports and third-party processors. `purge_source` is a backend primitive for one process only; it is not presently exposed as an operator command and cannot erase those other locations.
4. Remove applicable data from each verified location, keep non-content quota receipts when needed, and verify removal. Escalate any inaccessible processor or backup rather than recording completion without evidence.
5. Confirm the result to the requester, explain that deletion from TrendScout does not remove data from YouTube, and keep only minimal necessary operational records under a separately approved retention rule. The owner must implement and verify this procedure, inbox monitoring, deadlines and each storage deletion path before treating the targets as commitments.

## Findings requiring resolution

1. Privacy policy must be prominently accessible and require user agreement before access to the API client's features under YouTube Developer Policies III.A.2. Current link-only UI has no demonstrated acceptance step.
2. Policy must accurately describe actual data, uses, sharing, devices and third parties; hosting logs and any third-party practices are not established by the local source code. Do not claim them without checking the actual deployment.
3. Terms link to YouTube's terms and describe planned use, but final wording, factual scope and user agreement need owner review before activation.
4. Any YouTube search-result presentation must satisfy YouTube's search-result and attribution rules. The local implementation now adds the official YouTube logo linked to YouTube beside the Source Center and Fetch evidence components. This local asset/UI change and synthetic fixtures do not prove provider-live behavior or production compliance.
5. Keyword extraction, aggregation or derived metrics from YouTube data remain gated pending a case-specific policy review and any required written permission. Approval has not been granted.
6. Recreate public HTTPS screenshots and the Terms PDF only after the owner confirms the policy facts, reviews the pages and separately approves publication. Check actual deployment and GitHub/Vercel auto-deploy settings before any push.

Sources: [YouTube Developer Policies III.A and III.E](https://developers.google.com/youtube/terms/developer-policies), [policy guide](https://developers.google.com/youtube/terms/developer-policies-guide), [branding guidelines](https://developers.google.com/youtube/terms/branding-guidelines), [additional derived metrics policies](https://developers.google.com/youtube/terms/derived-metrics-policy), [YouTube API Services Terms](https://developers.google.com/youtube/terms/api-services-terms-of-service).
