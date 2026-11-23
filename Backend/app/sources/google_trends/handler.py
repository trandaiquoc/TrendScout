"""Guarded Trends discovery and optional single-candidate SERP verification."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone

from app.jobs.public_admission import (
    MANUAL_SOURCE_REFRESH_MARKER,
    OWNER_FRESH_ACCEPTANCE_MARKER,
    AdmissionDenied,
)
from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.sources.catalog import GOOGLE_SOURCE_ID, source_capability
from app.sources.google_trends.trending_now import (
    TRENDING_NOW_PARSER_VERSION,
    GoogleTrendsRequest,
    normalize_trending_candidates,
)
from app.sources.google_trends.verification_plan import (
    MAX_VERIFICATION_QUERIES,
    build_verification_plan,
    select_verification_candidates,
)
from app.sources.live_gateway import GatewayError
from app.sources.refresh_eligibility import (
    GOOGLE_REFRESH_STATE_KEY,
    OWNER_ACCEPTANCE_SLOT_PREFIX,
    candidate_slot,
    manual_candidate_slot,
    manual_schedule_metadata,
    reserve_manual_source_slot,
    reserve_owner_acceptance_slot,
    reserve_source_slot,
)
from app.sources.serpapi.errors import SerpApiError
from app.sources.serpapi.normalization import normalize_items
from app.sources.serpapi.pagination import initial_offset

_MAX_NORMALIZED_BYTES = 3_000_000


def _json_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8"))


def _safe_error_code(error: BaseException) -> str:
    value = getattr(error, "code", "SOURCE_EXECUTION_FAILED")
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value) else "SOURCE_EXECUTION_FAILED"


def _retry_after(error: BaseException) -> int | None:
    value = getattr(error, "retry_after_unbounded_seconds",
                    getattr(error, "retry_after", None))
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        return None
    # Preserve provider delays rather than shortening them. Large values stay
    # internal until the admission cooldown can represent them safely.
    return min(31_536_000, max(1, math.ceil(value)))


def trending_now_cache_scope(policy_fingerprint: str, grant: CacheGrant):
    """Return the canonical scope used by both Google discovery and shared readers."""
    if (not isinstance(policy_fingerprint, str) or len(policy_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in policy_fingerprint)
            or not isinstance(grant, CacheGrant) or grant.source_id != GOOGLE_SOURCE_ID):
        return None
    capability = source_capability(GOOGLE_SOURCE_ID)
    return CacheScope(
        source_id=GOOGLE_SOURCE_ID,
        provider_id=capability.provider_id,
        query="google-trends-trending-now:US:en:24h",
        plan_fingerprint=policy_fingerprint,
        market="US",
        language="en",
        window="24h",
        parser_version=TRENDING_NOW_PARSER_VERSION,
        policy_version=grant.policy_version,
    )


def _serialize_serp_page(context, page, *, fetched_at: datetime, data_mode: str) -> dict:
    normalized, rejected = normalize_items(
        context, page.items, fetched_at=fetched_at, page=1, offset=0,
        http_status=page.http_status, cache_state=page.cache_state,
        provider_created_at=page.provider_created_at, data_mode=data_mode,
    )
    rows = []
    for record in normalized:
        evidence = record.evidence[0]
        rows.append({
            "canonical_url": record.canonical_url,
            "title": record.title,
            "snippet": record.snippet,
            "provider_rank": evidence.position,
            "fetched_at": record.fetched_at.isoformat(),
            "provider_created_at": evidence.provider_created_at.isoformat()
                if evidence.provider_created_at else None,
            "cache_state": evidence.cache_state,
            "quality_flags": list(record.quality_flags),
        })
    return {
        "status": "VERIFIED",
        "provider_id": source_capability(GOOGLE_SOURCE_ID).provider_id,
        "verification_mode": "GOOGLE_SERP",
        "plan_id": context.plan_id,
        "plan_version": context.plan_version,
        "query": context.canonical_query,
        "market": "US",
        "language": "en",
        "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(),
        "provider_created_at": page.provider_created_at.isoformat()
            if page.provider_created_at else None,
        "delivery_state": "CACHED" if page.cache_state == "CACHED" else "FRESH",
        "result_count": len(rows),
        "rejected_result_count": len(rejected),
        "results": rows,
        "limitations": ["SERP_POSITION_IS_NOT_SEARCH_VOLUME", "SERP_IS_NOT_COMMERCIAL_DEMAND"],
    }


class GoogleTrendsDiscoveryHandler:
    """Serve validated discovery cache, then optionally refresh/verify candidates.

    A LIVE cache remains useful when the paid discovery gateway is not
    configured. In that case this handler is cache-only and reports an
    unavailable source on a cache miss; it never turns an empty list into a
    successful Trends response.
    """

    def __init__(self, *, trends_gateway=None, policy_fingerprint: str,
                 utcnow=lambda: datetime.now(timezone.utc), cache: SharedCache | None = None,
                 resolve_cache_grant=None, serp_gateway=None, force_refresh: bool = False,
                 data_mode: str | None = None,
                 atomic_store=None, allow_initial_acceptance: bool = False,
                 ensure_paid_budget=None,
                 remaining_paid_attempts=None,
                 previously_observed=lambda: frozenset(),
                 recently_verified=lambda: frozenset(),
                 previous_verification=lambda: {}):
        if ((trends_gateway is not None
                and not callable(getattr(trends_gateway, "fetch_page", None)))
                or not isinstance(policy_fingerprint, str) or len(policy_fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in policy_fingerprint)
                or not callable(utcnow) or (cache is None) != (resolve_cache_grant is None)
                or (trends_gateway is None and cache is None)
                or resolve_cache_grant is not None and not callable(resolve_cache_grant)
                or serp_gateway is not None and not callable(getattr(serp_gateway, "fetch_page", None))
                or data_mode not in {None, "LIVE", "SYNTHETIC"}
                or type(force_refresh) is not bool
                or type(allow_initial_acceptance) is not bool
                or (atomic_store is not None
                    and not callable(getattr(atomic_store, "transact", None)))
                or (ensure_paid_budget is not None and not callable(ensure_paid_budget))
                or (remaining_paid_attempts is not None
                    and not callable(remaining_paid_attempts))
                or not callable(previously_observed) or not callable(recently_verified)
                or not callable(previous_verification)):
            raise ValueError("validated Trends gateways and bounded server policy required")
        self.trends_gateway = trends_gateway
        self.data_mode = data_mode or getattr(trends_gateway, "data_mode", None)
        if self.data_mode not in {"LIVE", "SYNTHETIC"}:
            raise ValueError("a bounded source data mode is required")
        self.serp_gateway = serp_gateway
        self.policy_fingerprint = policy_fingerprint
        self.utcnow = utcnow
        self.cache = cache
        self.resolve_cache_grant = resolve_cache_grant
        self.force_refresh = force_refresh
        self.atomic_store = atomic_store
        self.allow_initial_acceptance = allow_initial_acceptance
        self.ensure_paid_budget = ensure_paid_budget
        self.remaining_paid_attempts = remaining_paid_attempts
        self.previously_observed = previously_observed
        self.recently_verified = recently_verified
        self.previous_verification = previous_verification

    def _cache_scope(self, context):
        now = self.utcnow()
        grant = self.resolve_cache_grant(GOOGLE_SOURCE_ID, now)
        if not isinstance(grant, CacheGrant) or grant.source_id != GOOGLE_SOURCE_ID:
            return None, None
        return trending_now_cache_scope(self.policy_fingerprint, grant), grant

    def cached_result_for_fallback(self, context):
        """Return validated LIVE cache data for a quota/deadline fallback."""
        if self.cache is None:
            return None
        try:
            cache_scope, cache_grant = self._cache_scope(context)
            return self._read_cached_result(cache_scope, cache_grant, self.utcnow())
        except Exception:
            return None

    def _read_cached_result(self, cache_scope, cache_grant, now):
        if self.cache is None or cache_scope is None:
            return None
        try:
            cached = self.cache.get(cache_scope, grant=cache_grant, now=now)
            if not isinstance(cached, dict) or cached.get("status") not in {"CACHED", "STALE"}:
                return None
            payload = cached.get("payload")
            records = payload.get("records") if isinstance(payload, dict) else None
            if (not isinstance(payload, dict) or payload.get("data_mode") != self.data_mode
                    or payload.get("status") not in {"SUCCESS", "PARTIAL"}
                    or not isinstance(records, list)
                    or type(payload.get("record_count")) is not int
                    or payload["record_count"] != len(records)):
                return None

            delivery_state = "CACHED" if cached["status"] == "CACHED" else "STALE_CACHE"
            coverage = payload.get("coverage")
            coverage = dict(coverage) if isinstance(coverage, dict) else {}
            coverage.pop("owner_fresh_acceptance", None)
            originating_count = coverage.get("provider_request_count")
            if type(originating_count) is not int or originating_count < 0:
                originating_count = payload.get("provider_request_count")
            if type(originating_count) is int and originating_count >= 0:
                coverage["cached_provider_request_count"] = originating_count
            coverage["provider_request_count"] = 0
            coverage["attempted_calls"] = 0
            coverage["cached_serp_verification_attempted"] = (
                coverage.get("serp_verification_attempted", 0)
            )
            coverage["serp_verification_attempted"] = 0
            coverage["cache_fetched_at"] = cached.get("fetched_at")
            return {
                **payload,
                "delivery_state": delivery_state,
                "shared_cache_state": cached["status"],
                "shared_cache_fetched_at": cached.get("fetched_at"),
                "provider_request_count": 0,
                "coverage": coverage,
                "records": [
                    ({**record, "delivery_state": delivery_state}
                     if isinstance(record, dict) else record)
                    for record in records
                ],
            }
        except Exception:
            return None

    async def __call__(self, context):
        if context.source_id != GOOGLE_SOURCE_ID:
            raise ValueError("Google Trends handler received a different source")
        acceptance_fresh = (
            isinstance(context.request_metadata, dict)
            and context.request_metadata.get("owner_fresh_acceptance")
            == OWNER_FRESH_ACCEPTANCE_MARKER
        )
        manual_refresh = (
            isinstance(context.request_metadata, dict)
            and context.request_metadata.get("manual_source_refresh")
            == MANUAL_SOURCE_REFRESH_MARKER
        )
        progress = ((context.resume_snapshot or {}).get("progress")
                    if isinstance(context.resume_snapshot, dict) else None)
        saved = progress.get("google_trends_receipt") if isinstance(progress, dict) else None
        has_saved_result = isinstance(saved, dict) and isinstance(saved.get("result"), dict)
        cache_scope, cache_grant = (None, None)
        cached_result = None
        cache_state = "UNAVAILABLE"
        if self.cache is not None and not has_saved_result and not acceptance_fresh:
            try:
                cache_scope, cache_grant = self._cache_scope(context)
                cached_result = self._read_cached_result(
                    cache_scope, cache_grant, self.utcnow(),
                )
            except Exception:
                cache_scope, cache_grant, cached_result = None, None, None
            if cached_result is not None:
                cache_state = cached_result.get("shared_cache_state", "UNAVAILABLE")
        elif self.cache is not None:
            # Acceptance runs and resumed receipts skip cache reads, but still
            # need the approved scope/grant to persist the fresh receipt.
            try:
                cache_scope, cache_grant = self._cache_scope(context)
            except Exception:
                cache_scope, cache_grant = None, None

        result = None
        account_quota = None
        resumed_receipt = False
        provider_fetched = False
        provider_attempted = False
        eligible_slot = None
        cache_reused = False
        if has_saved_result:
            result = saved["result"]
            if (result.get("data_mode") != self.data_mode
                    or not isinstance(result.get("records"), list)):
                raise ValueError("saved Google Trends result is invalid")
            resumed_receipt = True
        elif cached_result is not None and cache_state == "CACHED":
            result = cached_result
        elif self.trends_gateway is None:
            if cached_result is not None:
                result = cached_result
            else:
                return {
                    "status": "UNAVAILABLE", "data_mode": self.data_mode,
                    "records": [], "record_count": 0,
                    "error_code": "SOURCE_NOT_CONFIGURED",
                    "coverage": {
                        "discovery_mode": "SERPAPI_TRENDING_NOW",
                        "provider_id": "serpapi_google_trends_trending_now",
                        "attempted_calls": 0,
                        "cache_state": "UNAVAILABLE",
                    },
                }
        elif self.atomic_store is None:
            if cached_result is not None:
                result = cached_result
            else:
                return {
                    "status": "UNAVAILABLE", "data_mode": self.data_mode,
                    "records": [], "record_count": 0,
                    "error_code": "SCHEDULE_STATE_UNAVAILABLE",
                    "coverage": {"discovery_mode": "SERPAPI_TRENDING_NOW",
                                 "attempted_calls": 0},
                }
        else:
            try:
                eligible_slot = (
                    f"{OWNER_ACCEPTANCE_SLOT_PREFIX}{context.job_id}"
                    if acceptance_fresh else manual_candidate_slot(
                        self.atomic_store, GOOGLE_REFRESH_STATE_KEY, self.utcnow(),
                        job_id=context.job_id,
                        error_code="GOOGLE_SCHEDULE_STATE_INVALID",
                    ) if manual_refresh else candidate_slot(
                        self.atomic_store, GOOGLE_REFRESH_STATE_KEY, self.utcnow(),
                        error_code="GOOGLE_SCHEDULE_STATE_INVALID",
                        allow_initial_acceptance=self.allow_initial_acceptance,
                    )
                )
            except AdmissionDenied as error:
                return {
                    "status": "FAILED", "data_mode": self.data_mode,
                    "records": [], "record_count": 0, "error_code": error.code,
                    "coverage": {"discovery_mode": "SERPAPI_TRENDING_NOW",
                                 "attempted_calls": 0},
                }
            if eligible_slot is None:
                error_code = ("MANUAL_REFRESH_COOLDOWN" if manual_refresh
                              else "NOT_YET_ELIGIBLE")
                coverage = {"discovery_mode": "SERPAPI_TRENDING_NOW",
                            "attempted_calls": 0,
                            "next_slots_timezone": "Asia/Ho_Chi_Minh"}
                if manual_refresh:
                    try:
                        coverage["next_eligible_at"] = manual_schedule_metadata(
                            self.atomic_store, GOOGLE_REFRESH_STATE_KEY, self.utcnow(),
                            error_code="GOOGLE_SCHEDULE_STATE_INVALID",
                        ).get("next_eligible_at")
                    except AdmissionDenied as error:
                        return {
                            "status": "FAILED", "data_mode": self.data_mode,
                            "records": [], "record_count": 0,
                            "error_code": error.code, "coverage": coverage,
                        }
                if cached_result is not None:
                    cached_result["coverage"] = {
                        **cached_result.get("coverage", {}),
                        "refresh_state": error_code,
                        **coverage,
                    }
                    result = cached_result
                else:
                    return {
                        "status": "UNAVAILABLE", "data_mode": self.data_mode,
                        "records": [], "record_count": 0,
                        "error_code": error_code, "coverage": coverage,
                    }
            else:
                request = GoogleTrendsRequest(
                    source_id=GOOGLE_SOURCE_ID,
                    policy_fingerprint=self.policy_fingerprint,
                    force_refresh=self.force_refresh or acceptance_fresh or manual_refresh,
                )
                attempt_id = hashlib.sha256(
                    f"{context.job_id}:trends:{request.plan_id}:{request.plan_version}".encode()
                ).hexdigest()

                def save_trends_receipt(gateway_page):
                    fetched_at = self.utcnow()
                    page = gateway_page.page
                    records = normalize_trending_candidates(
                        page, fetched_at=fetched_at, data_mode=gateway_page.data_mode
                    )
                    status = ("PARTIAL" if page.rejected_record_count else
                              "SUCCESS" if records else "UNAVAILABLE")
                    result = {
                        "status": status,
                        "data_mode": gateway_page.data_mode,
                        "delivery_state": page.delivery_state,
                        "records": records,
                        "record_count": len(records),
                        **({"error_code": "NO_DATA"} if not records else {}),
                        "coverage": {
                            "discovery_mode": "SERPAPI_TRENDING_NOW",
                            "provider_id": "serpapi_google_trends_trending_now",
                            "transport_id": "serpapi",
                            "provider_record_count": page.provider_record_count,
                            "accepted_record_count": len(page.records),
                            "rejected_record_count": page.rejected_record_count,
                            "provider_status": page.provider_status,
                            "http_status": page.http_status,
                            "provider_created_at": page.provider_created_at.isoformat()
                                if page.provider_created_at else None,
                            "provider_data_at": page.provider_data_at.isoformat()
                                if page.provider_data_at else None,
                            "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(),
                            "source_url": records[0]["source_url"] if records else None,
                            "eligible_slot": eligible_slot,
                            "attempted_calls": 1,
                            "provider_request_count": 1,
                            "stop_reason": "REJECTED_PROVIDER_RECORDS"
                                if page.rejected_record_count else None,
                        },
                        "quality_flags": ["PROVIDER_APPROXIMATE_BUCKET", "NO_COMPATIBLE_BASELINE"],
                        "provider_request_count": 1,
                    }
                    if acceptance_fresh:
                        result["coverage"]["owner_fresh_acceptance"] = True
                    context.checkpoint({"google_trends_receipt": {"result": result}})
                    receipt_box.append(result)

                receipt_box = []
                try:
                    account_quota = (await self.ensure_paid_budget()
                                     if self.ensure_paid_budget is not None else None)
                    try:
                        eligible_slot = (
                            reserve_owner_acceptance_slot(
                                self.atomic_store, GOOGLE_REFRESH_STATE_KEY, self.utcnow(),
                                job_id=context.job_id,
                                error_code="GOOGLE_SCHEDULE_STATE_INVALID",
                            ) if acceptance_fresh else reserve_manual_source_slot(
                                self.atomic_store, GOOGLE_REFRESH_STATE_KEY, self.utcnow(),
                                job_id=context.job_id,
                                error_code="GOOGLE_SCHEDULE_STATE_INVALID",
                            ) if manual_refresh else reserve_source_slot(
                                self.atomic_store, GOOGLE_REFRESH_STATE_KEY, self.utcnow(),
                                error_code="GOOGLE_SCHEDULE_STATE_INVALID",
                                allow_initial_acceptance=self.allow_initial_acceptance,
                            )
                        )
                    except AdmissionDenied as error:
                        return {
                            "status": "FAILED", "data_mode": self.data_mode,
                            "records": [], "record_count": 0, "error_code": error.code,
                            "coverage": {"discovery_mode": "SERPAPI_TRENDING_NOW",
                                         "attempted_calls": 0},
                        }
                    if eligible_slot is None:
                        error_code = ("MANUAL_REFRESH_COOLDOWN" if manual_refresh
                                      else "NOT_YET_ELIGIBLE")
                        coverage = {"discovery_mode": "SERPAPI_TRENDING_NOW",
                                    "attempted_calls": 0,
                                    "next_slots_timezone": "Asia/Ho_Chi_Minh"}
                        if manual_refresh:
                            try:
                                coverage["next_eligible_at"] = manual_schedule_metadata(
                                    self.atomic_store, GOOGLE_REFRESH_STATE_KEY,
                                    self.utcnow(),
                                    error_code="GOOGLE_SCHEDULE_STATE_INVALID",
                                ).get("next_eligible_at")
                            except AdmissionDenied as error:
                                return {
                                    "status": "FAILED", "data_mode": self.data_mode,
                                    "records": [], "record_count": 0,
                                    "error_code": error.code, "coverage": coverage,
                                }
                        if cached_result is not None:
                            cached_result["coverage"] = {
                                **cached_result.get("coverage", {}),
                                "refresh_state": error_code,
                                **coverage,
                            }
                            result = cached_result
                        else:
                            return {
                                "status": "UNAVAILABLE", "data_mode": self.data_mode,
                                "records": [], "record_count": 0,
                                "error_code": error_code, "coverage": coverage,
                            }
                    else:
                        provider_attempted = True
                        await self.trends_gateway.fetch_page(
                            context=request, cursor=None, job_id=context.job_id,
                            owner=context.owner, fence=context.fence, attempt_id=attempt_id,
                            timeout_seconds=15, on_page_receipt=save_trends_receipt,
                        )
                        if not receipt_box:
                            raise ValueError("Google Trends receipt was not checkpointed")
                        result = receipt_box[0]
                        provider_fetched = True
                except (GatewayError, SerpApiError, AdmissionDenied) as error:
                    if isinstance(error, AdmissionDenied) and error.code == "LEASE_LOST":
                        raise
                    coverage = {
                        "discovery_mode": "SERPAPI_TRENDING_NOW",
                        "attempted_calls": 1 if provider_attempted else 0,
                        "records_omitted_storage_limit": 0,
                        "eligible_slot": eligible_slot,
                        **({"serpapi_account_quota": account_quota}
                           if account_quota is not None else {}),
                    }
                    request_count = 0 if not provider_attempted else None
                    if provider_attempted:
                        missing = object()
                        started_at = getattr(
                            self.trends_gateway, "last_provider_request_started_at", missing,
                        )
                        if started_at is not missing:
                            request_count = int(started_at is not None)
                    if request_count is not None:
                        coverage["provider_request_count"] = request_count
                    diagnostic = getattr(error, "diagnostic", None)
                    if diagnostic is not None:
                        if diagnostic.failure_phase == "RESULT_PERSISTENCE":
                            coverage["persistence_diagnostic"] = {
                                "source_id": diagnostic.source_id,
                                "failure_phase": diagnostic.failure_phase,
                                "failure_reason": diagnostic.failure_reason,
                                "exception_class": diagnostic.exception_class,
                                "attempt_id": diagnostic.attempt_id,
                                "error_code": diagnostic.error_code,
                            }
                        else:
                            coverage["transport_diagnostic"] = {
                                "source_id": diagnostic.source_id,
                                "failure_phase": diagnostic.failure_phase,
                                "http_status": diagnostic.http_status,
                                "exception_class": diagnostic.exception_class,
                                "bytes_received": diagnostic.bytes_received,
                                "response_size_limit": diagnostic.response_size_limit,
                                "attempt_id": diagnostic.attempt_id,
                                "error_code": diagnostic.error_code,
                            }
                    failure = {
                        "status": "FAILED", "data_mode": self.data_mode,
                        "records": [], "record_count": 0, "error_code": _safe_error_code(error),
                        **({"retry_after_seconds": _retry_after(error)}
                           if _retry_after(error) is not None else {}),
                        "coverage": coverage,
                    }
                    if request_count is not None:
                        failure["provider_request_count"] = request_count
                    return failure
        cache_reused = cached_result is not None and not provider_fetched and not has_saved_result
        records = [dict(item) for item in result["records"]]
        coverage = dict(result.get("coverage", {}))
        quality_flags = list(result.get("quality_flags", []))
        if cached_result is not None and not provider_fetched:
            coverage["shared_cache_state"] = cached_result.get("shared_cache_state")
            coverage["shared_cache_fetched_at"] = cached_result.get("shared_cache_fetched_at")
            if cached_result.get("shared_cache_state") == "STALE":
                quality_flags.append("STALE_SHARED_CACHE_REUSED")
                for record in records:
                    record["quality_flags"] = sorted(set(
                        [*record.get("quality_flags", []), "STALE_SHARED_CACHE_REUSED"]
                    ))
        try:
            saved_verification = self.previous_verification()
        except Exception:
            saved_verification = {}
        if not isinstance(saved_verification, dict):
            saved_verification = {}
        for record in records:
            identity = record.get("canonical_keyword_identity")
            previous = saved_verification.get(identity)
            current = record.get("serp_verification")
            if isinstance(current, dict) and current.get("status") in {"VERIFIED", "FAILED"}:
                continue
            if (isinstance(previous, dict)
                    and previous.get("status") in {"VERIFIED", "FAILED"}):
                record["serp_verification"] = previous
            else:
                record["serp_verification"] = {"status": "NOT_CONFIGURED", "results": []}
        verification_attempted = 0
        verification_failed = 0
        admission_blocked = 0
        if resumed_receipt:
            # A worker may resume after an uncertain provider response. Keep the
            # completed candidate attempts and do not select them again.
            verification_attempted = sum(
                1 for item in records
                if isinstance(item.get("serp_verification"), dict)
                and item["serp_verification"].get("status") in {"VERIFIED", "FAILED"}
            )
            verification_failed = sum(
                1 for item in records
                if isinstance(item.get("serp_verification"), dict)
                and item["serp_verification"].get("status") == "FAILED"
            )
        if self.serp_gateway is not None and not cache_reused:
            try:
                old = self.previously_observed()
                recent = self.recently_verified()
                attempted = frozenset(
                    item.get("canonical_keyword_identity") for item in records
                    if isinstance(item.get("serp_verification"), dict)
                    and item["serp_verification"].get("status") in {"VERIFIED", "FAILED"}
                    and isinstance(item.get("canonical_keyword_identity"), str)
                )
                recent = recent if isinstance(recent, frozenset) else frozenset()
                query_limit = max(0, MAX_VERIFICATION_QUERIES - verification_attempted)
                if self.remaining_paid_attempts is not None:
                    try:
                        remaining = self.remaining_paid_attempts()
                        if type(remaining) is not int or remaining < 0:
                            raise ValueError("invalid local quota remainder")
                        query_limit = min(query_limit, remaining)
                    except Exception:
                        query_limit = 0
                        quality_flags.append("SERP_LOCAL_QUOTA_UNAVAILABLE")
                selected = select_verification_candidates(
                    tuple(records),
                    previously_observed=old if isinstance(old, frozenset) else frozenset(),
                    recently_verified=recent | attempted,
                    limit=query_limit,
                )
            except Exception:
                selected = ()
                quality_flags.append("VERIFICATION_SELECTION_UNAVAILABLE")
            for candidate in selected:
                single = build_verification_plan(
                    (candidate,), policy_fingerprint=self.policy_fingerprint,
                    started_at=self.utcnow(),
                )
                serp_context = single.contexts[0]
                attempt_id = hashlib.sha256(
                    f"{context.job_id}:serp:{serp_context.query_fingerprint}".encode()
                ).hexdigest()
                try:
                    account_quota = (await self.ensure_paid_budget()
                                     if self.ensure_paid_budget is not None else account_quota)
                    verification_attempted += 1
                    gateway_page = await self.serp_gateway.fetch_page(
                        context=serp_context, cursor=initial_offset(serp_context),
                        job_id=context.job_id, owner=context.owner, fence=context.fence,
                        attempt_id=attempt_id, timeout_seconds=15,
                        **({"force_refresh": True}
                           if acceptance_fresh or manual_refresh else {}),
                    )
                    verification = _serialize_serp_page(
                        serp_context, gateway_page.page, fetched_at=self.utcnow(),
                        data_mode=gateway_page.data_mode,
                    )
                except (GatewayError, SerpApiError) as error:
                    verification_failed += 1
                    verification = {"status": "FAILED", "verification_mode": "GOOGLE_SERP",
                                    "plan_id": single.plan_id, "plan_version": single.version,
                                    "query": serp_context.canonical_query,
                                    "error_code": _safe_error_code(error), "results": [],
                                    "limits": ["MAX_TWO_QUERIES_PER_BATCH", "ONE_PAGE_PER_QUERY"]}
                except AdmissionDenied as error:
                    admission_blocked += 1
                    verification_failed += 1
                    verification = {"status": "FAILED", "verification_mode": "GOOGLE_SERP",
                                    "plan_id": single.plan_id, "plan_version": single.version,
                                    "query": serp_context.canonical_query,
                                    "error_code": _safe_error_code(error), "results": [],
                                    "limits": ["MAX_TWO_QUERIES_PER_BATCH", "ONE_PAGE_PER_QUERY"]}
                for record in records:
                    if record.get("canonical_keyword_identity") == candidate.get(
                            "canonical_keyword_identity"):
                        record["serp_verification"] = verification
                        break
                # This checkpoint includes the already verified Trend rows;
                # a resumed worker will not repeat the broad discovery call.
                context.checkpoint({"google_trends_receipt": {"result": {
                    **result, "records": records, "record_count": len(records),
                }}})
        verified_count = sum(
            1 for item in records
            if isinstance(item.get("serp_verification"), dict)
            and item["serp_verification"].get("status") == "VERIFIED"
        )
        coverage.update({
            "trending_candidate_count": len(records),
            "distinct_keyword_count": len({item.get("canonical_keyword_identity") for item in records}),
            "serp_verification_attempted": verification_attempted,
            "serp_verified_candidate_count": verified_count,
            "serp_verification_failed_count": verification_failed,
            "serp_admission_blocked_count": admission_blocked,
            "serp_max_queries_per_batch": MAX_VERIFICATION_QUERIES,
            "serp_pages_per_query": 1,
        })
        if acceptance_fresh:
            coverage["owner_fresh_acceptance"] = True
        if cache_reused:
            coverage["provider_request_count"] = 0
            coverage["attempted_calls"] = 0
            verification_attempted = 0
            provider_request_count = 0
        else:
            discovery_requests = coverage.get("attempted_calls", 0)
            if type(discovery_requests) is not int or discovery_requests < 0:
                discovery_requests = 0
            provider_request_count = discovery_requests + verification_attempted
            coverage["provider_request_count"] = provider_request_count
        if account_quota is not None:
            coverage["serpapi_account_quota"] = account_quota
        accepted, omitted, used = [], 0, 2
        for record in records:
            size = _json_bytes(record) + (1 if accepted else 0)
            if len(accepted) >= 5000 or used + size > _MAX_NORMALIZED_BYTES:
                omitted += 1
                continue
            accepted.append(record)
            used += size
        if omitted:
            coverage["records_omitted_storage_limit"] = omitted
            coverage["stop_reason"] = "NORMALIZED_RESULT_SIZE_LIMIT"
            quality_flags.append("PARTIAL_STORAGE_LIMIT")
        status = ("PARTIAL" if omitted or verification_failed
                  or coverage.get("rejected_record_count", 0) else result["status"])
        output = {
            "status": status, "data_mode": result["data_mode"],
            "delivery_state": result.get("delivery_state", "FRESH"),
            "records": accepted, "record_count": len(accepted),
            "coverage": coverage, "quality_flags": sorted(set(quality_flags)),
            "provider_request_count": provider_request_count,
        }
        if ((provider_fetched or resumed_receipt)
                and output["delivery_state"] == "FRESH"
                and cache_scope is not None and cache_grant is not None
                and cache_grant.allowed):
            try:
                raw_fetched_at = coverage.get("fetched_at")
                if not isinstance(raw_fetched_at, str):
                    raise ValueError("Google Trends fetch timestamp is missing")
                fetched_at = datetime.fromisoformat(raw_fetched_at.replace("Z", "+00:00"))
                if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
                    raise ValueError("Google Trends fetch timestamp is invalid")
                self.cache.put(cache_scope, grant=cache_grant, origin=output["data_mode"], payload=output,
                               fetched_at=fetched_at, now=self.utcnow())
            except Exception:
                output["quality_flags"].append("CACHE_WRITE_FAILED")
        return output

    def __call_mode__(self):
        return self.data_mode
