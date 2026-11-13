# G2 contextual classification and ledger replay

**Status:** Implemented and replayed locally on Windows, 2026-11-14 (Asia/Bangkok)
**Classification policy:** `contextual-classifier-v1.2`
**Taxonomy:** `topics-v1.4`
**Reconciliation ID:** `reconcile-8116096f-a5db-4450-b23c-04bd07251f8c`

## Safety and baseline

The configured Collections store was confirmed at `%LOCALAPPDATA%\TrendScout\collections-v1\collections.sqlite`. Before any database change, a consistent SQLite API backup was written outside the repository to:

`%LOCALAPPDATA%\TrendScout\backups\overnight-g2-g7\collections-20261114-020943.sqlite`

SHA-256: `4662079f510ff754a81032a747dc260f2e846fac08177e8c8685f8219e1da0d7`

The backup passed `integrity_check=ok` and `foreign_key_check=[]`. The private local baseline manifest at `%LOCALAPPDATA%\TrendScout\backups\overnight-g2-g7\baseline-manifest.json` records the exact prior memberships, pin/origin state, feedback, exclusions, context and classification versions, embedding cache coverage, and row counts. The backup and manifest are intentionally outside Git.

| Table / property | Before replay |
|---|---:|
| Keywords | 154 |
| Collections | 39 |
| Memberships | 180 |
| Manual or pinned memberships | 0 |
| `REMOVE` feedback | 0 |
| Excluded keywords / exclusion decisions | 0 / 0 |
| Entity contexts | 150 |
| Classification results | 154 |
| Embedding cache vectors | 517 (`fastembed-0.9.1`; vector rows are not keyword rows) |
| Algorithm observations | 154 |
| Algorithm result rows | 1,848 |

All existing memberships were automatic, unpinned memberships. The exact prior rows remain in the verified backup manifest.

## Implementation

- Added a versioned local classification policy that the classifier consumes. This remains deterministic local rules plus FastEmbed evidence matching; it is not an LLM prompt or a paid AI integration.
- Added evidence-backed descriptions for works, ships, people and occupations, settlements, roads, monuments, plants/biology, digital services, nonprofits, and crime/justice. Stable taxonomy IDs are preserved; the taxonomy gained only evidenced leaves for settlements, monuments and memorials, crime/justice, and Internet services. The existing Biology label now describes its stable `science_nature.biology` ID more clearly.
- Preserved original keyword text. Entity resolution now normalizes lookup titles to Unicode NFC, stores canonical redirect, language, namespace and disambiguation metadata, uses official project hosts from a fixed allowlist, and uses bounded page extracts when no page description is available. Unlisted project strings do not form request URLs.
- Non-main-namespace pages are recorded as `INELIGIBLE` for topic assignment without deleting their observations or treating them as user-excluded. Disambiguation pages remain reviewable. Missing descriptions remain `UNKNOWN`.
- The Collections summary and VI/EN workbench copy now count “not a topic” separately from “not enough information” and from user exclusions.

The resolver performed five bounded metadata lookups against official Wikimedia project endpoints for uncached or outdated context. It did not call Wikimedia Pageviews, Google, Bing, YouTube, or a paid model. [MediaWiki API:Pageprops](https://www.mediawiki.org/wiki/API%3APageprops) and [API:Info](https://www.mediawiki.org/wiki/API%3AInfo) document page properties and page information used for namespace and redirect metadata (reviewed 2026-11-14 Asia/Bangkok).

## Required 25-case review

The permanent offline fixture covers every owner-supplied title with a conservative no-context result and saved-context regression. The replay produced the following expected placements where the persisted entity evidence supports them:

| Example | Result |
|---|---|
| Dancing with the Stars season 35 | Entertainment → Television |
| SS United States | Places → Ships |
| Children's Games (Bruegel) | Creative Works → Artwork |
| Joran van der Sloot | Person entity; Crime & Justice from the saved “murderer” context |
| MediaWiki | Ambiguous: the `mediawiki` project resolves this title to a Wikimedia project home page, not verified wiki software |
| Istimewa:Pencarian | `INELIGIBLE`; official `id.wiktionary` page metadata reports namespace `-1` |
| Wikimedia APIs | Technology → Internet Services from the official page description |
| Tomb of the Unknown Soldier | Places → Monuments & Memorials |
| Home | Ambiguous: its saved Wikimedia page context identifies a project home page, not a topic |
| Neatsville, Kentucky | Places → Settlements |
| Avengers: Doomsday | Entertainment → Movies |
| Usman Nurmagomedov; Ezra Frech; Peter Norman; Sonny Liston; Fernando Mendoza | People → Athletes using saved entity descriptions and/or occupation IDs |
| Archive of Our Own | Non-profit Organizations and Internet Services |
| Sofia Franklyn | People → Content Creators |
| Evolution of spiders | Science & Nature → Biology |
| General Sherman (arbre) | Science & Nature → Plants |
| Thank You/en | `UNKNOWN`; the saved page is main namespace with no QID or description, so its suffix is not treated as proof of a translation page |
| Wikipedia | Media Organizations and Internet Services from the saved “online encyclopedia” description |
| U.S. Route 101 | Places → Roads |
| Maura Higgins | People → Performers from saved occupation/context |
| Marsha M. Linehan | People → Health Professionals from saved psychologist context |

The same fixture verifies every case stays `UNKNOWN` when entity context is absent; verified namespace metadata is the only basis for the special-page result. It does not infer a person’s occupation from their name.

## Dry run and applied result

The read-only dry run examined all 154 eligible persisted records before membership changes. It predicted 148 `CLASSIFIED`, 4 `AMBIGUOUS`, 1 `UNKNOWN`, and 1 `INELIGIBLE` result. The successful reconciliation then processed all 154 records, used saved context only, and reported 148 assigned and 6 without a Collection. It inferred 76 local embedding vectors from the already installed and cached model; no model was downloaded.

| Measure | Before | After |
|---|---:|---:|
| Keywords | 154 | 154 |
| Topic classification | 125 classified, 11 ambiguous, 18 unknown | 148 classified, 4 ambiguous, 1 unknown, 1 ineligible |
| Collections | 39 | 53 |
| Membership rows | 180 | 211 |
| Unique keywords with a Collection | 125 | 148 |
| Manual/pinned memberships | 0 | 0 |
| `REMOVE` feedback | 0 | 0 |
| Excluded keywords | 0 | 0 |
| Algorithm observations | 154 | 154 |
| Recomputed algorithm results | — | 0; 154 keyword records unchanged |

The increase to 53 Collections is taxonomy-backed hierarchy, not one Collection per keyword. The replay created no suggested topics. Existing suppressed/deleted taxonomy names were respected by the store’s category creation contract.

After replay, SQLite `integrity_check=ok`, `foreign_key_check=[]`, and the original keyword/observation counts remained unchanged. Classification results carry policy `contextual-classifier-v1.2` and taxonomy `topics-v1.4`.

## Verification and limits

- Focused classifier, resolver and workbench tests cover the 25 cases, no-context fallbacks, namespace status, untrusted project allowlisting, state counts, and offline persistence.
- Full Backend suite: **756 tests ran; 754 passed and 2 failed**. Both failures are the previously recorded synthetic discovery expectation mismatch (`INSUFFICIENT_EVIDENCE` expected, `UNAVAILABLE` returned). No failure was introduced in the G2 suites.
- The real store uses one Wikimedia Pageviews snapshot dated 2026-11-11. Classification describes entity/topic evidence; it does not validate trend growth, demand, or commercial relevance.
- `MediaWiki` is intentionally not labeled software from the title: its source project context resolves to QID `Q5296`, described as a Wikimedia project main page. The intended software label remains conditional on an unambiguous software entity context.
- `Thank You/en` remains unresolved; existing metadata does not establish that it is a translation page.
