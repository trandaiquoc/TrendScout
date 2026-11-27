from datetime import datetime, timedelta, timezone

from app.discovery.multi_source import build_multi_source_profile
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID

NOW = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
CANONICAL_ID = "a" * 64


def _profile(*, keyword_rows=(), evidence_rows=(), algorithm_results=()):
    return build_multi_source_profile(
        canonical_keyword="wireless headphones",
        canonical_identity=CANONICAL_ID,
        data_mode="SYNTHETIC",
        keyword_rows=list(keyword_rows),
        evidence_rows=list(evidence_rows),
        algorithm_results=list(algorithm_results),
        now=NOW,
    )


def _google_evidence(evidence_id, keyword_id, provider_id):
    return {
        "id": evidence_id,
        "source_id": GOOGLE_SOURCE_ID,
        "display_name": "Google Search",
        "external_id": CANONICAL_ID,
        "content_digest": "d" * 64,
        "query_scope": {
            "evidence_kind": "provider_record",
            "data_source": "GOOGLE_TRENDS",
            "discovery_mode": "SERPAPI_TRENDING_NOW",
            "provider_id": provider_id,
            "snapshot_status": "SUCCESS",
        },
        "geography": {"field": "region", "value": "US"},
        "observed_at": NOW,
        "collected_at": NOW,
        "expires_at": NOW + timedelta(days=7),
        "keyword_id": keyword_id,
    }


def test_missing_evidence_is_explicit_and_not_negative_or_mislabelled_policy():
    profile = _profile(keyword_rows=[{
        "id": "keyword-1", "keyword": "wireless headphones", "evidence": {},
    }])
    sources = profile["source_breakdown"]
    google_search = next(item for item in sources
                         if item["source_id"] == GOOGLE_SOURCE_ID
                         and item["platform_id"] == "google_search")
    bing = next(item for item in sources if item["source_id"] == "bing_web_serp")
    youtube = next(item for item in sources if item["source_id"] == "youtube_video_search")

    assert google_search["evidence_status"] == "NO_STORED_EVIDENCE"
    assert bing["evidence_status"] == "NO_STORED_EVIDENCE"
    assert youtube["evidence_status"] == "POLICY_RESTRICTED"
    assert profile["corroboration"]["status"] == "NO_TREND_SIGNAL_EVIDENCE"
    assert all(item["absence_is_negative_evidence"] is False for item in sources)


def test_adapters_for_one_platform_share_independence_group():
    keyword_rows = [
        {"id": "keyword-1", "keyword": "wireless headphones", "evidence": {}},
        {"id": "keyword-2", "keyword": "wireless headphones", "evidence": {}},
    ]
    evidence_rows = [
        _google_evidence("evidence-1", "keyword-1", "provider-one"),
        _google_evidence("evidence-2", "keyword-2", "provider-two"),
    ]
    results = [
        {
            "keyword_id": keyword_id,
            "algorithm_id": "google-trends-increase-rank",
            "version": "1",
            "status": "COMPLETED",
            "input_digest": "b" * 64,
            "result_json": {
                "source_id": GOOGLE_SOURCE_ID,
                "provider_reported_increase_percentage": 120,
            },
        }
        for keyword_id in ("keyword-1", "keyword-2")
    ]

    profile = _profile(keyword_rows=keyword_rows,
                       evidence_rows=evidence_rows,
                       algorithm_results=results)
    trend_groups = [item for item in profile["independent_evidence_groups"]
                    if item["role"] == "TREND_SIGNAL"]

    assert len(trend_groups) == 1
    assert trend_groups[0]["provider_ids"] == ["provider-one", "provider-two"]
    assert profile["corroboration"]["independent_trend_signal_group_count"] == 1
    assert profile["corroboration"]["status"] == "SINGLE_INDEPENDENT_TREND_SIGNAL"
    assert all(item["coverage"]["source_population_totals"] == "NOT_RETAINED"
               for item in profile["source_breakdown"])


def test_verification_results_never_count_as_trend_signals():
    profile = _profile(
        keyword_rows=[{
            "id": "keyword-1", "keyword": "wireless headphones", "evidence": {},
        }],
        evidence_rows=[{
            "id": "bing-evidence",
            "source_id": BING_SOURCE_ID,
            "display_name": "Bing Search",
            "content_digest": "d" * 64,
            "query_scope": {
                "evidence_kind": "search_verification",
                "provider_id": "serpapi_bing",
                "transport_id": "serpapi",
                "market": "US",
                "snapshot_status": "SUCCESS",
            },
            "geography": {"field": "market", "value": "US"},
            "observed_at": NOW,
            "collected_at": NOW,
            "expires_at": NOW + timedelta(days=7),
            "keyword_id": "keyword-1",
        }],
        algorithm_results=[{
            "keyword_id": "keyword-1",
            "algorithm_id": "growth-ratio",
            "version": "1",
            "status": "COMPLETED",
            "value": 1.5,
            "input_digest": "e" * 64,
            "result_json": {
                "source_id": BING_SOURCE_ID,
                "input_metric": "result_rank",
                "input_unit": "ordinal result position",
                "window_days": 1,
            },
        }],
    )

    assert profile["trend_signal_outcomes"] == []
    assert profile["corroboration"]["status"] == "NO_TREND_SIGNAL_EVIDENCE"


def test_negative_google_trends_increase_is_reported_as_declining():
    profile = _profile(
        keyword_rows=[{
            "id": "keyword-1", "keyword": "wireless headphones", "evidence": {},
        }],
        evidence_rows=[_google_evidence("evidence-1", "keyword-1", "provider-one")],
        algorithm_results=[{
            "keyword_id": "keyword-1",
            "algorithm_id": "google-trends-increase-rank",
            "version": "1",
            "status": "COMPLETED",
            "input_digest": "b" * 64,
            "result_json": {
                "source_id": GOOGLE_SOURCE_ID,
                "provider_reported_increase_percentage": -15,
            },
        }],
    )

    assert profile["trend_signal_outcomes"][0]["direction"] == "DECLINING"


def test_conflicting_comparable_results_are_reported_without_composite_score():
    keyword_rows = [
        {"id": "keyword-1", "keyword": "wireless headphones", "evidence": {}},
        {"id": "keyword-2", "keyword": "wireless headphones", "evidence": {}},
    ]
    evidence_rows = [
        _google_evidence("evidence-1", "keyword-1", "provider-one"),
        _google_evidence("evidence-2", "keyword-2", "provider-two"),
    ]
    results = []
    for keyword_id, value, digest in (
            ("keyword-1", 1.5, "b" * 64), ("keyword-2", 0.5, "c" * 64)):
        results.append({
            "keyword_id": keyword_id,
            "algorithm_id": "growth-ratio",
            "version": "1",
            "status": "COMPLETED",
            "value": value,
            "input_digest": digest,
            "result_json": {
                "source_id": GOOGLE_SOURCE_ID,
                "input_metric": "provider_increase_percentage",
                "input_unit": "provider_reported_percentage",
                "window_days": 1,
            },
        })

    profile = _profile(keyword_rows=keyword_rows,
                       evidence_rows=evidence_rows,
                       algorithm_results=results)

    assert profile["corroboration"]["status"] == "CONFLICTING_COMPARABLE_SIGNALS"
    assert profile["corroboration"]["conflicting_evidence"] == [{
        "metric_contract": ["provider_increase_percentage",
                            "provider_reported_percentage", 1],
        "directions": ["DECLINING", "INCREASING"],
    }]
    assert profile["corroboration"]["composite_score"] is None
