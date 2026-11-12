"""Build explainable discovery results from bounded normalized Fetch records."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import date, datetime, time, timezone

from app.discovery.assessment import assess_candidate
from app.discovery.contracts import Observation, ProcessingGrant
from app.discovery.extraction import extract_candidates
from app.discovery.prioritization import prioritize_for_research
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
    source_capability,
)
from app.sources.serpapi.normalization import canonical_url

_ALLOWED = frozenset({WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID})
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
    if source_id not in _ALLOWED or not isinstance(record, dict):
        return ()
    capability = source_capability(source_id)
    mode = source_result.get("data_mode")
    delivery = source_result.get("delivery_state", "FRESH")
    if (mode not in {"SYNTHETIC", "LIVE"} or delivery not in {"FRESH", "CACHED"}
            or record.get("data_mode", mode) != mode):
        return ()
    fetched = _timestamp(record.get("fetched_at"))
    query_evidence = _query_values(source_id, record)
    version = f"source-v{capability.capability_version}"

    if source_id == WIKIMEDIA_SOURCE_ID:
        phrase = _text(record.get("article_display") or record.get("canonical_keyword")
                       or record.get("article_original"))
        project = _text(record.get("project"))
        country = record.get("country_scope")
        access = _text(record.get("access"))
        observed = _timestamp(record.get("observation_date"), allow_date=True)
        document = hashlib.sha256(
            f"{project}:{record.get('article_original')}:{observed.date()}".encode("utf-8")
        ).hexdigest()
        scope = f"wikimedia:{country}:{access}:{project}"
        market = "US_COUNTRY_SCOPED" if country == "US" else "UNKNOWN"
    elif source_id in {GOOGLE_SOURCE_ID, BING_SOURCE_ID}:
        phrase = " ".join(part for part in (
            _text(record.get("title")), _text(record.get("snippet"))
        ) if part)
        document = _text(record.get("canonical_url"))
        observed = fetched
        fingerprints = sorted({
            str(row.get("query", {}).get("query_fingerprint"))
            for row in record.get("evidence", [])
            if isinstance(row, dict) and isinstance(row.get("query"), dict)
            and isinstance(row["query"].get("query_fingerprint"), str)
        })
        scope_id = hashlib.sha256(",".join(fingerprints).encode("ascii")).hexdigest()[:24]
        scope = f"{source_id}:{scope_id}:US-filter:en"
        market = "US_FILTER_ONLY"
    else:
        phrase = " ".join(part for part in (
            _text(record.get("title")), _text(record.get("description"))
        ) if part)
        document = _text(record.get("provider_video_id"))
        observed = fetched
        fingerprints = sorted({
            str(row.get("query_fingerprint")) for row in record.get("query_evidence", [])
            if isinstance(row, dict) and isinstance(row.get("query_fingerprint"), str)
        })
        scope_id = hashlib.sha256(",".join(fingerprints).encode("ascii")).hexdigest()[:24]
        scope = f"{source_id}:{scope_id}:US-filter:en"
        market = "US_FILTER_ONLY"

    if (not phrase or len(phrase) > 10_000 or len(_WORD.findall(phrase)) > 100
            or not document or len(scope) > 256):
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
    observation = Observation(
        source_id=source_id, provider_id=capability.provider_id,
        document_id=hashlib.sha256(
            f"{source_id}\0{document}\0{phrase}".encode("utf-8")
        ).hexdigest(),
        text=phrase,
        observed_at=observed, fetched_at=fetched, scope=scope,
        query_evidence=query_evidence, market_evidence=market,
        data_mode=mode, parser_version=version,
        language="und" if mode == "LIVE" else "en",
        source_url=url, delivery_mode=delivery,
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
    if (not isinstance(source_results, list) or len(source_results) > len(_ALLOWED)
            or empty_mode not in {"SYNTHETIC", "LIVE"}):
        raise ValueError("bounded source results required")
    grants = {grant.source_id: grant for grant in processing_grants
              if isinstance(grant, ProcessingGrant)}
    if len(grants) != len(processing_grants):
        raise ValueError("invalid or duplicate processing grants")
    observations = []
    rejected_records = 0
    for source_result in source_results:
        if not isinstance(source_result, dict):
            rejected_records += 1
            continue
        source_id = source_result.get("source_id")
        if source_id not in _ALLOWED:
            rejected_records += 1
            continue
        records = source_result.get("records", [])
        if not isinstance(records, list) or len(records) > 512:
            rejected_records += 1
            continue
        if source_result.get("data_mode") == "LIVE":
            grant = grants.get(source_id)
            if (grant is None or grant.extraction_allowed is not True
                    or not isinstance(grant.policy_version, str) or not grant.policy_version.strip()
                    or not isinstance(grant.expires_at, datetime)
                    or grant.expires_at.tzinfo is None or grant.expires_at.utcoffset() is None
                    or grant.expires_at <= now):
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

    candidates = prioritize_for_research(extract_candidates(
        tuple(observations), grants=processing_grants, now=now))
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
            "keyword": candidate.keyword,
            "categories": list(candidate.categories),
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
            } for item in assessments] + [{
                "source_id": source_id, "scope": scope, "market_evidence": market,
                "classification": "INSUFFICIENT_EVIDENCE",
                "reason": "DERIVED_METRICS_NOT_APPROVED",
                "limitations": ["NO_BASELINE"],
            } for source_id, scope, market in sorted(withheld)],
            "evidence": [{
                "source_id": item.source_id,
                "provider_id": item.provider_id,
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
            } for item in candidate.evidence],
        })
    return {
        "status": "INSUFFICIENT_EVIDENCE",
        "data_mode": result_mode,
        "candidate_count": len(candidates),
        "shown_count": len(shown),
        "rejected_record_count": rejected_records,
        "candidates": shown,
        "limitations": (["SYNTHETIC_ONLY", "NOT_LIVE_VERIFIED"] if result_mode == "SYNTHETIC" else [])
                       + ["NO_BASELINE", "COVERAGE_LIMITED", "US_FILTER_IS_NOT_US_AUDIENCE"],
    }
