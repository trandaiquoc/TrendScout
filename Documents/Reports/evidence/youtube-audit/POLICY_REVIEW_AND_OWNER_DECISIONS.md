# YouTube audit: policy review and owner decisions

Reviewed 12 November 2026. Status: **DRAFT — NOT READY TO SUBMIT**. The current pages describe the local synthetic demo and a proposed YouTube integration; they do not establish that the client is compliant or that YouTube has approved any derived metrics.

## What is implemented

- The local synthetic demo has an anonymous dashboard with links to the draft Privacy Policy and Terms. Both contain placeholders; YouTube live is not enabled there. Browser local storage holds theme and language preferences.
- The guarded live-capable adapter and a volatile checkpoint component exist, but a complete live local runtime, deletion-request handling and retention verification for real YouTube data are not complete. The volatile component removes expired checkpoint references on a periodic sweep and on transactions; it is not proof that all copies in logs, responses, swap, dumps or backups are removed.
- The dashboard currently has no demonstrated acceptance flow for the privacy policy. Before YouTube API features are enabled, implement and verify an appropriate acceptance flow and review how it interacts with the anonymous dashboard.

## Owner decisions required

The owner supplied identity, public contact email, proposed date, HTTPS URLs, hosting description and intended use in `.env.youtube-audit-owner` (Git-ignored). The owner confirmed review of the policies, selected an acknowledgment target of two business days and deletion within seven calendar days where applicable, and approved the proposed temporary-memory direction. These remain targets until implemented and verified. The optional governing-law field is empty; the actual effective date, request procedure and project log retention remain unresolved. Do not put API keys in the file or imply that a form submission or API key grants any approval.

The owner-provided public Privacy URL returned HTTP 200 on 12 November 2026 and served the earlier draft with placeholders. The connected Vercel project identifies the provided public domain and the project drain listing returned no drains; the active plan, any other log destinations and actual runtime log settings remain unverified. Vercel's [runtime log documentation](https://vercel.com/docs/logs/runtime) describes plan-dependent retention. The generic in-memory Fetch registry uses a 3,600-second TTL; the synthetic distributed demo and proposed local smoke admission each configure 600-second receipts. Cleanup and other quota-state lifetimes must be assessed separately.

## Retention and deletion — TARGETS APPROVED; IMPLEMENTATION INCOMPLETE

For a future personal local demo, propose a server-side cap of 120 seconds for YouTube API response data in process memory, then remove application references; clear references on normal shutdown; do not persist response bodies, normalized records or provider content to SQLite, logs, backups or browser storage. Persist only the minimum non-content quota receipts needed to prevent repeated requests, subject to an explicitly decided retention period. This is a design target, not a guarantee of secure erasure: OS swap, crash dumps, host logs, exception traces, transport buffers and other copies need investigation and controls. Verify all paths, including failures and restarts, before publishing the promise or enabling live use.

The owner chose an acknowledgment target of two business days and deletion as soon as possible and within **7 calendar days** when YouTube's requirement applies. A local `purge_source` primitive now removes a source's volatile checkpoint references and keeps quota receipts; an offline test covers the primitive. It does not process requests, identify users, prevent refetch, erase other copies or provide proof of deletion. Verify identity without requesting YouTube passwords or excessive personal information; document and implement request handling, confirmation and records. For non-authorized API data, YouTube generally requires limited retention no longer than 30 calendar days before deletion or refresh; this does not replace the applicable seven-day request deadline. Revocation, OAuth and other data categories can carry different rules. Deleting a local copy does not delete data held by YouTube.

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
4. Any YouTube search-result presentation must satisfy YouTube's search-result and attribution rules. Current synthetic screenshots do not show an active YouTube implementation or prove appropriate logo placement.
5. Keyword extraction, aggregation or derived metrics from YouTube data remain gated pending a case-specific policy review and any required written permission. Approval has not been granted.
6. Recreate public HTTPS screenshots and the Terms PDF only after the owner confirms the policy facts, reviews the pages and separately approves publication. Check actual deployment and GitHub/Vercel auto-deploy settings before any push.

Sources: [YouTube Developer Policies III.A and III.E](https://developers.google.com/youtube/terms/developer-policies), [policy guide](https://developers.google.com/youtube/terms/developer-policies-guide), [branding guidelines](https://developers.google.com/youtube/terms/branding-guidelines), [additional derived metrics policies](https://developers.google.com/youtube/terms/derived-metrics-policy), [YouTube API Services Terms](https://developers.google.com/youtube/terms/api-services-terms-of-service).
