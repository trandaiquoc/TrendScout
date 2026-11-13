"""Server-owned adapters from the four guarded sources into fetch-worker results.

This module creates no transports or credentials. The caller must supply already
constructed guarded gateways; live constructors remain approval-gated.
"""

import asyncio
import hashlib
import json
import math
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Mapping

from app.jobs.distributed_worker import SourceWorkContext
from app.jobs.public_admission import AdmissionDenied
from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
    source_capability,
)
from app.sources.guarded_collector import CollectionLimits, GuardedQueryCollector
from app.sources.live_gateway import GatewayError
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
                 observation_date: Callable[[], date] | None = None,
                 shared_cache: SharedCache | None = None,
                 resolve_cache_grant: Callable[[str, datetime], CacheGrant | None] | None = None,
                 max_wikimedia_date_attempts: int = 1,
                 include_wikimedia_transport_metrics: bool = False,
                 unavailable_mode: str | None = None,
                 utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if (set(query_collectors) != _QUERY_SOURCES
                or unavailable_mode not in {None, "LIVE", "SYNTHETIC"}
                or any((value is None and unavailable_mode is None)
                       or (value is not None and (not isinstance(value, GuardedQueryCollector)
                           or value.gateway.source_id != source))
                       for source, value in query_collectors.items())
                or (wikimedia is None and unavailable_mode is None)
                or (wikimedia is not None and not isinstance(wikimedia, WikimediaTopPageGateway))
                or not isinstance(plan, QueryPlan) or not isinstance(limits, CollectionLimits)
                or (observation_date is not None and not callable(observation_date))
                or (shared_cache is not None and not isinstance(shared_cache, SharedCache))
                or ((shared_cache is None) != (resolve_cache_grant is None))
                or (resolve_cache_grant is not None and not callable(resolve_cache_grant))
                or type(max_wikimedia_date_attempts) is not int
                or not 1 <= max_wikimedia_date_attempts <= 3
                or type(include_wikimedia_transport_metrics) is not bool
                or not callable(utcnow)):
            raise ValueError("fixed four-source collectors, plan and limits required")
        self.query_collectors = dict(query_collectors)
        self.wikimedia = wikimedia
        self.plan = plan
        self.limits = limits
        self.observation_date = observation_date or (lambda: datetime.now(timezone.utc).date())
        self.shared_cache = shared_cache
        self.resolve_cache_grant = resolve_cache_grant
        self.max_wikimedia_date_attempts = max_wikimedia_date_attempts
        self.include_wikimedia_transport_metrics = include_wikimedia_transport_metrics
        self.unavailable_mode = unavailable_mode
        self.utcnow = utcnow

    def build(self) -> dict[str, Callable[[SourceWorkContext], object]]:
        handlers = {source: (self._query_handler(source) if self.query_collectors[source] is not None
                             else self._unavailable_handler()) for source in sorted(_QUERY_SOURCES)}
        async def collect_wikimedia(context):
            observation_date = self.observation_date()
            return await self._with_cache(
                WIKIMEDIA_SOURCE_ID, context,
                lambda: self._wikimedia_handler(context, observation_date),
                query_scope=f"{self.plan.fingerprint}:{observation_date.isoformat()}",
            )

        if self.wikimedia is None:
            handlers[WIKIMEDIA_SOURCE_ID] = self._unavailable_handler()
        else:
            setattr(collect_wikimedia, "__trendscout_data_mode__", self.wikimedia.data_mode)
            handlers[WIKIMEDIA_SOURCE_ID] = collect_wikimedia
        if set(handlers) != _ALL_SOURCES:
            raise ValueError("incomplete guarded source registry")
        return handlers

    def _unavailable_handler(self):
        async def unavailable(_context):
            return {"status": "UNAVAILABLE", "data_mode": self.unavailable_mode,
                    "record_count": 0, "records": [], "error_code": "SOURCE_NOT_APPROVED"}
        setattr(unavailable, "__trendscout_data_mode__", self.unavailable_mode)
        return unavailable

    def _query_handler(self, source_id):
        collector = self.query_collectors[source_id]

        async def collect(context: SourceWorkContext):
            return await self._with_cache(
                source_id, context, lambda: collect_from_provider(context)
            )

        async def collect_from_provider(context: SourceWorkContext):
            context.heartbeat()
            saved_progress = (context.resume_snapshot or {}).get("progress", {})
            resume_snapshot = (saved_progress.get("collector")
                               if isinstance(saved_progress, dict) else None)
            result = await collector.collect(
                plan=self.plan,
                limits=self.limits,
                job_id=context.job_id,
                owner=context.owner,
                fence=context.fence,
                heartbeat=context.heartbeat,
                checkpoint=lambda snapshot: context.checkpoint({"collector": snapshot}),
                resume_snapshot=resume_snapshot,
            )
            retry_times = [row.retry_at for row in result.queries if row.retry_at is not None]
            should_defer = result.stop_reason == "DEFERRED" and bool(retry_times)
            if should_defer and result.attempted_calls == 0:
                return {"status": "DEFERRED", "retry_at": max(retry_times)}
            records, omitted = _bounded_records([_jsonable(record) for record in result.records])
            # SerpApi may return a cached provider response even on a network call.
            # Preserve that fact at the source level and keep per-query evidence.
            cached = any(
                evidence.get("cache_state") == "CACHED"
                for record in records for evidence in record.get("evidence", [])
                if isinstance(evidence, dict)
            )
            query_results = [{
                "query_fingerprint": row.query_fingerprint,
                "pages": row.pages,
                "accepted": row.accepted,
                "rejected": row.rejected,
                "reason": row.reason,
                "retry_at": _jsonable(row.retry_at),
            } for row in result.queries]
            return {
                "status": "PARTIAL" if omitted or result.checkpoint_records_omitted or should_defer
                          else result.status,
                "data_mode": result.data_mode or collector.gateway.data_mode,
                "records": records,
                "record_count": len(records),
                "delivery_state": "CACHED" if cached else "FRESH",
                **({"error_code": "RATE_LIMITED"} if should_defer else {}),
                "coverage": {"attempted_calls": result.attempted_calls,
                             "complete": result.complete,
                             "records_omitted_storage_limit": (
                                 omitted + result.checkpoint_records_omitted),
                             "queries": query_results,
                             "stop_reason": result.stop_reason,
                             "retry_at": _jsonable(max(retry_times)) if should_defer else None},
            }

        setattr(collect, "__trendscout_data_mode__", collector.gateway.data_mode)
        return collect

    async def _with_cache(self, source_id, context, collect, *, query_scope=None):
        if self.shared_cache is None:
            return await collect()
        now = self.utcnow()
        try:
            grant = self.resolve_cache_grant(source_id, now)
        except Exception:
            raise CacheUnavailable() from None
        scope = None
        if isinstance(grant, CacheGrant):
            capability = source_capability(source_id)
            scope = CacheScope(
                source_id=source_id,
                provider_id=capability.provider_id,
                query=query_scope or self.plan.fingerprint,
                plan_fingerprint=self.plan.fingerprint,
                market=self.plan.market,
                language=self.plan.language or "all",
                window=self.plan.time_filter_rule,
                parser_version=f"source-v{capability.capability_version}",
                policy_version=grant.policy_version,
            )
            cached = self.shared_cache.get(scope, grant=grant, now=now)
            if cached.get("status") == "CACHED" and cached.get("origin") in {"SYNTHETIC", "LIVE"}:
                payload = cached.get("payload")
                if (not isinstance(payload, dict)
                        or payload.get("data_mode") != cached.get("origin")
                        or payload.get("delivery_state", "FRESH") not in {"FRESH", "CACHED"}):
                    raise CacheUnavailable()
                return {**payload, "delivery_state": "CACHED"}

        result = await collect()
        if not isinstance(result, dict):
            return result
        result.setdefault("delivery_state", "FRESH")
        if (scope is not None and grant.allowed is True
                and result.get("status") in {"SUCCESS", "PARTIAL"}
                and result.get("record_count", 0) > 0
                and result.get("data_mode") in {"SYNTHETIC", "LIVE"}):
            fetched_at = self.utcnow()
            try:
                self.shared_cache.put(
                    scope, grant=grant, origin=result["data_mode"], payload=result,
                    fetched_at=fetched_at, now=self.utcnow(),
                )
            except Exception:
                flags = result.setdefault("quality_flags", [])
                if "CACHE_WRITE_FAILED" not in flags:
                    flags.append("CACHE_WRITE_FAILED")
        return result

    async def _wikimedia_handler(self, context: SourceWorkContext, observation_date=None):
        context.heartbeat()
        date_value = observation_date or self.observation_date()

        def result_for(page):
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
            coverage = {"observation_date": page.observation_date.isoformat(),
                        "rejected": page.rejected_count,
                        "records_omitted_storage_limit": omitted,
                        "item_count": page.item_count}
            if self.include_wikimedia_transport_metrics:
                coverage.update(http_status=page.http_status, response_bytes=page.response_bytes)
            return {"status": "PARTIAL" if page.rejected_count or omitted else "SUCCESS",
                    "data_mode": page.data_mode, "records": records,
                    "record_count": len(records),
                    "coverage": coverage}

        saved_progress = (context.resume_snapshot or {}).get("progress", {})
        saved_receipt = (saved_progress.get("wikimedia_receipt")
                         if isinstance(saved_progress, dict) else None)
        if saved_receipt is not None:
            try:
                saved_date = date.fromisoformat(saved_receipt["observation_date"])
            except (KeyError, TypeError, ValueError):
                raise AdmissionDenied("CHECKPOINT_INVALID") from None
            if saved_date not in {date_value - timedelta(days=i)
                                  for i in range(self.max_wikimedia_date_attempts)}:
                raise AdmissionDenied("CHECKPOINT_INVALID")
            attempt_id = hashlib.sha256(
                f"{context.job_id}:{WIKIMEDIA_SOURCE_ID}:{saved_date.isoformat()}:page-1".encode()
            ).hexdigest()
            result = saved_receipt.get("result") if isinstance(saved_receipt, dict) else None
            if (not isinstance(saved_receipt, dict)
                    or saved_receipt.get("schema") != 1
                    or saved_receipt.get("source_id") != WIKIMEDIA_SOURCE_ID
                    or saved_receipt.get("plan_fingerprint") != context.plan_fingerprint
                    or saved_receipt.get("observation_date") != saved_date.isoformat()
                    or saved_receipt.get("attempt_id") != attempt_id
                    or saved_receipt.get("data_mode") != self.wikimedia.data_mode
                    or not isinstance(result, dict)
                    or result.get("data_mode") != self.wikimedia.data_mode
                    or result.get("status") not in {"SUCCESS", "PARTIAL"}
                    or not isinstance(result.get("records"), list)
                    or type(result.get("record_count")) is not int
                    or result["record_count"] != len(result["records"])):
                raise AdmissionDenied("CHECKPOINT_INVALID")
            try:
                settle_recovered = getattr(
                    self.wikimedia.runner, "settle_recovered_attempt", None
                )
                if callable(settle_recovered):
                    await asyncio.to_thread(
                        settle_recovered, context.job_id, context.owner, context.fence,
                        attempt_id=attempt_id, now=self.utcnow(),
                    )
                else:
                    await asyncio.to_thread(
                        self.wikimedia.runner.admission.settle_attempt,
                        context.job_id, context.owner, context.fence,
                        attempt_id=attempt_id, now=self.utcnow(),
                    )
            except Exception:
                raise AdmissionDenied("SETTLEMENT_PENDING") from None
            return result

        # AQS may lag the latest complete UTC day. Try at most three dates,
        # treating 404 as unavailable (never as zero), and stop on any other
        # result. The gateway reserves every date independently before GET.
        last_error = None
        for fallback_days in range(self.max_wikimedia_date_attempts):
            request_date = date_value - timedelta(days=fallback_days)
            request = WikimediaRequest(request_date, context.plan_fingerprint)
            attempt_id = hashlib.sha256(
                f"{context.job_id}:{WIKIMEDIA_SOURCE_ID}:{request_date.isoformat()}:page-1".encode()
            ).hexdigest()

            def persist_page_receipt(page):
                result = result_for(page)
                context.checkpoint({"wikimedia_receipt": {
                    "schema": 1,
                    "source_id": WIKIMEDIA_SOURCE_ID,
                    "plan_fingerprint": context.plan_fingerprint,
                    "observation_date": request_date.isoformat(),
                    "attempt_id": attempt_id,
                    "data_mode": page.data_mode,
                    "result": result,
                }})

            try:
                page = await self.wikimedia.fetch_day(
                    request=request,
                    job_id=context.job_id,
                    owner=context.owner,
                    fence=context.fence,
                    attempt_id=attempt_id,
                    timeout_seconds=min(15, self.wikimedia.runner.admission.max_attempt_seconds(
                        WIKIMEDIA_SOURCE_ID)),
                    on_page_receipt=persist_page_receipt,
                )
            except (WikimediaGatewayError, GatewayError) as exc:
                if (exc.code == "DAY_UNAVAILABLE"
                        and fallback_days < self.max_wikimedia_date_attempts - 1):
                    last_error = exc
                    continue
                last_error = exc
                break
            result = result_for(page)
            context.checkpoint({"observation_date": page.observation_date.isoformat(),
                                "records": result["record_count"],
                                "rejected": page.rejected_count})
            return result

        exc = last_error
        if exc is None:
            return {"status": "FAILED", "data_mode": self.wikimedia.data_mode,
                    "records": [], "record_count": 0, "error_code": "SOURCE_EXECUTION_FAILED"}
        if exc.code in {"RESULT_PERSISTENCE_FAILED", "SETTLEMENT_PENDING"}:
            raise AdmissionDenied(exc.code) from None
        context.checkpoint({"status": "UNAVAILABLE", "error_code": exc.code})
        status = "PARTIAL" if exc.code in {"RATE_LIMITED", "DAY_UNAVAILABLE"} else "FAILED"
        result = {"status": status, "data_mode": self.wikimedia.data_mode,
                  "records": [], "record_count": 0, "error_code": exc.code}
        if exc.code == "RATE_LIMITED" and exc.retry_after_unbounded_seconds is not None:
            result["retry_after_seconds"] = min(
                31_536_000, max(1, math.ceil(exc.retry_after_unbounded_seconds)))
        diagnostic = getattr(exc, "diagnostic", None)
        if diagnostic is not None:
            result["transport_diagnostic"] = {
                "source_id": diagnostic.source_id,
                "failure_phase": diagnostic.failure_phase,
                "http_status": diagnostic.http_status,
                "exception_class": diagnostic.exception_class,
                "bytes_received": diagnostic.bytes_received,
                "response_size_limit": diagnostic.response_size_limit,
                "attempt_id": diagnostic.attempt_id,
                "error_code": diagnostic.error_code,
            }
        return result


class CacheUnavailable(Exception):
    code = "CACHE_UNAVAILABLE"
