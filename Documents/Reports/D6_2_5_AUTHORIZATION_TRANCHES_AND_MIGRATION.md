# D6.2.5 — Same-ledger authorization tranches and legacy-preserving migration

**Date:** 2026-11-13
**Status:** Offline implementation and verification complete for this scope. No LIVE authorization, migration, or request is enabled.
**Environment:** Native Linux, Python 3.12.3.
**Initial Git baseline:** `HEAD` and fetched `origin/main` both `60616148fcc45eece5f11675c781101fca268fa3`.
**Provider requests / LIVE transports / credentials resolved:** 0 / 0 / 0.

## 1. Scope and boundary

D6.2.5 implements a typed authorization contract, an isolated schema-v1 to schema-v2 migration, and atomic reservation accounting. The implementation lives in `Backend/app/storage/d6_authorization_tranches.py`. It has no provider, credential, transport, launcher, API route, or worker-dispatch dependency. It is restricted to stores whose data mode is `SYNTHETIC`; a LIVE-profile SQLite store is rejected at construction. Migration and reservation tests use temporary SQLite files only.

The ordinary `PublicAdmission`, `LocalSmokeStore`, `LocalVolatileCheckpoints`, `GuardedAttemptRunner`, local LIVE composition, distributed worker, gateways, policy digest, and retired launcher were not changed. The existing schema-v1 admission reader rejects schema v2, so this envelope cannot silently become a live admission state. No code path connects the new budget to outbound dispatch.

The D6.2.4 launcher remains permanently blocked for `D6.2-WIKIMEDIA-ONE-REQUEST-2026-11-12`. No new approval exists. Both proposed future requests remain inactive. The design ceiling is two additional requests, with one historical consumed request and a lifetime maximum of three; the ceiling is not usable quota.

## 2. Legacy format and migration

The migration uses the existing state identity `d6-smoke` and the same atomic store transaction key. Schema v2 wraps the complete v1 state in `legacy_state`, including its original policy digest, jobs, receipt maps, source counts, starts, quota buckets, inflight map, and any other preserved provenance. The migration stores a SHA-256 digest of the canonical v1 JSON as source provenance.

Before conversion, validation requires schema 1, the known v1 root key set (with only `last_observed_at` optional), a boolean operational flag, a 64-character policy digest, structurally valid jobs and receipts, source-to-pool consistency, a single consumed Wikimedia receipt, a matching sticky source count, one timestamped job start, and one Wikimedia daily and monthly request/cost unit matching the reservation day. Additional source receipts are validated and preserved. Receipt source, pool and absolute UTC reservation time are required; any additional receipt properties are preserved as opaque legacy provenance. No missing historical receipt is manufactured.

The v2 envelope stores a `legacy_tranche` with the retired approval ID, historical job and receipt IDs, full original receipt and its SHA-256, source/pool/time, original policy digest, consumed request and cost counts, and `CLOSED_CONSUMED` status. Its capacity record has `maximum_additional_requests=2`, `maximum_lifetime_requests=3`, `authorized_capacity_requests=0`, and `INACTIVE` status. `authorizations`, `reservations`, `settlements`, and new job starts begin empty. The operational switch is false in v2 regardless of the preserved v1 value.

`LocalSQLiteAtomicStateStore.transact()` holds `BEGIN IMMEDIATE` while validating and replacing the JSON state. Exceptions roll the transaction back. An empty ledger, unsupported version, malformed receipt, missing digest, inconsistent counters, wrong host, or failed invariant leaves the SQLite document unchanged. A second migration validates the existing v2 document and returns `ALREADY_MIGRATED` without changing it. The helper cannot access the canonical LIVE store because it accepts synthetic/test data mode only; actual canonical migration remains explicitly pending and was not attempted.

## 3. Authorization and budget contract

`AuthorizationTranche` requires all named fields: authorization and approval identities, source, approver, absolute UTC start and expiry, policy version/digest, query-plan fingerprint, endpoint/country/access scope, observation date, request/attempt/job-start/cost-unit caps, monetary fee and currency, processing and derived-metric permission, retention and raw-body policies, security controls, rollback owner, and status. The dataclass validates non-empty values, identifier and digest shapes, source, UTC ordering, ISO observation date, nonnegative finite fee, currency format, and caps no greater than two. It rejects reuse of the retired approval ID.

The public append method accepts only `INACTIVE` authorizations and rejects duplicate IDs. There is no activation method. Active test records are inserted directly into disposable fixture databases to exercise the accounting rules; those records are fake and are not present in any canonical ledger. Missing or pending owner decisions have not been filled in for a real grant.

Reservation checks execute within the same `d6-smoke` atomic transaction: one active authorization; approval identity; source; UTC validity; policy version and digest; query-plan, endpoint, country, access and observation-date scope; valid attempt/job/idempotency identities; request/attempt cap; cost-unit and monetary-fee cap; job-start cap; lifetime total including the historical one; duplicate/replay rules; and single in-flight reservation. The technical ceiling is clamped to two requests even if a malformed fixture asks for more.

Reservations are append-only and begin with `UNKNOWN` outcome. Settlement appends an outcome and releases the concurrency slot, but does not edit or refund the reservation. Duplicate attempt or idempotency replay returns `REPLAY`; conflicts fail closed. An unsettled reservation after a simulated crash retains both its consumed budget and concurrency slot. There is no automatic retry, alternate date, pagination, or provider fallback. A later explicit reservation would still require a distinct attempt and all checks to pass.

This storage API is not an outbound permission. Any future integration must separately retain `GuardedAttemptRunner` reservation/check fencing and require its own explicit operator activation action. D6.2.5 adds no such integration.

## 4. Single-host and processing rules

The future operating rule is one designated canonical execution host that owns the canonical `d6-smoke` SQLite file. The service requires an explicit designated host ID and compares the current host ID before migration or reservation. The actual owner-selected host remains **PENDING**. A second machine must not infer permission from Git history or an empty local ledger. SQLite here proves same-file process coordination only; it is not a multi-host quota service.

Provider-fetch approval, normalized-data retention, keyword extraction, derived metrics, display, and historical storage remain distinct permissions. The contract carries the fields, but no LIVE processing grant is created. Wikimedia Pageviews must not be labeled Google search volume; one daily snapshot does not establish demand or purchase intent. Raw provider bodies are not stored in reservation or settlement records.

## 5. Offline evidence

The new tests cover empty/missing state, valid state, PARTIAL job preservation, malformed receipt, missing policy digest, inconsistent counters, unsupported schema, idempotent re-migration, source/pool mismatch, additional source receipt preservation, wrong host, LIVE-profile rejection, duplicate authorization ID, missing contract values, retired approval ID, inactive/revoked/expired authorization, scope and digest mismatches, duplicate attempt/idempotency replay, same-job resume, uncertain outcome and non-refund, request/cost/start/lifetime limits, and two processes contending for one SQLite concurrency slot.

The guarded acceptance command, run from `Backend`, was:

```text
.venv/bin/python tests/run_d6_2_5_offline_acceptance.py
```

Result: **86 tests passed**. The run included migration/tranche tests, retired-launcher tests, local smoke policy, local composition and HTTP, public admission, Wikimedia gateway, synthetic E2E, and distributed-worker regressions. Strict offline guard totals: **0 real `AsyncHTTPTransport` constructions, 0 unmocked transport dispatches, 25 fake transport constructions, 0 provider requests**. Temporary SQLite was used for all migration and tranche tests. No canonical LIVE ledger was opened, read, migrated, or modified. No browser rerun was required because no browser-facing behavior changed.

Ruff, Python compilation, and `git diff --check` are recorded in the final handoff after verification. The current v1 receipt structure is inferred from the implementation and the D6.2.2/D6.2.4 reports; since the canonical ledger was intentionally not inspected, migration compatibility with its exact serialized bytes remains unverified until a separately authorized, read-only review. Any unexpected field difference fails closed rather than being repaired automatically.

## 6. Acceptance gates

| Gate | Result | Evidence |
|---|---|---|
| G1 Git baseline | PASS | Fetched origin; initial local and remote baseline matched; no prior user changes were present. |
| G2 Authorization model | PASS | Typed contract and v2 same-key envelope; old and future budgets are separate; no active grant created. |
| G3 Legacy migration | PASS offline | Atomic temporary SQLite migration, receipt/policy/provenance preservation, fail-closed cases and idempotent replay passed. Canonical compatibility is not claimed. |
| G4 Budget/admission | PASS offline | Two-additional and three-lifetime ceilings, scope, cost, starts, idempotency and no-refund checks passed; no dispatch connection exists. |
| G5 Concurrency/recovery | PASS offline | Crash/unknown-outcome behavior, replay and two-process same-file SQLite contention passed. |
| G6 Activation safety | PASS | New service is synthetic/test-only; no activation method or route; historical launcher remains blocked; no transport or credential access. |
| G7 Regression/security | PASS | 86 guarded tests; 0 real transports, 0 unmocked dispatches, 0 provider requests; Ruff/compile/diff checks PASS. |
| G8 Documentation/handoff | PASS | This report and D6 handoff updated; local commit only; no push or deployment. |

## 7. Owner decisions and future activation conditions

These remain **PENDING**: unique new approval ID and named approver; absolute UTC window; observation date(s); endpoint and source scope; monetary/provider fee cap and account limits; permission to retrieve data; permission to retain normalized records; processing/extraction and derived-metric grant; display and retention/deletion rules; attribution/provenance requirements; canonical migration approval; actual designated execution host; rollback owner; and independent LIVE execution authorization.

Future activation would require a separate Goal after those decisions are explicitly approved. That work must first verify the exact canonical state and migration compatibility read-only, review and approve any canonical migration, apply it only under explicit authority, preserve single-host ownership, independently activate a complete authorization and processing grant, and retain existing guarded runner, source approval, transport and storage checks. D6.2.5 does not claim `WIKIMEDIA_LIVE_SMOKE_VERIFIED`, complete D6.2, or authorize D6.2.6/D6.3.

**Implementation commit:** `1af86be` (`feat(d6): add offline authorization tranche accounting`). **Push/deployment:** none.
