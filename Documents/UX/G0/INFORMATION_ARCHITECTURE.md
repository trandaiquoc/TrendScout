# Proposed information architecture

**State:** G0 proposal for later approval. Does not change current routes.

## Primary navigation

1. **Dashboard** — two tabs: Overview and Analysis.
2. **Collections** — topic hierarchy, Unclassified review and full keyword ledger/search.
3. **Sources** — active/usable sources and locked research library.
4. **Runs** — chronological Fetch and processing history.
5. **Product Research** — future workflow, clearly labeled not connected.
6. **Settings** — locale, theme, actual runtime/model and system configuration state.

Advanced navigation contains **Algorithms**, **Data quality**, and operational detail. **Schedule** and **AI usage** should be hidden or labeled unavailable until backend contracts supply real values. Remove duplicate Keywords navigation after moving the explorer into Collections; retain a redirect for old deep links.

```text
TrendScout
├── Dashboard
│   ├── Overview
│   └── Analysis
├── Collections
│   ├── Collection tree / topic detail
│   ├── Unclassified (full-ledger search and triage)
│   └── Keyword detail / evidence
├── Sources
│   ├── In use
│   └── Source library (81 researched candidates, locked)
├── Runs
│   └── Run detail
├── Product Research (proposal / future)
├── Settings
└── Advanced
    ├── Algorithms
    ├── Data quality
    └── Operations (schedule/usage only when implemented)
```

## User journeys

### Find and review an emerging signal
Dashboard → Overview summary → Analysis evidence with scope/window/sufficiency → keyword detail → source observations and method outputs → save or assign to Collection. “Emerging” is available only with compatible history and support; otherwise say popularity or insufficient history.

### Organize a keyword
Collections → search whole ledger → filter Unclassified, Suggested, Ambiguous/Unknown, Classified, User decision or Excluded → select rows → review evidence → assign, accept/reject suggestion, exclude/restore or request reclassification. Toolbar states page vs all-filtered selection.

### Review a Collection
Collections → hierarchy/category filter → Collection detail → members with source, observation, assignment origin and evidence → add/copy/move/remove/merge. Delete confirmation explains keyword evidence is retained and suppression prevents automatic recreation.

### Check source coverage
Sources → In use → source detail → connection/configuration, live verification, metric/unit, geography/language filter, latest observation/fetch, cache provenance and quota state. Unknown quota is labeled “Quota not reported”; no made-up bar. Source library filters by category/integration state and explains blockers/prerequisites.

### Diagnose a run
Runs → chronological list → status, times, source coverage, counts and partial/error reasons → detail → observations, classification and algorithms. Manual ID entry remains an advanced “Open run by ID” action.

### Begin product research (future)
Signal → hypothesis → evidence collection → niche/competition review → trademark/IP check → commercial relevance assessment → human decision → saved research record. No conversion from rank/pageviews to sales or purchase intent.

## Ownership and contracts

| User concept | Proposed owner | Existing evidence |
| --- | --- | --- |
| Runtime/source readiness | Backend capability registry | `GET /api/v1/capabilities` |
| Durable keyword search | Collections store/API | `GET /api/v1/datasets/latest/keywords`; current explorer is in Overview |
| Classification/evidence | Collection service/API | `/api/v1/collections/inbox`, `/classifications/{keyword_id}` |
| Algorithm definitions/results | Backend registry/ledger | `/api/v1/algorithms`, `/api/v1/algorithms/results` |
| Run detail | Run service | `/api/runs/{id}` and children; no list endpoint found in reviewed routers |
| Schedule and metered AI usage | No verified current owner/contract | Keep unavailable pending a later implementation |
| Product opportunity evidence | No verified provider/contract | Proposal only |

## Migration contract to specify in G1

- `#keywords` and `#results` redirect to Collections’ keyword workspace; preserve query only if semantics remain valid.
- Keep Overview as default. Persist filters only after storage/privacy behavior is decided.
- Route redirects must not issue Fetch or provider requests.
