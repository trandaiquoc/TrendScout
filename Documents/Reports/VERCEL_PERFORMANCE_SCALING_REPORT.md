# Vercel Performance and Scaling Report

> **25 November 2026 — subsequent owner decision:** The owner has selected a
> unified Neon PostgreSQL database for the migration goal. Statements below
> about relational storage not being approved describe the earlier release,
> not the current decision. Migration, cutover and acceptance have not yet
> been completed. See [the migration working report](POSTGRESQL_MIGRATION_AND_PERFORMANCE_REPORT.md)
> for the separate 12-gate status and current evidence.

**Status:** Follow-up read-path and frontend optimizations are pushed and Production `READY` on `7faacb8cd9ee47c52bdbee0cc8318a1ca4b66c2a`. Focused regression checks passed. GET-only Production checks preserved the 1,580-record dataset; the first Collections read took 11.016 s and its warm repeat took 1.511 s. No Production Fetch or provider endpoint was called. The cache-only Fetch target, changed-data storage scalability, and Owner's manual browser performance acceptance remain open.
**Updated:** 26 November 2026
**Goal-gate progress:** 33% (3 of 9 gates fully passed: G1, G7, G8. Partial gates are not weighted.)

## Baseline and owner-provided acceptance run

The requested baseline was code `d2cfafb` and report `b823b6f`. Before this change, the synchronized `main` and `origin/main` were at `2f04daf440a3a53344bb18aa06d95b8d3211cc77`.

The owner accepted Production run `7c47b79810ea4238a21c16d3967d2b41` on 25 November 2026:

| Source | Delivery | Records |
| --- | --- | ---: |
| Wikimedia | `CACHED` | 1,000 |
| Google | `STALE_CACHE` | 529 |
| Bing | `STALE_CACHE` | 1 |
| YouTube | `CACHED` | 50 |
| **Total** |  | **1,580** |

The owner reported zero provider requests. The following Fetch timings are owner supplied and were not independently re-read from retained Production logs:

| Logged segment | Duration |
| --- | ---: |
| Initial Fetch POST | 8.477 s |
| Google source slice | 19.673 s |
| Bing source slice | 17.979 s |
| Wikimedia and YouTube source slice | 21.394 s |
| Collections processing | 59.480 s |
| Snapshot upload segment | 39.086 s |
| Main request segments reported by owner | about 127 s |

The snapshot segment overlaps Collections processing and is not added again to the reported total.

## Findings verified in the repository

- Google and Bing cache freshness is one hour (`max_fresh_seconds=60*60`); provider refresh spacing is eight hours (`MIN_SOURCE_SPACING`). These represent data freshness and permission to refresh, respectively. Stale delivery can be valid while refresh is ineligible. No cache TTL or refresh interval was extended.
- Before this release, the delivery-state allowlist already included both `CACHED` and `STALE_CACHE`. The no-op was blocked by zero-request validation: Google reports `serp_verification_attempted` as boolean `False`, while the digest and worker checkpoint checks treated that field as an integer counter. Since `bool` is not an `int` for the strict type checks used here, a valid zero-request cache result failed the proof and could fall through to expensive work.
- Reuse also depends on exact business evidence: source scope, normalized records, record count, data mode, and meaningful quality flags. Delivery state, refresh eligibility, and cache-only warning metadata remain visible in source receipts and Run History but do not by themselves change the business input digest.
- The earlier Fetch path paid avoidable orchestration cost in checkpoint reads/writes, provider-free request composition, metadata reads, and repeated SQLite restore/query work. Changed business data still requires a full SQLite backup, hash, compression, and snapshot publication.

## Implemented changes

1. **Correct zero-request proof.** `serp_verification_attempted=False` now means no attempt; `True`, malformed values, and nonzero counters still fail. Numeric counters remain strictly typed. The worker uses the same boolean-aware rule and can batch terminal cache receipts with missing optional coverage only when the explicit source-level request count is zero.
2. **No-op before expensive work.** An exact cache-only match is compared with the latest business digest before checkpoint blobs or the SQLite image are restored. It skips discovery, Collections analysis, and business snapshot publication while retaining the current source receipts, delivery labels, warnings, provenance, timestamps, and Run History.
3. **Checkpoint and Fetch orchestration.** Zero-provider cached and stale-cache sources can complete in one worker invocation. Redundant progress receipts are avoided; terminal receipts use a fenced batch. Bing reuses validated Google candidates. Refresh and cooldown metadata share reads, and cache-only admission avoids Wikimedia pacing preflight while the actual source boundary still enforces pacing.
4. **Provider-free request paths.** Fetch admission, status, Run History, and Collections recovery avoid constructing source provider adapters. Unchanged-input recovery checks the compact digest before restoring SQLite or source blobs. Existing summary and Run History reads remain where needed for accurate status and historical joins.
5. **Bounded snapshot uploads.** Changed SQLite images are split into independently compressed 4 MiB byte segments, stored as content-addressed blobs, and written in bounded Firestore transaction batches with up to four concurrent batches. The versioned pointer is published after all required segments are durable. Identical segments can be reused, but this is not row/page delta storage: the full database is still backed up, hashed, and compressed when business data changes.
6. **Dashboard read model.** The default dashboard response and Collections landing data can be read from a bounded projection fenced to the exact SQLite snapshot version and SHA-256. New snapshots can publish the projection with their required blobs; old snapshots can backfill it after a matching SQLite restore. Timestamp, delivery, warning, and provenance fields are preserved; only display age is recalculated. A corrupt or missing optional projection falls back to SQLite.
7. **Frontend startup and navigation.** The initial dashboard bootstrap and capabilities request start concurrently. Startup no longer preflights `/datasets/latest`; capabilities cached in the browser can render immediately. Failed or empty bootstrap still waits for capabilities before attempting recovery. Existing filtered and paginated keyword reads remain intact.
8. **Compatibility and operating limits.** Older snapshot encodings remain readable by this release. Releases predating `sqlite-segment-gzip-base64-v1` cannot read a pointer after a new-format snapshot is published; rollback after such a write must use a reader that supports the new encoding. Production verification only backfilled derived dashboard metadata; it did not publish a new SQLite snapshot.
9. **No policy or data reset.** Source freshness, eight-hour refresh spacing, LIVE mode, quotas, credentials, provider policy, and business records were not changed or reset. No provider was called for verification.

## Explicit cache-only Fetch and deferred-model recovery — 26 November 2026

The current working tree adds `mode: "CACHE_ONLY"` to Fetch admission and a
**Use saved cache only** button in the LIVE Fetch UI. Durable idempotency
includes the selected mode. The worker routes cache-only requests to a
separate handler registry; cache misses and missing cache-only handlers fail
closed without calling a provider-capable handler. Source adapters return
validated cached or stale-cache evidence with its provenance, or an explicit
unavailable result. This change is local and has not been deployed.

The existing Collections recovery endpoint can resume a terminal
`MODEL_UNAVAILABLE` Fetch when the current runtime enables model inference. It
reuses that Fetch's terminal checkpoint records and limits keyword loading to
IDs created from those records; it does not scan all saved LIVE keywords or
contact providers. Incomplete checkpoint evidence stays unavailable. A
storage-level claim prevents a concurrent model retry from starting a second
processing attempt. If inference is still unavailable, the saved status
remains `MODEL_UNAVAILABLE`.

The provider/cache group passed 137 tests and 11 subtests; two focused model
service tests and two Vercel recovery API tests passed. One isolated Neon
PostgreSQL composition passed with cache-only routing, zero provider counts,
and no invocation of its LIVE handler registry. Cache-miss tests fail closed.
All 32 Fetch controller tests and the offline Chrome workflow passed after the
new button was added. The installed FastEmbed model was loaded with network
access disabled and produced a nonzero 384-dimensional vector; an isolated
temporary-store lifecycle smoke resumed one Fetch from its retained checkpoint
without loading an unrelated keyword or calling a provider. That evidence is
local and does not establish model availability in Vercel. No Production Fetch
or deployment was performed. Production cache-only timing remains unmeasured,
so the under-five-second target is still open.

The PostgreSQL-only Vercel runtime now imports without the legacy Firestore,
gRPC, or protobuf packages. The core `pyproject.toml` keeps PostgreSQL and
FastAPI/model dependencies; Firestore/migration packages and Uvicorn are
optional local extras. The function excludes tests, emulator fixtures, and
migration scripts. Two Vercel request-timing tests, nine frontend API tests,
one isolated Neon scheduler test, and the import-blocking package check passed.
The local Firestore composition tests also passed. The current Production
release was not changed, and no post-change Vercel bundle was built or measured.
See the 12-gate migration report for the G9 evidence. The Vercel Python runtime
supports `pyproject.toml` dependencies and `excludeFiles` and does not
tree-shake Python packages ([official runtime documentation](https://vercel.com/docs/functions/runtimes/python),
accessed 26 November 2026).

## Previous-release Production GET verification

The application release `e421d2a9ba590c9055c17a37e0ad95e10b497f00` is Production `READY` as deployment `dpl_TPNbmCHHjJ3NHWtHhh5JaLySw2Tp` in `iad1`; the alias `trend-scout-mu.vercel.app` points to it. Vercel runtime logs on 25 November 2026 recorded these alias requests:

| GET route | Total | Composition | Handler |
| --- | ---: | ---: | ---: |
| `/api/v1/capabilities` | 2.104 s | 0.110 s | 1.958 s |
| `/api/v1/datasets/latest` | 1.475 s | 1.468 s | 0.006 s |
| `/api/v1/dashboard/bootstrap` (first observed alias request) | 3.662 s | 3.644 s | 0.017 s |
| `/api/v1/dashboard/bootstrap` (repeat) | 1.517 s | 1.498 s | 0.019 s |
| `/api/v1/collections` | 1.275 s | 1.272 s | 0.002 s |

`/dashboard/`, `/api/health`, `/api/v1/capabilities`, `/api/v1/datasets/latest`, `/api/v1/dashboard/bootstrap`, and `/api/v1/collections` returned HTTP 200. The bootstrap returned 20 keywords; Collections returned 13 collections. Latest dataset and bootstrap payloads retained all 1,580 records and the four original delivery states, including `STALE_CACHE` for Google and Bing. The repeat bootstrap served the same snapshot after the first request backfilled the optional, snapshot-versioned dashboard read model.

Runtime logs also contain two near-simultaneous bootstrap GETs to the deployment hostname (not the Production alias), at 8.478 s and 8.922 s, with snapshot read-cache misses. Those traces show cold-instance variance and are not an alias p95 measurement. The alias timings above are individual requests, not a latency distribution. The Fetch-under-five-second target remains unmeasured because no Fetch POST was permitted. These checks used GET only; no provider endpoint, quota, LIVE Fetch, or data reset was invoked.

## Follow-up release and Production GET verification

The follow-up application commit `7faacb8cd9ee47c52bdbee0cc8318a1ca4b66c2a` deployed as `dpl_4nrYBmDRyoKDVgYg97Mbub9CDCjs`, state `READY`, target `production`, region `iad1`. The Production alias `trend-scout-mu.vercel.app` points to this exact source SHA.

This release adds snapshot-versioned read models for Analytics, the default Collections page, Algorithms results, Research bootstrap, and the first Run History page. Frontend startup and page navigation reuse combined responses and avoid redundant status, latest-dataset, keyword, and capabilities reads. Snapshot saves reuse unchanged compressed segments; changed SQLite data still requires a full backup, digest, and compression. The filtered Run History query now applies status and mode before SQL pagination so a filtered page fills from matching saved rows.

Vercel app timing logs for GET requests on 25 November 2026 show these individual requests in `iad1`:

| GET route | First observed total | Composition | Handler | Warm repeat | Notes |
| --- | ---: | ---: | ---: | ---: | --- |
| `/api/v1/capabilities` | 2.027 s | 0.050 s | 1.977 s | — | Runtime metadata work remains the main cost |
| `/api/v1/datasets/latest` | 1.550 s | 1.544 s | 0.006 s | — | Summary-only read |
| `/api/v1/dashboard/bootstrap` | 3.668 s | 3.649 s | 0.019 s | Not captured in this log window | 20 dashboard keywords returned |
| `/api/v1/analytics/overview` | 1.539 s | 1.526 s | 0.012 s | — | Log route label was `unmatched`; request path is known |
| `/api/v1/collections` | 11.016 s | 11.012 s | 0.002 s | 1.511 s | Initial projection fallback/backfill is the likely cold-path cause; inference from the code path and warm repeat |
| `/api/v1/research/bootstrap` | 4.097 s | 4.094 s | 0.002 s | Not captured in this log window | Log route label was `unmatched`; request path is known |
| `/api/v1/algorithms/results` | 1.523 s | 1.513 s | 0.009 s | — | 100 results returned |
| `/api/v1/runs/history?limit=20&cursor=` | 2.394 s | 1.505 s | 0.887 s | Not captured in this log window | 13 history rows returned |

All ten authenticated Production GET checks returned HTTP 200: `/dashboard/`, `/api/health`, `/api/v1/capabilities`, `/api/v1/datasets/latest`, `/api/v1/dashboard/bootstrap`, `/api/v1/analytics/overview`, `/api/v1/collections`, `/api/v1/research/bootstrap`, `/api/v1/algorithms/results`, and the first Run History cursor page. The MCP client's wall time includes its authenticated proxy and is not used as application latency; runtime timing above is from Vercel function logs. Runtime error/fatal log scan for the ten minutes after deployment returned no entries.

The latest dataset response was `LIVE` / `PARTIAL` with 1,580 received records: Wikimedia `SUCCESS` 1,000, Google `SUCCESS` 529, Bing `PARTIAL` 1, and YouTube `PARTIAL` 50. Dashboard bootstrap returned 20 keywords; Collections returned 13 rows; Research bootstrap returned 100 keyword options and 0 saved research records; Algorithms returned 100 rows; Run History returned 13 rows. These source manifest statuses describe the current saved dataset and are distinct from the owner-accepted Fetch delivery labels recorded above. No Fetch or provider request was made during these checks. The first missing-projection path may backfill derived dashboard metadata; no business dataset or SQLite snapshot was rewritten by an explicit Fetch.

The Vercel build log noted a 235.75 MB bundle above its standard size and said it was optimizing dependencies. The deployment still completed successfully in 13 seconds and reached `READY`; bundle size remains an operational limit to monitor.

## Storage scalability assessment

SQLite remains the transactional query engine for Collections, keywords, observations, and analysis. Firestore continues to hold shared job/checkpoint state and immutable snapshot blobs. Segment compression and bounded batched writes reduce per-blob transaction overhead and allow identical 4 MiB blocks to be reused. They do not make changed-data persistence incremental: a changed image still pays for full backup, digest, compression, and upload. The 80 MB expanded-image and bounded-segment limits are capacity bounds, not an incremental storage design.

Direct Firestore rows, dependency-aware partitions/deltas, or managed relational storage would require a versioned data model, migration and backfill, consistency checks, query/index design, cost and IAM decisions, and rollback strategy. None is configured or approved in this repository, so this release keeps the existing data store and explicitly leaves changed-data scaling open.

**Subsequent migration checkpoint (26 November 2026):** The owner approved one
shared Neon PostgreSQL database as the target architecture. The separate
12-gate migration report records G2, G8 and G9 as passed (3 of 12, 25%); the `trendscout`
schema has migration versions 1 through 8 and no imported business rows. A verified Production
SQLite snapshot previously measured 71,614,464 bytes across PostgreSQL
relations, including indexes. A repeat comparison now shows 15,875 shared
SQLite rows with changed values and source-only rows in the compared local
backup and Production snapshot, so no source has been selected as canonical.
The latest database SQL
capacity probe reported 11,878,400 database bytes and one active connection
including its own; Neon project-wide usage and headroom remain unverified.
An opt-in Vercel PostgreSQL request composition now passes an isolated Neon
integration check: Local and Vercel collection writes were visible through the
other runtime's store in the same temporary schema. Without an OIDC header,
Vercel also returned HTTP 200 for dataset, keyword, Run History, Analytics,
Algorithms, Research, Dashboard and Scheduler GETs. This is composition
evidence, not a Production
performance result: the flag remains disabled by default, the current
Production alias still runs the legacy SQLite/Firestore release, and the
default Local path has not switched. Successful PostgreSQL identity and schema
validation is cached for five minutes per warm process and configuration;
failed checks are retried. The latest focused PostgreSQL Fetch regressions also
verify cache-source continuation, compact unchanged-input reuse and a
Collections no-op without SQLite snapshot flags. An isolated Neon composition
test completed every fixture-cached source within a `max_sources=1` slice and
read its terminal checkpoints back; the source handlers were fixtures and no
provider was called. This is correctness evidence, not a latency measurement.
The offline Chrome Fetch workflow also passes against a stubbed API: it resumes
after transient status failures, reuses the same idempotency key for two
continuations, recovers Collections once and reloads without another POST.
Production and browser request timings have not been measured.
Complete operational-state migration,
project-level capacity, cutover and performance acceptance remain open. See the
[PostgreSQL migration report](POSTGRESQL_MIGRATION_AND_PERFORMANCE_REPORT.md)
for the detailed evidence and 12-gate status. The earlier release measurements
in this report still describe the deployed SQLite/Firestore runtime at that
time.

## Acceptance gates

| Gate | Current state | Evidence required to close |
| --- | --- | --- |
| Unchanged business input skips analysis and SQLite snapshot publication | **Passed offline** | Production no-op Fetch timing remains unmeasured; code path has focused recovery and digest regressions |
| `CACHED` and `STALE_CACHE` remain distinct and reusable when inputs match | **Passed offline** | Delivery, warning, provenance, quality/scope, boolean zero-request, and legacy-manifest regressions |
| Cache-only checkpoint and Fetch overhead is reduced | **Passed offline; Production timing open** | Worker batching, request-path, admission, and refresh-spacing regressions; a permitted cache-only Production run is still needed for timing |
| Changed-data storage avoids whole-image work | **Open** | Incremental/partitioned storage or an approved relational migration with integrity and rollback evidence |
| Reload and view transitions avoid duplicate reads | **Partial** | Frontend request/cache/navigation regressions pass; Owner's manual Production browser transition and reload latency are not measured |
| Initial dashboard shares a bounded read model | **Partial** | Current alias GET returned 200 in 3.668 s; separate capability metadata took 2.027 s. The first Collections read was 11.016 s and its warm repeat 1.511 s |
| No-provider Fetch completes under 5 seconds | **Unproven** | No Production Fetch POST was made; a permitted cache-only run and request logs are required |
| Application commit is pushed and Production is READY on exact SHA | **Passed** | `7faacb8cd9ee47c52bdbee0cc8318a1ca4b66c2a` is on `origin/main` and READY as `dpl_4nrYBmDRyoKDVgYg97Mbub9CDCjs` |

### Goal-level gate rollup

| Goal gate | State | Basis |
| --- | --- | --- |
| G1 Repository synchronization and architecture analysis | **Passed** | Synced `origin/main`; checked repo instructions, reports, cache, Fetch, persistence, backend reads, and frontend startup. |
| G2 Cache-only Fetch path | **Partial** | Exact no-op and zero-request proof are implemented and covered offline; Production under-five-second Fetch remains unmeasured. |
| G3 Storage and persistence optimization | **Partial** | Exact unchanged inputs skip snapshot work; changed data still uses a full SQLite image. |
| G4 LIVE orchestration optimization | **Partial** | Checkpoint and read overhead was reduced and tested offline; no provider-backed Production run was made. |
| G5 Backend reads and frontend performance | **Partial** | Combined projections and request coalescing are deployed; server timings are recorded, but Owner's browser transitions and reload remain unmeasured. |
| G6 Incremental processing and scalability | **Partial** | Business digest avoids unchanged analysis; changed-data persistence is not incremental. |
| G7 Consolidated focused verification | **Passed** | After fixes, 92 targeted backend tests passed across the implicated modules, the native Chrome workflow passed, 9 frontend API tests passed, and Ruff, Python compile, JavaScript checks, and `git diff --check` passed. |
| G8 Commit, push, and Production READY | **Passed** | Exact commit `7faacb8cd9ee47c52bdbee0cc8318a1ca4b66c2a` is on `origin/main` and READY in Production. |
| G9 Production performance and data integrity | **Partial** | GETs returned 200 and retained 1,580 records; cold/warm server timings are recorded. Owner's browser performance check and the no-provider Fetch-under-five-second target remain open. |

Score: `3 fully passed / 9 total gates = 33.3%`, reported as **33%**. Partial gates receive no fractional credit.

## Consolidated offline verification

The post-fix focused verification completed successfully:

- 92 backend tests passed across the affected dashboard/bootstrap, Vercel shared-read, and keyword/Run History modules; the Windows multiprocessing regression also passed in its standalone runner.
- The native Chrome workflow passed against local FastAPI and temporary SQLite, including Run History cursor/status filtering.
- 9 frontend API tests passed; Ruff passed for modified Python files; `py_compile`, JavaScript syntax checks, and `git diff --check` passed.

Tests used local fixtures and fakes. They did not call a provider or Production Fetch. The under-five-second Fetch target therefore remains unverified. The broad initial run found issues; the implicated groups were fixed and rerun rather than repeating the full repository suite.

## Remaining limits

- A cache-only Fetch still reads and validates source cache evidence and writes its Fetch/Run History receipt. Its Production duration was not measured.
- Cold SQL-backed reads may restore SQLite; the first Collections read observed 11.016 s before a warm repeat at 1.511 s. The projections improve warm/default reads but do not guarantee sub-five-second cold response times.
- Changed business data still requires full-image backup, hashing, and compression. Row-level or partitioned storage remains future work.
- A previous deployment cannot read the new segmented snapshot encoding after a new-format pointer is published. The current Production read-only verification did not publish that pointer; a rollback after a future snapshot write requires a compatible reader.
- Owner's final task is to manually measure browser open/reload and page transitions on the Production UI. This report does not claim those UI timings or the no-provider Fetch target have passed, and it does not assert a Production LIVE provider test.

## Release record

**Application commit:** `7faacb8cd9ee47c52bdbee0cc8318a1ca4b66c2a` (`perf(vercel): optimize read paths and snapshot reuse`), pushed to `origin/main`.

**Vercel Production:** `dpl_4nrYBmDRyoKDVgYg97Mbub9CDCjs`, `READY`, exact source SHA `7faacb8cd9ee47c52bdbee0cc8318a1ca4b66c2a`, region `iad1`. Production alias: `trend-scout-mu.vercel.app`.

**Production verification:** Authenticated GET-only checks returned 200 for the site shell and nine API paths. The latest dataset contained 1,580 records. Runtime logs recorded capabilities 2.027 s, latest dataset 1.550 s, dashboard bootstrap 3.668 s, Analytics 1.539 s, Collections 11.016 s then 1.511 s, Research bootstrap 4.097 s, Algorithms 1.523 s, and Run History 2.394 s. The runtime error/fatal scan found no entries in ten minutes. No Fetch POST or provider endpoint was called. Owner browser performance acceptance remains pending.

Vercel repository `ignoreCommand` skips documentation-only changes when `Backend` and `Frontend` are unchanged, as configured in the repository. See [Vercel `ignoreCommand` documentation](https://vercel.com/docs/project-configuration/vercel-json#ignorecommand), checked 25 November 2026. Report-only commits can create a canceled deployment record, but do not run the application build or change the Production alias.

## Protected Production GET check — 26 November 2026

The deployed Production URL returned Vercel Deployment Protection to direct
unauthenticated requests. The connected Vercel fetch path then performed
read-only GETs without changing Deployment Protection or the deployment. That
connector may use a temporary Vercel share link. No Fetch POST, provider call,
or business-data write was made.

| Endpoint | HTTP | Response bytes |
| --- | ---: | ---: |
| `/api/health` | 200 | 15 |
| `/api/v1/capabilities` | 200 | 10,933 |
| `/api/v1/dashboard/bootstrap?limit=1` | 200 | 72,161 |
| `/api/v1/collections` | 200 | 14,445 |
| `/api/v1/algorithms/results?limit=5` | 200 | 41,105 |
| `/api/v1/analytics/overview` | 200 | 64,453 |
| `/api/v1/research/bootstrap` | 200 | 17,319 |
| `/api/v1/runs/history?limit=5` | 200 | 23,466 |
| `/api/v1/datasets/latest` | 200 | 4,431 |
| `/api/v1/scheduler` | 503 | 51 |

The Scheduler response was repeatable and returned
`SCHEDULER_STORAGE_UNAVAILABLE`. Earlier report text recorded a 200 response
for the Scheduler path; the latest check shows that prior result is not
current and no runtime explanation for the difference has been confirmed.
All ten responses were Vercel cache misses. Their `x-vercel-id` region path
was `sin1:iad1`; Vercel documents that this header identifies the regions the
request hit and the function execution region, confirming `iad1` execution
from the `sin1` edge path for these requests ([request-header documentation](https://vercel.com/docs/headers/request-headers),
accessed 26 November 2026).

This check confirms read-path availability on the existing Production
Firestore/SQLite release. The Vercel fetch response did not include function
TTFB, SQL time, or a successful trace, so these response sizes and statuses
are not latency measurements. They do not establish Neon connectivity,
Vercel-to-Neon latency, post-migration behavior, or the cache-only under-five-
second acceptance target.
