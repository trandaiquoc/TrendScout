# Vercel Shared LIVE Performance and Scaling Report

**Status:** Implementation, consolidated offline verification, and read-only production acceptance complete. No Production Fetch was submitted.
**Updated:** 25 November 2026
**Baseline revision:** `4c02c718b6177bc04aa4ab1b5069886236b620c5`
**Baseline production deployment:** `dpl_HKevZoBtmXC4xJxevvUQpvWSQ33k`
**Production alias:** `https://trend-scout-mu.vercel.app/`

This report records targeted changes to the existing Vercel Shared LIVE path. It does not approve a new product architecture or make a claim that the whole application has been load-tested. Baseline timings are measurements from the existing production deployment; expected changes below are structural until the new deployment has been checked.

## Scope and constraints

- Reduce repeated Firestore reads, checkpoint transactions, immutable blob writes, SQLite snapshot restoration, duplicate Collections analysis, and terminal response latency on the existing Shared LIVE path.
- Preserve provider-call counts, source records, data mode, observation times, source scope, retention rules, public response safety, and the existing 120-second function cap in `Backend/vercel.json`.
- Use only existing Vercel, Firestore, and application resources. No new service, dependency, plan, paid feature, provider permission, or data migration is introduced.
- Do not make a Production LIVE Fetch for this acceptance. Production verification is limited to read-only routes and deployment/runtime inspection. The cache-only Fetch path is eligible only if zero provider requests are guaranteed; it is not required for this release.

## Baseline measurements

The pre-change observations below were collected from the baseline deployment. Request wall time includes application work and external waits; it is not a direct measurement of billed active CPU.

| Operation or component | Baseline observation |
| --- | ---: |
| Initial Fetch admission POST | 7.933 s |
| Cached continuation POST | 76.748 s |
| Terminal Fetch status GET after continuation | 11.832 s |
| Source checkpoint load | 1.84–2.08 s per source |
| Source handler | 1.84–8.74 s |
| Source checkpoint save | 2.05–11.24 s |
| Collections database image | 72.6 MB |
| Compressed Collections snapshot | 6.75 MB |
| Snapshot save | 44 s, including 40.4 s upload |
| Collections processing request | 69.2 s |
| Read APIs | Several seconds per request |

The project function is currently capped at 120 seconds. Vercel documents `maxDuration` as the maximum function execution duration and states that the function is terminated when the configured maximum is exceeded ([Vercel function duration configuration](https://vercel.com/docs/functions/configuring-functions/duration), checked 25 November 2026). At the measured 76.748-second continuation baseline, the nominal margin to this project cap was about 43.3 seconds. No duration, memory, or region setting was changed.

The exact Firestore region was not confirmed during baseline inspection and remains unchanged. Source/provider limits, actual Vercel credentials, and production provider reachability are outside this report's verification.

## Request path and bottlenecks

```mermaid
sequenceDiagram
    participant UI as Browser
    participant API as Vercel Fetch Function
    participant FS as Firestore coordination and blobs
    participant DB as Collections SQLite image

    UI->>API: Initial Fetch POST
    API->>FS: Admit job and return durable ID
    UI->>API: Same-key continuation POST
    API->>FS: Claim job, load checkpoints, run one source, save result
    API->>FS: Previously, reread terminal status and checkpoint blobs
    API-->>UI: Terminal result
    UI->>API: Collections recovery POST
    API->>FS: Read terminal source results
    API->>DB: Previously, restore, reprocess, and write snapshot
    DB->>FS: Upload content-addressed snapshot chunks
```

The dominant repeated-run cost was work after source delivery: another terminal status read, repeated checkpoint coordination transactions, full Collections restoration and analysis, and re-upload of unchanged snapshot bytes. The slow first Collections build remains a different case: when source evidence is new, it still needs to run the established processing and persistence path.

## Implemented changes

1. **Terminal response from the active worker.** The completed continuation builds its terminal public projection from the already validated worker result, avoiding the follow-up terminal status read and checkpoint blob reads. It retains normal status projections, source time/coverage fields, discovery output, and YouTube's existing public record projection. The ordinary status route remains the fallback when a projection cannot be built.
2. **Skip discovery in Collections recovery.** Fetch processing and recovery request the source result projection without computing discovery that the Collections path does not use.
3. **Exact cache-only Collections reuse.** A versioned SHA-256 digest covers each source's status, mode, count, quality/error fields, stable coverage and normalized records. Only transient delivery markers/counters are excluded. Reuse requires a completed prior dataset, the same complete source set, LIVE/CACHED delivery, valid record counts, zero provider requests in both the source result and coverage when reported, matching digests, and non-empty scope coverage. Fresh, stale, unavailable, partial-incomplete, changed-scope, changed-record, or malformed evidence continues through normal processing. `MODEL_UNAVAILABLE` remains retryable.
4. **One-time legacy backfill.** Older dataset manifests have no input digest. Their first matching cache-only recovery will process and persist once to record digests; a later exact match can reuse that dataset. No database migration is needed because the digest is an additive source-manifest field.
5. **Avoid redundant SQLite restoration.** Deferred Shared LIVE requests read the latest dataset summary from the existing Firestore snapshot pointer. Public dataset and analytics responses remove internal digest fields before returning manifests.
6. **Read-only immutable cache hits.** Vercel Shared LIVE cache reads use Firestore's existing batch read when the admission clock is monotonic and the record would remain unchanged apart from `last_checked_at`. Expiry, malformed state, time regression, or retention tightening keeps the transactional path.
7. **Checkpoint claim snapshot reuse.** Source checkpoint loads use the already fenced checkpoint snapshot returned by the claim transaction when the grant and retention are unchanged. Retention shortening and other invalidating cases still use the owned transaction.
8. **Content-addressed blob reuse.** An active identical snapshot blob is not rewritten. Retention extension updates only the manifest/chunk retention fields atomically; expired content is rewritten. Reads continue to validate every chunk and its retention against the manifest.
9. **Browser no-op recovery.** When the backend confirms that the latest cached source evidence already matches the saved dataset, the UI loads saved dataset metadata if needed and skips Collections job polling, duplicate recovery, and analytics refresh.

The production workflow, source selection, provider adapters, request budgets, retention policy, and Vercel project configuration are unchanged. No provider request was added by these code changes.

## Correctness and acceptance checklist

- [x] Consolidated offline verification passes for the touched backend, storage, API, and browser paths.
- [x] Exact matching LIVE/CACHED evidence can reuse only a completed LIVE dataset and reports the saved dataset ID; regression coverage passed offline.
- [x] Changed source scope or content, non-zero/conflicting provider counts, stale delivery, malformed digest, incomplete sources, and non-completed processing fail closed or use normal processing; regression coverage passed offline.
- [x] The most recent saved production run still reports the four source outcomes, zero provider requests for each cached result, and original source timestamps/observation date (details below). No source adapter or timestamp logic changed in this release.
- [x] Internal source digests are absent from public history, analytics, and latest dataset responses.
- [x] Code commit `d2cfafb2d5ed6f1ed9730f6231197071a9646580` is pushed to `origin/main`; its Vercel Production deployment reached `READY` and the production alias mapped to it.
- [x] Production read-only checks returned HTTP 200 for capabilities, latest dataset, run history, analytics overview, and runtime; connector wall times are recorded below.
- [x] No Production LIVE Fetch or provider-call acceptance was performed.
- [x] No cache-only Production Fetch was submitted; no Production provider-call condition was needed.

## Verification and release record

### Consolidated offline verification

**Passed 25 November 2026.** One consolidated PowerShell run completed:

- Ruff passed on every changed Python application and test file.
- Backend focused suites passed: `test_d5_distributed_worker.py` (35), `test_d5_firestore_shared_cache.py` (29), `test_smart_collections.py` (36), `test_keyword_workbench.py` (26), `test_vercel_shared_live_fetch.py` (34), and `test_public_fetch.py` (15): **175 tests passed**.
- `browser_vercel_shared_fetch_e2e_offline.py`: **PASS**; unchanged cached evidence loads the saved dataset without analytics refresh or Collections polling.
- Frontend `node --test tests/*.test.mjs`: **55 passed, 0 failed**.
- `git diff --check`: **PASS**.
- Node printed its existing experimental `localStorage` warning; it did not affect the suite.

These are offline/fake-provider checks. They do not establish live Firestore latency, provider reachability, or a production end-to-end timing improvement.

### Deployment and read-only production acceptance

**Code deployment:** `dpl_9RaBjs8tSihQ6LB7QaFhWBrLec4b`, Production `READY`, source commit `d2cfafb2d5ed6f1ed9730f6231197071a9646580`, branch `main`, function region `iad1`. Vercel reported the alias `trend-scout-mu.vercel.app`; a separate alias lookup mapped it to the same deployment without a redirect. No project, domain, protection, billing, or region settings were changed.

The five checks below were GET requests through the protected Vercel connector on 25 November 2026. The measured time is connector round-trip wall time and includes its access/share-link handling; it is not an isolated Vercel function duration and must not be compared directly with the baseline request timings.

| Read-only route | HTTP | Connector round-trip | Result |
| --- | ---: | ---: | --- |
| `/api/v1/capabilities` | 200 | 5.409 s | Production, LIVE, Shared LIVE; all four sources enabled |
| `/api/v1/datasets/latest` | 200 | 4.531 s | Latest dataset `be19f94dd725cc80810ffe9d78e48843`, LIVE/PARTIAL, 1,580 received records, 3,447 retained keywords |
| `/api/v1/runs/history?limit=1&cursor=` | 200 | 5.544 s | Latest run is the same dataset; four CACHED source outcomes, zero requests each |
| `/api/v1/analytics/overview` | 200 | 3.671 s | Overview loaded; latest dataset matches; no digest field exposed |
| `/api/v1/runtime` | 200 | 3.294 s | System and SQLite available, integrity `OK`, 3,447 keyword rows, database 72,589,312 bytes |

Public dataset and history responses did not expose `source_input_digest`; the overview response did not expose it either. The latest saved run, created before this code deployment, remains evidence of previously delivered cached inputs and their provenance, not a live test of this release:

| Source | Outcome | Provider requests | Preserved source time |
| --- | --- | ---: | --- |
| Bing Search | SUCCESS / CACHED | 0 | `2026-11-25T01:32:55.620616Z` |
| Google Trends/Search | SUCCESS / CACHED | 0 | `2026-11-25T01:31:25.412106Z` |
| Wikimedia Pageviews | SUCCESS / CACHED | 0 | Observation date `2026-11-23`; cache evidence fetched `2026-11-25T01:33:20.581202Z` |
| YouTube Search | PARTIAL / CACHED | 0 | Source fetched `2026-11-25T01:33:55.099599Z`; cache delivered `2026-11-25T01:33:56.792886Z` |

No Fetch POST or provider endpoint was invoked during production verification. A cache-only request was intentionally not used to validate the optimization, so this release has no post-change production Fetch latency measurement.

## Risks and limits

- A fresh source result still requires the established Collections processing and snapshot commit. The measured 69.2-second Collections request and 44-second snapshot save are not claimed to be optimized for a first fresh build.
- The first cache-only run against a legacy dataset without digests must process once. The saved digest then enables later no-change reuse.
- A cache mismatch intentionally causes a false negative (extra processing) rather than risk reusing a dataset built from different inputs.
- The report has no post-change live Fetch timing because Production LIVE Fetch testing is prohibited. Offline tests establish behavior and work-count reductions, not production latency or throughput.
- The current 120-second Vercel function cap and current Firestore region remain as configured; this work does not claim an increased capacity limit or a specific regional latency improvement.
- Vercel's current pricing documentation distinguishes active CPU and provisioned-memory usage on Fluid Compute; wall-clock timings here are not converted into a cost estimate ([Vercel Fluid Compute pricing](https://vercel.com/docs/functions/usage-and-pricing), checked 25 November 2026).
