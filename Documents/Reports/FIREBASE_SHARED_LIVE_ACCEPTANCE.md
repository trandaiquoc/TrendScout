# TrendScout — Firebase Shared LIVE Runtime, Phase 1

**Status:** Active acceptance continuation; both Firestore Emulator suites pass 25/25 on the isolated Linux runner. Request-scoped WIF checks are locally tested, while live Google token exchange, effective IAM, and Windows/Vercel cross-runtime acceptance remain unverified. Production Shared LIVE is not enabled.

**As of:** 2026-11-18

**Progress:** 80% (weighted evidence below).

**Target selected by the owner:** `trendscout-192e4/(default)`; production writes were not authorized for this phase and none were made.

## Latest continuation checkpoint - full isolated Emulator pass (2026-11-18)

- Before this report update, `git fetch origin` completed. The feature branch was clean at `48c8f45618e1e1793e09340a8d528d3cc0951e32`; `origin/main` remained `86082460ce3956bd6837ddf5552b525acfbf367c`, and the feature remote matched the branch. No reset, force-push, or main-branch write occurred.
- GitHub Actions run [44218830785](https://github.com/trandaiquoc/TrendScout/actions/runs/44218830785) checked out exactly `48c8f45618e1e1793e09340a8d528d3cc0951e32` and completed successfully. Both guarded Firestore Emulator suites ran all **25 tests: 25 passed, 0 failed, 0 errors** in 21.581 seconds (`RESULT=PASS`).
- The run used only `demo-trendscout-p3s1/(default)` and Firestore Emulator at `127.0.0.1:8080`. Bubblewrap 0.9.0, Python 3.12.3, Java 21.0.13, and Node 22.12.0 were reported. The network probe confirmed only loopback, blocked external connect (result 101), and passed the seccomp probe. The synthetic-project guard was not bypassed. No production credentials, provider API requests, or production Firestore operations were used.
- Earlier runner diagnostics are resolved for this acceptance run: `44209551272` had 23 passes and two test-fixture errors; `44217155811` had 24 passes and one Emulator lock-timeout error from four simultaneous quota contenders; `44217711881` stalled during pre-test apt setup and was canceled when the replacement run started. The fixtures now avoid an illegal duplicate state transition and use valid, distinct admission metadata. The quota race uses two independent contenders and still asserts exactly one `NEW` plus one `QUOTA_EXHAUSTED`; the separate atomic-state stress case retains four writers. Production state-machine and quota contracts were not weakened.
- Workflow commit `48c8f45` caps the isolated runner prerequisite-install step at eight minutes; the full job remains bounded. The final run passed. The workflow change is supported by the [GitHub Actions workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).
- Local Windows 2 regressions passed: admission 22/22, Firestore runtime composition 4/4, and Emulator guard 12/12; Ruff, Python compilation, YAML parse, and `git diff --check` also passed on the code changes. The focused Vercel read-only suite was rerun with the repository's unittest runner: **10/10 passed**. Its identity tests use synthetic JWTs, and its STS exchange is mocked; these results verify local rejection/fail-closed behavior, not a real Vercel signature, Google STS exchange, or IAM grant.
- Read-only Vercel metadata shows the deployment for commit `48c8f45` is `READY` with `target: null` (a branch Preview, not Production). Production environment values remain masked and the project metadata does not independently establish that OIDC is enabled. No Vercel Production deployment or route was invoked.
- The owner previously reported that WIF/IAM was configured and checked from Cloud Shell. That remains owner-reported evidence, not an independent current IAM read. This Windows 2 session has no `gcloud` executable or Google credential environment; the connected Firebase tools expose Firestore metadata but not workload identity provider or IAM policy inspection. The current browser-access helper could not safely identify the Chrome URL, so no Cloud Console inspection was made. No real Vercel request token was obtained or exchanged.
- The read-only Firestore inventory still identifies `trendscout-192e4/(default)` as the intended target. No production document read or write, billing change, IAM or Security Rules change, provider call, D7 action, or Production LIVE test occurred; the earlier Firebase check read database metadata only.
- Weighted progress is now **80%**. The full isolated Emulator suites remove the prior transaction/quota/publication test failures. Remaining credit is held for independent identity/IAM evidence, untested Windows/Vercel interoperability, and the unaccepted runtime transition. This is not production readiness.

## Prior hosted-runner retry checkpoint (2026-11-18)

This checkpoint supersedes the 2026-11-17 runner-prepared checkpoint below.

- At the start of this resume, ran `git fetch origin` before edits. The feature branch was `codex/firebase-shared-live-acceptance`; HEAD was `1d7473d5f73f2c9577187166b6343bba747ba25f`, `origin/main` was `86082460ce3956bd6837ddf5552b525acfbf367c`, and HEAD was two commits ahead of `origin/main`. The feature branch remote matched HEAD. At that checkpoint, only the acceptance runner and workflow were uncommitted; no reset, force update, or main-branch write occurred.
- After those checks, focused commits `2bf6dc8`, `fec7131`, `f8b3c2e`, `9a71431`, and `1703393` were pushed to the feature branch without force. At the current report edit, HEAD and the feature remote are `17033931e0798f5ef5f2ecaf519971623bd02710`, `origin/main` remains `86082460ce3956bd6837ddf5552b525acfbf367c`, and the feature branch is seven commits ahead. The current worktree contains the next runner and report fix.
- GitHub Actions run `44137951604` was rejected during workflow validation because `runner.temp` was referenced in a job-level environment value; no job or test ran. Run `44138555508` checked out source SHA `1d7473d5f73f2c9577187166b6343bba747ba25f`, installed Bubblewrap 0.9.0 and Python 3.12.3, but initially selected OpenJDK 17.0.21 and failed at Bubblewrap UID-map setup before source fetch, dependencies, emulator startup, or tests. Run `44141597816` on `2bf6dc87d7cd20fc28cbf335576593311fb43fe6` failed at an overbroad metadata preflight; the follow-up distinguished the non-Google link-local service. Run `44142376296` on `fec7131a36f33766d793e71420e2e6dab135eb11` exposed and led to fixing the npm config collision. Run `44143427054` on `f8b3c2e2d4ede84d309c6fe3cbab707c409a0e5d` passed metadata, source fetch, dependencies, npm, and Firebase CLI 15.34.2, then hit a temp-file permissions error under `/tmp`. Run `44146997808` on `9a71431f6c9ac9400e8c1238bd5eb1228ebfa4d5` confirmed `[apiv2] GET https://storage.googleapis.com/firebase-preview-drop/emulator/cloud-firestore-emulator-v1.22.0.jar 200` and traced the failure to `EACCES` creating `/tmp/tmp-...`. Run `44147597823` on `17033931e0798f5ef5f2ecaf519971623bd02710` downloaded JAR v1.22.0 and verified SHA-256 `9b6498b7f62714d67f48f59b3818883cd682dbcd46b9f59511de81c97bb5166c`, passed loopback-only network checks and seccomp probe, but Firestore exited with code 1 during startup. The runner reported `firestore-debug.log` but did not include its contents; no integration test started. Latest log artifact: `14356618641`. No emulator test passed or failed.
- The runner uses `sudo -n` only for namespace and mount setup, then `setpriv` to run preparation, emulator, and test payloads as the unprivileged runner UID/GID with no capabilities and `no_new_privs=1`; runtime assertions verify those credentials. Metadata checks, exact-source fetch, Python/npm install, Java 21, Firebase CLI, and the pinned emulator JAR have passed. Loopback-only networking and the seccomp probe also passed. The Firestore process exited before tests, so the runner now bind-mounts a runner-owned temporary directory at `/tmp` and captures the final `*debug.log` contents on emulator/test failure. The emulator port is under `emulators.firestore`; deny-all rules and the safety guard are unchanged. These fixes are unrun. [Pinned Firebase CLI emulator metadata](https://github.com/firebase/firebase-tools/blob/v15.34.2/src/emulator/downloadableEmulatorInfo.json), checked 2026-11-18.
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

A PASS on deterministic or Emulator evidence means the tested contract passed in that environment; it does not prove production IAM or live Vercel access.

| Gate | Weight | Status and evidence | Credit |
| --- | ---: | --- | ---: |
| G0 - Git and architecture audit | 10% | **PARTIAL.** Git and repository wiring are reconciled. The owner reports the WIF provider and IAM setup as checked, but this session could not independently reread the provider, bindings, or effective permissions. | 8% |
| G1 - Shared Firestore storage | 20% | **PASS - isolated Emulator.** Shared cache source isolation, expiry/retention, blob cleanup, and the full existing storage suite passed against the guarded Emulator. Production Firestore has not been written. | 20% |
| G2 - Distributed quota/coordination | 20% | **PASS - isolated Emulator.** Lease fencing, one-winner claims, idempotent creation, quota admission race, ACK-loss recovery, retry behavior, publication pointer CAS, and four-writer atomic-state contention passed in the two suites. | 20% |
| G3 - Cache and SQLite transition | 15% | **PARTIAL.** The explicitly injected Firestore Shared LIVE composition and current NORMAL_DEMO/SQLite regressions pass. SQLite remains the runtime default; no accepted runtime switch connects Shared LIVE to production. | 14% |
| G4 - Cross-runtime acceptance | 15% | **PARTIAL.** A synthetic cache hit survives Emulator client/process reopen with zero provider requests. Windows 1/Windows 2/Linux/Vercel interoperability and shared restart recovery across machines remain unverified. | 2% |
| G5 - Security and regression | 15% | **PARTIAL.** Network isolation, seccomp, synthetic-project guard, Emulator rules denial, local regression suites, and 10/10 offline Vercel identity tests passed. Real Vercel token exchange and effective least-privilege IAM remain unverified. | 11% |
| G6 - Report and commits | 5% | **PASS.** This acceptance report is updated on the isolated feature branch; `origin/main` remains unchanged. | 5% |
| **Total** | **100%** | **Current Goal progress** | **80%** |

The remaining 20 points are G0 (2), G3 (1), G4 (13), and G5 (4). The score reflects passing Emulator and offline evidence; it does not claim that four-source Shared LIVE is production-ready or enabled.

## Current subgate status

| Status | Evidence |
| --- | --- |
| **PASS** | Run `44218830785`: isolated loopback-only network, no external routes, seccomp, synthetic-project guard, and all 25 Emulator cases passing. |
| **PASS** | Local Windows 2 checks: 22/22 admission, 4/4 runtime composition, 12/12 Emulator guard, and 10/10 Vercel read-only unit tests. The Vercel STS test is mocked. |
| **PARTIAL** | Emulator cache hit after client/process reopen passes; real cross-machine Windows 1/Windows 2/Linux/Vercel shared-state interoperability remains untested. Owner-reported Cloud Shell and WIF/IAM setup has not been independently reread in this session. |
| **OPEN / ACCESS-LIMITED** | Real production-subject Vercel OIDC token, Google STS exchange, and effective IAM least privilege have not been independently verified. Current Vercel Preview has no production target and the backend trust is production-subject restricted. |
| **NOT STARTED** | Production Shared LIVE, application-data reads/writes for this acceptance, billing changes, IAM changes, Security Rules changes, provider calls, and D7 were not started. |

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

Read-only Firebase MCP/CLI metadata inspection on 2026-11-17 found:

- Active project: `trendscout-192e4` (`Trendscout`), project number `556422620769`.
- Existing database: `(default)`, Firestore Native / Standard, `asia-southeast1`, `freeTier=true`. Delete protection and point-in-time recovery are disabled.
- Billing is disabled. No Firebase Apps, root `firebase.json`, `.firebaserc`, or project alias is configured in this repository. The owner selected the existing project/database as the eventual target. `Backend/emulator_tests/firebase.json` is isolated to Emulator tests only.
- Firestore client Security Rules deny all client reads and writes. This does not prove server access: server libraries authenticate through IAM, which this session did not inspect. Collection contents remain unknown.
- Firebase MCP authentication is available for project/database metadata, but does not provide an IAM policy or workload identity provider inspection. Firebase MCP sign-in is not backend authentication.
- The owner previously reported an active Vercel WIF provider, production subject restriction, `roles/iam.workloadIdentityUser`, `roles/datastore.user`, and a `(default)` database IAM condition. Treat these as owner-reported until a current read-only IAM response is independently reviewed.

**Production readiness: NOT READY / NOT ACCEPTED.** Both isolated Emulator suites pass, but live WIF token exchange, effective production IAM, cross-runtime consistency, and the Shared LIVE runtime transition remain unverified. Only database metadata was inspected; no production application data or writes were touched. Do not enable billing or point the new adapter at production until identity, IAM, retention, concurrency limits, and the runtime switch are accepted.

Official references consulted on 2026-11-18: [Vercel OIDC request headers and token claims](https://vercel.com/docs/oidc), [Vercel OIDC claim reference](https://vercel.com/docs/oidc/reference), and [Google Workload Identity Federation least-privilege guidance](https://docs.cloud.google.com/iam/docs/best-practices-for-using-workload-identity-federation). These documents describe provider behavior; they do not establish the settings or permissions of this project. Firestore quotas and transaction behavior were referenced from [Firestore quotas and limits](https://firebase.google.com/docs/firestore/quotas) and [Firestore transactions](https://firebase.google.com/docs/firestore/manage-data/transactions) (checked 2026-11-17).

## Existing code classification

- **Locally and Emulator tested:** `SharedCache`, `PublicAdmission`, source-specific `CacheScope`, Firestore atomic state/blob storage, and explicit shared-runtime composition. All 25 existing Emulator cases pass on the guarded synthetic project.
- **Read-only Vercel code path present, not live-verified:** `/api/runs` and analytics history receive the per-request `x-vercel-oidc-token` and call `read_only_storage`; local tests verify strict claim rejection and no ADC fallback. The STS test is mocked. Vercel documents the request-header token behavior, but project OIDC enablement and a real exchange have not been verified in this session.
- **Shared LIVE composition is opt-in and not production wired:** `create_firestore_shared_live_composition` takes an explicitly injected client. Current normal LIVE and NORMAL_DEMO profiles keep local SQLite state/cache. The passing Emulator composition test does not change that default.
- **Legacy/unsafe as a shared-LIVE switch:** `app/storage/firestore.py` can construct Firebase Admin clients from project/ADC for the older run API. It is not the current NORMAL_DEMO composition and is not the Shared LIVE cache/quota backend.
- **Still unaccepted:** Backend identity and least-privilege IAM, a bounded lifecycle for global admission state, cross-machine interoperability, and the runtime transition. Keep production Shared LIVE disabled until these gates are accepted.

## Prior runtime and emulator blockers (historical Windows state)

- The current Wikimedia and Google normal-LIVE compositions explicitly construct `LocalSQLiteAtomicStateStore` and `LocalSQLiteSnapshotBlobs`.
- `DistributedPublicFetch` enforces local SQLite for the existing normal LIVE profiles. Replacing that guard or silently falling back to local quota state would be unsafe.
- Windows 1 has `wsl.exe`, but its Linux subsystem is not operational: `wsl.exe --status` reports `Class not registered` / `Wsl/CallMsi/Install/REGDB_E_CLASSNOTREG`. Java, `bwrap`, Docker, Podman, Firebase CLI, and `gcloud` are absent. Emulator/ADC/project environment variables are unset. The existing emulator guard requires the synthetic project `demo-trendscout-p3s1`, numeric loopback host, a verified Firestore Emulator REST probe, and a Linux namespace exposing only loopback with no external routes; it was not bypassed.
- No safe managed Linux runner or workflow-dispatch tool is available to run the guarded tests in this session. The repo has a linked Vercel check and the owner prohibited starting Vercel, so no workflow push was made. Firebase's current CI guidance recommends `firebase emulators:exec`; non-Hosting emulators do not require Firebase login. This documented route does not remove the repository's stricter network-isolation guard. [Firebase Emulator Suite CI guidance](https://firebase.google.com/docs/emulator-suite/install_and_configure) (accessed 2026-11-17).
- The quota contract test covers sequential cross-instance reads/writes through one deterministic backing store. It does not establish concurrent transactions. Admission state is a single atomic document with a 700,000-byte limit; retention/compaction under sustained multi-host use remains unverified.
- The blob adapter stores at most 4,000,016 bytes of serialized list data across up to 22 documents per transaction. Runtime scheduling for `purge_expired`, storage-growth monitoring, and Firestore TTL/index configuration have not been deployed or accepted.
- Server client libraries bypass Firestore Security Rules and authenticate through Google Application Default Credentials; production backend access therefore needs IAM verification independently of the test rules. The production runtime identity and its effective project/database permissions were not available to inspect. [Firestore Rules testing and server-client guidance](https://firebase.google.com/docs/firestore/security/test-rules-emulator), [Firestore IAM roles and permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/firestore) (accessed 2026-11-17).

## Remaining acceptance work

1. Independently inspect the current Google workload identity provider, issuer/audience/attribute condition, exact production subject binding, service-account impersonation policy, and effective Firestore role/database condition using read-only IAM access. Do not provision or broaden roles.
2. Verify a real production-subject Vercel token exchange through Google STS and service-account impersonation, then prove the effective scope without production Firestore writes. This requires an authorized production runtime token path; do not use a Preview token, since the trust is restricted to the production subject.
3. Complete independent-client and cross-machine shared-cache/restart acceptance across Windows 1, Windows 2, and Linux. The Emulator child-process cache-hit case passed, but that does not prove Vercel or separate-host consistency.
4. Keep NORMAL_DEMO and current normal LIVE modes on SQLite. Do not perform production writes, enable billing, change IAM or Security Rules, deploy/activate Production Shared LIVE, call provider APIs, or perform D7 until the required owner authorization and acceptance evidence are in place.

## Security limitations

- Never store provider keys, access tokens, raw provider responses, or secrets in the cache. `SharedCache` validates payload fields before externalizing them; the adapter itself is a storage boundary and does not create an auth policy.
- Firestore rules deny client access, but server IAM is unknown. Production credentials must be injected by the hosting/runtime secret mechanism and restricted to the specific collections/documents required.
- A manifest/hash check detects missing, altered, or mismatched chunks; the deterministic tests do not replace emulator security tests or an IAM/rules review.
- This phase did not access private SQLite data or copy Windows 2 local runtime evidence to GitHub.
