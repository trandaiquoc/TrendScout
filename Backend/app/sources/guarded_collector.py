"""Bounded source-specific query pagination over a guarded page gateway.

This collector is not composed into public Fetch. It never accepts a public
query, URL, provider or quota; callers supply a registered server plan.
"""

import asyncio
import hashlib
import json
import math
import re
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta, timezone

from app.jobs.public_admission import AdmissionDenied
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.sources.live_gateway import GatewayError, LivePageGateway
from app.sources.query_plan import QueryPlan, get_query_plan
from app.sources.serpapi.errors import SerpApiError
from app.sources.serpapi.models import QueryContext as SerpQueryContext
from app.sources.serpapi.models import QueryEvidence as SerpQueryEvidence
from app.sources.serpapi.models import ScopedOffset, SearchRecord
from app.sources.serpapi.normalization import merge_record
from app.sources.serpapi.normalization import normalize_items as normalize_search
from app.sources.serpapi.pagination import build_contexts, initial_offset, validate_offset
from app.sources.youtube.adapter import YouTubeSearchAdapter
from app.sources.youtube.errors import YouTubeSourceError
from app.sources.youtube.models import (
    MetricDescriptor,
    ScopedPageToken,
    YouTubeSearchRecord,
)
from app.sources.youtube.models import (
    QueryEvidence as YouTubeQueryEvidence,
)
from app.sources.youtube.normalizer import normalize_items as normalize_youtube
from app.sources.youtube.pagination import build_execution_plan

_CHECKPOINT_RECORD_BUDGET_BYTES = 100_000


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
    checkpoint_records_omitted: int = 0


def _checkpoint_json(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("checkpoint timestamp must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _checkpoint_json(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _checkpoint_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_checkpoint_json(item) for item in value]
    return value


def _bounded_checkpoint_records(records):
    selected, used, omitted = [], 2, 0
    for record in records:
        value = _checkpoint_json(record)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
        if used + len(encoded) + (1 if selected else 0) > _CHECKPOINT_RECORD_BUDGET_BYTES:
            omitted += 1
            continue
        selected.append(value)
        used += len(encoded) + (1 if len(selected) > 1 else 0)
    return selected, omitted


def _parse_datetime(value, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid collector checkpoint timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid collector checkpoint timestamp")
    return parsed.astimezone(timezone.utc)


def _restore_record(source, value):
    if not isinstance(value, dict):
        raise ValueError("invalid normalized checkpoint record")
    if source in {GOOGLE_SOURCE_ID, BING_SOURCE_ID}:
        evidence = []
        for raw in value["evidence"]:
            query = dict(raw["query"])
            query["started_at"] = _parse_datetime(query["started_at"])
            evidence.append(SerpQueryEvidence(
                query=SerpQueryContext(**query), fetched_at=_parse_datetime(raw["fetched_at"]),
                page=raw["page"], offset=raw["offset"], position=raw["position"],
                http_status=raw["http_status"], cache_state=raw["cache_state"],
                provider_created_at=_parse_datetime(raw["provider_created_at"], optional=True),
            ))
        value = {**value, "fetched_at": _parse_datetime(value["fetched_at"]),
                 "evidence": tuple(evidence), "quality_flags": tuple(value["quality_flags"])}
        return SearchRecord(**value)
    if source == YOUTUBE_SOURCE_ID:
        evidence = []
        for raw in value["query_evidence"]:
            item = dict(raw)
            item["published_after"] = _parse_datetime(item["published_after"])
            item["fetched_at"] = _parse_datetime(item["fetched_at"])
            evidence.append(YouTubeQueryEvidence(**item))
        value = {**value,
                 "published_at": _parse_datetime(value["published_at"], optional=True),
                 "fetched_at": _parse_datetime(value["fetched_at"]),
                 "query_evidence": tuple(evidence),
                 "metric": MetricDescriptor(**value["metric"]),
                 "quality_flags": tuple(value["quality_flags"]),
                 "thumbnail_urls": tuple(value.get("thumbnail_urls", []))}
        return YouTubeSearchRecord(**value)
    raise ValueError("unsupported checkpoint source")


def _restore_checkpoint(source, data_mode, plan, limits, contexts, snapshot):
    if snapshot is None:
        return None
    if (not isinstance(snapshot, dict) or snapshot.get("schema") != 1
            or snapshot.get("source_id") != source
            or snapshot.get("data_mode") != data_mode
            or snapshot.get("plan_fingerprint") != plan.fingerprint
            or snapshot.get("limits") != {
                "max_queries": limits.max_queries,
                "max_pages_per_query": limits.max_pages_per_query,
                "max_requests": limits.max_requests,
                "max_retries_per_page": limits.max_retries_per_page,
                "deadline_seconds": limits.deadline_seconds,
                "max_results": limits.max_results,
            }
            or not isinstance(snapshot.get("started_at"), str)
            or not isinstance(snapshot.get("queries"), list)
            or not isinstance(snapshot.get("records"), list)
            or type(snapshot.get("records_omitted")) is not int
            or snapshot["records_omitted"] < 0):
        raise ValueError("collector checkpoint scope mismatch")
    started = _parse_datetime(snapshot["started_at"])
    expected_contexts = (build_contexts(plan, source, started)
                         if source in {GOOGLE_SOURCE_ID, BING_SOURCE_ID}
                         else build_execution_plan(plan, started).queries)
    if len(expected_contexts) != len(contexts):
        raise ValueError("collector checkpoint query scope mismatch")
    queries = snapshot["queries"]
    if len(queries) != len(contexts):
        raise ValueError("collector checkpoint query count mismatch")
    restored = []
    for context, item in zip(expected_contexts, queries):
        if (not isinstance(item, dict) or item.get("query_fingerprint") != context.query_fingerprint
                or type(item.get("page_number")) is not int or item["page_number"] < 1
                or type(item.get("pages")) is not int or item["pages"] < 0
                or type(item.get("accepted")) is not int or item["accepted"] < 0
                or type(item.get("rejected")) is not int or item["rejected"] < 0
                or type(item.get("rank_offset")) is not int or item["rank_offset"] < 0
                or type(item.get("done")) is not bool
                or not isinstance(item.get("seen_cursors"), list)
                or any(not ((isinstance(cursor, str) and len(cursor) <= 4096)
                            or type(cursor) is int and cursor >= 0)
                       for cursor in item.get("seen_cursors", []))
                or not isinstance(item.get("seen_pages"), list)
                or any(not isinstance(digest, str) or len(digest) != 64
                       or any(char not in "0123456789abcdef" for char in digest)
                       for digest in item.get("seen_pages", []))
                or not isinstance(item.get("reason"), str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", item.get("reason", ""))
                or (item.get("retry_at") is not None
                    and not isinstance(item.get("retry_at"), str))):
            raise ValueError("invalid collector query checkpoint")
        cursor_value = item.get("cursor")
        if source in {GOOGLE_SOURCE_ID, BING_SOURCE_ID}:
            if type(cursor_value) is not int:
                raise ValueError("invalid collector offset checkpoint")
            cursor = ScopedOffset(cursor_value, context.query_fingerprint)
            validate_offset(context, cursor)
        else:
            if (cursor_value is not None
                    and (not isinstance(cursor_value, str)
                         or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,2048}", cursor_value))):
                raise ValueError("invalid collector page token checkpoint")
            if any(not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,2048}", token)
                   for token in item["seen_cursors"]):
                raise ValueError("invalid collector seen-token checkpoint")
            cursor = (ScopedPageToken(cursor_value, context.context_key)
                      if cursor_value is not None else None)
        if not item["done"] and item["pages"] > 0 and cursor is None:
            raise ValueError("incomplete collector checkpoint has no cursor")
        restored.append({**item, "cursor": cursor,
                         "retry_at": _parse_datetime(item.get("retry_at"), optional=True)})
    records = [_restore_record(source, item) for item in snapshot["records"]]
    return started, restored, records, snapshot["records_omitted"]


class GuardedQueryCollector:
    def __init__(self, gateway: LivePageGateway, *, utcnow=lambda: datetime.now(timezone.utc),
                 monotonic=time.monotonic, sleep=asyncio.sleep):
        if not isinstance(gateway, LivePageGateway) or not all(callable(v) for v in (utcnow, monotonic, sleep)):
            raise ValueError("gateway and bounded clock dependencies required")
        self.gateway, self.utcnow, self.monotonic, self.sleep = gateway, utcnow, monotonic, sleep

    async def collect(self, *, plan: QueryPlan, limits: CollectionLimits,
                      job_id: str, owner: str, fence: int,
                      heartbeat=lambda: None, checkpoint=None,
                      resume_snapshot=None) -> CollectionResult:
        if not isinstance(plan, QueryPlan) or get_query_plan(plan.plan_id, plan.version) != plan:
            raise ValueError("registered server plan required")
        if plan.fingerprint != self.gateway.runner.admission.policy.plan_fingerprint:
            raise ValueError("plan differs from admission policy")
        if (not isinstance(limits, CollectionLimits) or not callable(heartbeat)
                or (checkpoint is not None and not callable(checkpoint))):
            raise ValueError("validated limits required")
        limits.validate(plan)
        source = self.gateway.source_id
        started = self.utcnow()
        contexts = (build_contexts(plan, source, started) if source in {GOOGLE_SOURCE_ID, BING_SOURCE_ID}
                    else build_execution_plan(plan, started).queries)
        restored = _restore_checkpoint(source, self.gateway.data_mode, plan, limits, contexts,
                                       resume_snapshot)
        if restored is not None:
            started, query_states, restored_records, checkpoint_records_omitted = restored
            contexts = (build_contexts(plan, source, started)
                        if source in {GOOGLE_SOURCE_ID, BING_SOURCE_ID}
                        else build_execution_plan(plan, started).queries)
        else:
            query_states, restored_records, checkpoint_records_omitted = [], [], 0
            for context in contexts:
                cursor = initial_offset(context) if source != YOUTUBE_SOURCE_ID else None
                query_states.append({
                    "query_fingerprint": context.query_fingerprint, "page_number": 1,
                    "cursor": cursor, "pages": 0, "accepted": 0, "rejected": 0,
                    "rank_offset": 0, "seen_cursors": [], "seen_pages": [],
                    "reason": "PAGE_CAP_REACHED", "retry_at": None, "done": False,
                })
        deadline = self.monotonic() + limits.deadline_seconds
        attempted = 0
        progress, records = [], {}
        for record in restored_records:
            key = (record.canonical_url if source in {GOOGLE_SOURCE_ID, BING_SOURCE_ID}
                   else (record.provider_id, record.provider_video_id))
            records[key] = record
        global_stop = None
        for query_index, context in enumerate(contexts[:limits.max_queries]):
            state = query_states[query_index]
            if global_stop:
                progress.append(QueryProgress(context.query_fingerprint, 0, 0, 0, global_stop))
                continue
            cursor = state["cursor"]
            seen_cursors = set(state["seen_cursors"])
            seen_pages = set(state["seen_pages"])
            pages, accepted, rejected = state["pages"], state["accepted"], state["rejected"]
            rank_offset = state["rank_offset"]
            reason, retry_at = state["reason"], state["retry_at"]
            if state["done"]:
                progress.append(QueryProgress(context.query_fingerprint, pages, accepted, rejected,
                                              reason, retry_at))
                continue
            for page_number in range(state["page_number"], limits.max_pages_per_query + 1):
                page = None
                for retry in range(limits.max_retries_per_page + 1):
                    remaining = deadline - self.monotonic()
                    if remaining <= 0:
                        reason = global_stop = "DEADLINE_EXCEEDED"
                        break
                    if attempted >= limits.max_requests:
                        reason = global_stop = "REQUEST_BUDGET_EXHAUSTED"
                        break
                    heartbeat()
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
                    state.update({"reason": reason, "done": True})
                    break
                seen_pages.add(page_digest)
                next_cursor = None
                page_done = False
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
                        page_done = True
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
                if page_done:
                    pass
                elif next_cursor is None:
                    reason = "END_OF_PAGES" if not rejected else "REJECTED_RECORDS"
                    page_done = True
                elif next_cursor.value in seen_cursors:
                    reason = "REPEATED_PAGE_CURSOR"
                    page_done = True
                else:
                    seen_cursors.add(next_cursor.value)
                    cursor = next_cursor
                    reason = "PAGE_CAP_REACHED"
                state.update({
                    "query_fingerprint": context.query_fingerprint,
                    "page_number": page_number + 1,
                    "cursor": cursor,
                    "pages": pages,
                    "accepted": accepted,
                    "rejected": rejected,
                    "rank_offset": rank_offset,
                    "seen_cursors": sorted(seen_cursors),
                    "seen_pages": sorted(seen_pages),
                    "reason": reason,
                    "retry_at": retry_at,
                    "done": page_done,
                })
                if checkpoint is not None:
                    checkpoint_records, newly_omitted = _bounded_checkpoint_records(records.values())
                    checkpoint(_checkpoint_json({
                        "schema": 1,
                        "source_id": source,
                        "data_mode": self.gateway.data_mode,
                        "plan_fingerprint": plan.fingerprint,
                        "limits": {
                            "max_queries": limits.max_queries,
                            "max_pages_per_query": limits.max_pages_per_query,
                            "max_requests": limits.max_requests,
                            "max_retries_per_page": limits.max_retries_per_page,
                            "deadline_seconds": limits.deadline_seconds,
                            "max_results": limits.max_results,
                        },
                        "started_at": started,
                        "queries": [{**entry,
                                     "cursor": (entry["cursor"].value
                                                if entry["cursor"] is not None else None)}
                                    for entry in query_states],
                        "records": checkpoint_records,
                        "records_omitted": checkpoint_records_omitted + newly_omitted,
                    }))
                if page_done:
                    break
            progress.append(QueryProgress(context.query_fingerprint, pages, accepted, rejected, reason, retry_at))
        all_selected = limits.max_queries == len(contexts)
        complete = all_selected and all(row.reason == "END_OF_PAGES" for row in progress)
        status = "SUCCESS" if complete else ("PARTIAL" if any(row.pages for row in progress) else "FAILED")
        source_stop = next((row.reason for row in progress if row.reason != "END_OF_PAGES"), None)
        observed_mode = self.gateway.data_mode if any(row.pages for row in progress) else None
        return CollectionResult(source, observed_mode, status, complete, attempted,
            tuple(progress), tuple(records.values()), global_stop or source_stop or (
                "QUERY_CAP_REACHED" if not all_selected else None), checkpoint_records_omitted)
