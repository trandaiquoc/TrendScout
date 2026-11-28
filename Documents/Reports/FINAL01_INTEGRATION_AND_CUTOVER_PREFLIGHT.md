# FINAL01 Integration and Cutover Preflight

Date: 2026-11-28
Branch: `integration/demo-final`
Starting HEAD: `4d85fcf7bee5e1703391c46e948d8092c54e34bf`
Requested baseline `edfc5fb` and the attachment baseline `4f8b14c` are both ancestors of the starting HEAD.
`origin/main` at preflight: `5122e6f0a717f72f2933bea56978c2b66f42139a`; it is an ancestor of the integration branch. No merge was needed.

## Outcome

**Preflight remains BLOCKED: 4/6 gates PASS (67%).** This report does not attest Production cutover, deployment, or live provider readiness. No Production schema writes, migrations, deployment, main-branch push, live inference, provider Fetch, or quota reset were performed.

| Gate | Result | Evidence / remaining condition |
|---|---|---|
| G1 — Security readiness | PASS | SEC01 G1–G5 remained PASS. The Owner confirmed Neon password rotation, invalidation of the old credential, root `.env` update, and pooled/direct TLS authentication. SEC01 G6 and its report were closed on that confirmation. The refreshed root `.env` check found both URLs present, no process-ENV mismatch, and matching pooled/direct branch, database, and role. The direct URL requires TLS and a read-only query connected successfully. PostgreSQL's `pg_stat_ssl` view reported `ssl=false`; this is a server-side session view and does not independently attest the client-to-Neon TLS leg. The Owner's direct and pooled TLS verification remains the evidence for that leg. No credentials were printed. |
| G2 — Contract reconciliation | PASS | `DB_CONTRACT.md`, `AI_CONTRACT.md`, `UI_CONTRACT.md`, and `AI_SOURCE_POLICY_MATRIX.md` were reconciled to the checked-in implementation, including the missing-ledger admission gap and the rule that only Wikimedia Pageviews is currently approved for live AI input. |
| G3 — Backend integration | PASS | Consolidated isolated-PostgreSQL regression: **92 passed, 10 subtests passed, 0 skipped, 1 warning**, 319.69 s. No live provider or inference requests were made. See the exact suite list below. |
| G4 — Full UI integration | PASS | Local browser used the actual backend with isolated PostgreSQL schemas, synthetic fixtures, and a process-local mocked AI provider. All 11 routes loaded with backend connected and correct active nav/breadcrumb. Recommendation generation reached a completed synthetic result. Empty, network-error, and disabled/deferred states; direct URL reload; in-app navigation; responsive width; VI/EN; light/dark; and keyboard skip navigation were checked. No external browser origin was contacted. A live Fetch was intentionally not submitted; the backend source-fetch switch was off. The existing Fetch progress frontend coverage from AI07 remains the evidence for that component. |
| G5 — Operational readiness | BLOCKED | FINAL01B fixes missing-ledger fail-open admission, adds an evidence-backed atomic operator bootstrap, and passes focused unit/PostgreSQL checks. Live quota usage and cooldown still have no Owner evidence; the live AI scope remains uninitialized and AI01H G4 remains blocked after earlier provider 429 responses. No live bootstrap or provider request was made. |
| G6 — DB08 cutover preflight | BLOCKED | Root `.env` schema `trendscout` remains at migration version 9 with a matching 0001–0009 checksum prefix; 0010–0014 remain unapplied. Five local SQLite stores were captured and restored into memory with integrity/schema/count checks. Production identity, Neon/Firestore backup and restore, Vercel linkage, and legacy-writer freeze remain unverified. |

## G1 — Security and database authentication

- Owner confirmation states the Neon password was changed, the old credential disabled, and pooled and direct TLS authentication passed. No repeat rotation was requested or performed.
- A sanitized in-memory inspection of root `.env` found `DATABASE_URL` and `DATABASE_URL_UNPOOLED`; neither was overridden by the current process environment. Their Neon endpoint identities (normalizing the pooler hostname), database, and role matched. Both DSNs require TLS.
- A direct read-only connection succeeded and stayed in a read-only transaction. No connection string, host, database name, username, password, or exception message was written to output or this report.
- The separate `pg_stat_ssl` server view returned `ssl=false`. This does not override the Owner's client-to-endpoint TLS confirmation, but it does not establish whether the provider encrypts the proxy-to-compute leg.

## G2 — Contracts and source policy

Updated contracts distinguish bundled migration inventory from the migration state of a verified Production target, explain that PostgreSQL startup does not initialize AI attempts/cooldown, and document the safe first-admission requirement. The source-policy matrix retains only `wikimedia_pageviews_top_per_country` as live AI input; no additional source was enabled. `git diff --check` passed after the documentation changes.

## G3 — Consolidated backend regression

The one consolidated isolated-schema run covered:

- `tests/test_postgres_runtime.py`
- `tests/test_postgres_operational_state.py`
- `tests/test_postgres_live_composition.py`
- `tests/test_postgres_collection_store.py`
- `tests/test_postgres_ai_engine.py`
- `tests/test_postgres_retention.py`
- `tests/test_postgres_analysis.py`
- `tests/test_incremental_fetch_persistence.py`
- `tests/test_postgres_atomic_state.py`
- `tests/test_postgres_payloads.py`
- `tests/test_postgres_scheduler.py`
- `tests/test_algorithm_engine.py`

The suite exercised persistence, incremental processing, retention, algorithms, AI classification/recommendation, proposals, recovery, and local/Vercel composition with isolated PostgreSQL and mock providers. It had no skipped required PostgreSQL tests. It did not write the root `trendscout` schema.

## G4 — Browser acceptance

- Direct loads were checked for `/`, `/keywords`, `/collections`, `/products`, `/trends`, `/sources`, `/research`, `/settings`, `/runs`, `/algorithms`, and `/quality`. Each reached the local backend and showed the corresponding active navigation destination and breadcrumb.
- The Collections page rendered two clearly labeled synthetic fixture keywords from the isolated PostgreSQL store. The dashboard bootstrap route returned successfully after the local route guard was corrected to allow `/api/v1/dashboard/bootstrap` and `/api/v1/research/bootstrap` reads.
- Product search's token matching returned the synthetic travel keyword. A mocked provider returned a synthetic recommendation; the UI displayed “preparing” progress, then a completed result with synthetic evidence and limitations.
- A browser-only aborted Research bootstrap request displayed the localized backend error state. Empty product search showed an empty result state. Sources showed provider quota as unavailable and Fetch disabled; no source provider was called.
- At 390×844, the document and body widths remained 390 px. Locale and theme toggles worked in both directions and were restored to Vietnamese/light. The first keyboard Tab focused “Skip navigation”.
- The UI exposed a breadcrumb defect on `/collections`; it is fixed in `Frontend/public/app.js`. The actual local route guard defect and its focused regression are recorded under validation.

## G5 — Quota admission, retention, and recovery

A read-only root `.env` target query found no row or field for scope `trendscout-ai-engine-v1`; specifically, `attempts` and `provider_retry_at` were absent. At the FINAL01 starting head, the missing scope became an empty attempts list and `_reserve()` could admit a request. FINAL01B changes this behavior: a missing scope is `UNINITIALIZED`, a legacy scope without verified evidence is `UNKNOWN`, usage is null in either state, and both block provider requests. PostgreSQL quota/capability reads remain read-only until the operator bootstrap is explicitly invoked. This code fix does not prove provider or shared-account quota remains.

Safe first admission requires all of the following:

1. Keep live AI disabled while the ledger or upstream provider state is unknown; report null usage and `UNKNOWN`, never zero from an absent row.
2. Reconcile the actual provider account's limit, usage window, reset boundary, and current cooldown using owner-authorized evidence. The earlier OpenRouter key metadata did not include numeric account limits, and previous attempts received 429.
3. Preserve any existing attempts, UTC-day and rolling-minute windows, retry timestamp, jobs, idempotency records, and reservations. Do not reset counters or fabricate historical timestamps.
4. Atomically record only evidence-backed current counters/cooldown in the shared PostgreSQL coordination state under the existing advisory-serialized transaction. Verify it by read-only inspection before enabling admission.
5. Permit one live request only after the shared ledger and cooldown are verified, while keeping the AI transfer allowlist restricted to Wikimedia Pageviews. AI01H's two remaining authorized requests remain unused.

PostgreSQL retention and failure-recovery coverage passed in G3. FINAL01B adds evidence validation for exclusive provider account identity, provider daily/rolling-minute limits and current use, UTC reset, cooldown, history, and reservations. The account and evidence references are stored as fingerprints. The transaction is idempotent, preserves existing attempts/reservations/cooldown, and enforces the lower provider/internal limit. Synthetic test bootstraps are marked `SYNTHETIC_TEST` and cannot establish live readiness. `Backend/scripts/bootstrap_ai_quota.py` is the operator entry point; it reads the root `.env` without process-ENV overrides and a private evidence JSON file. That command was not run because the Owner has not supplied quota/cooldown evidence and Production schema identity is unresolved.

### FINAL01B focused verification

- Branch start was `836ba3e7b39f3b9e3eb2818757bca8ee40474046`, matching `origin/integration/demo-final`; fetched `origin/main` was `5122e6f0a717f72f2933bea56978c2b66f42139a` and remained an ancestor. No merge was needed.
- `Backend/tests/test_ai_engine.py`: 59 passed, 6 subtests passed.
- Isolated PostgreSQL quota checks: 5 passed, 12 deselected, 0 skipped. Coverage included absent/legacy ledger, no provider call before admission, unknown usage null, bootstrap idempotence/concurrency, existing reservation and cooldown preservation, provider limits lower than internal limits, UTC rollover, shared service instances, and ambiguous transaction failure. The disposable `ts_quota_*` schemas were dropped by the test fixture; the root `trendscout` schema was not used by these tests.
- Ruff on changed Python files, Python `compileall`, and `git diff --check`: PASS.
- No live provider quota lookup, model verification, inference, or AI01H request was made.

## G6 — DB08 read-only preflight

### Migration inventory and observed root `.env` target

The bundled order and read-only ledger comparison are:

1. `0001_business.sql`
2. `0002_import_journal.sql`
3. `0003_coordination.sql`
4. `0004_source_payloads.sql`
5. `0005_legacy_audit.sql`
6. `0006_analysis_records.sql`
7. `0007_scheduler.sql`
8. `0008_payload_references.sql`
9. `0009_retention_audit.sql`
10. `0010_retention_lineage.sql`
11. `0011_dataset_processing_statuses.sql`
12. `0012_classification_input_digest.sql`
13. `0013_fetch_job_ai_work.sql`
14. `0014_collection_proposal_lifecycle.sql`

The root `.env` target's ledger contains versions 1–9, in order, and those entries' names and checksums match the repository prefix. This target has no Production environment marker; no claim is made that it is the Production Neon branch. Migrations 0010–0014 were not run.

### FINAL01B backup and writer inventory

- The current Windows host inventory captured five private SQLite backups: Collections, scheduler, normal Google LIVE state, normal Wikimedia LIVE state, and D6 smoke ledger. No configured local SQLite source was missing or failed capture. The source stores and their contents were preserved.
- A restore rehearsal restored all five standalone backups into in-memory SQLite databases. `integrity_check`, `foreign_key_check`, table definitions, and row counts matched for each restore. This is local SQLite evidence only; it is not a PostgreSQL, Firestore, cross-store snapshot, or application-level recovery test.
- The current host had zero TrendScout scheduled tasks, Windows services, or matching active processes. This does not verify home/company computers or Vercel workers and is not an operational freeze confirmation.
- No `.vercel/project.json` is present, and the connected Vercel account returned no TrendScout projects, so Production project/environment/connector linkage could not be verified. Firebase CLI, `pg_dump`, and `pg_restore` are unavailable on this host. No Neon or Firestore cloud backup/restore was performed. Repository SQLite capture and migration/import review tools are not evidence of remote backups.
- Fresh Start remains the selected cutover plan. The local source stores and newly captured backups remain intact; no user records or quota evidence were deleted or imported. No migration, deployment, or Production environment change was made.

### Required Production environment names

Set these in the confirmed Production runtime, without sharing their values in reports or chat:

- `DATABASE_URL` — Neon pooled URL with TLS.
- `DATABASE_URL_UNPOOLED` — direct URL with TLS; both URLs must resolve to the same intended branch, database, and role.
- `TREND_SCOUT_POSTGRES_RUNTIME_ENABLED` — explicit PostgreSQL runtime switch.
- `TREND_SCOUT_VERCEL_SHARED_LIVE_ENABLED` — explicit shared-live composition switch when the Vercel runtime is selected.
- `TREND_SCOUT_POSTGRES_SCHEMA` — target application schema (normally `trendscout`).
- `TREND_SCOUT_RUN_MODE` — required writable Local LIVE or Vercel LIVE profile when PostgreSQL runtime is enabled.
- `TREND_SCOUT_READ_ONLY` — keep enabled for preflight; writable mode requires a separate approved cutover.
- `TREND_SCOUT_PUBLIC_FETCH_ENABLED` — keep false until source permissions and quota budgets are approved.
- `TRENDSCOUT_AI_ENABLED` — keep false until G5 passes.
- `TRENDSCOUT_AI_DATA_PROFILE` — `PUBLIC_DATA` for any later public-source live use; never `PUBLIC_DEMO` in Production.
- `TRENDSCOUT_AI_ALLOWED_SOURCES` and `TRENDSCOUT_AI_TRANSFER_APPROVED_SOURCES` — separate lists; both stay restricted to the approved Wikimedia source.
- `TRENDSCOUT_AI_PUBLIC_DATA_COLLECTION_IDS` — only explicitly reviewed public Collections; otherwise empty.
- `TRENDSCOUT_AI_COLLECTION_MODEL` and `TRENDSCOUT_AI_RECOMMENDATION_MODEL` — exact verified models only, after provider readiness.
- `TRENDSCOUT_AI_PUBLIC_DEMO_SCHEMA` — unset in Production; the synthetic demo profile is local/test only.
- `OPENROUTER_API_KEY` — server-side secret; do not expose to the browser or report.

The deployment bundle must include the migration runner and all 14 SQL files. Verify the deployed artifact contains the exact code and migration checksums before any operator migration. PostgreSQL startup must not be used as a substitute for the ordered migration action.

### Owner actions before cutover

1. Identify and record the Production Neon project, branch, database, schema, and role through the owner's control plane. Confirm the root `.env` target is that Production identity without disclosing its DSNs; no Production project linkage was visible in the current Vercel account.
2. Keep the five captured local SQLite backups. Repeat inventory on each other home/company Windows host and obtain timestamped, integrity-checked Firestore and PostgreSQL backups through their owner control planes. Preserve the source stores; do not delete or import historical data as part of Fresh Start.
3. Rehearse PostgreSQL restore into an isolated schema/database and prove application reads, checksums, and recovery. Record the restore result and responsible operator.
4. Freeze legacy SQLite/Firestore writers and schedulers on every Windows host and deployment, including Vercel. Record the actual freeze and verify no old process can write after the cutover boundary.
5. Reconcile the exclusive provider account identity, 24-hour daily and rolling-minute limits/use, next UTC reset, current cooldown, historical provider activity, internal attempts, and existing reservations. Evidence must be observed within five minutes of bootstrap. Leave AI disabled while any provider window or shared ledger is unknown.
6. After Production identity is confirmed and a separate authorized write window is open, create the private evidence JSON and run `python Backend/scripts/bootstrap_ai_quota.py --schema <confirmed-production-schema> --evidence-file <private-owner-evidence.json>`. The evidence booleans for cooldown, provider activity/history, reservations, and account exclusivity must be verified; the command initializes shared AI quota state and was not run in FINAL01B.
7. After owner verification and a separate approved cutover window, apply migrations 0010–0014 in order using the reviewed bundle; verify all 1–14 checksums and current schema identity read-only afterward.
8. Set the confirmed Production environment values, start with read-only runtime and Fetch/AI disabled, then run the read-only smoke checklist below.
9. Only after smoke passes, source permissions are confirmed, and a controlled Fetch window is approved, run the first explicit Fetch All. Select only sources that are authorized and within known provider budgets; a Wikimedia-only first run is the conservative option if it is the only source ready. Observe each source status and the terminal run/snapshot before any second run.

### Production smoke and rollback checklist

Read-only smoke: confirm Production branch/database/schema/role; `GET /api/v1/runtime`; `GET /api/v1/capabilities`; `GET /api/v1/dashboard/bootstrap`; `GET /api/v1/sources/library`; `GET /api/v1/research/bootstrap`; and one saved-keyword detail read. Confirm the migration ledger is exactly 1–14 with matching checksums, Fresh Start emptiness is represented as empty rather than fabricated zero trends, source quotas remain visible as unknown when unverified, AI remains disabled, and no provider request was issued.

Stop before Fetch if the identity, migration checksum, backup/restore rehearsal, legacy-writer freeze, quota evidence, or smoke result differs from this plan. Keep writes and schedulers disabled; preserve both databases and logs. If failure occurs after PostgreSQL writes begin, pause all writers and reconcile the durable PostgreSQL state before choosing a runtime switch. Do not send new writes to a stale legacy store, reverse migrations, or delete either store as an automatic rollback.

### Known performance limitation

DB06 remains unchanged: unchanged 1,500-record Fetch median was 3.199 s with zero payload bytes read. Changed-input cases were 18.453 s (1%), 20.925 s (10%), and 28.152 s (100%), each above the 5 s target. No optimization was attempted in FINAL01; the measurements are from isolated CACHE_ONLY runs and not Production or provider latency.

## Related acceptance status

| Task | Status at FINAL01 handoff |
|---|---|
| SEC01 | 100% PASS after the Owner's rotation/invalidation confirmation; report updated and committed separately. |
| AI01H | G1, G2, G3, G5, G6 PASS; G4 BLOCKED after provider 429 responses and unverified shared quota/cooldown. Two remaining authorized inference requests were not used. |
| AI06 | G1–G6 PASS in prior acceptance; PostgreSQL recommendations, persistence, quota, cache, and recovery evidence retained. |
| AI07 | G1–G6 PASS in prior acceptance; 71 frontend tests passed against mocks. |
| AI08 | G1–G6 PASS in prior acceptance; previously committed on the integration branch. |

## FINAL01 validation and delivery

- `Backend/tests/test_local_live_product_routes.py`: 1 passed, 16 subtests passed. This regression covers allowing the dashboard and research bootstrap reads through the local guard.
- `Frontend/tests/router.test.mjs`: 5 passed, 0 failed, 0 skipped.
- `node --check Frontend/public/app.js`: passed.
- Ruff on changed Python files: passed.
- Python compileall on changed Python files: passed.
- Consolidated PostgreSQL regression: 92 passed, 10 subtests passed, 0 skipped, 1 warning.
- The final staged diff passed `git diff --cached --check`; commit, push, and remote SHA are recorded in the delivery handoff.
