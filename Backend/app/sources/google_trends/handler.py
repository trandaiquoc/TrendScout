"""Guarded Trends discovery and optional two-query SERP verification workflow."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone

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
    """One fixed Trends request, then optionally verify at most two server-selected terms."""

    def __init__(self, *, trends_gateway, policy_fingerprint: str,
                 utcnow=lambda: datetime.now(timezone.utc), cache: SharedCache | None = None,
                 resolve_cache_grant=None, serp_gateway=None, force_refresh: bool = False,
                 ensure_paid_budget=None,
                 previously_observed=lambda: frozenset(),
                 recently_verified=lambda: frozenset(),
                 previous_verification=lambda: {}):
        if (trends_gateway is None or not callable(getattr(trends_gateway, "fetch_page", None))
                or not isinstance(policy_fingerprint, str) or len(policy_fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in policy_fingerprint)
                or not callable(utcnow) or (cache is None) != (resolve_cache_grant is None)
                or resolve_cache_grant is not None and not callable(resolve_cache_grant)
                or serp_gateway is not None and not callable(getattr(serp_gateway, "fetch_page", None))
                or type(force_refresh) is not bool
                or (ensure_paid_budget is not None and not callable(ensure_paid_budget))
                or not callable(previously_observed) or not callable(recently_verified)
                or not callable(previous_verification)):
            raise ValueError("validated Trends gateways and bounded server policy required")
        self.trends_gateway = trends_gateway
        self.serp_gateway = serp_gateway
        self.policy_fingerprint = policy_fingerprint
        self.utcnow = utcnow
        self.cache = cache
        self.resolve_cache_grant = resolve_cache_grant
        self.force_refresh = force_refresh
        self.ensure_paid_budget = ensure_paid_budget
        self.previously_observed = previously_observed
        self.recently_verified = recently_verified
        self.previous_verification = previous_verification

    def _cache_scope(self, context):
        capability = source_capability(GOOGLE_SOURCE_ID)
        now = self.utcnow()
        grant = self.resolve_cache_grant(GOOGLE_SOURCE_ID, now)
        if not isinstance(grant, CacheGrant) or grant.source_id != GOOGLE_SOURCE_ID:
            return None, None
        return CacheScope(
            source_id=GOOGLE_SOURCE_ID, provider_id=capability.provider_id,
            query="google-trends-trending-now:US:en:24h",
            plan_fingerprint=self.policy_fingerprint, market="US", language="en",
            window="24h", parser_version=TRENDING_NOW_PARSER_VERSION,
            policy_version=grant.policy_version,
        ), grant

    async def __call__(self, context):
        if context.source_id != GOOGLE_SOURCE_ID:
            raise ValueError("Google Trends handler received a different source")
        cache_scope, cache_grant = (None, None)
        if self.cache is not None:
            try:
                cache_scope, cache_grant = self._cache_scope(context)
                if cache_scope is not None:
                    cached = self.cache.get(cache_scope, grant=cache_grant, now=self.utcnow())
                    if cached.get("status") == "CACHED":
                        payload = cached.get("payload")
                        if (isinstance(payload, dict)
                                and payload.get("data_mode") == self.trends_gateway.data_mode):
                            return {**payload, "delivery_state": "CACHED"}
            except Exception:
                # Cache failure does not authorize data substitution or a stale
                # result. Proceed with the one explicitly admitted source request.
                cache_scope, cache_grant = None, None

        progress = ((context.resume_snapshot or {}).get("progress")
                    if isinstance(context.resume_snapshot, dict) else None)
        saved = progress.get("google_trends_receipt") if isinstance(progress, dict) else None
        result = None
        account_quota = None
        if isinstance(saved, dict) and isinstance(saved.get("result"), dict):
            result = saved["result"]
            if (result.get("data_mode") != self.trends_gateway.data_mode
                    or not isinstance(result.get("records"), list)):
                raise ValueError("saved Google Trends result is invalid")
        else:
            request = GoogleTrendsRequest(
                source_id=GOOGLE_SOURCE_ID,
                policy_fingerprint=self.policy_fingerprint,
                force_refresh=self.force_refresh,
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
                result = {
                    "status": "PARTIAL" if page.rejected_record_count else "SUCCESS",
                    "data_mode": gateway_page.data_mode,
                    "delivery_state": page.delivery_state,
                    "records": records,
                    "record_count": len(records),
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
                        "stop_reason": "REJECTED_PROVIDER_RECORDS"
                            if page.rejected_record_count else None,
                    },
                    "quality_flags": ["PROVIDER_APPROXIMATE_BUCKET", "NO_COMPATIBLE_BASELINE"],
                }
                context.checkpoint({"google_trends_receipt": {"result": result}})
                receipt_box.append(result)

            receipt_box = []
            try:
                account_quota = (await self.ensure_paid_budget()
                                 if self.ensure_paid_budget is not None else None)
                await self.trends_gateway.fetch_page(
                    context=request, cursor=None, job_id=context.job_id,
                    owner=context.owner, fence=context.fence, attempt_id=attempt_id,
                    timeout_seconds=15, on_page_receipt=save_trends_receipt,
                )
                # The callback is the durable receipt and is invoked before the
                # attempt is settled; keep that exact object in the final result.
                if not receipt_box:
                    raise ValueError("Google Trends receipt was not checkpointed")
                result = receipt_box[0]
            except (GatewayError, SerpApiError) as error:
                return {
                    "status": "FAILED", "data_mode": self.trends_gateway.data_mode, "records": [],
                    "record_count": 0, "error_code": _safe_error_code(error),
                    **({"retry_after_seconds": _retry_after(error)}
                       if _retry_after(error) is not None else {}),
                    "coverage": {"discovery_mode": "SERPAPI_TRENDING_NOW",
                                 "attempted_calls": 1, "records_omitted_storage_limit": 0,
                                 **({"serpapi_account_quota": account_quota}
                                    if account_quota is not None else {})},
                }
        records = [dict(item) for item in result["records"]]
        coverage = dict(result.get("coverage", {}))
        quality_flags = list(result.get("quality_flags", []))
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
        if self.serp_gateway is not None:
            try:
                old = self.previously_observed()
                recent = self.recently_verified()
                selected = select_verification_candidates(
                    tuple(records),
                    previously_observed=old if isinstance(old, frozenset) else frozenset(),
                    recently_verified=recent if isinstance(recent, frozenset) else frozenset(),
                    limit=MAX_VERIFICATION_QUERIES,
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
                verification_attempted += 1
                try:
                    account_quota = (await self.ensure_paid_budget()
                                     if self.ensure_paid_budget is not None else account_quota)
                    gateway_page = await self.serp_gateway.fetch_page(
                        context=serp_context, cursor=initial_offset(serp_context),
                        job_id=context.job_id, owner=context.owner, fence=context.fence,
                        attempt_id=attempt_id, timeout_seconds=15,
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
            "serp_max_queries_per_batch": MAX_VERIFICATION_QUERIES,
            "serp_pages_per_query": 1,
        })
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
        }
        if cache_scope is not None and cache_grant is not None and cache_grant.allowed:
            try:
                self.cache.put(cache_scope, grant=cache_grant, origin=output["data_mode"], payload=output,
                               fetched_at=self.utcnow(), now=self.utcnow())
            except Exception:
                output["quality_flags"].append("CACHE_WRITE_FAILED")
        return output

    def __call_mode__(self):
        return getattr(self.trends_gateway, "data_mode", None)
