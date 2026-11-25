# Vercel Performance and Scaling Report

**Status:** Implementation and consolidated offline verification complete; application commit `27f861ed8e8c3bec190d0827c648b455bd3b9672` is deployed to Production and `READY`. Production verification used read-only GET requests; no Fetch or provider endpoint was called.
**Updated:** 25 November 2026

## Baseline and Production evidence

The repository was synchronized before work. `main` and `origin/main` both pointed to `b823b6f0c08a916766fdf231c76da309a0a70271`; the previously deployed code baseline was `d2cfafb2d5ed6f1ed9730f6231197071a9646580`.

The owner-provided Production acceptance run is `7c47b79810ea4238a21c16d3967d2b41` from 25 November 2026:

| Source | Delivery | Records |
| --- | --- | ---: |
| Wikimedia | `CACHED` | 1,000 |
| Google | `STALE_CACHE` | 529 |
| Bing | `STALE_CACHE` | 1 |
| YouTube | `CACHED` | 50 |
| **Total** |  | **1,580** |

Provider requests were reported as **0**. These run details and timings below were supplied by the owner; they have not been independently re-read from Production logs.

| Logged segment | Duration |
| --- | ---: |
| Initial Fetch POST | 8.477 s |
| Google source slice | 19.673 s |
| Bing source slice | 17.979 s |
| Wikimedia and YouTube source slice | 21.394 s |
| Collections processing | 59.480 s |
| SQLite snapshot upload segment | 39.086 s |
| Main request segments reported by owner | about 127 s |

The snapshot segment is part of or overlaps Collections processing and is not added again to the reported 127 seconds.

## Findings verified in the repository

- Google and Bing cache entries use a **one-hour freshness window** (`max_fresh_seconds=60*60`), while refresh eligibility uses `MIN_SOURCE_SPACING=8 hours`. A stale delivery is therefore expected when the cached evidence remains usable but another provider refresh is not yet eligible. Cache TTL was not extended.
- Before this change, collection reuse accepted only `CACHED`. It also hashed operational delivery fields and status metadata, so otherwise identical `STALE_CACHE` evidence could force Collections analysis and another full SQLite snapshot.
- The source result already carries distinct fields for delivery (`CACHED` / `STALE_CACHE`), provider request count, refresh eligibility, source coverage, warnings, and timestamps. Reuse can ignore delivery-only differences while keeping those fields in the Fetch receipt and Run History.
- Cache-only source handling also incurred avoidable orchestration cost: terminal source receipts were written separately, some cache branches wrote a progress receipt before the terminal receipt, Bing could reread Google candidate data, and the initial admission and metadata-read paths built or queried more shared LIVE state than they needed.

## Implemented changes

1. **Business-input digest v2.** The digest now covers normalized records, source scope, record count, data mode, and meaningful quality flags. It excludes delivery state, refresh scheduling, operational status, retry/error metadata, and the two stale-cache warning flags that are added only when delivering cached records. `STALE_CACHE` remains visible and is never converted to `CACHED`.
2. **Safe legacy matching.** A v1 dataset can be reused only when current business scope, count, data mode, and non-delivery quality flags still match its saved manifest. Exact and stale-warning-normalized v1 record fingerprints support the one-time upgrade path without accepting a changed query or changed evidence.
3. **No-op before expensive work.** A compact terminal job receipt and the current Firestore latest-dataset summary are compared before checkpoint blobs or the SQLite image are restored. An exact unchanged cache-only match skips discovery, Collections processing, and SQLite snapshot publication. The original source receipt, warning flags, delivery state, provenance, and timestamps remain available to status and history responses.
4. **Fewer checkpoint writes.** Confirmed zero-provider `CACHED` and `STALE_CACHE` sources can complete in one worker invocation. A terminal cache receipt is not first written as a redundant `RUNNING` checkpoint; terminal cache receipts are committed in one fenced batch. Provider-backed receipts continue to be persisted immediately. Oversized checkpoint blobs upload concurrently within the existing bounded storage design.
5. **Fewer redundant reads.** Bing consumes the already validated Google result in the worker context. Refresh permission and manual cooldown metadata use one state read. Vercel no longer preflights Wikimedia pacing for a cache-only Fetch; the actual Wikimedia request boundary still enforces pacing.
6. **Provider-free request contexts.** Latest-source Fetch admission, Fetch status, Run History, and Collections recovery avoid constructing source provider adapters. Initial admission skips the latest-dataset pointer read. Status and Run History retain one compact summary read so the no-op state is accurate; Run History still restores its existing SQLite ledger to preserve historical rows and joins. Collections recovery checks for unchanged inputs before restoring SQLite or source blobs, then lazily restores the database only when processing is required. A quota-preserving policy migration remains available only after an explicit policy mismatch.
7. **Page read efficiency.** Identical in-flight GETs share a same-origin request. Safe dashboard reads have a three-second in-memory cache that is invalidated by mutations and terminal Fetch status. Duplicate hash/history events no longer reload the already active view.
8. **Public contract.** Internal digest values stay out of public dataset and analytics manifests. Existing provider cache freshness, refresh spacing, provider budgets, LIVE mode, and stored data were not changed or reset.
9. **One application release.** Vercel's repository `ignoreCommand` builds when `Backend` or `Frontend` changes and skips documentation-only commits, so recording the post-deploy report does not start a second application build. Exit-code behavior follows [Vercel's `ignoreCommand` configuration](https://vercel.com/docs/project-configuration/vercel-json#ignorecommand), checked 25 November 2026.

## Storage scalability assessment

The current design has two distinct storage roles: Firestore holds shared job/quota/checkpoint state and immutable snapshot blobs; SQLite remains the transactional query engine for Collections, keyword observations, and analysis results. The owner-provided 72.6 MB SQLite size and 6.75 MB compressed image show that changed-data persistence still scales with the entire working database. The existing 80 MB expanded-image and bounded segment limits prevent unbounded writes, but they are capacity limits rather than an incremental storage design.

Options considered for this release:

- **Keep SQLite plus bounded Firestore snapshots:** preserves local compatibility, existing joins, transaction semantics, and rollback behavior. Skip restore, analysis, and snapshot publication when the source business digest proves nothing changed. Changed data still pays whole-image backup, digest, compression, and upload costs.
- **Write rows directly to Firestore:** could avoid moving a whole image, but Collections processing relies on relational joins and multi-table consistency. A safe change needs a versioned data model, query/index design, idempotent dual-write or backfill, consistency checks, and rollback path. It is not a small additive change and may increase document/index costs.
- **Partition or delta snapshots:** could reduce bytes for a narrow update, but requires dependency-aware partitions, a versioned atomic manifest, compaction, conflict handling, and restore compatibility. The current schema does not expose a safe partition boundary for all collection, keyword, observation, and analysis relationships.
- **Managed relational or hybrid storage:** better supports incremental SQL queries, but requires an approved service, credentials/IAM, connection-pooling and cost decisions, plus data migration and rollback. No such resource is configured or authorized in this repository.

The release therefore keeps the existing backward-compatible store and eliminates whole-image work for exact no-change Fetches. This protects existing data and reduces repeated-run cost without a migration. The changed-data snapshot path and its eventual storage migration remain explicit scalability work; this release does not close that gate or claim faster fresh-data uploads.

## Acceptance gates

| Gate | Current state | Evidence required to close |
| --- | --- | --- |
| Unchanged business input skips analysis and snapshot work | **Passed offline** | Production timing remains unmeasured; the code path is covered by no-op and recovery tests |
| `CACHED` and `STALE_CACHE` remain distinct and reusable when inputs match | **Passed offline** | Stale-state, warning, provenance, quality/scope, and legacy-manifest regression checks |
| Cache-only checkpoint and Fetch overhead is reduced | **Passed offline** | Batch checkpoint, read-path, admission, and refresh-eligibility regression checks; Production timing remains pending |
| Changed-data storage scales without whole-image snapshot cost | **Open**; exact no-change runs skip snapshot work | A backward-compatible incremental/partitioned store or approved database migration with integrity and rollback evidence |
| Page reload and view transitions avoid duplicate reads | **Passed offline** | Frontend request-coalescing, expiry, invalidation, navigation, and browser recovery checks |
| Fetch with no provider work completes under 5 seconds | **Unproven** | A Production cache-only Fetch with eligibility proven to forbid provider work, or equivalent Vercel runtime evidence. No Production Fetch has been run for this change. |
| Application release commit is on `origin/main`; Production is `READY` on that exact code SHA | **Passed** | Commit `27f861ed8e8c3bec190d0827c648b455bd3b9672` is deployed as `dpl_HbKCifTWYBbNmFFyn4K5nUEWFeLZ`; the Production alias points to that deployment. A later report-only commit is excluded from Vercel builds by `ignoreCommand`. |

### Consolidated offline verification

The final verification pass ran after implementation was complete:

- **261 focused backend tests passed:** 232 tests across 10 modules ran under the strict offline HTTP guard (`real_constructions=0`, `requests=0`); 29 Firestore integration tests passed with fake Firestore and mocked transport methods that were asserted not to send requests.
- **58 frontend Node tests passed**, with no failures.
- The offline Chrome Fetch-recovery harness passed.
- Ruff passed for every changed Python application and test file; Python compilation and `git diff --check` passed.

The tests use fixtures and mocked transports. No Production Fetch, provider endpoint, or LIVE provider was called. A cache-only Production Fetch is not a safe performance probe until refresh eligibility for that request is proven to forbid provider work. The under-five-second gate therefore remains open; this report does not claim the target has been met.

## Remaining limits

- The no-op path avoids Collections analysis and full-image snapshot work when source business inputs match. A Fetch still needs to read/validate the source cache evidence and persist its Run History receipt.
- When business input really changes, Collections continues to use the existing bounded, chunked SQLite snapshot. This work does not replace that storage model or claim that changed-data snapshot uploads are faster.
- Firestore row-level, partitioned/delta, and managed relational alternatives require data-model, migration, consistency, cost, and rollback decisions that cannot be inferred safely from the current repository. The report records these as future decisions rather than selecting an unapproved production service.
- SQL-backed analytics still require the SQLite working snapshot on a cold function instance. The page changes coalesce duplicate reads but do not eliminate that first restore.
- Cold-start, Firestore, and end-to-end Production latency must be measured after release. Offline tests cannot establish the Vercel under-five-second performance gate.

## Release record

**Application release:** `27f861ed8e8c3bec190d0827c648b455bd3b9672` (`perf(vercel): skip unchanged cached fetch processing`), pushed to `origin/main`.

**Vercel Production:** deployment `dpl_HbKCifTWYBbNmFFyn4K5nUEWFeLZ`, state `READY`, source SHA `27f861ed8e8c3bec190d0827c648b455bd3b9672`, region `iad1`. Alias `trend-scout-mu.vercel.app` resolves to this deployment.

**Read-only Production checks on 25 November 2026:** `GET /dashboard/`, `GET /api/v1/capabilities`, and `GET /api/v1/datasets/latest` returned HTTP 200. The latest-dataset response identifies owner run `7c47b79810ea4238a21c16d3967d2b41`, 1,580 received records, and distinct source delivery labels (`CACHED` and `STALE_CACHE`). These checks did not invoke a Fetch endpoint or a provider.

The report-only commit `27e2eab4548baac0c7f95385011b681a68794dda` was pushed separately. Vercel created deployment record `dpl_CurUenNXx1q1hZf5CZenq65s3D8b` and marked it `CANCELED` after `ignoreCommand` returned exit code `0`; the event log confirms the application build did not run. This is the configured ignore result, not a failed application release. The Production alias remained on `dpl_HbKCifTWYBbNmFFyn4K5nUEWFeLZ` at code SHA `27f861ed8e8c3bec190d0827c648b455bd3b9672`.
