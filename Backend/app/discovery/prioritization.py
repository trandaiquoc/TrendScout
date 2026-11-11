"""Bounded research and verification selection; never performs provider requests."""

from datetime import datetime, timedelta
from typing import Mapping

from app.discovery.contracts import Assessment, Candidate
from app.discovery.extraction import _utc


def prioritize_for_research(candidates: tuple[Candidate, ...]) -> tuple[Candidate, ...]:
    """Place Fashion/Home first without changing assessment or excluding other topics."""
    return tuple(sorted(candidates, key=lambda item: (
        not bool({"fashion", "home_living"} & set(item.categories)), item.keyword_id
    )))


def select_for_verification(candidates: tuple[Candidate, ...],
                            assessments: tuple[Assessment, ...], *,
                            last_verified: Mapping[str, datetime], now: datetime,
                            max_requests: int, freshness: timedelta) -> tuple[Candidate, ...]:
    """Select a server-bounded queue; no source is contacted and v1 is not changed."""
    now = _utc(now)
    if type(max_requests) is not int or not 0 <= max_requests <= 100:
        raise ValueError("Verification budget must be within 0..100")
    if not isinstance(freshness, timedelta) or not timedelta(0) < freshness <= timedelta(days=365):
        raise ValueError("Invalid verification freshness window")
    if len({item.keyword_id for item in candidates}) != len(candidates):
        raise ValueError("Duplicate candidate identity")
    if max_requests == 0:
        return ()
    eligible = {
        item.keyword_id for item in assessments
        if item.classification in {"EMERGING", "ACCELERATING", "SUSTAINED"}
        and item.current_support is not None and item.current_support >= 2
        and item.market_evidence != "UNKNOWN"
    }
    selected = []
    for candidate in prioritize_for_research(candidates):
        if candidate.keyword_id not in eligible:
            continue
        previous = last_verified.get(candidate.keyword_id)
        if previous is not None:
            previous = _utc(previous)
            if previous > now:
                raise ValueError("Verification timestamp is in the future")
            if now - previous < freshness:
                continue
        selected.append(candidate)
        if len(selected) >= max_requests:
            break
    return tuple(selected)
