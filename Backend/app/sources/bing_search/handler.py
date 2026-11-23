"""Selective, cache-first Bing verification over persisted Google candidates."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.jobs.public_admission import (
    MANUAL_SOURCE_REFRESH_MARKER,
    OWNER_FRESH_ACCEPTANCE_MARKER,
    AdmissionDenied,
)
from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.sources.bing_search.verification_plan import (
    build_verification_plan,
)
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, source_capability
from app.sources.live_gateway import GatewayError
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.refresh_eligibility import (
    BING_REFRESH_STATE_KEY,
    OWNER_ACCEPTANCE_SLOT_PREFIX,
    candidate_slot,
    manual_candidate_slot,
    manual_schedule_metadata,
    reserve_owner_acceptance_slot,
    reserve_manual_source_slot,
    reserve_source_slot,
)
from app.sources.serpapi.errors import SerpApiError
from app.sources.serpapi.normalization import normalize_items
from app.sources.serpapi.pagination import initial_offset

_CACHE_RETENTION = timedelta(days=7)
_MAX_CANDIDATES = 5_000


def _parse_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _read_saved_candidates(collection_store, *, now):
    with collection_store.connect() as db:
        rows = db.execute(
            "SELECT id,keyword,source,project,observed_at,data_mode,evidence "
            "FROM keywords WHERE source=? AND project LIKE 'google_trends:%' "
            "AND data_mode='LIVE' AND eligible=1 ORDER BY observed_at DESC LIMIT ?",
            (GOOGLE_SOURCE_ID, _MAX_CANDIDATES),
        ).fetchall()
    candidates, recent, cached = [], set(), []
    cutoff = now - _CACHE_RETENTION
    for row in rows:
        try:
            evidence = json.loads(row["evidence"])
        except (TypeError, ValueError):
            continue
        if not isinstance(evidence, dict):
            continue
        fetched_at = _parse_timestamp(evidence.get("fetched_at"))
        candidate = {
            "record_id": row["id"],
            "canonical_keyword": row["keyword"],
            "source_id": row["source"],
            "project": row["project"],
            "data_mode": row["data_mode"],
            "observed_at": row["observed_at"],
            "fetched_at": fetched_at,
            "evidence": evidence,
        }
        candidates.append(candidate)
        try:
            from app.sources.google_trends.trending_now import normalize_keyword

            _, normalized = normalize_keyword(row["keyword"])
            identity = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        except (TypeError, ValueError):
            continue
        verification = evidence.get("bing_verification")
        if not isinstance(verification, dict):
            continue
        verified_at = _parse_timestamp(verification.get("fetched_at"))
        if verification.get("status") in {"VERIFIED", "FAILED"} and verified_at:
            within_retention = cutoff <= verified_at <= now
            if within_retention:
                recent.add(identity)
            if verification.get("status") == "VERIFIED" and within_retention:
                cached.append((candidate, verification, verified_at))
    return tuple(candidates), frozenset(recent), tuple(cached)


def _result_record(candidate, verification):
    if isinstance(candidate, dict):
        from app.sources.google_trends.trending_now import normalize_keyword

        _, normalized = normalize_keyword(candidate.get("canonical_keyword"))
        return {
            "record_type": "BING_SAVED_CANDIDATE_VERIFICATION",
            "candidate_keyword_id": candidate["record_id"],
            "candidate_identity": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            "candidate_keyword": candidate["canonical_keyword"],
            "candidate_source_id": candidate["source_id"],
            "candidate_observed_at": candidate["observed_at"],
            "bing_verification": verification,
        }
    return {
        "record_type": "BING_SAVED_CANDIDATE_VERIFICATION",
        "candidate_keyword_id": candidate.record_id,
        "candidate_identity": candidate.identity,
        "candidate_keyword": candidate.keyword,
        "candidate_source_id": candidate.source_id,
        "candidate_observed_at": candidate.observed_at.isoformat(),
        "bing_verification": verification,
    }


def _current_cached(cached, now):
    if not cached:
        return None
    candidate, verification, fetched_at = sorted(
        cached, key=lambda item: (-item[2].timestamp(), item[0]["record_id"])
    )[0]
    stale = now - fetched_at > timedelta(hours=1)
    copied = dict(verification)
    copied["delivery_state"] = "STALE_CACHE" if stale else "CACHED"
    flags = list(copied.get("quality_flags", []))
    if stale:
        flags.append("STALE_LOCAL_CACHE_REUSED")
    copied["quality_flags"] = sorted(set(flags))
    results = verification.get("results")
    first_result = results[0] if isinstance(results, list) and results else {}
    if not isinstance(first_result, dict):
        first_result = {}
    return {
        "status": "SUCCESS",
        "data_mode": "LIVE",
        "delivery_state": "STALE_CACHE" if stale else "CACHED",
        "records": [_result_record(candidate, copied)],
        "record_count": 1,
        "coverage": {
            "discovery_mode": "SAVED_GOOGLE_CANDIDATE_BING_SERP",
            "cache_state": "STALE" if stale else "FRESH",
            "cache_fetched_at": verification.get("fetched_at"),
            "provider_request_count": 0,
            "cached_provider_request_count": 1,
            "pages": 1,
            "query": verification.get("query"),
            "market": verification.get("market"),
            "language": verification.get("language"),
            "provider_cache_state": first_result.get("cache_state", "UNKNOWN"),
            "provider_created_at": verification.get("provider_created_at"),
            "fetched_at": verification.get("fetched_at"),
            "provider_result_count": verification.get("provider_result_count", 0),
            "accepted_result_count": verification.get("result_count", 0),
            "rejected_result_count": verification.get("rejected_result_count", 0),
            "candidate_source_id": candidate.get("source_id"),
            "candidate_selection_reason": verification.get("candidate_selection_reason"),
        },
        "quality_flags": copied["quality_flags"],
        "provider_request_count": 0,
    }


class BingSavedCandidateHandler:
    """At most one saved candidate, one page and one paid attempt per invocation."""

    def __init__(self, *, gateway, collection_store, atomic_store, policy_fingerprint,
                 ensure_paid_budget=None, utcnow=lambda: datetime.now(timezone.utc),
                 allow_initial_acceptance: bool = False, candidate_loader=None,
                 checkpoint_candidate_loader=None,
                 shared_cache: SharedCache | None = None, resolve_cache_grant=None):
        if (not callable(getattr(gateway, "fetch_page", None))
                or (not callable(getattr(collection_store, "connect", None))
                    and not callable(candidate_loader))
                or not callable(getattr(atomic_store, "transact", None))
                or not isinstance(policy_fingerprint, str) or len(policy_fingerprint) != 64
                or (ensure_paid_budget is not None and not callable(ensure_paid_budget))
                or not callable(utcnow)
                or type(allow_initial_acceptance) is not bool
                or (shared_cache is not None and not isinstance(shared_cache, SharedCache))
                or ((shared_cache is None) != (resolve_cache_grant is None))
                or (resolve_cache_grant is not None and not callable(resolve_cache_grant))
                or candidate_loader is not None and not callable(candidate_loader)
                or (checkpoint_candidate_loader is not None
                    and not callable(checkpoint_candidate_loader))):
            raise ValueError("approved Bing gateway and durable stores required")
        self.gateway = gateway
        self.collection_store = collection_store
        self.candidate_loader = candidate_loader
        self.checkpoint_candidate_loader = checkpoint_candidate_loader
        self.atomic_store = atomic_store
        self.policy_fingerprint = policy_fingerprint
        self.ensure_paid_budget = ensure_paid_budget
        self.utcnow = utcnow
        self.allow_initial_acceptance = allow_initial_acceptance
        self.shared_cache = shared_cache
        self.resolve_cache_grant = resolve_cache_grant

    def _candidate_cache_scope(self, candidate, now):
        if self.shared_cache is None:
            return None, None
        try:
            grant = self.resolve_cache_grant(BING_SOURCE_ID, now)
        except Exception:
            return None, None
        if not isinstance(grant, CacheGrant) or grant.source_id != BING_SOURCE_ID:
            return None, None
        capability = source_capability(BING_SOURCE_ID)
        scope = CacheScope(
            source_id=BING_SOURCE_ID,
            provider_id=capability.provider_id,
            query=f"saved-google-candidate:{candidate.identity}",
            plan_fingerprint=self.policy_fingerprint,
            market="US",
            language="en-US",
            window=DEFAULT_QUERY_PLAN.time_filter_rule,
            parser_version=f"bing-candidate-v{capability.capability_version}",
            policy_version=grant.policy_version,
        )
        return scope, grant

    def _shared_cached_result(self, candidate, now):
        scope, grant = self._candidate_cache_scope(candidate, now)
        if scope is None:
            return None, None
        try:
            cached = self.shared_cache.get(scope, grant=grant, now=now)
        except Exception:
            return None, None
        if (not isinstance(cached, dict) or cached.get("status") not in {"CACHED", "STALE"}
                or cached.get("origin") != "LIVE"):
            return None, None
        payload = cached.get("payload")
        if (not isinstance(payload, dict) or payload.get("data_mode") != "LIVE"
                or payload.get("status") not in {"SUCCESS", "PARTIAL"}
                or not isinstance(payload.get("records"), list)
                or not payload["records"]):
            return None, None
        delivery = "CACHED" if cached["status"] == "CACHED" else "STALE_CACHE"
        coverage = payload.get("coverage")
        coverage = dict(coverage) if isinstance(coverage, dict) else {}
        original_count = coverage.get("provider_request_count")
        if type(original_count) is not int or original_count < 0:
            original_count = payload.get("provider_request_count")
        if type(original_count) is int and original_count >= 0:
            coverage["cached_provider_request_count"] = original_count
        coverage.pop("owner_fresh_acceptance", None)
        coverage["provider_request_count"] = 0
        coverage["cache_fetched_at"] = cached.get("fetched_at")
        coverage["cache_state"] = cached["status"]
        records = []
        for row in payload["records"]:
            if (not isinstance(row, dict)
                    or row.get("candidate_keyword_id") != candidate.record_id
                    or not isinstance(row.get("bing_verification"), dict)):
                return None, None
            verification = dict(row["bing_verification"])
            verification["delivery_state"] = delivery
            records.append(_result_record(candidate, verification))
        flags = list(payload.get("quality_flags", []))
        if delivery == "STALE_CACHE":
            flags.append("STALE_SHARED_CACHE_REUSED")
        result = {
            **payload,
            "delivery_state": delivery,
            "records": records,
            "record_count": len(records),
            "provider_request_count": 0,
            "coverage": coverage,
            "quality_flags": sorted(set(flags)),
        }
        return cached["status"], result

    @staticmethod
    def __call_mode__():
        return "LIVE"

    async def __call__(self, context):
        if context.source_id != BING_SOURCE_ID:
            raise ValueError("Bing handler received a different source")
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
        saved = progress.get("bing_receipt") if isinstance(progress, dict) else None
        if isinstance(saved, dict) and isinstance(saved.get("result"), dict):
            result = saved["result"]
            if result.get("data_mode") != self.gateway.data_mode:
                raise ValueError("saved Bing receipt has an invalid data mode")
            return result

        raw_now = self.utcnow()
        if raw_now.tzinfo is None or raw_now.utcoffset() is None:
            raise ValueError("timezone-aware Bing handler clock required")
        now = raw_now.astimezone(timezone.utc)
        if (not acceptance_fresh and not manual_refresh
                and self.collection_store is not None):
            rows, recently_verified, cached = _read_saved_candidates(
                self.collection_store, now=now
            )
        else:
            rows, recently_verified, cached = (), frozenset(), ()
        if self.candidate_loader is not None:
            try:
                loaded = self.candidate_loader(now)
            except Exception:
                loaded = ()
            if isinstance(loaded, (tuple, list)):
                if acceptance_fresh:
                    rows = tuple(row for row in loaded
                                 if isinstance(row, dict)
                                 and isinstance(row.get("record_id"), str))
                else:
                    by_id = {
                        row.get("record_id"): row for row in rows
                        if isinstance(row, dict) and isinstance(row.get("record_id"), str)
                    }
                    for row in loaded:
                        if (isinstance(row, dict)
                                and isinstance(row.get("record_id"), str)
                                and row["record_id"] not in by_id):
                            by_id[row["record_id"]] = row
                    rows = tuple(by_id.values())
        handoff_error = None
        if self.checkpoint_candidate_loader is not None:
            try:
                loaded = self.checkpoint_candidate_loader(context, now)
            except AdmissionDenied as error:
                loaded = ()
                handoff_error = error.code
            except Exception:
                loaded = ()
                handoff_error = "GOOGLE_CANDIDATE_HANDOFF_UNAVAILABLE"
            if isinstance(loaded, (tuple, list)):
                by_id = {
                    row.get("record_id"): row for row in rows
                    if isinstance(row, dict) and isinstance(row.get("record_id"), str)
                }
                for row in loaded:
                    if (isinstance(row, dict)
                            and isinstance(row.get("record_id"), str)):
                        # The current durable Google checkpoint is newer evidence
                        # than a same-ID shared cache or older collection row.
                        by_id[row["record_id"]] = row
                rows = tuple(by_id.values())
        cached_result = _current_cached(cached, now) if not acceptance_fresh else None
        plan = build_verification_plan(
            rows, policy_fingerprint=self.policy_fingerprint, started_at=now,
            recently_verified=(frozenset() if acceptance_fresh else recently_verified),
        )
        if not plan.candidates:
            if cached_result is not None:
                context.checkpoint({"bing_receipt": {"result": cached_result}})
                return cached_result
            if handoff_error and not rows:
                return {
                    "status": "FAILED", "data_mode": self.gateway.data_mode,
                    "records": [], "record_count": 0,
                    "error_code": handoff_error,
                    "coverage": {"discovery_mode": "SAVED_GOOGLE_CANDIDATE_BING_SERP",
                                 "failure_phase": "GOOGLE_CANDIDATE_HANDOFF",
                                 "provider_request_count": 0},
                }
            return {
                "status": "UNAVAILABLE", "data_mode": self.gateway.data_mode,
                "records": [], "record_count": 0, "error_code": "NO_SAVED_CANDIDATE",
                "coverage": {"discovery_mode": "SAVED_GOOGLE_CANDIDATE_BING_SERP",
                             "provider_request_count": 0},
            }

        candidate, serp_context = plan.candidates[0], plan.contexts[0]
        shared_stale_result = None
        if not acceptance_fresh and self.shared_cache is not None:
            cache_status, shared_result = self._shared_cached_result(candidate, now)
            if cache_status == "CACHED":
                context.checkpoint({"bing_receipt": {"result": shared_result}})
                return shared_result
            if cache_status == "STALE":
                shared_stale_result = shared_result
        slot = (
            f"{OWNER_ACCEPTANCE_SLOT_PREFIX}{context.job_id}"
            if acceptance_fresh else manual_candidate_slot(
                self.atomic_store, BING_REFRESH_STATE_KEY, now,
                job_id=context.job_id,
                error_code="BING_SCHEDULE_STATE_INVALID",
            ) if manual_refresh else candidate_slot(
                self.atomic_store, BING_REFRESH_STATE_KEY, now,
                error_code="BING_SCHEDULE_STATE_INVALID",
                allow_initial_acceptance=self.allow_initial_acceptance,
            )
        )
        if slot is None:
            if cached_result is not None:
                context.checkpoint({"bing_receipt": {"result": cached_result}})
                return cached_result
            if shared_stale_result is not None:
                context.checkpoint({"bing_receipt": {"result": shared_stale_result}})
                return shared_stale_result
            error_code = "MANUAL_REFRESH_COOLDOWN" if manual_refresh else "NOT_YET_ELIGIBLE"
            coverage = {"discovery_mode": "SAVED_GOOGLE_CANDIDATE_BING_SERP",
                        "provider_request_count": 0,
                        "next_slots_timezone": "Asia/Ho_Chi_Minh"}
            if manual_refresh:
                try:
                    coverage["next_eligible_at"] = manual_schedule_metadata(
                        self.atomic_store, BING_REFRESH_STATE_KEY, now,
                        error_code="BING_SCHEDULE_STATE_INVALID",
                    ).get("next_eligible_at")
                except AdmissionDenied as error:
                    return {
                        "status": "FAILED", "data_mode": self.gateway.data_mode,
                        "records": [], "record_count": 0,
                        "error_code": error.code, "coverage": coverage,
                    }
            return {
                "status": "UNAVAILABLE", "data_mode": self.gateway.data_mode,
                "records": [], "record_count": 0, "error_code": error_code,
                "coverage": coverage,
            }

        account_snapshot = None
        if self.ensure_paid_budget is not None:
            try:
                account_snapshot = await self.ensure_paid_budget()
            except SerpApiError as error:
                return {
                    "status": "FAILED", "data_mode": self.gateway.data_mode,
                    "records": [], "record_count": 0, "error_code": error.code,
                    "coverage": {"discovery_mode": "SAVED_GOOGLE_CANDIDATE_BING_SERP",
                                 "provider_request_count": 0,
                                 "query": serp_context.canonical_query,
                                 "eligible_slot": slot},
                }
        try:
            slot = (
                reserve_owner_acceptance_slot(
                    self.atomic_store, BING_REFRESH_STATE_KEY, self.utcnow(),
                    job_id=context.job_id,
                    error_code="BING_SCHEDULE_STATE_INVALID",
                ) if acceptance_fresh else reserve_manual_source_slot(
                    self.atomic_store, BING_REFRESH_STATE_KEY, self.utcnow(),
                    job_id=context.job_id,
                    error_code="BING_SCHEDULE_STATE_INVALID",
                ) if manual_refresh else reserve_source_slot(
                    self.atomic_store, BING_REFRESH_STATE_KEY, self.utcnow(),
                    error_code="BING_SCHEDULE_STATE_INVALID",
                    allow_initial_acceptance=self.allow_initial_acceptance,
                )
            )
        except AdmissionDenied:
            slot = None
        if slot is None:
            if cached_result is not None:
                context.checkpoint({"bing_receipt": {"result": cached_result}})
                return cached_result
            if shared_stale_result is not None:
                context.checkpoint({"bing_receipt": {"result": shared_stale_result}})
                return shared_stale_result
            error_code = "MANUAL_REFRESH_COOLDOWN" if manual_refresh else "NOT_YET_ELIGIBLE"
            coverage = {"discovery_mode": "SAVED_GOOGLE_CANDIDATE_BING_SERP",
                        "provider_request_count": 0,
                        "next_slots_timezone": "Asia/Ho_Chi_Minh"}
            if manual_refresh:
                try:
                    coverage["next_eligible_at"] = manual_schedule_metadata(
                        self.atomic_store, BING_REFRESH_STATE_KEY, self.utcnow(),
                        error_code="BING_SCHEDULE_STATE_INVALID",
                    ).get("next_eligible_at")
                except AdmissionDenied as error:
                    return {
                        "status": "FAILED", "data_mode": self.gateway.data_mode,
                        "records": [], "record_count": 0,
                        "error_code": error.code, "coverage": coverage,
                    }
            return {
                "status": "UNAVAILABLE", "data_mode": self.gateway.data_mode,
                "records": [], "record_count": 0, "error_code": error_code,
                "coverage": coverage,
            }
        attempt_id = hashlib.sha256(
            f"{context.job_id}:bing:{serp_context.query_fingerprint}".encode("utf-8")
        ).hexdigest()
        receipt_box = []

        def save_receipt(gateway_page):
            fetched_at = self.utcnow().astimezone(timezone.utc)
            page = gateway_page.page
            normalized, rejected = normalize_items(
                serp_context, page.items, fetched_at=fetched_at, page=1,
                offset=initial_offset(serp_context).value, http_status=page.http_status,
                cache_state=page.cache_state, provider_created_at=page.provider_created_at,
                data_mode=gateway_page.data_mode,
            )
            verification = {
                "status": "VERIFIED",
                "provider_id": source_capability(BING_SOURCE_ID).provider_id,
                "verification_mode": "BING_SERP",
                "plan_id": serp_context.plan_id,
                "plan_version": serp_context.plan_version,
                "query_fingerprint": serp_context.query_fingerprint,
                "query": serp_context.canonical_query,
                "market": "US",
                "language": "en-US",
                "candidate_selection_reason": candidate.reason,
                "fetched_at": fetched_at.isoformat(),
                "provider_created_at": (page.provider_created_at.isoformat()
                                         if page.provider_created_at else None),
                "delivery_state": "CACHED" if page.cache_state == "CACHED" else "FRESH",
                "provider_result_count": len(page.items),
                "result_count": len(normalized),
                "rejected_result_count": len(rejected),
                "results": [{
                    "canonical_url": record.canonical_url,
                    "title": record.title,
                    "snippet": record.snippet,
                    "provider_rank": record.evidence[0].position,
                    "fetched_at": record.fetched_at.isoformat(),
                    "provider_created_at": (
                        record.evidence[0].provider_created_at.isoformat()
                        if record.evidence[0].provider_created_at else None
                    ),
                    "cache_state": record.evidence[0].cache_state,
                    "quality_flags": list(record.quality_flags),
                } for record in normalized],
                "quality_flags": ["QUERY_SCOPED_POSITION_IS_NOT_SEARCH_VOLUME"],
                "limitations": ["SERP_POSITION_IS_NOT_SEARCH_VOLUME",
                                "SERP_IS_NOT_COMMERCIAL_DEMAND",
                                "MKT_US_IS_NOT_PROOF_OF_US_CUSTOMER_INTEREST"],
            }
            result = {
                "status": "PARTIAL" if rejected else "SUCCESS",
                "data_mode": gateway_page.data_mode,
                "delivery_state": verification["delivery_state"],
                "records": [_result_record(candidate, verification)],
                "record_count": 1,
                "coverage": {
                    "discovery_mode": "SAVED_GOOGLE_CANDIDATE_BING_SERP",
                    "candidate_source_id": candidate.source_id,
                    "candidate_selection_reason": candidate.reason,
                    "candidate_observed_at": candidate.observed_at.isoformat(),
                    "candidate_fetched_at": candidate.fetched_at.isoformat(),
                    "query": serp_context.canonical_query,
                    "market": "US", "language": "en-US",
                    "provider_request_count": 1, "pages": 1,
                    "provider_result_count": len(page.items),
                    "accepted_result_count": len(normalized),
                    "rejected_result_count": len(rejected),
                    "fetched_at": verification["fetched_at"],
                    "provider_created_at": verification["provider_created_at"],
                    "provider_cache_state": page.cache_state,
                    "eligible_slot": slot,
                    **({"account_quota": account_snapshot}
                       if account_snapshot is not None else {}),
                },
                "provider_request_count": 1,
            }
            if acceptance_fresh:
                result["coverage"]["owner_fresh_acceptance"] = True
            if self.shared_cache is not None:
                scope, grant = self._candidate_cache_scope(candidate, fetched_at)
                if scope is None or not isinstance(grant, CacheGrant):
                    raise GatewayError("RESULT_PERSISTENCE_FAILED")
                try:
                    self.shared_cache.put(
                        scope, grant=grant, origin="LIVE", payload=result,
                        fetched_at=fetched_at, now=self.utcnow(),
                    )
                except Exception:
                    raise GatewayError("RESULT_PERSISTENCE_FAILED") from None
            context.checkpoint({"bing_receipt": {"result": result}})
            receipt_box.append(result)

        try:
            await self.gateway.fetch_page(
                context=serp_context, cursor=initial_offset(serp_context),
                job_id=context.job_id, owner=context.owner, fence=context.fence,
                attempt_id=attempt_id, timeout_seconds=15,
                on_page_receipt=save_receipt,
            )
        except (GatewayError, SerpApiError, AdmissionDenied) as error:
            code = getattr(error, "code", "SOURCE_EXECUTION_FAILED")
            return {
                "status": "FAILED", "data_mode": self.gateway.data_mode,
                "records": [], "record_count": 0,
                "error_code": code if isinstance(code, str) else "SOURCE_EXECUTION_FAILED",
                "coverage": {"discovery_mode": "SAVED_GOOGLE_CANDIDATE_BING_SERP",
                             "provider_request_count": 1,
                             "query": serp_context.canonical_query,
                             "eligible_slot": slot},
            }
        if not receipt_box:
            raise ValueError("Bing provider receipt was not persisted")
        return receipt_box[0]
