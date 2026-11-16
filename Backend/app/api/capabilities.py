"""Public, non-secret view of runtime and source capabilities."""

import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from app.ports.storage import StorageError
from app.sources.catalog import SOURCE_CAPABILITIES, WIKIMEDIA_SOURCE_ID
from app.sources.query_plan import get_query_plan

router = APIRouter()
_RESEARCH_CATALOG = Path(__file__).resolve().parents[1] / "sources" / "research_catalog.json"


def _public_saved_account_snapshot(value):
    if not isinstance(value, dict):
        return None
    observed_at = value.get("observed_at")
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if observed.tzinfo is None or observed.utcoffset() is None:
        return None

    snapshot = {"observed_at": observed.astimezone(timezone.utc).isoformat()}
    for key in ("monthly_limit", "monthly_remaining", "total_remaining",
                "month_to_date_searches", "current_hour_searches", "hourly_limit"):
        count = value.get(key)
        if count is None and key in {"monthly_limit", "monthly_remaining"}:
            snapshot[key] = None
        elif type(count) is int and 0 <= count <= 1_000_000_000:
            snapshot[key] = count
        else:
            return None

    renewal = value.get("plan_renewal_date")
    if renewal is not None:
        try:
            parsed = date.fromisoformat(renewal)
        except (TypeError, ValueError):
            return None
        if not isinstance(renewal, str) or parsed.isoformat() != renewal:
            return None
    snapshot["plan_renewal_date"] = renewal
    return snapshot


def _safe_utc_timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _latest_saved_account_snapshot(saved_activity):
    saved_snapshot = _public_saved_account_snapshot(
        saved_activity.get("_serpapi_account_snapshot")
        if isinstance(saved_activity, dict) else None
    )
    if saved_snapshot is not None:
        return saved_snapshot

    snapshots = []
    for activity in saved_activity.values():
        if not isinstance(activity, dict):
            continue
        for coverage_name in ("google_coverage", "bing_coverage"):
            coverage = activity.get(coverage_name)
            quota = coverage.get("account_quota") if isinstance(coverage, dict) else None
            snapshot = _public_saved_account_snapshot(quota)
            if snapshot is not None:
                observed = datetime.fromisoformat(snapshot["observed_at"])
                snapshots.append((observed, snapshot))
    return max(snapshots, key=lambda item: item[0])[1] if snapshots else None


def _saved_source_activity(request):
    """Return the latest persisted Fetch evidence per source, never live health."""
    service = getattr(request.app.state, "collection_service", None)
    if service is None:
        return {}
    data_mode_scope = getattr(service.store, "data_mode_scope", None)
    try:
        with service.store.connect() as db:
            rows = db.execute(
                "SELECT status,data_mode,source_manifest,fetched_at,observation_start,observation_end "
                "FROM dataset_snapshots "
                + ("WHERE data_mode=? " if data_mode_scope else "")
                + "ORDER BY created_at DESC LIMIT 100",
                (data_mode_scope,) if data_mode_scope else (),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return {}

    latest = {}
    latest_youtube_observation = None
    latest_account_snapshot = None
    latest_account_observed_at = None
    for row in rows:
        try:
            manifest = json.loads(row["source_manifest"])
        except (TypeError, ValueError, KeyError):
            continue
        if not isinstance(manifest, list):
            continue
        for source in manifest:
            if not isinstance(source, dict):
                continue
            source_id = source.get("source_id")
            if not isinstance(source_id, str):
                continue
            if source_id in {"google_web_serp", "bing_web_serp"}:
                coverage = source.get("coverage")
                quota_key = ("serpapi_account_quota" if source_id == "google_web_serp"
                             else "account_quota")
                quota = coverage.get(quota_key) if isinstance(coverage, dict) else None
                account_snapshot = _public_saved_account_snapshot(quota)
                if account_snapshot is not None:
                    observed_at = datetime.fromisoformat(account_snapshot["observed_at"])
                    if (latest_account_observed_at is None
                            or observed_at > latest_account_observed_at):
                        latest_account_snapshot = account_snapshot
                        latest_account_observed_at = observed_at
            if source_id == "youtube_video_search":
                coverage = source.get("coverage")
                source_fetched_at = (_safe_utc_timestamp(coverage.get("source_fetched_at"))
                                     if isinstance(coverage, dict) else None)
                if (latest_youtube_observation is None and source_fetched_at is not None
                        and source.get("status") in {"SUCCESS", "PARTIAL"}
                        and source.get("data_mode", row["data_mode"]) == "LIVE"):
                    latest_youtube_observation = {
                        "fetched_at": source_fetched_at,
                        "record_count": (source.get("record_count")
                                         if type(source.get("record_count")) is int
                                         and source.get("record_count") >= 0 else None),
                    }
            if source_id in latest:
                if source_id == "bing_web_serp":
                    _merge_saved_bing_evidence(latest[source_id], source)
                continue
            status = source.get("status")
            status = status if status in {"SUCCESS", "PARTIAL", "FAILED"} else "UNKNOWN"
            record_count = source.get("record_count")
            activity = {
                "status": status,
                "data_mode": row["data_mode"],
                "record_count": record_count if type(record_count) is int and record_count >= 0 else None,
                "fetched_at": row["fetched_at"],
                "observation_start": row["observation_start"],
                "observation_end": row["observation_end"],
            }
            delivery_state = source.get("delivery_state")
            if delivery_state in {"FRESH", "CACHED", "STALE_CACHE"}:
                activity["delivery_state"] = delivery_state
            error_code = source.get("error_code")
            if (source_id == "youtube_video_search" and isinstance(error_code, str)
                    and re.fullmatch(r"[A-Z0-9_]{1,64}", error_code)):
                activity["error_code"] = error_code
            if source_id == "google_web_serp":
                coverage = source.get("coverage")
                if isinstance(coverage, dict):
                    summary = {}
                    for key in ("trending_candidate_count", "distinct_keyword_count",
                                "serp_verified_candidate_count", "serp_verification_attempted",
                                "serp_verification_failed_count", "provider_record_count",
                                "accepted_record_count", "rejected_record_count",
                                "records_omitted_storage_limit"):
                        value = coverage.get(key)
                        if type(value) is int and value >= 0:
                            summary[key] = value
                    mode = coverage.get("discovery_mode")
                    if mode in {"SERPAPI_TRENDING_NOW", "RSS_LIMITED_DISCOVERY"}:
                        summary["discovery_mode"] = mode
                    for key in ("provider_data_at", "provider_created_at"):
                        value = coverage.get(key)
                        if isinstance(value, str) and len(value) <= 64:
                            summary[key] = value
                    account_quota = _public_saved_account_snapshot(
                        coverage.get("serpapi_account_quota")
                    )
                    if account_quota is not None:
                        summary["account_quota"] = account_quota
                    if (status in {"SUCCESS", "PARTIAL"}
                            and source.get("delivery_state")
                            in {"FRESH", "CACHED", "STALE_CACHE"}):
                        summary["delivery_state"] = source["delivery_state"]
                    activity["google_coverage"] = summary
            if source_id == "bing_web_serp":
                coverage = source.get("coverage")
                if isinstance(coverage, dict):
                    summary = {}
                    for key in ("provider_request_count", "pages", "provider_result_count",
                                "accepted_result_count", "rejected_result_count"):
                        value = coverage.get(key)
                        if type(value) is int and value >= 0:
                            summary[key] = value
                    for key in ("query", "market", "language", "provider_cache_state",
                                "provider_created_at", "fetched_at", "cache_fetched_at",
                                "cache_state", "candidate_source_id",
                                "candidate_selection_reason"):
                        value = coverage.get(key)
                        if isinstance(value, str) and len(value) <= 256:
                            summary[key] = value
                    account_quota = _public_saved_account_snapshot(
                        coverage.get("account_quota")
                    )
                    if account_quota is not None:
                        summary["account_quota"] = account_quota
                    if (status in {"SUCCESS", "PARTIAL"}
                            and source.get("delivery_state")
                            in {"FRESH", "CACHED", "STALE_CACHE"}):
                        summary["delivery_state"] = source["delivery_state"]
                    activity["bing_coverage"] = summary
                    original_fetched_at = (summary.get("fetched_at")
                                           or summary.get("cache_fetched_at"))
                    if isinstance(original_fetched_at, str):
                        activity["fetched_at"] = original_fetched_at
            if source_id == "youtube_video_search":
                coverage = source.get("coverage")
                if isinstance(coverage, dict):
                    summary = {}
                    for key in (
                        "provider_method", "provider_id", "query_planner_version",
                        "query_plan_id", "market_filter", "language_filter",
                    ):
                        value = coverage.get(key)
                        if isinstance(value, str) and 0 < len(value) <= 128:
                            summary[key] = value
                    for key in ("query_plan_version", "provider_request_count",
                                "records_omitted_storage_limit"):
                        value = coverage.get(key)
                        if type(value) is int and value >= 0:
                            summary[key] = value
                    summary["source_fetched_at"] = _safe_utc_timestamp(
                        coverage.get("source_fetched_at")
                    )
                    summary["cache_delivery_at"] = _safe_utc_timestamp(
                        coverage.get("cache_delivery_at")
                    )
                    query_summaries = []
                    queries = coverage.get("queries")
                    if isinstance(queries, list):
                        for query in queries[:2]:
                            if not isinstance(query, dict):
                                continue
                            item = {}
                            for key in ("original_query", "query_source", "taxonomy_scope",
                                        "market_filter", "language_filter", "reason"):
                                value = query.get(key)
                                if isinstance(value, str) and len(value) <= 256:
                                    item[key] = value
                            for key in ("pages", "accepted", "rejected"):
                                value = query.get(key)
                                if type(value) is int and value >= 0:
                                    item[key] = value
                            item["request_timestamps"] = [
                                timestamp for raw in query.get("request_timestamps", [])[:3]
                                if (timestamp := _safe_utc_timestamp(raw)) is not None
                            ] if isinstance(query.get("request_timestamps", []), list) else []
                            query_summaries.append(item)
                    summary["queries"] = query_summaries
                    activity["youtube_coverage"] = summary
                    activity["last_cache_delivery_at"] = summary["cache_delivery_at"]
            latest[source_id] = activity
    if latest_account_snapshot is not None:
        latest["_serpapi_account_snapshot"] = latest_account_snapshot
    youtube_activity = latest.get("youtube_video_search")
    if isinstance(youtube_activity, dict) and latest_youtube_observation is not None:
        youtube_activity["last_successful_observation_at"] = (
            latest_youtube_observation["fetched_at"]
        )
        youtube_activity["last_successful_observation_record_count"] = (
            latest_youtube_observation["record_count"]
        )
        youtube_activity["fetched_at"] = latest_youtube_observation["fetched_at"]
    return latest


def _local_live_verification_status(
    source, activity, normal_local_live_runtime, normal_youtube_live_runtime=False
):
    if not (normal_local_live_runtime and isinstance(activity, dict)):
        return source.local_live_verification_status

    successful_live_delivery = (
        activity.get("status") in {"SUCCESS", "PARTIAL"}
        and activity.get("data_mode") == "LIVE"
    )
    if (successful_live_delivery
            and source.source_id in {"google_web_serp", "bing_web_serp"}):
        return "LOCAL_LIVE_VERIFIED"

    youtube_coverage = activity.get("youtube_coverage")
    youtube_request_count = (
        youtube_coverage.get("provider_request_count")
        if isinstance(youtube_coverage, dict) else None
    )
    record_count = activity.get("record_count")
    if (successful_live_delivery
            and source.source_id == "youtube_video_search"
            and normal_youtube_live_runtime
            and activity.get("delivery_state") == "FRESH"
            and type(record_count) is int
            and record_count > 0
            and type(youtube_request_count) is int
            and youtube_request_count > 0
            and youtube_coverage.get("provider_method") in {"search.list", "videos.list"}):
        return "LOCAL_LIVE_VERIFIED"
    return source.local_live_verification_status


def _merge_saved_bing_evidence(latest_activity, older_source):
    """Keep original Bing coverage visible after a metadata-light cache delivery."""
    if (not isinstance(latest_activity, dict)
            or latest_activity.get("data_mode") != "LIVE"
            or not isinstance(older_source, dict)
            or older_source.get("status") not in {"SUCCESS", "PARTIAL"}):
        return
    coverage = latest_activity.get("bing_coverage")
    older = older_source.get("coverage")
    if (not isinstance(coverage, dict)
            or coverage.get("delivery_state") != "CACHED"
            or not isinstance(older, dict)
            or older.get("discovery_mode") != "SAVED_GOOGLE_CANDIDATE_BING_SERP"):
        return

    for key in ("pages", "provider_result_count", "accepted_result_count",
                "rejected_result_count"):
        value = older.get(key)
        if key not in coverage and type(value) is int and value >= 0:
            coverage[key] = value
    for key in ("query", "market", "language", "provider_cache_state",
                "provider_created_at", "fetched_at", "candidate_source_id",
                "candidate_selection_reason"):
        value = older.get(key)
        if key not in coverage and isinstance(value, str) and len(value) <= 256:
            coverage[key] = value
    if "fetched_at" not in coverage:
        original_fetched_at = coverage.get("cache_fetched_at")
        if isinstance(original_fetched_at, str) and len(original_fetched_at) <= 64:
            coverage["fetched_at"] = original_fetched_at
    if isinstance(coverage.get("fetched_at"), str):
        latest_activity["fetched_at"] = coverage["fetched_at"]


@router.get("/sources/library")
def source_library():
    """Return the dated research inventory separately from runtime adapters."""
    try:
        payload = json.loads(_RESEARCH_CATALOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(503, "Source research catalog is unavailable") from None
    return payload


@router.get("/capabilities")
async def capabilities(request: Request, response: Response):
    settings = request.app.state.settings
    enabled = bool(request.app.state.public_fetch_enabled)
    block_reason = request.app.state.public_fetch_block_reason
    if enabled:
        distributed = request.app.state.distributed_public_fetch
        if distributed is not None:
            try:
                enabled = distributed.admission.operationally_enabled()
                if not enabled:
                    block_reason = "FEATURE_DISABLED"
            except Exception:
                enabled = False
                block_reason = "ADMISSION_STATE_UNAVAILABLE"
        else:
            run_service = request.app.state.run_service
            if run_service is None:
                enabled = False
                block_reason = "SYNTHETIC_RUNTIME_UNAVAILABLE"
            else:
                try:
                    block_reason = await request.app.state.fetch_registry.admission_reason(
                        storage=run_service.storage,
                        run_lock=run_service.lock,
                    )
                    enabled = block_reason is None
                except (OSError, StorageError):
                    enabled = False
                    block_reason = "ADMISSION_STATE_UNAVAILABLE"
    plan = get_query_plan(settings.query_plan_id, settings.query_plan_version)
    saved_activity = _saved_source_activity(request)
    distributed = request.app.state.distributed_public_fetch
    google_workflow = getattr(distributed, "google_capabilities", {})
    normal_local_live_runtime = bool(
        distributed is not None
        and (getattr(distributed, "normal_wikimedia_live", False)
             or getattr(distributed, "normal_google_live", False)
             or getattr(distributed, "normal_bing_live", False)
             or getattr(distributed, "normal_youtube_live", False))
        and distributed.data_mode == "LIVE"
    )
    google_quota = (distributed.google_quota_metadata()
                    if distributed is not None
                    and callable(getattr(distributed, "google_quota_metadata", None))
                    else None)
    youtube_workflow = getattr(distributed, "youtube_capabilities", {
        "approval_status": "PENDING_LIVE_APPROVAL",
        "configuration_status": "NOT_CONFIGURED",
        "live_status": "CACHE_ONLY",
        "provider_quota_status": "UNKNOWN",
        "keyword_extraction": "DISABLED_PENDING_POLICY_APPROVAL",
        "derived_metrics": "DISABLED_PENDING_POLICY_APPROVAL",
    })
    youtube_quota = (distributed.youtube_quota_metadata()
                     if distributed is not None
                     and callable(getattr(distributed, "youtube_quota_metadata", None))
                     else None)
    if (isinstance(google_quota, dict)
            and google_quota.get("provider_account") is None
            and google_quota.get("provider_account_status")
            in {"NOT_CHECKED", "NOT_CONFIGURED", "UNAVAILABLE"}):
        saved_account = _latest_saved_account_snapshot(saved_activity)
        if saved_account is not None:
            google_quota = {
                **google_quota,
                "provider_account": saved_account,
                "provider_account_status": "STALE",
            }
    response.headers["Cache-Control"] = "no-store"
    return {
        "contract_version": "1.0",
        "environment_mode": settings.environment_profile,
        "read_only": request.app.state.read_only,
        "authentication_required": False,
        "public_fetch_supported": True,
        "source_selection_supported": distributed is not None,
        "google_workflow": getattr(distributed, "google_capabilities", {
            "trends_discovery": "NOT_CONFIGURED",
            "rss_discovery": "NOT_CONFIGURED",
            "serp_verification": "NOT_CONFIGURED",
            "bing_serp_verification": "NOT_APPROVED",
            "account_api": "NOT_CONFIGURED",
            "live_verified": False,
        }),
        "google_quota": google_quota,
        "youtube_workflow": youtube_workflow,
        "youtube_quota": youtube_quota,
        "refresh_schedule": (distributed.refresh_schedule_metadata()
                             if distributed is not None
                             and callable(getattr(distributed, "refresh_schedule_metadata", None))
                             else None),
        "public_fetch_enabled": enabled,
        "fetch_enabled": enabled,
        "fetch_block_reason": block_reason,
        "data_mode": (request.app.state.distributed_public_fetch.data_mode
                      if request.app.state.public_fetch_enabled
                      and request.app.state.distributed_public_fetch is not None
                      else "SYNTHETIC" if request.app.state.public_fetch_enabled else None),
        "local_live_demo": bool(enabled and settings.environment_profile == "local"
                                and request.app.state.distributed_public_fetch is not None
                                and request.app.state.distributed_public_fetch.data_mode == "LIVE"),
        "allowed_operations": {"fetch": enabled},
        "query_plan": {
            "id": plan.plan_id,
            "version": plan.version,
            "fingerprint": plan.fingerprint,
            "market": plan.market,
            "language": plan.language,
            "request_budget_per_source": 1,
            "max_pages_per_query": 1,
        },
        "admission": {
            "global_fetch_limit": (
                request.app.state.distributed_public_fetch.admission.policy.max_starts_per_window
                if request.app.state.distributed_public_fetch is not None
                else request.app.state.public_fetch_limit
            ),
            "window_seconds": (
                request.app.state.distributed_public_fetch.admission.policy.window_seconds
                if request.app.state.distributed_public_fetch is not None
                else request.app.state.public_fetch_window_seconds
            ),
            "max_concurrent": 1,
            "cooldown_seconds": (
                request.app.state.distributed_public_fetch.admission.policy.cooldown_seconds
                if request.app.state.distributed_public_fetch is not None
                else settings.run_cooldown_seconds
            ),
            "status_poll_limit": (
                request.app.state.distributed_public_fetch.admission.policy.max_status_polls
                if request.app.state.distributed_public_fetch is not None
                else request.app.state.public_fetch_poll_limit
            ),
            "idempotency_ttl_seconds": (
                request.app.state.distributed_public_fetch.admission.policy.receipt_seconds
                if request.app.state.distributed_public_fetch is not None
                else request.app.state.public_fetch_ttl_seconds
            ),
            "guarantee_scope": (
                "injected AtomicStateStore; backing guarantees are environment-specific"
                if request.app.state.distributed_public_fetch is not None
                else "single local process; resets on restart"
            ),
        },
        "sources": [
            {
                "source_id": item.source_id,
                "display_name": item.display_name,
                "icon": item.icon,
                "provider_id": item.provider_id,
                "category": ("trends_search_interest" if item.source_id == "wikimedia_pageviews_top_per_country" else "video_streaming" if item.source_id == "youtube_video_search" else "search_engines"),
                "documentation_url": ("https://doc.wikimedia.org/generated-data-platform/aqs/" if item.source_id == "wikimedia_pageviews_top_per_country" else "https://developers.google.com/youtube/data/reference/rest/v3/search/list?hl=en" if item.source_id == "youtube_video_search" else "https://serpapi.com/bing-search-api" if item.source_id == "bing_web_serp" else "https://serpapi.com/search-api"),
                "quota": {"status": "UNKNOWN", "remaining": None, "total": None, "reset_at": None},
                "adapter_status": (
                    "LIVE_CONFIGURED"
                    if item.source_id == "bing_web_serp"
                    and getattr(distributed, "normal_bing_live", False)
                    else "LIVE_CONFIGURED"
                    if item.source_id == "google_web_serp"
                    and (google_workflow.get("trends_discovery") == "LIVE_CONFIGURED"
                         or google_workflow.get("rss_discovery") == "LIVE_CONFIGURED")
                    else "CACHE_ONLY"
                    if item.source_id == "google_web_serp"
                    and google_workflow.get("trends_discovery") == "CACHE_ONLY"
                    else item.adapter_status
                ),
                "approval_status": (
                    youtube_workflow.get("approval_status", item.approval_status)
                    if item.source_id == "youtube_video_search"
                    else "APPROVED_FOR_LOCAL_RUNTIME"
                    if item.source_id == "bing_web_serp"
                    and getattr(distributed, "normal_bing_live", False)
                    else "APPROVED_FOR_LOCAL_RUNTIME"
                    if item.source_id == "google_web_serp"
                    and (google_workflow.get("trends_discovery") == "LIVE_CONFIGURED"
                         or google_workflow.get("rss_discovery") == "LIVE_CONFIGURED")
                    else "CACHE_DELIVERY_ONLY"
                    if item.source_id == "google_web_serp"
                    and google_workflow.get("trends_discovery") == "CACHE_ONLY"
                    else item.approval_status
                ),
                "configuration_status": (
                    youtube_workflow.get("configuration_status", item.configuration_status)
                    if item.source_id == "youtube_video_search"
                    else "PRESENT"
                    if item.source_id == "bing_web_serp"
                    and getattr(distributed, "normal_bing_live", False)
                    else "PRESENT"
                    if item.source_id == "google_web_serp"
                    and (google_workflow.get("trends_discovery") == "LIVE_CONFIGURED"
                         or google_workflow.get("rss_discovery") == "LIVE_CONFIGURED")
                    else "CACHE_ONLY"
                    if item.source_id == "google_web_serp"
                    and google_workflow.get("trends_discovery") == "CACHE_ONLY"
                    else item.configuration_status
                ),
                "live_verification_status": item.live_verification_status,
                "local_live_verification_status": _local_live_verification_status(
                    item,
                    saved_activity.get(item.source_id),
                    normal_local_live_runtime,
                    getattr(distributed, "normal_youtube_live", False),
                ),
                "production_readiness": item.production_readiness,
                "supported_data_modes": (["LIVE"] if normal_local_live_runtime
                    and (item.source_id == WIKIMEDIA_SOURCE_ID
                         or getattr(distributed, "normal_google_live", False)
                         and item.source_id == "google_web_serp"
                         or getattr(distributed, "normal_bing_live", False)
                         and item.source_id == "bing_web_serp"
                         or getattr(distributed, "normal_youtube_live", False)
                         and item.source_id == "youtube_video_search")
                    else list(item.supported_data_modes)),
                "query_based": item.query_based,
                "metric": {
                    "name": item.metric_name,
                    "unit": item.metric_unit,
                    "meaning": item.metric_description,
                },
                "market_scope": item.market,
                "language_scope": item.language,
                "known_limitations": list(item.known_limitations),
                "enabled_for_current_operation": (
                    enabled and item.source_id in request.app.state.public_fetch_source_ids
                ),
                "last_saved_fetch": saved_activity.get(item.source_id),
                "disabled_reason": None
                if enabled and item.source_id in request.app.state.public_fetch_source_ids
                else (block_reason or "SOURCE_NOT_ALLOWED"),
            }
            for item in SOURCE_CAPABILITIES
        ],
    }
