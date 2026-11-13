# D6.2.3.1 — Strict offline isolation recovery

**Run date:** 2026-11-13
**Status:** Remediation implemented; offline Python acceptance passed; native Windows browser gate pending.
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

The remediation is **7/8 gates accepted; G6 remains pending**. Do not report 100% or D6.2 complete.

## 6. Limitations and remaining LIVE blockers

- Linux Python 3.12.3 is secondary evidence; native Windows Python 3.12.10 and Chrome remain required for G6.
- The report's zero-DNS conclusion follows from intercepted transport construction and fixture-only code paths; no separate OS-level DNS syscall monitor was installed.
- The test's `LIVE` mode assertion verifies mode separation only. It is not live provider evidence.
- Wikimedia LIVE is still unverified. The historical one-request authorization and quota reservation remain consumed, and the approval is expired. No new authorization tranche, live budget, quota reset, or Fetch was created.
- Any future live attempt still requires a separate owner-approved authorization and resolution of the existing D6.2 data-processing, retention, policy and launcher-diagnostic blockers. Synthetic verification does not satisfy those requirements.

## 7. Next action

On native Windows Python 3.12.10 with Chrome, run:

```powershell
cd Backend
py -3.12 tests/run_d6_2_3_1_offline_acceptance.py
```

That run must pass the same guarded suite and the browser E2E before G6 can be marked PASS. Stop after D6.2.3.1; do not start a new LIVE attempt or D6.3.
