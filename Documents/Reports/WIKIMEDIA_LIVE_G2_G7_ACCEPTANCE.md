# TrendScout Windows G2–G7 Final Acceptance

**Reviewed:** 2026-11-15 (Asia/Bangkok)
**Windows Local Product Acceptance:** PASS
**Overall Master Goal:** open external gates remain; not 100%.
**Deployment:** none. No push performed.

## Runtime and data integrity

- Started the current workspace with `D:\TrendScout\start.ps1 -Port 8123`. After a verified restart, listener PID `744` started at 15:58:31 on 2026-11-15. `/api/health` returned `ok`; runtime reports local `LIVE`, one configured source, SQLite integrity `OK`, and 1,219 keyword records.
- Database paths: `%LOCALAPPDATA%\TrendScout\collections-v1\collections.sqlite` and `%USERPROFILE%\.local\state\trendscout\normal-wikimedia-live-v1\state.sqlite`.
- Post-acceptance SQLite online backups: `%LOCALAPPDATA%\TrendScout\acceptance-backups\20261115T085848Z-final`. Both database copies passed `PRAGMA integrity_check`.

## Wikimedia LIVE evidence

- The one authorized LIVE fetch is run `e98b004b05fc1242cf6d84b2b8fba539`, fetched at `2026-11-15T07:39:32Z` from the official Wikimedia Analytics API, using the US `all-access` daily top-per-country endpoint. It returned 1,999 records across Nov 12 (1,000) and Nov 13 (999). Source accounting: 1,999 accepted, 0 source rejects, 0 explicitly omitted.
- Normalization retained 1,840 observations and marked 159 cleaning rejects. Stable ledger identity deduplication produced 1,219 keyword records. Processing is `COMPLETED`; the dataset remains `PARTIAL` to expose cleaning rejects. This final acceptance used cached evidence and made no additional Wikimedia request.
- The source is a censored top list, not all pages or search queries. `views_ceil` is approximate; absence is not zero, and US query scope does not establish reader nationality, search interest, or commercial demand.
- Official Wikimedia docs reviewed 2026-11-15: [Page view API reference](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html), [country data privacy rules](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/concepts/country-data.html), [availability, missing values, and throttling](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/troubleshooting.html), and [access policy](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/access-policy.html). They describe country privacy thresholds, delayed publication, omitted zero values, the required identifying User-Agent, and rate limits that depend on access level. No fixed quota was assumed.

## G2–G7 results

- **G2 Collections and classification:** 1,216 topic-eligible records: 265 `CLASSIFIED`, 13 `AMBIGUOUS`, and 938 `UNKNOWN`. There are 269 assigned and 947 unassigned. All classified topics have a Collection membership (0 membership gaps). `Unclassified` is 951 (`AMBIGUOUS` + `UNKNOWN`); it differs from unassigned because the metrics describe classification and membership separately. Populated taxonomy parents show descendant counts.
- The eligibility bug is fixed from saved evidence. `MediaWiki` and `Home` are verified Wikimedia project home pages; `Istimewa:Pencarian` is a non-topic namespace page. All three now have `eligible=0` and appear in the separate `non_topic` state, not user exclusions or unassigned keywords. Source observations and existing user decisions/memberships are preserved.
- Exact-record regression covers all six requested records: `YouTube` → Video Platforms; `Katie Miller` → Political Advisers; the three non-topic/system pages above; `Thank You/en` remains `UNKNOWN` without sufficient evidence. Tests also cover missing evidence, wrong-project evidence, disambiguation, and title-only false matches.
- **G3 Source Center:** Wikimedia is the only configured LIVE source. Google, Bing, and YouTube are disabled/not configured; provider approval and production readiness remain pending.
- **G4 Dashboard and quality:** values come from saved LIVE SQLite evidence. The displayed top rows identify approximate pageview ranking. Cleaning rejects, dates, and censored top-list coverage remain visible.
- **G5 Run History:** 14 persisted runs, `history_truncated=false`. The latest run has processing status `COMPLETED` and dataset status `PARTIAL`; saved history and research survived backend restart.
- **G6 Algorithms:** observed volume and percentile ranks each show 1,206 `COMPLETED` and 10 `SKIPPED` over 1,216 topic-eligible rows. Methods needing longer compatible history remain skipped with applicability reasons. Effectiveness is `NOT_YET_EVALUATED` because no labeled evaluation history exists.
- **G7 Research and Settings:** Native Chrome edited and reopened a LIVE-linked research record; source, project, observation date, edit, and archived status persisted across restart. FastEmbed is local; LLM is `NOT_CONFIGURED`; scheduler remains disabled with `LOCAL_SYNTHETIC_ONLY` policy.

## Windows verification and open gates

- Native Chrome 157.0.8103.51 at 1365×960 loaded Dashboard, Collections, Source Center, Run History, Algorithms, Data Quality, Research, and Settings without horizontal overflow or browser errors. The `non_topic` filter returned exactly three rows. VI/EN and Light/Dark toggles worked.
- Final offline regression: **243 passed**. Focused keyword-workbench suite: **22 passed**. Changed-file Ruff and Python compilation, frontend `node --check`, and `git diff --check` passed.
- Screen reader: `NOT_VERIFIED` (NVDA not installed). Firestore integration: `BLOCKED_ENVIRONMENT` (not attempted on Windows; no Linux/WSL setup). Algorithm effectiveness: `NOT_YET_EVALUATED`. Production integrations remain unapproved. These external gates do not change the Windows local PASS.
