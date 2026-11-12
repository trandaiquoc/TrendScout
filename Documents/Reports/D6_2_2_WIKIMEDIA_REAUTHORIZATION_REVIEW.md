# D6.2.2 Wikimedia re-authorization and quota recovery feasibility review

**Review date:** 2026-11-12
**Status:** Design and code review only. No new authorization is granted or inferred.
**Provider requests:** None.

## 1. Executive finding

The current D6 local-smoke implementation does **not** support a second Wikimedia attempt under a separately added authorization while preserving its existing runtime contracts. The canonical ledger and policy combine several independent one-attempt guards, and the local source wrapper enforces a persistent one-reservation-per-source cap. A changed policy also fails closed against the existing ledger's `policy_digest`. The correct behavior today is to keep the D6.2 receipt consumed and reject another attempt.

A future additive budget is technically feasible as a separately designed, owner-approved ledger feature. It would require an append-only authorization tranche in the **same canonical ledger**, receipt-to-authorization linkage, atomic per-tranche and aggregate caps, a carefully reviewed schema migration preserving the D6.2 job and receipt, an absolute expiry, and updated activation/output guards. None of that exists today and none was implemented in this review.

For current development, use the existing synthetic `MockTransport` fixtures (Option C) and keep live spending stopped (Option A). Option B should be considered only after the owner explicitly approves a new, fully scoped budget and a separate implementation goal lands the required code and migration. The historical D6.2 failure remains unresolved.

## 2. Git baseline and scope

- `git fetch origin` completed.
- Starting worktree was clean; there were no staged or unstaged changes.
- Local `HEAD` and `origin/main` both matched `18b7a87a00b051586cc143cc7e0acae4b0a1809d`.
- The review did not alter runtime code, policy, approval register, dependencies, or ledger.
- Git synchronization and review were performed without reset, overwrite, or push.

## 3. Existing quota and reservation architecture

### Effective D6.2 local policy

| Control | Current Wikimedia value | Meaning |
| --- | ---: | --- |
| Daily request limit | 1 | Internal admission cap per UTC day for the Wikimedia pool. |
| Monthly request limit | 1 | Internal admission cap per UTC month. |
| Daily cost units | 1 | Internal cost-unit cap; these units are not a provider price. |
| Monthly cost units | 1 | Internal cost-unit cap. |
| Cost per attempt | 1 | One unit is reserved before network I/O. |
| Maximum in-flight | 1 | At most one concurrent Wikimedia attempt. |
| Attempt timeout | 15 seconds | Bounded provider-attempt slot. |
| Starts per window | 1 per 86,400 seconds | At most one new local job start in the rolling window. |
| Cooldown | 86,400 seconds | Also blocks a new job during the cooldown. |
| Job claim attempts | 1 | `max_attempts` bounds worker job claiming; it does not refund or reopen a provider receipt. |
| Job lifetime / idempotency receipt | 120 / 600 seconds | Job payload lifetime and idempotency-key retention. |
| Local source cap | 1 reservation per source | `LocalSmokeStore` rejects a second Wikimedia receipt even under a different job/attempt ID. |
| Normalized record budget | 100 | Admission state record bound. |

These controls are server-owned; the policy is default-disabled. The launcher additionally checks that the one-request policy values still match before accepting its Fetch flow.

### Reservation lifecycle

`PublicAdmission.admit()` returns the existing job for the same idempotency key. A new key may create a new job, but it consumes a start and is subject to the rolling start and cooldown checks.

`reserve_attempt()` atomically checks the fenced job, derives a receipt key as SHA-256 of the attempt ID, returns `REPLAY` for the same receipt, then checks daily/monthly request and cost caps. On `NEW`, it increments those counters and writes a receipt containing source, pool and reservation time before outbound I/O. It also creates an in-flight slot. The local `LocalSmokeStore` wrapper then enforces its independent source cap over all receipts.

`settle_attempt()` removes only the in-flight slot. It does not refund request or cost counters or delete the receipt. Thus an error after the request, an uncertain timeout, or cancellation remains conservatively charged; duplicate receipt replay never sends another request. The worker rechecks the reservation immediately before dispatch.

Quota receipts and counters are in the canonical SQLite atomic-state file. For the local LIVE composition, source checkpoints are volatile and are lost on process restart; the durable quota record is intentionally retained. The configured idempotency/job records may expire, but `local_smoke_source_counts` is a sticky map and is not reset by job cleanup.

### Read-only historical ledger inspection

The canonical path resolved by the current Windows code is `%LOCALAPPDATA%\TrendScout\local\d6-smoke\ledger.sqlite`, state key `d6-smoke`. It was opened with SQLite `mode=ro`. Inspection found:

- 1 job, state `PARTIAL`;
- 1 start;
- `local_smoke_source_counts[wikimedia_pageviews_top_per_country] = 1`;
- 1 Wikimedia receipt.

No ledger, WAL, policy counter, namespace, or receipt was changed.

## 4. Why another attempt is currently blocked

There is no supported additive-authorization path in this ledger version:

1. The one-source count must be exactly 1 and a new receipt for a source already in that map is rejected as `SMOKE_SOURCE_LIMIT`.
2. Daily and monthly Wikimedia request and cost limits are each 1. A day rollover alone does not clear the source cap; month rollover alone does not clear it either.
3. A new job requires a new idempotency key and another start, but the D6.2 start is consumed for the 24-hour window. Reusing its key replays the old job.
4. The existing receipt has no `authorization_id`; it cannot be reconciled against a new independent budget.
5. The policy digest includes the configured limits. Changing the policy under the same ledger causes `POLICY_MISMATCH`; simply raising the limits does not migrate or authorize the old ledger.
6. The D6.2 one-request approval has expired. Its record is scoped to one request and cannot authorize another request, company use, discovery extraction, or a new observation date.

There is also a code-level approval lifecycle concern: `d6_wikimedia_live.py` creates `expires_at` as `started_at + 5 minutes` each time `_run_once()` starts, while reusing the fixed D6.2 approval ID and policy version. The ledger preflight stops this particular launcher on the consumed reservation, but the old approval ID should not be treated as a re-usable owner authorization. A future path should load an explicit, independently approved record with an absolute expiry, rather than minting a fresh expiry from process start.

## 5. Authorization options A/B/C

| Option | Code changes | Approval changes | Quota and audit implications | Security implications and risks | Current ledger support |
| --- | --- | --- | --- | --- | --- |
| **A. Keep the one-request budget consumed; no more live attempt** | None. Keep existing fail-closed gates. | None. Old approval stays expired. | Historical cost remains counted; no new receipt. Simplest audit trail. | Lowest new egress and data-retention risk. Live D6.2 acceptance remains incomplete. | **Supported now and recommended for live use.** |
| **B. Add a separately approved one-time tranche** | Requires versioned policy/ledger work: append-only authorization records, receipt-to-authorization linkage, independent and aggregate request/cost/start caps, idempotency and restart rules, atomic admission, and a migration that keeps the existing D6.2 receipt/counters intact. Update the one-shot launcher/summary and test the migration on temporary databases. Do not change the existing record into an unused state. | New approval ID and absolute expiry; explicit source/scope/date, budget, use/processing, retention, cost/fees, security and rollback owner. Old D6.2 approval cannot be extended or reused. | The old one-unit budget remains consumed. A second one-request tranche would make the sequence total at most two attempts (one historical plus one new) only if the owner explicitly sets that lifetime maximum. A ledger migration and append-only audit entries are necessary; no namespace split. | Increased egress/retention and migration risk. A changed policy digest currently fails closed; migration must be atomic, validated, backed by temporary-database tests and preserve legacy receipts. New per-tranche caps must not accidentally open other providers or general Fetch. | **Not supported today. Feasible only after a new approved design and implementation.** |
| **C. Use offline fixtures or a provider-supported historical artifact** | Existing synthetic `MockTransport` fixtures already exercise transport, schema and worker paths. A truly provider-authentic offline artifact would need a documented source, permitted retrieval/retention, immutable provenance and fixture parser tests; none was added here. | No new live-request approval for synthetic fixtures. Any real historical artifact still needs explicit acquisition rights, attribution/provenance, retention and processing decisions. | No live quota is spent for synthetic fixtures. A previously downloaded official dataset could avoid repeat API calls, but this review did not verify a provider-supported downloadable artifact for the exact US top-per-country response. | Synthetic data cannot prove credentials, connectivity, live schema, or provider behavior. An unverified/cached body must not be represented as LIVE or as a current result. | **Synthetic offline fixtures supported; exact official offline artifact not established.** |

The Wikimedia Analytics API reference states that page-view endpoints serve data starting on 2015-07-01. That documents historical *API* availability, not an offline file: retrieving it would still invoke the provider and is prohibited by this goal. The country-data documentation also says per-country page views are bucketed, suppressed at or below 100 views per requested period, and zero values are not reported; historical replay must preserve those limitations. These official pages were checked on 2026-11-12: [Page view analytics reference](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html) and [Country data](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/concepts/country-data.html).

## 6. Recommended architecture if the owner later chooses Option B

Keep the single canonical local ledger and add a versioned, append-only authorization-tranche model inside its existing state key. Never delete or rewrite the historical attempt into an unconsumed state. Before any implementation, define a migration that maps the legacy D6.2 receipt and consumed count to a **closed/consumed legacy tranche**, preserving the receipt hash, source, pool, reservation time, request/cost counters, and original approval reference. Add the new tranche as a distinct record only after explicit owner authorization.

Each authorization tranche should bind:

- unique authorization ID, approver, absolute UTC `valid_from` and expiry, and a policy/version digest;
- exactly one source (`wikimedia_pageviews_top_per_country`), approved endpoint shape, country/access scope, observation date or date-selection rule, and query-plan fingerprint;
- maximum additional provider requests and attempts, daily/monthly request caps, internal cost units, maximum monetary/provider fee, concurrency and start budget;
- permitted use and any processing grant; retention duration, raw-body handling, attribution and deletion decision;
- security/egress/telemetry controls and named rollback owner.

The atomic reservation must bind `{authorization_id, source_id, job_id, attempt_id}`. Replays of that binding must return `REPLAY` without dispatch; a second distinct attempt under the tranche must fail. Admission should enforce both the tranche's remaining budget and a separately approved aggregate cap. Migration, adding a tranche, counter changes and reservations must transact under the existing canonical SQLite store and fail closed if any receipt, policy digest or legacy record cannot be reconciled. No alternate ledger is appropriate.

The current status API carries sanitized D6.2.1 transport diagnostics, but the D6.2 launcher `_summary()` deliberately selects only status, mode, record count and error code. Before a future authorized diagnostic run, the launcher would need a separate reviewed update to emit only the validated fields (`failure_phase`, available HTTP status, exception class, bytes received, response limit, attempt ID and stable code). Body-stream failures after headers can then report status 200 without calling that body valid. The exact original D6.2 exception remains unknown. A decode/dispatch failure that occurs before HTTPX yields a response object may still have no status. Diagnostics do not justify retries; policy remains one request and zero automatic retries.

## 7. Explicit owner decisions required before any second request

The owner must answer and record all of the following in a new, dated authorization. No choice is inferred here.

1. **Additional budget:** maximum additional Wikimedia requests and attempts (recommended ceiling for a follow-up: one GET / one attempt); maximum total attempts including the already consumed D6.2 attempt (e.g. two total for this sequence); maximum starts/jobs; whether zero retries, fallback, pagination and parallel requests are required.
2. **Observation:** exact observation date; whether any date fallback is prohibited; exact endpoint, `US`, `all-access`, top-per-country scope and query-plan fingerprint.
3. **Cost:** maximum provider request cost, maximum monetary fee, and explicit provider-fee authorization (including an explicit zero-fee decision if that is intended). Internal cost units are not money.
4. **Use and processing:** permitted retrieval/use; whether only normalized source-level records are allowed or any discovery extraction/derived analysis is authorized. The old D6.2 record does not authorize processing.
5. **Retention/deletion/attribution:** raw response body retention (recommended none), normalized record retention, checkpoint duration, log/diagnostic retention, deletion responsibilities and attribution/provenance.
6. **Approval identity:** new unique approval ID and policy version; named approver; local-only scope; absolute UTC start and expiry; security, egress and telemetry controls.
7. **Operations:** named rollback owner; evidence to retain; stop condition; recovery procedure if the new request has an uncertain result. A failed/uncertain second attempt must remain consumed.

The original approval ID `D6.2-WIKIMEDIA-ONE-REQUEST-2026-11-12` is expired and cannot be extended by this review. The approval register remains unchanged pending explicit owner decisions.

## 8. D6.2.1 transport diagnostic review

The D6.2.1 changes are present at the baseline commit. `HttpPolicy.get_bounded()` streams and returns a reconstructed response only after the complete bounded body is received. The activation transport wrapper's status 200 therefore does not establish body completion or validation.

Offline tests establish that:

- body `ReadError` and `RemoteProtocolError` after HTTP 200 are exposed as sanitized diagnostics with phase/status/class/bytes;
- read timeout and total deadline are distinguishable from a generic transport error;
- completed invalid JSON (`JSON_DECODE`) and valid JSON with invalid Wikimedia schema/scope/date (`SCHEMA_VALIDATION`) have separate classifications;
- diagnostics exclude raw body, exception message and unnecessary response headers;
- cancellation propagates and no automatic retry was introduced;
- source status validates and returns only the fixed diagnostic field set.

This improves a future diagnosis but does not resolve or identify the historical failure. The D6.2 launcher summary currently drops the new diagnostic fields, so the end-to-end launcher evidence is not sufficient until that summary is updated under a later approved goal. No live launcher was run as part of this review.

## 9. Offline verification and operations not performed

On the existing Windows Python 3.12.10 environment, these offline modules passed: `test_d6_local_smoke_policy` and `test_d6_wikimedia_live_activation` (**11 tests, PASS**). Their quota-state cases create temporary databases. The D6.2.1 baseline also recorded 66 focused offline regressions passing; this documentation-only review did not rerun that broader set.

Not performed: any Wikimedia, Google, Bing, YouTube or SerpApi request; any Fetch submission; any live launcher; any quota reset or policy change; any write to the canonical ledger; any new approval or credential; any D6.3 implementation; any deployment; any push.

## 10. Recommendation and acceptance

Choose **A + C now**: preserve the consumed reservation, continue synthetic offline integration development, and leave live fetching disabled. If the owner later chooses **B**, create a separate implementation goal after documenting all decisions in Section 7. That goal must implement same-ledger append-only authorization tranches, preserve the legacy receipt, update the launcher diagnostic summary, and pass isolated migration/admission tests before any independently approved one-request live goal is considered.

| D6.2.2 gate | Result | Evidence |
| --- | --- | --- |
| G1 Git baseline | PASS | Clean tree; `HEAD` = `origin/main` = `18b7a87a00b051586cc143cc7e0acae4b0a1809d`. |
| G2 Historical reservation preserved | PASS | Read-only ledger check: one PARTIAL job, one start, one Wikimedia source reservation and one receipt. |
| G3 Quota architecture understood | PASS | Policy, `LocalSmokeStore`, `PublicAdmission`, SQLite and guarded attempt lifecycle reviewed. |
| G4 Additive feasibility established | PASS | Not supported by current ledger; feasible only with a new same-ledger append-only tranche design and preserved migration. |
| G5 Approval expiry respected | PASS | Old request is expired and one-shot; no new approval inferred. Dynamic launcher expiry generation is flagged as a future hardening item. |
| G6 Diagnostics reviewed | PASS with limitation | Gateway/status diagnostics are sanitized; launcher `_summary()` currently omits them. Original failure remains unknown. |
| G7 Options A/B/C documented | PASS | Section 5. |
| G8 Owner decisions documented | PASS | Section 7. |
| G9 Documentation committed locally | PASS | This review and its main-report pointer were committed locally as a documentation-only change; no push. |
