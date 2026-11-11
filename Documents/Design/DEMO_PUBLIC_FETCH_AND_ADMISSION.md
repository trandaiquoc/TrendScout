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
5. A process-local global admission window (three new fetches per hour), one-active-run lock, cooldown, request-size limit, bounded polling, deadline, and idempotency registry protect the isolated synthetic runtime. A shared local file lock admits one app process at a time; a second process fails closed. These controls do not provide multi-host/distributed guarantees and reset on restart.
6. Because anonymous rate limits can be evaded with multiple IPs and the current code has no distributed limiter or durable quota ledger, live public Fetch remains hard-disabled. Enabling live sources requires a separate approval and verified distributed admission, quota, cost, and abuse controls.
7. Fetch IDs are high-entropy opaque identifiers. Status reads are read-only and reveal only the record associated with that ID; they never start work or call providers.
8. In-memory idempotency and fetch status expire after a bounded TTL and are lost on restart. The runtime may not claim duplicate suppression across restarts or independent hosts. Multiple IPs and direct API clients cannot bypass the one process's global synthetic request window, but they can exhaust it; process restart clears it.

## API contract

### `GET /api/v1/capabilities`

Anonymous, read-only, `Cache-Control: no-store`. It reports contract version, mode, read-only state, whether public fetch is supported/enabled, a stable disabled reason, fixed plan identity/version/fingerprint, fixed request budget, and the four source descriptors. Each descriptor distinguishes adapter implementation, approval, configuration, live verification, source limitations, and whether it is allowed in the current synthetic operation. It never returns environment values, credentials, or internal endpoint authorization data.

### `POST /api/v1/fetches`

- Requires `Idempotency-Key` (16–128 safe ASCII characters), same-origin browser checks where an Origin is present, and the server-side public-fetch gate.
- Requires JSON `{}` only, with a 1 KiB body limit.
- Returns `202` with opaque `fetch_id`, `QUEUED`, `SYNTHETIC`, the fixed source and plan, creation time, and status URL.
- Same key and fingerprint returns the existing accepted fetch. Same key with a different fingerprint returns `409`. Concurrent duplicates in the supported single-process mode create at most one run.
- New work is limited by a global per-process window, one active run, and the existing `HostRunLock` cooldown. Rate-limit rejections return `429` with a bounded retry hint.
- Live mode and non-injected source/storage dependencies are rejected before `RunService.start()`.

### `GET /api/v1/fetches/{fetch_id}`

Validates the opaque ID, applies bounded read polling, and returns a safe status projection. It performs storage reads only; no provider or pipeline work is triggered. Internal run errors are mapped to safe codes and never expose tracebacks or raw exception text. The record and idempotency entry live for a bounded in-memory TTL.

## Offline execution boundary

The supported D3A test path injects a marked offline fixture adapter and `FakeStorage` into `create_app`, sets an explicit test/local synthetic profile and feature flag, and is run inside the repository's strict network-isolation harness. The default source registry still constructs the live Wikimedia adapter when used by the legacy path; the public synthetic path refuses that default and requires the offline-only injection marker. The path never creates Firestore storage or activates YouTube, Google, or Bing.

## Frontend behavior

The dashboard has no login/logout/account UI and never asks for credentials. It loads public capabilities, stays disabled if policy is unavailable or false, and enables Fetch only when the server advertises the injected offline synthetic operation. A one-click action generates an idempotency key in memory, preserves it after an uncertain response, sends one POST without automatic retry, and polls a returned opaque fetch ID with a finite bound. Theme, locale, the ten D2 views, and existing keyword workflows remain intact.

## Future live gate

Before any public live Fetch, require explicit product/security approval and implement/verify a shared distributed limiter, durable idempotency and quota ledger, multi-process and multi-host concurrency control, bot/abuse protections resilient to IP rotation, provider-specific request/cost caps, monitoring/alerting, and provider/legal approval. Origin checks, one-process counters, UI disabling, and an opaque ID are not sufficient controls for public live collection.
