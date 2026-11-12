# D5/D6 four-source approval register

Metadata only. No provider secrets or account credentials belong in this register. Status reviewed 2026-11-12; the absence of evidence is not approval. The owner confirms current GitHub/Vercel/Google Cloud/Firebase and development resources are personal and the company has provided no accounts or infrastructure. Those personal resources are excluded from production approval/evidence; DEMO_ACCEPTANCE uses local synthetic fixtures and needs no provider account.

| Provider / source | Organization account owner | Use and derived metrics | Quota, cost and retention | Credential | Live verification |
|---|---|---|---|---|---|
| Wikimedia Pageviews | No organization account/key required for this AQS endpoint; organization operational owner remains unassigned | **Personal/local D6.2 one-request approval only:** retrieve and validate the US daily top-per-country response and return normalized source-level evidence. No discovery extraction or derived analytics; organization/product use remains pending. | **D6.2 only:** at most one Wikimedia GET/attempt; no retry/fallback/pagination; zero provider fee under the owner-authorized budget. Normalized checkpoint data in memory for at most 120 seconds; raw response body is not persisted; quota/attempt receipts remain in the canonical local ledger. No physical-erasure claim. Organization retention remains pending. | No API key; default User-Agent includes the public project website as contact, accepted contact type under current AQS policy | **ATTEMPTED_FAILED** 2026-11-12: one GET, HTTP 200 response headers, gateway `UPSTREAM_ERROR`, no validated records; request budget consumed |
| Google via SerpApi | Unassigned | Pending | Pending; shared SerpApi pool | NOT_CONFIGURED | NOT_VERIFIED |
| Bing via SerpApi | Unassigned | Pending | Pending; shared SerpApi pool | NOT_CONFIGURED | NOT_VERIFIED |
| YouTube Data API v3 | Unassigned | Pending | Pending | NOT_CONFIGURED | NOT_VERIFIED |

Before a D6 activation add approver, approval time, expiry, usage and processing rights, deletion and attribution terms, budget, account/credential metadata references, evidence and rollback owner. Credential values must remain in an approved secret manager. The [D6 plan](D6_PROVIDER_ACCOUNT_CREDENTIAL_AND_LIVE_ACTIVATION_PLAN.md) defines the workflow. No approval is implied by D5 fixture tests or code readiness.

## D6.2 scoped personal/local approval record — one request only

This record documents the project owner's explicit D6.2 instruction dated 2026-11-12. It is limited to the local, loopback-only D6.2 verification; it does not approve organization ownership, company production, other sources, public Fetch, derived discovery analytics, or any future request. The allowed use is one request to retrieve, validate, and expose normalized source-level evidence from `wikimedia_pageviews_top_per_country` using the fixed US / all-access / daily top-per-country endpoint. No API key or paid service is authorized or required.

| Field | D6.2 decision |
| --- | --- |
| Approval ID / approver | `D6.2-WIKIMEDIA-ONE-REQUEST-2026-11-12`; project owner, explicitly authorized by the owner-provided D6.2 task on 2026-11-12. |
| Scope and expiry | One Wikimedia GET and one attempt at most, for this D6.2 execution only; authorization expires after the attempt or D6.2 completion, whichever comes first. No retry, second job, date fallback, pagination, or extension to other sources. |
| Usage / processing rights | Fetch, validate, and report Wikimedia source-level evidence only. No keyword extraction or derived metrics without a separate processing grant. Wikimedia AQS states its data is CC0 1.0; retain source attribution/provenance in TrendScout regardless. [AQS access policy](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/access-policy.html). |
| Attribution | Preserve Wikimedia source ID, observation date, country/access scope, source URL, metric name, and provenance. CC0 1.0 does not require attribution, but TrendScout retains source attribution for explainability. |
| Quota / cost | Existing local policy only: one Wikimedia request/attempt, one start per 24 hours, durable receipt, zero provider fee authorized; no policy or ledger reset. Internal cost units are attempt counters, not monetary amounts. |
| Retention / deletion | Normalized checkpoint only, volatile in-process store, maximum retention 120 seconds under the existing local composition. Do not persist raw provider bodies. Keep durable quota and attempt receipts. No claim of physical erasure from WAL, backups, swap, crash dumps, or filesystem copies. |
| User-Agent / credentials | Use configured User-Agent; the code default includes the public TrendScout project URL, a website contact type allowed by the [Wikimedia User-Agent policy](https://foundation.wikimedia.org/wiki/Policy%3AWikimedia_Foundation_User-Agent_Policy). No API key; no unrelated provider credential may be resolved. |
| Security / egress / telemetry | Existing loopback-only `LocalLiveGuard`, fixed Wikimedia HTTPS host/path, certificate verification, `trust_env=False`, redirects disabled, one worker, and bounded response size. Report only sanitized status/counts; no raw provider body or credential values. |
| Evidence / rollback owner | D6.2 focused native Windows tests, the one-request activation output, and this report. Project owner stops the flow after the single attempt; failed or uncertain attempts remain reserved and are not retried. |

This scoped record supersedes the Wikimedia row's generic pending status only for this one personal/local D6.2 request. The organization operational owner and all company/production approvals remain pending. The single authorized live attempt has been consumed; no retry or second request is permitted under this record.
