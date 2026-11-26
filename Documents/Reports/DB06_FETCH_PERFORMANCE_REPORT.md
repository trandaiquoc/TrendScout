# DB06 PostgreSQL Fetch Performance Report

Date: 2026-11-27
Branch: integration/demo-final
Baseline: 45c1f2f74bd31d08eb3fe700a2804bfec5eeb0e6

## Outcome

DB06 is blocked at P5. Five of six acceptance gates pass. In the final 1,500-record measurements, warm changed Fetches took 62.648–110.934 seconds (median 98.800 seconds), and warm unchanged Fetches took 39.104–72.897 seconds (median 70.162 seconds). Both warm cases miss the 5-second target. No provider or OpenRouter requests were made.

## Environment and method

The benchmark harness runs the actual local CACHE_ONLY Fetch API and PostgreSQL-backed application pipeline against Neon. It seeds synthetic Google Trends cache payloads, blocks provider and OpenRouter network calls, applies migrations to a random ts_test_db06_* schema, validates the test URL identifies the configured runtime database, and drops only schemas it created. Setup and migrations are timed separately from Fetch. Cold measurements include application startup. Cold and changed samples were completed in one acceptance run; after correcting terminal status to include the verified reused-dataset ID, the five unchanged samples were resumed in a separate isolated schema. All schemas created by these runs were cleaned.

The harness records stage durations, SQL client dispatches, connection establishment, transactions, input/output counts, readiness, and terminal status. Its SQL count is the number of execute/executemany client calls; executemany may be pipelined, so this is not a packet-level TCP round-trip count.

No production schema, quota, scheduler, or business data was changed. Migration `0011_dataset_processing_statuses.sql` was applied only in random test schemas.

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

- PostgreSQL coordination, checkpoint, and collection writes were batched where their contracts allowed it. Transaction, fencing, quota, idempotency, source lineage, and user-membership behavior remains covered by focused regressions.
- `Backend/app/storage/postgres_connection.py` adds a bounded PostgreSQL connection pool with TLS validation and connection reset handling. `Backend/app/storage/postgres_payloads.py` and `Backend/app/jobs/shared_cache.py` support metadata-only payload inspection and record-level payload accounting.
- `Backend/app/collections/source_digest.py`, `Backend/app/sources/google_trends/handler.py`, and `Backend/app/jobs/distributed_worker.py` add compact source-input manifests and a worker-level unchanged-dataset reuse path. The final unchanged measurements did not take the compact path: each timed sample still read one 2,094,486-byte payload blob.
- `Backend/app/api/fetches.py` uses completed worker results to avoid rereading checkpoint payloads and includes the verified prior dataset ID when terminal status confirms that Collections reused that dataset.
- Migration `0011_dataset_processing_statuses.sql` separates source ingestion, materialization, algorithm processing, and AI classification status. It was applied only in isolated benchmark schemas.
- `Backend/scripts/benchmark_db06_fetch.py` measures SQL dispatches, transactions, pooled checkouts, payload bytes, and isolated acceptance samples; it also supports the 100-record smoke profile.
- Focused tests cover worker reuse, terminal status, payload, connection pool, and shared-live behavior. A regression verifies that unchanged terminal status includes the existing materialized dataset ID.

At 500 records, SQL dispatches fell from 196 to 131 (33.2%), opened connections from 34 to 31, executemany rows from 3,002 to 2,001, and source-evidence writes from 1,000 to 500. The single post-change 500-record wall-clock sample was slower than baseline (56.369 s versus 50.803 s), so these reductions do not establish an end-to-end speedup. The 100-record smoke and 1,500-record acceptance below are the final correctness and performance evidence.

### 100-record smoke

The final profile run completed in 11.456 seconds from Fetch submission to ready data, excluding the separately measured 3.840-second application startup. It persisted 100 source records, 100 keywords, 100 source-evidence rows, and 100 evidence-to-keyword links. It issued 120 SQL client dispatches across 25 transactions and 26 pooled checkouts. Provider and OpenRouter requests were both zero; the random schema was cleaned.

## Final 1,500-record acceptance measurements

Fixture setup and migrations are excluded. Every sample reached terminal status and passed the data-readiness checks. Cold and changed samples were measured in the first final-code acceptance run; the unchanged set resumed in a separate isolated schema after fixing the terminal projection to include its verified reused dataset ID.

### Cold application with cached source input

End-to-end time includes application startup. Each run read 1,500 records from one 2,085,486-byte source payload and preserved the complete evidence lineage.

| Sample | End to end, startup included | SQL dispatches | Transactions | Pooled checkouts |
|---:|---:|---:|---:|---:|
| 1 | 87.096 s | 147 | 28 | 29 |
| 2 | 78.315 s | 147 | 28 | 29 |
| 3 | 84.927 s | 147 | 28 | 29 |
| Min / median / max | 78.315 / 84.927 / 87.096 s | 147 / 147 / 147 | 28 / 28 / 28 | 29 / 29 / 29 |

### Warm application with changed cached input

| Sample | End to end | SQL dispatches | Transactions | Pooled checkouts | Cache lookup | Collections | Terminal wait |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 86.066 s | 141 | 26 | 28 | 20.94 s | 21.56 s | 1.50 s |
| 2 | 110.934 s | 144 | 27 | 29 | 20.29 s | 23.69 s | 2.13 s |
| 3 | 104.795 s | 144 | 27 | 29 | 17.72 s | 21.24 s | 2.46 s |
| 4 | 98.800 s | 144 | 27 | 29 | 13.89 s | 21.15 s | 2.64 s |
| 5 | 62.648 s | 144 | 27 | 29 | 2.33 s | 20.72 s | 2.91 s |
| Min / median / max | 62.648 / 98.800 / 110.934 s | 141 / 144 / 144 | 26 / 27 / 27 | 28 / 29 / 29 | 2.33 to 20.94 s | 20.72 to 23.69 s | 1.50 to 2.91 s |

Each changed sample read one 2,094,486-byte source payload. The five source-input digests changed as intended, and none was classified as unchanged or dataset-reused.

### Warm application with unchanged cached input

| Sample | End to end | SQL dispatches | Transactions | Pooled checkouts | Payload bytes read |
|---:|---:|---:|---:|---:|---:|
| 1 | 72.897 s | 90 | 17 | 19 | 2,094,486 |
| 2 | 39.104 s | 90 | 17 | 19 | 2,094,486 |
| 3 | 70.226 s | 90 | 17 | 19 | 2,094,486 |
| 4 | 70.162 s | 90 | 17 | 19 | 2,094,486 |
| 5 | 60.518 s | 90 | 17 | 19 | 2,094,486 |
| Min / median / max | 39.104 / 70.162 / 72.897 s | 90 / 90 / 90 | 17 / 17 / 17 | 19 / 19 / 19 | 2,094,486 each |

All five unchanged samples used the same source-input digest, reported `collection_input_unchanged=true` and `dataset_reused=true`, and pointed to the same durable dataset ID. The worker's metadata-only fast path did not engage: every timed sample still read the full 2,094,486-byte source payload. Materialization completed, while `processing_status` was `MODEL_UNAVAILABLE` because this credentialless benchmark did not run AI classification. Those status fields represent separate stages.

All ten warm runs passed checks for 1,500 input records, 1,500 persisted keywords, 1,500 source-evidence rows, and 1,500 distinct evidence-to-keyword links. Provider and OpenRouter request counts were zero. User-owned collection membership remained present with USER origin and pinned status.

## Correctness, recovery, and tests

- The 100-record smoke passed with 100 source records, keywords, source-evidence rows, and evidence-to-keyword links.
- The 1,500-record changed and unchanged samples retained the complete fixture and lineage. The unchanged terminal projection identified the already materialized dataset instead of looking up a nonexistent snapshot under the new Fetch ID.
- User membership remained pinned with USER origin after the unchanged runs.
- The focused worker and PostgreSQL Collections reuse set passed: 50 tests, no skips. This includes terminal projection, worker reuse, lease renewal, payload handling, connection pool, and shared-live Fetch regressions.
- Ruff passed for the changed backend files. Compileall and `git diff --check` passed.
- All benchmark runs validated the test database identity and cleaned their owned random schemas. No production schema, quota, scheduler, or business data was changed.

## Migration and remaining limitations

Migration `0011_dataset_processing_statuses.sql` adds separate source-ingestion, materialization, algorithm-processing, and AI-classification status fields. It was applied only in the benchmark's random schemas; no production migration was run.

P5 remains blocked after the DB06 changes. The fastest changed sample was 12.5 times the 5-second target; the fastest unchanged sample was 7.8 times the target. Changed samples spent 20.72 to 23.69 seconds in Collections and read a 2.09 MB cache payload. Unchanged samples also read the full payload, spending 14.94 to 20.50 seconds in payload reading, before remote coordination and status handling completed. The source-payload query plan uses its primary-key index and is fast inside PostgreSQL, so these measurements do not support adding an index.

The work reduced SQL dispatches and duplicate writes, but did not establish an end-to-end wall-clock speedup. Neon timings varied across samples. The current remote cache, checkpoint, and request lifecycle does not meet the warm 5-second target. No deployment, quota change, paid service, or production change was made.

## Acceptance gates

| Gate | Result | Evidence |
|---|---|---|
| P1 - Baseline profiling | PASS | 10/100/500 scaling, stage timings, SQL and connection counts, cache query plan, and root cause recorded. |
| P2 - PostgreSQL optimization | PASS | SQL dispatches, row writes, and evidence writes reduced with before/after measurements; no wall-clock speedup claimed. |
| P3 - Correctness preservation | PASS | Full input/output counts and lineage, user membership preservation, unchanged algorithm implementation, and retention tests. |
| P4 - Coordination and recovery | PASS | Focused quota, idempotency, checkpoint, distributed recovery, and lease-renewal tests passed. |
| P5 - Performance | BLOCKED | Warm changed min/median/max: 62.648/98.800/110.934 s. Warm unchanged: 39.104/70.162/72.897 s. Both miss 5 s. |
| P6 - Regression and delivery | PASS | Focused 50-test suite, Ruff, compileall, diff check, schema cleanup, updated report, and delivery to integration/demo-final verified. |

Conclusion: DB06 BLOCKED at P5. Five of six gates pass (83%). No production deployment or main-branch update was performed.
