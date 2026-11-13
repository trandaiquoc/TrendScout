# D6.2.4 — Wikimedia LIVE recovery readiness

**Date:** 2026-11-13
**Scope:** Offline recovery hardening only.
**Verification environment:** Ubuntu Linux, kernel `6.17.0-1033-oem`, Python 3.12.3. Native Windows was not available or rerun.
**LIVE requests / new budget / approval:** 0 / none / none.
**Status:** Historical D6.2 launcher permanently blocked; a future LIVE attempt is not ready or authorized.

## 1. Git baseline and scope

- `git fetch origin` completed before inspection.
- Initial `HEAD` and fetched `origin/main` both matched `4933a28ab804ef2e7a139191308e20b5344b4744`; the tree, index, and worktree were clean.
- No reset, overwrite, force operation, push, deployment, provider call, credential resolution, or canonical LIVE ledger access occurred.
- Implementation commit: `d33a8b69a3be9da3fcdca4baefa22ee81f370f23` (`fix(d6): retire legacy Wikimedia launcher`).
- D6.2 quota policy, starts/cooldown, request and cost limits, source cap, receipt persistence, policy digest, admission, checkpoint, and store implementations were not changed.

## 2. Historical failure and authorization state

The 2026-11-12 historical attempt used approval `D6.2-WIKIMEDIA-ONE-REQUEST-2026-11-12` for one US/all-access daily top-per-country Wikimedia request for observation date 2026-11-10. It received HTTP 200 response headers, failed body/response validation as `UPSTREAM_ERROR`, accepted zero records, and has no known exact original exception. The one-request reservation is consumed and the approval expired. Historical D6.2.3 G2 failure remains recorded in [the D6 report](BAO_CAO_DEMO_D6.md); earlier reports and outcomes were preserved.

The canonical Windows ledger was not opened in this Goal. Prior read-only evidence recorded one PARTIAL job, one start, one Wikimedia reservation and one attempt receipt. This implementation did not change, migrate, reset, or replace that ledger. A missing local ledger on another checkout or machine does not alter the retired approval.

## 3. Historical launcher retirement and preflight ordering

`Backend/scripts/d6_wikimedia_live.py` is now a retirement shim. It returns `BLOCKED / LEGACY_APPROVAL_EXPIRED_OR_CONSUMED` before importing or constructing the app, reading settings or credentials, opening a listener, admitting a job, reserving quota, or constructing a provider transport. The old `--owner-approved-wikimedia-only` flag is accepted only for command compatibility and cannot activate a request. `_run_once()` has the same blocked result.

`WikimediaOnlyApprovals.current()` always returns `None`. Caller-supplied expiry, process startup time, calendar date, host, checkout, environment and ledger state cannot change that result. No new approval ID, expiry, authorization source, route, or fallback ledger was introduced. Missing or corrupt ledger state cannot enable the launcher because there is no activation path; the retained reservation reader is read-only and treats missing/corrupt state as unavailable/invalid, never as authorization.

Cross-machine safety evidence is structural: the retired launcher has no host-, date-, process-, environment-, or ledger-dependent authorization branch. Tests simulate past/current/future expiry values and empty/consumed/corrupt temporary ledgers; they do not claim a multi-machine Windows/Linux execution test. Since this entrypoint has no activation route at all, switching machines cannot make the old ID valid. No centralized authorization service was introduced.

Offline tests used only temporary SQLite files. The consumed temporary receipt remained byte-for-byte unchanged. No canonical ledger connection was made.

## 4. Sanitized launcher diagnostic schema

The status summary now carries `transport_diagnostic` only when it matches the worker's complete, validated eight-field schema:

| Field | Validation |
|---|---|
| `source_id` | Exactly `wikimedia_pageviews_top_per_country` |
| `failure_phase` | One of `REQUEST_DISPATCH`, `RESPONSE_HEADERS`, `BODY_STREAM`, `JSON_DECODE`, `SCHEMA_VALIDATION` |
| `http_status` | `null` or exact integer 100–599 (booleans rejected) |
| `exception_class` | `null` or an existing allowlisted class name |
| `bytes_received` | Exact integer 0–1,000,000,000 |
| `response_size_limit` | Exact integer 1–10,000,000 |
| `attempt_id` | 1–128 ASCII letters, digits, underscore or hyphen |
| `error_code` | Safe uppercase code matching the source row's code |

The key set must match exactly. Missing, malformed, mismatched, or extra diagnostic fields cause the entire diagnostic to be omitted. The summary copies no nested provider dictionary, raw body, exception message, headers, credentials, path, or arbitrary provider properties. A body-stream failure may retain `http_status=200` while its source remains `FAILED`; HTTP 200 is not converted into body or schema success. Existing distinctions (`UPSTREAM_ERROR`, `UPSTREAM_TIMEOUT`, `MALFORMED_RESPONSE`, `RESPONSE_TOO_LARGE`, `JSON_DECODE`, and `SCHEMA_VALIDATION`) remain represented by the upstream gateway and status contracts.

Example sanitized output:

```json
{
  "status": "FAILED",
  "error_code": "UPSTREAM_ERROR",
  "transport_diagnostic": {
    "source_id": "wikimedia_pageviews_top_per_country",
    "failure_phase": "BODY_STREAM",
    "http_status": 200,
    "exception_class": "ReadError",
    "bytes_received": 128,
    "response_size_limit": 1000000,
    "attempt_id": "attempt_fixture_1",
    "error_code": "UPSTREAM_ERROR"
  }
}
```

This is a synthetic summary example, not historical live diagnostic evidence. The original D6.2 exception is still unknown.

## 5. Offline authorization and diagnostic cases

New tests verify:

- The historical ID is retired for past, current, and future caller-supplied expiries; the old CLI flag and a fresh `_run_once()` return the stable blocked result.
- App startup is not reached; zero provider transports, admissions, or reservations can be created through this launcher.
- Empty and consumed temporary ledgers both leave the approval blocked; a consumed temporary ledger remains byte-for-byte unchanged. Missing and corrupt ledger files do not authorize anything.
- Diagnostic fields survive when valid. Repeated summary polling is stable. HTTP 200 after `BODY_STREAM` `ReadError` or `RemoteProtocolError`, `ReadTimeout`, total deadline, malformed JSON, invalid schema, and oversized response preserve failure status and classifications.
- Missing diagnostics and invalid types, mismatched error code, extra keys, raw-body marker, or unsafe attempt ID cause diagnostic omission; the raw marker and private path do not appear in output.
- Existing gateway tests cover zero-retry behavior, duplicate attempt protection, receipt preservation, scope/date rejection, MockTransport parsing and provider-shaped failures.

## 6. Guarded regression evidence

Command, from `Backend`:

```text
.venv/bin/python tests/run_d6_2_4_offline_acceptance.py
```

Result: **62 tests passed** across the D6.2 activation, local smoke policy, Wikimedia gateway, HTTP policy, local LIVE HTTP fixture, synthetic E2E, and distributed worker modules. Ruff and Python compilation passed for the changed Python files; `git diff --check` passed.

The strict offline HTTP guard reported:

- Real `AsyncHTTPTransport` constructions: **0**.
- Unmocked transport dispatches: **0**.
- Fake transport constructions: **13**.
- Real provider requests: **0**.
- Provider credential values resolved: **0** (the retired launcher does not resolve credentials; its guard test raises if resolution is attempted).

Synthetic `MockTransport` and ASGI fixture dispatches occurred only inside tests and are not provider requests. No provider DNS lookup was made. Chrome was not rerun because this change affects only the backend launcher/summary and the prior D6.2.3.1 synthetic browser acceptance remains the accepted evidence for its scope.

## 7. Future LIVE owner decision packet

Every item below is **PENDING** for a future, separately authorized attempt. Values from the expired D6.2 approval are historical facts only and do not fill or authorize a new request.

| Decision | Status | Required before a future attempt |
|---|---|---|
| New approval ID | PENDING | Owner-issued unique identifier; never reuse the retired ID. |
| Named approver | PENDING | Identify the person with authority for this use. |
| Absolute UTC start and expiry | PENDING | Fixed timestamps; no process-relative expiry refresh. |
| Exact Wikimedia endpoint | PENDING | Confirm exact path and API version. |
| Country and access scope | PENDING | Confirm the requested geography/access dimensions. |
| Observation date | PENDING | Approve one explicit date. |
| Query-plan fingerprint | PENDING | Review and bind the approved fixed plan. |
| Maximum additional requests | PENDING | Set an explicit request cap. |
| Maximum additional attempts | PENDING | Set an explicit attempt cap, including uncertain outcomes. |
| Maximum aggregate attempts | PENDING | Define lifetime total including the consumed historical attempt. |
| Maximum job starts | PENDING | Define start cap independently from request/attempt caps. |
| Zero retry policy | PENDING | Explicitly decide retry count; do not infer it from D6.2. |
| Zero fallback policy | PENDING | Explicitly decide date/endpoint/provider fallback behavior. |
| Maximum monetary/provider fee | PENDING | Confirm currency, fee cap, overage behavior and account-level limits. |
| Processing/extraction permission | PENDING | Specify allowed extraction and downstream use. |
| Derived metrics permission | PENDING | Specify whether calculations or assessments are allowed. |
| Data retention | PENDING | Specify data classes, duration and deletion obligations. |
| Raw-body prohibition | PENDING | Confirm raw-body handling and any permitted transient processing. |
| Provenance requirements | PENDING | Define source, fetch time, observation time, scope and evidence record. |
| Same-ledger authorization tranche | PENDING | Approve design for append-only tranche linkage in the canonical ledger. |
| Atomic legacy-preserving migration | PENDING | Approve migration plan that preserves the consumed job and receipt. |
| Failure stop conditions | PENDING | Define conditions that stop without retries or reauthorization. |
| Rollback owner | PENDING | Name who owns rollback and incident handling. |

No decision above has been invented, and the D6.2.2 same-ledger append-only tranche remains a proposed prerequisite, not implemented functionality. A separate implementation and migration design must be reviewed and tested against temporary databases before any future launch can be considered.

## 8. Acceptance gates

| Gate | Result | Evidence |
|---|---|---|
| G1 Git synchronization | PASS | Fetched `origin/main`; initial HEAD matched; clean tree and index. |
| G2 Historical approval retired | PASS | Fixed ID always unavailable; old flag, refreshed expiry and new-process call return blocked. |
| G3 Safe preflight | PASS | Launcher has no app/transport/admission path; test asserts app startup is not called; temporary quota state is unchanged. |
| G4 Diagnostic output | PASS | Exact-schema whitelist, value/range validation, error-code match, omission of malformed fields, and synthetic failure summary cases. |
| G5 Quota/ledger preservation | PASS | No policy/store code changes or canonical ledger access; temporary consumed receipt unchanged. |
| G6 Offline regression | PASS | 62/62 guarded tests; 0 real transports, 0 unmocked dispatches, 0 provider requests. |
| G7 Future LIVE readiness | PASS | All required owner decisions and technical prerequisites are explicitly listed as pending. |
| G8 Documentation and handoff | PASS | New readiness report, D6 report pointer, local implementation commit; no push or deployment. |

## 9. Remaining LIVE blockers and next Goal

Wikimedia LIVE remains unverified. The old one-request authorization is expired, its reservation remains consumed, the original processing exception is unknown, future authorization/processing/retention decisions are pending, and the same-ledger append-only authorization design and preserving migration do not exist. No live path is available through the historical launcher. `WIKIMEDIA_LIVE_SMOKE_VERIFIED` remains unset and D6.2 overall remains incomplete.

Recommended next Goal: obtain explicit owner decisions for the pending packet, then separately review and authorize a same-ledger authorization design/migration. That work must not attempt a provider request. Any future LIVE attempt requires its own explicit Goal and valid authorization after the technical work is accepted.

**Push/deployment:** none.
**Implementation commit:** `d33a8b69a3be9da3fcdca4baefa22ee81f370f23`.
