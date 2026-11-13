# Navigation and Routing Contract

## Sidebar destinations

| Group | Label | Hash | Behavior |
| --- | --- | --- | --- |
| Primary | Overview | `#overview` | Overview dashboard tab and current keyword explorer |
| Primary | Collections | `#collections` | Existing Collections experience |
| Primary | Sources | `#sources` | Runtime capability cards |
| Primary | Product research | `#research` | Existing research view |
| Primary | Settings | `#settings` | Existing settings view |
| Advanced | Runs | `#runs` | Manual Run ID lookup and current run detail; no run list is invented |
| Advanced | Algorithm comparison | `#algorithms` | Existing algorithm registry and results |
| Advanced | Data quality | `#quality` | Existing quality detail |

The duplicate Keywords navigation item, disabled Refresh schedule, and unmeasured AI usage destination are removed. Their dormant business logic or API contracts were not removed.

## Dashboard tabs

Overview and Analysis use semantic button tabs under the Overview destination. `#overview` selects Overview; `#analysis` selects Analysis. The active sidebar destination remains Overview for both tabs.

The tab list supports Left/Right arrows and Home/End. Enter and Space use native button activation. `aria-selected`, `tabindex`, `aria-controls`, and the associated tab panels stay synchronized. Browser back/forward restores the tab through the hash route.

## Legacy and invalid routes

- `#keywords` and `#results` are compatibility aliases for Overview. The legacy hash remains addressable, the existing explorer remains available in Overview, and the query string is preserved.
- Unsupported old hashes, including `#refresh` and `#usage`, fall back to `#overview` while retaining the query string.
- Unknown non-empty hashes also fall back to Overview. No view is created solely as an empty route.
- The explorer's `q` query parameter initializes local search. Search updates `q` while preserving other query parameters and the current hash.

## Network behavior

Changing Dashboard tabs and sidebar routes does not start Fetch or contact a provider. Existing local application reads still occur for their supported destinations, such as Collections or the algorithm registry. Browser observation against the local runtime recorded no `/api/v1/fetches` requests and no non-local requests.

The keyword explorer remains in Overview until G2 moves the full ledger workflow to Collections. This contract does not change a backend API or data schema.
