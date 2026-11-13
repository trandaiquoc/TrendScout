"""Read-only algorithm registry endpoint."""
from fastapi import APIRouter, HTTPException, Query, Request

from app.algorithms.catalog import catalog
from app.api.fetches import validate_public_request_origin

router = APIRouter()


@router.get('/algorithms')
def list_algorithms():
    return catalog()


@router.get('/algorithms/results')
def list_algorithm_results(request: Request, limit: int = Query(default=100, ge=1, le=500)):
    validate_public_request_origin(request, request.app.state.settings.environment_profile)
    service = getattr(request.app.state, 'collection_service', None)
    if service is None:
        raise HTTPException(503, {'code': 'ANALYTICS_UNAVAILABLE'})
    with service.store.connect() as db:
        counts = db.execute('''SELECT algorithm_id,version,
            sum(status='COMPLETED') AS completed,sum(status='SKIPPED') AS skipped,
            sum(status='FAILED') AS failed,count(*) AS total, max(updated_at) AS updated_at
            FROM algorithm_results GROUP BY algorithm_id,version ORDER BY algorithm_id''').fetchall()
        rows = db.execute('''SELECT r.keyword_id,k.keyword,r.algorithm_id,r.version,r.status,r.reason_code,
            r.value,r.metric,r.unit,r.result_json,r.updated_at FROM algorithm_results r
            JOIN keywords k ON k.id=r.keyword_id WHERE k.eligible=1
            ORDER BY r.updated_at DESC,k.keyword COLLATE NOCASE,r.algorithm_id LIMIT ?''', (limit,)).fetchall()
    return {'summary': [dict(row) for row in counts], 'items': [dict(row) for row in rows],
            'limit': limit, 'evaluation_status': catalog()['evaluation_status']}
