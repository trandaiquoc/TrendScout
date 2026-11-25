"""Read-only algorithm registry endpoint."""
from fastapi import APIRouter, HTTPException, Query, Request

from app.algorithms.catalog import catalog
from app.api.fetches import validate_public_request_origin

router = APIRouter()


@router.get('/algorithms')
def list_algorithms():
    return catalog()


def algorithm_results_for_service(service, *, limit=100):
    mode = service.store.data_mode_scope
    keyword_filter = " AND k.data_mode=?" if mode else ""
    parameters = (mode,) if mode else ()
    with service.store.connect() as db:
        counts = db.execute('''SELECT r.algorithm_id,r.version,
            sum(status='COMPLETED') AS completed,sum(status='SKIPPED') AS skipped,
            sum(status='FAILED') AS failed,count(*) AS total, max(updated_at) AS updated_at
            FROM algorithm_results r JOIN keywords k ON k.id=r.keyword_id
            LEFT JOIN classification_results cr ON cr.keyword_id=k.id
            WHERE COALESCE(cr.classification_status,'')<>'INELIGIBLE' '''
            + keyword_filter + ' GROUP BY r.algorithm_id,r.version ORDER BY r.algorithm_id', parameters).fetchall()
        rows = db.execute('''SELECT r.keyword_id,k.keyword,r.algorithm_id,r.version,r.status,r.reason_code,
            r.value,r.metric,r.unit,r.result_json,r.updated_at FROM algorithm_results r
            JOIN keywords k ON k.id=r.keyword_id LEFT JOIN classification_results cr ON cr.keyword_id=k.id
            WHERE k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE' '''
            + keyword_filter + ' ORDER BY r.updated_at DESC,k.keyword COLLATE NOCASE,r.algorithm_id LIMIT ?',
            (*parameters, limit)).fetchall()
    return {'summary': [dict(row) for row in counts], 'items': [dict(row) for row in rows],
            'limit': limit}


@router.get('/algorithms/results')
def list_algorithm_results(request: Request, limit: int = Query(default=100, ge=1, le=500)):
    validate_public_request_origin(request, request.app.state.settings.environment_profile)
    request_state = getattr(request, 'state', None)
    if (getattr(request_state, 'algorithm_results_available', False)
            and isinstance(getattr(request_state, 'algorithm_results', None), dict)
            and limit <= 100):
        projection = request_state.algorithm_results
        result = {**projection, 'items': projection['items'][:limit], 'limit': limit}
    else:
        service = (getattr(request_state, 'collection_service', None)
                   or getattr(request.app.state, 'collection_service', None))
        if service is None:
            raise HTTPException(503, {'code': 'ANALYTICS_UNAVAILABLE'})
        result = algorithm_results_for_service(service, limit=limit)
    algorithm_catalog = catalog()
    return {**result, 'evaluation_status': algorithm_catalog['evaluation_status'],
            'catalog': algorithm_catalog}
