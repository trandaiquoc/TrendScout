# D6 — Organization-Owned Provider Accounts, Credentials and Live Activation

**Status: blueprint; no accounts, secrets, live activation or production changes authorized.** Roadmap decision: D5 builds the live-capable, distributed foundation and verifies it offline; D6 owns provider onboarding, credential management, a separate secure admin console and actual four-source live activation; D7 performs final live E2E, security, data quality, performance and UAT. There is no D3C. D5 acceptance does not require provider live verification.

## Identity and boundaries

| Identity | Responsibility | Boundary |
|---|---|---|
| Provider account owner | Organization-controlled provider account, billing, provider terms and ownership recovery | Assign an accountable organization owner and backup before activation; developer personal accounts are not a long-term operating identity. |
| Provider API credential | Backend credential with minimum provider scopes and restrictions | Store only in an approved secret manager; never expose a value to browser, logs, inventory or source control. |
| Runtime service identity | Backend workload identity for Storage Port, secrets and worker operations | Dedicated least-privilege identity, distinct from provider account and administrator. |
| Organization administrator | Manage approvals, source status, quota and rotation | Separately authenticated and authorized; deny by default, least privilege and audited changes. |
| Public anonymous user | View dashboard and request a server-controlled Fetch when allowed | No credentials, login, admin writes, arbitrary source/query/URL, quota changes or paid-provider activation. |

The separate admin management interface must never add Login/Logout to the public dashboard. There is no public API key entry form. The D5 `CredentialProvider` port takes a server-owned reference; deployment composition supplies an implementation. Source adapters must not read environment secrets directly. Wikimedia may require no API credential; its approved identification and policy still apply. The existing legacy Bearer token is an independent legacy authorization mechanism, not a provider credential or public login.

## Metadata-only registers

Before activation, an authorized owner completes an account ownership register containing provider, organization owner and backup, legal entity, account reference (non-secret), billing owner, plan, account recovery, access review date, approval and risk. A credential inventory contains provider/source, secret reference, owner, purpose, scope, environment, restrictions, creation/rotation/revocation dates, status and audit reference. Do not record values, raw provider URLs with secret parameters, private keys or account passwords. Empty registers mean onboarding is pending, not that ownership or access has been verified.

| Source | Ownership / credential | Approval and live evidence |
|---|---|---|
| Wikimedia Pageviews | Organization operational owner; user-agent/contact policy, no assumed key | Pending: use, attribution, retention, limits and controlled live verification. |
| Google SERP via SerpApi | Organization SerpApi account; secret reference only | Pending: plan, cost, data rights, Google scope, quota and live evidence. |
| Bing SERP via SerpApi | Shared organization SerpApi account and quota pool | Pending: Bing-specific rights, quota and live evidence; sharing does not imply approval. |
| YouTube Data API v3 | Organization-controlled Google project and restricted credential | Pending: API access, quota, policy, retention and live evidence. |

Existing personal Firebase ownership, if any, is **unverified**. Inventory ownership before any production use; if personal ownership exists, record it as an ownership and continuity risk, then propose a separately approved transfer. Keep current Firebase/Firestore and Storage Port. No database migration in D5 or D6 without a separate decision; no new personal Firebase resources, cloud project, billing or credentials are created by this plan.

## Secret lifecycle and operations contract

1. An administrator proposes a provider/use-case approval with terms, source scope, processing/derived metrics rights, retention/deletion, attribution, quota, budget, owner and expiry. A separate authorized approver records the decision. Default is disabled; approval expires or revokes fail closed.
2. An authorized operator provisions the secret through an approved secret manager using organization ownership. The application receives only a reference and workload authorization. Restrict credential scopes, API, egress and environment where supported; do not assume restrictions substitute for budgets.
3. Support per-provider enable/disable, cost and quota ceilings, connection status, bounded health checks, credential expiry alerts and audit events. Health checks consume provider quota and require explicit approval; status must distinguish unconfigured, disabled, approved, unhealthy and verified. Do not display secret material.
4. Rotate by creating a replacement in the secret manager, verifying it under an approved budget, atomically switching the reference/version, observing health and revoking the old credential. On compromise disable admission/source, revoke, audit and investigate. Never rotate keys to bypass quotas.
5. Admin API enforces server-side authentication and authorization for every read/write, CSRF protection where cookies are used, secure session handling, rate limits, change history and audit events containing actor, action, target, time, decision and correlation ID. Protect audit integrity and retention; redact secrets and sensitive provider responses. Concrete identity provider and threat review remain D6 decisions.

## Deployment and live verification sequence

Prerequisites: all revised D5 gates pass, including real distributed admission, quota/cost accounting, durable queue/cache, failure recovery and independent security review. The required runtime, owner, budget, provider approval, retention controls, operational monitoring, rollback and emergency contacts must be approved before live requests. A process-local pool, IP or Origin check alone cannot protect anonymous APIs against direct clients, bots, multiple IPs or multiple processes.

For each source independently: approve organization ownership, use and budget; provision credential where required; verify configuration without exposing the credential; enable only a controlled backend test and run a bounded live smoke including endpoint/schema, pagination, timeouts, rate/quota, source scope, provenance, error/partial behavior and deletion requirements; record dated evidence and actual cost. Mark `LIVE_VERIFIED` only for the source with sufficient evidence. Google and Bing share a SerpApi budget but are verified independently. Public live Fetch remains disabled until its separate anti-abuse and cost approval. D7 performs final system-wide live tests and UAT.

Rollback: close admission and provider flags, stop issuing leases and requests, preserve truthful partial/stale status, revoke affected credentials if necessary, reconcile quotas and queue, audit and follow retention/deletion obligations. A failed live verification never turns fixture data into live evidence.

## Handoff status

`D6_PROVIDER_ONBOARDING_PENDING`; `LIVE_ACTIVATION_NOT_YET_ACCEPTED`. Current D5 work has not demonstrated all revised gates. No secret manager, admin console, production onboarding or live verification is claimed by this blueprint.
