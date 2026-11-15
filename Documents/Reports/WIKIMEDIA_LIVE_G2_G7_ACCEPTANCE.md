# TrendScout Windows G2–G7 Final Acceptance

**Reviewed:** 2026-11-15 (Asia/Bangkok)
**Windows Local Product Acceptance:** PASS
**Overall Master Goal:** open external gates remain; not 100%.
**Deployment:** none. No push performed.

**Git baseline:** `origin/main` `151eed29922c1af947c399067e111744390b5e1c`.

**Implementation commits:** `8aa9f7ecfaff3a62ce567eb54b391f113e476ab9` (Wikimedia G2–G7) and `cd38d2f251f5c4b34836e0ca2464f84c2711608c` (Run History source coverage), both local.

## Runtime and data integrity

- Started the current workspace with `D:\TrendScout\start.ps1 -Port 8123`. After a verified restart, listener PID `744` started at 15:58:31 on 2026-11-15. `/api/health` returned `ok`; runtime reports local `LIVE`, one configured source, SQLite integrity `OK`, and 1,219 keyword records.
- Database paths: `%LOCALAPPDATA%\TrendScout\collections-v1\collections.sqlite` and `%USERPROFILE%\.local\state\trendscout\normal-wikimedia-live-v1\state.sqlite`.
- Post-acceptance SQLite online backups: `%LOCALAPPDATA%\TrendScout\acceptance-backups\20261115T085848Z-final`. Both database copies passed `PRAGMA integrity_check`.

## Wikimedia LIVE evidence

- The one authorized LIVE fetch is run `e98b004b05fc1242cf6d84b2b8fba539`, fetched at `2026-11-15T07:39:32Z` from the official Wikimedia Analytics API, using the US `all-access` daily top-per-country endpoint. It returned 1,999 records across Nov 12 (1,000) and Nov 13 (999). Source accounting: 1,999 accepted, 0 source rejects, 0 explicitly omitted.
- Normalization retained 1,840 observations and marked 159 cleaning rejects. Stable ledger identity deduplication produced 1,219 keyword records. Processing is `COMPLETED`; the dataset remains `PARTIAL` to expose cleaning rejects. This final acceptance used cached evidence and made no additional Wikimedia request.
- Earlier run `f9a3008316b45e16acc69a7a5d5482a9` remains preserved with a `PARTIAL` source manifest: 1,000 provider records, 212 retained, 0 source rejects, and 788 omitted by the former storage limit. It was not backfilled or re-fetched. Run History now displays these source counts separately from downstream cleaning counts.
- Persisted observation dates span Nov 11–13, 2026 (154, 918, and 922 observations in the analytics timeline). The latest explicit history job requested Nov 12–13 only.
- The source is a censored top list, not all pages or search queries. `views_ceil` is approximate; absence is not zero, and US query scope does not establish reader nationality, search interest, or commercial demand.
- Official Wikimedia docs reviewed 2026-11-15: [Page view API reference](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html), [country data privacy rules](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/concepts/country-data.html), [availability, missing values, and throttling](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/troubleshooting.html), and [access policy](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/access-policy.html). They describe country privacy thresholds, delayed publication, omitted zero values, the required identifying User-Agent, and rate limits that depend on access level. No fixed quota was assumed.

## G2–G7 results

- **G2 Collections and classification:** 1,216 topic-eligible records: 265 `CLASSIFIED`, 13 `AMBIGUOUS`, and 938 `UNKNOWN`. There are 269 assigned and 947 unassigned. All classified topics have a Collection membership (0 membership gaps). `Unclassified` is 951 (`AMBIGUOUS` + `UNKNOWN`); it differs from unassigned because the metrics describe classification and membership separately. Populated taxonomy parents show descendant counts.
- The eligibility bug is fixed from saved evidence. `MediaWiki` and `Home` are verified Wikimedia project home pages; `Istimewa:Pencarian` is a non-topic namespace page. All three now have `eligible=0` and appear in the separate `non_topic` state, not user exclusions or unassigned keywords. Source observations and existing user decisions/memberships are preserved.
- Exact-record regression covers all six requested records: `YouTube` → Video Platforms; `Katie Miller` → Political Advisers; the three non-topic/system pages above; `Thank You/en` remains `UNKNOWN` without sufficient evidence. Tests also cover missing evidence, wrong-project evidence, disambiguation, and title-only false matches.
- **G3 Source Center:** Wikimedia is the only configured LIVE source. Google, Bing, and YouTube are disabled/not configured; provider approval and production readiness remain pending.
- **G4 Dashboard and quality:** values come from saved LIVE SQLite evidence. The displayed top rows identify approximate pageview ranking. Cleaning rejects, dates, and censored top-list coverage remain visible.
- **G5 Run History:** 14 persisted runs, `history_truncated=false`. The latest run has processing status `COMPLETED` and dataset status `PARTIAL`; saved history and research survived backend restart. Native Chrome details distinguish provider-returned, retained, source-rejected, and storage-omitted counts for the latest run and preserved pre-fix history.
- **G6 Algorithms:** observed volume and percentile ranks each show 1,206 `COMPLETED` and 10 `SKIPPED` over 1,216 topic-eligible rows. Methods needing longer compatible history remain skipped with applicability reasons. Effectiveness is `NOT_YET_EVALUATED`: the Collections DB has 0 feedback rows and 0 keyword-decision rows; model classifications are not independent ground-truth labels.
- **G7 Research and Settings:** Native Chrome edited and reopened a LIVE-linked research record; source, project, observation date, edit, and archived status persisted across restart. FastEmbed is local; LLM is `NOT_CONFIGURED`; scheduler remains disabled with `LOCAL_SYNTHETIC_ONLY` policy.

## Windows verification and open gates

### Local completion gate matrix

| Gate | Result | Evidence |
|---|---|---|
| WIKI.1 Official LIVE source | PASS | Current runtime is LIVE; one configured Wikimedia source; saved real run above. |
| WIKI.2 No synthetic normal path | PASS | `start.ps1` defaults to Wikimedia LIVE; synthetic demo requires explicit test-fixture opt-in. |
| WIKI.3 No silent source truncation | PASS | Both regular and history handlers disable the 100 KB generic cap for Wikimedia; 1,999 returned, 0 source rejected/omitted. The 200-record admission ledger limit is not a payload cap. |
| WIKI.4 Provenance and timestamps | PASS | Saved LIVE mode, observation dates, fetched timestamp, project, source rank, approximate views, and source scope. |
| WIKI.5 Fetch/cache/retry/limits | PASS | Bounded, idempotent history route; cached/resumed source handling and admission policy covered by tests; real saved Fetch verified. |
| WIKI.6 Safe history | PASS | User-initiated two-day window; source-day cache, durable checkpoint/resume, no fabricated dates or zeros. |
| G2 Collections/classification | PASS | Exact six-record regression; 0 classified-topic membership gaps; non-topic and user exclusion are separate. |
| G3–G7, UI, WIN, DATA | PASS | Source Center, Dashboard, Run History, Algorithms/Data Quality, Research/Settings, Native Chrome, SQLite, and restart evidence above. |
| REG Regression | PASS | Three focused source-coverage tests passed for new, pre-fix, and unavailable manifests; prior 22 focused keyword-workbench and 243-test offline integration runs passed. |

**A. Wikimedia Windows Feature Completion: 100% (PASS).**

**B. Remaining external G2–G7 acceptance:** Firestore `BLOCKED_ENVIRONMENT`; algorithm effectiveness `NOT_YET_EVALUATED` (0 feedback/decision labels); cross-provider overlap `NOT_VERIFIED` (only one configured LIVE source); production integrations `NOT_APPROVED`; screen reader `NOT_VERIFIED` (NVDA not installed).

- Native Chrome 157.0.8103.51 loaded Dashboard, Collections, Source Center, Run History, Algorithms, Data Quality, Research, and Settings. In the real Windows backend, Run History showed latest source counts `1,999 / 1,999 / 0 / 0` and preserved earlier counts `1,000 / 212 / 0 / 788`. The `non_topic` filter returned exactly three rows; VI/EN and Light/Dark toggles worked.
- Prior final offline regression: **243 passed**; focused keyword-workbench suite: **22 passed**. For the Run History fix, **3 focused Node tests passed**, Native Chrome verified both actual records, changed frontend modules passed `node --check`, and `git diff --check` passed.
- Screen reader: `NOT_VERIFIED` (NVDA not installed). Firestore integration: `BLOCKED_ENVIRONMENT` (not attempted on Windows; no Linux/WSL setup). Algorithm effectiveness: `NOT_YET_EVALUATED`. Production integrations remain unapproved. These external gates do not change the Windows local PASS.
