"""Deterministic execution contexts, scoped cursors, and request budget tracking."""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.sources.catalog import YOUTUBE_SOURCE_ID, source_capability
from app.sources.query_plan import QueryPlan, get_query_plan
from app.sources.youtube.models import (
    QueryContext,
    QueryExecutionPlan,
    RequestBudgetSummary,
)


@dataclass(frozen=True)
class ExecutionLimits:
    """Server-selected bounds. The production defaults preserve plan v1's one-page cap."""

    max_requests: int | None = None
    max_pages_per_query: int | None = None
    max_queries: int | None = None
    deadline_seconds: float = 60.0
    max_results: int = 50

    def resolved(self, plan: QueryPlan) -> "ResolvedExecutionLimits":
        query_count = len(plan.seeds) if self.max_queries is None else self.max_queries
        page_count = (
            plan.max_pages_per_query
            if self.max_pages_per_query is None
            else self.max_pages_per_query
        )
        if type(query_count) is not int or not 1 <= query_count <= len(plan.seeds):
            raise ValueError("Query count limit is outside the selected plan")
        if type(page_count) is not int or not 1 <= page_count <= 5:
            raise ValueError("Page limit must be between 1 and 5")
        planned = query_count * page_count
        max_requests = planned if self.max_requests is None else self.max_requests
        if type(max_requests) is not int or not 1 <= max_requests <= 500:
            raise ValueError("Request budget must be between 1 and 500")
        if (
            isinstance(self.deadline_seconds, bool)
            or not isinstance(self.deadline_seconds, (int, float))
            or not math.isfinite(self.deadline_seconds)
            or not 0.1 <= self.deadline_seconds <= 300
        ):
            raise ValueError("Batch deadline must be within [0.1, 300] seconds")
        if type(self.max_results) is not int or not 1 <= self.max_results <= 50:
            raise ValueError("maxResults must be between 1 and 50")
        return ResolvedExecutionLimits(
            max_requests=max_requests,
            max_pages_per_query=page_count,
            max_queries=query_count,
            planned_calls=planned,
            deadline_seconds=float(self.deadline_seconds),
            max_results=self.max_results,
        )


@dataclass(frozen=True)
class ResolvedExecutionLimits:
    max_requests: int
    max_pages_per_query: int
    max_queries: int
    planned_calls: int
    deadline_seconds: float
    max_results: int


def build_execution_plan(plan: QueryPlan, started_at: datetime) -> QueryExecutionPlan:
    """Capture one UTC timestamp and derive one stable rolling seven-day cutoff."""
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("Batch start time must be timezone-aware")
    canonical = get_query_plan(plan.plan_id, plan.version)
    if canonical != plan:
        raise ValueError("Only the registered server-controlled query plan is supported")
    started_utc = started_at.astimezone(timezone.utc)
    published_after = started_utc - timedelta(days=7)
    capability = source_capability(YOUTUBE_SOURCE_ID)
    contexts = []
    for seed in plan.seeds:
        fingerprint_input = {
            "query_plan_id": plan.plan_id,
            "query_plan_version": plan.version,
            "query_plan_fingerprint": plan.fingerprint,
            "canonical_query": seed.query,
            "taxonomy_scope_label": seed.scope,
            "region_code": "US",
            "relevance_language": "en",
            "published_after": published_after.isoformat().replace("+00:00", "Z"),
        }
        serialized = json.dumps(
            fingerprint_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        query_fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        contexts.append(
            QueryContext(
                plan_id=plan.plan_id,
                plan_version=plan.version,
                plan_fingerprint=plan.fingerprint,
                query_fingerprint=query_fingerprint,
                canonical_query=seed.query,
                taxonomy_scope_label=seed.scope,
                source_id=capability.source_id,
                provider_id=capability.provider_id,
                region_code="US",
                relevance_language="en",
                batch_started_at=started_utc,
                published_after=published_after,
            )
        )
    return QueryExecutionPlan(
        plan_id=plan.plan_id,
        plan_version=plan.version,
        plan_fingerprint=plan.fingerprint,
        batch_started_at=started_utc,
        published_after=published_after,
        queries=tuple(contexts),
    )


class RequestBudgetTracker:
    """Counts every reserved network attempt, including retries."""

    def __init__(self, planned_calls: int, max_requests: int):
        self.planned_calls = planned_calls
        self.max_requests = max_requests
        self.attempted_calls = 0
        self.completed_calls = 0
        self.failed_calls = 0
        self.retried_calls = 0
        self.pages_fetched = 0

    @property
    def remaining(self) -> int:
        return self.max_requests - self.attempted_calls

    def reserve(self, *, retry: bool = False) -> bool:
        if self.remaining <= 0:
            return False
        self.attempted_calls += 1
        if retry:
            self.retried_calls += 1
        return True

    def completed(self, *, page_fetched: bool = False) -> None:
        self.completed_calls += 1
        if page_fetched:
            self.pages_fetched += 1

    def failed(self) -> None:
        self.failed_calls += 1

    def snapshot(self, stop_reason: str | None) -> RequestBudgetSummary:
        return RequestBudgetSummary(
            planned_calls=self.planned_calls,
            max_requests=self.max_requests,
            attempted_calls=self.attempted_calls,
            completed_calls=self.completed_calls,
            failed_calls=self.failed_calls,
            retried_calls=self.retried_calls,
            pages_fetched=self.pages_fetched,
            remaining_budget=self.remaining,
            stop_reason=stop_reason,
        )
