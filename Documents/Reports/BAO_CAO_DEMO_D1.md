# TrendScout Demo Online — D1 Implementation Report

- **Date/time:** 2026-11-11 11:05 ICT
- **Repository:** /mnt/Data/Mine/TrendScout
- **Branch:** main
- **Baseline HEAD:** e4764a89865c24600b2ca6ede47f05b694d7a900
- **D0 documentation commit:** 4a2e1a858883ef1d2771008251c12277c5a3882e
- **D1 implementation commit:** ec3a5cdb6f72d01ca60da5d30499254cda9d92a0
- **D1 scoped-formatting commit:** 9ab5c7b6713334c1fc51ad63f9451b30e817d154
- **D1 verdict:** PASS for the requested local configuration, source boundary, HTTP policy, and regression scope. No live multi-source readiness or owner approval is inferred.

## Summary

D1 expands the existing settings entry point into one typed, validated, injectable configuration boundary. A versioned source catalog centralizes endpoints, provider names, credential-reference names, market/language descriptions, metrics, and readiness. The versioned `demo-us-en` query plan has 14 canonical seeds and a deterministic fingerprint. A shared bounded HTTP policy is now used by the existing Wikimedia adapter.

Wikimedia keeps its existing source ID, top-per-country US/all-access request, fixture behavior, scoring inputs, and public compatibility helpers. YouTube, Google SERP, and Bing SERP remain disabled and without adapters. The existing API paths and authentication remain in place. The dashboard already had a single classic-script API client, so D1 extended offline browser coverage rather than changing the user workflow or asset architecture.

No API keys or credentials were added. No provider, Cloud Firestore, emulator, paid service, deployment, or production environment was contacted or changed. The future one-click Fetch endpoint and online orchestration remain design-only.

## Git baseline and commits

The work started from the clean `main` baseline recorded in [the D0 report](BAO_CAO_DEMO_D0.md). Local HEAD and fetched `origin/main` matched at `e4764a89865c24600b2ca6ede47f05b694d7a900`. The required prior commits were present and ancestral. No user-authored changes were present at the start.

Local commits created for this work:

| Commit | Purpose |
|---|---|
| `4a2e1a858883ef1d2771008251c12277c5a3882e` | D0 source audit, capability matrix, and contracts/query-plan proposals |
| `ec3a5cdb6f72d01ca60da5d30499254cda9d92a0` | D1 typed settings, source catalog, HTTP policy, query plan, and tests |
| `9ab5c7b6713334c1fc51ad63f9451b30e817d154` | Scoped Ruff formatting for changed Python sections in existing modules |

No history was rewritten. No push was performed.

## Files changed

The D1 implementation commit changed 19 files:

- **Settings and composition:** `Backend/app/settings.py`, `Backend/app/composition.py`, `Backend/app/main.py`, `Backend/app/storage/firestore.py`, `Backend/app/storage/vercel_identity.py`.
- **Sources and plan:** `Backend/app/sources/catalog.py`, `Backend/app/sources/http_policy.py`, `Backend/app/sources/query_plan.py`, `Backend/app/sources/wikimedia/adapter.py`.
- **Development tooling:** `Backend/pyproject.toml`, `Backend/requirements-dev.txt`.
- **Tests:** `Backend/tests/browser_workspace_offline.py`, `Backend/tests/test_http_policy.py`, `Backend/tests/test_query_plan.py`, `Backend/tests/test_settings.py`, `Backend/tests/test_source_catalog.py`, `Backend/tests/test_source_registry.py`, `Backend/tests/test_wikimedia.py`, `Backend/tests/test_workspace_ui.py`.

The scoped-formatting follow-up only changed edited sections in `composition.py`, `main.py`, `sources/wikimedia/adapter.py`, `browser_workspace_offline.py`, and `test_wikimedia.py`. The dashboard source files and algorithm modules were not changed.

## Contracts preserved and changed

| Boundary | Result |
|---|---|
| Existing environment names | Preserved for app title, Wikimedia User-Agent, read-only mode, operator token, read password, Google project/WIF audience, run lock path, and cooldown. Parsing is centralized in `Settings.from_environment`. |
| Settings | Added typed profiles, run mode, source enablement, immutable endpoint/provider/credential-reference maps, market/language defaults, query-plan identity/budget, HTTP policy, and allowed operation metadata. Invalid values and attempts to enable pending providers fail closed. Sensitive fields are excluded from `repr`. |
| Source resolution | A versioned catalog now owns the four source descriptors and fixed endpoints. Registry creation remains lazy and registers only enabled implemented sources. No caller-provided URL can redirect the Wikimedia adapter. |
| HTTP | Added explicit connect/read/write/pool timeouts, an overall deadline, bounded retries and wait, exponential backoff, Retry-After seconds/date parsing, retryable status classification, and cancellation behavior. The existing Wikimedia retry helper remains as a compatibility wrapper. |
| Query plan | Added immutable `demo-us-en` v1 with taxonomy provenance, 14 canonical queries, US/en-US defaults, one-page-per-query cap, rolling-seven-day rule when a provider supports it, deduplication version, and SHA-256 fingerprint. This is curated demo configuration, not a representative market sample. |
| API and auth | `/api/config` still returns only `read_only`; local `POST /api/runs` still requires the existing Bearer operator token; read-only Basic auth and Vercel POST 405 remain. No new Fetch API or credentialed proxy was added. |
| Frontend | Existing `api()` remains the single request boundary and preserves the classic-script/Vercel three-asset packaging. Tests now cover custom headers, JSON Accept, unauthorized responses, invalid JSON, and timeout handling. |
| Analysis/storage | Algorithm IDs, formulas, Golden Expected data, stored schemas, run routes, and Firestore ownership remain unchanged. Firestore and Vercel identity only read project/audience settings through the centralized boundary. |

The data contract and future HTTP API contract are separate proposals in [DEMO_ONLINE_CONTRACTS_AND_QUERY_PLAN.md](../Design/DEMO_ONLINE_CONTRACTS_AND_QUERY_PLAN.md). D1 did not persist the future multi-source contract or implement its orchestration.

## Provider readiness and approval status

| Source | D1 status | D1 behavior |
|---|---|---|
| Wikimedia AQS | Implemented; fixture/offline regression verified | Enabled by default. No live request was made. `views_ceil` remains approximate pageviews and top-list membership is censored. |
| YouTube Data API v3 | Pending approval and not configured | Disabled by default; enabling it through settings is rejected; no adapter exists. |
| Google Search via SerpApi candidate | Pending provider/terms/cost approval | Disabled by default; no adapter or key exists. SerpApi remains a candidate only. |
| Bing Search via SerpApi candidate | Pending provider/terms/cost approval | Disabled by default; no adapter or key exists. SerpApi remains a candidate only. |

The official provider findings, terms/quota questions, and source-specific data limitations are in [the versioned source capability matrix](../Design/DEMO_ONLINE_SOURCE_CAPABILITIES.md), checked on 2026-11-11. Documentation review does not establish account access, live quota, API availability, or redistribution rights.

## Verification

### Offline readiness and regression

Command:

```bash
bash Backend/scripts/verify_offline_readiness.sh
```

**Actual result:** exit code 0; `OFFLINE_READINESS_VERIFICATION=PASS`.

| Gate | Actual result |
|---|---|
| Strict network isolation | IPv4/IPv6 access blocked by the runner; AF_UNIX retained for browser IPC. |
| Backend unittest discovery | 393 tests ran, all passed. |
| Golden Regression, `PYTHONHASHSEED=1` | 7/7 passed. |
| Golden Regression, `PYTHONHASHSEED=983` | 7/7 passed. |
| Dependency consistency | `pip check`: no broken requirements. |
| Shell and JS syntax | `bash -n` for setup/start/readiness scripts and `node --check Frontend/public/app.js` passed. |
| Vercel assets | Generated bundle matched `index.html`, `app.js`, and `styles.css` byte-for-byte; temporary generated asset was removed. |
| Browser workspace | 400/768/1280 requested widths, read-only mode, and config-error mode passed. Headless Chrome reports a 500px actual viewport for the 400px request. |
| Keyboard/mobile | 320/400px mobile viewport and Tab/Enter/Escape checks passed. |
| Offline end-to-end | Synthetic source → ASGI API → Chrome dashboard passed. |
| Synthetic workload | 1k, 5k, and 10k entity runs completed; these are local fake workloads, not provider or Firestore evidence. |

The test count was also captured with the strict unittest discovery from `Backend/`: 393 tests, all passed. A separate count-only invocation from the repository root initially failed import discovery due to the wrong working directory; the corrected invocation and the readiness runner passed. During implementation, an earlier regression run exposed a timeout classification issue for a synthetic transport timeout. The shared policy was corrected to distinguish its own deadline from a transport-raised timeout, and the final full suite passed.

### Scoped Ruff checks

Ruff 0.17.3 is pinned as a development-only tool in `Backend/requirements-dev.txt`; runtime requirements were not changed. The version is listed in the [official Ruff releases](https://github.com/astral-sh/ruff/releases/tag/0.17.3), and Ruff's [installation documentation](https://docs.astral.sh/ruff/installation/) supports installing it with pip. Both sources were checked on 2026-11-11.

Commands run from `Backend/`:

```bash
.venv/bin/ruff check app/composition.py app/main.py app/settings.py \
  app/sources/catalog.py app/sources/http_policy.py app/sources/query_plan.py \
  app/sources/wikimedia/adapter.py app/storage/firestore.py \
  app/storage/vercel_identity.py tests/browser_workspace_offline.py \
  tests/test_http_policy.py tests/test_query_plan.py tests/test_settings.py \
  tests/test_source_catalog.py tests/test_source_registry.py \
  tests/test_wikimedia.py tests/test_workspace_ui.py

.venv/bin/ruff format --check app/settings.py app/sources/catalog.py \
  app/sources/http_policy.py app/sources/query_plan.py tests/test_http_policy.py \
  tests/test_query_plan.py tests/test_settings.py tests/test_source_catalog.py \
  tests/test_workspace_ui.py
```

Both passed. Ruff range formatting was also applied to the changed sections in ten existing Python modules; five required formatting changes. Full-file formatting is not claimed for legacy modules, to avoid unrelated whole-file churn. The existing compact browser harness retains narrow per-file E701/E702 lint ignores; its new request/error assertions were exercised by the offline Chrome runner.

### Security review

- A staged-diff scan for common API-key, bearer-token, private-key, and GitHub-token patterns returned no matches.
- Test credentials are synthetic placeholders and are not real keys. No credential value is checked into application config, logs, reports, or frontend source.
- Secret-bearing `Settings` fields are hidden from `repr`; `/api/config` tests verify that token, password, and project ID do not appear in the response.
- No source, credential, network client, or cloud client is initialized by importing configuration. Adapters are created lazily by the source registry.
- No provider endpoint was called; no Cloud Firestore, Firestore Emulator, Vercel, billing, or production service was called or changed.

## Acceptance matrix

| D1 criterion | Verdict | Evidence |
|---|---|---|
| One typed, validated, test-injectable settings boundary | PASS | `Settings.from_environment`, immutable maps, invalid-input tests, and environment compatibility tests |
| Centralized source endpoints/capabilities | PASS | Versioned four-source catalog; Wikimedia endpoint resolved from catalog |
| Wikimedia behavior and registry preserved | PASS | Existing fixture/API/Golden checks plus registry and adapter configuration tests |
| Future sources disabled and unimplemented | PASS | Default-off catalog entries; invalid enablement rejected; no placeholder adapters |
| Shared bounded HTTP policy | PASS | Timeout, Retry-After, retry cap, cancellation, and transport-timeout tests |
| Frontend API boundary and Vercel packaging preserved | PASS | Existing one-fetch helper, added browser response/error coverage, bundle parity and Chrome checks |
| Algorithm outputs and Golden Expected unchanged | PASS | Golden 7/7 under both seeds; no Golden Expected file changed |
| Scoped formatter/linter | PASS | Ruff lint over all touched Python files, formatter on new/reworked modules, and range formatting on changed sections |
| No credential/cloud/provider calls | PASS | Secret scan and strict offline readiness run; no external data service was contacted |
| No D2+ work | PASS | No one-click Fetch endpoint, live adapters, source orchestration, redesign, billing, or deployment added |

## Open approvals, limitations, and deferred work

- **Provider and rights approval:** YouTube policy/use case, derived metric permissions, audit requirements and retention; Google/Bing SERP provider choice; SERP terms, snippet/URL storage and display rights; account and API access.
- **Budget approval:** Paid SERP plan, per-source/query caps, and operational cost owner. The 14-query plan is a bounded proposal, not an approved market sample or provider quota guarantee.
- **Product approval:** Query seeds, taxonomy scope, market meaning, language, and time-window semantics. Provider locale or language does not prove US authorship or audience residence.
- **Security/privacy approval:** Future demo session/allowlist identity, CSRF/session model, retention/deletion, and privacy review before enabling an authenticated paid Fetch path.
- **Cloud gate:** Firestore Emulator integration and production transaction behavior remain unverified and outside this task. No transaction coordinator was wired into production.
- **Live evidence:** No provider permissions, quota, freshness, response schema, account state, or live source behavior was verified. Only Wikimedia's offline/fixture behavior is regression-verified.
- **Formatting scope:** Legacy files retain unrelated full-file style differences; Ruff format was applied to new/reworked files and changed ranges, and Ruff lint passed with the documented compact-harness exceptions.

## Rollback

The D1 implementation can be reverted locally by reverting the D1 code commit `ec3a5cdb6f72d01ca60da5d30499254cda9d92a0` and the formatting follow-up `9ab5c7b6713334c1fc51ad63f9451b30e817d154`, while retaining the independent D0 audit/design commit. No external state, schema migration, or provider data exists to roll back. Do not revert unrelated owner changes if any are added later.

## Handoff

D0 and D1 reports and supporting proposals are local review artifacts. Owner review is required for provider, rights, budget, market, and demo identity decisions before any D2 implementation. D2 must not begin until the owner reviews these reports and explicitly approves the next scope.
