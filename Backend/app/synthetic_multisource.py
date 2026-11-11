"""Offline-only synthetic query-source fixtures and discovery handoff.

All HTTP clients use MockTransport. This module has no real credential provider,
cannot contact a provider and does not enable any live source.
"""

from datetime import datetime, timezone

import httpx

from app.discovery.assessment import assess_candidate
from app.discovery.contracts import Observation
from app.discovery.extraction import extract_candidates
from app.discovery.prioritization import prioritize_for_research
from app.sources.bing_search.adapter import BingSearchAdapter
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.sources.google_search.adapter import GoogleSearchAdapter
from app.sources.http_policy import HttpPolicy
from app.sources.serpapi.adapter import ExecutionLimits as SearchLimits
from app.sources.serpapi.client import SerpApiClient, create_offline_client
from app.sources.serpapi.quota import SharedQuotaPool
from app.sources.youtube import YouTubeSearchAdapter
from app.sources.youtube.client import create_offline_youtube_client
from app.sources.youtube.pagination import ExecutionLimits as VideoLimits

FIXTURE_TIME = datetime(2026, 1, 9, tzinfo=timezone.utc)
_QUERY_SCOPE = "one server-selected query; US localization filter"


class _FixtureCredentials:
    """Sent only into httpx.MockTransport; this is not a real API credential."""

    def resolve(self, reference):
        if reference not in {"SERPAPI_API_KEY", "YOUTUBE_API_KEY"}:
            return None
        return "offline-fixture-placeholder"


def _search_response(request):
    engine = request.url.params.get("engine")
    titles = {
        "google": "Synthetic linen pants guide",
        "bing": "Synthetic kitchen organizer ideas",
    }
    if engine not in titles:
        return httpx.Response(400, json={"error": "invalid fixture engine"})
    return httpx.Response(200, json={
        "search_metadata": {"status": "Success"},
        "organic_results": [{"position": 1, "link": f"https://example.org/{engine}-fixture",
                             "title": titles[engine], "snippet": "Invented demo content"}],
    })


def _video_response(_request):
    return httpx.Response(200, json={
        "kind": "youtube#searchListResponse", "pageInfo": {"totalResults": 1},
        "items": [{"kind": "youtube#searchResult",
                   "id": {"kind": "youtube#video", "videoId": "AbCdEf12345"},
                   "snippet": {"title": "Synthetic robot gardening demo",
                               "description": "Invented demo content",
                               "publishedAt": "2026-01-08T12:00:00Z"}}],
    })


async def collect_synthetic_discovery():
    """Run bounded fixture adapters and return JSON-safe source-scoped evidence."""
    credentials = _FixtureCredentials()
    policy = HttpPolicy(max_retries=0, max_retry_wait_seconds=0)
    observations = []
    sources = []
    pool = SharedQuotaPool(2, 2)
    async with create_offline_client(httpx.MockTransport(_search_response)) as http:
        client = SerpApiClient(http, credentials, pool, policy)
        for adapter_type in (GoogleSearchAdapter, BingSearchAdapter):
            batch = await adapter_type(client, utcnow=lambda: FIXTURE_TIME).execute(
                limits=SearchLimits(max_queries=1, max_pages_per_query=1))
            sources.append({"source_id": batch.source_id, "data_mode": batch.data_mode,
                            "status": batch.status, "complete": batch.complete,
                            "stop_reason": batch.stop_reason or "QUERY_CAP_REACHED",
                            "record_count": len(batch.records)})
            for record in batch.records:
                observations.append(Observation(
                    source_id=record.source_id, provider_id=record.provider_id,
                    document_id=record.canonical_url, text=record.title or "",
                    observed_at=record.fetched_at, fetched_at=record.fetched_at,
                    scope=_QUERY_SCOPE, query_evidence=tuple(e.query.canonical_query
                                                            for e in record.evidence),
                    market_evidence="US_FILTER_ONLY", data_mode="SYNTHETIC",
                    parser_version="serpapi-v1", source_url=record.canonical_url,
                    delivery_mode=("CACHED" if any(e.cache_state == "CACHED"
                                                   for e in record.evidence) else "FRESH"),
                ))
    async with create_offline_youtube_client(httpx.MockTransport(_video_response)) as http:
        batch = await YouTubeSearchAdapter(
            client=http, credential_resolver=credentials, http_policy=policy,
            utcnow=lambda: FIXTURE_TIME,
        ).execute(limits=VideoLimits(max_queries=1, max_pages_per_query=1, max_requests=1))
        sources.append({"source_id": batch.source_id, "data_mode": batch.data_mode,
                        "status": batch.status, "complete": batch.complete,
                        "stop_reason": batch.stop_reason or "QUERY_CAP_REACHED",
                        "record_count": len(batch.records)})
        for record in batch.records:
            observations.append(Observation(
                source_id=record.source_id, provider_id=record.provider_id,
                document_id=record.provider_video_id, text=record.title or "",
                observed_at=record.published_at or record.fetched_at, fetched_at=record.fetched_at,
                scope=_QUERY_SCOPE,
                query_evidence=tuple(e.canonical_query for e in record.query_evidence),
                market_evidence="US_FILTER_ONLY", data_mode="SYNTHETIC",
                parser_version="youtube-v1", source_url=record.canonical_video_url,
            ))
    candidates = prioritize_for_research(extract_candidates(tuple(observations), now=FIXTURE_TIME))
    results = []
    for candidate in candidates[:30]:
        assessments = assess_candidate(candidate, (), as_of=FIXTURE_TIME.date(), now=FIXTURE_TIME)
        results.append({
            "keyword_id": candidate.keyword_id, "keyword": candidate.keyword,
            "categories": list(candidate.categories), "data_mode": "SYNTHETIC",
            "assessments": [{"source_id": item.source_id, "scope": item.scope,
                             "market_evidence": item.market_evidence,
                             "classification": item.classification, "reason": item.reason}
                            for item in assessments],
            "evidence": [{"source_id": e.source_id, "provider_id": e.provider_id,
                          "observed_at": e.observed_at.isoformat(),
                          "fetched_at": e.fetched_at.isoformat(), "scope": e.scope,
                          "query_evidence": list(e.query_evidence), "data_mode": e.data_mode,
                          "delivery_mode": e.delivery_mode, "source_url": e.source_url}
                         for e in candidate.evidence],
        })
    expected = {GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID}
    if {item["source_id"] for item in sources} != expected:
        raise ValueError("Synthetic source set is incomplete")
    return {"data_mode": "SYNTHETIC", "sources": sources, "candidates": results,
            "candidate_count": len(candidates), "shown_count": len(results),
            "limitations": ["SYNTHETIC_ONLY", "ONE_QUERY_PER_SOURCE", "BASELINE_UNAVAILABLE",
                            "US_FILTER_IS_NOT_US_AUDIENCE", "NOT_LIVE_VERIFIED"]}


collect_synthetic_discovery.__trendscout_offline_only__ = True
