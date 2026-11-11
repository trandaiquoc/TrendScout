# TrendScout Demo Online — D0 Audit Report

- **Date/time:** 2026-11-11 10:14 ICT
- **Repository:** /mnt/Data/Mine/TrendScout
- **Branch:** main
- **Baseline HEAD:** e4764a89865c24600b2ca6ede47f05b694d7a900
- **D0 verdict:** PASS for audit/design deliverables only; provider/legal/market decisions remain pending.

## Executive summary

The local checkout and fetched origin/main both resolve to the same clean commit. The required offline readiness command was run in this session and passed: 371/371 backend tests; Golden Regression 7/7 under each of PYTHONHASHSEED=1 and 983; dependency, shell/JS, Vercel bundle, browser, and synthetic workload checks all passed.

The repository is a working offline/demo implementation, not a verified multi-source live service. Wikimedia has one implemented adapter and mock-backed tests. YouTube Data API, Google SERP, and Bing SERP adapters do not exist. SerpApi remains an unapproved candidate for Google/Bing. No provider, Firestore Cloud, emulator, Vercel production, AI, or paid service was called or changed in this audit.

Supporting proposals:

- [Source capability matrix](../Design/DEMO_ONLINE_SOURCE_CAPABILITIES.md)
- [Contracts and query plan](../Design/DEMO_ONLINE_CONTRACTS_AND_QUERY_PLAN.md)

## Git and instruction baseline

| Check | Result |
|---|---|
| Repository root | /mnt/Data/Mine/TrendScout, verified by git rev-parse --show-toplevel |
| Branch / working tree | main, clean at audit start; no tracked or untracked user changes |
| Remote | origin fetch/push URLs point to https://github.com/trandaiquoc/TrendScout.git |
| Fetch | git fetch origin main succeeded; fetched commit equals local HEAD and origin/main |
| Full baseline SHA | e4764a89865c24600b2ca6ede47f05b694d7a900, commit time 2026-11-11T09:26:01+07:00, docs: report phase 3 step 1 blocker |
| Prior implementation SHA | cecd1f374bb074c7f3858bb6e3184f59b5039d41, feat(firestore): add emulator transaction adapter |
| Ancestry | Both cecd1f3 and e4764a8 resolve to commits and are ancestors of HEAD |
| History | Latest 12 commits inspected; no local-only or remote-only commit after fetch |
| Instructions | Root AGENTS.md and Frontend/AGENTS.md read; no Backend-specific AGENTS.md exists |
| Knowns | MCP initial was run. This repository has no .knowns/ directory and the Knowns code/docs/task project could not be loaded. Code discovery therefore used targeted repository symbol/path searches after confirming the service was unavailable. |
| Roadmaps | No local D0/D1 online/handoff roadmap dated 2026-11-11 was found. The current user request is the active scope. Existing product docs remain working references, not approved decisions. |

The origin/main remote was fetched without merge, checkout, reset, or history rewrite.

## Local documentation reviewed

- AGENTS.md and Frontend/AGENTS.md
- Documents/REQUIREMENTS_TECH_BA_DEV.md
- Documents/TREND_KEYWORD_DEMO_BUSINESS_SYSTEM_DESIGN.md
- Documents/Reports/BAO_CAO_BUOC_21_GIAI_DOAN_2.md
- Documents/Reports/BAO_CAO_BUOC_01_GIAI_DOAN_3.md
- Documents/Reports/BAO_CAO_BUOC_14_GIAI_DOAN_2.md through BAO_CAO_BUOC_20_GIAI_DOAN_2.md

Important distinctions from those working reports:

- Phase 2 Step 21 is documented as MVP_OFFLINE_ACCEPTED, BLOCKED_FOR_LIVE, and COMMERCIAL_NOT_READY; its historical test count is not evidence for this session.
- Phase 3 Step 01 is IMPLEMENTED_NOT_EMULATOR_VERIFIED; the requested 16 Firestore Emulator cases remain unverified. This D0 did not install or run an emulator and did not wire a transactional coordinator into application composition.
- The older business requirements describe a legacy repository and its collectors. They are not current behavior or authorization to restore those sources.
- Existing docs leave market definition, source rights, user identity, retention, and costs open.

## Actual architecture and capability audit

| Area | Implemented and observed | Verified in this session / limits |
|---|---|---|
| Settings and composition | app/settings.py only reads TREND_SCOUT_APP_NAME. Environment reads are distributed across main.py, composition.py, the Wikimedia adapter, Firestore storage, and Vercel identity code. SourceRegistry lazily creates the Wikimedia adapter. | Existing tests cover composition injection and Vercel read-only behavior. There is not yet one typed application settings object for source flags, provider/endpoint policy, HTTP budget, market defaults, or credential references. |
| Wikimedia source | app/sources/wikimedia/adapter.py calls the fixed AQS top-per-country US/all-access endpoint with httpx. It validates the response and preserves zero, missing, not-in-top, day-unavailable, rejected, and partial states. A legacy import path is an alias to the source adapter. | test_wikimedia.py, integration, cleaning, and Golden tests use fixtures/MockTransport. The adapter has bounded retries and Retry-After handling. This audit did not call the live API. |
| YouTube / Google / Bing | No source modules or live adapters exist. | Pending approval/configuration. No dummy adapter will be added or reported as live-ready. |
| API and auth | FastAPI provides health, read-only config, manual POST /api/runs, and stored run/keyword reads. Local manual run requires the existing Bearer operator token; read-only deployments retain Basic auth and Vercel POST 405. | API, auth, ASGI, and read-only regression tests passed inside the offline runner. D1 must preserve this contract and must not remove auth. |
| Algorithms | Registry, runner, Wikimedia legacy strategy, ratio/baseline strategies, and opt-in statistics already have separate modules and stable IDs/versions. | Algorithm/Gateway and Golden tests passed. No formula, strategy ID, rounding, entity identity, or expected fixture change is needed. |
| Storage/jobs/workers | Storage port, FakeStorage, Firestore CRUD, emulator guard, transactional coordinator, offline workers/scheduler/retention prototypes exist in separate ownership areas. The transactional coordinator is not in production composition. | Offline suite passed. No Cloud Firestore calls or emulator tests were run; fake evidence is not cloud proof. |
| Frontend | Vanilla JS has ten workspace screens. One api() helper owns fetch, timeout and response/error handling; views use that helper. Rendering uses DOM/text APIs. Existing Vercel packaging embeds exactly index.html, app.js, and styles.css. | node --check, bundle byte parity, Chrome workspace, keyboard/mobile, and source→API→browser offline E2E passed. A separate ES-module file would alter the packaging boundary; D1 should keep the client boundary inside the current classic-script entrypoint unless packaging is deliberately updated and tested. |
| Legacy category filtering | Current run API preserves legacy filters fashion, home_goods, other, and unclassified; a richer versioned taxonomy exists but is not a reason to filter source ingestion to two categories. | Existing API and taxonomy tests passed. The proposed query plan spans all taxonomy topic parents plus open discovery; it does not change the legacy UI or scorer. |

## Source capabilities and current provider facts

The full four-source matrix, parameters, pagination, quotas, freshness, metrics, terms, retry states, provenance, and UI labels is in the supporting capability document. The central findings are:

- Wikimedia is a country-scoped, censored pageview top list. Missing/not-in-top is not zero; views_ceil is approximate pageviews, not unique audience.
- YouTube videos.list(chart=mostPopular) changed on 2025-07-21 and no longer represents the former broad Trending Now list. search.list is query-based, uses a separate per-day quota bucket, and must follow page tokens. YouTube policy currently restricts derived metrics, aggregation, and long-term storage without the required audit/approval.
- Google Custom Search JSON API is closed to new customers and existing customers have a transition deadline of 2027-01-01. It is not selected as the default.
- Traditional Microsoft Bing Search APIs retired on 2025-08-11. Foundry Web Search/Grounding is not a substitute structured organic SERP contract.
- SerpApi has structured Google/Bing SERP candidates but is not approved. Its current docs expose API keys in query parameters, paid plans, result retention, and provider-specific error states. Its public status page showed ongoing Google Search issues as of 2026-11-09. No account, key, paid plan, or provider request was made.

Official external documentation was checked on 2026-11-11 and cited in the supporting source matrix. This verifies documentation claims, not credentials, account quota, availability, legal rights, or live API behavior.

## Versioned query-plan proposal

The proposed default is demo-us-en version 1, scoped to the repository's topics-v1.0 taxonomy and a single open-discovery seed. It has 14 canonical query strings across the 13 active topic parents plus open discovery. It uses a one-page cap per query, a rolling seven-day proposal for sources that support time filters, a deterministic SHA-256 fingerprint, and entity-scoped deduplication that retains links to every query that found the entity.

Fashion and Home & Living (legacy home_goods) are priority fixture/regression areas, not inclusion filters, weighting rules, or claims of market representativeness. The seed set is explicitly a curated demo configuration, not a representative sample of US users or all Internet topics. Quota estimates and provider mappings are proposals pending owner and legal approval.

## Data and HTTP contract proposal

DEMO_ONLINE_CONTRACTS_AND_QUERY_PLAN.md specifies separate internal data and browser HTTP contracts for source capability, fetch request, page/batch, canonical observation, metric descriptor, evidence, progress, summary, quota, errors, and outcome states. It keeps readiness (IMPLEMENTED, NOT_CONFIGURED, PENDING_APPROVAL), data mode (LIVE, CACHE, REPLAY, SYNTHETIC), and one mutually exclusive terminal fetch outcome separate.

The future one-click API is proposed as POST /api/v1/fetches plus authenticated progress polling. It is not implemented in D0 or D1. It requires session/allowlist auth, CSRF where cookie-authenticated, rate and concurrency bounds, idempotency, fixed provider hosts, page/query/deadline limits, and a server-side budget. No arbitrary URL, credential, raw query, or Run ID is accepted from the browser. Existing /api/runs auth remains in place.

## Baseline verification run

Command:

    bash Backend/scripts/verify_offline_readiness.sh

Actual result during this session: exit code 0 and final marker OFFLINE_READINESS_VERIFICATION=PASS.

| Gate | Actual result |
|---|---|
| Strict network guard | Python/libc IPv4/IPv6 socket and connect blocked with EPERM; AF_UNIX retained only for local IPC |
| Full backend unittest discovery | 371 tests ran, 371 passed, no failure or skip reported |
| Golden Regression, seed 1 | 7/7 passed |
| Golden Regression, seed 983 | 7/7 passed |
| Dependency consistency | pip check: no broken requirements |
| Shell / JavaScript syntax | bash -n setup.sh start.sh Backend/scripts/verify_offline_readiness.sh and node --check Frontend/public/app.js passed |
| Vercel assets | Generated bundle matched the three source assets byte-for-byte; temporary generated bundle absent after runner |
| Chrome workspace | 500, 768, and 1280 widths; read-only and config-error cases passed |
| Chrome keyboard/mobile | DevTools viewport 320/400 CSS pixels, Tab/Enter/Escape passed |
| Offline pipeline/browser | Synthetic source → ASGI API → dashboard rendered expected keyword and provenance |
| Synthetic workload | 1k/5k/10k entities completed; local fake workload only |

No live source/provider, Cloud Firestore, emulator, AI, marketplace, Vercel deployment, or billing operation was part of this verification. Baseline counts are measured in this session, not copied from previous reports.

## Acceptance matrix

| D0 gate | Verdict | Evidence |
|---|---|---|
| Git baseline and applicable instructions | PASS | Fetch, SHA, ancestry, tree, branch, remote, history, root/frontend instructions verified |
| Baseline rerun | PASS | 371/371, both Golden seeds 7/7, offline readiness runner exit 0 |
| Actual implementation audit | PASS | Existing ownership and missing/fake/unverified states recorded above |
| Four-source capability matrix | PASS | Supporting versioned capability proposal with official references dated 2026-11-11 |
| Versioned default query plan | PASS | demo-us-en v1 proposal and 14 seeds, broad taxonomy scope, deterministic fingerprint, quota cap and provenance |
| D2–D6 data and HTTP contracts | PASS | Supporting contract design specifies states, fields, security, and future API; not implemented here |
| Risk and approval register | PASS | Pending items listed below; no provider/cost approval inferred |

## Security, privacy, rights, and approval register

| Decision / risk | Current status | Required owner action before activation |
|---|---|---|
| YouTube API use case, derived metrics, aggregation, retention, compliance audit | PENDING_APPROVAL | Legal/product/security review, current Google policy review, and required approval; no cross-source score |
| Google/Bing SERP provider and provider terms | PENDING_APPROVAL | Select provider, confirm rights to collect/store/display snippets and URLs, reliability, retention, terms, and account security |
| SerpApi paid plan, cost cap, credential, query-string key redaction | PENDING_APPROVAL | Explicit budget and provider/account approval; no purchase or key creation in D0 |
| Query seeds, taxonomy scope, market meaning, and seven-day window | PROPOSAL | Product/BA owner approval; validate that user decisions are served and maintain disclosure that seeds are not representative |
| US market claims | OPEN | Define whether US means provider locale, source geography, or audience residence for each metric; do not infer user geography from language or region filters |
| Rights, retention, deletion, source attribution, privacy | OPEN | Legal/privacy review of source-specific storage and redistribution rules before persistence/public display |
| Authenticated demo Fetch, CSRF/session, allowlist, concurrency/budget | DESIGN ONLY | Approve identity/session model and operational owner before endpoint implementation |
| Firestore production transaction path | BLOCKED | Emulator/Cloud validation remains a separate gate; not wired or exercised here |
| Host, production deployment, scheduler, workers, AI | NOT IN SCOPE / DISABLED | Must not be enabled as a side effect of D0/D1 |

No API key or credential was included in the documents, source, logs, tests, or generated frontend.

## D1 implementation scope planned from the audit

Only these ownership boundaries require D1 work:

1. Expand the existing Settings entry point to typed, validated, test-injectable settings while preserving environment names consumed by start.sh, Vercel, and the current backend.
2. Centralize source endpoint/capability descriptors. Keep only Wikimedia implemented; future sources default disabled and unconfigured.
3. Reuse Wikimedia's adapter/registry and MockTransport tests. Add a shared bounded HTTP policy only where it is used by the existing adapter; do not add fake live adapters.
4. Keep the browser's single classic-script API client boundary and Vercel's three-asset packaging; preserve old request paths and auth.
5. Leave the existing algorithm registry/strategies in place and prove stable IDs/Golden output; no scoring or classification change.

No source fetch orchestration, new public Fetch route, identity provider, tenant auth, schema migration, algorithm change, or D2 redesign is part of D1.

## Rollback

D0 is documentation-only and can be rolled back by reverting its local documentation commit. Planned D1 changes must remain additive and preserve old API/data contracts; if a targeted change cannot pass the offline and Golden regressions, revert only that change before making any implementation commit. There is no provider call, cloud mutation, or schema migration to undo.

## Files created for D0

- Documents/Reports/BAO_CAO_DEMO_D0.md
- Documents/Design/DEMO_ONLINE_SOURCE_CAPABILITIES.md
- Documents/Design/DEMO_ONLINE_CONTRACTS_AND_QUERY_PLAN.md

## Local commit

This D0 report and its two supporting proposals are intended for the local documentation commit: docs(demo): audit D0 source capabilities and contracts. No push is authorized or performed.
