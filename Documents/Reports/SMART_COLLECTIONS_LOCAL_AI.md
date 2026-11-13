# Smart Collections and Local AI — Acceptance Report

**Status:** Implemented and locally verified
**Verification date:** 2026-11-13

## Model and installation

- The default inference path is CPU-only FastEmbed `0.9.1`, pinned in `Backend/pyproject.toml`, using `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`.
- The model card documents 50 languages, 384 dimensional sentence embeddings, and Apache-2.0 licensing. FastEmbed's supported-model page lists this model and its 0.220 GB model size. [Model card](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2) · [FastEmbed supported models](https://qdrant.github.io/fastembed/examples/Supported_Models/)
- Dependencies were installed into `Backend/.venv`; `pip check` passed. The model was downloaded from FastEmbed's published model mapping into `~/.cache/trendscout/fastembed` (241 MB on disk), outside Git. FastEmbed initializes/downloads the selected model when `TextEmbedding` is first used. [FastEmbed getting started](https://qdrant.github.io/fastembed/Getting%20Started/)
- `Backend/scripts/check_local_collection_model.py` performed actual local inference and asserted the expected vector dimension and representative semantic pairs. No cloud AI key, paid inference service, Ollama installation, or model call from the browser is needed.
- Cold model initialization and inference took about 16.4 seconds, with peak RSS about 683 MiB. A warm 11-text evaluation took 0.97 seconds with peak RSS about 658 MiB. The host and model runtime affect these measurements; CPU inference has a material memory cost.

## Data model and persistence

Collections use a dedicated SQLite database at `~/.local/state/trendscout/collections-v1/collections.sqlite`, with owner-only file permissions (0600). The existing Wikimedia LIVE state remains under `~/.local/state/trendscout/normal-wikimedia-live-v1/`; the historical D6 quota ledger is not used or migrated.

The schema stores Collections, source-scoped keyword evidence, many-to-many memberships, user feedback, embedding cache entries, proposals, suppression markers, settings, and fetch-job results. Collection deletion removes memberships while preserving keyword/source evidence and records a name suppression marker. SQLite transactions and restart tests cover persistence. The Collections DB is independent of the D6 ledger.

Keyword records preserve original titles, source, Wikimedia project, observation date, data mode, collection/fetch time, source scope, rank, views, and normalization/filter provenance. Collection-specific multilingual technical-page filtering is versioned and does not change the existing Wikimedia cleaning contract.

## Assignment and feedback behavior

- The candidate representation includes the topic title and available Wikimedia project, country-scope, and access context. Collection representations include the name, description, examples, and approved manually added members. Exclusion examples are checked separately.
- The current fixture-reviewed assignment threshold is cosine `0.46`, with a `0.06` winner margin. Proposal groups require at least three unassigned records and every pair in the group must meet the `0.82` cosine cutoff. At most two AI Collections can be created per analysis cycle.
- These cosine values are raw similarities, not probabilities. They are conservative thresholds selected against a small reviewable bilingual fixture set; the fixture does not establish general accuracy.
- Manual additions pin membership, removals create persistent negative feedback, and a removed keyword is not re-added by automatic matching. Collection edits version the semantic representation. Embeddings are cached by model version, Collection version, and text fingerprint.
- AI-generated names and descriptions are deterministic and bound to supporting titles. Suggestions remain reviewable in SUGGEST mode. AUTO_CREATE is user-selectable, requires multiple supporting titles, respects duplicate/suppression checks, and is capped per cycle.

## Model evaluation sample

The local inference script reports the following raw cosine examples:

| Pair | Cosine | Observed behavior |
| --- | ---: | --- |
| Vietnamese Collection → Vietnamese topic | 0.4841 | Above assignment cutoff |
| English Collection → Vietnamese topic | 0.5012 | Above assignment cutoff |
| Vietnamese Collection → English topic | 0.6508 | Above assignment cutoff |
| English Collection → English topic | 0.6748 | Above assignment cutoff |
| Unrelated Mars topic | 0.1228 | Below assignment cutoff |
| Unrelated architecture topic | 0.1808 | Below assignment cutoff |
| Unrelated cooking topic | 0.1710 | Below assignment cutoff |
| 2026 US elections proposal positive | 0.9262 | Above proposal cutoff |
| Unrelated people proposal negative | 0.7535 | Below proposal cutoff |
| Technical search page proposal negative | 0.5165 | Below proposal cutoff |

The sample is intentionally small and hand-curated. Semantic similarity can still be wrong; uncertain or tied matches remain unassigned, and proposed Collections require human review unless the user enables AUTO_CREATE.

## Wikimedia coverage

The normal LIVE Wikimedia route and `bash start.sh` launcher remain in place. During integration verification, the existing fresh Wikimedia response cache was used; no additional Wikimedia request was made for reprocessing.

The verified cached source run reported 1,000 provider-accepted rows and 212 rows retained in the source checkpoint. Discovery extracted 717 candidates and displayed 100, so the UI display cap does not bound Collection processing. The separate Collection ledger held 214 persisted keyword identities at validation time; the organizer considered all 214, processed 156 eligible records, and skipped 58 technical or invalid records. The counts come from different persisted stages and are reported separately.

With only test Collections present, the final organize pass produced zero assignments and one evidence-linked `MediaWiki` suggestion from three related Wikimedia platform titles. In a temporary acceptance check, a matching election Collection received five relevant 2026/2028 election records with raw similarities from 0.618 to 0.704. That test Collection was deleted afterward; keyword/evidence rows remained intact. Three obsolete proposals from earlier threshold/filter iterations were rejected, leaving the current `MediaWiki` suggestion for review. This was a cached local replay, not a claim of a new live provider fetch.

## UI verification

The Collections view adds dynamic user-created topic cards, search, edit/delete, automatic-match toggles, SUGGEST/AUTO_CREATE selection, an organize action, a proposal review list, and a paginated unassigned inbox. Collection keyword rows show membership origin, raw cosine explanation, source, project, observation date, and available view count. Existing Discover navigation and presentation remain intact.

Chromium checks on the local LIVE launcher verified the Collection view at 390 px and 1440 px widths with no horizontal overflow, Vietnamese and English labels, and light/dark theme toggles. Collection API requests returned successfully. The existing page requested `/favicon.ico`, which returned 404; this did not affect the dashboard or Collection interactions.

## Verification and limitations

- Full backend regression suite: **705 tests passed, 2 skipped**.
- Collections, loopback guard, Wikimedia LIVE, cleaning, dashboard, and other previously failing contract tests passed in the full suite.
- Local embedding evaluation: passed; 384 dimensions and bilingual/negative examples asserted.
- `pip check`, Ruff for changed Python files, `node --check Frontend/public/app.js`, and `git diff --check`: passed.
- Dedicated Collections tests cover CRUD and restart persistence, all-record processing independent of the 100-row display cap, cache reuse, bilingual matching, ambiguous matches, multiple membership, manual overrides, model-unavailable behavior, proposals/auto-create, suppression, localized technical-page filtering, and pairwise proposal cohesion.

Known limits: Wikimedia's retained source snapshot is bounded upstream; Collection processing covers all records available in its local ledger, not the provider's full population. Pageviews and a US country parameter do not prove authorship or demand by US residents. The embedding model is multilingual but not TrendScout-fine-tuned; thresholds need broader human-reviewed evaluation before any accuracy claim. Local CPU inference can use roughly 0.65–0.68 GiB RSS, and proposal grouping remains heuristic.
