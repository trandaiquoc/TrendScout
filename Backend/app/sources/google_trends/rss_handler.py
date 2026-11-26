"""Limited official-feed discovery handler with optional bounded SERP verification."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from app.jobs.public_admission import AdmissionDenied
from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.sources.catalog import GOOGLE_SOURCE_ID, source_capability
from app.sources.google_trends.handler import _retry_after, _safe_error_code, _serialize_serp_page
from app.sources.google_trends.rss import RSS_PARSER_VERSION
from app.sources.google_trends.verification_plan import (
    MAX_VERIFICATION_QUERIES,
    build_verification_plan,
    select_verification_candidates,
)
from app.sources.live_gateway import GatewayError
from app.sources.serpapi.errors import SerpApiError
from app.sources.serpapi.pagination import initial_offset

_MAX_NORMALIZED_BYTES = 3_000_000


def normalize_rss_candidates(page, *, fetched_at: datetime) -> list[dict]:
    if (not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None
            or fetched_at.utcoffset() is None):
        raise ValueError("timezone-aware fetch time is required")
    fetched = fetched_at.astimezone(timezone.utc).isoformat()
    groups = {}
    cluster_ids = {}
    for row in page.records:
        key = row["canonical_keyword_identity"]
        candidate = groups.get(key)
        if candidate is None:
            candidate = {
                key_name: row.get(key_name) for key_name in (
                    "source_id", "provider_id", "transport_id", "data_source",
                    "discovery_mode", "plan_id", "plan_version", "canonical_keyword",
                    "canonical_keyword_identity", "region", "language", "time_window_hours",
                    "provider_data_at", "provider_created_at", "source_url", "delivery_state",
                    "data_mode", "quality_flags",
                )
            }
            candidate["fetched_at"] = fetched
            candidate["trend_clusters"] = []
            groups[key] = candidate
            cluster_ids[key] = set()
        for cluster in row.get("trend_clusters", []):
            cluster_id = cluster.get("trend_cluster_id")
            if cluster_id in cluster_ids[key]:
                continue
            cluster_ids[key].add(cluster_id)
            candidate["trend_clusters"].append({
                **cluster,
                "parent_keyword_identity": key,
                "search_volume": None,
                "search_volume_semantics": cluster.get("search_volume_semantics")
                    or "RSS_APPROX_TRAFFIC_BUCKET",
                "increase_percentage": None,
                "categories": None,
                "related_queries": None,
            })
    output = list(groups.values())
    output.sort(key=lambda item: (item["canonical_keyword_identity"],
                                 item["canonical_keyword"].casefold()))
    return output


class GoogleTrendsRSSHandler:
    def __init__(self, *, gateway, policy_fingerprint: str,
                 utcnow=lambda: datetime.now(timezone.utc), cache: SharedCache | None = None,
                 resolve_cache_grant=None, serp_gateway=None,
                 ensure_paid_budget=None,
                 previously_observed=lambda: frozenset(),
                 recently_verified=lambda: frozenset(),
                 previous_verification=lambda: {}):
        if (not callable(getattr(gateway, "fetch_page", None))
                or not isinstance(policy_fingerprint, str) or len(policy_fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in policy_fingerprint)
                or not callable(utcnow) or (cache is None) != (resolve_cache_grant is None)
                or resolve_cache_grant is not None and not callable(resolve_cache_grant)
                or serp_gateway is not None and not callable(getattr(serp_gateway, "fetch_page", None))
                or (ensure_paid_budget is not None and not callable(ensure_paid_budget))
                or not callable(previously_observed) or not callable(recently_verified)
                or not callable(previous_verification)):
            raise ValueError("validated RSS gateway and server-owned policy required")
        self.gateway, self.policy_fingerprint, self.utcnow = gateway, policy_fingerprint, utcnow
        self.cache, self.resolve_cache_grant = cache, resolve_cache_grant
        self.serp_gateway = serp_gateway
        self.ensure_paid_budget = ensure_paid_budget
        self.previously_observed, self.recently_verified = previously_observed, recently_verified
        self.previous_verification = previous_verification

    def __call_mode__(self):
        return self.gateway.data_mode

    def _cache(self):
        now = self.utcnow()
        grant = self.resolve_cache_grant(GOOGLE_SOURCE_ID, now)
        if not isinstance(grant, CacheGrant) or grant.source_id != GOOGLE_SOURCE_ID:
            return None, None
        scope = CacheScope(
            source_id=GOOGLE_SOURCE_ID, provider_id=source_capability(GOOGLE_SOURCE_ID).provider_id,
            query="google-trends-rss:US",
            plan_fingerprint=self.policy_fingerprint, market="US", language="en",
            window="limited-feed", parser_version=RSS_PARSER_VERSION,
            policy_version=grant.policy_version,
        )
        return scope, grant

    @staticmethod
    def _stale_cache_fallback(payload, *, fetched_at, error_code, provider_request_count):
        records = payload["records"]
        coverage = payload.get("coverage")
        coverage = dict(coverage) if isinstance(coverage, dict) else {}
        cached_count = coverage.get("provider_request_count")
        if type(cached_count) is not int or cached_count < 0:
            cached_count = payload.get("provider_request_count")
        if type(cached_count) is int and cached_count >= 0:
            coverage["cached_provider_request_count"] = cached_count
        quality_flags = payload.get("quality_flags")
        quality_flags = {
            flag for flag in quality_flags
            if isinstance(flag, str)
        } if isinstance(quality_flags, list) else set()
        quality_flags.add("STALE_SHARED_CACHE_REUSED")
        coverage.update(
            cache_fetched_at=fetched_at,
            cache_state="STALE",
            refresh_attempt_status="FAILED",
            refresh_error_code=error_code,
        )
        result = {
            **payload,
            "status": "PARTIAL",
            "delivery_state": "STALE_CACHE",
            "records": [
                {**record, "delivery_state": "STALE_CACHE"}
                for record in records
            ],
            "record_count": len(records),
            "shared_cache_state": "STALE",
            "shared_cache_fetched_at": fetched_at,
            "error_code": error_code,
            "quality_flags": sorted(quality_flags),
        }
        if type(provider_request_count) is int and provider_request_count >= 0:
            coverage["provider_request_count"] = provider_request_count
            coverage["refresh_provider_request_count"] = provider_request_count
            result["provider_request_count"] = provider_request_count
        else:
            coverage.pop("provider_request_count", None)
            result.pop("provider_request_count", None)
        result["coverage"] = coverage
        return result

    async def __call__(self, context):
        if context.source_id != GOOGLE_SOURCE_ID:
            raise ValueError("RSS handler received a different source")
        scope, grant = None, None
        stale_cache_fallback = None
        if self.cache is not None:
            try:
                scope, grant = self._cache()
                if scope is not None:
                    cached = self.cache.get(scope, grant=grant, now=self.utcnow())
                    payload = cached.get("payload")
                    records = payload.get("records") if isinstance(payload, dict) else None
                    if (cached.get("status") == "CACHED"
                            and isinstance(payload, dict)
                            and payload.get("data_mode") == self.gateway.data_mode
                            and payload.get("status") in {"SUCCESS", "PARTIAL"}
                            and isinstance(records, list)
                            and type(payload.get("record_count")) is int
                            and payload["record_count"] == len(records)
                            and all(isinstance(record, dict) for record in records)):
                        coverage = payload.get("coverage")
                        coverage = dict(coverage) if isinstance(coverage, dict) else {}
                        cached_count = coverage.get("provider_request_count")
                        if type(cached_count) is not int or cached_count < 0:
                            cached_count = payload.get("provider_request_count")
                        if type(cached_count) is int and cached_count >= 0:
                            coverage["cached_provider_request_count"] = cached_count
                        coverage.update(
                            provider_request_count=0,
                            attempted_calls=0,
                            cache_fetched_at=cached.get("fetched_at"),
                            cache_state="CACHED",
                        )
                        return {
                            **payload,
                            "delivery_state": "CACHED",
                            "shared_cache_state": "CACHED",
                            "shared_cache_fetched_at": cached.get("fetched_at"),
                            "provider_request_count": 0,
                            "coverage": coverage,
                            "records": [
                                {**record, "delivery_state": "CACHED"}
                                for record in records
                            ],
                        }
                    if (cached.get("status") == "STALE"
                            and isinstance(payload, dict)
                            and payload.get("data_mode") == self.gateway.data_mode
                            and payload.get("status") in {"SUCCESS", "PARTIAL"}
                            and isinstance(records, list)
                            and type(payload.get("record_count")) is int
                            and payload["record_count"] == len(records)
                            and all(isinstance(record, dict) for record in records)):
                        stale_cache_fallback = (payload, cached.get("fetched_at"))
            except Exception:
                scope, grant = None, None

        request_metadata = getattr(context, "request_metadata", None)
        if (isinstance(request_metadata, dict)
                and request_metadata.get("cache_only") is True):
            if stale_cache_fallback is not None:
                payload, fetched_at = stale_cache_fallback
                records = payload["records"]
                coverage = payload.get("coverage")
                coverage = dict(coverage) if isinstance(coverage, dict) else {}
                cached_count = coverage.get("provider_request_count")
                if type(cached_count) is not int or cached_count < 0:
                    cached_count = payload.get("provider_request_count")
                if type(cached_count) is int and cached_count >= 0:
                    coverage["cached_provider_request_count"] = cached_count
                coverage.update(provider_request_count=0, attempted_calls=0,
                                cache_fetched_at=fetched_at, cache_state="STALE",
                                cache_only=True, refresh_attempt_status="NOT_ATTEMPTED")
                flags = payload.get("quality_flags")
                flags = {flag for flag in flags if isinstance(flag, str)} \
                    if isinstance(flags, list) else set()
                flags.add("STALE_SHARED_CACHE_REUSED")
                return {
                    **payload, "status": "PARTIAL", "delivery_state": "STALE_CACHE",
                    "records": [{**record, "delivery_state": "STALE_CACHE"}
                                for record in records],
                    "record_count": len(records), "provider_request_count": 0,
                    "shared_cache_state": "STALE",
                    "shared_cache_fetched_at": fetched_at,
                    "coverage": coverage, "quality_flags": sorted(flags),
                }
            return {
                "status": "UNAVAILABLE", "data_mode": self.gateway.data_mode,
                "records": [], "record_count": 0, "error_code": "CACHE_ONLY_MISS",
                "delivery_state": "UNAVAILABLE", "provider_request_count": 0,
                "coverage": {"discovery_mode": "RSS_LIMITED_DISCOVERY",
                             "cache_only": True, "cache_state": "UNAVAILABLE",
                             "provider_request_count": 0},
            }

        progress = ((context.resume_snapshot or {}).get("progress")
                    if isinstance(context.resume_snapshot, dict) else None)
        saved = progress.get("google_trends_receipt") if isinstance(progress, dict) else None
        result = saved.get("result") if isinstance(saved, dict) else None
        if result is None:
            receipt_box = []

            def save_receipt(gateway_page):
                page = gateway_page.page
                fetched_at = self.utcnow()
                records = normalize_rss_candidates(page, fetched_at=fetched_at)
                value = {
                    "status": "PARTIAL" if page.rejected_record_count else "SUCCESS",
                    "data_mode": gateway_page.data_mode,
                    "delivery_state": "FRESH",
                    "records": records,
                    "record_count": len(records),
                    "coverage": {
                        "discovery_mode": "RSS_LIMITED_DISCOVERY",
                        "provider_id": "google_trends_rss",
                        "transport_id": "google_trends_rss",
                        "provider_record_count": page.provider_record_count,
                        "accepted_record_count": len(page.records),
                        "rejected_record_count": page.rejected_record_count,
                        "http_status": page.http_status,
                        "provider_data_at": page.provider_data_at.isoformat()
                            if page.provider_data_at else None,
                        "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(),
                        "source_url": "https://trends.google.com/trending/rss?geo=US",
                        "coverage_limit": "Official RSS export sample; not a full historical window",
                        "provider_request_count": 1,
                    },
                    "quality_flags": ["RSS_LIMITED_COVERAGE", "NO_COMPATIBLE_BASELINE"],
                    "provider_request_count": 1,
                }
                receipt_box.append(value)
                context.checkpoint({"google_trends_receipt": {"result": value}})

            try:
                await self.gateway.fetch_page(
                    job_id=context.job_id, owner=context.owner, fence=context.fence,
                    attempt_id=hashlib.sha256(f"{context.job_id}:google-rss-us".encode()).hexdigest(),
                    timeout_seconds=15, on_page_receipt=save_receipt,
                )
                if not receipt_box:
                    raise ValueError("RSS receipt was not checkpointed")
                result = receipt_box[0]
            except (GatewayError, SerpApiError, AdmissionDenied) as error:
                if isinstance(error, AdmissionDenied) and error.code == "LEASE_LOST":
                    raise
                retry = _retry_after(error)
                request_count = 0 if isinstance(error, AdmissionDenied) else None
                if not isinstance(error, AdmissionDenied):
                    missing = object()
                    started_at = getattr(
                        self.gateway, "last_provider_request_started_at", missing,
                    )
                    if started_at is not missing:
                        request_count = int(started_at is not None)
                coverage = {"discovery_mode": "RSS_LIMITED_DISCOVERY",
                            "attempted_calls": 1, "records_omitted_storage_limit": 0}
                if request_count is not None:
                    coverage["provider_request_count"] = request_count
                if stale_cache_fallback is not None:
                    payload, fetched_at = stale_cache_fallback
                    return self._stale_cache_fallback(
                        payload, fetched_at=fetched_at,
                        error_code=_safe_error_code(error),
                        provider_request_count=request_count,
                    )
                failure = {
                    "status": "FAILED", "data_mode": self.gateway.data_mode,
                    "records": [], "record_count": 0, "error_code": _safe_error_code(error),
                    **({"retry_after_seconds": retry} if retry is not None else {}),
                    "coverage": coverage,
                }
                if request_count is not None:
                    failure["provider_request_count"] = request_count
                return failure
        if not isinstance(result, dict) or not isinstance(result.get("records"), list):
            raise ValueError("saved RSS result is invalid")
        records = [dict(item) for item in result["records"]]
        coverage = dict(result.get("coverage", {}))
        flags = list(result.get("quality_flags", []))
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
        attempted = failed = verified = 0
        account_quota = None
        if self.serp_gateway is not None:
            try:
                old, recent = self.previously_observed(), self.recently_verified()
                selected = select_verification_candidates(
                    tuple(records),
                    previously_observed=old if isinstance(old, frozenset) else frozenset(),
                    recently_verified=recent if isinstance(recent, frozenset) else frozenset(),
                    limit=MAX_VERIFICATION_QUERIES,
                )
            except Exception:
                selected = ()
                flags.append("VERIFICATION_SELECTION_UNAVAILABLE")
            for candidate in selected:
                plan = build_verification_plan((candidate,),
                    policy_fingerprint=self.policy_fingerprint, started_at=self.utcnow())
                serp_context = plan.contexts[0]
                attempted += 1
                try:
                    account_quota = (await self.ensure_paid_budget()
                                     if self.ensure_paid_budget is not None else account_quota)
                    page = await self.serp_gateway.fetch_page(
                        context=serp_context, cursor=initial_offset(serp_context),
                        job_id=context.job_id, owner=context.owner, fence=context.fence,
                        attempt_id=hashlib.sha256(
                            f"{context.job_id}:rss-serp:{serp_context.query_fingerprint}".encode()
                        ).hexdigest(),
                        timeout_seconds=15,
                    )
                    verification = _serialize_serp_page(
                        serp_context, page.page, fetched_at=self.utcnow(),
                        data_mode=page.data_mode,
                    )
                    verified += 1
                except (GatewayError, SerpApiError, AdmissionDenied) as error:
                    if isinstance(error, AdmissionDenied) and error.code == "LEASE_LOST":
                        raise
                    failed += 1
                    verification = {"status": "FAILED", "verification_mode": "GOOGLE_SERP",
                                    "plan_id": plan.plan_id, "plan_version": plan.version,
                                    "query": serp_context.canonical_query,
                                    "error_code": _safe_error_code(error), "results": []}
                for record in records:
                    if record.get("canonical_keyword_identity") == candidate.get(
                            "canonical_keyword_identity"):
                        record["serp_verification"] = verification
                        break
                context.checkpoint({"google_trends_receipt": {"result": {
                    **result, "records": records, "record_count": len(records),
                }}})
        accepted, omitted, used = [], 0, 2
        for record in records:
            size = len(json.dumps(record, ensure_ascii=False, allow_nan=False,
                                  separators=(",", ":")).encode("utf-8"))
            if len(accepted) >= 5000 or used + size + (1 if accepted else 0) > _MAX_NORMALIZED_BYTES:
                omitted += 1
            else:
                accepted.append(record)
                used += size + (1 if len(accepted) > 1 else 0)
        if omitted:
            coverage.update(records_omitted_storage_limit=omitted,
                            stop_reason="NORMALIZED_RESULT_SIZE_LIMIT")
            flags.append("PARTIAL_STORAGE_LIMIT")
        coverage.update(
            trending_candidate_count=len(accepted),
            distinct_keyword_count=len({row.get("canonical_keyword_identity") for row in accepted}),
            serp_verification_attempted=attempted,
            serp_verified_candidate_count=verified,
            serp_verification_failed_count=failed,
            serp_max_queries_per_batch=MAX_VERIFICATION_QUERIES,
            serp_pages_per_query=1,
        )
        if account_quota is not None:
            coverage["serpapi_account_quota"] = account_quota
        output = {
            "status": "PARTIAL" if omitted or failed or coverage.get("rejected_record_count", 0)
                      else result["status"],
            "data_mode": result["data_mode"],
            "delivery_state": result.get("delivery_state", "FRESH"),
            "records": accepted, "record_count": len(accepted),
            "coverage": coverage, "quality_flags": sorted(set(flags)),
        }
        request_count = coverage.get("provider_request_count")
        if attempted > 0:
            # The base RSS request is known, but SERP verification can fail
            # before its provider request starts. Keep the aggregate unknown.
            request_count = None
            coverage.pop("provider_request_count", None)
            output["coverage"] = coverage
        if type(request_count) is int and request_count >= 0:
            output["provider_request_count"] = request_count
        if scope is not None and grant is not None and grant.allowed:
            try:
                self.cache.put(scope, grant=grant, origin=output["data_mode"],
                               payload=output, fetched_at=self.utcnow(), now=self.utcnow())
            except Exception:
                output["quality_flags"].append("CACHE_WRITE_FAILED")
        return output
