"""Build explainable discovery results from bounded normalized Fetch records."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import date, datetime, time, timezone

from app.analysis.keyword_identity import canonical_keyword_digest
from app.discovery.assessment import assess_candidate
from app.discovery.contracts import Observation, ProcessingGrant
from app.discovery.extraction import extract_candidates
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
    source_capability,
)
from app.sources.serpapi.normalization import canonical_url

_MAX_SOURCE_RESULTS = 32
_MAX_CANDIDATES_SHOWN = 100
_MAX_OBSERVATIONS = 10_000
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)


def _timestamp(value: object, *, allow_date: bool = False) -> datetime:
    if allow_date and isinstance(value, str):
        try:
            day = date.fromisoformat(value)
            return datetime.combine(day, time.min, timezone.utc)
        except ValueError:
            pass
    if not isinstance(value, str):
        raise ValueError("timestamp required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("timestamp invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return parsed.astimezone(timezone.utc)


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _query_values(source_id: str, record: dict) -> tuple[str, ...]:
    rows = record.get("evidence", []) if source_id in {GOOGLE_SOURCE_ID, BING_SOURCE_ID} else record.get(
        "query_evidence", []
    )
    if not isinstance(rows, list):
        return ()
    names = []
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        query = row.get("query") if source_id in {GOOGLE_SOURCE_ID, BING_SOURCE_ID} else row
        if isinstance(query, dict):
            value = query.get("canonical_query")
            if isinstance(value, str) and value.strip() and len(value) <= 256:
                names.append(value.strip())
    return tuple(dict.fromkeys(names))


def _observation(source_id: str, record: dict, source_result: dict) -> tuple[Observation, ...]:
    if not isinstance(record, dict):
        return ()
    try:
        capability = source_capability(source_id)
    except (TypeError, ValueError):
        return ()
    mode = source_result.get("data_mode")
    delivery = source_result.get("delivery_state", record.get("delivery_state", "FRESH"))
    if (mode not in {"SYNTHETIC", "LIVE"} or delivery not in {"FRESH", "CACHED"}
            or record.get("data_mode", mode) != mode):
        return ()
    fetched = _timestamp(record.get("fetched_at"))
    query_evidence = _query_values(source_id, record)
    version = f"source-v{capability.capability_version}"
    extraction_mode = "DIRECT_KEYWORD"
    source_metric_name = None
    source_metric_unit = None
    source_metric_value = None

    if source_id == WIKIMEDIA_SOURCE_ID:
        phrase = _text(record.get("article_display") or record.get("canonical_keyword")
                       or record.get("article_original"))
        project = _text(record.get("project"))
        rank = record.get("provider_rank")
        approximate_views = record.get("views_ceil")
        country = record.get("country_scope")
        access = _text(record.get("access"))
        observed = _timestamp(record.get("observation_date"), allow_date=True)
        document = hashlib.sha256(
            f"{project}:{record.get('article_original')}:{observed.date()}".encode("utf-8")
        ).hexdigest()
        scope = f"wikimedia:{country}:{access}:{project}"
        market = ("US_COUNTRY_SCOPED" if country == "US" else "GLOBAL"
                  if isinstance(country, str) and country.strip().casefold()
                  in {"global", "world", "worldwide"} else "UNKNOWN")
        source_metric_name = capability.metric_name
        source_metric_unit = capability.metric_unit
        source_metric_value = approximate_views
        raw_source_keyword = record.get("article_original")
    elif source_id == GOOGLE_SOURCE_ID and record.get("data_source") == "GOOGLE_TRENDS":
        phrase = _text(record.get("canonical_keyword"))
        document = _text(record.get("canonical_keyword_identity"))
        if document != canonical_keyword_digest(phrase):
            return ()
        cluster_times = [
            _timestamp(cluster.get("started_at"))
            for cluster in record.get("trend_clusters", [])
            if isinstance(cluster, dict) and isinstance(cluster.get("started_at"), str)
        ]
        provider_data_at = record.get("provider_data_at")
        observed = (min(cluster_times) if cluster_times else
                    _timestamp(provider_data_at) if isinstance(provider_data_at, str) else fetched)
        window = record.get("time_window_hours")
        scope = f"google-trends:US:en:{window}h" if window in {4, 24, 48, 168} else (
            "google-trends:US:en:limited-feed"
        )
        market = "US_FILTER_ONLY"
        source_metric_name = "google_trends_cluster_metrics"
        source_metric_unit = "provider-defined per cluster"
        raw_source_keyword = record.get("source_keyword_raw") or phrase
    elif source_id in {GOOGLE_SOURCE_ID, BING_SOURCE_ID}:
        # A result title, snippet, URL, or query is not itself a keyword contract.
        return ()
    else:
        extraction_mode = record.get("keyword_extraction_mode")
        if extraction_mode not in capability.keyword_extraction_modes:
            return ()
        raw_keyword = (record.get("canonical_keyword") if extraction_mode == "DIRECT_KEYWORD"
                       else record.get("extraction_text"))
        phrase = _text(raw_keyword)
        document = _text(record.get("source_record_id") or record.get("record_id")
                         or record.get("canonical_keyword_identity"))
        observed = _timestamp(record.get("observed_at")) if record.get("observed_at") else fetched
        scope = _text(record.get("scope"))
        market_value = record.get("market_evidence")
        market = (market_value if market_value in {
            "US_COUNTRY_SCOPED", "US_FILTER_ONLY", "GLOBAL", "UNKNOWN"
        } else "UNKNOWN")
        reported_geographies = (
            record.get("country_scope"), record.get("region"),
            record.get("regionCode"), record.get("market"),
        )
        if any(isinstance(value, str) and value.strip().casefold()
               in {"global", "world", "worldwide"} for value in reported_geographies):
            market = "GLOBAL"
        source_metric_name = capability.metric_name
        source_metric_unit = capability.metric_unit
        source_metric_value = record.get("metric_value")
        raw_source_keyword = record.get("raw_source_keyword") or phrase

    if (not phrase or len(phrase) > 10_000 or len(_WORD.findall(phrase)) > 100
            or not document or len(scope) > 256):
        return ()
    if extraction_mode == "DIRECT_KEYWORD" and len(phrase) > 256:
        return ()
    if source_id == WIKIMEDIA_SOURCE_ID and (
        (rank is not None and (type(rank) is not int or rank < 1))
        or (approximate_views is not None
            and (type(approximate_views) is not int or approximate_views < 0))
        or (project and len(project) > 128)
    ):
        return ()
    url_value = record.get("source_url") if "source_url" in record else (
        record.get("canonical_url") or record.get("canonical_video_url")
    )
    url = _text(url_value) or None
    if url is not None:
        try:
            url = canonical_url(url)
        except ValueError:
            return ()
        if not url.startswith("https://") or len(url) > 2048:
            return ()
    source_metrics = None
    if source_id == GOOGLE_SOURCE_ID and record.get("data_source") == "GOOGLE_TRENDS":
        clusters = record.get("trend_clusters")
        if not isinstance(clusters, list) or len(clusters) > 5000:
            return ()
        # Keep Trend metrics and SERP evidence attached to the parent keyword.
        # Related phrases remain relationships, never separate observations.
        source_metrics = {
            "data_source": "GOOGLE_TRENDS",
            "discovery_mode": record.get("discovery_mode"),
            "provider_id": record.get("provider_id"),
            "transport_id": record.get("transport_id"),
            "region": record.get("region"),
            "language_filter": record.get("language"),
            "time_window_hours": record.get("time_window_hours"),
            "provider_data_at": record.get("provider_data_at"),
            "provider_created_at": record.get("provider_created_at"),
            "source_url": record.get("source_url"),
            "trend_clusters": clusters,
            "serp_verification": record.get("serp_verification"),
        }
        if (not isinstance(source_metrics["discovery_mode"], str)
                or not isinstance(source_metrics["provider_id"], str)
                or not isinstance(source_metrics["transport_id"], str)
                or source_metrics["region"] != "US"
                or source_metrics["language_filter"] != "en"
                or source_metrics["time_window_hours"] not in {24, None}):
            return ()
    observation = Observation(
        source_id=source_id, provider_id=capability.provider_id,
        document_id=hashlib.sha256(
            f"{source_id}\0{document}\0{phrase}".encode("utf-8")
        ).hexdigest(),
        text=phrase,
        observed_at=observed, fetched_at=fetched, scope=scope,
        query_evidence=query_evidence, market_evidence=market,
        data_mode=mode, parser_version=version,
        extraction_mode=extraction_mode,
        language="und" if mode == "LIVE" else "en",
        source_url=url, delivery_mode=delivery,
        article=(_text(record.get("article_original")) or phrase
                 if source_id == WIKIMEDIA_SOURCE_ID else None),
        project=(project if source_id == WIKIMEDIA_SOURCE_ID else None),
        provider_rank=(rank if source_id == WIKIMEDIA_SOURCE_ID and type(rank) is int else None),
        approximate_views=(approximate_views if source_id == WIKIMEDIA_SOURCE_ID
                           and type(approximate_views) is int else None),
        source_metrics=source_metrics,
        source_metric_name=source_metric_name,
        source_metric_unit=source_metric_unit,
        source_metric_value=source_metric_value,
        raw_source_keyword=(raw_source_keyword if isinstance(raw_source_keyword, str) else None),
    )
    if not query_evidence:
        return (observation,)
    return tuple(
        Observation(**{**observation.__dict__, "query_evidence": (query,)})
        for query in query_evidence
    )


def discover_fetch_results(source_results: list[dict], *, now: datetime,
                           processing_grants: tuple[ProcessingGrant, ...] = (),
                           empty_mode: str = "SYNTHETIC") -> dict:
    """Extract bounded keywords while preserving source, scope, mode and cache state.

    Fetches have no historical coverage yet, so classifications intentionally stay
    `INSUFFICIENT_EVIDENCE`; query ranking or one snapshot cannot imply growth.
    """
    if (not isinstance(source_results, list) or len(source_results) > _MAX_SOURCE_RESULTS
            or empty_mode not in {"SYNTHETIC", "LIVE"}):
        raise ValueError("bounded source results required")
    grants = {grant.source_id: grant for grant in processing_grants
              if isinstance(grant, ProcessingGrant)}
    if len(grants) != len(processing_grants):
        raise ValueError("invalid or duplicate processing grants")
    observations = []
    rejected_records = 0
    processing_blocked = False
    source_coverage = []
    for source_result in source_results:
        if not isinstance(source_result, dict):
            rejected_records += 1
            continue
        source_id = source_result.get("source_id")
        try:
            capability = source_capability(source_id)
        except (TypeError, ValueError):
            rejected_records += 1
            continue
        if not capability.keyword_extraction_modes:
            records = source_result.get("records", [])
            processing_blocked |= source_id == YOUTUBE_SOURCE_ID and source_result.get(
                "data_mode"
            ) == "LIVE"
            rejected_records += len(records) if isinstance(records, list) else 1
            continue
        coverage = source_result.get("coverage", {})
        if source_id == WIKIMEDIA_SOURCE_ID and isinstance(coverage, dict):
            source_coverage.append({
                "source_id": source_id,
                "provider_returned_records": coverage.get(
                    "provider_returned_records", source_result.get("record_count", 0)),
                "accepted_records": coverage.get("accepted_records", source_result.get("record_count", 0)),
                "rejected_records": coverage.get("rejected_records", coverage.get("rejected", 0)),
                "explicitly_omitted_records": coverage.get(
                    "explicitly_omitted_records", coverage.get("records_omitted_storage_limit", 0)),
                "source_coverage": coverage.get("source_coverage", "TOP_PER_COUNTRY_DAILY_LIST"),
            })
        grant = grants.get(source_id)
        extraction_granted = (
            isinstance(grant, ProcessingGrant)
            and grant.extraction_allowed is True
            and isinstance(grant.policy_version, str) and bool(grant.policy_version.strip())
            and isinstance(grant.expires_at, datetime)
            and grant.expires_at.tzinfo is not None and grant.expires_at.utcoffset() is not None
            and grant.expires_at > now
        )
        if source_result.get("data_mode") == "LIVE" and not extraction_granted:
            processing_blocked = True
        records = source_result.get("records", [])
        maximum_records = (2000 if source_id == WIKIMEDIA_SOURCE_ID else
                           5000 if source_id == GOOGLE_SOURCE_ID else 512)
        if not isinstance(records, list) or len(records) > maximum_records:
            rejected_records += len(records) if isinstance(records, list) else 1
            continue
        if source_result.get("data_mode") == "LIVE":
            if not extraction_granted:
                rejected_records += len(records)
                continue
        for record in records:
            try:
                mapped = _observation(source_id, record, source_result)
                if not mapped:
                    rejected_records += 1
                    continue
                if len(observations) + len(mapped) > _MAX_OBSERVATIONS:
                    rejected_records += 1
                    continue
                observations.extend(mapped)
            except (TypeError, ValueError):
                rejected_records += 1

    # This response contains source evidence, not business research priority.
    # Fetch has no compatible history here, so every trend assessment is
    # insufficient; use identity as the deterministic display tie-breaker and
    # leave Fashion/Home preference to the separate verification queue.
    candidates = tuple(sorted(extract_candidates(
        tuple(observations), grants=processing_grants, now=now),
        key=lambda item: item.keyword_id))
    modes = {observation.data_mode for observation in observations}
    result_mode = next(iter(modes)) if len(modes) == 1 else "MIXED" if modes else empty_mode
    shown = []
    for candidate in candidates[:_MAX_CANDIDATES_SHOWN]:
        assessable = tuple(item for item in candidate.evidence
                           if item.data_mode == "SYNTHETIC"
                           or grants[item.source_id].derived_metrics_allowed is True)
        assessments = (assess_candidate(replace(candidate, evidence=assessable), (),
                                        as_of=now.date(), now=now, grants=processing_grants)
                       if assessable else ())
        withheld = {(item.source_id, item.scope, item.market_evidence)
                    for item in candidate.evidence if item not in assessable}
        shown.append({
            "keyword_id": candidate.keyword_id,
            "canonical_keyword_identity": candidate.keyword_id,
            "keyword": candidate.keyword,
            "categories": list(candidate.categories),
            "taxonomy_version": candidate.taxonomy_version,
            "extraction_version": candidate.extraction_version,
            "classification_version": candidate.classification_version,
            "status": "INSUFFICIENT_DATA",
            "score": None,
            "data_mode": (next(iter({item.data_mode for item in candidate.evidence}))
                          if len({item.data_mode for item in candidate.evidence}) == 1 else "MIXED"),
            "quality_flags": list(candidate.quality_flags),
            "assessments": [{
                "source_id": item.source_id,
                "scope": item.scope,
                "market_evidence": item.market_evidence,
                "classification": item.classification,
                "reason": item.reason,
                "limitations": list(item.limitations),
                "algorithm_version": item.algorithm_version,
            } for item in assessments] + [{
                "source_id": source_id, "scope": scope, "market_evidence": market,
                "classification": "INSUFFICIENT_EVIDENCE",
                "reason": "DERIVED_METRICS_NOT_APPROVED",
                "limitations": ["NO_BASELINE"],
            } for source_id, scope, market in sorted(withheld)],
            "evidence": [{
                "source_id": item.source_id,
                "provider_id": item.provider_id,
                "parser_version": item.parser_version,
                "document_id": item.document_id,
                "observed_at": item.observed_at.isoformat(),
                "fetched_at": item.fetched_at.isoformat(),
                "scope": item.scope,
                "query_evidence": list(item.query_evidence),
                "market_evidence": item.market_evidence,
                "data_mode": item.data_mode,
                "delivery_mode": item.delivery_mode,
                "language": item.language,
                "source_url": item.source_url,
                "article": item.article,
                "project": item.project,
                "provider_rank": item.provider_rank,
                "approximate_views": item.approximate_views,
                "source_metrics": item.source_metrics,
                "canonical_keyword_identity": candidate.keyword_id,
                "source_metric_name": item.source_metric_name,
                "source_metric_unit": item.source_metric_unit,
                "source_metric_value": item.source_metric_value,
                "raw_source_keyword": item.raw_source_keyword,
            } for item in candidate.evidence],
        })
    return {
        "status": "DISCOVERY_PROCESSING_BLOCKED" if processing_blocked else "INSUFFICIENT_EVIDENCE",
        "data_mode": result_mode,
        "candidate_count": len(candidates),
        "shown_count": len(shown),
        "rejected_record_count": rejected_records,
        "source_coverage": source_coverage,
        "candidates": shown,
        "limitations": (["SYNTHETIC_ONLY", "NOT_LIVE_VERIFIED"] if result_mode == "SYNTHETIC" else [])
                       + (["DISCOVERY_PROCESSING_BLOCKED"] if processing_blocked else [])
                       + (["YOUTUBE_KEYWORD_EXTRACTION_DISABLED"]
                          if any(item.get("source_id") == YOUTUBE_SOURCE_ID
                                 for item in source_results if isinstance(item, dict)) else [])
                       + ["NO_BASELINE", "COVERAGE_LIMITED", "US_FILTER_IS_NOT_US_AUDIENCE"],
    }
