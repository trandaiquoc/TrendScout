# D6.2.6 — Wikimedia LIVE integration readiness

**Date:** 2026-11-13  
**Status:** Offline integration acceptance only; LIVE activation remains BLOCKED.  
**Environment:** Native Linux, Python 3.12.3. Windows acceptance deferred.  
**Starting HEAD / fetched `origin/main`:** `0be1864e286a95310facb54560dc47edae95f67a`.  
**Verified implementation HEAD:** `cd5f66a` (`feat(d6): integrate offline tranche guarded runtime`); the report/register update is committed separately.  
**Provider requests / real HTTP transport constructions / unmocked requests:** 0 / 0 / 0.

## 1. Scope and controls

D6.2.6 connects the existing schema-v2 authorization tranche accounting to the existing `WikimediaTopPageGateway`, `GuardedAttemptRunner`, `PublicAdmission`, distributed worker, source checkpoint, status, and discovery path for offline acceptance. It does not enable Wikimedia LIVE, change the retired launcher, or change the browser-facing API contract.

The new `FutureWikimediaLiveActivation` reports `BLOCKED` and its dispatch guard always raises `LIVE_ACTIVATION_BLOCKED`. It has no CLI activation flag, environment override, credentials input, or network transport factory. The separate tranche-aware simulation accepts only a `SYNTHETIC` store and `httpx.MockTransport`. Test approvals and active authorization contracts live only in disposable temporary databases. The historic approval `D6.2-WIKIMEDIA-ONE-REQUEST-2026-11-12` remains retired.

The future technical ceiling stays at two additional requests; the historical consumed request remains one; the lifetime ceiling remains three. The schema-v2 capacity record still has authorized capacity zero and state `INACTIVE`. No fixture authorization is copied into a canonical LIVE ledger.

## 2. Integration and accounting design

`SameLedgerRuntimeAdmissionStore` stores the existing `PublicAdmission` state as `runtime_admission_state` within the schema-v2 envelope at the existing `d6-smoke` key. The schema-v1 state remains preserved as `legacy_state`. This creates no second key or quota database. The adapter validates the v2 envelope on every transaction and rejects non-synthetic stores.

`TrancheAwareGuardedAttemptRunner` subclasses the current runner so the Wikimedia gateway retains its type and approval checks. It rechecks the current runtime source approval and binds its approval ID and policy version to the active fixture tranche. It binds the policy digest, query-plan fingerprint, fixed top-per-country endpoint, US country, all-access mode, and fixed observation date before any transport call. The gateway retains its existing exact host/path/query checks, one-request policy, response limits, parser, and sanitized diagnostics.

The handoff is deliberately ordered:

1. Append the tranche reservation atomically in schema v2.
2. Reserve the normal `PublicAdmission` attempt in the same `d6-smoke` record.
3. Recheck fenced admission and dispatch through the existing Wikimedia gateway.
4. Persist normalized page receipt/checkpoint before normal settlement.
5. Settle the tranche only after the runtime attempt reaches a definite terminal settlement.

The two service-level reservations are separate operations inside one SQLite state key, not one transaction. A crash or uncertain handoff after step 1 remains consumed and blocks replay; it is never refunded. A crash after a page receipt is recoverable: checkpoint replay settles both the runtime attempt and the tranche idempotently without a second GET. Timeouts and uncertain outcomes remain reserved. Completed provider failures settle the concurrency slot but retain their consumed budget. No automatic retry, pagination, fallback, or second attempt is introduced.

The standard API/worker graph is used with Wikimedia enabled and Google, Bing, and YouTube source handlers unavailable. The simulated requests are `SYNTHETIC`; their candidates and provenance are not LIVE evidence. Existing discovery behavior blocks LIVE candidates without a processing grant; the D6.2.3 regression for a LIVE result with no extraction grant remains in the combined run. Raw provider response bodies are not persisted.

## 3. Offline compatibility preflight and rollback plan

`read_only_ledger_compatibility_preflight()` opens SQLite with `mode=ro`, reads only `d6-smoke`, validates representative v1 or v2 structure, checks the designated host, and returns a bounded status/reason without returning ledger contents. Temporary v1/v2 tests confirmed the preflight does not change the database bytes. Missing file/state, unsupported/corrupt schema, or host mismatch returns `BLOCKED`. The canonical owner ledger was not opened, read, or migrated; no real compatibility PASS is claimed.

For a separately authorized future canonical migration:

1. Stop the application and every worker on the designated host; confirm no process can write the ledger.
2. Record the canonical path, host identity, file ownership/permissions, SQLite integrity status, and a secure pre-migration backup made with SQLite's backup API. Verify the backup opens read-only and contains the same `d6-smoke` receipt and counters.
3. Run the compatibility preflight against the canonical file in read-only mode. Compare the full actual v1 shape and receipt against migration invariants. Stop if anything is unknown; do not repair or fabricate fields.
4. Obtain independent migration approval. Apply the same-key migration transaction once, then read back schema, legacy receipt/hash, source counts, and inactive capacity. Keep all dispatch gates blocked.
5. If migration validation fails before commit, the SQLite transaction rolls back. If post-commit validation fails, keep the service stopped and restore the verified backup only if no later reservations or ledger changes occurred and the rollback owner authorizes it. If any later state was written, do not restore an older snapshot or reset quota; preserve both artifacts and reconcile under explicit owner review.

These steps are a prepared plan, not canonical-host verification or permission to migrate.

## 4. Offline acceptance evidence

The D6.2.6 cases use temporary schema-v1 state, offline migration, active fixture-only authorization, `MockTransport`, an isolated API client, the real worker and checkpoint pipeline, the actual Wikimedia parser, status, discovery, and provenance.

| Case | Result |
|---|---|
| A — successful normalized page, API through discovery and provenance | PASS |
| B — HTTP 200 then body `ReadError` | PASS; request stays consumed |
| C — body `ReadTimeout` | PASS; request stays consumed |
| D — malformed JSON | PASS; no automatic retry |
| E — wrong country/date scope | PASS; parser rejects the page |
| F — duplicate Fetch and duplicate physical attempt | PASS; no duplicate GET |
| G — failure/crash does not automatically start another request | PASS |
| H — two separate operator actions consume two fixture reservations | PASS |
| I — third additional request is rejected before dispatch | PASS |
| J — process restart and same-key replay preserve counters | PASS |
| K — missing, expired, wrong-country, or policy-mismatched tranche; revoked runtime approval | PASS; zero dispatch |
| L — missing LIVE extraction grant blocks LIVE discovery candidates | PASS in existing synthetic E2E regression |
| M — Google, Bing, and YouTube remain unavailable | PASS |
| N — wrong host or unavailable ledger blocks; v1/v2 preflight is read-only | PASS |

The final acceptance command, run once after the code and test changes, is:

```text
.venv/bin/python tests/run_d6_2_6_offline_acceptance.py
```

It includes the existing D6.2.5 migration/tranche tests, D6.2.4 retired-launcher tests, HTTP policy and Wikimedia gateway tests, PublicAdmission, worker/checkpoint recovery, synthetic E2E, and new tranche integration tests. The final run passed **97 tests**. The combined guard counted **36 MockTransport fixture dispatches** (9 in the new D6.2.6 integration cases), **0 real `AsyncHTTPTransport` constructions, 0 unmocked transport requests, and 0 real provider requests**. It also counted 25 fake transport constructions used by the existing isolated HTTP-composition regressions. All provider behavior in this run used mocks; no provider DNS or credential resolution path was invoked.

Ruff passed for all changed Python files, Python compilation passed, and `git diff --check` passed. A repository-wide Ruff scan was also attempted and reported 1,551 style findings in existing unrelated files; those files were left unchanged. Browser-facing behavior did not change, so no Chrome rerun was needed; Linux browser evidence remains the accepted D6.2.3.1 run. This is not Windows browser acceptance.

## 5. Acceptance gates

| Gate | Result | Evidence / limit |
|---|---|---|
| G1 — Git baseline | PASS | Fetched `origin`; starting HEAD matched the known baseline; no existing local changes were present. |
| G2 — runtime integration | PASS offline | New tranche-aware guarded simulation uses same-key schema-v2 state. Future LIVE gate is always BLOCKED. |
| G3 — budget safety | PASS offline | Historical 1 + two additional fixture reservations = lifetime 3; third request denied; failures and crash are non-refundable. |
| G4 — approvals/security | PASS offline | Active tranche and current runtime approval are both required and scope/version-bound; launcher stays retired; host and missing-ledger bypasses fail closed. |
| G5 — E2E | PASS offline | API, worker, actual parser, checkpoint, status, discovery/provenance, and replay use temporary state and MockTransport. |
| G6 — failure/recovery | PASS offline | ReadError, timeout, malformed response, invalid scope/date, duplicate, restart, and crash cases passed; no automatic retry. |
| G7 — regression/isolation | PASS offline | 97 combined D6.2.5/D6.2.4 and runtime tests; 36 mocked fixture dispatches, 0 real transport constructions, 0 unmocked dispatches, 0 provider requests; canonical ledger untouched. |
| G8 — handoff | PASS on local commit | This report and BAO register are updated; Linux-only status and owner decisions are listed; no push/deployment. |

## 6. Technical readiness and pending owner approvals

### A. Technical readiness

- Offline tranche-aware integration and failure/recovery coverage are complete.
- The real LIVE activation state remains `BLOCKED`; no production path is enabled by this work.
- Canonical host compatibility remains unverified. The read-only helper has only been tested on temporary representative ledgers.
- Future execution still requires an approved canonical migration and same-ledger verification on the designated host.
- Windows Python/browser verification remains deferred. Linux dashboard acceptance was not rerun because the browser contract did not change.

### B. Missing owner approvals

- New approval ID and named approver.
- Absolute UTC validity window and exact observation date.
- Exact endpoint, source, country, access scope, and query-plan fingerprint.
- Request, attempt, and job-start authorization; the owner-selected technical additional-request ceiling remains 2 but grants zero usable capacity by itself.
- Monetary fee ceiling and currency.
- Separate retrieval, normalized-record retention, extraction, derived-metric, and UI display permissions.
- Retention and deletion period, attribution/provenance rules, and raw-body policy.
- Designated canonical host and read-only compatibility review.
- Explicit authorization for canonical migration and named rollback owner.
- Separate permission for a future LIVE execution.

The exact next LIVE sequence is: obtain and record all approvals; select the canonical host; stop all writers and run the read-only preflight there; independently review the actual ledger; authorize and perform the same-ledger migration with verified backup/rollback controls; append a new complete active tranche and processing grant under separate approval; review the still-blocked LIVE transport boundary; then perform one explicitly authorized request at a time with post-request reconciliation. None of these LIVE steps was performed in D6.2.6.

`WIKIMEDIA_LIVE_SMOKE_VERIFIED` remains unset. D6.2 remains incomplete. No provider request, credential resolution, DNS lookup, canonical ledger access/mutation, deployment, or push occurred.

**Documentation commit:** recorded in the final user handoff. The tested code commit and report/register commit are local only; no push.
