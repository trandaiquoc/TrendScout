"""Public, non-secret view of runtime and source capabilities."""

from fastapi import APIRouter, Request, Response

from app.ports.storage import StorageError
from app.sources.catalog import SOURCE_CAPABILITIES
from app.sources.query_plan import get_query_plan

router = APIRouter()


@router.get("/capabilities")
async def capabilities(request: Request, response: Response):
    settings = request.app.state.settings
    enabled = bool(request.app.state.public_fetch_enabled)
    block_reason = request.app.state.public_fetch_block_reason
    if enabled:
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
    response.headers["Cache-Control"] = "no-store"
    return {
        "contract_version": "1.0",
        "environment_mode": settings.environment_profile,
        "read_only": request.app.state.read_only,
        "authentication_required": False,
        "public_fetch_supported": True,
        "public_fetch_enabled": enabled,
        "fetch_enabled": enabled,
        "fetch_block_reason": block_reason,
        "data_mode": "SYNTHETIC" if request.app.state.public_fetch_enabled else None,
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
            "global_fetch_limit": request.app.state.public_fetch_limit,
            "window_seconds": request.app.state.public_fetch_window_seconds,
            "max_concurrent": 1,
            "cooldown_seconds": settings.run_cooldown_seconds,
            "status_poll_limit": request.app.state.public_fetch_poll_limit,
            "idempotency_ttl_seconds": request.app.state.public_fetch_ttl_seconds,
            "guarantee_scope": "single local process; resets on restart",
        },
        "sources": [
            {
                "source_id": item.source_id,
                "provider_id": item.provider_id,
                "adapter_status": item.adapter_status,
                "approval_status": item.approval_status,
                "configuration_status": item.configuration_status,
                "live_verification_status": item.live_verification_status,
                "supported_data_modes": list(item.supported_data_modes),
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
                "disabled_reason": None
                if enabled and item.source_id in request.app.state.public_fetch_source_ids
                else (block_reason or "SOURCE_NOT_ALLOWED"),
            }
            for item in SOURCE_CAPABILITIES
        ],
    }
