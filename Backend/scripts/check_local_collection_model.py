"""No-network smoke/evaluation for the already-cached multilingual local model."""

from __future__ import annotations

import json
import resource
import tempfile
import time
from pathlib import Path

from app.api.collections import _mode
from app.collections.embeddings import MODEL_NAME, embed
from app.collections.service import CollectionService, cosine
from app.collections.store import CollectionStore


def main():
    texts = [
        "Collection topic: Sustainable fashion. Context: recycled clothing and circular textiles. Examples: eco fashion; upcycled denim.",
        "Topic: Áo khoác denim tái chế. Wikimedia Pageviews source context: project vi.wikipedia, country scope US.",
        "Collection topic: Thời trang bền vững. Context: recycled clothing, eco friendly textiles. Examples: circular fashion; quần áo tái chế.",
        "Topic: Upcycled denim jacket. Wikimedia Pageviews source context: project en.wikipedia, country scope US.",
        "Topic: Mars rover exploration. Wikimedia Pageviews source context: project en.wikipedia, country scope US.",
        "Topic: Ancient Roman architecture. Wikimedia Pageviews source context: project en.wikipedia, country scope US.",
        "Topic: Vietnamese traditional cooking recipes. Wikimedia Pageviews source context: project vi.wikipedia, country scope US.",
        "Wikimedia topic: 2026 United States Senate elections.",
        "Wikimedia topic: 2026 United States elections.",
        "Wikimedia topic: Stacy Keach.",
        "Wikimedia topic: Katie Miller.",
        "Wikimedia topic: Special:Search.",
    ]
    started = time.perf_counter()
    vectors = embed(texts)
    elapsed = time.perf_counter() - started
    pairs = {
        "vi_collection_vi_keyword": cosine(vectors[2], vectors[1]),
        "en_collection_vi_keyword": cosine(vectors[0], vectors[1]),
        "vi_collection_en_keyword": cosine(vectors[2], vectors[3]),
        "en_collection_en_keyword": cosine(vectors[0], vectors[3]),
        "negative_mars": cosine(vectors[0], vectors[4]),
        "negative_architecture": cosine(vectors[0], vectors[5]),
        "negative_cooking": cosine(vectors[0], vectors[6]),
        "proposal_positive_us_elections": cosine(vectors[7], vectors[8]),
        "proposal_negative_unrelated_people": cosine(vectors[9], vectors[10]),
        "proposal_negative_technical_search": cosine(vectors[9], vectors[11]),
    }
    report = {"model": MODEL_NAME, "dimensions": len(vectors[0]), "elapsed_seconds": round(elapsed, 3),
        "max_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "cosine_examples_raw_not_probability": {key: round(value, 4) for key, value in pairs.items()}}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    assert all(len(vector) == 384 for vector in vectors)
    assert min(pairs[key] for key in ("vi_collection_vi_keyword", "en_collection_vi_keyword",
        "vi_collection_en_keyword", "en_collection_en_keyword")) >= 0.43
    assert max(pairs[key] for key in ("negative_mars", "negative_architecture", "negative_cooking")) < 0.45
    assert pairs["proposal_positive_us_elections"] >= 0.82
    assert pairs["proposal_negative_unrelated_people"] < 0.82
    assert pairs["proposal_negative_technical_search"] < 0.82

    # Full service acceptance replay: this deliberately uses the real local model above,
    # including the 105th record beyond the current inbox page size.
    with tempfile.TemporaryDirectory(prefix="trendscout-local-ai-acceptance-") as directory:
        store = CollectionStore(Path(directory) / "collections.sqlite")
        assert store.connect().execute("SELECT value FROM settings WHERE key='creation_mode'").fetchone() is None
        smart_home = store.create({"name": "Smart Home"})
        rows = [{"article_display": f"Ancient Roman architecture {index}",
                 "article_original": f"Ancient_Roman_architecture_{index}",
                 "project": "en.wikipedia", "observation_date": "2026-11-12",
                 "fetched_at": "2026-11-13T00:00:00Z", "provider_rank": index + 1,
                 "views_ceil": 1000} for index in range(104)]
        rows.extend([
            {"article_display": "Robot vacuum cleaner", "article_original": "Robot_vacuum_cleaner",
             "project": "en.wikipedia", "observation_date": "2026-11-12",
             "fetched_at": "2026-11-13T00:00:00Z", "provider_rank": 105, "views_ceil": 1000},
            {"article_display": "Mars rover mission", "article_original": "Mars_rover_mission",
             "project": "en.wikipedia", "observation_date": "2026-11-12",
             "fetched_at": "2026-11-13T00:00:00Z", "provider_rank": 106, "views_ceil": 1000},
        ])
        source = [{"source_id": "wikimedia_pageviews_top_per_country", "data_mode": "LIVE", "records": rows}]
        service = CollectionService(store)
        assert _mode(service) == "AUTO_CREATE"
        result = service._process_fetch("real-model-acceptance-1", source)
        assert result["processed"] == 106, result
        smart_keywords = store.keywords_for(smart_home)
        robot = next(row for row in rows if row["article_display"] == "Robot vacuum cleaner")
        robot_id = __import__("app.collections.store", fromlist=["keyword_record"]).keyword_record(
            robot, source[0]["source_id"], "LIVE")["id"]
        assert any(item["id"] == robot_id and item["origin"] == "LOCAL_AI" for item in smart_keywords), result
        assert not any(item["keyword"] == "Mars rover mission" for item in smart_keywords), result
        persisted_robot = next(row for row in store.connect().execute(
            "SELECT * FROM keywords WHERE id=?", (robot_id,)))
        store.feedback_action(smart_home, dict(persisted_robot), "REMOVE")
        service._process_fetch("real-model-acceptance-2", source)
        reopened = CollectionStore(Path(directory) / "collections.sqlite")
        assert not any(item["id"] == robot_id for item in reopened.keywords_for(smart_home))
        space = reopened.create({"name": "Space exploration", "examples": ["Mars rover mission"]})
        import asyncio
        asyncio.run(CollectionService(reopened).reclassify_unassigned())
        assert any(item["keyword"] == "Mars rover mission" for item in reopened.keywords_for(space))
        assert reopened.list_collections()
        print(json.dumps({"automatic_assignment": "PASS", "full_persisted_records": 106,
            "late_record_assigned": True, "unrelated_record_rejected": True,
            "rejection_survives_repeated_fetch_and_reopen": True,
            "new_collection_reclassified_cached_embeddings": True,
            "collection_auto_creation_default": "AUTO_CREATE"}, indent=2))


if __name__ == "__main__":
    main()
