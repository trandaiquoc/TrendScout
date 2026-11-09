"""Manual run endpoints; list/search only access stored results."""

import hmac
import unicodedata

from fastapi import APIRouter, Header, HTTPException, Query, Request

from app.orchestration.runs import RunBusy, RunCooldown
from app.storage.base import StorageError

router = APIRouter()


def service(request):
    result = getattr(request.app.state, "run_service", None)
    if result is None:
        raise HTTPException(503, "Storage is not configured")
    return result


def check_run_id(value):
    if len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise HTTPException(404, "Run not found")


async def get_run_or_404(storage, run_id):
    check_run_id(run_id)
    run = await storage.get_run(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


def ensure_results_available(run):
    if run.get("status") not in ("COMPLETED", "PARTIAL") or "keyword_count" not in run:
        raise HTTPException(409, "Results are not available for this run")


@router.post("/runs", status_code=202)
async def create_run(request: Request, authorization: str | None = Header(default=None)):
    token = getattr(request.app.state, "operator_token", None)
    if not token or len(token) < 24:
        raise HTTPException(503, "Operator token is not configured")
    if not authorization or not hmac.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(401, "Operator authentication required", headers={"WWW-Authenticate": "Bearer"})
    try:
        run_id = await service(request).start()
    except RunBusy:
        raise HTTPException(409, "Another run is active")
    except RunCooldown:
        raise HTTPException(429, "Run cooldown is active")
    return {"run_id": run_id, "status": "QUEUED"}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    return await get_run_or_404(service(request).storage, run_id)


@router.get("/runs/{run_id}/keywords")
async def list_keywords(run_id: str, request: Request, q: str = Query(default="", max_length=100),
                        category: str = Query(default="all", pattern="^(all|fashion|home_goods|unclassified)$"),
                        sort: str = Query(default="score", pattern="^(score|views|keyword)$"),
                        limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0, le=10000)):
    storage = service(request).storage
    run = await get_run_or_404(storage, run_id)
    ensure_results_available(run)
    shards = await storage.list_records(run_id, "keyword_results")
    items = [item for shard in shards for item in shard.get("items", [])]
    if len(items) != run["keyword_count"]:
        raise HTTPException(503, "Stored results are incomplete")
    query = " ".join(unicodedata.normalize("NFC", q).casefold().split())
    items = [item for item in items if (not query or query in item["search_keyword"]) and
             (category == "all" or category in item["categories"])]
    if sort == "keyword":
        items.sort(key=lambda item: (item["search_keyword"], item["keyword_id"]))
    else:
        field = "score" if sort == "score" else "current_views_ceil"
        items.sort(key=lambda item: (item.get(field) is None, -(item[field] if item.get(field) is not None else 0), item["keyword_id"]))
    return {"items": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit,
            "next_offset": offset + limit if offset + limit < len(items) else None,
            "empty_reason": "NO_MATCHES_IN_ANALYZED_DATA" if not items else None}


@router.get("/runs/{run_id}/keywords/{keyword_id}")
async def get_keyword(run_id: str, keyword_id: str, request: Request):
    if len(keyword_id) != 64 or any(c not in "0123456789abcdef" for c in keyword_id):
        raise HTTPException(404, "Keyword not found")
    storage = service(request).storage
    run = await get_run_or_404(storage, run_id)
    ensure_results_available(run)
    shard = await storage.get_record(run_id, "keyword_results", f"shard-{int(keyword_id[0], 16):02d}")
    if shard is None:
        raise HTTPException(503, "Stored results are incomplete")
    for item in shard.get("items", []):
        if item.get("keyword_id") == keyword_id:
            return item
    raise HTTPException(404, "Keyword not found")
