"""Read-only algorithm registry endpoint."""
from fastapi import APIRouter, HTTPException, Query, Request

from app.algorithms.catalog import catalog
from app.api.fetches import validate_public_request_origin

router = APIRouter()


@router.get('/algorithms')
def list_algorithms():
    return catalog()


def algorithm_results_for_service(service, *, limit=100, offset=0, source="",
                                  algorithm_id="", status=""):
    return service.store.algorithm_results_overview(
        limit=limit, offset=offset, source=source,
        algorithm_id=algorithm_id, status=status,
    )


@router.get('/algorithms/results')
def list_algorithm_results(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    source: str = Query(default="", max_length=100),
    algorithm_id: str = Query(default="", max_length=100),
    status: str = Query(default="", pattern="^(|COMPLETED|SKIPPED|INSUFFICIENT_DATA|FAILED)$"),
):
    validate_public_request_origin(request, request.app.state.settings.environment_profile)
    request_state = getattr(request, 'state', None)
    if (getattr(request_state, 'algorithm_results_available', False)
            and isinstance(getattr(request_state, 'algorithm_results', None), dict)
            and limit <= 100 and offset == 0 and not source and not algorithm_id and not status):
        projection = request_state.algorithm_results
        items = projection['items'][:limit]
        total = projection.get("total")
        result = {
            **projection, "items": items, "total": total, "limit": limit,
            "offset": 0,
            "next_offset": (limit if total is not None and total > limit else None),
        }
    else:
        service = (getattr(request_state, 'collection_service', None)
                   or getattr(request.app.state, 'collection_service', None))
        if service is None:
            raise HTTPException(503, {'code': 'ANALYTICS_UNAVAILABLE'})
        result = algorithm_results_for_service(
            service, limit=limit, offset=offset, source=source,
            algorithm_id=algorithm_id, status=status,
        )
    algorithm_catalog = catalog()
    return {**result, 'evaluation_status': algorithm_catalog['evaluation_status'],
            'catalog': algorithm_catalog}
