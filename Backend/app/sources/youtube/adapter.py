"""Offline-verifiable query batch orchestration for YouTube search.list."""

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable

import httpx

from app.ports.credentials import CredentialProvider
from app.sources.catalog import YOUTUBE_SOURCE_ID
from app.sources.http_policy import HttpDeadlineExceeded, HttpPolicy
from app.sources.query_plan import DEFAULT_QUERY_PLAN, QueryPlan
from app.sources.youtube.client import MAX_RESPONSE_BYTES, YouTubeSearchClient
from app.sources.youtube.errors import YouTubeErrorCode, YouTubeSourceError
from app.sources.youtube.models import (
    ProviderPage,
    QueryBatchResult,
    QueryContext,
    QueryExecutionResult,
    ScopedPageToken,
    YouTubeSearchRecord,
)
from app.sources.youtube.normalizer import normalize_items
from app.sources.youtube.pagination import (
    ExecutionLimits,
    RequestBudgetTracker,
    ResolvedExecutionLimits,
    build_execution_plan,
)


class YouTubeSearchAdapter:
    """Execute the fixed server query plan with injected transport and fakeable dependencies."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        credential_resolver: CredentialProvider | None,
        http_policy: HttpPolicy | None = None,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        self.http_policy = http_policy or HttpPolicy()
        self.utcnow = utcnow
        self.monotonic = monotonic
        self.sleep = sleep
        self.client = YouTubeSearchClient(
            client=client,
            credential_resolver=credential_resolver,
            http_policy=self.http_policy,
            utcnow=utcnow,
            max_response_bytes=max_response_bytes,
        )

    async def execute(
        self,
        *,
        plan: QueryPlan = DEFAULT_QUERY_PLAN,
        limits: ExecutionLimits | None = None,
        cancellation_event: asyncio.Event | None = None,
    ) -> QueryBatchResult:
        started_at = self.utcnow()
        execution = build_execution_plan(plan, started_at)
        limits = (limits or ExecutionLimits()).resolved(plan)
        contexts = execution.queries[: limits.max_queries]
        deadline_at = self.monotonic() + limits.deadline_seconds
        budget = RequestBudgetTracker(limits.planned_calls, limits.max_requests)
        records_by_id: dict[tuple[str, str], YouTubeSearchRecord] = {}
        query_results: list[QueryExecutionResult] = []
        batch_stop_reason = None

        try:
            api_key = self.client.resolve_credential()
        except YouTubeSourceError as exc:
            batch_stop_reason = exc.code.value
            query_results = [
                self._not_attempted(context, batch_stop_reason) for context in contexts
            ]
            return QueryBatchResult(
                status="FAILED",
                complete=False,
                data_mode="SYNTHETIC",
                source_id=YOUTUBE_SOURCE_ID,
                provider_id="youtube_data_api_v3",
                query_plan_id=execution.plan_id,
                query_plan_version=execution.plan_version,
                query_plan_fingerprint=execution.plan_fingerprint,
                batch_started_at=execution.batch_started_at,
                published_after=execution.published_after,
                records=(),
                queries=tuple(query_results),
                budget=budget.snapshot(batch_stop_reason),
                stop_reason=batch_stop_reason,
            )

        for context in contexts:
            if batch_stop_reason is not None:
                query_results.append(self._not_attempted(context, batch_stop_reason))
                continue
            stop = self._preflight_stop(cancellation_event, deadline_at, budget)
            if stop is not None:
                batch_stop_reason = stop
                query_results.append(self._not_attempted(context, stop))
                continue

            result, global_stop = await self._execute_query(
                context,
                api_key=api_key,
                limits=limits,
                budget=budget,
                deadline_at=deadline_at,
                cancellation_event=cancellation_event,
                records_by_id=records_by_id,
            )
            query_results.append(result)
            if global_stop is not None:
                batch_stop_reason = global_stop

        all_selected = len(contexts) == len(plan.seeds)
        all_query_pages_complete = all(
            item.pagination_state == "COMPLETE" and item.error is None for item in query_results
        )
        complete = all_selected and all_query_pages_complete
        has_errors = any(item.error is not None or item.rejected_count for item in query_results)
        successful_query = any(item.requested_pages > 0 for item in query_results)
        if has_errors or not all_selected:
            status = "PARTIAL" if successful_query else "FAILED"
        else:
            status = "SUCCESS"

        if batch_stop_reason is None:
            if any(item.stop_reason == "PAGE_CAP_REACHED" for item in query_results):
                batch_stop_reason = "PAGE_CAP_REACHED"
            elif has_errors:
                batch_stop_reason = "PARTIAL_BATCH"
        return QueryBatchResult(
            status=status,
            complete=complete,
            data_mode="SYNTHETIC",
            source_id=YOUTUBE_SOURCE_ID,
            provider_id="youtube_data_api_v3",
            query_plan_id=execution.plan_id,
            query_plan_version=execution.plan_version,
            query_plan_fingerprint=execution.plan_fingerprint,
            batch_started_at=execution.batch_started_at,
            published_after=execution.published_after,
            records=tuple(records_by_id.values()),
            queries=tuple(query_results),
            budget=budget.snapshot(batch_stop_reason),
            stop_reason=batch_stop_reason,
        )

    async def _execute_query(
        self,
        context: QueryContext,
        *,
        api_key: str,
        limits: ResolvedExecutionLimits,
        budget: RequestBudgetTracker,
        deadline_at: float,
        cancellation_event: asyncio.Event | None,
        records_by_id: dict[tuple[str, str], YouTubeSearchRecord],
    ) -> tuple[QueryExecutionResult, str | None]:
        page_token: ScopedPageToken | None = None
        seen_tokens: set[str] = set()
        requested_pages = 0
        returned_count = 0
        accepted_count = 0
        rejected_count = 0
        rejection_codes: list[str] = []
        rank_offset = 0
        last_error = None
        pagination_state = "INCOMPLETE"
        stop_reason = "PARTIAL_BATCH"
        global_stop = None

        for page_index in range(1, limits.max_pages_per_query + 1):
            preflight = self._preflight_stop(cancellation_event, deadline_at, budget)
            if preflight is not None:
                code = self._stop_code(preflight)
                last_error = self._error_for_context(code, context, preflight)
                stop_reason = preflight
                global_stop = preflight
                break

            page, error, call_stop = await self._request_page(
                context,
                page_token,
                api_key=api_key,
                max_results=limits.max_results,
                budget=budget,
                deadline_at=deadline_at,
                cancellation_event=cancellation_event,
            )
            if error is not None:
                last_error = error
                stop_reason = error.stop_reason or error.code.value
                if page_index > 1 and stop_reason in {"CANCELLED", "DEADLINE_EXCEEDED"}:
                    global_stop = stop_reason
                elif call_stop is not None:
                    global_stop = call_stop
                elif error.code in {
                    YouTubeErrorCode.CREDENTIAL_NOT_CONFIGURED,
                    YouTubeErrorCode.INVALID_CONFIGURATION,
                    YouTubeErrorCode.UNAUTHORIZED,
                    YouTubeErrorCode.PERMISSION_DENIED,
                    YouTubeErrorCode.QUOTA_EXHAUSTED,
                    YouTubeErrorCode.RATE_LIMITED,
                }:
                    global_stop = error.code.value
                pagination_state = "MORE_AVAILABLE" if page_token is not None else "INCOMPLETE"
                break

            assert page is not None
            requested_pages += 1
            returned_count += len(page.items)
            fetched_at = self.utcnow()
            normalized, rejection_reasons = normalize_items(
                context,
                page.items,
                fetched_at=fetched_at,
                page_index=page_index,
                rank_offset=rank_offset,
                provider_response_status=page.response_status,
            )
            accepted_count += len(normalized)
            rejected_count += len(rejection_reasons)
            rejection_codes.extend(rejection_reasons)
            for record in normalized:
                key = (record.provider_id, record.provider_video_id)
                records_by_id[key] = self._merge_record(records_by_id.get(key), record)
            rank_offset += len(page.items)

            if page.next_page_token is None:
                pagination_state = "COMPLETE"
                stop_reason = "END_OF_PAGES"
                break
            if page.next_page_token in seen_tokens:
                last_error = self._error_for_context(
                    YouTubeErrorCode.INVALID_PAGE_TOKEN,
                    context,
                    "REPEATED_PAGE_TOKEN",
                )
                stop_reason = "REPEATED_PAGE_TOKEN"
                pagination_state = "MORE_AVAILABLE"
                break
            seen_tokens.add(page.next_page_token)
            if page_index >= limits.max_pages_per_query:
                pagination_state = "MORE_AVAILABLE"
                stop_reason = "PAGE_CAP_REACHED"
                break
            page_token = ScopedPageToken(page.next_page_token, context.context_key)
        else:
            pagination_state = "MORE_AVAILABLE" if page_token else "INCOMPLETE"
            stop_reason = "PAGE_CAP_REACHED"

        result = QueryExecutionResult(
            query=context,
            requested_pages=requested_pages,
            returned_count=returned_count,
            accepted_count=accepted_count,
            rejected_count=rejected_count,
            rejection_reasons=tuple(rejection_codes),
            pagination_state=pagination_state,
            stop_reason=stop_reason,
            error=last_error,
        )
        return result, global_stop

    async def _request_page(
        self,
        context: QueryContext,
        page_token: ScopedPageToken | None,
        *,
        api_key: str,
        max_results: int,
        budget: RequestBudgetTracker,
        deadline_at: float,
        cancellation_event: asyncio.Event | None,
    ) -> tuple[ProviderPage | None, YouTubeSourceError | None, str | None]:
        attempt = 0
        is_retry = False
        while True:
            stop = self._preflight_stop(cancellation_event, deadline_at, budget)
            if stop is not None:
                return None, self._error_for_context(self._stop_code(stop), context, stop), stop
            if not budget.reserve(retry=is_retry):
                code = (
                    YouTubeErrorCode.RETRY_BUDGET_EXHAUSTED
                    if attempt
                    else YouTubeErrorCode.REQUEST_BUDGET_EXHAUSTED
                )
                reason = "REQUEST_BUDGET_EXHAUSTED"
                return None, self._error_for_context(code, context, reason), reason

            attempt += 1
            remaining = max(0.001, deadline_at - self.monotonic())
            try:
                page = await self.client.search(
                    context,
                    page_token,
                    api_key=api_key,
                    max_results=max_results,
                    timeout_seconds=min(self.http_policy.total_timeout_seconds, remaining),
                )
            except HttpDeadlineExceeded:
                budget.failed()
                error = self._error_for_context(
                    YouTubeErrorCode.UPSTREAM_TIMEOUT,
                    context,
                    "UPSTREAM_TIMEOUT",
                    attempt=attempt,
                    retryable=True,
                )
            except (httpx.TimeoutException, TimeoutError):
                budget.failed()
                error = self._error_for_context(
                    YouTubeErrorCode.UPSTREAM_TIMEOUT,
                    context,
                    "UPSTREAM_TIMEOUT",
                    attempt=attempt,
                    retryable=True,
                )
            except httpx.RequestError:
                budget.failed()
                error = self._error_for_context(
                    YouTubeErrorCode.NETWORK_ERROR,
                    context,
                    "NETWORK_ERROR",
                    attempt=attempt,
                    retryable=True,
                )
            except YouTubeSourceError as exc:
                if exc.request_completed:
                    budget.completed()
                else:
                    budget.failed()
                error = self._copy_error(exc, attempt=attempt)
            else:
                budget.completed(page_fetched=True)
                return page, None, None

            may_retry = error.retryable and attempt <= self.http_policy.max_retries
            if not may_retry:
                return None, error, self._fatal_stop(error)
            if error.retry_after_exceeds_limit:
                return None, error, None
            if budget.remaining <= 0:
                retry_error = self._error_for_context(
                    YouTubeErrorCode.RETRY_BUDGET_EXHAUSTED,
                    context,
                    "REQUEST_BUDGET_EXHAUSTED",
                    attempt=attempt,
                    cause_code=error.code,
                )
                return None, retry_error, "REQUEST_BUDGET_EXHAUSTED"

            delay = self._retry_delay(error, attempt - 1)
            remaining = deadline_at - self.monotonic()
            if remaining <= 0 or delay >= remaining:
                deadline_error = self._error_for_context(
                    YouTubeErrorCode.DEADLINE_EXCEEDED,
                    context,
                    "DEADLINE_EXCEEDED",
                    attempt=attempt,
                    cause_code=error.code,
                )
                return None, deadline_error, "DEADLINE_EXCEEDED"
            if cancellation_event is not None and cancellation_event.is_set():
                cancel_error = self._error_for_context(
                    YouTubeErrorCode.CANCELLED,
                    context,
                    "CANCELLED",
                    attempt=attempt,
                    cause_code=error.code,
                )
                return None, cancel_error, "CANCELLED"
            await self.sleep(delay)
            is_retry = True

    def _retry_delay(self, error: YouTubeSourceError, retry_index: int) -> float:
        if error.retry_after_seconds is not None:
            return error.retry_after_seconds
        if error.code == YouTubeErrorCode.RATE_LIMITED and error.retry_after_exceeds_limit:
            return float("inf")
        return self.http_policy.backoff_delay(retry_index)

    @staticmethod
    def _copy_error(error: YouTubeSourceError, *, attempt: int) -> YouTubeSourceError:
        return YouTubeSourceError(
            error.code,
            http_status=error.http_status,
            retryable=error.retryable,
            attempt_count=attempt,
            retry_after_seconds=error.retry_after_seconds,
            retry_after_exceeds_limit=error.retry_after_exceeds_limit,
            retry_after_unbounded_seconds=error.retry_after_unbounded_seconds,
            source_id=error.source_id,
            provider_id=error.provider_id,
            query_scope=error.query_scope,
            stop_reason=error.stop_reason,
            request_completed=error.request_completed,
            cause_code=error.cause_code,
        )

    @staticmethod
    def _error_for_context(
        code: YouTubeErrorCode,
        context: QueryContext,
        stop_reason: str,
        *,
        attempt: int = 0,
        retryable: bool = False,
        cause_code: YouTubeErrorCode | None = None,
    ) -> YouTubeSourceError:
        return YouTubeSourceError(
            code,
            retryable=retryable,
            attempt_count=attempt,
            query_scope=context.taxonomy_scope_label,
            stop_reason=stop_reason,
            cause_code=cause_code,
        )

    @staticmethod
    def _stop_code(stop_reason: str) -> YouTubeErrorCode:
        return {
            "CREDENTIAL_NOT_CONFIGURED": YouTubeErrorCode.CREDENTIAL_NOT_CONFIGURED,
            "INVALID_CONFIGURATION": YouTubeErrorCode.INVALID_CONFIGURATION,
            "UNAUTHORIZED": YouTubeErrorCode.UNAUTHORIZED,
            "PERMISSION_DENIED": YouTubeErrorCode.PERMISSION_DENIED,
            "REQUEST_BUDGET_EXHAUSTED": YouTubeErrorCode.REQUEST_BUDGET_EXHAUSTED,
            "DEADLINE_EXCEEDED": YouTubeErrorCode.DEADLINE_EXCEEDED,
            "CANCELLED": YouTubeErrorCode.CANCELLED,
            "QUOTA_EXHAUSTED": YouTubeErrorCode.QUOTA_EXHAUSTED,
            "RATE_LIMITED": YouTubeErrorCode.RATE_LIMITED,
        }.get(stop_reason, YouTubeErrorCode.PARTIAL_BATCH)

    @staticmethod
    def _fatal_stop(error: YouTubeSourceError) -> str | None:
        if error.code in {
            YouTubeErrorCode.CREDENTIAL_NOT_CONFIGURED,
            YouTubeErrorCode.INVALID_CONFIGURATION,
            YouTubeErrorCode.UNAUTHORIZED,
            YouTubeErrorCode.PERMISSION_DENIED,
            YouTubeErrorCode.QUOTA_EXHAUSTED,
            YouTubeErrorCode.RATE_LIMITED,
        }:
            return error.code.value
        return None

    def _preflight_stop(self, cancellation_event, deadline_at, budget) -> str | None:
        if cancellation_event is not None and cancellation_event.is_set():
            return "CANCELLED"
        if self.monotonic() >= deadline_at:
            return "DEADLINE_EXCEEDED"
        if budget.remaining <= 0:
            return "REQUEST_BUDGET_EXHAUSTED"
        return None

    def _not_attempted(self, context: QueryContext, reason: str) -> QueryExecutionResult:
        code = self._stop_code(reason)
        error = self._error_for_context(code, context, reason)
        return QueryExecutionResult(
            query=context,
            requested_pages=0,
            returned_count=0,
            accepted_count=0,
            rejected_count=0,
            rejection_reasons=(),
            pagination_state="INCOMPLETE",
            stop_reason=reason,
            error=error,
        )

    @staticmethod
    def _merge_record(
        existing: YouTubeSearchRecord | None,
        incoming: YouTubeSearchRecord,
    ) -> YouTubeSearchRecord:
        if existing is None:
            return incoming
        if (existing.source_id, existing.provider_id, existing.provider_video_id, existing.data_mode) != (
            incoming.source_id, incoming.provider_id, incoming.provider_video_id, incoming.data_mode
        ):
            raise ValueError("Cannot merge unrelated or differently sourced records")
        return replace(
            existing,
            query_evidence=existing.query_evidence + incoming.query_evidence,
            quality_flags=tuple(
                dict.fromkeys((*existing.quality_flags, "DUPLICATE_ENTITY_IN_SOURCE_SCOPE"))
            ),
        )
