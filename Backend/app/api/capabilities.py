"""Public, non-secret view of runtime and source capabilities."""

import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from app.ports.storage import StorageError
from app.sources.catalog import SOURCE_CAPABILITIES, WIKIMEDIA_SOURCE_ID
from app.sources.query_plan import get_query_plan

router = APIRouter()
_RESEARCH_CATALOG = Path(__file__).resolve().parents[1] / "sources" / "research_catalog.json"


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
            if not isinstance(source_id, str) or source_id in latest:
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
                    if source.get("delivery_state") in {"FRESH", "CACHED"}:
                        summary["delivery_state"] = source["delivery_state"]
                    activity["google_coverage"] = summary
            latest[source_id] = activity
    return latest


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
    normal_local_live_runtime = bool(
        distributed is not None
        and (getattr(distributed, "normal_wikimedia_live", False)
             or getattr(distributed, "normal_google_live", False))
        and distributed.data_mode == "LIVE"
    )
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
            "account_api": "NOT_CONFIGURED",
            "live_verified": False,
        }),
        "google_quota": (distributed.google_quota_metadata()
                         if distributed is not None
                         and callable(getattr(distributed, "google_quota_metadata", None))
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
                "documentation_url": ("https://doc.wikimedia.org/generated-data-platform/aqs/" if item.source_id == "wikimedia_pageviews_top_per_country" else "https://developers.google.com/youtube/v3/docs/search/list" if item.source_id == "youtube_video_search" else "https://serpapi.com/search-api"),
                "quota": {"status": "UNKNOWN", "remaining": None, "total": None, "reset_at": None},
                "adapter_status": item.adapter_status,
                "approval_status": item.approval_status,
                "configuration_status": item.configuration_status,
                "live_verification_status": item.live_verification_status,
                "local_live_verification_status": item.local_live_verification_status,
                "production_readiness": item.production_readiness,
                "supported_data_modes": (["LIVE"] if normal_local_live_runtime
                    and (item.source_id == WIKIMEDIA_SOURCE_ID
                         or getattr(distributed, "normal_google_live", False)
                         and item.source_id == "google_web_serp")
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
