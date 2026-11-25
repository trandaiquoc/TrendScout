"""Read-only restoration and search over the durable local keyword ledger."""
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.collections import _service
from app.collections.source_digest import PRIVATE_SOURCE_DIGEST_FIELDS

router = APIRouter()


@router.get('/datasets/latest')
def latest_dataset(request: Request):
    if getattr(request.state, 'latest_dataset_summary_available', False):
        item = request.state.latest_dataset_summary
    else:
        service = _service(request)
        item = service.store.latest_dataset()
    if item is None:
        return {'item': None}
    manifest = item.get('source_manifest')
    if isinstance(manifest, list):
        item['source_manifest'] = [
            {key: value for key, value in source.items()
             if key not in PRIVATE_SOURCE_DIGEST_FIELDS}
            if isinstance(source, dict) else source
            for source in manifest
        ]
    item['restoration_status'] = 'CACHED'
    collection_at = item.get('created_at') or item.get('fetched_at')
    if collection_at:
        try:
            stamp = datetime.fromisoformat(collection_at.replace('Z', '+00:00'))
            item['age_seconds'] = max(0, int((datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()))
        except (ValueError, TypeError):
            item['age_seconds'] = None
    else:
        item['age_seconds'] = None
    return {'item': item}


@router.get('/datasets/latest/keywords')
def latest_keywords(request: Request, q: str = Query(default='', max_length=100),
                    state: str = Query(default='all', pattern='^(all|unassigned|ai|manual|mixed|needs_review|excluded|non_topic)$'),
                    classification: Literal['all', 'CLASSIFIED', 'AMBIGUOUS', 'UNKNOWN', 'INELIGIBLE'] = 'all',
                    evidence: Literal['all', 'metric', 'algorithm', 'missing'] = 'all',
                    collection_ids: list[str] = Query(default=[]), source: str = Query(default='', max_length=100),
                    sort: str = Query(default='recent', pattern='^(recent|keyword|views|rank)$'),
                    date_from: date | None = None, date_to: date | None = None,
                    limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0, le=1000000),
                    include_algorithm_results: bool = True):
    service = _service(request)
    return latest_keywords_for_service(
        service, q=q, state=state, classification=classification, evidence=evidence,
        collection_ids=collection_ids, source=source, sort=sort,
        date_from=date_from.isoformat() if date_from else "",
        date_to=date_to.isoformat() if date_to else "",
        limit=limit, offset=offset,
        include_algorithm_results=include_algorithm_results,
    )


def latest_keywords_for_service(service, *, q="", state="all", classification="all",
                                evidence="all", collection_ids=(), source="", sort="recent",
                                date_from="", date_to="", limit=50, offset=0,
                                include_algorithm_results=True):
    """Run the validated keyword query for an already-authorized service."""
    if len(collection_ids) > 20 or any(not value or len(value) > 64 for value in collection_ids):
        raise HTTPException(422, {'code': 'INVALID_COLLECTION_FILTER'})
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, {'code': 'INVALID_OBSERVATION_DATE_RANGE'})
    try:
        result = service.store.search_keywords(query=q, state=state, collection_ids=collection_ids,
                                               source=source, sort=sort, limit=limit, offset=offset,
                                               classification=classification, evidence=evidence,
                                               include_algorithm_results=include_algorithm_results,
                                               date_from=date_from, date_to=date_to)
    except ValueError:
        raise HTTPException(422, {'code': 'INVALID_KEYWORD_QUERY'}) from None
    result['state'] = state
    return result
