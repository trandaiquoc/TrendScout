# TrendScout — Firebase Shared LIVE Runtime, Phase 1

**Status:** Active isolated-emulator acceptance continuation; the request-scoped WIF implementation is locally tested, Cloud Shell and WIF/IAM setup are owner-verified, and emulator, runtime token-exchange, least-privilege, and cross-runtime gates remain open.

**As of:** 2026-11-18

**Progress:** 64% (weighted evidence below).

**Target selected by the owner:** `trendscout-192e4/(default)`; production writes were not authorized for this phase and none were made.

## Latest continuation checkpoint - hosted runner retry (2026-11-18)

This checkpoint supersedes the 2026-11-17 runner-prepared checkpoint below.

- Ran `git fetch origin` before edits. The feature branch is `codex/firebase-shared-live-acceptance`; HEAD was `1d7473d5f73f2c9577187166b6343bba747ba25f`, `origin/main` was `86082460ce3956bd6837ddf5552b525acfbf367c`, and HEAD was two commits ahead of `origin/main`. The feature branch remote matched HEAD. The only worktree edits at this checkpoint are the acceptance runner and its workflow; no reset, force update, or main-branch write occurred.
- GitHub Actions run `44137951604` was rejected during workflow validation because `runner.temp` was referenced in a job-level environment value; no job or test ran. The corrected run `44138555508` checked out the exact source SHA `1d7473d5f73f2c9577187166b6343bba747ba25f`, installed Bubblewrap 0.9.0, Python 3.12.3, and Node 22.12.0; although the OpenJDK 21 package was installed, the runner PATH still selected OpenJDK 17.0.21. It then failed at the first Bubblewrap invocation with `bwrap: setting up uid map: Permission denied`. The runner exited during isolated preparation before repository fetch, dependency installation, emulator startup, or either integration suite. No emulator test passed or failed. GitHub uploaded log artifact `firestore-emulator-acceptance-44138555508-1` (artifact `14352752842`).
- The current uncommitted retry runs Bubblewrap through `sudo -n` only for namespace and mount setup, then uses `setpriv` to run preparation, the emulator, and test payloads as the unprivileged runner UID/GID with an empty capability set and `no_new_privs=1`. A runtime assertion checks those credentials. The emulator safety guard, deny-all rules, and test suites are unchanged. The workflow now places the installed Java 21 binary first on PATH and verifies Java 21. This fix has not yet been exercised by a Linux run. Before any source or dependency download, preparation also probes the IPv4 and IPv6 Google metadata endpoints and fails closed if either accepts a connection; the sandbox maps `metadata.google.internal` to loopback. This guard has not yet been exercised on Linux.
- Local checks on this patch passed: Git Bash syntax parse, YAML parse, workflow permission/scope assertions (`contents: read`, no secrets or OIDC permission), and `git diff --check`. Windows itself still cannot run the Linux namespace. The real Firestore Emulator suites remain unexecuted.
- The prior branch pushes triggered Vercel Preview builds; those were observed READY and were not Production deployments. No production deployment, D7 action, billing change, IAM change, Security Rules change, production Firestore operation, or provider API call occurred. Preview creation is recorded because the connected Vercel project automatically builds branch pushes.
- The test workflow has no production secrets or OIDC permission, clears the child environment, omits the host home and gcloud configuration from its sandbox, and uses only `demo-trendscout-p3s1/(default)`. Dependency preparation has outbound access for the public repository and package/emulator downloads; emulator and test execution is separate and has loopback-only networking. The existing guard and seccomp probe remain required. Cloud Shell metadata reachability is checked before those downloads begin.
- No acceptance credit is added for the failed runner attempt or the unexecuted emulator suites. Weighted progress remains 64% until a real emulator run passes. A one-command Cloud Shell invocation will pin the reviewed runner/source commit and return the complete generated log.

## Latest continuation checkpoint — Cloud Shell runner prepared (2026-11-17)

This checkpoint supersedes the Windows-only runner blocker and earlier IAM availability notes below.

- Required Git reconciliation completed before edits: fetch succeeded; worktree was clean; HEAD and origin/main both equaled 281bd7d7610489f778ddb1618d56491c22eeb1f0; divergence was 0/0. The commits previously called local-only are already upstream. No reset, merge, duplicate commit, or overwrite was needed.
- The owner reports the existing GCP WIF provider vercel-production is ACTIVE with the correct Vercel issuer, audience, production subject restriction, roles/iam.workloadIdentityUser binding, roles/datastore.user binding, and a Firestore (default) IAM condition. This is owner-verified evidence, not an independent IAM API read. No production operation was performed.
- The owner reports Cloud Shell provides Linux, Java 21, Python 3.12, Firebase CLI 15.34.2, Bubblewrap 0.9.0, and libseccomp. Its network-namespace check reported interfaces ["lo"], no external routes, and external connect result 101. This confirms runner availability only; it is not emulator acceptance.
- Added Backend/emulator_tests/run_cloud_shell_acceptance.sh. It pins the reviewed origin/main SHA, checks the Node runtime against the pinned Firebase CLI, uses only demo-trendscout-p3s1/(default), prepares dependencies and the Firestore Emulator artifact in a separate Bubblewrap filesystem with a cleared environment and no host home, /root, /run, gcloud config, or ADC mounted, then starts the emulator and both suites inside one Bubblewrap network and PID namespace. It raises loopback, checks only lo/no external routes and blocked egress, runs the existing seccomp probe, and uses firebase emulators:exec for readiness and shutdown. The existing emulator guard and test rules are unchanged. Firebase CLI release notes identify startup errors with older than Node 20.19/22.12; the preflight follows those minimums ([official release notes](https://firebase.google.com/support/release-notes/cli), checked 2026-11-17).
- The preparation sandbox has network access for the public repository, PyPI/npm dependencies, and the Firestore Emulator artifact. Emulator and test execution uses a separate no-egress namespace; the suites cannot reach provider APIs. No YouTube/SerpApi quota is used.
- Local evidence: runner passes Bash syntax validation; Python 3.12 compilation of both emulator suites, the guard, and seccomp/network probe passes; the existing emulator safety-guard suite passes 10/10 on the Windows Python 3.12 venv. The Firestore Emulator suites have not run, so none of their acceptance cases are marked PASS.
- Static fixture audit found 25 emulator test methods (16 transactional and 9 shared-runtime). The only direct HTTP client call is the unauthenticated rules test using the guarded client host and synthetic project; the Wikimedia URL in the transactional suite is provenance fixture data. No provider HTTP client calls were found. This is source inspection, not executed emulator evidence.
- No workflow was created or dispatched. No Vercel, D7, billing, IAM, Security Rules, production Firestore, provider, or Git push action occurred. The runner and updated report are committed locally; origin/main has not been changed.

## Acceptance gates

The percentage follows the weights in the Goal request. A `PASS` on a local deterministic test double means the adapter contract passed locally; it does not mean production Firestore is ready.

| Gate | Weight | Status and evidence | Credit |
| --- | ---: | --- | ---: |
| G0 — Git and architecture audit | 10% | **PARTIAL.** Git and repository wiring are reconciled. The owner reports the active WIF provider, issuer/audience/production subject restriction, and IAM bindings/condition as verified; this was not independently re-read. Vercel still masks environment values, and runtime identity/effective permissions remain unverified. | 8% |
| G1 — Shared Firestore storage | 20% | **PASS — deterministic contract.** The injected Firestore snapshot adapter, optional external-payload support in `SharedCache`, and same-client cache factory pass the listed contracts against a transactional fake. Emulator follow-up tests have been added on Windows 1 but have not run; this remains local contract evidence only. | 20% |
| G2 — Distributed quota/coordination | 20% | **PARTIAL.** The sequential cross-instance fake contract remains passed. New emulator tests cover concurrent atomic updates, one-winner shared quota races, and lost-ack receipt recovery; they have not run on an isolated Linux emulator. | 8% |
| G3 — Cache and SQLite transition | 15% | **PARTIAL.** SQLite remains the runtime default. Added an explicit Firestore shared LIVE composition for admission, cache blobs, worker, and fetch facade; local composition tests pass and the NORMAL_DEMO plus SQLite Wikimedia regressions pass. Emulator-backed cache/storage acceptance remains unverified. | 12% |
| G4 - Cross-runtime acceptance | 15% | **BLOCKED.** The two emulator suites were not reached: the hosted runner failed at Bubblewrap UID-map setup before emulator preparation. A privilege-drop retry is prepared but unrun. Cross-machine Windows 1/Windows 2/Linux/Vercel reads, restarts, and concurrent execution also remain unverified. | 0% |
| G5 - Security and regression | 15% | **PARTIAL.** Ten focused read-only/authentication tests and Ruff pass; the local emulator safety guard passes 10/10. The owner reports the production WIF provider and bindings/condition verified. The hosted runner's first attempt failed before tests; real Google token exchange, effective least privilege, and emulator-rules acceptance remain open. | 11% |
| G6 - Report and commits | 5% | **PASS.** The WIF implementation/tests/docs, emulator-test lint cleanup, source-isolation/expiry cases, and runner workflow are on the dedicated feature branch in focused commits. The report records current workflow evidence. `origin/main` is unchanged; no force-push or production deployment occurred. | 5% |
| **Total** | **100%** | **Current Goal progress** | **64%** |

The 64% is a partial Goal state. It does not claim four-source shared LIVE is production-ready. The one added point reflects owner-verified WIF/IAM configuration; it does not count Cloud Shell diagnostics or unexecuted emulator tests as acceptance.

## Current subgate status

| Status | Evidence |
| --- | --- |
| **PASS** | Git reconciliation; unchanged emulator guard; local safety-guard tests (10/10); Python 3.12 compilation of both emulator suites and network scripts; runner Bash syntax. Prior deterministic shared-cache and NORMAL_DEMO/SQLite evidence remains as previously recorded. |
| **PARTIAL** | Owner-verified Cloud Shell network isolation and WIF/IAM configuration; code-level external-account plus service-account impersonation; local fake/cache contracts and explicit injected Firestore composition. This does not prove a real token exchange or emulator acceptance. |
| **BLOCKED** | The GitHub-hosted runner's first attempt stopped before test startup at Bubblewrap UID-map setup; its privilege-drop retry is not yet run. Cross-runtime Windows 1/Windows 2/Linux/Vercel acceptance; actual Google token exchange and independently verified effective least-privilege permissions also remain blocked. |
| **NOT_STARTED** | The real Firestore Emulator suites and their transaction contention/recovery, duplicate-write handling, quota race, lease/fencing, cache isolation, cross-client/process reads, restart/expiry/retention, zero-provider cache-hit, and emulator-rules cases. No suite tests ran in the failed workflow. Production Shared LIVE and production Firestore writes are also not started or authorized. |

## Prior Windows 1 checkpoint evidence (historical; superseded above)

This checkpoint supersedes the earlier Windows 1 notes below that described the three continuation commits as local-only or described origin/main as 1f2009a.

### Git reconciliation and workflow review

- At the start of this continuation, reran git fetch origin, git status, git diff, and git log -1 --oneline before edits: HEAD was f02d3b607ab8b2e04b0578ec60fcf37c09b0420f, origin/main was 284615c583d8d82dce897a82020b0514d6076a7e, the worktree was clean, and divergence was 0/5.
- On the original 61% resume, local HEAD and origin/main matched at 284615c583d8d82dce897a82020b0514d6076a7e; f94d377, c2c463a, and 284615c were already upstream. This continuation began from local report checkpoint 6ad0f9a while origin/main remained 284615c. No merge, reset, duplicate commit, or overwrite was needed.
- The only repository workflow is .github/workflows/publish-policy-pages.yml. It is manual workflow_dispatch, requires owner_approved=true, and builds/deploys GitHub Pages policy files; it is not a Linux Firestore test workflow. No workflow was dispatched.
- Read-only GitHub status for 284615c includes a successful Vercel context. This confirms a linked Vercel check exists; no push or deployment was started.
- Local continuation commits are 6ad0f9a (checkpoint report), eca1378 (WIF implementation), 8ee1666 (WIF evidence), 5589e32 (emulator-test lint), f02d3b6 (static-check report), and 621f336 (source-isolation/expiry cases). Before this report update, HEAD was 621f336, origin/main remained 284615c, the worktree was clean, and the branch was ahead by six commits. This report update is committed separately. No push was made.

### Prior Windows-only runner status (superseded by the Cloud Shell checkpoint above)

- The existing emulator safety guard was inspected and left unchanged. It requires the exact synthetic project demo-trendscout-p3s1, numeric loopback 127.0.0.1:PORT, a successful Firestore Emulator REST probe, and a Linux namespace with only lo and no IPv4/IPv6 external routes. The client factory also explicitly requires callers to run under bwrap --unshare-net.
- On Windows 1, wsl.exe --status reports Class not registered / Wsl/CallMsi/Install/REGDB_E_CLASSNOTREG. WSL was not repaired. Java, bwrap, Docker, Podman, Firebase CLI, and gcloud are unavailable. Python 3.12 exists in a temporary virtual environment, but Windows does not provide the required Linux network namespace. Emulator and project variables are unset; the local ADC file is absent.
- No connected Google IAM/Firestore inspection tool or GitHub workflow-dispatch tool is available. The existing GitHub workflow is not a test runner.
- Vercel Sandbox APIs are exposed, but there is no pre-existing sandbox. Creating one would start Vercel-hosted compute, with metered CPU/memory and sandbox creation usage; it was excluded under the owner's no-Vercel/no-billing constraints. No sandbox was created. See [Vercel Sandbox](https://vercel.com/docs/sandbox) and [Vercel pricing](https://vercel.com/pricing).
- Consequently, Backend/emulator_tests/test_firestore_transactional.py and Backend/emulator_tests/test_firestore_shared_runtime.py were **NOT RUN** on this resume. The fixtures and authored cases do not count as executed evidence. Both suites remain blocked until a permitted isolated Linux runner is supplied with Java, Firebase Emulator, Python 3.12, bwrap, and loopback-only emulator networking. The emulator safety guard was not weakened or bypassed.
- Coverage audit found no explicit emulator assertion for cross-source cache grants or the Firestore cache stale-to-retention-expiry transition; added test_firestore_cache_isolates_source_scopes_and_grants and test_firestore_cache_enforces_stale_and_retention_expiry in 621f336. The fixtures are synthetic and make no provider calls. Python 3.12 AST parsing passes for both emulator Python files, and Ruff passes on Backend/emulator_tests. These tests remain unrun, and static checks add no functional acceptance credit.

### Prior owner-reported WIF configuration and verification state (superseded by the Cloud Shell checkpoint above)

| Area | State | Evidence and limitation |
| --- | --- | --- |
| Google WIF pool/provider, issuer, source-token audience, production subject, service account, and Datastore User database condition | **CONFIGURED - owner-reported, not independently verified** | Owner supplied pool trendscout-vercel, provider vercel-production, issuer https://oidc.vercel.com/rakoon1, source-token audience https://vercel.com/rakoon1, subject owner:rakoon1:project:trend-scout:environment:production, service account trendscout-vercel-backend@trendscout-192e4.iam.gserviceaccount.com, and roles/datastore.user conditioned to (default). No Google Cloud IAM/provider API or authenticated read-only Console inspection was available in this session. |
| Vercel Production environment variables | **CONFIGURED - presence verified, values unavailable** | Vercel read-only APIs list GOOGLE_CLOUD_PROJECT and GOOGLE_WIF_AUDIENCE targeting Production. Both return visibility=secret and decrypted=false. Their configured values were not read or compared with the owner-supplied project/provider values. Project metadata reports SSO protection but has no OIDC setting field, so Secure Backend Access remains unverified. |
| Request-scoped Google WIF and service-account impersonation | **IMPLEMENTED** | Code rejects any project/provider audience other than trendscout-192e4 and the configured provider resource; it preflights the Vercel issuer, source-token audience, exact production subject, and expiration, then uses Google Auth external-account credentials and impersonated_credentials.Credentials for the designated service account. No ADC or key fallback exists. Local claim parsing is rejection-only; Google STS must still validate the token signature and provider conditions. [Vercel OIDC claims](https://vercel.com/docs/oidc/reference), [Google Auth external-account credentials](https://google-auth.readthedocs.io/en/latest/reference/google.auth.external_account.html), [Google Auth impersonated credentials](https://google-auth.readthedocs.io/en/latest/reference/google.auth.impersonated_credentials.html). |
| Focused local authentication/read-only suite | **TESTED - PASS** | Python 3.12.10 ran Backend/tests/test_vercel_readonly.py: 10/10 tests pass. Cases cover correct and wrong issuer/audience/subject, wrong project/provider, missing config/token, expired/malformed token, mocked STS then IAM generateAccessToken for the intended service account, impersonation 403, Firestore PermissionDenied without ADC fallback, and the existing Vercel read-only route protections. Ruff passes on the two changed Python files. STS/IAM/Firestore are mocked; this is not a real credential exchange or production access test. |
| Actual Google IAM and runtime token exchange | **BLOCKED / PARTIAL** | The actual workload identity provider, issuer/audience config, exact impersonation binding, service-account role binding, database condition, and effective permissions were not read. Vercel hides the environment values; no real Vercel request token was accessed or exchanged. |
| Production Shared LIVE | **NOT_STARTED** | The explicit injected FirestoreSharedLiveComposition remains unused by app/main.py. The existing Vercel API remains read-only; no production Firestore operation was made. |

The owner-reported roles/datastore.user role is a read/write role that includes datastore.entities.*. If that binding exists as described, it grants more than the legacy private-run GET path needs; a correct database condition would still cover all documents in (default). The role may have been selected for future Shared LIVE writes, but production Vercel must remain read-only and the role/condition must be confirmed before activation. Do not treat OAuth scopes or read-only HTTP routes as an IAM write boundary. Firestore server clients use IAM rather than client Security Rules; Google supports custom roles and database-level IAM Conditions. [Firestore IAM permissions and custom roles](https://docs.cloud.google.com/firestore/native/docs/security/iam), [database access conditions](https://docs.cloud.google.com/firestore/native/docs/manage-databases), [WIF service-account impersonation best practices](https://docs.cloud.google.com/iam/docs/best-practices-for-using-workload-identity-federation). Official docs checked 2026-11-17.

Owner read-only verification still needed: in project trendscout-192e4, inspect **IAM & Admin -> Workload Identity Federation** for pool/provider issuer, allowed audience, mappings, and CEL conditions; inspect **IAM & Admin -> Service Accounts -> trendscout-vercel-backend -> Permissions** for a roles/iam.workloadIdentityUser binding limited to the exact production principal; and inspect project IAM bindings/conditions for roles/datastore.user and resource projects/trendscout-192e4/databases/(default). Vercel **Project Settings -> Security** must separately confirm Secure Backend Access with OIDC. The Vercel API cannot reveal the two environment values in this session; the owner can confirm that GOOGLE_CLOUD_PROJECT equals trendscout-192e4 and GOOGLE_WIF_AUDIENCE equals the provider resource shown in this report without sharing tokens. Google Cloud Console does not enforce database IAM conditions while browsing the database, so use a read-only IAM policy export/API check to confirm the condition; do not test by accessing production documents. No changes to IAM, rules, environment variables, billing, or production data are requested here.

### Protected system behavior

- No production Firestore read or write, provider API call, Firebase billing change, Vercel deployment, D7 action, workflow dispatch, WSL repair, or Git push occurred.
- NORMAL_DEMO and SQLite were not changed. Existing four-source integrations, quota accounting, YouTube policy consent, and LIVE/CACHED behavior were not modified.
- At the prior checkpoint progress was 63%. The current 64% adds one weighted point for the owner-verified WIF/IAM configuration; no emulator, token-exchange, or cross-runtime credit is claimed.


## Previous Windows 2 pause checkpoint (historical)

- At that earlier pause, progress was 57%. This historical checkpoint predates the Windows 1 continuation below; the multi-session Goal remained active.
- At the checkpoint before this report-only commit, local `HEAD` and `origin/main` both equaled `b9d979c9f99d2dabc770697a89da7203683cefc0`; `git status` was clean.
- The Backend suite was at 939 PASS / 2 SKIPPED at that earlier implementation checkpoint; it was not rerun after the Windows 1 composition changes. The focused Firestore cache tests and Ruff passed then. No provider requests or production Firestore writes were made.

## Windows 1 continuation checkpoint (historical; superseded by latest resume evidence above)

- `git fetch origin` succeeded. Before edits, `git status` was clean, `git diff` was empty, and both `HEAD` and `origin/main` were `1f2009a19b09530a30eca3f1a591ee355de2dedb`.
- Python 3.12 is available. `wsl.exe` exists, but `wsl.exe --status` fails with `Class not registered` / `Wsl/CallMsi/Install/REGDB_E_CLASSNOTREG`. Java, `bwrap`, Docker, Podman, Firebase CLI, and `gcloud` are absent; emulator, ADC, and project environment variables are unset.
- The existing emulator safety guard is unchanged. Its ten configuration tests passed locally. This machine cannot satisfy the guard's required Linux namespace with only loopback and no external routes, plus a successful emulator REST probe.
- No managed Linux runner or GitHub Actions dispatch tool is exposed in this session. No workflow or push was used because the repository has a linked Vercel status check and the owner prohibited starting Vercel. No browser or machine UI was used, per the owner's instruction.
- Production backend IAM remains unverified; the existing client rules were not changed and do not establish server access. No production Firestore reads/writes, provider requests, Firebase billing changes, Vercel actions, or D7 actions were made.

## Git state and commits

### Windows 1 continuation state (historical; superseded by the latest resume evidence above)

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

## Prior runtime and emulator blockers (historical Windows state)

- The current Wikimedia and Google normal-LIVE compositions explicitly construct `LocalSQLiteAtomicStateStore` and `LocalSQLiteSnapshotBlobs`.
- `DistributedPublicFetch` enforces local SQLite for the existing normal LIVE profiles. Replacing that guard or silently falling back to local quota state would be unsafe.
- Windows 1 has `wsl.exe`, but its Linux subsystem is not operational: `wsl.exe --status` reports `Class not registered` / `Wsl/CallMsi/Install/REGDB_E_CLASSNOTREG`. Java, `bwrap`, Docker, Podman, Firebase CLI, and `gcloud` are absent. Emulator/ADC/project environment variables are unset. The existing emulator guard requires the synthetic project `demo-trendscout-p3s1`, numeric loopback host, a verified Firestore Emulator REST probe, and a Linux namespace exposing only loopback with no external routes; it was not bypassed.
- No safe managed Linux runner or workflow-dispatch tool is available to run the guarded tests in this session. The repo has a linked Vercel check and the owner prohibited starting Vercel, so no workflow push was made. Firebase's current CI guidance recommends `firebase emulators:exec`; non-Hosting emulators do not require Firebase login. This documented route does not remove the repository's stricter network-isolation guard. [Firebase Emulator Suite CI guidance](https://firebase.google.com/docs/emulator-suite/install_and_configure) (accessed 2026-11-17).
- The quota contract test covers sequential cross-instance reads/writes through one deterministic backing store. It does not establish concurrent transactions. Admission state is a single atomic document with a 700,000-byte limit; retention/compaction under sustained multi-host use remains unverified.
- The blob adapter stores at most 4,000,016 bytes of serialized list data across up to 22 documents per transaction. Runtime scheduling for `purge_expired`, storage-growth monitoring, and Firestore TTL/index configuration have not been deployed or accepted.
- Server client libraries bypass Firestore Security Rules and authenticate through Google Application Default Credentials; production backend access therefore needs IAM verification independently of the test rules. The production runtime identity and its effective project/database permissions were not available to inspect. [Firestore Rules testing and server-client guidance](https://firebase.google.com/docs/firestore/security/test-rules-emulator), [Firestore IAM roles and permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/firestore) (accessed 2026-11-17).

## Remaining acceptance work

1. Run Backend/emulator_tests/run_cloud_shell_acceptance.sh from the owner-prepared Cloud Shell. Return the complete trendscout-firestore-acceptance-<UTC>.log file. Emulator and tests use only the synthetic project with no external route.
2. Independently verify the production backend identity and effective Firestore IAM with read-only inspection, then confirm real token exchange and least privilege without production Firestore writes. Do not provision or broaden roles as part of verification.
3. Complete shared-cache acceptance across independent clients/processes and runtimes, with restart durability and zero provider calls on cache hits. The authored emulator suite now covers two Firestore clients and a separate Python reader process, but has not run; Windows 1/Windows 2/Linux/Vercel runtime acceptance remains open.
4. Keep NORMAL_DEMO and current normal LIVE modes on SQLite. Do not perform any production Firestore write until the owner explicitly authorizes it. No billing, Vercel, D7, or provider-quota action is authorized by this Goal.

## Security limitations

- Never store provider keys, access tokens, raw provider responses, or secrets in the cache. `SharedCache` validates payload fields before externalizing them; the adapter itself is a storage boundary and does not create an auth policy.
- Firestore rules deny client access, but server IAM is unknown. Production credentials must be injected by the hosting/runtime secret mechanism and restricted to the specific collections/documents required.
- A manifest/hash check detects missing, altered, or mismatched chunks; the deterministic tests do not replace emulator security tests or an IAM/rules review.
- This phase did not access private SQLite data or copy Windows 2 local runtime evidence to GitHub.
