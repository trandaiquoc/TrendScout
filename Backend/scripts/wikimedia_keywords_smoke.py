"""Live fetch -> clean -> classify; read-only and no trend scoring."""

import asyncio
from collections import Counter

from app.adapters.wikimedia import WikimediaAdapter
from app.analysis.keywords import CLASSIFICATION_VERSION, extract_keywords
from app.processing.wikimedia_cleaning import clean_snapshot


async def main():
    collected = await WikimediaAdapter().collect()
    if collected.analysis_date is None:
        raise SystemExit("No analysis date available")
    all_results = []
    for snapshot, rows in collected.days:
        cleaned = clean_snapshot(snapshot, rows)
        results = extract_keywords(cleaned.accepted)
        if len(results) != len(cleaned.accepted):
            raise RuntimeError("Observation/keyword mismatch")
        all_results.extend(results)
        print(f"day={snapshot.observation_date} http={snapshot.http_status} status={snapshot.fetch_status} "
              f"raw={cleaned.report.raw_count} accepted={cleaned.report.accepted_count} keywords={len(results) if cleaned.report.raw_count is not None else None}")
    entities = {x.keyword_id: x for x in all_results}
    by_observation = Counter(category for result in all_results for category in result.categories)
    by_entity = Counter(category for result in entities.values() for category in result.categories)
    print(f"analysis_date={collected.analysis_date} requests={collected.request_count} version={CLASSIFICATION_VERSION} "
          f"observations={len(all_results)} keyword_results={len(all_results)} unique_entities={len(entities)}")
    print(f"observation_categories={dict(by_observation)} entity_categories={dict(by_entity)} "
          f"multilabel_observations={sum(len(x.categories) > 1 for x in all_results)} "
          f"multilabel_entities={sum(len(x.categories) > 1 for x in entities.values())}")
    for category in ("fashion", "home_goods", "other", "unclassified"):
        examples = [x for x in entities.values() if category in x.categories][:8]
        print(f"EXAMPLES {category}: " + repr([(x.project, x.display_keyword, [(e.rule_id, e.matched_text) for e in x.classification_evidence]) for x in examples]))


if __name__ == "__main__":
    asyncio.run(main())
