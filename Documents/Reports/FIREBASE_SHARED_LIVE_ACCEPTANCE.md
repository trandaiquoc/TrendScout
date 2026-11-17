# TrendScout — Firebase Shared LIVE Runtime, Phase 1

**Status:** Phase 1 handoff; the multi-session Goal remains active.

**As of:** 2026-11-17

**Progress:** 57% (weighted evidence below).

**Target selected by the owner:** `trendscout-192e4/(default)`; production writes were not authorized for this phase and none were made.

## Acceptance gates

The percentage follows the weights in the Goal request. A `PASS` on a local deterministic test double means the adapter contract passed locally; it does not mean production Firestore is ready.

| Gate | Weight | Status and evidence | Credit |
| --- | ---: | --- | ---: |
| G0 — Git and architecture audit | 10% | **PARTIAL.** Repository, Git, Firebase project/database metadata, rules, existing adapters, source-cache contracts, and runtime wiring were inspected. Firestore collection contents and server IAM could not be inspected with available read tools. | 8% |
| G1 — Shared Firestore storage | 20% | **PASS — deterministic contract.** Added an injected Firestore snapshot adapter, optional external-payload support in `SharedCache`, and an explicit factory that binds both stores to one supplied client. The read/write, identity, expiry, duplicate, integrity, purge, missing-blob, large-payload, provenance/timestamp, scope-isolation across all four source IDs, cross-instance, and atomic-visibility contracts pass against a transactional fake. | 20% |
| G2 — Distributed quota/coordination | 20% | **PARTIAL.** `PublicAdmission` uses the existing Firestore atomic-state port in a cross-instance fake test; a shared Google/Bing quota is enforced. No emulator-backed contention, real lease/fencing, or production transaction acceptance was possible. | 8% |
| G3 — Cache and SQLite transition | 15% | **PARTIAL.** SQLite remains the runtime default. Existing cache, Wikimedia, Google, and distributed-worker regression tests pass; the Firestore adapter is opt-in and is not wired into a runtime composition. | 8% |
| G4 — Cross-runtime acceptance | 15% | **BLOCKED.** No emulator or authenticated backend Firestore client was available. Windows 1/Windows 2/Linux/Vercel shared reads, restarts, and concurrent execution remain unverified. | 0% |
| G5 — Security and regression | 15% | **PARTIAL.** The full Backend suite, focused regression, Ruff, and diff checks pass. Firestore client rules deny reads and writes. Backend IAM, emulator security, and live Firestore behavior were not verified. | 8% |
| G6 — Report and commits | 5% | **PASS.** Implementation and report commits were pushed by fast-forward; a post-push fetch confirmed `HEAD` and `origin/main` at `82e56219f608c503c92f54db7b8bd9f0149d210a`. This correction is report-only and is synced after a final fetch. | 5% |
| **Total** | **100%** | **Current Goal progress** | **57%** |

The 57% is a partial Goal state. It does not claim four-source shared LIVE is production-ready.

## Git state and commits

- Initial `HEAD` and `origin/main` matched at `3dd80a534bc33152ecc7809831528f63caed2130`; the worktree was clean.
- Implementation commit: `0fe00e5b5fc0750c20abfc6dd961433b402fc4cf` — `Add bounded Firestore shared cache adapter`.
- After committing the implementation, `git fetch origin` succeeded. `origin/main` remained `3dd80a534bc33152ecc7809831528f63caed2130`; local `main` was one commit ahead, with a clean worktree and no divergence.
- Initial report commit: `8458f99c972712ca5ea3f22ac258005a0f6d74db` — `Document Firebase shared runtime handoff`.
- Earlier report evidence commit: `aaeb98c40b8eca52d0d1a346ef25284b2c025e90` — `Record Firebase handoff sync evidence`.
- Before that push, `git fetch origin` succeeded; `origin/main` was exactly `3dd80a534bc33152ecc7809831528f63caed2130`, local `main` was two commits ahead, `git diff --check origin/main...HEAD` passed, and the worktree was clean. A normal push moved `main` from `3dd80a5` to `8458f99`.
- A post-push fetch confirmed `HEAD` and `origin/main` both equal `8458f99c972712ca5ea3f22ac258005a0f6d74db`; the worktree was clean at that checkpoint.
- Follow-up contract test commit: `e16c0595917dfccba413cdf4934b1c72f93252a6` — `Test Firestore cache across four source scopes`. It verifies readback and non-collision for Wikimedia, Google, Bing, and YouTube cache scopes. This report revision records that evidence. The report/test commits were then checked against `origin/main` and pushed as a fast-forward; the resulting SHA and clean status are reported in the current session handoff. Never force-push.
- Explicit composition commit: `44d7006f404ea063922af0c6f0c09801140b41de` — `Add explicit Firestore cache composition`. It binds the atomic-state adapter and blob adapter to the same injected client; the full Backend test suite and Ruff passed after this change.
- Before the latest push, `git fetch origin` showed a clean worktree with two local commits ahead and zero remote-only commits; `git diff --check origin/main...HEAD` passed. A fast-forward moved `main` from `ccd49fc` to `82e5621`. A post-push fetch confirmed `HEAD` and `origin/main` both equal `82e56219f608c503c92f54db7b8bd9f0149d210a`, with a clean worktree.
- This revision is report-only. At edit time the worktree was clean at that synced SHA; fetch again and push this report commit only if the remote remains a fast-forward target. Never force-push.
- Do not push `.env*`, owner tokens, API credentials, SQLite databases, WAL/SHM files, or local runtime data.

## Implementation

### Added

- `Backend/app/storage/firestore_snapshot_blobs.py` adds `FirestoreSnapshotBlobs`, which accepts an explicitly injected Firestore client and creates no credentials or client itself. It stores bounded finite JSON lists under content-addressed manifest/chunk paths. Each raw chunk is at most 196,608 bytes; all chunks and their manifest are published in one bounded transaction. Reads verify the manifest, reference metadata, every chunk, total byte count, record count, and SHA-256 digest. Expired reads fail closed; `purge_expired` removes an expired manifest and its chunks atomically.
- `Backend/app/storage/firestore_shared_cache.py` adds `create_firestore_shared_cache`, an explicit opt-in factory that constructs `SharedCache` with `FirestoreAtomicStateStore` and `FirestoreSnapshotBlobs` bound to the same injected client. It does not create credentials, select a project, or connect by itself.
- `SharedCache` now optionally accepts a payload blob store and inline-byte limit. Existing inline schema-1 cache records and default SQLite behavior are unchanged. Oversized opted-in records use schema 2 with an external blob reference; the cache record is committed only after the blob write succeeds. Blob reads happen outside the atomic state callback, so transaction retries do not repeat external reads. A cache hit returns the original origin and fetched timestamp and does not call a provider.
- `Backend/tests/test_d5_firestore_shared_cache.py` exercises the injected adapter, cache contract, a YouTube cache-first handler, and the existing shared quota ledger with a deterministic transactional Firestore double.

### Not changed

- NORMAL_DEMO remains the default. No SQLite database or runtime cache was migrated, copied, reset, or deleted.
- No production Firestore document was read or written by the adapter; no YouTube, SerpApi, Wikimedia, or Bing provider calls were made for this Goal.
- No Firebase billing, app, rules, database, deployment, Vercel LIVE, or D7 changes were made.
- No dependency was added.

## Tests and checks

All checks below were run after the implementation changes:

- `python -m unittest discover -s tests` — **939 tests PASS, 2 SKIPPED**.
- `test_d5_firestore_shared_cache` — **9 tests PASS** after the final adapter changes.
- Ruff on all changed Python files — **PASS**.
- `git diff --check` — **PASS**.

The deterministic fake models atomic transaction commit and rollback. It is not a Firestore Emulator, does not prove server-side transaction retries or multi-machine contention, and made no network calls.

## Firebase inventory and production readiness

Read-only Firebase MCP/CLI inspection on 2026-11-17 found:

- Active project: `trendscout-192e4` (“Trendscout”), project number `556422620769`.
- Existing database: `(default)`, Firestore Native / Standard, `asia-southeast1`, `freeTier=true`. Delete protection and point-in-time recovery are disabled.
- Billing is disabled. No Firebase Apps, `firebase.json`, `.firebaserc`, or project alias is configured in this repository. The owner selected the existing project/database as the eventual target.
- Firestore client Security Rules currently deny all client reads and writes. This does not establish server access: Admin SDK/server requests use IAM, which was not inspected or provisioned.
- Firebase CLI/MCP authentication exists, but no backend ADC or service-account credential is configured. Do not treat Firebase MCP sign-in as backend authentication.
- The available Firebase MCP has project, app, environment, and rules inspection but no Firestore document/collection query tool. Existing collection contents are therefore **unknown**; collection names in repository code are not evidence that those collections already exist in the project.

**Production readiness: NOT READY / NOT ACCEPTED.** No backend identity/IAM was verified, no emulator or live transaction was exercised, and no production write was authorized. Do not enable billing or point the new adapter at production until the owner has reviewed identity, IAM, retention, concurrency limits, and the runtime switch.

Official references consulted on 2026-11-17: [Firestore quotas and limits](https://firebase.google.com/docs/firestore/quotas) and [Firestore transactions](https://firebase.google.com/docs/firestore/manage-data/transactions). The implementation budgets chunk/document and transaction sizes below those documented limits. Documentation lookup does not prove project credentials, API reachability, or runtime authorization.

## Existing code classification

- **Reusable, local/runtime tested:** `SharedCache`, `PublicAdmission`, source-specific `CacheScope`, and local SQLite atomic state/blob stores. Existing source handlers use the generic cache contract; SQLite-backed normal compositions remain protected by their current type checks.
- **Reusable candidate, not production wired:** `FirestoreAtomicStateStore` provides bounded one-document serializable transforms (700,000-byte JSON ceiling) with ambiguous-commit handling. The new blob adapter uses an injected Firestore client. These paths need emulator and real service-identity acceptance before runtime use.
- **Emulator guarded:** `app/jobs/firestore_transactional.py` implements transactional job leases, fencing, shard staging, and dataset pointer publication, but its coordinator requires the guarded synthetic Firestore Emulator client. Its emulator tests were not run in this phase.
- **Legacy/unsafe as a shared-LIVE switch:** `app/storage/firestore.py` constructs Firebase Admin clients from project/ADC for the older run API; it is not the current NORMAL_DEMO composition. Do not treat it as the shared cache/quota backend.
- **Missing:** A production Firestore runtime composition, backend service identity and least-privilege IAM, a bounded lifecycle for global admission state, real transaction contention/recovery tests, and cross-runtime acceptance.

## Runtime and emulator blockers

- The current Wikimedia and Google normal-LIVE compositions explicitly construct `LocalSQLiteAtomicStateStore` and `LocalSQLiteSnapshotBlobs`.
- `DistributedPublicFetch` enforces local SQLite for the existing normal LIVE profiles. Replacing that guard or silently falling back to local quota state would be unsafe.
- Firestore Emulator environment variables are absent. Java, `bwrap`, Docker, Podman, and WSL are unavailable; nothing is listening on port 8080. The existing emulator guard requires an isolated loopback-only network namespace and a synthetic project, so it was not bypassed.
- The quota contract test covers sequential cross-instance reads/writes through one deterministic backing store. It does not establish concurrent transactions. Admission state is a single atomic document with a 700,000-byte limit; retention/compaction under sustained multi-host use remains unverified.
- The blob adapter stores at most 4,000,016 bytes of serialized list data across up to 22 documents per transaction. Runtime scheduling for `purge_expired`, storage-growth monitoring, and Firestore TTL/index configuration have not been deployed or accepted.

## Windows 1 continuation point

1. Run `git fetch origin`; compare local `HEAD`, `origin/main`, and `git status`. Read this report and the implementation commit; do not recreate the adapter or repeat its passing fake tests unless code changes.
2. Establish an isolated Firestore Emulator environment that satisfies `Backend/app/storage/firestore_emulator.py`: synthetic project `demo-trendscout-p3s1`, numeric `127.0.0.1` host, no ADC/real-project env vars, and loopback-only network namespace. Install/provide Java and `bwrap` only in that isolated test environment; do not weaken the guard.
3. Run the existing `Backend/emulator_tests/test_firestore_transactional.py` integration suite and add focused emulator tests for `FirestoreSnapshotBlobs`, `SharedCache`, and `PublicAdmission`: transaction retries/ambiguous outcomes, concurrent duplicate writes, quota reservation races, cross-client readback, expiry/purge, and restart durability.
4. Keep NORMAL_DEMO on SQLite. Design a separate explicit Firestore runtime composition with injected least-privilege credentials, bounded state migration/retention, fail-closed behavior, and no split SQLite/Firestore quota accounting. Test it on the emulator before any production write.
5. After the owner separately authorizes a production test, write only synthetic, clearly namespaced records in `trendscout-192e4/(default)`, read them through a second client after restart, verify all four source scopes and zero additional provider calls on cache hits, and delete only those test records. Do not enable billing, deploy, or start D7 in this continuation.

## Security limitations

- Never store provider keys, access tokens, raw provider responses, or secrets in the cache. `SharedCache` validates payload fields before externalizing them; the adapter itself is a storage boundary and does not create an auth policy.
- Firestore rules deny client access, but server IAM is unknown. Production credentials must be injected by the hosting/runtime secret mechanism and restricted to the specific collections/documents required.
- A manifest/hash check detects missing, altered, or mismatched chunks; the deterministic tests do not replace emulator security tests or an IAM/rules review.
- This phase did not access private SQLite data or copy Windows 2 local runtime evidence to GitHub.
