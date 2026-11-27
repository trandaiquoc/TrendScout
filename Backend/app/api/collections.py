"""Loopback-only Collections API; no client-supplied source or model access."""

import json
import uuid
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.ai.runtime import AIExecutionError
from app.api.fetches import validate_public_request_origin
from app.collections.store import saved_source_record

router = APIRouter()


class CollectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1200)
    examples: list[str] = Field(default_factory=list, max_length=30)
    exclusions: list[str] = Field(default_factory=list, max_length=30)
    auto_match: bool = True
    parent_id: str | None = Field(default=None, max_length=64)

    @field_validator("examples", "exclusions")
    @classmethod
    def bounded_examples(cls, values):
        if any(not isinstance(value, str) or not value.strip() or len(value) > 200 for value in values):
            raise ValueError("examples must be non-empty strings of at most 200 characters")
        return values


class CollectionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=1200)
    examples: list[str] | None = Field(default=None, max_length=30)
    exclusions: list[str] | None = Field(default=None, max_length=30)
    auto_match: bool | None = None

    @field_validator("examples", "exclusions")
    @classmethod
    def bounded_examples(cls, values):
        if values is not None and any(not isinstance(value, str) or not value.strip() or len(value) > 200 for value in values):
            raise ValueError("examples must be non-empty strings of at most 200 characters")
        return values


class CollectionMergeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str = Field(min_length=1, max_length=64)


class MembershipInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword_id: str = Field(min_length=1, max_length=64)


class CachedReclassificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("keyword_ids")
    @classmethod
    def valid_ids(cls, values):
        if any(not value.strip() or len(value) > 64 for value in values):
            raise ValueError("keyword IDs must be non-empty and at most 64 characters")
        return list(dict.fromkeys(values))


class ModeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["SUGGEST_ONLY", "AUTO_CREATE", "OFF"]


class ProposalAcceptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=1200)

    @field_validator("name")
    @classmethod
    def non_empty_name(cls, value):
        if value is not None and not value.strip():
            raise ValueError("proposal name must not be empty")
        return value.strip() if value is not None else None

    @field_validator("description")
    @classmethod
    def trim_description(cls, value):
        return value.strip() if value is not None else None


class BulkMembershipInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["ADD", "COPY", "MOVE", "REMOVE", "APPROVE", "REJECT", "EXCLUDE", "RESTORE"]
    keyword_ids: list[str] = Field(default_factory=list, max_length=500)
    all_filtered: bool = False
    query: str = Field(default="", max_length=100)
    state: Literal["all", "unassigned", "ai", "manual", "mixed", "needs_review", "excluded", "non_topic"] = "all"
    classification: Literal["all", "CLASSIFIED", "AMBIGUOUS", "UNKNOWN", "INELIGIBLE"] = "all"
    evidence: Literal["all", "metric", "algorithm", "missing"] = "all"
    date_from: str = Field(default="", max_length=10)
    date_to: str = Field(default="", max_length=10)
    collection_ids: list[str] = Field(default_factory=list, max_length=20)
    source: str = Field(default="", max_length=100)
    collection_id: str | None = Field(default=None, max_length=64)
    source_collection_id: str | None = Field(default=None, max_length=64)

    @field_validator("keyword_ids")
    @classmethod
    def valid_ids(cls, values):
        if any(not value.strip() or len(value) > 64 for value in values):
            raise ValueError("keyword IDs must be non-empty and at most 64 characters")
        return list(dict.fromkeys(values))


def _service(request):
    validate_public_request_origin(request, request.app.state.settings.environment_profile)
    service = (getattr(getattr(request, "state", None), "collection_service", None)
               or getattr(request.app.state, "collection_service", None))
    if service is None:
        raise HTTPException(503, {"code": "COLLECTIONS_UNAVAILABLE"})
    return service


def _mutating(request):
    validate_public_request_origin(request, request.app.state.settings.environment_profile)


@router.get("/collections")
def list_collections(request: Request):
    if (getattr(request.state, "collection_workbench_available", False)
            and isinstance(getattr(request.state, "collection_workbench", None), dict)):
        validate_public_request_origin(request, request.app.state.settings.environment_profile)
        return request.state.collection_workbench
    service = _service(request)
    return collection_workbench_for_service(service)


def collection_workbench_for_service(service, *, keyword_page=None):
    """Build the current Collections landing-page response from one store."""
    # This first page is bounded and uses the same snapshot as the workbench
    # summary. Returning it here avoids a second serverless invocation and a
    # second SQLite restore when Collections opens.
    from app.api.datasets import latest_keywords_for_service

    page = keyword_page or latest_keywords_for_service(
        service, limit=25, offset=0, include_algorithm_results=False,
    )
    page = {**page, "items": list(page.get("items", []))}
    page["items"] = [_compact_workbench_keyword(item) for item in page.get("items", [])]
    return {
        "items": service.store.list_collections(),
        "suggestions": service.store.suggestions(),
        "mode": _mode(service),
        "summary": service.store.workbench_summary(),
        "keyword_page": page,
    }


def _compact_workbench_keyword(item):
    """Keep only list-row fields; the inspector fetches complete evidence on demand."""
    evidence = item.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    details = evidence.get("evidence")
    measured = next((entry for entry in details if isinstance(entry, dict)
                     and (entry.get("views_ceil") is not None
                          or entry.get("approximate_views") is not None)), None) \
        if isinstance(details, list) else None
    measured = measured or evidence
    compact_evidence = {
        key: measured[key] for key in ("views_ceil", "approximate_views")
        if measured.get(key) is not None
    }
    memberships = item.get("memberships")
    return {
        "id": item.get("id"),
        "keyword": item.get("keyword"),
        "source": item.get("source"),
        "observed_at": item.get("observed_at"),
        "eligible": item.get("eligible"),
        "classification_status": item.get("classification_status"),
        "classification_reason": item.get("classification_reason"),
        "memberships": [
            {key: membership[key] for key in ("id", "name", "origin", "category_id")
             if key in membership}
            for membership in memberships if isinstance(membership, dict)
        ] if isinstance(memberships, list) else [],
        "evidence": compact_evidence,
    }


def _mode(service):
    value = service.store.get_setting("creation_mode")
    return "SUGGEST_ONLY" if value == "SUGGEST" else value or "AUTO_CREATE"


@router.post("/collections", status_code=201)
async def create_collection(value: CollectionInput, request: Request):
    _mutating(request)
    name = value.name.strip()
    if not name:
        raise HTTPException(422, {"code": "COLLECTION_NAME_REQUIRED"})
    service = _service(request)
    if value.parent_id:
        if not service.store.collection_exists(value.parent_id):
            raise HTTPException(404, {"code": "COLLECTION_PARENT_NOT_FOUND"})
    try:
        identifier = service.store.create({**value.model_dump(), "name": name})
    except ValueError as exc:
        raise HTTPException(409, {"code": str(exc)}) from None
    result = await service.reclassify_unassigned()
    return {"id": identifier, "reclassification": result}


@router.patch("/collections/{collection_id}")
async def update_collection(collection_id: str, value: CollectionPatch, request: Request):
    _mutating(request)
    changes = {key: item for key, item in value.model_dump(exclude_unset=True).items() if item is not None}
    if not changes:
        raise HTTPException(422, {"code": "EMPTY_UPDATE"})
    if "name" in changes:
        changes["name"] = changes["name"].strip()
        if not changes["name"]:
            raise HTTPException(422, {"code": "COLLECTION_NAME_REQUIRED"})
    service = _service(request)
    if not service.store.update(collection_id, changes):
        raise HTTPException(404, {"code": "COLLECTION_NOT_FOUND"})
    result = await service.reclassify_unassigned()
    return {"updated": True, "reclassification": result}


@router.delete("/collections/{collection_id}")
def delete_collection(collection_id: str, request: Request):
    _mutating(request)
    if not _service(request).store.delete(collection_id):
        raise HTTPException(404, {"code": "COLLECTION_NOT_FOUND"})
    return {"deleted": True}


@router.post("/collections/{collection_id}/merge")
def merge_collection(collection_id: str, value: CollectionMergeInput, request: Request):
    _mutating(request)
    try:
        return _service(request).store.merge(collection_id, value.target_id)
    except ValueError as exc:
        code = str(exc)
        status = 404 if code == "COLLECTION_NOT_FOUND" else 422
        raise HTTPException(status, {"code": code}) from None


@router.get("/collections/{collection_id}/keywords")
def list_keywords(collection_id: str, request: Request):
    service = _service(request)
    if not service.store.collection_exists(collection_id):
        raise HTTPException(404, {"code": "COLLECTION_NOT_FOUND"})
    return {"items": service.store.keywords_for(collection_id)}


@router.get("/collections/inbox")
def unassigned_keywords(request: Request, limit: int = Query(default=40, ge=1, le=100),
                       offset: int = Query(default=0, ge=0, le=10000)):
    service = _service(request)
    result = service.store.search_keywords(state="unassigned", sort="recent", limit=limit, offset=offset)
    return {"items": result["items"], "total": result["total"], "limit": limit, "offset": offset}


@router.post("/collections/reclassify-cached")
def reclassify_cached_keywords(value: CachedReclassificationInput, request: Request):
    _mutating(request)
    try:
        return _service(request).reclassify_cached_keywords(value.keyword_ids)
    except ValueError as exc:
        code = str(exc)
        status = 404 if code == "CACHED_KEYWORD_NOT_FOUND" else 422
        raise HTTPException(status, {"code": code}) from None


@router.get("/collections/classifications/{keyword_id}")
def keyword_classification(keyword_id: str, request: Request):
    service = _service(request)
    row = service.store.classification_for(keyword_id)
    if row is None:
        raise HTTPException(404, {"code": "CLASSIFICATION_NOT_FOUND"})
    item = dict(row)
    for key in ("topic_category_ids", "subcategory_ids", "rule_evidence", "source_evidence"):
        if isinstance(item[key], str):
            item[key] = json.loads(item[key] or ("{}" if key == "source_evidence" else "[]"))
    from app.taxonomy.registry import resolve
    item["topic_categories"] = [{"id": value, "name": resolve(value).display_name,
        "scope": resolve(value).scope} for value in item["topic_category_ids"]]
    return item


@router.post("/collections/reconciliation/{run_id}/rollback")
def rollback_collection_reconciliation(run_id: str, request: Request):
    _mutating(request)
    if not _service(request).store.rollback_membership_reconciliation(run_id):
        raise HTTPException(404, {"code": "RECONCILIATION_NOT_FOUND_OR_ROLLED_BACK"})
    return {"rolled_back": True, "run_id": run_id}


@router.post("/collections/organize")
async def organize(request: Request):
    _mutating(request)
    service = _service(request)
    rows = service.store.load_keywords_for_collection_processing()
    grouped = {}
    for row in rows:
        item = dict(row)
        grouped.setdefault(item["source"], {"source_id": item["source"], "data_mode": item["data_mode"], "records": []})
        grouped[item["source"]]["records"].append(saved_source_record(item))
    return await service.process_fetch("organize-" + str(uuid.uuid4()), list(grouped.values()))


@router.post("/collections/reclassify")
async def reclassify_from_cached_evidence(request: Request):
    """Re-run local classification without making provider requests."""
    _mutating(request)
    service = _service(request)
    rows = service.store.load_keywords_for_collection_processing(include_invalid_google=True)
    grouped = {}
    for row in rows:
        item = dict(row)
        grouped.setdefault(item["source"], {"source_id": item["source"], "data_mode": item["data_mode"], "records": []})
        grouped[item["source"]]["records"].append(saved_source_record(item))
    # Reuse the accepted local pipeline and cached entity context; do not enrich from Wikimedia.
    return await service.process_fetch("organize-cache-" + str(uuid.uuid4()), list(grouped.values()),
                                       cached_context_only=True)


@router.post("/collections/{collection_id}/keywords")
def add_keyword(collection_id: str, value: MembershipInput, request: Request):
    _mutating(request)
    service = _service(request)
    keyword = service.store.collection_keyword_record(value.keyword_id)
    collection = service.store.collection_exists(collection_id)
    if not keyword or not collection:
        raise HTTPException(404, {"code": "KEYWORD_OR_COLLECTION_NOT_FOUND"})
    service.store.feedback_action(collection_id, dict(keyword), "ADD")
    return {"added": True}


@router.post("/collections/bulk")
def bulk_membership(value: BulkMembershipInput, request: Request):
    _mutating(request)
    service = _service(request)
    for value_date in (value.date_from, value.date_to):
        if value_date:
            try:
                date.fromisoformat(value_date)
            except ValueError:
                raise HTTPException(422, {"code": "INVALID_OBSERVATION_DATE"}) from None
    if value.date_from and value.date_to and value.date_from > value.date_to:
        raise HTTPException(422, {"code": "INVALID_OBSERVATION_DATE_RANGE"})
    if value.all_filtered:
        bulk_filtered = getattr(service.store, "bulk_membership_filtered", None)
        if callable(bulk_filtered):
            if value.action in {"ADD", "COPY", "MOVE", "APPROVE", "REMOVE", "REJECT"} and not value.collection_id:
                raise HTTPException(422, {"code": "COLLECTION_REQUIRED"})
            if value.action == "MOVE" and not value.source_collection_id:
                raise HTTPException(422, {"code": "SOURCE_COLLECTION_REQUIRED"})
            if value.action == "MOVE" and value.collection_id == value.source_collection_id:
                raise HTTPException(422, {"code": "COLLECTION_MOVE_SAME_TARGET"})
            try:
                return bulk_filtered(query=value.query, state=value.state,
                    collection_ids=value.collection_ids, source=value.source,
                    classification=value.classification, evidence=value.evidence,
                    date_from=value.date_from, date_to=value.date_to,
                    action=value.action, collection_id=value.collection_id,
                    source_collection_id=value.source_collection_id)
            except ValueError as error:
                code = str(error)
                if code in {"COLLECTION_NOT_FOUND", "INVALID_BULK_ACTION", "KEYWORDS_REQUIRED"}:
                    status = 404 if code == "COLLECTION_NOT_FOUND" else 422
                    raise HTTPException(status, {"code": code}) from None
                raise HTTPException(422, {"code": "INVALID_KEYWORD_QUERY"}) from None
    keyword_ids = value.keyword_ids
    if value.all_filtered:
        result = service.store.search_keywords(query=value.query, state=value.state,
            collection_ids=value.collection_ids, source=value.source, sort="recent", limit=None,
            offset=0, ids_only=True, classification=value.classification, evidence=value.evidence,
            date_from=value.date_from, date_to=value.date_to)
        keyword_ids = result["ids"]
    if not keyword_ids:
        raise HTTPException(422, {"code": "KEYWORDS_REQUIRED"})
    if value.action in {"ADD", "COPY", "MOVE", "APPROVE", "REMOVE", "REJECT"} and not value.collection_id:
        raise HTTPException(422, {"code": "COLLECTION_REQUIRED"})
    if value.action == "MOVE" and not value.source_collection_id:
        raise HTTPException(422, {"code": "SOURCE_COLLECTION_REQUIRED"})
    if value.action == "MOVE" and value.collection_id == value.source_collection_id:
        raise HTTPException(422, {"code": "COLLECTION_MOVE_SAME_TARGET"})
    try:
        return service.store.bulk_membership_action(keyword_ids, value.action,
            collection_id=value.collection_id, source_collection_id=value.source_collection_id)
    except ValueError as error:
        code = str(error)
        if code in {"COLLECTION_NOT_FOUND", "INVALID_BULK_ACTION"}:
            raise HTTPException(404 if code == "COLLECTION_NOT_FOUND" else 422, {"code": code}) from None
        raise


@router.get("/collections/keywords/{keyword_id}")
def keyword_workbench_detail(keyword_id: str, request: Request):
    service = _service(request)
    result = service.store.search_keywords(keyword_id=keyword_id, limit=1, offset=0)
    if not result["items"]:
        raise HTTPException(404, {"code": "KEYWORD_NOT_FOUND"})
    item = result["items"][0]
    metadata = service.store.keyword_detail_metadata(keyword_id)
    if metadata is None:
        raise HTTPException(404, {"code": "KEYWORD_NOT_FOUND"})
    decision, feedback = metadata["decision"], metadata["feedback"]
    item["user_decision"] = dict(decision) if decision else None
    item["user_feedback"] = [dict(row) for row in feedback]
    for algorithm in item["algorithm_results"]:
        try:
            algorithm["result"] = json.loads(algorithm.pop("result_json") or "{}")
        except (TypeError, ValueError):
            algorithm["result"] = {}
    return item


@router.delete("/collections/{collection_id}/keywords/{keyword_id}")
def remove_keyword(collection_id: str, keyword_id: str, request: Request):
    _mutating(request)
    service = _service(request)
    if not service.store.has_membership(collection_id, keyword_id):
        raise HTTPException(404, {"code": "MEMBERSHIP_NOT_FOUND"})
    service.store.feedback_action(collection_id, {"id": keyword_id, "keyword": "", "normalized": "",
        "source": "", "project": "", "observed_at": "", "data_mode": "LIVE", "evidence": "{}", "context": ""}, "REMOVE")
    return {"removed": True}


@router.post("/collections/mode")
def set_mode(value: ModeInput, request: Request):
    _mutating(request)
    service = _service(request)
    service.store.set_setting("creation_mode", value.mode)
    return {"mode": value.mode}


@router.post("/collections/suggestions/{proposal_id}/accept")
def accept_suggestion(proposal_id: str, request: Request, value: ProposalAcceptInput | None = None):
    _mutating(request)
    service = _service(request)
    ai_service = (getattr(getattr(request, "state", None), "ai_service", None)
                  or getattr(request.app.state, "ai_service", None))
    overrides = value.model_dump(exclude_unset=True) if value is not None else {}
    proposal = service.store.get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(404, {"code": "SUGGESTION_NOT_FOUND"})
    try:
        identifier = service.store.resolve_proposal(
            proposal_id, True, name=overrides.get("name"), description=overrides.get("description"),
            allowed_sources=getattr(ai_service, "_allowed_sources", None),
        )
    except ValueError as exc:
        code = str(exc)
        status = 409 if code in {"AI_PROPOSAL_EXPIRED", "AI_INPUT_CHANGED",
                                 "COLLECTION_NAME_CONFLICT", "COLLECTION_CONTENT_CONFLICT"} else 422
        if code == "AI_POLICY_DENIED":
            status = 403
        raise HTTPException(status, {"code": code}) from None
    if identifier is None:
        raise HTTPException(404, {"code": "SUGGESTION_NOT_FOUND"})
    support_ids = proposal.get("support_ids", [])
    if isinstance(support_ids, str):
        try:
            support_ids = json.loads(support_ids)
        except (TypeError, ValueError):
            support_ids = []
    related_ids = support_ids if isinstance(support_ids, list) else []
    return {"collection_id": identifier, "reclassification_keyword_ids": related_ids,
            "reclassification_candidate_count": len(related_ids)}


@router.get("/collections/suggestions/{proposal_id}")
def get_suggestion(proposal_id: str, request: Request):
    service = _service(request)
    proposal = service.store.get_proposal(proposal_id)
    if proposal is None or proposal.get("status") not in {None, "PENDING"}:
        raise HTTPException(404, {"code": "SUGGESTION_NOT_FOUND"})
    support_ids = proposal.get("support_ids", [])
    if isinstance(support_ids, str):
        try:
            support_ids = json.loads(support_ids)
        except (TypeError, ValueError):
            support_ids = []
    if not isinstance(support_ids, list):
        support_ids = []
    rows = service.store.keyword_records_by_ids(support_ids)
    keyword_by_id = {dict(row)["id"]: dict(row) for row in rows}
    keywords = []
    for identifier in support_ids:
        row = keyword_by_id.get(identifier)
        keywords.append({
            "id": identifier,
            "keyword": row.get("keyword") if row else None,
            "normalized": row.get("normalized") if row else None,
            "source_id": row.get("source") if row else None,
            "project": row.get("project") if row else None,
            "observed_at": row.get("observed_at") if row else None,
            "available": row is not None,
        })
    expires_at = proposal.get("expires_at")
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            expires_at = None
    if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    expired = (expires_at is not None and expires_at <= datetime.now(timezone.utc))
    return {**proposal, "support_ids": support_ids, "keywords": keywords, "expired": expired}


@router.post("/collections/suggestions/{proposal_id}/reject")
def reject_suggestion(proposal_id: str, request: Request):
    _mutating(request)
    if _service(request).store.resolve_proposal(proposal_id, False) is None:
        raise HTTPException(404, {"code": "SUGGESTION_NOT_FOUND"})
    return {"rejected": True}


@router.get("/collections/jobs/{fetch_id}")
def get_job(fetch_id: str, request: Request):
    service = _service(request)
    row, created = service.store.collection_job(fetch_id)
    if row is None:
        return {"status": "NOT_PROCESSED", "processed": 0, "available": 0}
    result = dict(row)
    ai_jobs = result.get("ai_jobs", [])
    if isinstance(ai_jobs, str):
        try:
            ai_jobs = json.loads(ai_jobs)
        except (TypeError, ValueError):
            ai_jobs = []
    if not isinstance(ai_jobs, list):
        ai_jobs = []
    engine = getattr(service, "ai_engine", None)
    statuses = []
    for item in ai_jobs:
        if not isinstance(item, dict) or not isinstance(item.get("job_id"), str):
            continue
        state = None
        if engine is not None:
            try:
                state = engine.get_job(item["job_id"])
            except AIExecutionError:
                state = None
        statuses.append({
            "job_id": item["job_id"],
            "status": state.get("status") if isinstance(state, dict) else "PENDING",
            "processed": state.get("processed", 0) if isinstance(state, dict) else 0,
            "retry_at": state.get("retry_at") if isinstance(state, dict) else None,
            "error_code": state.get("error_code") if isinstance(state, dict) else None,
        })
    result["ai_jobs"] = statuses
    result["ai_job_ids"] = [item["job_id"] for item in statuses]
    result["created_collection_names"] = [item["name"] for item in created]
    return result


@router.post("/collections/jobs/{fetch_id}/resume")
def resume_collection_ai_job(fetch_id: str, request: Request):
    """Resume only persisted AI batches; source Fetch and algorithms are not rerun."""
    _mutating(request)
    service = _service(request)
    engine = getattr(service, "ai_engine", None)
    if engine is None:
        raise HTTPException(503, {"code": "AI_STORAGE_UNAVAILABLE"})
    row, _created = service.store.collection_job(fetch_id)
    if row is None:
        raise HTTPException(404, {"code": "FETCH_COLLECTION_JOB_NOT_FOUND"})
    entry = dict(row)
    ai_jobs = entry.get("ai_jobs", [])
    if isinstance(ai_jobs, str):
        try:
            ai_jobs = json.loads(ai_jobs)
        except (TypeError, ValueError):
            ai_jobs = []
    if not isinstance(ai_jobs, list) or not ai_jobs:
        raise HTTPException(409, {"code": "AI_WORK_NOT_RESUMABLE"})

    states = []
    for item in ai_jobs:
        if (not isinstance(item, dict) or not isinstance(item.get("job_id"), str)
                or not isinstance(item.get("keyword_ids"), list)
                or not item["keyword_ids"]):
            raise HTTPException(409, {"code": "AI_WORK_NOT_RESUMABLE"})
        job_id = item["job_id"]
        try:
            state = engine.get_job(job_id)
            if isinstance(state, dict) and state.get("status") == "COMPLETED":
                result = {"job_id": job_id, "status": "COMPLETED"}
            elif isinstance(state, dict) and state.get("status") == "RUNNING":
                states.append({"job_id": job_id, "status": "RUNNING"})
                continue
            elif isinstance(state, dict):
                result = engine.resume_job(service.store, job_id)
            else:
                result = engine.classify_keyword_ids(
                    service.store, item["keyword_ids"], job_id=job_id, automatic=True,
                )
        except AIExecutionError as exc:
            if exc.code == "AI_JOB_NOT_RESUMABLE":
                raise HTTPException(409, {"code": exc.code}) from None
            raise HTTPException(503, {"code": exc.code}) from None
        except Exception:
            raise HTTPException(503, {"code": "AI_STORAGE_UNAVAILABLE"}) from None
        states.append({"job_id": job_id, "status": result.get("status", "FAILED"),
                       "error_code": result.get("error_code"),
                       "retry_at": result.get("retry_at")})
        if result.get("status") != "COMPLETED":
            service.store.update_fetch_job(fetch_id, {
                "status": "MODEL_UNAVAILABLE", "stage": "MODEL_UNAVAILABLE",
                "error_code": result.get("error_code") or "AI_MODEL_UNAVAILABLE",
            })
            return {"fetch_id": fetch_id, "status": "MODEL_UNAVAILABLE", "ai_jobs": states}

    if any(item["status"] == "RUNNING" for item in states):
        return {"fetch_id": fetch_id, "status": "PROCESSING", "ai_jobs": states}
    assigned = service.store.count_ai_assigned_keywords()
    unassigned = service._unassigned_count()
    service.store.complete_fetch_dataset_snapshot(
        fetch_id,
        algorithm_processing_status=entry.get("algorithm_processing_status") or "UNKNOWN",
        ai_classification_status="COMPLETED",
    )
    service.store.update_fetch_job(fetch_id, {
        "status": "COMPLETED", "stage": "COMPLETED", "error_code": None,
        "assigned": assigned, "unassigned": unassigned,
    })
    return {"fetch_id": fetch_id, "status": "COMPLETED", "ai_jobs": states,
            "assigned": assigned, "unassigned": unassigned}
