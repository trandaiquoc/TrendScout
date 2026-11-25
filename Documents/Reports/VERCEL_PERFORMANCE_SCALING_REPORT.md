# Vercel Performance and Scaling Report

**Status:** Dashboard read coalescing is deployed and its Production GET path was verified. The cache-only Fetch target, sub-five-second page reads, and changed-data storage scalability remain open. No new Production Fetch or provider endpoint was called.
**Updated:** 25 November 2026
**Goal-gate progress:** 67% (3 of 9 gates passed; the other 6 are partial and weighted at one half each. See the gate states below.)

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

Read-only Vercel runtime evidence from deployment `dpl_HbKCifTWYBbNmFFyn4K5nUEWFeLZ` adds a separate page-read cost: two `GET /api/v1/analytics/overview` requests took 5.788 s and 5.978 s, with 5.522 s and 5.582 s in request composition. Each request logged Firestore snapshot cache misses for 5,600,051 and 3,423,315 raw bytes. Nearby `GET /api/v1/datasets/latest` took 1.553 s and `GET /api/v1/capabilities` took 2.349 s. These were GET-only dashboard checks, not a Fetch; no provider endpoint was called. The measurements show that a cold SQL-backed analytics read can exceed five seconds, but do not establish the cache-only Fetch duration.

Read-only Production verification on the follow-up deployment `dpl_77t9y9Qb9ez6nv7M4PFknjwRrcmu` (code SHA `49fcc68eb74cb77d3c371bc6ce232f18d30039b4`) confirmed the alias and browser path. At 06:17 UTC, `/dashboard/`, `/api/health`, `/api/v1/capabilities`, `/api/v1/datasets/latest`, and `/api/v1/dashboard/bootstrap` returned HTTP 200; the browser recorded 23 requests total including static assets. The startup still makes three separate data reads: capabilities, a lightweight latest-dataset summary, and the combined dashboard bootstrap. Runtime timings were:

| Production GET | Total | Composition | Handler |
| --- | ---: | ---: | ---: |
| `/api/v1/capabilities` | 7.523 s | 1.596 s | 5.927 s |
| `/api/v1/datasets/latest` | 1.654 s | 1.606 s | 0.039 s |
| `/api/v1/dashboard/bootstrap` | 5.988 s | 5.560 s | 0.425 s |

The bootstrap response returned owner dataset `7c47b79810ea4238a21c16d3967d2b41`, still `LIVE` and `PARTIAL`, with 1,580 received source records and the saved analytics/keyword page. The source evidence retained `CACHED` for Wikimedia and YouTube and `STALE_CACHE` for Google and Bing. The first page therefore has one bootstrap read for latest dataset, analytics, and first-page keywords, but that read still takes about six seconds. A separate latest-summary GET remains to paint the dataset summary earlier.

At the same time, the runtime log query reported Firestore snapshot-blob cache misses of 5,600,051 and 3,423,315 bytes under both the capabilities and bootstrap request IDs. These are logged as blob reads, not enough evidence to classify them as full SQLite restores or to identify whether they are separate application payloads. The new runtime data does not support a claim that page startup is fast. Capabilities showed Google, Bing, and YouTube next-eligible at 16:00 UTC; no Fetch POST was made. The isolated browser had no saved agreement for the current YouTube policy version. The Fetch button would redirect to the consent settings, so I did not record consent on the owner's behalf or submit a partial-source Fetch. No provider endpoint or quota was touched.

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
10. **Dashboard read coalescing.** The initial dashboard requests analytics and its first keyword page through one provider-free bootstrap GET, which runs those reads and the latest-dataset read through one request-scoped SQLite restoration. The browser renders the same response fields and retains the existing filtered/paginated keyword route. Production confirmed this route returns the combined data, but took 5.988 s. A separate latest-summary request remains for earlier summary rendering, and capabilities remains another startup request; overall page latency is still open.

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
| Page reload and view transitions avoid duplicate reads | **Partial** | Offline cache/navigation checks passed. The Production first load returned 200 but still issued separate capabilities, latest-summary, and bootstrap reads; capabilities took 7.523 s. Production transition/reload latency remains unmeasured. |
| Initial dashboard shares one SQLite restore for latest summary, analytics, and first keyword page | **Passed for coalescing; latency open** | Production bootstrap returned all three read results in one GET and took 5.988 s. The early latest-summary request is separate but does not restore SQLite. Snapshot-blob log attribution remains ambiguous. |
| Fetch with no provider work completes under 5 seconds | **Unproven** | No Production Fetch POST was made. The current browser had no saved YouTube policy agreement; clicking the default four-source Fetch would route to consent settings. Owner consent or an owner-consented session is required before this Production check. |
| Application release commit is on `origin/main`; Production is `READY` on that exact code SHA | **Passed** | Commit `49fcc68eb74cb77d3c371bc6ce232f18d30039b4` is deployed as `dpl_77t9y9Qb9ez6nv7M4PFknjwRrcmu`, state `READY`; the Production alias points to that deployment. A later report-only commit is excluded from Vercel builds by `ignoreCommand`. |

### Goal-level gate rollup (score basis)

| Goal gate | State | Basis |
| --- | --- | --- |
| G1 Repository synchronization and architecture analysis | **Passed** | Synced `origin/main`, checked repository instructions and reports, and audited Fetch, cache, persistence, read paths, and frontend startup. |
| G2 Cache-only Fetch path | **Partial** | Reuse/no-op implementation and offline regressions passed; Production under-five-second Fetch remains unmeasured. |
| G3 Storage and persistence optimization | **Partial** | Unchanged inputs skip the whole-image write; changed inputs still use the full SQLite snapshot. |
| G4 LIVE orchestration optimization | **Partial** | Checkpoint/read overhead was reduced and covered offline; no new provider-backed Production run was made. |
| G5 Backend read paths and frontend performance | **Partial** | Startup reads are coalesced in bootstrap, but Production capabilities/bootstrap measured 7.523 s/5.988 s. |
| G6 Incremental processing and scalability | **Partial** | Business digest reuse avoids unchanged analysis; changed-data persistence is not incremental. |
| G7 Consolidated focused regression verification | **Passed** | Final offline pass: 41 backend and 60 frontend tests plus lint, syntax, and diff checks. |
| G8 Commit, push, and Production READY | **Passed** | Application commit `49fcc68` is on `origin/main` and the exact SHA is Production READY. |
| G9 Production performance and data integrity | **Partial** | Read-only Production checks preserved the owner dataset and delivery states; performance targets remain open. |

Progress is scored as 3 fully passed gates plus 6 partial gates weighted at one half: `(3 + 6 × 0.5) / 9 = 66.7%`, rounded to **67%**. No gate is counted as fully passed solely because code was deployed.

### Consolidated offline verification

The final verification pass ran after implementation was complete:

- **261 focused backend tests passed:** 232 tests across 10 modules ran under the strict offline HTTP guard (`real_constructions=0`, `requests=0`); 29 Firestore integration tests passed with fake Firestore and mocked transport methods that were asserted not to send requests.
- **58 frontend Node tests passed**, with no failures.
- The offline Chrome Fetch-recovery harness passed.
- Ruff passed for every changed Python application and test file; Python compilation and `git diff --check` passed.

The tests use fixtures and mocked transports. No Production Fetch, provider endpoint, or LIVE provider was called. A cache-only Production Fetch is not a safe performance probe until refresh eligibility for that request is proven to forbid provider work. The under-five-second gate therefore remains open; this report does not claim the target has been met.

### Follow-up dashboard bootstrap verification

- **41 backend tests passed:** 39 Vercel shared LIVE route/composition regressions and 2 dashboard bootstrap regressions.
- **60 frontend Node tests passed**, including the initial request coalescing, cache expiry, and mutation invalidation checks.
- Ruff passed for the changed Python files; Python compilation, JavaScript syntax checks, and `git diff --check` passed.
- These checks use local fakes and do not call LIVE providers or Production Fetch. The combined bootstrap GET is now confirmed in Production; its 5.988 s runtime shows that the page performance gate remains open.

## Remaining limits

- The no-op path avoids Collections analysis and full-image snapshot work when source business inputs match. A Fetch still needs to read/validate the source cache evidence and persist its Run History receipt.
- When business input really changes, Collections continues to use the existing bounded, chunked SQLite snapshot. This work does not replace that storage model or claim that changed-data snapshot uploads are faster.
- Firestore row-level, partitioned/delta, and managed relational alternatives require data-model, migration, consistency, cost, and rollback decisions that cannot be inferred safely from the current repository. The report records these as future decisions rather than selecting an unapproved production service.
- SQL-backed analytics still require the SQLite working snapshot on a cold function instance. The dashboard bootstrap coalesces its initial reads into one restore but does not eliminate that full-image restore.
- Production read latency is still material: capabilities measured 7.523 s and dashboard bootstrap 5.988 s in the observed initial page load. This is separate from Fetch and does not prove the cache-only Fetch target.
- A cache-only Production Fetch remains unmeasured. The browser context had no saved YouTube policy agreement; no agreement was recorded on the owner's behalf.

## Release record

**Current application release:** `49fcc68eb74cb77d3c371bc6ce232f18d30039b4` (`perf(dashboard): share one SQLite restore on startup`), pushed to `origin/main`.

**Vercel Production:** deployment `dpl_77t9y9Qb9ez6nv7M4PFknjwRrcmu`, state `READY`, source SHA `49fcc68eb74cb77d3c371bc6ce232f18d30039b4`, region `iad1`. Alias `trend-scout-mu.vercel.app` resolves to this deployment.

**Read-only Production checks on 25 November 2026:** `/dashboard/`, `/api/health`, `/api/v1/capabilities`, `/api/v1/datasets/latest`, and `/api/v1/dashboard/bootstrap` returned HTTP 200. The dataset/latest and bootstrap responses preserve owner run `7c47b79810ea4238a21c16d3967d2b41`, 1,580 source records, and distinct `CACHED` and `STALE_CACHE` delivery labels. Runtime logs measured the startup API calls at 1.654 s, 7.523 s, and 5.988 s as listed above. These checks did not invoke a Fetch endpoint or a provider.

The earlier application release `27f861ed8e8c3bec190d0827c648b455bd3b9672` (`perf(vercel): skip unchanged cached fetch processing`) remains the first implementation release in this goal; it was superseded by `49fcc68` after the dashboard bootstrap change. The report-only commits `27e2eab4548baac0c7f95385011b681a68794dda` and `018a4741f18fc287726c6cf6cc35c2c3bd06f7f0` were excluded from Vercel builds by `ignoreCommand`; they did not change the Production alias.

The report-only commit `27e2eab4548baac0c7f95385011b681a68794dda` was pushed separately. Vercel created deployment record `dpl_CurUenNXx1q1hZf5CZenq65s3D8b` and marked it `CANCELED` after `ignoreCommand` returned exit code `0`; the event log confirms the application build did not run. The Production alias then remained on `dpl_HbKCifTWYBbNmFFyn4K5nUEWFeLZ` at code SHA `27f861ed8e8c3bec190d0827c648b455bd3b9672`. The same ignore behavior applied to the later report-only commit `018a4741f18fc287726c6cf6cc35c2c3bd06f7f0`; the alias is now verified on `49fcc68`.
