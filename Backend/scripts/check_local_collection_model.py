"""No-network smoke/evaluation for the already-cached multilingual local model."""

from __future__ import annotations

import json
import resource
import time

from app.collections.embeddings import MODEL_NAME, embed
from app.collections.service import cosine


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


if __name__ == "__main__":
    main()
