"""Server-owned adapters from the four guarded sources into fetch-worker results.

This module creates no transports or credentials. The caller must supply already
constructed guarded gateways; live constructors remain approval-gated.
"""

import asyncio
import hashlib
import json
import math
import re
from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Mapping

from app.jobs.distributed_worker import SourceWorkContext
from app.jobs.public_admission import OWNER_FRESH_ACCEPTANCE_MARKER, AdmissionDenied
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
from app.sources.youtube.query_planner import YOUTUBE_QUERY_PLANNER_VERSION

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


def _bounded_records(records, *, byte_budget=_NORMALIZED_RESULT_BUDGET_BYTES):
    """Keep normalized rows within checkpoint budget and report every omission."""
    selected, used, omitted = [], 2, 0
    for record in records:
        encoded = json.dumps(record, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
        addition = len(encoded) + (1 if selected else 0)
        if byte_budget is not None and used + addition > byte_budget:
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
                 google_trends_handler: Callable | None = None,
                 bing_search_handler: Callable | None = None,
                 observation_date: Callable[[], date] | None = None,
                 shared_cache: SharedCache | None = None,
                 resolve_cache_grant: Callable[[str, datetime], CacheGrant | None] | None = None,
                 source_limits: Mapping[str, CollectionLimits] | None = None,
                 resolve_provider_batch: Callable[[str, datetime], dict | None] | None = None,
                 resolve_query_scopes: Callable[[str, datetime], tuple[str, ...] | None] | None = None,
                 max_wikimedia_date_attempts: int = 1,
                 include_wikimedia_transport_metrics: bool = False,
                 unavailable_mode: str | None = None,
                 youtube_query_mode: str = "NORMAL_DEMO",
                 force_youtube_refresh_once: bool = False,
                 utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if (set(query_collectors) != _QUERY_SOURCES
                or unavailable_mode not in {None, "LIVE", "SYNTHETIC"}
                or any((value is None and unavailable_mode is None)
                       or (value is not None and (not isinstance(value, GuardedQueryCollector)
                           or value.gateway.source_id != source))
                       for source, value in query_collectors.items())
                or (wikimedia is None and unavailable_mode is None)
                or (wikimedia is not None and not isinstance(wikimedia, WikimediaTopPageGateway))
                or (google_trends_handler is not None and (
                    not callable(google_trends_handler)
                    or not callable(getattr(google_trends_handler, "__call_mode__", None))
                ))
                or (bing_search_handler is not None and (
                    not callable(bing_search_handler)
                    or not callable(getattr(bing_search_handler, "__call_mode__", None))
                ))
                or not isinstance(plan, QueryPlan) or not isinstance(limits, CollectionLimits)
                or (observation_date is not None and not callable(observation_date))
                or (shared_cache is not None and not isinstance(shared_cache, SharedCache))
                or ((shared_cache is None) != (resolve_cache_grant is None))
                or (resolve_cache_grant is not None and not callable(resolve_cache_grant))
                or (source_limits is not None and (
                    not isinstance(source_limits, Mapping)
                    or not set(source_limits) <= _QUERY_SOURCES
                    or any(not isinstance(value, CollectionLimits)
                           for value in source_limits.values())
                ))
                or (resolve_provider_batch is not None
                    and not callable(resolve_provider_batch))
                or (resolve_query_scopes is not None and not callable(resolve_query_scopes))
                or type(max_wikimedia_date_attempts) is not int
                or not 1 <= max_wikimedia_date_attempts <= 3
                or type(include_wikimedia_transport_metrics) is not bool
                or youtube_query_mode not in {"NORMAL_DEMO", "OWNER_MANUAL_LIVE"}
                or type(force_youtube_refresh_once) is not bool
                or force_youtube_refresh_once and youtube_query_mode != "OWNER_MANUAL_LIVE"
                or not callable(utcnow)):
            raise ValueError("fixed four-source collectors, plan and limits required")
        self.query_collectors = dict(query_collectors)
        self.wikimedia = wikimedia
        self.google_trends_handler = google_trends_handler
        self.bing_search_handler = bing_search_handler
        self.plan = plan
        self.limits = limits
        self.observation_date = observation_date or (lambda: datetime.now(timezone.utc).date())
        self.shared_cache = shared_cache
        self.resolve_cache_grant = resolve_cache_grant
        self.source_limits = dict(source_limits or {})
        self.resolve_provider_batch = resolve_provider_batch
        self.resolve_query_scopes = resolve_query_scopes
        self.max_wikimedia_date_attempts = max_wikimedia_date_attempts
        self.include_wikimedia_transport_metrics = include_wikimedia_transport_metrics
        self.unavailable_mode = unavailable_mode
        self.youtube_query_mode = youtube_query_mode
        self._youtube_force_fresh_once = force_youtube_refresh_once
        self.utcnow = utcnow

    def build(self) -> dict[str, Callable[[SourceWorkContext], object]]:
        handlers = {}
        for source in sorted(_QUERY_SOURCES):
            if source == GOOGLE_SOURCE_ID and self.google_trends_handler is not None:
                async def collect_google(context, handler=self.google_trends_handler):
                    return await handler(context)
                mode = self.google_trends_handler.__call_mode__()
                setattr(collect_google, "__trendscout_data_mode__", mode)
                handlers[source] = collect_google
            elif source == BING_SOURCE_ID and self.bing_search_handler is not None:
                async def collect_bing(context, handler=self.bing_search_handler):
                    return await handler(context)
                mode = self.bing_search_handler.__call_mode__()
                setattr(collect_bing, "__trendscout_data_mode__", mode)
                handlers[source] = collect_bing
            else:
                if self.query_collectors[source] is not None:
                    handlers[source] = self._query_handler(source)
                elif source == YOUTUBE_SOURCE_ID and self.shared_cache is not None:
                    handlers[source] = self._youtube_cache_only_handler()
                else:
                    handlers[source] = self._unavailable_handler()
        async def collect_wikimedia(context):
            observation_date = self.observation_date()
            history_days = context.request_metadata.get("history_days")
            if context.request_metadata.get("kind") == "WIKIMEDIA_HISTORY":
                return await self._wikimedia_history_handler(context, observation_date, history_days)
            return await self._wikimedia_handler(context, observation_date)

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
        limits = self.source_limits.get(source_id, self.limits)

        async def collect(context: SourceWorkContext):
            return await self._with_cache(
                source_id, context, lambda: collect_from_provider(context)
            )

        async def collect_from_provider(context: SourceWorkContext):
            context.heartbeat()
            saved_progress = (context.resume_snapshot or {}).get("progress", {})
            resume_snapshot = (saved_progress.get("collector")
                               if isinstance(saved_progress, dict) else None)
            query_scopes = (self.resolve_query_scopes(source_id, self.utcnow())
                            if source_id == YOUTUBE_SOURCE_ID
                            and self.resolve_query_scopes is not None else None)
            result = await collector.collect(
                plan=self.plan,
                limits=limits,
                job_id=context.job_id,
                owner=context.owner,
                fence=context.fence,
                heartbeat=context.heartbeat,
                checkpoint=lambda snapshot: context.checkpoint({"collector": snapshot}),
                resume_snapshot=resume_snapshot,
                query_scopes=query_scopes,
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
            query_results = []
            selected_scopes = query_scopes or tuple(seed.scope for seed in self.plan.seeds)
            seeds_by_scope = {seed.scope: seed for seed in self.plan.seeds}
            for index, row in enumerate(result.queries):
                query_result = {
                    "query_fingerprint": row.query_fingerprint,
                    "pages": row.pages,
                    "accepted": row.accepted,
                    "rejected": row.rejected,
                    "reason": row.reason,
                    "retry_at": _jsonable(row.retry_at),
                }
                if source_id == YOUTUBE_SOURCE_ID:
                    scope = selected_scopes[index] if index < len(selected_scopes) else None
                    seed = seeds_by_scope.get(scope)
                    query_result.update({
                        "original_query": seed.query if seed else None,
                        "query_source": ("CURATED_MANUAL_ROTATION"
                                         if self.youtube_query_mode == "OWNER_MANUAL_LIVE"
                                         else "CURATED_SLOT_ROTATION"),
                        "taxonomy_scope": scope,
                        "market_filter": self.plan.market,
                        "language_filter": self.plan.language,
                        "request_timestamps": [
                            _jsonable(value) for value in row.request_timestamps
                        ],
                        "next_page_available": row.reason == "PAGE_CAP_REACHED",
                        "next_page_token_saved": row.reason == "PAGE_CAP_REACHED",
                    })
                query_results.append(query_result)
            youtube_fetch_times = [record.fetched_at for record in result.records
                                   if source_id == YOUTUBE_SOURCE_ID
                                   and isinstance(getattr(record, "fetched_at", None), datetime)
                                   and record.fetched_at.tzinfo is not None
                                   and record.fetched_at.utcoffset() is not None]
            youtube_request_times = [timestamp for query in query_results
                                     for timestamp in query.get("request_timestamps", [])]
            youtube_coverage = ({
                "provider_method": "search.list",
                "provider_id": "youtube_data_api_v3",
                "provider_request_count": result.attempted_calls,
                "query_planner_version": YOUTUBE_QUERY_PLANNER_VERSION,
                "query_plan_id": self.plan.plan_id,
                "query_plan_version": self.plan.version,
                "query_plan_fingerprint": self.plan.fingerprint,
                "market_filter": self.plan.market,
                "language_filter": self.plan.language,
                "batch_requested_at": min(youtube_request_times)
                    if youtube_request_times else None,
                "source_fetched_at": _jsonable(max(youtube_fetch_times))
                    if youtube_fetch_times else None,
            } if source_id == YOUTUBE_SOURCE_ID else {})
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
                             "query_scopes": list(query_scopes or ()),
                             "queries": query_results,
                             "stop_reason": result.stop_reason,
                             **youtube_coverage,
                             "retry_at": _jsonable(max(retry_times)) if should_defer else None},
            }

        setattr(collect, "__trendscout_data_mode__", collector.gateway.data_mode)
        return collect

    def _youtube_cache_only_handler(self):
        async def no_live_adapter(_context):
            return {"status": "UNAVAILABLE", "data_mode": "LIVE", "record_count": 0,
                    "records": [], "error_code": "SOURCE_NOT_APPROVED"}

        async def cached_only(context):
            return await self._with_cache(YOUTUBE_SOURCE_ID, context, lambda: no_live_adapter(context))

        setattr(cached_only, "__trendscout_data_mode__", "LIVE")
        return cached_only

    async def _with_cache(self, source_id, context, collect, *, query_scope=None,
                          skip_cache_read=False, cache_fetched_at=None):
        if (type(skip_cache_read) is not bool
                or cache_fetched_at is not None
                and (not isinstance(cache_fetched_at, datetime)
                     or cache_fetched_at.tzinfo is None
                     or cache_fetched_at.utcoffset() is None)):
            raise ValueError("valid cache recovery policy required")
        acceptance_fresh = (
            isinstance(getattr(context, "request_metadata", None), dict)
            and context.request_metadata.get("owner_fresh_acceptance")
            == OWNER_FRESH_ACCEPTANCE_MARKER
        )
        owner_manual_fresh = (source_id == YOUTUBE_SOURCE_ID
                              and self._youtube_force_fresh_once)
        force_fresh = acceptance_fresh or owner_manual_fresh
        if owner_manual_fresh:
            # This local-launcher one-shot is consumed by the next user-triggered
            # YouTube handler, even when admission later rejects the attempt.
            self._youtube_force_fresh_once = False
        if self.shared_cache is None:
            result = await collect()
            if owner_manual_fresh:
                result = self._mark_owner_fresh_test(result)
            return self._mark_owner_acceptance_fresh(result) if acceptance_fresh else result
        now = self.utcnow()
        if cache_fetched_at is not None and cache_fetched_at > now:
            raise ValueError("cache recovery timestamp cannot be in the future")
        batch_decision = None
        stale_fallback = None
        cache_lookup = None
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
            if not force_fresh and not skip_cache_read:
                cached = self.shared_cache.get(scope, grant=grant, now=now)
                cache_lookup = cached
                cached_status = cached.get("status")
                if (cached_status in {"CACHED", "STALE"}
                        and cached.get("origin") in {"SYNTHETIC", "LIVE"}):
                    payload = cached.get("payload")
                    if (not isinstance(payload, dict)
                            or payload.get("data_mode") != cached.get("origin")
                            or payload.get("delivery_state", "FRESH")
                            not in {"FRESH", "CACHED", "STALE_CACHE"}):
                        raise CacheUnavailable()
                    if cached_status == "CACHED":
                        return self._cache_delivery(
                            payload, "CACHED", now, cache_fetched_at=cached.get("fetched_at"),
                        )
                    if source_id == YOUTUBE_SOURCE_ID and self.resolve_provider_batch is not None:
                        try:
                            batch_decision = self.resolve_provider_batch(source_id, now)
                        except Exception:
                            batch_decision = {"allowed": False,
                                              "error_code": "REFRESH_POLICY_UNAVAILABLE"}
                        if (not isinstance(batch_decision, dict)
                                or batch_decision.get("allowed") is not True):
                            return self._cache_delivery(
                                payload, "STALE_CACHE", now,
                                cache_fetched_at=cached.get("fetched_at"),
                                next_eligible_at=(batch_decision.get("next_eligible_at")
                                                  if isinstance(batch_decision, dict) else None),
                                refresh_eligible=False,
                            )
                    else:
                        return self._cache_delivery(
                            payload, "STALE_CACHE", now,
                            cache_fetched_at=cached.get("fetched_at"),
                        )
                    stale_fallback = (payload, cached.get("fetched_at"))

        def provider_batch_decision():
            if acceptance_fresh:
                return self.resolve_provider_batch(
                    source_id, now, owner_fresh_acceptance=True,
                    acceptance_job_id=context.job_id,
                )
            return self.resolve_provider_batch(source_id, now)

        if (source_id == YOUTUBE_SOURCE_ID and self.resolve_provider_batch is not None
                and batch_decision is None):
            try:
                decision = provider_batch_decision()
            except Exception:
                decision = {"allowed": False, "error_code": "REFRESH_POLICY_UNAVAILABLE"}
            if not isinstance(decision, dict) or decision.get("allowed") is not True:
                cache_reason = (cache_lookup.get("reason")
                                if isinstance(cache_lookup, dict) else None)
                cache_error = {
                    "CACHE_MISS": "NO_CACHE_AVAILABLE",
                    "RETENTION_EXPIRED": "CACHE_EXPIRED",
                    "STALE_WINDOW_EXPIRED": "CACHE_EXPIRED",
                    "INVALID_CACHE_RECORD": "CACHE_INVALID",
                    "INVALID_CACHE_TIME": "CACHE_INVALID",
                    "POLICY_UNAVAILABLE": "CACHE_POLICY_UNAVAILABLE",
                    "POLICY_REVOKED": "CACHE_POLICY_REVOKED",
                }.get(cache_reason)
                error_code = decision.get("error_code") if isinstance(decision, dict) else None
                if error_code == "NOT_YET_ELIGIBLE" and cache_error:
                    error_code = cache_error
                cache_coverage = {
                    "refresh_eligible": False,
                    "provider_request_count": 0,
                    "cache_status": cache_lookup.get("status")
                        if isinstance(cache_lookup, dict) else "UNAVAILABLE",
                    "cache_reason": cache_reason or "CACHE_LOOKUP_UNAVAILABLE",
                }
                provider_request_count = 0
                next_eligible_at = (decision.get("next_eligible_at")
                                    if isinstance(decision, dict) else None)
                if isinstance(next_eligible_at, str):
                    cache_coverage["next_eligible_at"] = next_eligible_at
                return {
                    "status": "UNAVAILABLE", "data_mode": "LIVE", "records": [],
                    "record_count": 0,
                    "error_code": error_code or "NOT_YET_ELIGIBLE",
                    "delivery_state": "NOT_ELIGIBLE",
                    "provider_request_count": provider_request_count,
                    "coverage": {**cache_coverage,
                                  **({"owner_manual_fresh_test": True}
                                     if owner_manual_fresh else {}),
                                  **({"owner_fresh_acceptance": True}
                                     if acceptance_fresh else {})},
                }

        try:
            result = await collect()
        except Exception as exc:
            if stale_fallback is None:
                raise
            error_code = getattr(exc, "code", "SOURCE_REFRESH_FAILED")
            if not isinstance(error_code, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", error_code):
                error_code = "SOURCE_REFRESH_FAILED"
            result = {
                "status": "FAILED", "data_mode": "LIVE", "records": [],
                "record_count": 0, "error_code": error_code,
            }
        if not isinstance(result, dict):
            return result
        if owner_manual_fresh:
            result = self._mark_owner_fresh_test(result)
        if acceptance_fresh:
            result = self._mark_owner_acceptance_fresh(result)
        if (stale_fallback is not None
                and result.get("status") in {"FAILED", "UNAVAILABLE", "PARTIAL"}
                and not result.get("records")):
            fallback_payload, cached_fetched_at = stale_fallback
            fallback = self._cache_delivery(
                fallback_payload, "STALE_CACHE", now,
                cache_fetched_at=cached_fetched_at,
            )
            error_code = result.get("error_code")
            if not isinstance(error_code, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", error_code):
                error_code = "SOURCE_REFRESH_FAILED"
            fallback["status"] = "PARTIAL"
            fallback["error_code"] = error_code
            fallback["coverage"].update({
                "refresh_attempt_status": result.get("status"),
                "refresh_error_code": error_code,
                "refresh_attempted_at": now.isoformat(),
                "provider_request_count": self._provider_request_count(result),
            })
            request_count = self._provider_request_count(result)
            if type(request_count) is int:
                fallback["provider_request_count"] = request_count
            else:
                fallback.pop("provider_request_count", None)
            return fallback
        result.setdefault("delivery_state", "FRESH")
        request_count = self._provider_request_count(result)
        if request_count is not None:
            result["provider_request_count"] = request_count
            coverage = result.get("coverage")
            coverage = dict(coverage) if isinstance(coverage, dict) else {}
            coverage["provider_request_count"] = request_count
            result["coverage"] = coverage
        negative_day_cache = (result.get("status") == "UNAVAILABLE"
                              and result.get("error_code") == "DAY_UNAVAILABLE")
        cache_grant = grant
        if negative_day_cache and isinstance(grant, CacheGrant):
            short_ttl = min(6 * 60 * 60, grant.max_retention_seconds)
            cache_grant = replace(grant,
                max_fresh_seconds=min(grant.max_fresh_seconds, short_ttl),
                max_stale_seconds=min(grant.max_stale_seconds, short_ttl),
                max_retention_seconds=short_ttl)
        if (scope is not None and cache_grant.allowed is True
                and (result.get("status") in {"SUCCESS", "PARTIAL"}
                     and result.get("record_count", 0) > 0 or negative_day_cache)
                and result.get("data_mode") in {"SYNTHETIC", "LIVE"}):
            fetched_at = cache_fetched_at or self.utcnow()
            try:
                self.shared_cache.put(
                    scope, grant=cache_grant, origin=result["data_mode"], payload=result,
                    fetched_at=fetched_at, now=self.utcnow(),
                )
            except Exception:
                flags = result.setdefault("quality_flags", [])
                if "CACHE_WRITE_FAILED" not in flags:
                    flags.append("CACHE_WRITE_FAILED")
        return result

    @staticmethod
    def _mark_owner_fresh_test(result):
        if not isinstance(result, dict):
            return result
        coverage = result.get("coverage")
        coverage = dict(coverage) if isinstance(coverage, dict) else {}
        coverage["owner_manual_fresh_test"] = True
        return {**result, "coverage": coverage}

    @staticmethod
    def _mark_owner_acceptance_fresh(result):
        if not isinstance(result, dict):
            return result
        coverage = result.get("coverage")
        coverage = dict(coverage) if isinstance(coverage, dict) else {}
        coverage["owner_fresh_acceptance"] = True
        return {**result, "coverage": coverage}

    @staticmethod
    def _cache_delivery(payload, delivery_state, delivered_at, *, cache_fetched_at=None,
                        next_eligible_at=None, refresh_eligible=None):
        """Return cached evidence with its original observation time and this delivery time."""
        coverage = payload.get("coverage")
        coverage = dict(coverage) if isinstance(coverage, dict) else {}
        coverage.pop("owner_fresh_acceptance", None)
        originating_count = coverage.get("provider_request_count")
        if type(originating_count) is not int or originating_count < 0:
            originating_count = payload.get("provider_request_count")
        if type(originating_count) is int and originating_count >= 0:
            coverage["cached_provider_request_count"] = originating_count
        coverage["provider_request_count"] = 0
        coverage["cache_delivery_at"] = delivered_at.astimezone(timezone.utc).isoformat()
        if isinstance(cache_fetched_at, str):
            coverage["cache_fetched_at"] = cache_fetched_at
        if isinstance(next_eligible_at, str):
            coverage["next_eligible_at"] = next_eligible_at
        if type(refresh_eligible) is bool:
            coverage["refresh_eligible"] = refresh_eligible
        coverage["cache_freshness"] = delivery_state
        return {**payload, "delivery_state": delivery_state, "coverage": coverage,
                "provider_request_count": 0}

    @staticmethod
    def _provider_request_count(result):
        coverage = result.get("coverage") if isinstance(result, dict) else None
        for value in (
            coverage.get("provider_request_count") if isinstance(coverage, dict) else None,
            coverage.get("attempted_calls") if isinstance(coverage, dict) else None,
            result.get("provider_request_count") if isinstance(result, dict) else None,
        ):
            if type(value) is int and value >= 0:
                return value
        return None

    async def _wikimedia_handler(self, context: SourceWorkContext, observation_date=None):
        context.heartbeat()
        acceptance_fresh = (
            isinstance(getattr(context, "request_metadata", None), dict)
            and context.request_metadata.get("owner_fresh_acceptance")
            == OWNER_FRESH_ACCEPTANCE_MARKER
        )
        date_value = observation_date or self.observation_date()
        provider_requests = 0

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
            } for row in page.observations], byte_budget=None)
            coverage = {"observation_date": page.observation_date.isoformat(),
                        "rejected": page.rejected_count,
                        "provider_returned_records": page.item_count,
                        "accepted_records": len(records),
                        "rejected_records": page.rejected_count,
                        "explicitly_omitted_records": omitted,
                        "source_coverage": "TOP_PER_COUNTRY_DAILY_LIST",
                        "records_omitted_storage_limit": omitted,
                        "item_count": page.item_count,
                        "provider_request_count": 1}
            if self.include_wikimedia_transport_metrics:
                coverage.update(http_status=page.http_status, response_bytes=page.response_bytes)
            return {"status": "PARTIAL" if page.rejected_count or omitted else "SUCCESS",
                    "data_mode": page.data_mode, "records": records,
                    "record_count": len(records), "provider_request_count": 1,
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
            recovered_cache_timestamp = None
            if result["records"]:
                raw_fetched_at = result["records"][0].get("fetched_at")
                if isinstance(raw_fetched_at, str):
                    try:
                        parsed_fetched_at = datetime.fromisoformat(
                            raw_fetched_at.replace("Z", "+00:00")
                        )
                    except ValueError:
                        parsed_fetched_at = None
                    if (isinstance(parsed_fetched_at, datetime)
                            and parsed_fetched_at.tzinfo is not None
                            and parsed_fetched_at.utcoffset() is not None
                            and parsed_fetched_at <= self.utcnow()):
                        recovered_cache_timestamp = parsed_fetched_at

            async def recovered_page():
                return result

            return await self._with_cache(
                WIKIMEDIA_SOURCE_ID, context, recovered_page,
                query_scope=f"{self.plan.fingerprint}:{saved_date.isoformat()}",
                skip_cache_read=True,
                cache_fetched_at=recovered_cache_timestamp,
            )

        # AQS may lag the latest complete UTC day. Try at most three dates,
        # treating 404 as unavailable (never as zero), and stop on any other
        # result. The gateway reserves every date independently before GET.
        last_error = None
        last_error_code = None
        for fallback_days in range(self.max_wikimedia_date_attempts):
            request_date = date_value - timedelta(days=fallback_days)
            request = WikimediaRequest(request_date, context.plan_fingerprint)
            attempt_id = hashlib.sha256(
                f"{context.job_id}:{WIKIMEDIA_SOURCE_ID}:{request_date.isoformat()}:page-1".encode()
            ).hexdigest()

            def persist_page_receipt(page):
                result = result_for(page)
                result["provider_request_count"] = provider_requests
                result["coverage"]["provider_request_count"] = provider_requests
                receipt = {
                    "schema": 1,
                    "source_id": WIKIMEDIA_SOURCE_ID,
                    "plan_fingerprint": context.plan_fingerprint,
                    "observation_date": request_date.isoformat(),
                    "attempt_id": attempt_id,
                    "data_mode": page.data_mode,
                    "result": result,
                }
                context.checkpoint({"wikimedia_receipt": receipt})

            async def fetch_date():
                nonlocal provider_requests
                try:
                    provider_requests += 1
                    page = await self.wikimedia.fetch_day(
                        request=request, job_id=context.job_id, owner=context.owner,
                        fence=context.fence, attempt_id=attempt_id,
                        timeout_seconds=min(15, self.wikimedia.runner.admission.max_attempt_seconds(
                            WIKIMEDIA_SOURCE_ID)), on_page_receipt=persist_page_receipt,
                    )
                except (WikimediaGatewayError, GatewayError) as exc:
                    if exc.code != "DAY_UNAVAILABLE":
                        raise
                    return {"status": "UNAVAILABLE", "data_mode": self.wikimedia.data_mode,
                        "records": [], "record_count": 0, "error_code": exc.code,
                        "coverage": {"observation_date": request_date.isoformat(),
                                     "source_coverage": "TOP_PER_COUNTRY_DAILY_LIST",
                                     "provider_request_count": provider_requests},
                        "provider_request_count": provider_requests}
                return result_for(page)

            try:
                result = await self._with_cache(
                    WIKIMEDIA_SOURCE_ID, context, fetch_date,
                    query_scope=f"{self.plan.fingerprint}:{request_date.isoformat()}",
                )
            except (WikimediaGatewayError, GatewayError) as exc:
                if (exc.code == "DAY_UNAVAILABLE"
                        and fallback_days < self.max_wikimedia_date_attempts - 1):
                    last_error = exc
                    last_error_code = exc.code
                    continue
                last_error = exc
                last_error_code = exc.code
                break
            if (not isinstance(result, dict) or result.get("data_mode") != self.wikimedia.data_mode
                    or result.get("status") not in {"SUCCESS", "PARTIAL", "UNAVAILABLE"}):
                raise AdmissionDenied("SOURCE_RESULT_INVALID")
            if (result.get("status") == "UNAVAILABLE"
                    and result.get("error_code") == "DAY_UNAVAILABLE"):
                last_error_code = "DAY_UNAVAILABLE"
                continue
            coverage_date = (result.get("coverage") or {}).get("observation_date")
            if coverage_date != request_date.isoformat():
                raise AdmissionDenied("SOURCE_OBSERVATION_DATE_MISMATCH")
            if result.get("delivery_state") != "CACHED":
                result["provider_request_count"] = provider_requests
                coverage = result.get("coverage")
                coverage = dict(coverage) if isinstance(coverage, dict) else {}
                coverage["provider_request_count"] = provider_requests
                if acceptance_fresh:
                    coverage["owner_fresh_acceptance"] = True
                result["coverage"] = coverage
            return result

        exc = last_error
        if exc is None and last_error_code is None:
            return {"status": "FAILED", "data_mode": self.wikimedia.data_mode,
                    "records": [], "record_count": 0, "error_code": "SOURCE_EXECUTION_FAILED"}
        error_code = exc.code if exc is not None else last_error_code
        if error_code in {"RESULT_PERSISTENCE_FAILED", "SETTLEMENT_PENDING"}:
            raise AdmissionDenied(error_code) from None
        context.checkpoint({"status": "UNAVAILABLE", "error_code": error_code})
        status = "PARTIAL" if error_code in {"RATE_LIMITED", "DAY_UNAVAILABLE"} else "FAILED"
        result = {"status": status, "data_mode": self.wikimedia.data_mode,
                  "records": [], "record_count": 0, "error_code": error_code,
                  "provider_request_count": provider_requests,
                  "coverage": {"provider_request_count": provider_requests,
                               **({"owner_fresh_acceptance": True}
                                  if acceptance_fresh else {})}}
        if (exc is not None and exc.code == "RATE_LIMITED"
                and exc.retry_after_unbounded_seconds is not None):
            result["retry_after_seconds"] = min(
                31_536_000, max(1, math.ceil(exc.retry_after_unbounded_seconds)))
        diagnostic = getattr(exc, "diagnostic", None) if exc is not None else None
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

    async def _wikimedia_history_handler(self, context, start_date, history_days):
        """Fetch an explicitly requested, bounded set of recent LIVE daily snapshots."""
        if (self.wikimedia.data_mode != "LIVE" or type(history_days) is not int
                or not 1 <= history_days <= 2):
            raise AdmissionDenied("HISTORICAL_FETCH_UNAVAILABLE")
        dates = [start_date - timedelta(days=offset) for offset in range(history_days)]
        allowed_dates = {item.isoformat() for item in dates}
        progress = (context.resume_snapshot or {}).get("progress", {})
        saved_receipts = progress.get("wikimedia_history_receipts", []) if isinstance(progress, dict) else []
        records = progress.get("wikimedia_history_records", []) if isinstance(progress, dict) else []
        if (not isinstance(saved_receipts, list) or len(saved_receipts) > 2
                or not isinstance(records, list) or len(records) > history_days * 1000
                or any(not isinstance(row, dict) or row.get("observation_date") not in allowed_dates
                       for row in records)):
            raise AdmissionDenied("CHECKPOINT_INVALID")
        receipts = {}
        for receipt in saved_receipts:
            if (not isinstance(receipt, dict) or receipt.get("schema") != 1
                    or receipt.get("source_id") != WIKIMEDIA_SOURCE_ID
                    or receipt.get("plan_fingerprint") != context.plan_fingerprint
                    or receipt.get("data_mode") != "LIVE"
                    or receipt.get("observation_date") not in allowed_dates
                    or receipt.get("status") not in {"SUCCESS", "PARTIAL", "UNAVAILABLE"}
                    or type(receipt.get("record_count")) is not int
                    or not 0 <= receipt["record_count"] <= 1000
                    or receipt["observation_date"] in receipts):
                raise AdmissionDenied("CHECKPOINT_INVALID")
            attempt_id = receipt.get("attempt_id")
            expected_attempt = hashlib.sha256(
                f"{context.job_id}:{WIKIMEDIA_SOURCE_ID}:{receipt['observation_date']}:page-1".encode()
            ).hexdigest()
            if attempt_id is not None and attempt_id != expected_attempt:
                raise AdmissionDenied("CHECKPOINT_INVALID")
            receipts[receipt["observation_date"]] = dict(receipt)

        records_by_date = {day: [row for row in records if row["observation_date"] == day]
                           for day in allowed_dates}
        for day, receipt in receipts.items():
            if receipt["status"] in {"SUCCESS", "PARTIAL"}:
                if len(records_by_date[day]) != receipt["record_count"]:
                    raise AdmissionDenied("CHECKPOINT_INVALID")
                if receipt.get("attempt_id"):
                    try:
                        settle_recovered = getattr(self.wikimedia.runner, "settle_recovered_attempt", None)
                        if callable(settle_recovered):
                            await asyncio.to_thread(
                                settle_recovered, context.job_id, context.owner, context.fence,
                                attempt_id=receipt["attempt_id"], now=self.utcnow(),
                            )
                        else:
                            await asyncio.to_thread(
                                self.wikimedia.runner.admission.settle_attempt,
                                context.job_id, context.owner, context.fence,
                                attempt_id=receipt["attempt_id"], now=self.utcnow(),
                            )
                    except Exception:
                        raise AdmissionDenied("SETTLEMENT_PENDING") from None
            elif records_by_date[day]:
                raise AdmissionDenied("CHECKPOINT_INVALID")

        def save_progress():
            context.checkpoint({
                "wikimedia_history_receipts": [receipts[day] for day in sorted(receipts)],
                "wikimedia_history_records": [row for day in sorted(records_by_date)
                                               for row in records_by_date[day]],
            })

        def result_for(page):
            normalized = [{
                "source": row.source, "country_scope": row.country_scope, "access": row.access,
                "observation_date": _jsonable(row.observation_date), "project": row.project,
                "article_original": row.article_original, "article_display": row.article_display,
                "views_ceil": row.views_ceil, "provider_rank": row.provider_rank,
                "quality_flags": list(row.quality_flags), "canonical_keyword": row.canonical_keyword,
                "fetched_at": _jsonable(row.fetched_at), "source_url": row.source_url,
            } for row in page.observations]
            normalized, omitted = _bounded_records(normalized, byte_budget=None)
            coverage = {"observation_date": page.observation_date.isoformat(),
                "rejected": page.rejected_count, "provider_returned_records": page.item_count,
                "accepted_records": len(normalized), "rejected_records": page.rejected_count,
                "explicitly_omitted_records": omitted, "source_coverage": "TOP_PER_COUNTRY_DAILY_LIST",
                "records_omitted_storage_limit": omitted, "item_count": page.item_count}
            if self.include_wikimedia_transport_metrics:
                coverage.update(http_status=page.http_status, response_bytes=page.response_bytes)
            return {"status": "PARTIAL" if page.rejected_count or omitted else "SUCCESS",
                    "data_mode": page.data_mode, "records": normalized,
                    "record_count": len(normalized), "coverage": coverage}

        rate_limited = None
        for day in dates:
            day_key = day.isoformat()
            receipt = receipts.get(day_key)
            if receipt and receipt["status"] in {"SUCCESS", "PARTIAL", "UNAVAILABLE"}:
                continue
            context.heartbeat()
            attempt_id = hashlib.sha256(
                f"{context.job_id}:{WIKIMEDIA_SOURCE_ID}:{day_key}:page-1".encode()
            ).hexdigest()

            def fetch_live_day():
                async def fetch():
                    request = WikimediaRequest(day, context.plan_fingerprint)

                    def persist_page_receipt(page):
                        daily = result_for(page)
                        records_by_date[day_key] = daily["records"]
                        receipts[day_key] = {
                            "schema": 1, "source_id": WIKIMEDIA_SOURCE_ID,
                            "plan_fingerprint": context.plan_fingerprint,
                            "observation_date": day_key, "attempt_id": attempt_id,
                            "data_mode": "LIVE", "status": daily["status"],
                            "record_count": daily["record_count"],
                            "coverage": daily["coverage"], "delivery_state": "FRESH",
                        }
                        save_progress()

                    page = await self.wikimedia.fetch_day(
                        request=request, job_id=context.job_id, owner=context.owner,
                        fence=context.fence, attempt_id=attempt_id,
                        timeout_seconds=min(15, self.wikimedia.runner.admission.max_attempt_seconds(
                            WIKIMEDIA_SOURCE_ID)), on_page_receipt=persist_page_receipt,
                    )
                    return result_for(page)
                return fetch()

            try:
                daily = await self._with_cache(
                    WIKIMEDIA_SOURCE_ID, context, fetch_live_day,
                    query_scope=f"{self.plan.fingerprint}:{day_key}",
                )
            except (WikimediaGatewayError, GatewayError) as exc:
                if exc.code in {"RESULT_PERSISTENCE_FAILED", "SETTLEMENT_PENDING"}:
                    raise AdmissionDenied(exc.code) from None
                receipts[day_key] = {
                    "schema": 1, "source_id": WIKIMEDIA_SOURCE_ID,
                    "plan_fingerprint": context.plan_fingerprint,
                    "observation_date": day_key, "data_mode": "LIVE",
                    "status": "UNAVAILABLE", "record_count": 0,
                    "error_code": exc.code,
                }
                records_by_date[day_key] = []
                save_progress()
                if exc.code == "RATE_LIMITED":
                    rate_limited = exc
                    break
                continue

            if (isinstance(daily, dict) and daily.get("data_mode") == "LIVE"
                    and daily.get("status") == "UNAVAILABLE"
                    and daily.get("error_code") == "DAY_UNAVAILABLE"):
                receipts[day_key] = {
                    "schema": 1, "source_id": WIKIMEDIA_SOURCE_ID,
                    "plan_fingerprint": context.plan_fingerprint,
                    "observation_date": day_key, "data_mode": "LIVE",
                    "status": "UNAVAILABLE", "record_count": 0,
                    "error_code": "DAY_UNAVAILABLE",
                    "delivery_state": daily.get("delivery_state", "FRESH"),
                }
                records_by_date[day_key] = []
                save_progress()
                continue

            if (not isinstance(daily, dict) or daily.get("data_mode") != "LIVE"
                    or daily.get("status") not in {"SUCCESS", "PARTIAL"}
                    or not isinstance(daily.get("records"), list)
                    or len(daily["records"]) > 1000
                    or any(row.get("observation_date") != day_key for row in daily["records"])):
                raise AdmissionDenied("SOURCE_RESULT_INVALID")
            records_by_date[day_key] = daily["records"]
            existing_receipt = receipts.get(day_key, {})
            receipts[day_key] = {
                "schema": 1, "source_id": WIKIMEDIA_SOURCE_ID,
                "plan_fingerprint": context.plan_fingerprint,
                "observation_date": day_key,
                "attempt_id": existing_receipt.get("attempt_id")
                    if daily.get("delivery_state") != "CACHED" else None,
                "data_mode": "LIVE", "status": daily["status"],
                "record_count": len(daily["records"]),
                "coverage": daily.get("coverage", {}),
                "delivery_state": daily.get("delivery_state", "FRESH"),
            }
            save_progress()

        ordered_records = [row for day in sorted(records_by_date)
                           for row in records_by_date[day]]
        successful = [receipts[day.isoformat()] for day in dates
                      if day.isoformat() in receipts
                      and receipts[day.isoformat()]["status"] in {"SUCCESS", "PARTIAL"}]
        missing = [receipts[day.isoformat()] for day in dates
                   if day.isoformat() in receipts
                   and receipts[day.isoformat()]["status"] == "UNAVAILABLE"]
        provider_count = sum(item.get("coverage", {}).get("provider_returned_records", 0)
                             for item in successful)
        rejected_count = sum(item.get("coverage", {}).get("rejected_records", 0)
                             for item in successful)
        omitted_count = sum(item.get("coverage", {}).get("explicitly_omitted_records", 0)
                            for item in successful)
        accepted_count = len(ordered_records)
        if provider_count != accepted_count + rejected_count + omitted_count:
            raise AdmissionDenied("SOURCE_COVERAGE_INVALID")
        all_requested_dates = all(day.isoformat() in receipts for day in dates)
        all_success = (all_requested_dates and len(successful) == history_days
                       and all(item["status"] == "SUCCESS" for item in successful))
        delivery = "CACHED" if successful and all(
            item.get("delivery_state") == "CACHED" for item in successful) else "FRESH"
        coverage = {
            "history_days_requested": history_days,
            "history_dates": [{"date": day.isoformat(),
                "status": receipts[day.isoformat()]["status"] if day.isoformat() in receipts else "NOT_REQUESTED",
                "record_count": receipts[day.isoformat()]["record_count"] if day.isoformat() in receipts else 0,
                "delivery_state": receipts[day.isoformat()].get("delivery_state") if day.isoformat() in receipts else None,
                "error_code": receipts[day.isoformat()].get("error_code") if day.isoformat() in receipts else None}
                for day in dates],
            "provider_returned_records": provider_count,
            "accepted_records": accepted_count,
            "rejected_records": rejected_count,
            "explicitly_omitted_records": omitted_count,
            "source_coverage": "TOP_PER_COUNTRY_DAILY_LIST",
            "missing_dates": [item["observation_date"] for item in missing],
        }
        result = {"status": "SUCCESS" if all_success else "PARTIAL", "data_mode": "LIVE",
                  "delivery_state": delivery, "records": ordered_records,
                  "record_count": accepted_count, "coverage": coverage}
        if rate_limited is not None:
            result["error_code"] = rate_limited.code
            retry_after = getattr(rate_limited, "retry_after_unbounded_seconds", None)
            if retry_after is not None:
                result["retry_after_seconds"] = min(31_536_000, max(1, math.ceil(retry_after)))
        elif missing:
            result["error_code"] = missing[0].get("error_code", "DAY_UNAVAILABLE")
        return result


class CacheUnavailable(Exception):
    code = "CACHE_UNAVAILABLE"
