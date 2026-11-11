"""Bounded offline query execution for either SerpApi search engine."""

import asyncio
import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, source_capability
from app.sources.query_plan import DEFAULT_QUERY_PLAN, QueryPlan
from app.sources.serpapi.client import SerpApiClient
from app.sources.serpapi.errors import SerpApiError
from app.sources.serpapi.models import BatchResult, QueryResult, SearchRecord
from app.sources.serpapi.normalization import merge_record, normalize_items
from app.sources.serpapi.pagination import build_contexts, initial_offset


@dataclass(frozen=True)
class ExecutionLimits:
    max_pages_per_query: int = 1
    max_queries: int = 14
    deadline_seconds: float = 60

    def validate(self, plan: QueryPlan) -> None:
        if type(self.max_pages_per_query) is not int or not 1 <= self.max_pages_per_query <= 5:
            raise ValueError("Page limit must be within 1..5")
        if type(self.max_queries) is not int or not 1 <= self.max_queries <= len(plan.seeds):
            raise ValueError("Query limit is outside the registered plan")
        if (type(self.deadline_seconds) not in (int, float)
                or not math.isfinite(self.deadline_seconds)
                or not 0.1 <= self.deadline_seconds <= 300):
            raise ValueError("Deadline must be within 0.1..300 seconds")


class SerpApiSearchAdapter:
    def __init__(self, source_id: str, client: SerpApiClient, *,
                 utcnow=lambda: datetime.now(timezone.utc), monotonic=time.monotonic,
                 sleep=asyncio.sleep):
        if source_id not in {GOOGLE_SOURCE_ID, BING_SOURCE_ID}:
            raise ValueError("Unsupported source")
        if not isinstance(client, SerpApiClient):
            raise TypeError("Expected an offline SerpApiClient")
        self.source_id, self.client = source_id, client
        self.utcnow, self.monotonic, self.sleep = utcnow, monotonic, sleep

    async def execute(self, *, plan: QueryPlan = DEFAULT_QUERY_PLAN,
                      limits: ExecutionLimits | None = None,
                      cancellation_event: asyncio.Event | None = None) -> BatchResult:
        limits = limits or ExecutionLimits()
        limits.validate(plan)
        contexts = build_contexts(plan, self.source_id, self.utcnow())
        deadline = self.monotonic() + limits.deadline_seconds
        records: dict[str, SearchRecord] = {}
        results = []
        fatal = None
        for context in contexts[:limits.max_queries]:
            if fatal is not None:
                results.append(QueryResult(context, 0, 0, 0, 0, fatal, False, fatal))
                continue
            cursor = initial_offset(context)
            pages = returned = accepted = rejected = 0
            rejection_reasons = []
            seen_pages = set()
            reason, error, complete = "INCOMPLETE", None, False
            for page_number in range(1, limits.max_pages_per_query + 1):
                try:
                    page = await self._request(context, cursor, deadline, cancellation_event)
                except SerpApiError as exc:
                    reason = error = exc.code
                    if exc.code in {"CANCELLED", "DEADLINE_EXCEEDED", "REQUEST_BUDGET_EXHAUSTED",
                                    "QUOTA_EXHAUSTED", "RATE_LIMITED", "UNAUTHORIZED",
                                    "PERMISSION_DENIED", "CREDENTIAL_NOT_CONFIGURED",
                                    "INVALID_CONFIGURATION"}:
                        fatal = exc.code
                    break
                pages += 1
                returned += len(page.items)
                digest = hashlib.sha256(json.dumps(page.items, sort_keys=True, ensure_ascii=True,
                                                   separators=(",", ":")).encode()).hexdigest()
                if digest in seen_pages:
                    reason = error = "REPEATED_PAGE"
                    break
                seen_pages.add(digest)
                normalized, invalid = normalize_items(
                    context, page.items, fetched_at=self.utcnow(), page=page_number,
                    offset=cursor.value, http_status=page.http_status, cache_state=page.cache_state,
                    provider_created_at=page.provider_created_at,
                )
                accepted += len(normalized)
                rejected += len(invalid)
                rejection_reasons.extend(invalid)
                for item in normalized:
                    records[item.canonical_url] = merge_record(records.get(item.canonical_url), item)
                if page.pagination_error:
                    reason = error = page.pagination_error
                    break
                if page.next_offset is None:
                    reason, complete = "END_OF_PAGES", True
                    break
                cursor = page.next_offset
                reason = "PAGE_CAP_REACHED"
            results.append(QueryResult(context, pages, returned, accepted, rejected,
                                       reason, complete and not rejected, error,
                                       tuple(rejection_reasons)))
        all_selected = limits.max_queries == len(contexts)
        complete = all_selected and all(item.complete for item in results)
        has_partial = not complete or any(item.error_code or item.rejected for item in results)
        any_page = any(item.pages_fetched for item in results)
        status = "SUCCESS" if not has_partial else ("PARTIAL" if any_page else "FAILED")
        return BatchResult(self.source_id, source_capability(self.source_id).provider_id,
                           plan.fingerprint, status, complete, tuple(records.values()),
                           tuple(results), self.client.pool.snapshot(), fatal or (
                               next((x.stop_reason for x in results if not x.complete), None)
                               if has_partial else None))

    async def _request(self, context, cursor, deadline, event):
        for attempt in range(self.client.policy.max_retries + 1):
            if event is not None and event.is_set():
                raise SerpApiError("CANCELLED")
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise SerpApiError("DEADLINE_EXCEEDED")
            try:
                request = self.client.search(
                    context, cursor, timeout_seconds=min(
                        remaining, self.client.policy.total_timeout_seconds
                    ),
                )
                if event is None:
                    return await request
                request_task = asyncio.create_task(request)
                cancel_task = asyncio.create_task(event.wait())
                try:
                    done, _ = await asyncio.wait(
                        {request_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if cancel_task in done and event.is_set():
                        raise SerpApiError("CANCELLED")
                    return await request_task
                finally:
                    for task in (request_task, cancel_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(request_task, cancel_task, return_exceptions=True)
            except SerpApiError as exc:
                if event is not None and event.is_set():
                    raise SerpApiError("CANCELLED") from None
                if self.monotonic() >= deadline:
                    raise SerpApiError("DEADLINE_EXCEEDED") from None
                if not exc.retryable or attempt >= self.client.policy.max_retries:
                    raise
                delay = (exc.retry_after if exc.retry_after is not None
                         else self.client.policy.backoff_delay(attempt))
                if delay >= deadline - self.monotonic():
                    raise SerpApiError("DEADLINE_EXCEEDED") from None
                if event is None:
                    await self.sleep(delay)
                else:
                    try:
                        await asyncio.wait_for(event.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                    else:
                        raise SerpApiError("CANCELLED") from None
        raise SerpApiError("PROVIDER_ERROR")
