# D6.2.3.1 — Strict offline isolation recovery

**Run date:** 2026-11-13
**Status:** Accepted for the owner-authorized Linux browser scope; native Windows browser behavior was not rerun.
**Provider requests:** 0. **Pushes/deployments:** 0.

## 1. Git baseline and synchronization

- Repository: `/mnt/Data/Mine/TrendScout`.
- `git fetch origin` completed before review.
- Starting `HEAD` and fetched `origin/main`: `401c30d2ed6d112966ffa25cf17507a45e24a0f3`.
- Starting tree: clean; no staged or unstaged changes; local and remote histories had zero divergence.
- No reset, overwrite, force operation, push, or deployment occurred.
- Test remediation commit: `77be0bd` (`test(d6): isolate offline HTTP transports`).
- The documentation commit is recorded in the final handoff and local Git history.

The environment available for this run was Ubuntu Linux with the repository venv at Python 3.12.3. It did not expose native Windows, PowerShell, or Python 3.12.10. Knowns code navigation was unavailable because this checkout has no `.knowns/` directory; the specified sources and reports were inspected directly.

## 2. Historical failure and confirmed cause

D6.2.3's historical acceptance remains **7/8, G2 FAIL**. The included test `test_controlled_network_constructor_opens_no_request` called `WikimediaTopPageGateway.controlled_network()`. That production factory constructed `httpx.AsyncHTTPTransport(verify=True, trust_env=False, retries=0)` before validating source approval. The test sent no request and did not resolve provider DNS, but the transport construction alone violated strict offline isolation. The historical report was left intact.

## 3. Remediation

The regression test now enters `strict_offline_http_guard()`. The scoped helper replaces HTTPX's constructor with an `AsyncBaseTransport` fake and patches the original class initializer as a second fail-fast barrier, including code holding a reference to the original class. The fake raises on unmocked request dispatch, tracks construction and closure, and is restored at scope exit. `MockTransport` and `ASGITransport` are not patched.

The fake class is temporarily exposed under `httpx.AsyncHTTPTransport` inside the test scope so the live gateway's exact type check continues to evaluate as written. The production implementation, exact-type requirement, construction token, approval behavior, and network behavior were not changed. No environment-variable bypass was introduced.

The repaired test verifies:

- a valid fixture approval permits factory construction and preserves `data_mode == "LIVE"` as a mode contract only;
- the fake receives `verify=True`, `trust_env=False`, and `retries=0`;
- missing, expired, and invalid-scope approvals fail closed and close the fake;
- invalid gateway initialization closes the allocated fake;
- a constructor failure creates no returned transport to leak;
- successful closure works and no background task remains;
- zero original constructors and zero fake request dispatches occur.

The synthetic pipeline still uses its separate `httpx.MockTransport`; no synthetic record or fixture is labeled as provider-verified LIVE evidence.

The `test_d6_local_live_http.py` module also runs inside the scoped guard, ensuring the existing patched local fixture handlers cannot accidentally construct a real transport. `run_d6_2_3_1_offline_acceptance.py` wraps the full focused suite in the guard and aggregates counts.

## 4. Offline evidence and commands

Acceptance command, run from `Backend`:

```text
.venv/bin/python tests/run_d6_2_3_1_offline_acceptance.py
```

Result: **60 tests passed** across `test_d5_wikimedia_gateway.py`, `test_http_policy.py`, `test_d6_wikimedia_synthetic_e2e.py`, `test_d6_local_live_http.py`, `test_d6_offline_worker_composition.py`, `test_distributed_discovery.py`, `test_d6_dashboard_capabilities.py`, and `test_public_fetch.py`.

The passing output reported:

- strict offline guard: ACTIVE;
- real `AsyncHTTPTransport` constructions: **0**;
- unmocked fake transport requests: **0**;
- fake transport constructions: **13** across the guarded acceptance run;
- the previously offending constructor test was included and passed.

The API synthetic E2E asserted exactly **1 Wikimedia MockTransport request per isolated E2E fixture run**, with zero Google/Bing/YouTube gateway requests. The local Wikimedia-only HTTP fixture asserted exactly **1 intercepted Wikimedia fixture request** after one Fetch; idempotent replay did not send another. The run uses temporary synthetic state and did not load provider credentials or a real approval record. Because every provider-capable transport construction was intercepted and all fixture sends were mocked, the test paths made no provider DNS lookup.

Additional checks passed:

```text
.venv/bin/ruff check tests/offline_http_guard.py tests/test_d5_wikimedia_gateway.py tests/test_d6_local_live_http.py tests/run_d6_2_3_1_offline_acceptance.py
.venv/bin/python -m compileall -q tests/offline_http_guard.py tests/test_d5_wikimedia_gateway.py tests/test_d6_local_live_http.py tests/run_d6_2_3_1_offline_acceptance.py
git diff --check
```

Python syntax compilation and Ruff passed. No JavaScript changed. The browser launcher `browser_wikimedia_synthetic_e2e_offline.py` was not run: it requires the task's primary native Windows Chrome environment, which is not available here. The acceptance runner reports that skip explicitly.

The canonical LIVE ledger was **not opened or modified**. Tests use temporary SQLite files and fixture approvals. No live approval was activated, no quota or policy changed, and no credential was loaded for a provider request.

## 5. Acceptance gates

| Gate | Result | Evidence |
| --- | --- | --- |
| G1 Git baseline | PASS | Fetch succeeded; initial `HEAD` matched `origin/main`; clean tree and zero divergence. |
| G2 Strict offline isolation | PASS for this Python acceptance run | Guard active across all 60 tests; 0 real HTTP transport constructions; 0 unmocked transport sends; no provider credentials or real approvals; canonical ledger untouched. The browser gate was not run. |
| G3 Constructor contract | PASS | Fake captured exact configuration; valid/missing/expired/wrong-scope approval cases; cleanup on approval and initialization failure; mode remains a test contract; production security checks unchanged. |
| G4 Synthetic pipeline preservation | PASS | API, worker, checkpoint, discovery, replay, and regression tests passed with isolated fixtures. |
| G5 Provenance and business semantics | PASS | Existing assertions retain `SYNTHETIC`, null scores and insufficient evidence; live discovery without grants remains blocked; no volume or purchase-demand inference was introduced. |
| G6 Dashboard browser E2E | BLOCKED | Required native Windows Chrome run was unavailable. The new harness explicitly reports NOT RUN; prior D6.2.3 browser evidence remains historical and is not reused as this gate. |
| G7 Security and regression | PASS for focused Python checks | 60 focused tests, Ruff, syntax compilation, and diff check passed under the guard. |
| G8 Documentation and handoff | PASS, except G6 remains pending | Historical failure preserved, this recovery report and main report pointer added, local commits created, and no push/deployment made. |

The **initial** remediation run was 7/8 with G6 pending. The owner-authorized Linux follow-up and its separate final matrix are recorded in Section 8; D6.2 remains incomplete.

## 6. Limitations and remaining LIVE blockers

- Linux Python 3.12.3 is secondary evidence; native Windows Python 3.12.10 and Chrome remain required for G6.
- The report's zero-DNS conclusion follows from intercepted transport construction and fixture-only code paths; no separate OS-level DNS syscall monitor was installed.
- The test's `LIVE` mode assertion verifies mode separation only. It is not live provider evidence.
- Wikimedia LIVE is still unverified. The historical one-request authorization and quota reservation remain consumed, and the approval is expired. No new authorization tranche, live budget, quota reset, or Fetch was created.
- Any future live attempt still requires a separate owner-approved authorization and resolution of the existing D6.2 data-processing, retention, policy and launcher-diagnostic blockers. Synthetic verification does not satisfy those requirements.

## 7. Initial acceptance status

The first D6.2.3.1 run passed 7/8 gates and left G6 pending because it ran on Linux without a supported loopback network sandbox. That historical run and its limitations remain part of this report. The owner subsequently authorized Linux Chrome for the remaining browser acceptance; this follow-up is recorded below and does not claim Windows browser compatibility.

## 8. Owner-authorized Linux browser acceptance (2026-11-13)

### Environment and network isolation

- OS: Ubuntu 24.04.5 LTS; kernel `6.17.0-1033-oem`.
- Python: 3.12.3 from `Backend/.venv`.
- Browser: Google Chrome `157.0.8103.50`.
- Unprivileged user and network namespaces were available. The run used `unshare --user --map-root-user --net --fork`, then enabled only `lo` with `ip link set lo up` inside the disposable namespace. No host interface, route, firewall, or network configuration was changed.
- The browser preflight observed only `lo`, zero non-loopback IPv4 routes, zero non-loopback IPv6 routes, successful TCP loopback connectivity, and `ENETUNREACH` (errno 101) for TEST-NET-1. Thus the test namespace had no external interface or route and external egress was unavailable.
- The preflight checked Chrome and its descendants; all 15 observed Chrome process-tree PIDs used the test network namespace. The namespace and its processes were destroyed on command exit.
- Browser-run namespace identifier: `net:[4026532747]`.
- The original `require_linux_network_isolation()` all-IP-blocked seccomp check remains unchanged for other browser suites. Only this D6 test uses the new loopback-only network-namespace preflight.

### Commands and results

The guarded suite ran first and then invoked the existing browser test's `main()` inside the same isolation boundary:

```text
unshare --user --map-root-user --net --fork bash -c 'set -euo pipefail; ip link set lo up; cd Backend; exec .venv/bin/python tests/run_d6_2_3_1_offline_acceptance.py'
```

- Guarded Python suite: **60/60 passed**, including `test_controlled_network_constructor_opens_no_request`.
- Strict HTTP guard: active; **0** real `AsyncHTTPTransport` constructions, **0** unmocked transport requests, **13** fake transport constructions.
- Chrome E2E: **PASS**, executed against the actual dashboard assets, loopback API, admission, worker, and synthetic fixture. One Fetch POST, one status poll, one Wikimedia `MockTransport` request, **34** scripted browser assertions with zero failures. Mobile viewport and post-reload no-duplicate-job checks also passed.
- Browser checks covered duplicate clicks/idempotency, same-origin requests, credentials, HTML injection, synthetic provenance, unavailable Google/Bing/YouTube sources, insufficient evidence, null candidate scores, no search-volume claim, Fashion and Home & Living filters, source/date/scope evidence, themes, locales, and reload behavior.
- Real provider requests: **0**. Provider DNS/Internet access was unavailable because the namespace had no external interfaces or routes. The fixture request to the Wikimedia hostname remained inside `MockTransport` and performed no DNS lookup.
- Canonical LIVE ledger: not opened or modified. No credentials, real approvals, quota changes, or live launchers were used.
- Ruff, Python syntax compilation, and `git diff --check` passed for the changed files.

### Updated acceptance gates

| Gate | Result | Evidence |
| --- | --- | --- |
| G1 Git baseline | PASS | Follow-up began clean at `14729618849b0eff8cd1958bf41fd01da447e484`, matching `origin/main`; no history rewrite or push. |
| G2 Strict offline isolation | PASS | Guarded suite and browser ran in the route-less namespace; zero real transports, unmocked sends, provider requests, or external egress. |
| G3 Constructor contract | PASS | Fake constructor config, approval cases, cleanup, mode distinction, and production type/security checks remain verified. |
| G4 Synthetic pipeline | PASS | 60 regression tests and actual synthetic API/worker/dashboard E2E passed. |
| G5 Provenance and semantics | PASS | SYNTHETIC shown; scores null; insufficient evidence retained; no live, volume, or purchase-demand claim. |
| G6 Dashboard browser E2E | PASS for owner-authorized Linux scope | Chrome 157; one Fetch, one Wikimedia fixture request, 34 browser assertions, zero failures; loopback-only egress verification passed. |
| G7 Security and regression | PASS | Guard, namespace boundary, process tree, focused tests, Ruff, compilation, and diff checks passed. |
| G8 Documentation and handoff | PASS | Historical G2 failure preserved; reports updated; local commit created; no push or deployment. |

**[100%] D6.2.3.1 — LINUX OFFLINE ACCEPTED.** This acceptance uses the owner's explicit Linux scope update. Windows browser compatibility was not rerun or newly verified. D6.2.3 historical acceptance remains 7/8 with G2 FAIL; the D6.2.3.1 initial Linux run remains recorded as 7/8 with G6 not run. Wikimedia LIVE is still unverified and remains blocked by the consumed/expired one-request authorization and the outstanding processing, retention, policy, and launcher-diagnostic decisions. Stop after D6.2.3.1; do not begin a live attempt or D6.3.

**Follow-up code commit:** `1e879b8` (`test(d6): run isolated Linux Chrome E2E`).
