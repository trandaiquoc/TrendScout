"""Read-only summaries over persisted local keyword and algorithm evidence."""

import base64
import binascii
import json
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.collections import _service
from app.collections import embeddings
from app.ports.storage import StorageError
from app.taxonomy.classification_policy import VERSION as CLASSIFICATION_POLICY_VERSION
from app.taxonomy.registry import TAXONOMY_VERSION

router = APIRouter()


@router.get("/runtime")
def runtime_status(request: Request):
    service = _service(request)
    try:
        fastembed_version = version("fastembed")
    except PackageNotFoundError:
        fastembed_version = None
    with service.store.connect() as db:
        cache = db.execute(
            """SELECT count(*) AS vectors,coalesce(sum(dimension),0) AS dimensions,
            group_concat(DISTINCT dimension) AS vector_dimensions FROM cache WHERE model_version=?""",
            (embeddings.MODEL_VERSION,),
        ).fetchone()
        integrity = db.execute("PRAGMA quick_check(1)").fetchone()[0]
        keyword_count = db.execute("SELECT count(*) FROM keywords").fetchone()[0]
    database_path = getattr(service.store, "path", None)
    try:
        database_size_bytes = int(database_path.stat().st_size) if database_path else None
    except OSError:
        database_size_bytes = None
    return {
        "embedding": {
            "package": "fastembed",
            "package_version": fastembed_version,
            "model": embeddings.MODEL_NAME,
            "model_version": embeddings.MODEL_VERSION,
            "inference_device": "CPU",
            "adapter_installed": bool(fastembed_version),
            "cache_vectors": cache["vectors"] or 0,
            "cached_dimensions_total": cache["dimensions"] or 0,
            "vector_dimensions": cache["vector_dimensions"],
            "model_load_status": "LAZY_ON_DEMAND",
            "classification_policy_version": CLASSIFICATION_POLICY_VERSION,
            "taxonomy_version": TAXONOMY_VERSION,
        },
        "llm": {"status": "NOT_CONFIGURED", "provider": None, "usage_instrumented": False},
        "scheduler": _scheduler_status(request),
        "system": {
            "status": "AVAILABLE",
            "environment_profile": request.app.state.settings.environment_profile,
            "run_mode": request.app.state.settings.run_mode,
            "read_only": bool(request.app.state.read_only),
            "configured_sources": len(request.app.state.settings.enabled_source_ids),
        },
        "storage": {
            "status": "AVAILABLE" if integrity == "ok" else "CHECK_REQUIRED",
            "engine": "SQLITE",
            "integrity": "OK" if integrity == "ok" else "CHECK_REQUIRED",
            "keyword_records": keyword_count,
            "database_size_bytes": database_size_bytes,
            "read_only": bool(request.app.state.read_only),
        },
    }


def _scheduler_status(request: Request):
    settings = request.app.state.settings
    worker = getattr(request.app.state, "scheduler_worker", None)
    running = bool(worker and worker._task and not worker._task.done())
    enabled = bool(settings.scheduler_enabled)
    return {
        "status": "RUNNING" if running else "DISABLED",
        "implemented": True,
        "enabled": enabled,
        "worker_running": running,
        "execution_policy": "LOCAL_SYNTHETIC_ONLY",
        "reason": None if running else (
            "Worker is not running." if enabled else "Disabled in backend configuration; no scheduled Fetch is started."
        ),
    }


@router.get("/analytics/overview")
def overview(request: Request):
    service = _service(request)
    with service.store.connect() as db:
        totals = db.execute("""SELECT count(*) AS keywords,
            sum(CASE WHEN eligible=1 THEN 1 ELSE 0 END) AS eligible,
            sum(CASE WHEN eligible=0 THEN 1 ELSE 0 END) AS excluded
            FROM keywords""").fetchone()
        classifications = db.execute("""SELECT classification_status,count(*) AS count
            FROM classification_results GROUP BY classification_status""").fetchall()
        source_rows = db.execute("""SELECT source,count(DISTINCT id) AS keywords,
            min(substr(observed_at,1,10)) AS first_seen,max(substr(observed_at,1,10)) AS last_seen
            FROM keywords GROUP BY source ORDER BY keywords DESC,source""").fetchall()
        algorithm_rows = db.execute("""SELECT algorithm_id,version,status,count(*) AS count
            FROM algorithm_results GROUP BY algorithm_id,version,status ORDER BY algorithm_id,status""").fetchall()
        observations = db.execute("""SELECT source,observation_date,count(DISTINCT keyword_id) AS keywords,
            sum(status='OBSERVED') AS observed,sum(status<>'OBSERVED') AS unavailable
            FROM algorithm_observations GROUP BY source,observation_date ORDER BY observation_date,source""").fetchall()
        top = db.execute("""SELECT k.id,k.keyword,k.source,k.project,k.observed_at,r.value,r.metric,r.unit,r.updated_at,r.result_json
            FROM algorithm_results r JOIN keywords k ON k.id=r.keyword_id
            WHERE r.algorithm_id='observed-volume-rank' AND r.status='COMPLETED' AND r.value IS NOT NULL
            AND k.eligible=1 ORDER BY r.updated_at DESC,k.keyword COLLATE NOCASE LIMIT 1000""").fetchall()
        latest = db.execute("""SELECT dataset_id,status,data_mode,source_manifest,provider_record_count,
            received_record_count,retained_keyword_count,rejected_record_count,observation_start,
            observation_end,fetched_at,quality_flags,processing_status,eligible_keyword_count,
            assigned_keyword_count,unassigned_keyword_count FROM dataset_snapshots
            ORDER BY created_at DESC LIMIT 1""").fetchone()
        history = db.execute("""SELECT dataset_id,status,data_mode,source_manifest,provider_record_count,
            received_record_count,retained_keyword_count,rejected_record_count,observation_start,
            observation_end,fetched_at,quality_flags,processing_status FROM dataset_snapshots
            ORDER BY created_at DESC LIMIT 30""").fetchall()
        successful = db.execute("""SELECT dataset_id,status,data_mode,source_manifest,fetched_at,observation_start,
            observation_end,quality_flags FROM dataset_snapshots WHERE status IN ('SUCCESS','PARTIAL')
            ORDER BY created_at DESC LIMIT 1""").fetchone()
        membership_count = db.execute(
            "SELECT count(DISTINCT keyword_id) FROM memberships"
        ).fetchone()[0]
    classified = sum(
        int(row["count"]) for row in classifications if row["classification_status"] == "CLASSIFIED"
    )
    ambiguous = sum(
        int(row["count"]) for row in classifications if row["classification_status"] == "AMBIGUOUS"
    )
    unknown = sum(
        int(row["count"]) for row in classifications if row["classification_status"] == "UNKNOWN"
    )
    ineligible = sum(
        int(row["count"]) for row in classifications if row["classification_status"] == "INELIGIBLE"
    )
    latest_item = dict(latest) if latest else None
    if latest_item:
        for key in ("source_manifest", "quality_flags"):
            try:
                latest_item[key] = json.loads(latest_item[key])
            except (TypeError, ValueError):
                latest_item[key] = []
    snapshots = []
    for row in history:
        item = dict(row)
        for key in ("source_manifest", "quality_flags"):
            try:
                item[key] = json.loads(item[key])
            except (TypeError, ValueError):
                item[key] = []
        snapshots.append(item)
    cohorts = {}
    for row in top:
        item = dict(row)
        try:
            result = json.loads(item.pop("result_json") or "{}")
        except (TypeError, ValueError):
            result = {}
        try:
            evidence = dict(result.get("evidence") or [])
        except (TypeError, ValueError):
            evidence = {}
        scope = evidence.get("scope") or []
        observation_date = (
            evidence.get("observation_date") or str(item.get("observed_at") or "")[:10]
        )
        group_key = (observation_date, tuple(scope))
        item.update(
            rank=item["value"],
            observed_value=result.get("current_value"),
            metric=scope[4] if len(scope) > 4 else None,
            unit=scope[5] if len(scope) > 5 else None,
        )
        cohort = cohorts.setdefault(
            group_key,
            {
                "items": [],
                "context": {
                    "source": item["source"],
                    "project": item["project"],
                    "observation_date": observation_date,
                    "cohort_size": evidence.get("cohort_size"),
                    "scope": scope,
                    "quality_flags": result.get("quality_flags") or [],
                },
            },
        )
        cohort["items"].append(item)
    selected_key = (
        max(
            cohorts,
            key=lambda key: (
                key[0],
                cohorts[key]["context"].get("cohort_size") or len(cohorts[key]["items"]),
                key[1],
            ),
        )
        if cohorts
        else None
    )
    top_items = (
        sorted(
            cohorts[selected_key]["items"],
            key=lambda item: (item["rank"], item["keyword"].casefold()),
        )[:5]
        if selected_key
        else []
    )
    top_context = cohorts[selected_key]["context"] if selected_key else None
    successful_item = dict(successful) if successful else None
    if successful_item:
        for key in ("source_manifest", "quality_flags"):
            try:
                successful_item[key] = json.loads(successful_item[key])
            except (TypeError, ValueError):
                successful_item[key] = []
        try:
            stamp = datetime.fromisoformat(
                (successful_item.get("fetched_at") or "").replace("Z", "+00:00")
            )
            successful_item["age_seconds"] = max(
                0,
                int((datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()),
            )
        except (ValueError, TypeError):
            successful_item["age_seconds"] = None
    return {
        "keyword_totals": {
            "unique": totals["keywords"] or 0,
            "eligible": totals["eligible"] or 0,
            "excluded": totals["excluded"] or 0,
            "classified": classified,
            "ambiguous": ambiguous,
            "unknown": unknown,
            "ineligible": ineligible,
            # Ineligible system/non-topic records are excluded from the topic review backlog.
            "unclassified": ambiguous + unknown,
        },
        "active_sources": (
            [
                {"source_id": source_id}
                for source_id in sorted(
                    getattr(request.app.state, "public_fetch_source_ids", set())
                )
            ]
            if getattr(request.app.state, "public_fetch_enabled", False)
            else []
        ),
        "source_volume": [dict(row) for row in source_rows],
        "algorithm_distribution": [dict(row) for row in algorithm_rows],
        "algorithm_effectiveness": {
            "status": "NOT_YET_EVALUATED",
            "reason": "No comparable labeled historical evaluation is stored.",
        },
        "source_operations": snapshots,
        "multi_source_coverage": {
            "status": "INSUFFICIENT_SOURCES" if len(source_rows) < 2 else "SOURCE_LEDGER_ONLY",
            "sources": [dict(row) for row in source_rows],
            "shared_keyword_overlap": None,
        },
        "observations_over_time": [dict(row) for row in observations],
        "top_observed": top_items,
        "top_observed_context": top_context,
        "latest_dataset": latest_item,
        "last_successful_fetch": successful_item,
        "membership_keyword_count": membership_count,
    }


def _history_storage(request):
    if getattr(request.app.state, "read_only", False):
        factory = getattr(request.app.state, "read_storage_factory", None)
        if not factory:
            return None
        try:
            return factory(request.headers.get("x-vercel-oidc-token"))
        except (ValueError, OSError):
            return None
    service = getattr(request.app.state, "run_service", None)
    return getattr(service, "storage", None)


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _history_cursor_decode(token, filters):
    if not token:
        return {"local": None, "manual": None}
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        value = json.loads(raw)
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise HTTPException(422, {"code": "INVALID_RUN_HISTORY_CURSOR"}) from None
    if (not isinstance(value, dict) or value.get("v") != 1
            or any(value.get(key) != expected for key, expected in filters.items())):
        raise HTTPException(422, {"code": "INVALID_RUN_HISTORY_CURSOR"})
    local = value.get("local")
    manual = value.get("manual")
    if local is not None and (not isinstance(local, list) or len(local) != 2
                              or not all(isinstance(part, str) and part for part in local)):
        raise HTTPException(422, {"code": "INVALID_RUN_HISTORY_CURSOR"})
    if manual is not None and (not isinstance(manual, str) or not manual or len(manual) > 1500):
        raise HTTPException(422, {"code": "INVALID_RUN_HISTORY_CURSOR"})
    return {"local": local, "manual": manual}


def _history_cursor_encode(filters, local, manual):
    value = {"v": 1, **filters, "local": local, "manual": manual}
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _history_matches(item, q, status, mode):
    haystack = " ".join(str(item.get(key) or "") for key in (
        "fetch_id", "run_type", "status", "error_code", "mode", "source_manifest"
    )).casefold()
    return q.casefold() in haystack and (not status or item.get("status") == status) and (not mode or item.get("mode") == mode)


def _history_item_from_job(row):
    item = dict(row)
    try:
        manifest = json.loads(item.get("source_manifest") or "[]")
        item["sources"] = [source.get("source_id") for source in manifest if isinstance(source, dict)]
    except (TypeError, ValueError):
        manifest = []
        item["sources"] = []
    item["run_type"] = "FETCH" if item.get("dataset_id") else (
        "COLLECTION_RECONCILIATION" if item.get("membership_reconciliation_id") else "COLLECTION_PROCESSING"
    )
    item["mode"] = item.pop("data_mode", None)
    item["run_at"] = item.get("fetched_at") or item.get("updated_at")
    item["duration_seconds"] = None
    item["_cursor_local"] = [str(item.get("history_sort_at") or item.get("updated_at") or ""), str(item["fetch_id"])]
    return item


def _history_item_from_manual(run):
    run_id = str(run.get("run_id") or "")
    if not run_id:
        return None
    manifest = run.get("source_manifest") or run.get("sources") or []
    if not isinstance(manifest, list):
        manifest = []
    mode_value = run.get("mode") or run.get("data_mode")
    run_at = run.get("finished_at") or run.get("updated_at") or run.get("created_at")
    return {
        "fetch_id": run_id,
        "run_type": "MANUAL_ANALYSIS",
        "status": run.get("status") or "UNKNOWN",
        "mode": mode_value,
        "data_mode": mode_value,
        "sources": [entry.get("source_id") if isinstance(entry, dict) else str(entry) for entry in manifest],
        "source_manifest": json.dumps(manifest),
        "quality_flags": run.get("quality_flags") or [],
        "error_code": run.get("error_code") or run.get("error"),
        "stage": run.get("status"),
        "available": run.get("keyword_count"),
        "processed": run.get("scored_count"),
        "received_record_count": run.get("keyword_count"),
        "retained_keyword_count": run.get("scored_count"),
        "rejected_record_count": None,
        "run_at": _iso(run_at),
        "created_at": _iso(run.get("created_at")),
        "updated_at": _iso(run.get("updated_at") or run.get("finished_at")),
        "fetched_at": _iso(run.get("finished_at")),
        "duration_seconds": None,
    }


async def _run_history_cursor_page(request, q, status, mode, limit, token):
    filters = {"q": q, "status": status, "mode": mode}
    positions = _history_cursor_decode(token, filters)
    service = getattr(request.app.state, "collection_service", None)
    local_rows = []
    local_has_more = False
    if service:
        with service.store.connect() as db:
            cursor = positions["local"]
            after_sql = ""
            parameters = []
            if cursor:
                after_sql = "WHERE (COALESCE(d.fetched_at,j.updated_at)<? OR (COALESCE(d.fetched_at,j.updated_at)=? AND j.fetch_id<?))"
                parameters.extend((cursor[0], cursor[0], cursor[1]))
            local_rows = db.execute(f"""SELECT j.*,d.dataset_id,d.data_mode,d.source_manifest,d.provider_record_count,
                d.received_record_count,d.rejected_record_count,d.observation_start,d.observation_end,d.fetched_at,
                d.quality_flags,d.processing_status,COALESCE(d.fetched_at,j.updated_at) AS history_sort_at
                FROM fetch_jobs j LEFT JOIN dataset_snapshots d ON d.dataset_id=j.fetch_id
                {after_sql} ORDER BY history_sort_at DESC,j.fetch_id DESC LIMIT ?""",
                (*parameters, limit + 1)).fetchall()
        local_has_more = len(local_rows) > limit
        local_rows = local_rows[:limit]
    local_page = [_history_item_from_job(row) for row in local_rows]

    history_status = "AVAILABLE"
    storage = _history_storage(request)
    manual_page = []
    manual_has_more = False
    history_truncated = False
    if storage is not None and hasattr(storage, "list_runs_after"):
        try:
            batch_size = min(limit + 1, 100)
            runs = await storage.list_runs_after(limit=batch_size, after_run_id=positions["manual"])
            manual_has_more = len(runs) > limit
            if limit == 100 and len(runs) == 100:
                tail_id = str(runs[-1].get("run_id") or "")
                if tail_id:
                    manual_has_more = bool(await storage.list_runs_after(limit=1, after_run_id=tail_id))
            manual_page = [item for item in (_history_item_from_manual(run) for run in runs[:limit]) if item]
        except ValueError:
            raise HTTPException(422, {"code": "INVALID_RUN_HISTORY_CURSOR"}) from None
        except StorageError:
            history_status = "PARTIAL"
    elif storage is not None and hasattr(storage, "list_runs"):
        history_status = "CURSOR_UNAVAILABLE"
        try:
            runs = await storage.list_runs(limit=100, offset=0)
            all_manual = [item for item in (_history_item_from_manual(run) for run in runs) if item]
            if positions["manual"]:
                matching_cursor = next((index for index, item in enumerate(all_manual)
                                        if item["fetch_id"] == positions["manual"]), None)
                if matching_cursor is not None:
                    all_manual = all_manual[matching_cursor + 1:]
                else:
                    all_manual = []
            manual_page = all_manual[:limit]
            manual_has_more = len(all_manual) > limit
            history_truncated = len(runs) == 100 and not manual_has_more
        except StorageError:
            history_status = "PARTIAL"
    else:
        history_status = "LOCAL_LEDGER_ONLY"

    local_ids = {item["fetch_id"] for item in local_page}
    manual_page = [item for item in manual_page if item["fetch_id"] not in local_ids]
    for item in manual_page:
        item["_cursor_manual"] = item["fetch_id"]
    candidates = sorted(local_page + manual_page,
                        key=lambda item: (str(item.get("created_at") if item["run_type"] == "MANUAL_ANALYSIS"
                                              else item.get("_cursor_local", [item.get("run_at") or ""])[0]
                                              or item.get("run_at") or item.get("updated_at") or ""), item["fetch_id"]),
                        reverse=True)
    matches = [item for item in candidates if _history_matches(item, q, status, mode)]
    selected = matches[:limit]
    if len(matches) >= limit:
        cutoff = candidates.index(selected[-1])
        consumed = candidates[:cutoff + 1]
    else:
        consumed = candidates
    next_local, next_manual = positions["local"], positions["manual"]
    for item in consumed:
        if item["run_type"] == "MANUAL_ANALYSIS":
            next_manual = item["_cursor_manual"]
        else:
            next_local = item["_cursor_local"]
    has_unconsumed = len(consumed) < len(candidates)
    more = has_unconsumed or local_has_more or manual_has_more
    for item in selected:
        item.pop("_cursor_local", None)
        item.pop("_cursor_manual", None)
    next_cursor = _history_cursor_encode(filters, next_local, next_manual) if more else None
    return {
        "items": selected,
        "total": None,
        "offset": None,
        "limit": limit,
        "next_offset": None,
        "next_cursor": next_cursor,
        "duration_available": False,
        "history_source": "local_sqlite_and_analysis_storage" if storage is not None else "local_sqlite_fetch_jobs",
        "manual_history_status": history_status,
        "history_truncated": history_truncated,
        "cursor_unavailable": history_status == "CURSOR_UNAVAILABLE",
    }


@router.get("/runs/history")
async def run_history(
    request: Request,
    q: str = Query(default="", max_length=100),
    status: str = Query(default="", max_length=32),
    mode: str = Query(default="", max_length=24),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
    cursor: str | None = Query(default=None, max_length=2048),
):
    if cursor is not None:
        if offset:
            raise HTTPException(422, {"code": "CURSOR_AND_OFFSET_CANNOT_BE_COMBINED"})
        return await _run_history_cursor_page(request, q, status, mode, limit, cursor)
    service = getattr(request.app.state, "collection_service", None)
    if service:
        with service.store.connect() as db:
            rows = db.execute("""SELECT j.*,d.dataset_id,d.data_mode,d.source_manifest,d.provider_record_count,
                d.received_record_count,d.rejected_record_count,d.observation_start,d.observation_end,d.fetched_at,
                d.quality_flags,d.processing_status
                FROM fetch_jobs j LEFT JOIN dataset_snapshots d ON d.dataset_id=j.fetch_id
                ORDER BY j.updated_at DESC,j.fetch_id DESC""").fetchall()
    else:
        rows = []
    items = []
    for row in rows:
        item = dict(row)
        if item.get("source_manifest"):
            try:
                item["sources"] = [
                    source.get("source_id") for source in json.loads(item["source_manifest"])
                ]
            except (TypeError, ValueError):
                item["sources"] = []
        else:
            item["sources"] = []
        item["run_type"] = (
            "FETCH"
            if item.get("dataset_id")
            else (
                "COLLECTION_RECONCILIATION"
                if item.get("membership_reconciliation_id")
                else "COLLECTION_PROCESSING"
            )
        )
        item["mode"] = item.pop("data_mode", None)
        item["run_at"] = item.get("fetched_at") or item.get("updated_at")
        item["duration_seconds"] = None
        haystack = " ".join(
            str(item.get(key) or "")
            for key in ("fetch_id", "run_type", "status", "error_code", "mode", "source_manifest")
        ).casefold()
        if (
            q.casefold() not in haystack
            or (status and item.get("status") != status)
            or (mode and item.get("mode") != mode)
        ):
            continue
        items.append(item)
    history_status = "AVAILABLE"
    history_truncated = False
    storage = _history_storage(request)
    if storage is not None and hasattr(storage, "list_runs"):
        try:
            manual_runs = await storage.list_runs(limit=100, offset=0)
        except StorageError:
            manual_runs = []
            history_status = "PARTIAL"
        history_truncated = len(manual_runs) == 100
        existing = {str(item.get("fetch_id")) for item in items}
        for run in manual_runs:
            run_id = str(run.get("run_id") or "")
            if not run_id or run_id in existing:
                continue
            manifest = run.get("source_manifest") or run.get("sources") or []
            if not isinstance(manifest, list):
                manifest = []
            sources = [entry.get("source_id") if isinstance(entry, dict) else str(entry) for entry in manifest]
            mode_value = run.get("mode") or run.get("data_mode")
            run_at = run.get("finished_at") or run.get("updated_at") or run.get("created_at")
            item = {
                "fetch_id": run_id,
                "run_type": "MANUAL_ANALYSIS",
                "status": run.get("status") or "UNKNOWN",
                "mode": mode_value,
                "data_mode": mode_value,
                "sources": sources,
                "source_manifest": json.dumps(manifest),
                "quality_flags": run.get("quality_flags") or [],
                "error_code": run.get("error_code") or run.get("error"),
                "stage": run.get("status"),
                "available": run.get("keyword_count"),
                "processed": run.get("scored_count"),
                "received_record_count": run.get("keyword_count"),
                "retained_keyword_count": run.get("scored_count"),
                "rejected_record_count": None,
                "run_at": _iso(run_at),
                "created_at": _iso(run.get("created_at")),
                "updated_at": _iso(run.get("updated_at") or run.get("finished_at")),
                "fetched_at": _iso(run.get("finished_at")),
                "duration_seconds": None,
            }
            haystack = " ".join(str(item.get(key) or "") for key in ("fetch_id", "run_type", "status", "error_code", "mode", "source_manifest")).casefold()
            if q.casefold() not in haystack or (status and item["status"] != status) or (mode and item["mode"] != mode):
                continue
            items.append(item)
            existing.add(run_id)
    else:
        history_status = "LOCAL_LEDGER_ONLY"
    items.sort(key=lambda item: str(item.get("run_at") or item.get("updated_at") or ""), reverse=True)
    return {
        "items": items[offset : offset + limit],
        "total": len(items),
        "offset": offset,
        "limit": limit,
        "next_offset": offset + limit if offset + limit < len(items) else None,
        "duration_available": False,
        "history_source": "local_sqlite_and_analysis_storage" if history_status == "AVAILABLE" else "local_sqlite_fetch_jobs",
        "manual_history_status": history_status,
        "history_truncated": history_truncated,
    }


@router.get("/runs/history/{fetch_id}")
async def run_history_detail(fetch_id: str, request: Request):
    if not fetch_id or len(fetch_id) > 64:
        raise HTTPException(404, "Run not found")
    service = getattr(request.app.state, "collection_service", None)
    if service:
        with service.store.connect() as db:
            row = db.execute(
                """SELECT j.*,d.dataset_id,d.status AS dataset_status,d.data_mode,d.source_manifest,
                d.provider_record_count,d.received_record_count,d.retained_keyword_count,d.rejected_record_count,
                d.observation_start,d.observation_end,d.fetched_at,d.quality_flags,d.processing_status
                FROM fetch_jobs j LEFT JOIN dataset_snapshots d ON d.dataset_id=j.fetch_id WHERE j.fetch_id=?""",
                (fetch_id,),
            ).fetchone()
    else:
        row = None
    if row is None:
        storage = _history_storage(request)
        if storage is None or len(fetch_id) != 32 or any(character not in "0123456789abcdef" for character in fetch_id):
            raise HTTPException(404, "Run not found")
        try:
            run = await storage.get_run(fetch_id)
        except StorageError:
            raise HTTPException(503, "Run history is temporarily unavailable") from None
        if run is None:
            raise HTTPException(404, "Run not found")
        manifest = run.get("source_manifest") or run.get("sources") or []
        return {
            "item": {
                "fetch_id": fetch_id,
                "status": run.get("status"),
                "data_mode": run.get("mode") or run.get("data_mode"),
                "source_manifest": manifest,
                "quality_flags": run.get("quality_flags") or [],
                "error_code": run.get("error_code") or run.get("error"),
                "available": run.get("keyword_count"),
                "retained_keyword_count": run.get("scored_count"),
                "duration_seconds": None,
                "created_at": _iso(run.get("created_at")),
                "updated_at": _iso(run.get("updated_at") or run.get("finished_at")),
                "fetched_at": _iso(run.get("finished_at")),
            },
            "duration_available": False,
        }
    item = dict(row)
    for key in ("source_manifest", "quality_flags"):
        try:
            item[key] = json.loads(item[key]) if item.get(key) else []
        except (TypeError, ValueError):
            item[key] = []
    item["duration_seconds"] = None
    return {"item": item, "duration_available": False}
