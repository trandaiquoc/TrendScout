# TrendScout D3A — Public Fetch and Admission Design

- **Status:** D3A implementation design, constrained to offline synthetic execution.
- **Product correction:** The public TrendScout website has no login, logout, registration, or user account. This supersedes the D0 authenticated-session proposal for this product flow.
- **Live status:** `LIVE_FETCH_DISABLED_PENDING_APPROVAL`.
- **Production status:** Public live admission, distributed abuse controls, and paid-provider budgets are not approved or verified.

## Threat model

### Assets and trust boundaries

- A fetch can consume server CPU, storage writes, provider quota, and money.
- Browser input, request headers, IP addresses, fetch IDs, and retry behavior are untrusted.
- The server owns source selection, query plan/version, request/page budget, execution mode, and storage choice.
- The legacy `POST /api/runs` remains a separate operator-only contract protected by its Bearer token.
- Test-only fixture adapters and fake storage are trusted only when explicitly injected by the local test composition.

### Abuse considered

- Direct HTTP clients bypass the dashboard and submit concurrent or repeated POSTs.
- Bots rotate IPs or share NAT addresses; `Origin` and `Host` can be forged outside browsers.
- Replayed idempotency keys, changed payloads, slow polling, oversized bodies, and retries after uncertain responses.
- A client attempts to select a paid source, change the plan/budget, supply a query, or turn synthetic execution into a live provider call.
- Multiple workers/processes do not share in-memory rate-limit or idempotency state.
- Read-only hosting or a runtime without injected fake dependencies is misconfigured to permit writes.

### Decisions and limits

1. `/api/v1/capabilities` is anonymous and returns only non-secret, server-derived policy and source metadata.
2. `/api/v1/fetches` is a separate public endpoint. Its feature flag defaults off. An enabled request still requires the local/test profile, synthetic mode, a non-read-only app, explicitly injected offline-only adapter and fake storage, and no cloud project configuration. Production and Vercel force the endpoint off.
3. The request body is exactly `{}`. The server selects the fixed `demo-us-en` plan and one-source offline allowlist; no arbitrary URL, query, source, budget, tenant, role, or run ID is accepted.
4. Origin/Host and Fetch Metadata are checked as browser abuse signals. They are not authentication and do not stop direct clients.
5. The default local synthetic path uses a process-local admission window, one-active-run lock, cooldown, request-size limit, bounded polling, deadline, and in-memory idempotency registry. A separate injected `DistributedPublicFetch` path is available only in local/test synthetic composition with all four server-declared synthetic handlers and an enabled `AtomicStateStore` policy; it uses atomic admission, idempotency, quota and poll budgets with a queue worker. The integration test uses a local SQLite transaction harness only. Neither path proves production or multi-host backing guarantees.
6. Since anonymous limits can be evaded by direct clients, bots and multiple IPs, both compositions remain limited to local/test synthetic mode. Production and Vercel remain read-only/disabled; no distributed backend, bot defense or durable quota service has been verified for production. Provider live activation requires separate D6 approvals and verified infrastructure.
7. Fetch IDs are high-entropy opaque identifiers. Status reads are read-only and reveal only the record associated with that ID; they never start work or call providers.
8. In-memory idempotency and fetch status expire after a bounded TTL and are lost on restart. The runtime may not claim duplicate suppression across restarts or independent hosts. Multiple IPs and direct API clients cannot bypass the one process's global synthetic request window, but they can exhaust it; process restart clears it.

## API contract

### `GET /api/v1/capabilities`

Anonymous, read-only, `Cache-Control: no-store`. It reports contract version, mode, read-only state, whether public fetch is supported/enabled, a stable disabled reason, fixed plan identity/version/fingerprint, fixed request budget, and the four source descriptors. Each descriptor distinguishes adapter implementation, approval, configuration, live verification, source limitations, and whether it is allowed in the current synthetic operation. It never returns environment values, credentials, or internal endpoint authorization data.

### `POST /api/v1/fetches`

- Requires `Idempotency-Key` (16–128 safe ASCII characters), same-origin browser checks where an Origin is present, and the server-side public-fetch gate.
- Requires JSON `{}` only, with a 1 KiB body limit.
- Returns `202` with opaque `fetch_id`, `QUEUED`, `SYNTHETIC`, the fixed source set, creation time and status URL.
- Same key returns the existing accepted job. The distributed facade fingerprints the immutable server query plan and does not accept client query/source/budget fields.
- In the default composition, new work is limited by a global per-process window, one active run and `HostRunLock` cooldown. In the injected distributed local/test composition, atomic admission enforces server policy rate, cooldown, idempotency, request/cost quota and bounded polling.
- Live mode and non-injected source/storage dependencies are rejected before `RunService.start()`.

### `GET /api/v1/fetches/{fetch_id}`

Validates the opaque ID, applies bounded read polling, and returns a safe status projection. In the default composition it performs storage reads only; in injected distributed local/test mode the POST schedules a bounded worker and GET only reads retained status/results. Internal errors are mapped to safe codes and never expose tracebacks or raw exception text. Default fetch receipts are in-memory; distributed job/idempotency/poll state follows the injected atomic store's retention policy.

## Offline execution boundary

The supported D3A test path injects a marked offline fixture adapter and `FakeStorage` into `create_app`. The D5 facade path injects an `AtomicStateStore`, worker and four explicitly `SYNTHETIC` handlers; it does not require Firebase or the legacy run storage. Both require an explicit test/local synthetic profile and feature flag and are covered by strict network-isolation readiness tests. These local/test seams do not permit production composition, and no live gateway or provider activation is implied.

## Frontend behavior

The dashboard has no login/logout/account UI and never asks for credentials. It loads public capabilities, stays disabled if policy is unavailable or false, and enables Fetch only when the server advertises the injected offline synthetic operation. A one-click action generates an idempotency key in memory, preserves it after an uncertain response, sends one POST without automatic retry, and polls a returned opaque fetch ID with a finite bound. Theme, locale, the ten D2 views, and existing keyword workflows remain intact.

## Future live gate

Before any public live Fetch, require explicit product/security approval and implement/verify a shared distributed limiter, durable idempotency and quota ledger, multi-process and multi-host concurrency control, bot/abuse protections resilient to IP rotation, provider-specific request/cost caps, monitoring/alerting, and provider/legal approval. Origin checks, one-process counters, UI disabling, and an opaque ID are not sufficient controls for public live collection.
