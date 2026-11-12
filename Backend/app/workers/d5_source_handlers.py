"""Server-owned adapters from the four guarded sources into fetch-worker results.

This module creates no transports or credentials. The caller must supply already
constructed guarded gateways; live constructors remain approval-gated.
"""

import hashlib
import json
import math
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Callable, Mapping

from app.jobs.distributed_worker import SourceWorkContext
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)
from app.sources.guarded_collector import CollectionLimits, GuardedQueryCollector
from app.sources.query_plan import QueryPlan
from app.sources.wikimedia.live_gateway import (
    WikimediaGatewayError,
    WikimediaRequest,
    WikimediaTopPageGateway,
)

_QUERY_SOURCES = frozenset({GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID})
_ALL_SOURCES = _QUERY_SOURCES | {WIKIMEDIA_SOURCE_ID}
_NORMALIZED_RESULT_BUDGET_BYTES = 100_000


def _jsonable(value, *, depth=0):
    if depth > 16:
        raise ValueError("normalized evidence nesting limit exceeded")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence timestamp must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _jsonable(value.value, depth=depth + 1)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name), depth=depth + 1)
                for field in fields(value)}
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("normalized evidence keys must be strings")
        return {key: _jsonable(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item, depth=depth + 1) for item in value]
    if type(value) is float and not math.isfinite(value):
        raise ValueError("normalized evidence must be finite")
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise ValueError("unsupported normalized evidence value")


def _bounded_records(records):
    """Keep normalized rows within checkpoint budget and report every omission."""
    selected, used, omitted = [], 2, 0
    for record in records:
        encoded = json.dumps(record, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
        addition = len(encoded) + (1 if selected else 0)
        if used + addition > _NORMALIZED_RESULT_BUDGET_BYTES:
            omitted += 1
            continue
        selected.append(record)
        used += addition
    return selected, omitted


class D5SourceHandlers:
    """Build the fixed four-source handler map expected by DistributedFetchWorker."""

    def __init__(self, *, query_collectors: Mapping[str, GuardedQueryCollector],
                 wikimedia: WikimediaTopPageGateway, plan: QueryPlan,
                 limits: CollectionLimits,
                 observation_date: Callable[[], date] | None = None):
        if (set(query_collectors) != _QUERY_SOURCES
                or any(not isinstance(value, GuardedQueryCollector)
                       or value.gateway.source_id != source
                       for source, value in query_collectors.items())
                or not isinstance(wikimedia, WikimediaTopPageGateway)
                or not isinstance(plan, QueryPlan) or not isinstance(limits, CollectionLimits)
                or (observation_date is not None and not callable(observation_date))):
            raise ValueError("fixed four-source collectors, plan and limits required")
        self.query_collectors = dict(query_collectors)
        self.wikimedia = wikimedia
        self.plan = plan
        self.limits = limits
        self.observation_date = observation_date or (lambda: datetime.now(timezone.utc).date())

    def build(self) -> dict[str, Callable[[SourceWorkContext], object]]:
        handlers = {source: self._query_handler(source) for source in sorted(_QUERY_SOURCES)}
        handlers[WIKIMEDIA_SOURCE_ID] = self._wikimedia_handler
        if set(handlers) != _ALL_SOURCES:
            raise ValueError("incomplete guarded source registry")
        return handlers

    def _query_handler(self, source_id):
        collector = self.query_collectors[source_id]

        async def collect(context: SourceWorkContext):
            context.heartbeat()
            result = await collector.collect(
                plan=self.plan,
                limits=self.limits,
                job_id=context.job_id,
                owner=context.owner,
                fence=context.fence,
                heartbeat=context.heartbeat,
            )
            retry_times = [row.retry_at for row in result.queries if row.retry_at is not None]
            should_defer = result.stop_reason == "DEFERRED" and bool(retry_times)
            if should_defer and result.attempted_calls == 0:
                return {"status": "DEFERRED", "retry_at": max(retry_times)}
            records, omitted = _bounded_records([_jsonable(record) for record in result.records])
            query_results = [{
                "query_fingerprint": row.query_fingerprint,
                "pages": row.pages,
                "accepted": row.accepted,
                "rejected": row.rejected,
                "reason": row.reason,
                "retry_at": _jsonable(row.retry_at),
            } for row in result.queries]
            context.checkpoint({"pages": sum(row.pages for row in result.queries),
                                "records": len(records), "status": result.status})
            return {
                "status": "PARTIAL" if omitted or should_defer else result.status,
                "data_mode": result.data_mode or collector.gateway.data_mode,
                "records": records,
                "record_count": len(records),
                **({"error_code": "RATE_LIMITED"} if should_defer else {}),
                "coverage": {"attempted_calls": result.attempted_calls,
                             "complete": result.complete,
                             "records_omitted_storage_limit": omitted,
                             "queries": query_results,
                             "stop_reason": result.stop_reason,
                             "retry_at": _jsonable(max(retry_times)) if should_defer else None},
            }

        return collect

    async def _wikimedia_handler(self, context: SourceWorkContext):
        context.heartbeat()
        date_value = self.observation_date()
        request = WikimediaRequest(date_value, context.plan_fingerprint)
        attempt_id = hashlib.sha256(
            f"{context.job_id}:{WIKIMEDIA_SOURCE_ID}:{date_value.isoformat()}:page-1".encode()
        ).hexdigest()
        try:
            page = await self.wikimedia.fetch_day(
                request=request,
                job_id=context.job_id,
                owner=context.owner,
                fence=context.fence,
                attempt_id=attempt_id,
                timeout_seconds=min(15, self.wikimedia.runner.admission.max_attempt_seconds(
                    WIKIMEDIA_SOURCE_ID)),
            )
        except WikimediaGatewayError as exc:
            context.checkpoint({"status": "UNAVAILABLE", "error_code": exc.code})
            status = "PARTIAL" if exc.code in {"RATE_LIMITED", "DAY_UNAVAILABLE"} else "FAILED"
            return {"status": status, "data_mode": self.wikimedia.data_mode,
                    "records": [], "record_count": 0, "error_code": exc.code}
        records, omitted = _bounded_records([{
            "source": row.source,
            "country_scope": row.country_scope,
            "access": row.access,
            "observation_date": _jsonable(row.observation_date),
            "project": row.project,
            "article_original": row.article_original,
            "article_display": row.article_display,
            "views_ceil": row.views_ceil,
            "provider_rank": row.provider_rank,
            "quality_flags": list(row.quality_flags),
            "canonical_keyword": row.canonical_keyword,
            "fetched_at": _jsonable(row.fetched_at),
            "source_url": row.source_url,
        } for row in page.observations])
        context.checkpoint({"observation_date": page.observation_date.isoformat(),
                            "records": len(records), "rejected": page.rejected_count})
        return {"status": "PARTIAL" if page.rejected_count or omitted else "SUCCESS",
                "data_mode": page.data_mode, "records": records,
                "record_count": len(records),
                "coverage": {"observation_date": page.observation_date.isoformat(),
                             "rejected": page.rejected_count,
                             "records_omitted_storage_limit": omitted,
                             "item_count": page.item_count}}
