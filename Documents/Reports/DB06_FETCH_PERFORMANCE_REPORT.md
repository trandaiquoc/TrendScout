# DB06 PostgreSQL Fetch Performance Report

Date: 2026-11-26
Branch: integration/demo-final
Baseline: 45c1f2f74bd31d08eb3fe700a2804bfec5eeb0e6

## Outcome

DB06 is blocked at P5. Five of six acceptance gates pass, but neither warm 1,500-record case is close to the 5-second target. The fastest warm changed sample took 126.958 seconds; the fastest unchanged sample took 139.083 seconds. No provider or OpenRouter requests were made.

## Environment and method

The benchmark harness runs the actual local CACHE_ONLY Fetch API and PostgreSQL-backed application pipeline against Neon. It seeds synthetic Google Trends cache payloads, blocks provider and OpenRouter network calls, applies existing migrations to a random ts_test_db06_* schema, validates the test URL identifies the configured runtime database, and drops only schemas it created. Setup and migrations are timed separately from Fetch. Cold measurements include application startup.

The harness records stage durations, SQL client dispatches, connection establishment, transactions, input/output counts, readiness, and terminal status. Its SQL count is the number of execute/executemany client calls; executemany may be pipelined, so this is not a packet-level TCP round-trip count.

No production schema, quota, scheduler, environment variable, or business data was changed. No migration was added.

## Baseline profile and root cause

The baseline profile was measured at commit 45c1f2f:

| Fixture | Fetch to ready | SQL dispatches | Connections opened |
|---:|---:|---:|---:|
| 10 records | 13.642 s | 204 | 29 |
| 100 records | 18.810 s | 185 | 30 |
| 500 records | 50.803 s | 196 | 34 |

At 500 records, baseline cache lookup took about 6.79 s, keyword normalization about 31 ms, and the source payload SELECT about 5.36 s at the client. Application startup was about 4.52 s. The SQL dispatch count stayed roughly flat as rows increased, while bulk row work and payload transfer grew. This did not show an N+1 query pattern or CPU-bound keyword normalization.

The post-optimization 500-record profile took 56.369 s. Its largest instrumented spans were collection processing (15.116 s), terminal-status availability (16.232 s), atomic-state operations (14.380 s), and cache lookup (5.872 s). Job/lease/checkpoint spans totaled 64.754 s across nested and concurrent work; stage totals overlap and must not be added together. Keyword normalization remained 31.51 ms.

The post-optimization profile opened 31 connections, with 7.934 s cumulative client connection-establishment time, and issued 131 SQL dispatches across 27 transactions. A test-schema EXPLAIN ANALYZE of the source payload lookup used the source_payload_blobs primary-key index, returned one row, and took 0.031 ms inside PostgreSQL (0.061 ms planning, two shared buffer hits and zero reads). The inference is that the much longer client lookup includes remote connection, coordination, and payload-transfer overhead rather than a missing payload index.

## Changes and measured effect

- Backend/app/storage/postgres_atomic_state.py: combined the three coordination table reads into one UNION ALL query and combined transaction-local timeout settings into one call. The transaction, fencing, quota and idempotency behavior remains in place.
- Backend/app/collections/postgres_store.py and Backend/app/collections/service.py: combined transaction settings, batched taxonomy-root creation, and avoided persisting the same source evidence twice during incremental Fetch while retaining dataset-evidence and evidence-keyword links.
- Backend/app/jobs/distributed_worker.py: added fenced heartbeats before and after a potentially long CACHE_ONLY handler. A slow remote cache read could outlast a lease without doing provider work; the renewal keeps checkpoint and completion writes fenced.
- Backend/scripts/benchmark_db06_fetch.py: added isolated-schema profiling and acceptance instrumentation for the real Fetch path.
- Backend/tests/test_postgres_collection_store.py and Backend/tests/test_d5_distributed_worker.py: added evidence-batching, taxonomy-batching and lease-renewal regression coverage.
- Backend/tests/test_vercel_shared_live_fetch.py: corrected a stale runtime-status assertion to the existing LEGACY_CACHE_ONLY value returned by the runtime endpoint. No runtime behavior changed.

At 500 records, SQL dispatches fell from 196 to 131 (33.2%), opened connections from 34 to 31, executemany rows from 3,002 to 2,001, and source-evidence writes from 1,000 to 500. The single post-change 500-record wall-clock sample was slower than baseline (56.369 s versus 50.803 s), so these reductions do not establish an end-to-end speedup. The warm acceptance results below are the deciding performance evidence.

## Final 1,500-record acceptance measurements

Fixture setup and migrations are excluded. Each sample completed the Fetch pipeline, reached terminal status, and exposed data ready for reads.

### Cold application with cached source input

| Sample | Startup included | Fetch to ready | End to end |
|---:|---:|---:|---:|
| 1 | 4.300 s | 112.789 s | 117.089 s |
| 2 | 3.669 s | 113.221 s | 116.890 s |
| 3 | 3.742 s | 102.314 s | 106.056 s |
| Min / median / max | — | 102.314 / 112.789 / 113.221 s | 106.056 / 116.890 / 117.089 s |

### Warm application with changed cached input

| Sample | End to end | SQL dispatches | Connections | Cache lookup | Terminal wait |
|---:|---:|---:|---:|---:|---:|
| 1 | 126.958 s | 144 | 30 | 15.577 s | 32.929 s |
| 2 | 130.728 s | 147 | 31 | 20.678 s | 29.126 s |
| 3 | 133.331 s | 147 | 31 | 24.567 s | 26.419 s |
| 4 | 128.191 s | 147 | 31 | 17.943 s | 30.269 s |
| 5 | 128.806 s | 147 | 31 | 22.551 s | 28.697 s |
| Min / median / max | 126.958 / 128.806 / 133.331 s | 144 / 147 / 147 | 30 / 31 / 31 | 15.577–24.567 s | 26.419–32.929 s |

### Warm application with unchanged cached input

| Sample | End to end | SQL dispatches | Connections | Cache lookup | Terminal wait |
|---:|---:|---:|---:|---:|---:|
| 1 | 165.859 s | 148 | 31 | 24.283 s | 36.289 s |
| 2 | 156.222 s | 148 | 31 | 25.759 s | 35.093 s |
| 3 | 145.832 s | 148 | 31 | 23.775 s | 32.836 s |
| 4 | 139.083 s | 148 | 31 | 21.047 s | 28.306 s |
| 5 | 159.194 s | 148 | 31 | 24.864 s | 32.429 s |
| Min / median / max | 139.083 / 156.222 / 165.859 s | 148 / 148 / 148 | 31 / 31 / 31 | 21.047–25.759 s | 28.306–36.289 s |

Every changed and unchanged run had 1,500 input records, 1,500 persisted keywords, 1,500 source-evidence records, and 1,500 distinct evidence-to-keyword links. Provider network requests: 0. OpenRouter requests: 0. The unchanged case used the same durable source-input digest in all five timed runs. User-owned collection membership remained present with USER origin and pinned status after the Fetches.

The collection layer reported collection_input_unchanged=false for unchanged runs because model inference was intentionally unavailable in this credentialless benchmark. This report does not claim completed AI classification or reuse of model results. CACHE_ONLY algorithm processing took about 0.04 ms in the 500-record profile; no algorithm formula or algorithm implementation was changed.

## Correctness, recovery, and tests

- Record and lineage checks matched the complete 1,500-row fixture in all warm measurements; no record was dropped and no duplicate evidence link was observed.
- User membership was created after prime materialization and verified after the unchanged runs.
- The lease-expiry regression test simulates delayed checkpoint loading and an eight-second cache-only handler; it passes with fenced heartbeat calls before and after handler work.
- The focused backend/PostgreSQL set passed: 165 tests and 11 subtests, no skips. It covered PostgreSQL atomic state, collection persistence, incremental Fetch, public admission, distributed recovery, live composition, operational quota state, and retention.
- Local/Vercel composition passed: 55 tests and 42 subtests, no skips.
- Ruff, compileall, and git diff --check passed.
- The benchmark profile and acceptance harness reported cleanup of their owned random schemas. The EXPLAIN schema was also dropped in a finally block.

An additional broad smart-collections run exposed six failures outside the required DB06 focused set. Two cache-helper assertions also fail on an isolated worktree at baseline 45c1f2f. Four other failures assume an AI engine or completed model classification, while this environment intentionally has no OpenRouter configuration. The broad run's failures were not silently counted as passes or changed as part of DB06. The stale distributed-worker test fixture was corrected and its focused regression now passes.

## Migration and remaining limitations

Migration requirements: none. Existing migrations were applied only to random test schemas; no schema version changed.

The warm target remains unmet by more than 25 times at the fastest measurement. The measured remaining work is remote coordination and cache retrieval, collection persistence/finalization, connection setup, and repeated terminal-status reads. The payload query plan is indexed and fast inside PostgreSQL, so adding an index is not supported by the evidence. The existing connection factory deliberately closes each short-lived connection; a pool would require lifecycle and concurrency review across Local and Vercel and would not account for the measured 120–166 second warm totals by itself.

The profile demonstrates fewer SQL dispatches and duplicate writes, but not a wall-clock speedup. Timing is variable on Neon, and this report does not claim the 5-second target is feasible with the current remote coordination and request lifecycle. Further architectural work would be required; no such redesign, deployment, or production change was made in DB06.

## Acceptance gates

| Gate | Result | Evidence |
|---|---|---|
| P1 — Baseline profiling | PASS | 10/100/500 scaling, stage timings, SQL and connection counts, cache query plan, and root cause recorded. |
| P2 — PostgreSQL optimization | PASS | SQL dispatches, row writes, and evidence writes reduced with before/after measurements; no wall-clock speedup claimed. |
| P3 — Correctness preservation | PASS | Full input/output counts and lineage, user membership preservation, unchanged algorithm implementation, and retention tests. |
| P4 — Coordination and recovery | PASS | Focused quota, idempotency, checkpoint, distributed recovery, and lease-renewal tests passed. |
| P5 — Performance | BLOCKED | Warm changed min/median/max: 126.958/128.806/133.331 s. Warm unchanged: 139.083/156.222/165.859 s. Both exceed 5 s. |
| P6 — Regression and delivery | PASS | Required focused suites, Local/Vercel composition, lint, compile, diff check, schema cleanup, report, and branch delivery verified. |

Conclusion: DB06 BLOCKED at P5. Five of six gates pass (83%). No Production deployment or main-branch update was performed.
