"""Bounded source-specific query pagination over a guarded page gateway.

This collector is not composed into public Fetch. It never accepts a public
query, URL, provider or quota; callers supply a registered server plan.
"""

import asyncio
import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.jobs.public_admission import AdmissionDenied
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.sources.live_gateway import GatewayError, LivePageGateway
from app.sources.query_plan import QueryPlan, get_query_plan
from app.sources.serpapi.errors import SerpApiError
from app.sources.serpapi.normalization import merge_record
from app.sources.serpapi.normalization import normalize_items as normalize_search
from app.sources.serpapi.pagination import build_contexts, initial_offset
from app.sources.youtube.adapter import YouTubeSearchAdapter
from app.sources.youtube.errors import YouTubeSourceError
from app.sources.youtube.models import ScopedPageToken
from app.sources.youtube.normalizer import normalize_items as normalize_youtube
from app.sources.youtube.pagination import build_execution_plan


@dataclass(frozen=True)
class CollectionLimits:
    max_queries: int = 1
    max_pages_per_query: int = 1
    max_requests: int = 1
    max_retries_per_page: int = 0
    deadline_seconds: float = 60
    max_results: int = 50

    def validate(self, plan: QueryPlan):
        for value, lower, upper in ((self.max_queries, 1, len(plan.seeds)),
                                    (self.max_pages_per_query, 1, 5),
                                    (self.max_requests, 1, 500),
                                    (self.max_retries_per_page, 0, 2),
                                    (self.max_results, 1, 50)):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("invalid bounded collection limit")
        if (type(self.deadline_seconds) not in (int, float) or not math.isfinite(self.deadline_seconds)
                or not 0.1 <= self.deadline_seconds <= 300):
            raise ValueError("invalid collection deadline")


@dataclass(frozen=True)
class QueryProgress:
    query_fingerprint: str
    pages: int
    accepted: int
    rejected: int
    reason: str
    retry_at: datetime | None = None


@dataclass(frozen=True)
class CollectionResult:
    source_id: str
    data_mode: str | None
    status: str
    complete: bool
    attempted_calls: int
    queries: tuple[QueryProgress, ...]
    records: tuple[object, ...] = field(repr=False)
    stop_reason: str | None = None


class GuardedQueryCollector:
    def __init__(self, gateway: LivePageGateway, *, utcnow=lambda: datetime.now(timezone.utc),
                 monotonic=time.monotonic, sleep=asyncio.sleep):
        if not isinstance(gateway, LivePageGateway) or not all(callable(v) for v in (utcnow, monotonic, sleep)):
            raise ValueError("gateway and bounded clock dependencies required")
        self.gateway, self.utcnow, self.monotonic, self.sleep = gateway, utcnow, monotonic, sleep

    async def collect(self, *, plan: QueryPlan, limits: CollectionLimits,
                      job_id: str, owner: str, fence: int) -> CollectionResult:
        if not isinstance(plan, QueryPlan) or get_query_plan(plan.plan_id, plan.version) != plan:
            raise ValueError("registered server plan required")
        if plan.fingerprint != self.gateway.runner.admission.policy.plan_fingerprint:
            raise ValueError("plan differs from admission policy")
        if not isinstance(limits, CollectionLimits):
            raise ValueError("validated limits required")
        limits.validate(plan)
        source = self.gateway.source_id
        started = self.utcnow()
        contexts = (build_contexts(plan, source, started) if source in {GOOGLE_SOURCE_ID, BING_SOURCE_ID}
                    else build_execution_plan(plan, started).queries)
        deadline = self.monotonic() + limits.deadline_seconds
        attempted = 0
        progress, records = [], {}
        global_stop = None
        for context in contexts[:limits.max_queries]:
            if global_stop:
                progress.append(QueryProgress(context.query_fingerprint, 0, 0, 0, global_stop))
                continue
            cursor = initial_offset(context) if source != YOUTUBE_SOURCE_ID else None
            seen_cursors = set()
            seen_pages = set()
            pages = accepted = rejected = rank_offset = 0
            reason, retry_at = "PAGE_CAP_REACHED", None
            for page_number in range(1, limits.max_pages_per_query + 1):
                page = None
                for retry in range(limits.max_retries_per_page + 1):
                    remaining = deadline - self.monotonic()
                    if remaining <= 0:
                        reason = global_stop = "DEADLINE_EXCEEDED"
                        break
                    if attempted >= limits.max_requests:
                        reason = global_stop = "REQUEST_BUDGET_EXHAUSTED"
                        break
                    attempt_id = hashlib.sha256(
                        f"{job_id}:{source}:{context.query_fingerprint}:{page_number}:{retry}".encode()
                    ).hexdigest()
                    attempted += 1
                    try:
                        result = await self.gateway.fetch_page(
                            context=context, cursor=cursor, job_id=job_id, owner=owner,
                            fence=fence, attempt_id=attempt_id, max_results=limits.max_results,
                            timeout_seconds=min(remaining, self.gateway.policy.total_timeout_seconds,
                                self.gateway.runner.admission.max_attempt_seconds(source)),
                        )
                        if result.data_mode != self.gateway.data_mode or result.source_id != source:
                            raise GatewayError("PROVENANCE_MISMATCH")
                        page = result.page
                        break
                    except (GatewayError, SerpApiError, YouTubeSourceError, AdmissionDenied) as exc:
                        code = exc.code.value if isinstance(exc, YouTubeSourceError) else exc.code
                        retryable = (bool(getattr(exc, "retryable", False))
                                     or isinstance(exc, GatewayError) and code in {
                                         "UPSTREAM_TIMEOUT", "UPSTREAM_ERROR"})
                        delay = (getattr(exc, "retry_after", None)
                                 if isinstance(exc, SerpApiError)
                                 else getattr(exc, "retry_after_seconds", None))
                        original_delay = getattr(exc, "retry_after_unbounded_seconds", None)
                        # Never shorten a provider Retry-After. Delay beyond the
                        # deadline is surfaced to a durable queue rather than slept.
                        if code == "RATE_LIMITED" and original_delay is not None and delay is None:
                            if math.isfinite(original_delay) and 0 <= original_delay <= 31_536_000:
                                retry_at = self.utcnow() + timedelta(seconds=original_delay)
                                reason = global_stop = "DEFERRED"
                            else:
                                reason = global_stop = "RATE_LIMITED"
                            break
                        if delay is None and retryable:
                            delay = self.gateway.policy.backoff_delay(retry)
                        if code == "RATE_LIMITED" and delay is not None:
                            retry_at = self.utcnow() + timedelta(seconds=delay)
                        if retryable and delay is not None and delay >= deadline - self.monotonic():
                            reason = global_stop = "DEFERRED"
                            retry_at = self.utcnow() + timedelta(seconds=delay)
                            break
                        if not retryable or retry >= limits.max_retries_per_page:
                            reason = "DEFERRED" if code == "RATE_LIMITED" and retry_at else code
                            if code in {"SOURCE_NOT_APPROVED", "ALREADY_RESERVED", "QUOTA_EXHAUSTED",
                                        "RATE_LIMITED", "CREDENTIAL_UNAVAILABLE", "ADMISSION_REVOKED",
                                        "LEASE_LOST", "FEATURE_DISABLED", "PROVIDER_CONCURRENCY_LIMIT",
                                        "SETTLEMENT_PENDING"}:
                                global_stop = reason
                            break
                        if delay:
                            await self.sleep(delay)
                if page is None:
                    break
                pages += 1
                try:
                    page_digest = hashlib.sha256(json.dumps(
                        page.items, sort_keys=True, ensure_ascii=True, allow_nan=False,
                        separators=(",", ":"),
                    ).encode()).hexdigest()
                except (TypeError, ValueError, RecursionError):
                    reason = "MALFORMED_RESPONSE"
                    break
                if page_digest in seen_pages:
                    reason = "REPEATED_PAGE"
                    break
                seen_pages.add(page_digest)
                if source in {GOOGLE_SOURCE_ID, BING_SOURCE_ID}:
                    normalized, invalid = normalize_search(
                        context, page.items, fetched_at=self.utcnow(), page=page_number,
                        offset=cursor.value, http_status=page.http_status, cache_state=page.cache_state,
                        provider_created_at=page.provider_created_at, data_mode=result.data_mode,
                    )
                    for record in normalized:
                        records[record.canonical_url] = merge_record(records.get(record.canonical_url), record)
                    next_cursor = page.next_offset
                    if page.pagination_error:
                        reason = page.pagination_error
                        accepted += len(normalized)
                        rejected += len(invalid)
                        break
                else:
                    normalized, invalid = normalize_youtube(
                        context, page.items, fetched_at=self.utcnow(), page_index=page_number,
                        rank_offset=rank_offset, provider_response_status=page.response_status,
                        data_mode=result.data_mode,
                    )
                    rank_offset += len(page.items)
                    for record in normalized:
                        key = (record.provider_id, record.provider_video_id)
                        records[key] = YouTubeSearchAdapter._merge_record(records.get(key), record)
                    next_cursor = (ScopedPageToken(page.next_page_token, context.context_key)
                                   if page.next_page_token is not None else None)
                accepted += len(normalized)
                rejected += len(invalid)
                if next_cursor is None:
                    reason = "END_OF_PAGES" if not invalid else "REJECTED_RECORDS"
                    break
                cursor_key = next_cursor.value
                if cursor_key in seen_cursors:
                    reason = "REPEATED_PAGE_CURSOR"
                    break
                seen_cursors.add(cursor_key)
                cursor = next_cursor
            progress.append(QueryProgress(context.query_fingerprint, pages, accepted, rejected, reason, retry_at))
        all_selected = limits.max_queries == len(contexts)
        complete = all_selected and all(row.reason == "END_OF_PAGES" for row in progress)
        status = "SUCCESS" if complete else ("PARTIAL" if any(row.pages for row in progress) else "FAILED")
        source_stop = next((row.reason for row in progress if row.reason != "END_OF_PAGES"), None)
        observed_mode = self.gateway.data_mode if any(row.pages for row in progress) else None
        return CollectionResult(source, observed_mode, status, complete, attempted,
            tuple(progress), tuple(records.values()), global_stop or source_stop or (
                "QUERY_CAP_REACHED" if not all_selected else None))
