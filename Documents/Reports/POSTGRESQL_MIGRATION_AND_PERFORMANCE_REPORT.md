# PostgreSQL migration and performance — working report

**Status: migration in progress; no cutover.** Updated 26 November 2026. This
report tracks the active 12-gate migration goal. Three gates have passed:
**3 / 12 (25%)**.

## Production dependency regression recheck — 26 November 2026, 04:34 UTC

Authenticated GETs to the Production alias returned HTTP 200 from
`/api/health` and HTTP 403 `FIRESTORE_IDENTITY_REJECTED` from
`/api/v1/capabilities` and `/api/v1/datasets/latest`. Vercel request IDs
reported `sin1:iad1`. A runtime-log query for the known `cf09b2e` deployment,
covering the preceding 30 minutes and warning/error/fatal levels, returned no
records. `Backend/app/main.py` maps every Firestore client-construction
exception to this same 403 code, so the response does not identify the
exception class.

Source evidence still makes the omitted Firestore package a strong regression
candidate: `cf09b2e` placed `google-cloud-firestore` in an optional extra while
the legacy Vercel path still constructs a Firestore client. The exact runtime
exception remains unverified because no matching log record was available. A
compatible, non-alias deployment on `7faacb8` previously returned 200 for the
same routes. No Production promotion, alias change, Fetch, or provider call was
made. The most recent alias mapping readback in this report remains
`cf09b2e`.

## Production dependency regression and recovery — 26 November 2026

Owner pushed `cf09b2e0402a22dcb07dbd422f4f52e9dcdd5af4` to `origin/main`, and
Vercel created READY Production deployment `dpl_8khsVQLtwEmBqapb2Gum3k4nJmXx`
from that SHA in `iad1`. Authorized, read-only Production GETs returned 200 for
`/api/health`, but 403 `FIRESTORE_IDENTITY_REJECTED` for
`/api/v1/capabilities` and `/api/v1/datasets/latest`.

The code/package path identifies a concrete regression candidate: `cf09b2e`
moved `google-cloud-firestore` and its pinned support packages out of the
default project dependencies into the `legacy-firestore` extra. The Vercel
FastAPI build reads dependencies from `pyproject.toml`; before this correction,
`Backend/vercel.json` did not install that extra. The legacy request path
imports `google.cloud.firestore` while constructing the Firestore client, and
the middleware maps any client-construction exception to the observed 403.
Vercel's Hobby runtime-log query did not provide retained logs for the
deployment window, so the exact exception class could not be read; a WIF token
or identity configuration error is not ruled out solely by the HTTP response.

Compatible deployment `dpl_4nrYBmDRyoKDVgYg97Mbub9CDCjs` (SHA
`7faacb8cd9ee47c52bdbee0cc8318a1ca4b66c2a`) was checked before recovery:
health, capabilities, and latest-dataset GETs all returned 200. The Production
alias `trend-scout-mu.vercel.app` was then assigned back to that deployment and
read back from Vercel. The same three GETs against the Production alias returned
200 afterward. No fetch POST, provider request, PostgreSQL runtime switch, IAM
change, or data write was made. The `cf09b2e` commit and its deployment remain intact.

An initial source correction added a custom `installCommand` for the
`.[legacy-firestore]` extra while this codebase still used the legacy Firestore
path. Its Preview build later failed the project's function-size limit. The
follow-up correction removed that command and put the pinned Firestore SDK
stack back in normal project dependencies. Preview `dpl_Fy8661oca8SH43Lzqw2PL7GjAgxK`
for commit `361a167019c4befefad35a26d02bfd2e54d9565d` is now READY. Its build
used standard dependency installation, optimized a 245.45 MB bundle, and
completed. PostgreSQL remains disabled until business and operational data,
writer fencing, and cutover gates are complete. Firestore packages can leave
the Vercel runtime only after that cutover is verified. Vercel's [Python
runtime documentation](https://vercel.com/docs/functions/runtimes/python) and
[project install configuration](https://vercel.com/docs/project-configuration)
were checked on 26 November 2026.

## Production regression recheck before packaging correction — 26 November 2026

Read-only Vercel metadata currently maps `trend-scout-mu.vercel.app` to READY
Production deployment `dpl_AizETu6tMZXvScnxWTSLa22GXW8j`, source SHA
`cf09b2e0402a22dcb07dbd422f4f52e9dcdd5af4`, region `iad1`. Fresh authenticated
GETs returned HTTP 200 for `/api/health`, and HTTP 403
`FIRESTORE_IDENTITY_REJECTED` for `/api/v1/capabilities` and
`/api/v1/datasets/latest`. The response request IDs include the `sin1:iad1`
edge/function route.

The previous compatible deployment `dpl_4nrYBmDRyoKDVgYg97Mbub9CDCjs` (SHA
`7faacb8cd9ee47c52bdbee0cc8318a1ca4b66c2a`) was checked directly: the same
three GETs returned HTTP 200 there. That verifies a compatible release is
available; it does not restore the Production alias. The current instruction
explicitly disallows a new Production deployment or alias change, so the alias
was left on `cf09b2e`. No Fetch POST, provider call, IAM change, or data write
was made.

The optional Firestore SDK packaging change remains the leading code-level
regression candidate: `cf09b2e` moved `google-cloud-firestore` into the
`legacy-firestore` extra, while the legacy request path still constructs a
Firestore client. At the time of this recheck, the feature branch added a
custom install command for that extra but had not been built. A read-only 30-minute
runtime-log query returned no matching entries and the two request trace
lookups returned 404, so the exact exception is still unknown; a credential or
identity failure is not ruled out. No IAM workaround or authentication bypass
was attempted.

A further authenticated, GET-only check during the current goal resume again
returned HTTP 200 for `/api/health` and HTTP 403
`FIRESTORE_IDENTITY_REJECTED` for both `/api/v1/capabilities` and
`/api/v1/datasets/latest`. The response IDs again show the `sin1:iad1` route.
This reconfirms the incident; it does not identify the exact client-construction
exception. No Fetch, provider request, IAM change, deployment, or alias change
was made.

## Production dependency investigation — 26 November 2026

Fresh deployment metadata confirms `trend-scout-mu.vercel.app` aliases to
READY deployment `dpl_AizETu6tMZXvScnxWTSLa22GXW8j`, built from
`cf09b2e0402a22dcb07dbd422f4f52e9dcdd5af4`. Deployment
`dpl_8khsVQLtwEmBqapb2Gum3k4nJmXx` is also READY from the same SHA but has no
Production alias. Authenticated, GET-only requests at 03:21 UTC returned JSON
HTTP 200 for `/api/health`, and JSON HTTP 403
`FIRESTORE_IDENTITY_REJECTED` for `/api/v1/capabilities` and
`/api/v1/datasets/latest`. The API responses were `no-store` cache misses with
`x-vercel-id` showing the `sin1` edge and `iad1` function route. An unauthenticated
direct request instead returned cached HTML (`text/html`, `HIT`), so it is not
valid evidence about the API and is excluded from the status assessment.

The deployment build log says Vercel installed required dependencies from
`pyproject.toml`; it does not show a custom package-install override. The exact
`cf09b2e` source confirms that its `Backend/vercel.json` had no
`installCommand`, while `google-cloud-firestore` was absent from the default
`project.dependencies` and present only in `project.optional-dependencies` as
`legacy-firestore`. The deployed legacy path imports
`google.cloud.firestore` while constructing its request-scoped client, and the
middleware maps any client-construction exception to the observed 403. This
build/configuration evidence makes omission of the optional Firestore SDK the
leading and concrete regression cause; the exact runtime exception remains
unproven because Vercel Hobby log retention rejected the wider query and a
bounded query after reproduction returned no matching log records. Vercel's
official [Python runtime dependency documentation](https://vercel.com/docs/functions/runtimes/python)
and [Install Command configuration](https://vercel.com/docs/builds/configure-a-build)
were checked on 26 November 2026.

## Safe Git resume check — 26 November 2026

After `git fetch origin`, `origin/main` remains at
`cf09b2e0402a22dcb07dbd422f4f52e9dcdd5af4`. The goal branch is
`feature/postgresql` at `6fcfc95e1ab9b7ea56b42045b17c9aaf469db107`, matching
`origin/feature/postgresql` with four commits after `origin/main`. The working
tree contains the existing source-lineage implementation and report edits;
they were preserved. No branch switch, merge, reset, or force update occurred.
The other three local goal worktrees remain at `cf09b2e`; the fetch exposed no
remote AI, Algorithms, or Frontend contract branch requiring reconciliation.

At this checkpoint, the feature branch added this install command to
`Backend/vercel.json`:

```text
python3 -m pip install -e '.[legacy-firestore]'
```

The then-current packaging tests asserted the command and extra. The focused
packaging and legacy Vercel path suite passed 55 tests and 59 subtests. The
next Preview build found that this approach exceeded the function size limit;
see the later Preview packaging section. No Production deployment, alias
change, IAM change, Fetch, or provider request was made; Production still
returns the two 403 responses.

## Latest read-only Production recheck — 26 November 2026

Vercel alias metadata currently maps `trend-scout-mu.vercel.app` to READY
deployment `dpl_AizETu6tMZXvScnxWTSLa22GXW8j`, built from
`cf09b2e0402a22dcb07dbd422f4f52e9dcdd5af4` and configured for `iad1`. Fresh,
authenticated GETs at 03:56 UTC returned JSON HTTP 200 for `/api/health` and
HTTP 403 `FIRESTORE_IDENTITY_REJECTED` for `/api/v1/capabilities` and
`/api/v1/datasets/latest`. The latter responses were `no-store` cache misses;
their `x-vercel-id` values again show the `sin1` edge and `iad1` function
route. This confirms the Production regression is still present. It does not
identify the exact runtime exception or rule out a separate identity failure.

The previously verified compatible deployment `dpl_4nrYBmDRyoKDVgYg97Mbub9CDCjs`
remains READY on `7faacb8cd9ee47c52bdbee0cc8318a1ca4b66c2a`. Fresh authenticated
GETs to that deployment at 04:00 UTC returned HTTP 200 for health, capabilities,
and latest dataset. This confirms a known working release is still available;
it is not the current alias. Comparing it with the current `cf09b2e` deployment
strengthens the dependency-packaging regression hypothesis but does not prove
the exact exception.

The earlier report entry records a temporary alias restoration to compatible
deployment `dpl_4nrYBmDRyoKDVgYg97Mbub9CDCjs` on `7faacb8`; that is not the
current alias. The latest branch-only instruction does not authorize a new
Production release or alias mutation, so no promotion, rollback, deployment,
Fetch POST, IAM change, or data write was made. At this check, the Firestore
extra correction had not yet been built.

## Preview packaging validation and correction — 26 November 2026

Pushing commit `40bd77bd19c9f1ef40ced851fc79322a54d453fc` to the authorized
`feature/postgresql` branch triggered Vercel Preview deployment
`dpl_B792ELGgj8gtTP8ZB2UbfFApijCM`. It finished in `ERROR` before serving
traffic: the build reported a 246.78 MB function bundle against the project's
225 MB limit and identified the custom install command as preventing its
automatic bundle optimization. Production aliases were not assigned to this
Preview and were not changed.

Commit `361a167` removes the custom `installCommand` and restores
the pinned Firestore SDK/support packages to `[project].dependencies`, which
Vercel installs through its standard Python build. This matches the dependency
layout used by the previously compatible `7faacb8` release. `requirements.txt`
now adds only the local Firebase Admin and Uvicorn extras. These packages must
remain in the Vercel dependency set while its live composition still uses
Firestore; they can move to a local-only extra after PostgreSQL cutover.
The successful `7faacb8` build log had optimized a 235.75 MB pre-optimization
bundle. The corrected `361a167` Preview independently reports 245.45 MB before
automatic dependency optimization and reached READY. The Production alias
readback still points to `cf09b2e`; the Preview has only the feature-branch
alias. No API route was called on that Preview, and no Production deployment,
alias change, database write, or provider request occurred. The affected
packaging and legacy runtime suite passed 25 tests and 16 subtests; Ruff,
Python compilation, `pip check`, and `git diff --check` passed. The failed
Preview `dpl_B792ELGgj8gtTP8ZB2UbfFApijCM` is already terminal and needed no
cancellation.

## Rolling retention implementation — G6 subcriterion, not a new gate

The owner's seven-local-calendar-day policy is defined in
`app/retention/policy.py`: on date D, keep D-6 through D in
`Asia/Ho_Chi_Minh`; store instants as UTC. Provider expiry shortens that
deadline. The Postgres retention runner is bounded to 500 payloads per batch,
uses a schema-scoped transaction advisory lock and `SKIP LOCKED`, releases
expired payload-reference rows and deletes their blobs in one transaction,
records an aggregate audit row, supports dry run, and reports remaining
payload backlog. Missed batches can continue on a later invocation. It makes
no provider requests and is reached opportunistically from the explicit
PostgreSQL LIVE composition. No Vercel Cron or paid service was added.

The PostgreSQL Fetch path now writes a deterministic `source_evidence` index
for newly collected provider records in the same transaction as each dataset
snapshot. It records the content digest, provider and query scope, explicitly
scoped geography, an aware collection timestamp, an aware observation
timestamp when one exists, and the seven-day policy deadline. Date-only
observations remain text in `query_scope`; no instant is invented. Evidence
payloads stay null, and dataset/keyword relationships are idempotent. Google
records and attached Bing verifications are indexed as separate provider
evidence. The combined Wikimedia/Wikidata entity-context copy is indexed in
the same transaction as its `entity_context` upsert and linked to the keyword;
when returned during Fetch, it is also linked to that dataset snapshot. Records
without an explicit aware per-record collection timestamp are not assigned a
guessed one; PostgreSQL snapshots carry the
`SOURCE_EVIDENCE_LINEAGE_INCOMPLETE` quality flag. This adds provenance for
future cleanup but does not migrate historical snapshots or remove any
provider content copies.

Provider-policy documentation was checked on 26 November 2026. YouTube's
Developer Policies generally require non-authorized Search API data to be
deleted or refreshed after 30 calendar days. Longer storage for some
statistical data applies only to accepted clients and does not cover titles,
creator names, descriptions, or comment text; this project has no approved
exception. The application's seven-local-date policy is shorter. SerpApi's
Legal and FAQ pages say its own search files expire after 31 days on SerpApi's
service; this is not a client-side retention ceiling. Wikimedia's API guidance
requires license compliance and sets API-use limits; it does not state a
shorter numeric client-retention period for the checked public Pageviews API.
Google Trends RSS and Wikidata-specific client terms still need review. See
the official [YouTube Developer Policies](https://developers.google.com/youtube/terms/developer-policies),
[YouTube derived-metrics policy](https://developers.google.com/youtube/terms/derived-metrics-policy),
[SerpApi Legal Documents](https://serpapi.com/legal), [SerpApi FAQ](https://serpapi.com/faq),
and [Wikimedia API Usage Guidelines](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_API_Usage_Guidelines/en),
checked 26 November 2026.

The versioned registry in `app/retention/source_policy.py` covers every
catalog source plus the internal Wikimedia entity-context alias. It applies a
provider maximum when one is known and stores version, review status, and
limit under `sources.configuration.retention_policy`. Google/Bing and
Wikimedia/Wikidata context remain marked `partial` where terms need more
review. New lineage uses the registry to calculate
`source_evidence.expires_at`; unknown sources are not silently defaulted.
Replays of an existing evidence identity can shorten but never extend its
stored expiry. No schema migration was needed. This control does not establish
complete terms coverage or redaction of all provider-derived copies.

The runner deliberately does **not** delete `source_evidence` yet. It reports
rows whose `collected_at` or provider `expires_at` is due, but the current
schema also stores provider-derived content in keyword and classification
JSON, algorithm values, and saved research without complete field-level source
lineage. `algorithm_observations` has no reliable collection timestamp. A
delete could therefore leave expired content in derived or saved rows, or
invalidate user assignments. Those rows remain intact until the shared schema
has reliable collection timestamps, source references, and dependent-record
redaction/invalidation contracts. No user Collections, Research, settings,
quota, reservation, cooldown, or refresh-eligibility state is age-purged.
Current YouTube cache limits remain unchanged and are not extended by this
default policy.

Focused offline coverage for the new lineage builder, Fetch snapshot path and
entity-context lineage passed 8 tests; its isolated PostgreSQL transaction test was skipped because
`TRENDSCOUT_TEST_POSTGRES_URL` was not configured. Existing collection tests
passed 38 tests and 7 subtests; 7 PostgreSQL-backed cases were skipped for the
same missing test URL. No Neon connection or Production Fetch was made.

`Documents/Integration/DB_CONTRACT.md` publishes the database ownership and
cross-goal storage/migration requirements. At this fetch there were no remote
AI, Algorithms, or Frontend contract branches to reconcile. The retention
acceptance subcriterion **G6-R** requires end-to-end expiry for every provider
content copy, provider-specific shorter limits, no orphan references,
transactional/idempotent bounded cleanup, durable audit and dry-run evidence,
backlog recovery, and proof that protected user/operational rows survive. It
does not add a thirteenth gate; G6 remains open.

Vercel Cron was assessed against the official docs on 26 November 2026. Cron
invokes Vercel Functions and is available on all plans; Hobby permits one run
per day with up to 59 minutes of timing variance, schedules use UTC, and
invocations consume Function usage. The Cron feature is marked legacy and is
planned to migrate to Schedules. The connected project metadata did not verify
the team's plan or remaining usage, so this branch uses opportunistic cleanup
and does not configure a schedule. See Vercel's [Cron behavior and timezone
documentation](https://vercel.com/docs/cron-jobs) and [Cron usage and pricing
limits](https://vercel.com/docs/cron-jobs/usage-and-pricing). No Cron was
configured or deployed.

Migration 9 adds the aggregate retention-audit table and expiry indexes. It
was applied to Neon only after confirming the existing `trendscout` ledger was
exactly versions 1–8 and all 34 existing non-ledger tables were empty. The
checksum ledger now verifies versions 1–9; the schema has 36 tables total,
including 35 non-ledger tables, all still empty. The new audit table is empty,
and no runtime or Production deployment was switched. An isolated Neon test
found that the process-local sweep throttle could skip a follow-up batch while
backlog remained; that behavior is fixed. The same test now passes for dry-run,
backlog continuation, the `retain_until <= now` expiry boundary, durable audit,
and preservation of saved and operational rows.

## Fail-closed PostgreSQL activation guard — 26 November 2026

The explicit PostgreSQL LIVE composition and Vercel app construction now
validate readiness before exposing PostgreSQL-backed runtime stores. The
readiness query requires at least one migrated keyword row and dataset snapshot,
plus the normal Google LIVE coordination policy digest and quota entries. Empty
schema-only PostgreSQL therefore cannot be selected as the runtime store. This
is a minimum fail-closed guard; it does not prove that every SQLite/Firestore
domain, payload, user edit, reservation, job, or schedule has been reconciled.
No PostgreSQL flag was enabled and no production deployment was made for this
change. The isolated composition fixture now seeds explicit synthetic readiness
rows, with no provider requests. The affected-area suite and isolated Neon
retention integration now pass; the Firestore emulator suite remains unrun.

The Windows inventory utility described below has been added with an owner-only
backup directory and SQLite online-backup capture. A first-host capture is now
available below; the second Windows host and Production Firestore still need
separate authorized inventories. G1 and G3 remain open until these captures can
be reconciled.

## Verified starting point

At the previous report checkpoint, after `git fetch origin`, local `main` and `origin/main` resolved to
`47a4b54db6883f5e5fbe7ead0bc45c4cab8f4726`. The existing untracked
`.playwright-cli/` directory was left untouched. Read-only Vercel metadata on
26 November 2026 confirmed that `trend-scout-mu.vercel.app` aliases to READY
Production deployment `dpl_4nrYBmDRyoKDVgYg97Mbub9CDCjs`, built from SHA
`7faacb8cd9ee47c52bdbee0cc8318a1ca4b66c2a`; its deployment region setting is
`iad1`. A later Production-target deployment, `dpl_93G1DMmsz5UpfRtVJESKJDWCPpnK`,
for SHA `47a4b54db6883f5e5fbe7ead0bc45c4cab8f4726` is `CANCELED`; the production
hostname remains aliased to the earlier READY deployment. The Vercel project
contains Production `DATABASE_URL` and `DATABASE_URL_UNPOOLED` variables with
Sensitive type; no values were decrypted. The deployment region setting does
not prove the region of an actual function invocation, which remains to be
measured. Historical performance measurements are in
[the performance report](VERCEL_PERFORMANCE_SCALING_REPORT.md).

The local `DATABASE_URL` and `DATABASE_URL_UNPOOLED` both connected using
client-side TLS. They returned the same PostgreSQL system identifier, database
name and role, and their normalized pooled/direct endpoint names matched. The
server reported PostgreSQL 18.7, database size 7,790,592 bytes, and database
CREATE privilege. The Neon endpoint region token is `ap-southeast-1`; the
Production deployment metadata lists `iad1`, so the configured database and
function regions differ. No region change has been made. Runtime invocation
region and Vercel-to-Neon latency remain unmeasured. This is a point-in-time
connectivity check, not proof of project capacity, schema permissions or future
availability. The database-side `pg_stat_ssl` result was false while the client
reported TLS; the client-to-endpoint connection is encrypted, but this check
does not establish encryption for every internal provider hop. No schema or
business data was written during that connectivity check.

An independent connection foundation now selects pooled or direct URLs,
requires `verify-full` TLS with the existing pinned Certifi CA bundle, disables
automatic prepared statements for pooled compatibility and closes each
connection. Both configured URLs passed a read-only `SELECT 1` through this
factory with certificate verification on this machine. Three targeted unit
tests and Ruff passed. It is not yet integrated with runtime, schema migration,
transaction timeouts, retry/idempotency or application-managed pooling. No gate
passes on the basis of this connection foundation alone.

The first versioned PostgreSQL business-schema migration and an explicit
migration runner are now present locally. The schema has distinct business
tables and source/evidence relationships, rather than database-image blobs.
The runner serializes schema changes, records checksums, rejects changed or
unknown history, and rolls failed changes back. A targeted real-PostgreSQL
test applied and reapplied the migration inside a randomly named schema in an
outer transaction that was rolled back; it also tested checksum mismatch and
failed-DDL rollback. No durable schema or business data was created by that
test. Six targeted tests passed. The schema remains preliminary: mapping and
reconciliation against every actual business and operational record, runtime
repositories and complete data migration remain unfinished, and the schema has
not been certified against all existing data.

On 26 November 2026, after a read-only check confirmed that the target
`trendscout` schema was absent and empty, the authorized migration CLI applied
versions 1–7 to that schema in the configured Neon database. A follow-up
`verify_migrations` check confirmed the complete checksum ledger and all 34
tables; only `_schema_migrations` contains rows (seven ledger entries). No
business rows were imported, and no application runtime uses this schema yet.
Together with the verified pooled/direct connectivity and connection
configuration above, this completes G2's schema/foundation scope. Runtime
integration is tracked separately under G5.

### Payload-reference lifecycle — 26 November 2026

The expired-payload cleanup path now respects durable state references. New
versioned migration 8 adds a reference index with a restrictive foreign key to
payload blobs. PostgreSQL coordination writes replace that index in the same
transaction as cache and source-checkpoint state, recording cache retention and
the earlier of checkpoint and job deadlines. The bounded cleanup query skips
live references, removes expired reference rows with expired blobs, and uses
`FOR UPDATE SKIP LOCKED`; the foreign key also prevents a concurrent reference
from becoming dangling.

Migration 8 was applied to `trendscout` and checksum-verified. The schema now
has 35 tables and an eight-row migration ledger. A read-only follow-up counted
all 34 non-ledger tables: zero rows in each, including payload blobs and the new
reference table; no isolated test schemas remained. Ten focused PostgreSQL
foundation tests passed against isolated schemas, including a checkpoint
reference blocking cleanup until the checkpoint is removed. Ruff passed.
After adding the candidate-specific deletion method used by checkpoint cleanup,
the two-test payload module passed again against isolated PostgreSQL.
This adapter work is not yet wired into the running application, and does not
complete G6 or G7.

### Local PostgreSQL runtime composition — 26 November 2026

The explicitly composed Local Google LIVE facade now exposes its selected
collection store to `create_app`. When that facade is PostgreSQL-backed, Local
Collections, Run History reads, and Scheduler storage use PostgreSQL analysis
and scheduler stores from the same configured schema. The Local application
does not replace the PostgreSQL collection store with its legacy SQLite store.
The existing non-PostgreSQL Local LIVE composition continues to provide its
own store during the migration period.

An isolated Neon schema integration test constructed the facade and entered
the application lifespan. It verified the PostgreSQL collection, analysis and
scheduler stores were selected, while the credential provider failed on any
attempt to read credentials. The test passed (1 test); the schema was removed.
The focused Local LIVE route, run and scheduler regression tests passed (24
tests and 14 subtests). Ruff and Python compilation passed. A read-only
post-test check found no leftover `ts_test_*` schema, 35 tables in `trendscout`,
eight migration ledger rows, and zero rows across the other 34 tables.

This verifies the Local composition boundary only. It does not verify Vercel
PostgreSQL route composition, cross-host transactions, migrated operational
state, or a Production runtime switch.

A read-only runtime inventory confirms the default Local LIVE composition uses
`LocalSQLiteAtomicStateStore`, while Vercel constructs a request-scoped
Firestore client and uses Firestore for coordination. The normal Local Google
LIVE composition now also accepts an explicit PostgreSQL environment, verifies
pooled/direct database identity and the bundled migration ledger, and constructs
PostgreSQL coordination, payload and collection stores. Its isolated Neon test constructs without reading
provider credentials. `create_app` and the deployed Vercel middleware do not
select this path yet; no application runtime was switched. The focused suite
covering this composition, payloads, Local Google contracts and YouTube
retention and synthetic Firestore staging passed 47 tests and 2 subtests; Ruff
passed. After batching reference writes, the expanded focused PostgreSQL and
runtime-contract suite passed 55 tests and 2 subtests; Ruff passed again. The
isolated composition test passed once more after requiring pooled/direct
database identity verification.

An unfinished SQLite import utility now supports standalone immutable backups,
type-checked dry runs, conflict checks, transactional batches, an import
journal, restart, and post-import row comparison. It uses PostgreSQL rows, not
SQLite images; JSON duplicate keys and timestamps lacking a timezone are
rejected rather than silently converted. A synthetic integration exercise on
an isolated PostgreSQL schema covered interruption/restart, replay, conflict
rejection, parent-before-child insertion and preservation of a precise JSON
decimal. The isolated test schema was deleted after the test. This is not yet
the final production migration: full-source dry-run, complete data/operational
reconciliation, writer fencing, rollback and runtime integration remain open.
Local validation inspected 62,340 JSON values from the saved Production
snapshot and found no malformed values or duplicate object keys; that does not
replace the PostgreSQL dry-run or establish semantic equivalence.

The snapshot reader's original backup had an empty WAL sidecar, which the
import utility correctly refused to ignore. Separate consistent standalone
SQLite backups were created with SQLite's backup API for the inspected
Production image and this machine's local Collections database. Both passed
SQLite integrity and foreign-key checks. Counts and bidirectional SQL `EXCEPT`
comparisons found no row differences across all 18 tables between the original
Production snapshot and its standalone copy. A subsequent dry-run staged every row
into temporary tables against an empty, isolated PostgreSQL schema: Production
18 tables / 55,332 rows passed in 10.5 seconds; local 18 tables / 50,783 rows
passed in 12.5 seconds. After preserving the algorithm JSON string as text, the
current schema was checked again: Production passed in 15.8 seconds and local
in 14.8 seconds with the same counts. The isolated schemas were removed. These are individual
development-machine timings, not Production performance measurements. They
prove type staging against an empty target, not a successful merge, import,
row-by-row reconciliation or capacity for full persistent data. The original
backups and SQLite databases remain in place.

The prototype CLI can provision an explicitly named schema and inspect a
standalone backup without asking the operator to write SQL. It reads the two
database URLs from the process environment or the private root `.env`, checks
that their normalized Neon endpoints, cluster identifiers and database names
match, and uses verified TLS. Example commands, from the repository root:

```powershell
Backend\.venv\Scripts\python.exe Backend\scripts\postgres_migrate.py schema --schema ts_migration_staging
Backend\.venv\Scripts\python.exe Backend\scripts\postgres_migrate.py dry-run --schema ts_migration_staging --backup 'C:\path\to\backup.sqlite'
```

The identity check was exercised against both local Neon URLs. The migration
runner was tested in an isolated rolled-back schema, then applied to the
previously absent, empty `trendscout` schema as described above. The
migration/import tests use synthetic rows for restart and conflict cases. These
checks do not verify the sensitive environment configured in Vercel.

An unwired PostgreSQL implementation of the existing atomic-state port now
stores top-level fields, map entries and per-job source checkpoints separately;
only changed entries are written. It uses a transaction-scoped per-scope lock,
bounded transaction settings, repeatable-read batch reads and conservative
unknown-commit handling. Focused tests on an isolated PostgreSQL schema with
synthetic data checked concurrent quota reservations, exclusive job claims,
lease fencing, preservation of an unchanged checkpoint, and boolean versus
numeric state. Existing Firestore atomic-state and guarded-attempt tests also
passed. An initial claim test incorrectly assumed a second admission could not
coalesce with the first; it was corrected to use a separate state scope and
the affected test passed on rerun. This adapter still reconstructs a complete
scope for callbacks, and is not yet the targeted read/query design or a
replacement for Firestore runtime. Production state migration, old-writer
fencing, actual provider behavior and cross-host cutover are unverified.

An unwired content-addressed PostgreSQL source-payload adapter stores bounded
JSON bytes and their original expiry, validates digest/count on read and rejects
the legacy SQLite snapshot encodings. Its legacy-import operation refuses a
different retention deadline rather than extending it. Synthetic PostgreSQL
tests passed for payload integrity and fresh/stale/expired cache delivery.
An existing Windows multiprocessing cache test failed when pytest was launched
from stdin because spawned processes could not import `<stdin>`; rerunning the
affected cache file with `python -m pytest` passed all 11 tests and 14 subtests.
This is not a retention-deletion implementation or a completed Firestore import.

A preliminary Firestore coordination capture/staging utility now reads the
legacy documents, checks referenced payload digests and exact retention, and
rechecks document and manifest fingerprints around staging. It refuses active
leases/reservations, changed source state, conflicting target state and
credential-shaped fields. Expired payloads are not read or refreshed; their
original references remain in staged state, where they remain unavailable.
The SQLite snapshot pointer is represented only by an audit digest, never by
copying its database image into PostgreSQL. Synthetic tests against an isolated
PostgreSQL schema passed for idempotent staging, exact quota preservation,
payload expiry and rejection paths. A read-only capture of the current
Firestore coordination returned 17 documents, one unexpired referenced payload
and two expired references. No actual Firestore state was staged into the
business schema. This utility does not capture every Firestore namespace,
fence old writers, reconcile the second machine or establish a safe cutover;
concurrent legacy writers could still change state immediately after a check.

The captured Production coordination was subsequently staged and semantically
compared in a randomly named isolated PostgreSQL schema: 17 documents and one
unexpired payload reconciled; two already-expired payload references were not
revived. The test took 27.5 seconds on this development machine and removed the
isolated schema afterward. Firestore and Production business tables were not
changed. This is a point-in-time coordination exercise, not proof that legacy
writers are fenced or that other Firestore namespaces have migrated.

The separate `analysis_runs` namespace was captured using the public Firestore
`RunQuery` protobuf API at one server-provided read time, rather than the SDK's
`DocumentSnapshot.to_dict()` deepcopy, which can discard nanosecond precision.
The [RunQuery documentation](https://docs.cloud.google.com/python/docs/reference/firestore/latest/google.cloud.firestore_v1.types.RunQueryRequest)
(accessed 25 November 2026) documents the read-time constraint. The capture
contained one run and 226 records with 8,719 timestamps; raw protobuf inspection
found none with submicrosecond precision at this point in time. A synthetic
test separately verified preservation of nonzero nanoseconds. A real-data
staging and reconciliation exercise in an isolated PostgreSQL schema passed
for all 227 documents in 19.0 seconds; the schema was then removed. Firestore
was not changed. An unwired PostgreSQL analysis repository now implements the
existing analysis storage contract, including cursor pagination and preservation
of timestamp precision. Its isolated real-PostgreSQL contract and staging
tests passed (10 targeted tests); it is not yet composed into either runtime.
Unknown nested namespaces, the final write fence and Production migration
remain open.

The PostgreSQL scheduler schema and an unwired schedule repository now use
transactional row claims and a monotonically increasing occurrence fence.
An isolated synthetic PostgreSQL test passed concurrent claim, recovery of the
same occurrence and rejection of a stale worker's completion. The existing
SQLite scheduler's 10 focused tests passed after extracting the shared worker
and calendar helpers. A separate standalone SQLite scheduler importer now
supports dry-run, idempotent batches and row comparison; it refuses any
`RUNNING` occurrence pending explicit reconciliation. A synthetic isolated
PostgreSQL import/replay test passed. Scheduler persistence has not been composed
into Production, actual schedules have not been imported, and no automatic LIVE
execution was enabled. Fencing completion alone does not prove duplicate
provider calls are impossible: the eventual scheduled Fetch must also use the
occurrence identity for idempotent admission and be tested end to end.
The worker now runs blocking storage calls off the event loop. Its 10 existing
focused tests passed again. Ruff passes on the affected files; a broader check
of the entire scheduling directory also reported 10 existing style violations
in untouched `dispatch.py` and `recurrence.py`.

Preliminary PostgreSQL Collections reads now cover the latest dataset summary,
the active collection tree and keyword search, including filtering, pagination,
distinct descendant keyword counts and one batched algorithm-results query.
Synthetic fixtures imported into an isolated PostgreSQL schema matched the
current SQLite results for empty, recorded and legacy-fallback cases, workbench
summary counts, and the tested keyword filters and sort orders, including zero
and boolean values.
Legacy membership aggregation had no `ORDER BY`; the new query orders those
memberships by collection ID, so the test compares that otherwise unordered
array by membership identity. `algorithm_results.result_json` remains text to
preserve the string currently exposed by the keyword API. These reads are not
composed into request runtime; remaining direct read consumers, real-data
reconciliation and query-plan/performance validation remain unfinished.

An unwired PostgreSQL collection store also implements create, metadata
update, delete, merge and taxonomy-category reuse under a transaction-scoped
mutation lock. Synthetic isolated tests covered concurrent duplicate-name
creation, category reuse, existing vector-cache invalidation, cascading deletion
and merge/feedback rules compared with SQLite; the affected existing SQLite
workbench check also passed. It now also implements membership feedback, all
eight bulk membership actions, archive/rollback reconciliation, nested keyword
reads, and proposal accept/reject. A dedicated real-PostgreSQL test in a
temporary schema passed these flows together with workbench counts, settings,
classification detail and collection/job lookups; the schema was removed. Bulk
actions now use set-based PostgreSQL statements instead of a query sequence per
selected keyword. The PostgreSQL `all_filtered` path streams IDs through a
server-side cursor in batches of at most 500 and applies every batch inside one
transaction; the API handler dispatches to it when given a PostgreSQL store.
An isolated test exercised multiple two-row batches through the API handler.
The legacy SQLite runtime still materializes all matching IDs until runtime
composition switches away from SQLite.
Collections API handlers now use repository methods for these read operations
when given a PostgreSQL store, while retaining their existing SQLite path for
the current runtime. The Fetch checkpoint-recovery route now gets persisted
completion results through the store as well. PostgreSQL storage is not yet
composed into runtime requests; distributed Fetch admission and source
checkpoint orchestration, runtime composition, real-data reconciliation and
integrated verification remain unfinished. An initial set of four PostgreSQL
Collections integration tests passed on isolated Neon schemas; the fixture
read test has since been rerun with workbench parity and the later read/store
coverage is summarized below.
The existing SQLite Collections/workbench suite passed 63 tests and 7 subtests.
Ruff passed on the touched API and PostgreSQL repository modules.

The Fetch service now writes cleaned keywords and Bing verification through a
shared `persist_cleaned_keywords` repository contract. SQLite retains the
existing SQL semantics; PostgreSQL uses bounded multi-row upserts, mode-scoped
ID preservation, decision-aware eligibility and exact candidate checks. Job
field updates, dataset snapshot writes and fetch admission/replay decisions
also use repository methods. PostgreSQL computes snapshot aggregates in the
same transaction as its JSONB upsert, and begins a fetch under the serialized
store transaction while locking any existing job and snapshot rows. The
begin-fetch contract preserves duplicate-ID rejection, retry after a failed
attempt without a snapshot, replay from a completed snapshot and mode mismatch
rejection. Fetch failure recording, empty-fetch completion and
model-unavailable publication also use store transactions; empty completion
retains the snapshot's eligible count, and failure updates preserve existing
counters. Embedding cache reads and writes now use repository methods with
bounded batches while preserving the model version, collection version, text
digest and packed vector representation. An isolated real-PostgreSQL test
inserted 501 candidates across the 500-row batch boundary and checked
cross-mode identity preservation, exclusion state, rejection and upsert
ordering, candidate attachment and replay, dataset aggregates, job updates,
fetch admission/recovery, terminal state transitions, embedding cache
validation/upsert, unassigned counts, refreshed snapshot/job aggregates,
settings, proposal storage, generated memberships and duplicate-name candidate
reads, user-pinned member examples and scope-aware fetch ledger reads. The
PostgreSQL read adapter serializes JSONB keyword evidence to the existing
service-level JSON text contract. Cumulative AI assignments and terminal
dataset aggregate publication also use store methods. Classification results
and eligibility decisions now persist through one scoped contract, including
user exclusions, classification ineligibility and recovery from an earlier
classification rejection. Cached-keyword reclassification now loads the
entity-context projection through the read adapter and reuses transactional
classification persistence; taxonomy membership writes preserve user-removal
feedback and auto-match checks. Fetch membership rebuilding now batches
feedback/pinned guardrails and applies mode-scoped replacement writes in a
transaction while preserving Google-only ownership. Algorithm analysis now
runs through a storage-specific analysis session: observation upserts, keyword
and history reads, result digests/upserts, and the final state-cache update all
remain in one SQLite or PostgreSQL transaction. `CollectionService` no longer
opens a database connection or contains direct SQL. A dedicated isolated
PostgreSQL test executed all 12 registered algorithms for one keyword, verified
the persisted observation/results and confirmed replay reused the cached state.
Another isolated PostgreSQL fixture passed a cached Wikimedia record through
the full `CollectionService` processing path and verified keyword, algorithm,
snapshot, job and Run History persistence. Model inference was explicitly
disabled and no provider client was constructed; this does not exercise the
distributed Fetch admission or source-checkpoint worker.
The `/algorithms/results` summary and Research list/create/update routes now
delegate to store contracts; PostgreSQL supports mode-scoped summaries,
pagination and filtering, keyword linking, and transactional Research writes.
An isolated PostgreSQL test confirmed its result summary matches SQLite, and a
second covered Research filters and CRUD. `/analytics/overview` now also reads
its complete set of summary inputs through the storage layer; the isolated
PostgreSQL read test compared those inputs with SQLite, including algorithm
results and dataset snapshots. Run History projection, cursor paging, offset
reads and detail now use store contracts as well; the PostgreSQL fixture matched
SQLite ordering and the tested cursor, status, mode and ID filters.
Collection routes no longer contain direct SQL fallbacks; both SQLite and
PostgreSQL stores now own their lookup contracts. Runtime metrics and recent
dataset/source-activity reads also use store methods. PostgreSQL reports the
database size and marks integrity as `NOT_CHECKED`; no SQLite integrity result
is carried over. Wikimedia entity-context cache reads, version checks and
upserts now use the store contract; the resolver handles SQLite JSON text and
PostgreSQL JSONB through the same result mapping. Its isolated PostgreSQL test
verified a JSON/timestamp round trip and cache-version filtering. All eight
tests across
`test_postgres_collection_reads.py` and `test_postgres_collection_store.py`
passed (one read test and seven store tests across the completed isolated-schema
runs). Separate runs passed all seven SQLite entity-context tests and all seven
PostgreSQL store tests. The combined SQLite
Collections/workbench/source-catalog run passed 71 tests and 257 subtests;
Ruff and `py_compile` passed. No API route or entity-context resolver now opens
a store connection directly, but application composition, scheduler and
orchestration paths still use SQLite or Firestore. Runtime composition has not
switched to PostgreSQL; provider-to-Collections Fetch orchestration and cutover
remain unverified.

On 26 November 2026, Google candidate-history and Bing saved-candidate reads
were moved behind a bounded `keyword_evidence_rows` store contract. Both the
SQLite and PostgreSQL adapters enforce source, exact project-prefix, mode,
eligibility and row-limit filters and return normalized JSON evidence without
exposing a database connection to source orchestration. A focused PostgreSQL
parity test matched the SQLite result on an isolated Neon schema, including an
underscore in the project prefix and excluded rows; the schema was removed and
a read-only check found zero remaining `ts_test_*` schemas. The Google, Bing
and SQLite contract tests passed (45 tests and 2 subtests), the PostgreSQL read
test passed separately, and Ruff passed on all changed files. The full
`Backend/tests` suite passed 1,190 tests and 1,107 subtests, with 26 tests
skipped. The separate `Backend/emulator_tests` suite was not run because no
loopback Firestore emulator is configured. Missing retained source results now
report `UNAVAILABLE` without claiming a data mode; missing results after
recovery exhaustion continue to report `FAILED`. This closes two direct
SQLite connection dependencies and improves missing-result accuracy, but the
surrounding fetch composition and runtime still use SQLite/Firestore and G5
remains open.

The PostgreSQL-gated migration, state, payload, scheduler, capacity and
Collections tests were then run against the configured unpooled Neon endpoint.
All 34 tests passed in 230.8 seconds; each integration test used a randomized
temporary schema and its teardown removed that schema. A read-only check found
zero remaining `ts_test_*` schemas, one `trendscout` application schema and
seven verified migrations. The PostgreSQL database name is `neondb`; `trendscout`
is the application schema. The latest stable database-size reading was
13,230,080 bytes, with 34 application relations and 704,512 total relation
bytes (8,192 heap bytes and 425,984 index bytes). Previous post-test reads were
15,785,984 bytes and then 12,886,016 bytes, the latter stable across three reads
two seconds apart. A later isolated payload-cleanup test was followed by the
13,230,080-byte read. The reason for these database-size changes is unverified.
The checks confirm zero remaining test schemas, seven migration rows and no
rows in the 33 business tables. The database-size readings do not report Neon
project-wide usage or available quota headroom.

`PostgresSourcePayloads` now also supports bounded expiry cleanup with
`FOR UPDATE SKIP LOCKED`, plus an explicit preserve-expired mode for retention
windows that must remain available for review. The isolated Neon payload test
passed integrity, retention, cleanup and preservation cases (2 tests total).
This adapter is still not wired into application runtime or coordinated with
the durable reference lifecycle.

`schema` creates persistent PostgreSQL objects; do not run it against the
business schema until the migration design and capacity assessment are ready.
The CLI's explicit `import` action is an unfinished staging utility, not a
cutover: it does not fence legacy writers, migrate Firestore, resolve differing
local records, or prove rollback. Do not use it to switch Production authority.
The second Windows machine needs its own consistent backup and conflict
inspection; do not assume its data matches this machine.

Inspection of the saved Production image and local database found that 1,993
and 1,631 keyword observation values respectively are date-only strings. Some
dataset observation bounds are also date-only. The initial schema preserves
these mixed-precision fields verbatim as text; converting them all to UTC
timestamps would invent a time of day. Existing JSON fields examined in those
two snapshots parsed successfully, but that check is not full semantic or
duplicate-key validation.

The local Neon endpoint hostname indicates `ap-southeast-1`; the earlier
deployment report records Vercel function execution in `iad1`. The actual
current production configuration and timing must be rechecked before any region
change; these locations suggest a cross-region latency risk, not a measured
PostgreSQL latency breakdown.

The default local Windows paths include `collections-v1/collections.sqlite`
(72,351,744 bytes), `local/d6-smoke/ledger.sqlite` (12,288 bytes), and
`scheduler-v1/schedules.sqlite` (28,672 bytes). These are main-file sizes only,
not consistent database backups; WAL files exist and may contain additional
data. There are also historical backups. None is assumed equivalent to the
latest Production snapshot or to the second Windows machine.

Read-only `quick_check` returned `ok` for all three local databases. Their
logical row counts demonstrate that local Collections differs from the
Production snapshot: local has 2,665 keywords, 62 collections, 399 memberships,
6,044 cached embedding rows and one research record; the inspected Production
snapshot has 3,664, 13, zero, zero and zero respectively. These differences do
not prove which rows are new, conflicting or redundant. The local atomic ledger
contains one state row; the local scheduler has no schedules or occurrences.
The other Windows machine remains uninspected. Do not silently overwrite or
merge these datasets on the basis of counts.

A read-only comparison by declared primary key found overlapping records with
different full row values: 1,133 keywords, 13 collections, 1,133
classification results and 13,596 algorithm results. Local and Production
also each have rows absent from the other. Row inequality is evidence requiring
semantic analysis, not proof that every difference is an irreconcilable
conflict. Import must preserve
source provenance, handle schema versions, and surface unresolved differences
instead of silently selecting a winner. The comparison used the inspected
Production backup and a read transaction on this machine; it is not a final
cutover comparison.

A follow-up read-only comparison of the standalone backup pair
`local-standalone-3423025b74a947d2bb1e883ac64fae51.sqlite`
(SHA-256 `12540ba7ff046ab6e957d895a465d603b024ee372dd74e23531dc1355e298afb`)
and `verified-standalone-f6aced0c2be14e14afa63c933984161d.sqlite`
(SHA-256 `892f178a8427217b1c042c90b4c3707c18e83126871aa138984a76488bd1126a`)
passed SQLite quick and foreign-key checks. Their 18 table names and column-name
sets matched; `entity_context` columns had different ordinal order. This
corrects an earlier description of those column sets as different. Among shared
primary keys,
`algorithm_observations` had 929 rows with identical values. All 13 shared
collections differed only in `created_at` and `updated_at`. All 1,133 shared
keywords differed in `evidence`; 832 also had different observation calendar
dates, `eligible` differed on three rows, `rejection_reason` differed on three
rows, and one had a keyword-label change. The differing evidence JSON values
were unequal after parsing, with changes across provider rank, view bucket,
source URL, observation/fetch times, quality flags and other provider fields.

All 1,133 shared classification rows and all 13,596 shared algorithm-result
rows also differed. Classification timestamps differed on every shared row;
source evidence differed on 850 rows and classification/status/category fields
differed on subsets. Algorithm result timestamps differed on every shared row,
input digests on 13,443 and parsed result JSON values on 12,751; status and
numeric value fields also differed on subsets. This rules out treating the
overlap as JSON formatting or timestamp precision alone. The comparison emitted
aggregate counts only, changed no data, and does not determine which snapshot
should win or incorporate later writes.

To make this check repeatable without a PostgreSQL connection, an offline
read-only comparator is available at
`Backend/scripts/sqlite_backup_compare.py`. From `Backend/`, rerun it with:

```powershell
.\.venv\Scripts\python.exe scripts\sqlite_backup_compare.py --local "$env:LOCALAPPDATA\TrendScout\migration-backups\local-standalone-3423025b74a947d2bb1e883ac64fae51.sqlite" --production "$env:LOCALAPPDATA\TrendScout\migration-backups\verified-standalone-f6aced0c2be14e14afa63c933984161d.sqlite"
```

On 26 November 2026, the tool revalidated both file hashes and SQLite
integrity, then streamed every table in primary-key order. It found 16,804
shared keys, 33,979 local-only rows, 38,528 Production-only rows and 15,875
shared rows with at least one changed cell. All 18 table layouts could be
compared by key; exact schema compatibility remains false because
`entity_context` column order differs and Production has a non-unique
`memberships(keyword_id)` index that local lacks. Both are reported for review.
The report contains source hashes, row/cell counts, and schema/table/column
names only; it emits no row keys or values and selects no winner.

The same comparator was rerun from the verified backups on 26 November to
locate the overlap conflicts by table without exposing record contents. Data
differences occur in 15 of 18 tables. `algorithm_results` has 13,596 changed
shared rows (12,751 differ in `result_json`, 13,443 in `input_digest`, and all
13,596 in `updated_at`); it also has 18,384 local-only and 30,372
Production-only rows. `classification_results` and `keywords` each have 1,133
changed shared rows, 1,532 local-only rows and 2,531 Production-only rows.
`algorithm_observations` has 1,994 local-only and 3,066 Production-only rows;
`cache` has 6,044 local-only rows. Other affected tables and changed-column
counts are available in the comparator output. These are overlap diagnostics,
not merge instructions: the data owners, recency semantics and relationship
effects still need reconciliation before any import can choose a value.

A separate aggregate timestamp check on the same two immutable backups found
that all 13,596 shared `algorithm_results` rows have timezone-aware timestamps,
and every Production `updated_at` is later than its matching local timestamp.
The observed differences span 0.359 to 10.090 days. The corresponding input
digests and result JSON also differ, so timestamp order is useful recency
evidence but does not prove that Production is the correct value for every
algorithm or justify discarding local-only rows. No IDs or cell values were
emitted, and no winner was selected.

The other overlapping timestamps also show later Production rows in this
backup pair: all 1,133 shared `classification_results.classified_at` values
are later by 5.130 days, while all 13 shared collection creation and update
timestamps are later by 6.924–7.141 days. The collection comparison found only
those timestamp columns changed, so preserving the earliest creation time and
latest update time is a possible reviewable reconciliation for those rows.
For `keywords.observed_at`, 1,048 of 1,133 shared rows have naive timestamps
on both sides and cannot be ordered safely; the remaining 85 timezone-aware
timestamps are later in Production. Classification fields and keyword evidence
still differ, so these timestamp comparisons do not authorize choosing a
canonical source for those records.

`sqlite_backup_compare.py` now reports these time comparisons under each
table's `timestamp_order_by_column`: it orders only parseable timezone-aware
values and counts naive, missing and invalid values as not orderable. The
summary includes counts and min/max absolute day differences only; it emits no
row identifiers or timestamp values. The focused comparator suite passes all
6 tests, and Ruff plus `py_compile` pass. A full rerun on the same verified
backup hashes reproduced the 50,783 local / 55,332 Production rows and 15,875
changed shared rows, with the same timestamp directions above.

JSONB-mapped columns are compared as parsed JSON with exact decimal handling;
there were no representation-only JSON differences in this pair. The target
schema intentionally stores `algorithm_results.result_json` as text, so that
field is compared exactly: 12,751 shared rows differ there. A separate parsed
JSON probe also found semantic differences. The comparison is still point-in-
time evidence: it does not include later writes or the second Windows machine,
and it does not resolve any conflict.

### Private row-level reconciliation review — 26 November 2026

`Backend/scripts/sqlite_backup_reconciliation_review.py` now writes a JSONL
review artifact for changed and one-sided Collections rows. It first runs the
aggregate comparator, requires compatible column and text-primary-key layouts,
then streams both immutable backups again and checks their hashes before
finishing. Input backups and the existing output directory must pass the
owner-only file-security checks. The destination must be new; it is created
exclusively with owner-only permissions and is never overwritten. The CLI
prints only the artifact path and aggregate counts, never row keys or values.

The private artifact carries both row versions, changed-column names,
timezone-aware timestamp ordering where possible, source hashes, and a
table-based `domain_hint`. Naive or missing timestamps remain
`not_orderable`. Binary values are represented by digest and byte length.
Every differing or one-sided row remains `unresolved`; the artifact records no
winner and makes no database changes. `domain_hint` helps route review but is
not proof of authority. This is a review input, not an import plan. Do not put
the artifact in Git, a shared folder, or a terminal transcript.

From `Backend/`, use the following only after a current, authorized pair of
standalone backups is available and stored privately:

```powershell
$review = Join-Path "$env:LOCALAPPDATA\TrendScout\migration-backups" "reconciliation-review-<new-id>.jsonl"
.\.venv\Scripts\python.exe scripts\sqlite_backup_reconciliation_review.py --local "<private-local-backup.sqlite>" --production "<private-production-backup.sqlite>" --output $review
```

Synthetic verification on 26 November passed the aggregate comparator and
private-artifact tests (9 tests total), including semantic JSON equality,
changed and one-sided rows, unresolved authority, timezone-naive timestamps,
owner-only output, refusal to overwrite, and unchanged source bytes. Ruff,
Python compilation and `git diff --check` passed. No real backup pair was
reviewed with this exporter; the older pair is not a current authorized
capture, and the second host/current Production snapshots remain outstanding.
This implementation does not close G3.

On 26 November 2026, `inspect_backup` completed a full PostgreSQL type and row
dry-run for both standalone files against schema versions 1–7: 50,783 local rows
and 55,332 Production snapshot rows. It verified source integrity, foreign keys,
JSON, timestamp conversion and batch staging on an empty isolated schema; the
random `ts_verify_*` schema was removed afterward. No source rows were imported
and no business schema was touched. Because the target was empty, this validates
each source independently but does not resolve the overlapping-row conflicts
above or establish semantic reconciliation.

Neon's [Free plan announcement](https://neon.com/blog/neon-free-plan-1-gb-per-project)
lists 1 GB database storage per project and 100 CU-hours per project per month
(accessed 26 November 2026). Actual project usage, effective quota, storage
accounting and available headroom remain unverified. The observed PostgreSQL
database size is not the total project storage usage. Production SQLite,
Firestore payloads, indexes and migration working space must all be measured
before capacity can pass.

An isolated capacity probe imported the verified Production standalone backup
(`892f178a8427217b1c042c90b4c3707c18e83126871aa138984a76488bd1126a`) into a
new `ts_cap_*` schema, applied schema versions 1–7, and verified all 18 source
tables / 55,332 rows against the imported values. Across 34 PostgreSQL
relations, `pg_total_relation_size` summed to 71,614,464 bytes, including
15,327,232 bytes of indexes; `algorithm_results` accounted for 60,243,968
bytes. The exact temporary schema was dropped in `finally`, and a separate
read-only query confirmed it was absent. No business schema was modified.
This measures the mapped Production Collections snapshot only; it excludes
Firestore operational state, source payloads, local-only records and the
second Windows machine. It is a schema-level footprint, not Neon project
storage billing or proof of safe migration headroom. After cleanup,
`pg_database_size(current_database())` returned 9,428,992 bytes; this differs
from the earlier 7,790,592-byte observation and must not be interpreted as
project usage or attributable solely to the temporary import.

After applying the empty `trendscout` schema on 26 November, a read-only query
reported 11,911,168 bytes for the PostgreSQL database, 34 base tables and only
the seven migration-ledger rows. No temporary test schemas remained. This is a
new point-in-time database-size measurement; it is not Neon project storage
usage and does not establish safe capacity for the SQLite and Firestore data.

A subsequent read-only run of
`Backend/scripts/postgres_capacity.py --schema trendscout --top 1` returned
11,878,400 database bytes, 34 schema relations / 704,512 relation bytes and
migration version 7. PostgreSQL reported one connection including the probe's
own connection, against a server `max_connections` setting of 112. The probe
returned `neon_project_capacity.usage_verified=false` and no headroom estimate;
these SQL measurements still do not establish Neon project-wide usage or the
Free-plan capacity gate. The CLI is read-only and the probe ran after the
temporary test schemas had been removed.

The available browser profile redirected the Neon project page to login; no
login was attempted. No Neon API-token variable or
`neonctl`/`neon` executable is available in the local environment, so actual
project usage and remaining headroom cannot currently be read. The published
plan limits do not establish this project's current usage.

## Production snapshot inspection

Using read-only Firestore access on 25 November 2026, the current Collections
pointer was version 16. The existing snapshot reader restored its 19 segments,
checked their content and the SQLite SHA-256, and saved a separate backup under
`%LOCALAPPDATA%/TrendScout/migration-backups/`. The pointer version and checksum
still matched immediately after the read. The restored image is 76,603,392
bytes. SQLite `integrity_check` returned `ok` and `foreign_key_check` returned
zero violations. This is a point-in-time backup, not a write fence: later
production changes require another final snapshot and reconciliation.

| Table | Rows | Table | Rows |
| --- | ---: | --- | ---: |
| algorithm_observations | 3,995 | algorithm_results | 43,968 |
| cache | 0 | classification_results | 3,664 |
| collections | 13 | dataset_snapshots | 13 |
| entity_context | 0 | feedback | 0 |
| fetch_jobs | 14 | keyword_decisions | 0 |
| keywords | 3,664 | membership_reconciliation_items | 0 |
| membership_reconciliation_runs | 0 | memberships | 0 |
| proposals | 0 | research_records | 0 |
| settings | 1 | suppression | 0 |

These counts describe only this snapshot. They do not establish semantic
equivalence, completeness of Firestore operational state, or the contents of
either local machine. The existing local-to-Firestore import script deliberately
excludes some tables; it must not be reused as a complete PostgreSQL migration.

Read-only Firestore inspection at the same stage listed three root collections:
`_ts_d5_coordination` (17 documents), `_ts_d5_cache_blobs` (101 manifests),
and `analysis_runs` (1 document). The six `_ts3_*` root collection counts were
zero at inspection time. The blob manifests declare 143,615,148 raw bytes and
782 chunks in total; these are logical manifest values, not measured billed
storage or a claim that all payloads are current or must be migrated. Analysis
collection-group counts were 10 snapshots, 200 observations, 16 keyword
results, and zero baseline buckets. These counts do not inspect quota values,
blob references, expiry, nested data outside those groups, active writers or
concurrent changes; they are not sufficient for reconciliation or capacity
approval.

The coordination inventory includes 11 cache-shaped documents, the Collections
pointer, three source scheduling records, a Wikimedia pacing record and the
`normal-google-live-v1` coordination record. The latter contains quota,
in-flight jobs, idempotency keys, provider timestamps, policy audit and owner
approval state together. These fields must be mapped and reconciled together;
the record's name must not be taken to mean its quota state is Google-only.
At inspection it held one job in `PARTIAL` state, one SerpApi inflight record
with an expiry, one idempotency key and 11 quota buckets. Presence of an
inflight record alone does not prove an active request; its expiry, receipt and
lease semantics must be reconciled at the actual cutover. No counters or state
were changed.

On 26 November 2026, a local read-only Firestore permission probe using the
configured Google Cloud project and `datastore.readonly` scope returned
`PermissionDenied` before any document contents were read. No Firestore state
was written. The 25 November captures and isolated staging results above remain
the available evidence; a final current capture still requires an authorized
read path, followed by active-job reconciliation and writer fencing.

## Initial storage inventory — incomplete

| Boundary | Verified implementation | Migration concern |
| --- | --- | --- |
| Collections business data | `Backend/app/collections/store.py`: SQLite collections, keywords, memberships, feedback, embedding cache, proposals, suppression, settings, fetch jobs, observations, algorithm results, datasets, decisions, context, classification, reconciliation and research | Preserve keys, relationships, timestamps, JSON contents, vectors, semantics and all rows; inventory schema changes and actual production image. |
| Runtime storage wiring | Default Local composition still creates `CollectionStore.local()`; explicit Local LIVE opt-in now constructs PostgreSQL collection, analysis, and scheduler stores. The deployed Vercel release still restores `FirestoreCollectionSnapshot` into SQLite, while the disabled-by-default Vercel PostgreSQL path constructs PostgreSQL stores per request. | Do not switch default Local or Production authorities until SQLite and Firestore data, active jobs, quotas, and reservations are reconciled. The PostgreSQL path is verified only in an isolated schema; it has not replaced the current business-data authority. |
| Production Collections | `Backend/app/collections/firestore_snapshot.py`: Firestore pointer and content-addressed blobs for complete SQLite images | Verify latest pointer and checksums; retain a consistent backup; do not use the image as the PostgreSQL runtime model. |
| Shared coordination and source payloads | `_ts_d5_coordination` and `_ts_d5_cache_blobs`, including chunk subcollections | Inventory every key and referenced blob, retention and expiry. Quota and approvals must not reset. |
| Distributed jobs | `_ts3_jobs`, `_ts3_idempotency`, `_ts3_occurrences`, `_ts3_resources`, `_ts3_datasets`, `_ts3_publications` and dataset `shards`/`meta` | Preserve jobs, claims, receipts and idempotency; reconcile in-flight work and reservations before fencing legacy writers. |
| Analysis storage | `analysis_runs` and per-run record subcollections | Determine whether each namespace contains relevant data before designing mapping and reconciliation. |
| Local atomic coordination and source caches | `LocalSQLiteAtomicStateStore.atomic_state` and `LocalSQLiteSnapshotBlobs.source_snapshot_blobs`; runtime keys include source quota/cache/checkpoint state, refresh/pacing state and `youtube-deletion-v1` | Inventory both machines independently; preserve counters, reservations, expiry, receipts and deletion history. Replace host-local transactions and payload files with PostgreSQL rows and bounded payload records without extending retention. |
| D6 authorization ledger | `d6_authorization_tranches.py` validates schema-versioned request reservations and approvals through the local atomic-state store; it is a separate offline authorization tranche | Do not copy or activate an approval blindly. Inspect whether a real ledger exists on each host; preserve policy digest, limits, reservations and settlements if active, while keeping credentials outside the database. |
| Scheduler | `Backend/app/scheduling/persistent.py` stores schedules and occurrence leases in separate SQLite; Vercel currently reports unavailable | Preserve schedule state and occurrence identities; do not enable automatic LIVE execution. |
| Provider credentials | `LocalCredentialProvider` resolves `YOUTUBE_API_KEY` and `SERPAPI_API_KEY` from a protected local `.env`; deployment credentials are environment variables | Secrets remain in the environment/secret store and are excluded from PostgreSQL and migration exports. |
| Browser preferences | Frontend `localStorage` holds theme, locale, sidebar state, Fetch source defaults and a client-side YouTube policy acknowledgement | These are browser-scoped preferences, not a substitute for server-side approval or quota state. Confirm any preference that must be shared before moving it to server storage. |
| Process locks | `LocalProcessLock` and local run locks are host files used for single-machine exclusion | Do not migrate stale lock files; replace cross-host correctness with PostgreSQL claims, leases and fencing before cutover. |
| Model files and vectors | FastEmbed model assets are cached under the local model directory; embedding vectors are stored in the Collections `cache` table | Preserve business vectors and their model/version metadata in PostgreSQL. Model assets are derived local files, not proof of Vercel model availability. |

This is a code-level inventory, not a complete production data census. The
current host has now been captured, but its current Collections row count
differs from the older local backup used for the earlier comparator. The local
ADC identity was denied a read-only Firestore probe, and the second Windows
host has not been inventoried. Namespace volumes, retention and active
consumers must still be checked against current, consistent captures before
G1 can pass.

### Current Windows host capture — 26 November 2026

The Owner-only inventory utility captured three local SQLite databases using
SQLite's online-backup API. All three standalone backups passed
`integrity_check`, had zero foreign-key violations, and reported no source-file
change during capture. Two expected locations were missing; there were no
capture failures.

| Store | Capture | Tables | Rows | Integrity | FK violations |
| --- | --- | ---: | ---: | --- | ---: |
| Collections | Captured | 18 | 31,954 | `ok` | 0 |
| Normal Google LIVE | Captured | 2 | 10 | `ok` | 0 |
| Normal Wikimedia LIVE | Captured | 2 | 1 | `ok` | 0 |
| Scheduler SQLite | Missing | — | — | — | — |
| D6 smoke ledger | Missing | — | — | — | — |

The full backups remain in the owner-only
`%LOCALAPPDATA%\TrendScout\migration-backups` folder outside the Git
repository; the inventory manifest exposes only schema and row-count metadata.
I attempted a PostgreSQL type-staging dry run against the Collections backup,
but its execution output was not retained, so this is not counted as a passing
staging result. The CLI uses temporary tables for dry-run and no import action
was run. This capture is per database and is not a cross-database transaction
snapshot.
The previous local standalone image used by the comparator had 50,783 rows;
it is not present in the current migration-backups folder for a same-file
comparison. The Production Collections snapshot available in the report is
from 25 November and had 55,332 rows. The differences do not establish which
snapshot is authoritative and are not migration or deletion instructions.

### Second Windows host: safe local capture

`Backend/scripts/inventory_windows_storage.py` now captures the known local
Collections, Scheduler, Google LIVE, Wikimedia LIVE and D6 SQLite stores. It
uses SQLite's online backup API, so committed WAL pages are included; checks
the standalone copy's integrity and foreign keys; and records only table names,
columns, row counts, file hashes and timing metadata in `inventory.json`. The
full SQLite data exists only in the local backup files. The tool never reads
`.env`, starts Fetch, or writes to a source database. Each file is a consistent
per-database snapshot; the group is not an atomic cross-database snapshot, so
this inventory is evidence for reconciliation and not a cutover capture.
Backups and the manifest are placed in an owner-only folder beneath
`%LOCALAPPDATA%\TrendScout\migration-backups` and must be handled as private
business and operational data.

When the second Windows host is available, the Owner can run this one
PowerShell line from the TrendScout checkout:

```powershell
Set-Location F:\TrendScout\Backend; .\.venv\Scripts\python.exe scripts\inventory_windows_storage.py
```

The command prints the capture folder. Transfer that folder back for comparison;
no SQL or manual row export is needed. G1 and G3 remain open until the capture
is reviewed with the current host and Production evidence.

## Proposed per-domain authority and reconciliation rules

These are staging rules derived from the current schemas and storage contracts;
they are not a claim that any source database has been approved as the winner.
Apply them to records only after the third-host inventory and final authorized
Production capture are available.

| Domain | Authority evidence | Reconciliation rule |
| --- | --- | --- |
| User-authored Collections, feedback, decisions and research | Stable record identity, `created_by`/membership `origin`, explicit user actions, and timezone-aware edit timestamps | Union non-conflicting identities. Preserve explicit user edits and deletions with their provenance. If edit provenance or ordering is missing, keep both source versions in review staging; do not overwrite by database recency. |
| Provider observations and Fetch evidence | Source/provider ID, query or sampling scope, observation time, fetch/collection time, source URL and source record identity | Deduplicate only when the documented identity and evidence digest match. Preserve distinct observations and query relationships. A newer provider observation does not erase older source evidence. |
| Classifications and algorithm results | Input digest, algorithm/model and taxonomy versions, source evidence, and timezone-aware calculation time | Reuse only when the input digest and version match. Preserve divergent outputs as stale/review evidence or recompute after source rows are reconciled; timestamps alone do not select a winner. |
| Quotas, reservations, idempotency and cooldowns | Provider account/source scope, reservation ID, attempt receipt, request timestamps, settlement and policy version | Never add stored counters blindly. Reconcile unique reservations and receipts; treat an attempt with uncertain dispatch as consumed until proven otherwise. Preserve the most restrictive outstanding cooldown/eligibility and do not create new refresh eligibility during migration. |
| Jobs, checkpoints and scheduler occurrences | Stable job/occurrence IDs, idempotency key, attempt receipt, lease/fence token, expiry and terminal state | Keep unresolved or possibly dispatched work non-runnable until reconciled. Fence every old writer before issuing new PostgreSQL claims. Do not replay work solely because an old lease appears expired. |
| Payloads, cache and snapshot references | Content digest plus each cache/checkpoint/job reference and its own retention deadline | Deduplicate only byte-identical payloads. Union valid references while preserving each reference's source and expiry; remove expired payloads only after all references are reconciled. Cache-only reads must remain distinguishable from fresh provider observations. |
| Identity and relationships | Source-local primary keys plus verified natural/business keys and foreign-key relationships | Preserve source IDs and build explicit cross-source mappings. Do not infer identity from row order, matching counts or a single matching label. |

Until current records provide those fields with compatible timestamps, the
authoritative value for a conflicting row remains unresolved. PostgreSQL's
empty business tables are a staging destination, not an authority source.

## Firestore staging verification — synthetic fixtures

On 26 November 2026, 12 focused tests passed against randomly named PostgreSQL
schemas that each test removed in cleanup: Firestore coordination capture and
staging (3), analysis capture and staging (2), PostgreSQL atomic state (3),
source payload validation and retention (2), SQLite scheduler migration (1),
and PostgreSQL scheduler lease recovery/fencing (1). The fixtures verified
idempotent staging, quota values, payload expiry, checkpoint revisions,
Firestore timestamp precision, orphan analysis records, and rejection of active
legacy reservations. The tests used synthetic Firestore clients; they did not
read or modify the real Firestore project and did not stage current production
state. A post-test read-only check confirmed the migration ledger still
verifies and no temporary `ts_test_*`, `ts_verify_*` or `ts_cap_*` schemas
remain. This supports the migration implementation but does not pass G4: a final
consistent capture, writer fencing, actual quota reconciliation and production
staging remain outstanding.

The PostgreSQL staging test now also verifies that an imported active payload
reference is indexed with its blob's manifest retention deadline, even when the
legacy coordination record carries no separate `retain_until` field. This is
synthetic evidence only; the real Firestore capture is still unavailable.

## Local and Vercel PostgreSQL composition — isolated schema

On 26 November 2026, an isolated Neon integration test verified both runtime
compositions against one randomly named schema. The Local LIVE `create_app`
composition created a collection row; the Vercel PostgreSQL request path read
it without an OIDC header. A Vercel collection write committed through the
same schema and was visible through the Local PostgreSQL collection store.
The test removed its schema in `finally`; a read-only check then found no
`ts_test_*` schemas, 35 tables in the durable `trendscout` schema, eight
`_schema_migrations` rows, and no business rows in the remaining 34 tables.
The same no-OIDC Vercel request test also returned HTTP 200 for latest dataset,
latest keywords, Run History, Analytics overview, Algorithms results, Research
bootstrap, Dashboard bootstrap and Scheduler reads.

The runtime now caches a successful pooled/direct identity and migration-ledger
check for five minutes per process, database configuration and schema. Failed
checks are retried, and neither connection URL is retained as a cache key.
Focused tests cover reuse, expiration and failure retry. This reduces repeated
composition work; no Production latency improvement has been measured.

The Vercel setting remains disabled by default, Production was not deployed or
changed, and Local's normal default runtime still has legacy SQLite wiring.
This verifies shared-schema request composition and committed-write visibility;
it does not establish PostgreSQL-only behavior for every default application
path or an actual two-host deployment. G5 therefore remains open until the
legacy data and coordination state are reconciled and both runtimes can switch
without abandoning the current authorities.

## Cutover constraints and proposed sequence

1. Inventory actual legacy data and active writers; identify the latest
   Production pointer, verify its hashes, and create consistent backups.
2. Measure actual Neon quota/headroom and source data, indexes and migration
   overhead. Stop cutover if safe capacity cannot be established.
3. Design versioned relational schema and repositories around existing
   contracts and query patterns, including transactional job claims, fencing,
   checkpoints, quota reservations and independent source evidence.
4. Implement deterministic, resumable, idempotent import with transactional
   batches and conflict reporting. Test on an isolated PostgreSQL schema using
   synthetic fixtures; never run destructive tests on business tables.
5. Reconcile every SQLite table and Firestore namespace, relationships,
   semantic values, source timestamps, expiry and quota/reservation state.
   Counts alone are insufficient. Review differing local data separately.
6. Quiesce and fence old writers, reconcile active jobs and final changes,
   then migrate and verify before switching the sole runtime authority.
   PostgreSQL failure must fail safely without legacy fallback.
7. Demonstrate rollback/reconciliation of writes made after cutover before
   releasing; an old snapshot alone is not a lossless rollback. Keep backups
   and Firestore project and credentials until explicitly approved for cleanup.

These steps are proposed work, not completed migration or verified rollback.
No Production LIVE Fetch is authorized by this goal. Cache-only production
verification must first prove server-side provider calls are impossible.

### PostgreSQL Fetch continuation and unchanged-input reuse — 26 November 2026

The PostgreSQL-backed Fetch facade now enables cached sources to finish in one
bounded continuation slice. Its reuse check reads only the latest dataset ID,
processing status and source manifest; it does not run the aggregate queries in
the full latest-dataset view. Terminal status, Run History, and Collections
recovery can skip repeat processing when the retained Fetch receipt proves the
same zero-provider business input. PostgreSQL recovery does not set Firestore
snapshot flags or load a SQLite snapshot. The Local runtime reads the manifest
fresh for each operation so a long-lived facade cannot reuse an older dataset
summary.

Focused offline verification passed 110 tests and 46 subtests, with two
environment-gated skips. Ruff and Python compilation passed. Five isolated
Neon tests across PostgreSQL atomic state, Local/Vercel composition and
collection reads passed. The updated composition integration also passed
independently after adding a real PostgreSQL `max_sources=1` cache continuation
with terminal checkpoint readback. It simulated interruption after a durable
progress checkpoint, verified same-key replay and `LEASE_BUSY` during the live
lease, then resumed after expiry from the saved result. The fixture handler
recorded one simulated provider attempt and made no external request. A
read-only follow-up found zero remaining `ts_test_*` schemas. Browser or
Production latency and real adapter attempt receipts remain unverified. The
offline Chrome Fetch workflow also passed after its response fixture was
updated to the current `/dashboard/bootstrap` contract. It verified three
same-key Fetch submissions, recovery once after transient status failures, and
a reload with no repeat POST; the API was stubbed and no backend or provider
was contacted.

### Explicit cache-only Fetch and deferred-model resume — 26 November 2026

The current working tree adds `mode: "CACHE_ONLY"` to `POST /api/v1/fetches`
and a separate **Use saved cache only** control in the LIVE Fetch UI. The mode
is part of durable idempotency metadata. Distributed workers route it to a
separate cache-only handler registry; a missing handler and a cache miss return
an unavailable result instead of invoking the provider-capable handler. The
Google Trends, Google RSS, Bing, YouTube and Wikimedia paths check saved cache
evidence before their provider gateways. A cache-only request does not acquire
manual-refresh permission or bypass provider cooldowns. Source delivery state,
timestamps, provenance and cache-miss reasons remain in the receipt.

For an existing Fetch whose Collections result is `MODEL_UNAVAILABLE`, the
recovery endpoint now retries only when the current CollectionService enables
inference. It reuses that Fetch's terminal checkpoint records and limits
keyword loading to IDs created from those records; it does not scan all saved
LIVE keywords or run provider work. PostgreSQL row locking and the Local SQLite
write transaction make a concurrent retry return the active processing state
instead of starting another model attempt. If retained checkpoint records are
incomplete or the model remains unavailable, the endpoint preserves the
unavailable result instead of publishing a false completion.

Focused evidence: the provider/cache group passed 137 tests and 11 subtests;
two deferred-model service tests and two recovery API tests passed; the
isolated Neon PostgreSQL composition passed with cache-only routing and retry
claim assertions. On Neon, cache-only completed without invoking the LIVE
handler registry and every source receipt reported zero provider requests.
Cache-miss tests cover the supported source paths and fail closed. The frontend
Fetch controller suite passed all 32 tests, and the offline Chrome Fetch
workflow passed after the new control was added. Ruff, Python compilation and
`git diff --check` passed for the changed backend scope. Separately, the
installed FastEmbed model loaded with network access disabled and returned a
nonzero 384-dimensional vector. An isolated temporary-SQLite lifecycle smoke
started at `MODEL_UNAVAILABLE`, then resumed from that Fetch's retained source
checkpoint and completed using only its keyword; a separate LIVE keyword was
not loaded. The lifecycle fixture was synthetic and no provider was called.
This verifies the local installed-model path, not model availability in Vercel.
No Production deployment, Fetch or latency measurement was made. G8 passes on
the implementation and isolated runtime evidence; Production timing remains
part of G12, and G10 remains open pending consolidated regression.

### PostgreSQL reads, scheduler, request coalescing and runtime packaging — 26 November 2026

The PostgreSQL Vercel path logs per-request composition, handler and total
duration using a route template; its timing test checks the response status,
`no-store` header, and that query values do not enter logs. The existing
Firestore middleware has the corresponding route-template and credential
redaction test. Both timing tests passed. This verifies instrumentation and
privacy, not Production latency.

The browser API client shares identical in-flight GETs, applies a 3-second
cache to selected read models, invalidates reads after mutations, and keeps
Collections job polling uncached. All nine API timeout/read-cache tests passed,
including the single-request coalescing case. The frontend controller suite
has 32 passing tests, and the offline Chrome Fetch workflow passed. These are
functional browser checks; real deployed cold/warm and navigation timings stay
in G12.

The Neon scheduler integration passed concurrent claiming, lease recovery and
fencing in an isolated schema. The PostgreSQL Live composition and eight
read-route checks had already passed against isolated Neon. The temporary
schema audit after the recent Neon tests found zero `ts_test_*` schemas.

For the PostgreSQL-only Vercel profile, the core manifest now excludes the
legacy Firestore client stack, the local Uvicorn launcher, and unused PyJWT.
Those packages remain available through pinned `legacy-firestore`,
`local-firestore` and `local-runtime` extras used by the local requirements
file. FastEmbed stays in core for the model lifecycle, while OpenTelemetry
stays because the pinned FastAPI runtime imports it. Development tests,
emulator fixtures and migration scripts are excluded from the Vercel function
bundle. An import-blocking test loads `app.main`, the Vercel PostgreSQL
composition and PostgreSQL analysis storage without Firestore, gRPC or
protobuf packages. Packaging/codec tests passed (6 tests, 17 subtests), local
composition and Firestore tests passed (18 tests, 12 subtests), and `pip check`
reported no broken requirements. Vercel documents `pyproject.toml` runtime
dependencies and `excludeFiles`, and notes that Python dependencies are not
tree-shaken ([Python runtime and function bundling](https://vercel.com/docs/functions/runtimes/python),
accessed 26 November 2026).

This changes the next PostgreSQL deployment's dependency profile; it does not
alter the currently deployed release. The post-change Vercel bundle has not
been built or size-measured, and no deployment occurred. Local Firestore
compatibility remains available for migration/rollback work. G9 passes on the
Neon read/scheduler, frontend request, timing instrumentation and package
boundary evidence; live browser latency and Production request timings remain
under G12.

### Consolidated regression and Production read checks — 26 November 2026

The full local core suite, run from `Backend/` as
`.venv/Scripts/python.exe -m pytest tests -q`, passed **1,207 tests and 1,115 subtests**; 27 environment-gated tests
were skipped. This run did not include `Backend/emulator_tests`. A preceding
full run with the Neon test URL enabled passed 1,231 tests and 1,115 subtests,
with two skips, but had one Windows `WinError 32` while cleaning the temporary
`ledger.sqlite` file. The affected timeout test passed alone, and its complete
gateway test file passed (22 tests, 18 subtests). The Firestore emulator suite
still requires an isolated Linux network namespace; those tests have not been
counted as passing.

A read-only Neon audit after regression rechecked all eight migration
checksums, counted 34 business tables with no populated table, and found no
`ts_test_*` or `ts_migration*` schemas. The migration ledger still contains
eight rows. No business rows were written.

The read-only capacity report measured the Neon database at 13,000,704 bytes,
the `trendscout` schema at 737,280 relation bytes across 35 relations, and
one active connection, including the probe itself, out of a reported maximum
of 112. These are PostgreSQL database/schema measurements; they do not report Neon project-wide storage,
compute usage or quota headroom.

Vercel's authenticated fetch path was used for read-only requests to the
protected Production deployment. `/api/health`, capabilities, dashboard,
Collections, algorithm results, analytics, research, Run History and latest
dataset all returned HTTP 200; `/api/v1/scheduler` returned HTTP 503, matching
the currently unavailable Vercel scheduler. The returned `x-vercel-id`
headers consistently included `sin1:iad1`; Vercel documents that this header
identifies the regions hit and function execution region, so this request set
confirms function execution in `iad1` from the `sin1` edge path
([request-header documentation](https://vercel.com/docs/headers/request-headers),
accessed 26 November 2026). This confirms the live Production execution region
for these reads, not the future PostgreSQL runtime.

These checks did not create or modify business data, start a Fetch, or call a
provider. The authenticated fetch connector may use a temporary Vercel share
link; no Deployment Protection setting was changed. The Production function
still uses the earlier deployed release and its current Firestore/SQLite path.
The fetch result did not expose function TTFB or database timing, and the
health-request trace lookup returned 404. Therefore Vercel-to-Neon latency,
Production cold/warm TTFB, and post-migration behavior remain unmeasured.

## Guarded attempt replay after dispatch interruption — 26 November 2026

An offline SQLite regression now models cancellation after a synthetic request
has been marked as dispatched. After the worker lease expires, a successor with
a newer fence reuses the same attempt identity. The durable receipt returns
`ALREADY_RESERVED`, the synthetic request callback is not invoked again, the
daily quota remains at one reservation, and the provider-request timestamp is
preserved. This verifies the local coordination path without contacting a
provider.

An equivalent isolated-schema PostgreSQL integration test has been added, but
the current focused run skipped both PostgreSQL environment-gated tests because
`TRENDSCOUT_TEST_POSTGRES_URL` was not configured. It therefore does not yet
provide new PostgreSQL cancellation/replay evidence. The affected focused suite
passed 65 tests and 23 subtests with 2 skips; Ruff, Python compilation and
`git diff --check` also passed. No gate changed state: G7 and G10 remain open,
and the overall score remains **3 / 12 (25%)**.

## Gate status

| Gate | State | Remaining evidence |
| --- | --- | --- |
| G1 | Open | The current Windows host's 26 November inventory captured 3 SQLite stores (31,965 total rows) with valid integrity and no FK violations; Scheduler and D6 smoke SQLite files were missing. Its 31,954-row Collections image differs from the prior 50,783-row local comparator image, which is unavailable for same-file verification. The second host and a fresh authorized Production capture remain missing. Production GETs confirm function execution in `iad1` from `sin1`; Neon SQL reports 13,000,704 database bytes and 1/112 connections including the probe, but project-level capacity and Vercel-to-Neon latency are unavailable. |
| G2 | **PASS** | Versions 1–9 applied and checksum-verified in `trendscout`; 36 tables total, with all 35 non-ledger tables empty and only the nine-row migration ledger populated. Local pooled/direct URLs share the verified Neon identity and pass TLS connectivity checks. Runtime cutover remains G5. |
| G3 | Open | The earlier local/Production comparator (50,783 / 55,332 rows) found 15,875 changed shared rows and source-only rows (33,979 local / 38,528 Production); no canonical winner was selected. The current first-host capture has 31,954 Collections rows and no matching Production snapshot is available for a fresh comparison. A private row-level review exporter now preserves both sides and marks each candidate unresolved, but has only synthetic verification; no current authorized pair has been reconciled. The second Windows host has not been inventoried; per-domain authority and reconciliation are open. |
| G4 | Open | 25 November real-data captures reconciled in isolated schemas, but the 26 November local read-only probe was denied; final current capture through an authorized path, writer fence, quota/payload reconciliation and production staging remain. |
| G5 | Open | Local LIVE `create_app` selects PostgreSQL collection, analysis and scheduler stores. In one isolated Neon schema, Vercel without OIDC read a Local collection write; the Local store observed a Vercel collection write; eight major read routes returned 200. A fail-closed startup guard now requires business rows and imported LIVE quota state, but the Neon schema is still migration-only; the Vercel flag remains off by default, and Local's normal default path and Production have not switched. |
| G6 | Open | New G6-R subcriterion: rolling seven-local-date expiry is calculated and a 500-row transactional/audited payload cleanup is wired into the explicit PostgreSQL LIVE composition. A real Neon isolated-schema test verifies dry-run, batch continuation, reference expiry, audit, and protected-row preservation. New PostgreSQL Fetches index per-record source digests and source/dataset/keyword lineage in the snapshot transaction; entity-context copies are indexed with their keyword rows, and missing aware collection times are flagged. A versioned source-policy registry now covers the capability catalog plus the entity-context alias, applies the 30-day YouTube Search maximum under the stricter seven-day local deadline, records review status/limit in source metadata, and prevents expiry extension on replay; Google/Bing and Wikimedia/Wikidata context remain partial pending further terms review. The PostgreSQL lineage transaction test was skipped because no test URL was configured. Historical records remain unindexed, and provider copies in keyword/classification/algorithm/research records are not yet purged because field-level lineage and dependent-record redaction are incomplete. Neon business/operational import, default Local SQLite removal, broader incremental-ingestion coverage, project-level capacity, and end-to-end retention proof remain. |
| G7 | Open | PostgreSQL cached continuation, terminal checkpoint readback, compact manifest reuse, Collections no-op, same-key replay, lease fencing and recovery after a simulated interruption have focused regression coverage (110 tests, 46 subtests; isolated Neon atomic-state, composition and collection-read integration passed). A new offline SQLite case confirms that cancellation after a synthetic dispatch preserves the same attempt receipt and one quota reservation across worker recovery. The matching isolated PostgreSQL test is added but skipped until `TRENDSCOUT_TEST_POSTGRES_URL` is supplied. Offline Chrome verifies continuation after transient status failures, single recovery and reload without repeat POST using a stubbed API. PostgreSQL cancellation/replay, provider reliability and end-to-end latency acceptance remain. |
| G8 | **PASS** | Explicit cache-only API/UI and separate worker registry; provider/cache tests and isolated Neon composition prove no LIVE handler invocation and zero reported provider requests, with cache misses failing closed. Same-Fetch checkpoint replay, exact keyword filtering, concurrent retry protection, and offline inference with the installed local model passed. Production availability/timing is not claimed; latency belongs to G12. |
| G9 | **PASS** | PostgreSQL read routes and the five-minute identity/schema validation cache; PostgreSQL request-timing and credential-safe route logging; 9 frontend API tests for in-flight coalescing/cache invalidation, 32 Fetch-controller tests and offline Chrome workflow; isolated Neon scheduler claim/recovery/fencing. PostgreSQL composition import tests remain independent of Firestore, while Vercel retains its Firestore SDK in core dependencies until the legacy runtime is cut over; the function excludes development sources. Preview `dpl_Fy8661oca8SH43Lzqw2PL7GjAgxK` for `361a167` reached READY after bundle optimization. Production latency and browser measurements are G12. |
| G10 | Open | Full local core suite: 1,207 tests and 1,115 subtests passed, 27 skipped. The preceding Neon-configured full run had 1,231 passed, 1,115 subtests, 2 skipped and one Windows SQLite cleanup failure; the affected gateway file then passed all 22 tests and 18 subtests. The retention update's focused suite passed 80 tests and 59 subtests with 4 skips; guarded-attempt coverage passed 65 tests and 23 subtests with 2 PostgreSQL environment-gated skips; and the prior packaging/legacy Vercel check passed 55 tests and 59 subtests. The updated packaging and legacy runtime group passed 25 tests and 16 subtests; Ruff, Python compilation and `git diff --check` passed. Source-lineage coverage now passes 8 tests with its PostgreSQL transaction test skipped; the current retention/source-lineage/PostgreSQL-store focused group passed 61 tests, 82 subtests, and 9 environment-gated skips. The private reconciliation exporter passed all 9 focused tests; Ruff and Python compilation passed. The isolated Neon retention test passed separately. Nine Neon migration checksums were verified. The isolated Firestore emulator suite remains unrun, so consolidated regression is not yet a full gate pass. |
| G11 | Open | Fenced cutover, proven rollback, release and exact deployed SHA. |
| G12 | Open | The last alias readback confirms `trend-scout-mu.vercel.app` points to READY `dpl_AizETu6tMZXvScnxWTSLa22GXW8j` on `cf09b2e`. Authenticated GETs at 04:34 UTC returned health 200 and capabilities/latest-dataset 403 `FIRESTORE_IDENTITY_REJECTED` from `sin1:iad1`. A 30-minute runtime-log query found no warning/error/fatal records, so the exact exception remains unproven. The deployed SHA omitted the Firestore SDK from default `pyproject.toml` dependencies while the legacy path still constructs a Firestore client; the middleware maps all client-construction exceptions to this same 403. Compatible deployment `dpl_4nrYBmDRyoKDVgYg97Mbub9CDCjs` on `7faacb8` previously returned 200 for the same paths, but it is not the current alias. Preview `dpl_B792ELGgj8gtTP8ZB2UbfFApijCM` for `40bd77b` failed with a 246.78 MB bundle after a custom install command was added. The correction in `361a167` removed the custom command and restored Firestore to core dependencies; Preview `dpl_Fy8661oca8SH43Lzqw2PL7GjAgxK` is READY after automatic optimization of its 245.45 MB bundle. No Production deployment or alias mutation was made. Production timing and Vercel-to-Neon latency remain open. |

No business-data migration, runtime cutover, Production deployment, live
provider request, quota reset or business-row modification has been performed.
The durable Neon schema now includes additive migrations 8 (payload
references) and 9 (retention audit); versions 1–9 checksum-verify, and all 35
non-ledger tables remain empty. The isolated retention and Fetch integration
tests created and removed only their random temporary schemas; no persistent
test schemas were left behind.
