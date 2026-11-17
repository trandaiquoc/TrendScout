# TrendScout — Firebase Shared LIVE Runtime, Phase 1

**Status:** Active Windows 1 continuation; emulator, production IAM, and cross-runtime acceptance remain open.

**As of:** 2026-11-17

**Progress:** 61% (weighted evidence below).

**Target selected by the owner:** `trendscout-192e4/(default)`; production writes were not authorized for this phase and none were made.

## Acceptance gates

The percentage follows the weights in the Goal request. A `PASS` on a local deterministic test double means the adapter contract passed locally; it does not mean production Firestore is ready.

| Gate | Weight | Status and evidence | Credit |
| --- | ---: | --- | ---: |
| G0 — Git and architecture audit | 10% | **PARTIAL.** Repository, Git, Firebase project/database metadata, rules, existing adapters, source-cache contracts, and runtime wiring were inspected. Firestore collection contents and server IAM could not be inspected; the Firebase MCP has no document or IAM query tools and `gcloud` is unavailable on this machine. | 8% |
| G1 — Shared Firestore storage | 20% | **PASS — deterministic contract.** The injected Firestore snapshot adapter, optional external-payload support in `SharedCache`, and same-client cache factory pass the listed contracts against a transactional fake. Emulator follow-up tests have been added on Windows 1 but have not run; this remains local contract evidence only. | 20% |
| G2 — Distributed quota/coordination | 20% | **PARTIAL.** The sequential cross-instance fake contract remains passed. New emulator tests cover concurrent atomic updates, one-winner shared quota races, and lost-ack receipt recovery; they have not run on an isolated Linux emulator. | 8% |
| G3 — Cache and SQLite transition | 15% | **PARTIAL.** SQLite remains the runtime default. Added an explicit Firestore shared LIVE composition for admission, cache blobs, worker, and fetch facade; local composition tests pass and the NORMAL_DEMO plus SQLite Wikimedia regressions pass. Emulator-backed cache/storage acceptance remains unverified. | 12% |
| G4 — Cross-runtime acceptance | 15% | **BLOCKED.** The emulator acceptance now includes a child Python process reading the same synthetic cache through the source-handler path after the writer client closes, with a zero-provider-call assertion. This suite is authored but unrun; cross-machine Windows 1/Windows 2/Linux/Vercel reads, restarts, and concurrent execution remain unverified. | 0% |
| G5 — Security and regression | 15% | **PARTIAL.** Windows 1 reran the emulator guard, NORMAL_DEMO, SQLite LIVE, and composition regressions; Ruff passes. A test-only deny-rules fixture and unauthenticated emulator read/write checks are authored but unrun. Production backend IAM remains unverified; the existing production client rules were only confirmed to deny client access and were not changed. | 8% |
| G6 — Report and commits | 5% | **PASS.** The verified Windows 1 implementation and this acceptance update are committed locally. No push was made, so `origin/main` remains at the checkpoint SHA; a push could start the linked Vercel check, which the owner prohibited. | 5% |
| **Total** | **100%** | **Current Goal progress** | **61%** |

The 61% is a partial Goal state. It does not claim four-source shared LIVE is production-ready.

## Previous Windows 2 pause checkpoint (historical)

- At that earlier pause, progress was 57%. This historical checkpoint predates the Windows 1 continuation below; the multi-session Goal remained active.
- At the checkpoint before this report-only commit, local `HEAD` and `origin/main` both equaled `b9d979c9f99d2dabc770697a89da7203683cefc0`; `git status` was clean.
- The Backend suite was at 939 PASS / 2 SKIPPED at that earlier implementation checkpoint; it was not rerun after the Windows 1 composition changes. The focused Firestore cache tests and Ruff passed then. No provider requests or production Firestore writes were made.

## Windows 1 continuation checkpoint

- `git fetch origin` succeeded. Before edits, `git status` was clean, `git diff` was empty, and both `HEAD` and `origin/main` were `1f2009a19b09530a30eca3f1a591ee355de2dedb`.
- Python 3.12 is available. `wsl.exe` exists, but `wsl.exe --status` fails with `Class not registered` / `Wsl/CallMsi/Install/REGDB_E_CLASSNOTREG`. Java, `bwrap`, Docker, Podman, Firebase CLI, and `gcloud` are absent; emulator, ADC, and project environment variables are unset.
- The existing emulator safety guard is unchanged. Its ten configuration tests passed locally. This machine cannot satisfy the guard's required Linux namespace with only loopback and no external routes, plus a successful emulator REST probe.
- No managed Linux runner or GitHub Actions dispatch tool is exposed in this session. No workflow or push was used because the repository has a linked Vercel status check and the owner prohibited starting Vercel. No browser or machine UI was used, per the owner's instruction.
- Production backend IAM remains unverified; the existing client rules were not changed and do not establish server access. No production Firestore reads/writes, provider requests, Firebase billing changes, Vercel actions, or D7 actions were made.

## Git state and commits

### Windows 1 continuation state

- The required fetch completed before edits. At the start, local `main` and `origin/main` both equaled `1f2009a19b09530a30eca3f1a591ee355de2dedb`; they had zero commits of divergence.
- Windows 1 implementation commit: `f94d377b693cab2816d9bdeb0a36a91773025bc5` — `Add explicit Firestore shared LIVE runtime`, parent `1f2009a19b09530a30eca3f1a591ee355de2dedb`. Immediately after that commit, a fresh fetch confirmed local `main` was one commit ahead of `origin/main` and the worktree was clean.
- The report correction and follow-up emulator-test changes are committed locally; no continuation commit was pushed. The last fresh fetch left `origin/main` at `1f2009a19b09530a30eca3f1a591ee355de2dedb`. No force-push was used. A push is intentionally deferred because it may start the Vercel status check, which the owner prohibited.

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
- A report-only correction commit, `3ba6ae2155037cc54746c19353c5f51b91e029c9`, was then pushed from `82e5621`; a post-push fetch confirmed `HEAD` and `origin/main` matched at `3ba6ae2155037cc54746c19353c5f51b91e029c9`, with a clean worktree.
- At the earlier Windows 2 checkpoint, an IAM-tool availability note was committed and fast-forward pushed; that historical post-push `HEAD` and `origin/main` matched at `3ba6ae2155037cc54746c19353c5f51b91e029c9`, with a clean worktree.
- Do not push `.env*`, owner tokens, API credentials, SQLite databases, WAL/SHM files, or local runtime data.

## Implementation

### Added

- `Backend/app/storage/firestore_snapshot_blobs.py` adds `FirestoreSnapshotBlobs`, which accepts an explicitly injected Firestore client and creates no credentials or client itself. It stores bounded finite JSON lists under content-addressed manifest/chunk paths. Each raw chunk is at most 196,608 bytes; all chunks and their manifest are published in one bounded transaction. Reads verify the manifest, reference metadata, every chunk, total byte count, record count, and SHA-256 digest. Expired reads fail closed; `purge_expired` removes an expired manifest and its chunks atomically.
- `Backend/app/storage/firestore_shared_cache.py` adds `create_firestore_shared_cache`, an explicit opt-in factory that constructs `SharedCache` with `FirestoreAtomicStateStore` and `FirestoreSnapshotBlobs` bound to the same injected client. It does not create credentials, select a project, or connect by itself.
- `Backend/app/orchestration/firestore_shared_live_composition.py` adds an explicit opt-in LIVE composition that builds state, snapshot blobs, shared cache, admission, worker, and fetch facade from one injected client. It accepts only `trendscout-192e4/(default)` or the guarded `demo-trendscout-p3s1/(default)` emulator client. It creates no credentials/client and does not switch NORMAL_DEMO or existing SQLite LIVE compositions.
- `DistributedPublicFetch` now has an explicit `firestore_shared_live` mode with fail-closed checks for LIVE-declared handlers, Firestore atomic state, Firestore snapshot blobs, shared client identity, the `(default)` database, and processing-grant resolution. The default flag is false; other runtime modes remain mutually exclusive.
- `SharedCache` now optionally accepts a payload blob store and inline-byte limit. Existing inline schema-1 cache records and default SQLite behavior are unchanged. Oversized opted-in records use schema 2 with an external blob reference; the cache record is committed only after the blob write succeeds. Blob reads happen outside the atomic state callback, so transaction retries do not repeat external reads. A cache hit returns the original origin and fetched timestamp and does not call a provider.
- `Backend/tests/test_d5_firestore_shared_cache.py` exercises the injected adapter, cache contract, a YouTube cache-first handler, and the existing shared quota ledger with a deterministic transactional Firestore double.
- `Backend/tests/test_firestore_runtime_composition.py` checks one-client wiring and fail-closed project/database/blob-store requirements with offline fakes.
- `Backend/emulator_tests/test_firestore_shared_runtime.py` adds emulator cases for independent-client transaction contention, shared quota race, ambiguous commit receipt recovery, cache survival across client close/reopen and a separate Python process with zero provider calls, blob expiry/purge, and unauthenticated app read/write denial. The suite is authored but has not run because the required isolated Linux emulator environment is unavailable on Windows 1.
- `Backend/emulator_tests/firestore.deny_all.rules` is a test-only emulator fixture. It permits only the empty synthetic guard-probe collection and denies all application document reads and writes. It is not the production ruleset and is not evidence of server IAM.
- `Backend/emulator_tests/firebase.json` binds the test-only rules to the Firestore Emulator on loopback port 8080 and disables the emulator UI. It is not a root project config and is not a production deployment config.

### Not changed

- NORMAL_DEMO remains the default. No SQLite database or runtime cache was migrated, copied, reset, or deleted.
- No production Firestore document was read or written by the adapter; no YouTube, SerpApi, Wikimedia, or Bing provider calls were made for this Goal.
- No Firebase billing, app, rules, database, deployment, Vercel LIVE, or D7 changes were made.
- No dependency was added.

## Tests and checks

### Windows 1 continuation

- `test_firestore_runtime_composition.py` — **4 tests PASS** on the Windows 1 temporary Python 3.12 environment.
- Ruff on the updated emulator integration test — **PASS**; static compilation of its embedded child-process code — **PASS**. The Firestore Emulator test itself remains **NOT RUN**.
- NORMAL_DEMO — **7 tests PASS**; SQLite Wikimedia LIVE — **11 tests PASS**; existing composition regressions — **6 tests PASS**; emulator guard — **10 tests PASS**. These focused local checks passed earlier in this continuation and were not repeated without a relevant code change.
- Ruff on all changed Python files — **PASS** after the current changes.
- Firestore Emulator integration tests, including `test_firestore_transactional.py` and `test_firestore_shared_runtime.py` — **NOT RUN**. Windows 1 lacks an isolated Linux network namespace satisfying the unchanged guard; the exact environment blocker is recorded above.
- Full Backend suite (939 PASS / 2 SKIPPED) and 9 focused adapter tests — historical baseline only, before the Windows 1 runtime-composition changes; not repeated in this continuation.
- `git diff --check` — **PASS** before the local commit.

The deterministic fake models atomic transaction commit and rollback. It is not a Firestore Emulator and does not prove server-side transaction retries or multi-machine contention. The emulator test suite uses only the dedicated synthetic project and makes no provider calls when run; it has not run on Windows 1.

## Firebase inventory and production readiness

Read-only Firebase MCP/CLI inspection on 2026-11-17 found:

- Active project: `trendscout-192e4` (“Trendscout”), project number `556422620769`.
- Existing database: `(default)`, Firestore Native / Standard, `asia-southeast1`, `freeTier=true`. Delete protection and point-in-time recovery are disabled.
- Billing is disabled. No Firebase Apps, root `firebase.json`, `.firebaserc`, or project alias is configured in this repository. The owner selected the existing project/database as the eventual target. The new `Backend/emulator_tests/firebase.json` is isolated to emulator tests only.
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
- **Still unaccepted or unwired:** The new Firestore composition is opt-in and not connected to a production/default runtime. Backend service identity and least-privilege IAM, a bounded lifecycle for global admission state, real transaction contention/recovery evidence, and cross-runtime acceptance remain open.

## Runtime and emulator blockers

- The current Wikimedia and Google normal-LIVE compositions explicitly construct `LocalSQLiteAtomicStateStore` and `LocalSQLiteSnapshotBlobs`.
- `DistributedPublicFetch` enforces local SQLite for the existing normal LIVE profiles. Replacing that guard or silently falling back to local quota state would be unsafe.
- Windows 1 has `wsl.exe`, but its Linux subsystem is not operational: `wsl.exe --status` reports `Class not registered` / `Wsl/CallMsi/Install/REGDB_E_CLASSNOTREG`. Java, `bwrap`, Docker, Podman, Firebase CLI, and `gcloud` are absent. Emulator/ADC/project environment variables are unset. The existing emulator guard requires the synthetic project `demo-trendscout-p3s1`, numeric loopback host, a verified Firestore Emulator REST probe, and a Linux namespace exposing only loopback with no external routes; it was not bypassed.
- No safe managed Linux runner or workflow-dispatch tool is available to run the guarded tests in this session. The repo has a linked Vercel check and the owner prohibited starting Vercel, so no workflow push was made. Firebase's current CI guidance recommends `firebase emulators:exec`; non-Hosting emulators do not require Firebase login. This documented route does not remove the repository's stricter network-isolation guard. [Firebase Emulator Suite CI guidance](https://firebase.google.com/docs/emulator-suite/install_and_configure) (accessed 2026-11-17).
- The quota contract test covers sequential cross-instance reads/writes through one deterministic backing store. It does not establish concurrent transactions. Admission state is a single atomic document with a 700,000-byte limit; retention/compaction under sustained multi-host use remains unverified.
- The blob adapter stores at most 4,000,016 bytes of serialized list data across up to 22 documents per transaction. Runtime scheduling for `purge_expired`, storage-growth monitoring, and Firestore TTL/index configuration have not been deployed or accepted.
- Server client libraries bypass Firestore Security Rules and authenticate through Google Application Default Credentials; production backend access therefore needs IAM verification independently of the test rules. The production runtime identity and its effective project/database permissions were not available to inspect. [Firestore Rules testing and server-client guidance](https://firebase.google.com/docs/firestore/security/test-rules-emulator), [Firestore IAM roles and permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/firestore) (accessed 2026-11-17).

## Remaining acceptance work

1. Use a supported isolated Linux environment that can run `bwrap` and the Firestore Emulator without external routes; leave the existing emulator guard intact. Run both `Backend/emulator_tests/test_firestore_transactional.py` and `Backend/emulator_tests/test_firestore_shared_runtime.py`. Do not use YouTube or SerpApi quota; the new quota race uses only synthetic Wikimedia admission state and invokes no provider.
2. Verify the actual production backend identity and its effective Firestore IAM with an authorized read-only IAM inspection. The expected identity/permissions are not currently available; do not provision or broaden roles as part of verification. Google's role matrix is documented in the Firestore IAM reference linked above.
3. Complete shared-cache acceptance across independent clients/processes and runtimes, with restart durability and zero provider calls on cache hits. The authored emulator suite now covers two Firestore clients and a separate Python reader process, but has not run; Windows 1/Windows 2/Linux/Vercel runtime acceptance remains open.
4. Keep NORMAL_DEMO and current normal LIVE modes on SQLite. Do not perform any production Firestore write until the owner explicitly authorizes it. No billing, Vercel, D7, or provider-quota action is authorized by this Goal.

## Security limitations

- Never store provider keys, access tokens, raw provider responses, or secrets in the cache. `SharedCache` validates payload fields before externalizing them; the adapter itself is a storage boundary and does not create an auth policy.
- Firestore rules deny client access, but server IAM is unknown. Production credentials must be injected by the hosting/runtime secret mechanism and restricted to the specific collections/documents required.
- A manifest/hash check detects missing, altered, or mismatched chunks; the deterministic tests do not replace emulator security tests or an IAM/rules review.
- This phase did not access private SQLite data or copy Windows 2 local runtime evidence to GitHub.
