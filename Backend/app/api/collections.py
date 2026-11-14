"""Loopback-only Collections API; no client-supplied source or model access."""

import json
import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.fetches import validate_public_request_origin

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


class BulkMembershipInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["ADD", "COPY", "MOVE", "REMOVE", "APPROVE", "REJECT", "EXCLUDE", "RESTORE"]
    keyword_ids: list[str] = Field(default_factory=list, max_length=500)
    all_filtered: bool = False
    query: str = Field(default="", max_length=100)
    state: Literal["all", "unassigned", "ai", "manual", "mixed", "needs_review", "excluded"] = "all"
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
    service = getattr(request.app.state, "collection_service", None)
    if service is None:
        raise HTTPException(503, {"code": "COLLECTIONS_UNAVAILABLE"})
    return service


def _mutating(request):
    validate_public_request_origin(request, request.app.state.settings.environment_profile)


@router.get("/collections")
def list_collections(request: Request):
    service = _service(request)
    return {"items": service.store.list_collections(), "suggestions": service.store.suggestions(),
            "mode": _mode(service), "summary": service.store.workbench_summary()}


def _mode(service):
    with service.store.connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key='creation_mode'").fetchone()
        if row:
            # Preserve an explicit legacy choice while making the old label unambiguous.
            if row["value"] == "SUGGEST":
                db.execute("UPDATE settings SET value='SUGGEST_ONLY' WHERE key='creation_mode'")
                return "SUGGEST_ONLY"
            return row["value"]
        db.execute("INSERT INTO settings(key,value) VALUES('creation_mode','AUTO_CREATE')")
        return "AUTO_CREATE"


@router.post("/collections", status_code=201)
async def create_collection(value: CollectionInput, request: Request):
    _mutating(request)
    name = value.name.strip()
    if not name:
        raise HTTPException(422, {"code": "COLLECTION_NAME_REQUIRED"})
    service = _service(request)
    if value.parent_id:
        with service.store.connect() as db:
            parent = db.execute("SELECT 1 FROM collections WHERE id=? AND status='ACTIVE'", (value.parent_id,)).fetchone()
        if not parent:
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
    with service.store.connect() as db:
        exists = db.execute("SELECT 1 FROM collections WHERE id=? AND status='ACTIVE'", (collection_id,)).fetchone()
        if not exists:
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
    with service.store.connect() as db:
        row = db.execute("SELECT * FROM classification_results WHERE keyword_id=?", (keyword_id,)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "CLASSIFICATION_NOT_FOUND"})
    item = dict(row)
    for key in ("topic_category_ids", "subcategory_ids", "rule_evidence", "source_evidence"):
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
    with service.store.connect() as db:
        rows = db.execute("SELECT * FROM keywords ORDER BY observed_at DESC").fetchall()
    grouped = {}
    for row in rows:
        item = dict(row)
        try:
            evidence = json.loads(item["evidence"])
        except (TypeError, ValueError):
            evidence = {}
        grouped.setdefault(item["source"], {"source_id": item["source"], "data_mode": item["data_mode"], "records": []})
        grouped[item["source"]]["records"].append({"article_display": item["keyword"],
            "article_original": item["keyword"], "project": item["project"], "observation_date": item["observed_at"],
            "fetched_at": evidence.get("fetched_at"), "provider_rank": evidence.get("provider_rank"),
            "views_ceil": evidence.get("views_ceil"), "country_scope": evidence.get("country_scope"),
            "access": evidence.get("access")})
    return await service.process_fetch("organize-" + str(uuid.uuid4()), list(grouped.values()))


@router.post("/collections/reclassify")
async def reclassify_from_cached_evidence(request: Request):
    """Re-run local classification without making provider requests."""
    _mutating(request)
    service = _service(request)
    with service.store.connect() as db:
        rows = db.execute("SELECT * FROM keywords WHERE eligible=1 ORDER BY observed_at DESC").fetchall()
    grouped = {}
    for row in rows:
        item = dict(row)
        try:
            evidence = json.loads(item["evidence"])
        except (TypeError, ValueError):
            evidence = {}
        grouped.setdefault(item["source"], {"source_id": item["source"], "data_mode": item["data_mode"], "records": []})
        grouped[item["source"]]["records"].append({
            "article_display": item["keyword"], "article_original": item["keyword"],
            "project": item["project"], "observation_date": item["observed_at"],
            "fetched_at": evidence.get("fetched_at"), "provider_rank": evidence.get("provider_rank"),
            "views_ceil": evidence.get("views_ceil"), "country_scope": evidence.get("country_scope"),
            "access": evidence.get("access"),
        })
    # Reuse the accepted local pipeline and cached entity context; do not enrich from Wikimedia.
    return await service.process_fetch("organize-cache-" + str(uuid.uuid4()), list(grouped.values()),
                                       cached_context_only=True)


@router.post("/collections/{collection_id}/keywords")
def add_keyword(collection_id: str, value: MembershipInput, request: Request):
    _mutating(request)
    service = _service(request)
    with service.store.connect() as db:
        keyword = db.execute("SELECT * FROM keywords WHERE id=?", (value.keyword_id,)).fetchone()
        collection = db.execute("SELECT 1 FROM collections WHERE id=? AND status='ACTIVE'", (collection_id,)).fetchone()
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
    with service.store.connect() as db:
        decision = db.execute("SELECT decision,reason,updated_at FROM keyword_decisions WHERE keyword_id=?", (keyword_id,)).fetchone()
        feedback = db.execute("""SELECT f.collection_id,c.name,f.action,f.created_at
            FROM feedback f LEFT JOIN collections c ON c.id=f.collection_id
            WHERE f.keyword_id=? ORDER BY f.created_at DESC""", (keyword_id,)).fetchall()
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
    with service.store.connect() as db:
        exists = db.execute("SELECT 1 FROM memberships WHERE collection_id=? AND keyword_id=?",
                            (collection_id, keyword_id)).fetchone()
    if not exists:
        raise HTTPException(404, {"code": "MEMBERSHIP_NOT_FOUND"})
    service.store.feedback_action(collection_id, {"id": keyword_id, "keyword": "", "normalized": "",
        "source": "", "project": "", "observed_at": "", "data_mode": "LIVE", "evidence": "{}", "context": ""}, "REMOVE")
    return {"removed": True}


@router.post("/collections/mode")
def set_mode(value: ModeInput, request: Request):
    _mutating(request)
    service = _service(request)
    with service.store.connect() as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES('creation_mode',?)", (value.mode,))
    return {"mode": value.mode}


@router.post("/collections/suggestions/{proposal_id}/accept")
def accept_suggestion(proposal_id: str, request: Request):
    _mutating(request)
    identifier = _service(request).store.resolve_proposal(proposal_id, True)
    if identifier is None:
        raise HTTPException(404, {"code": "SUGGESTION_NOT_FOUND"})
    return {"collection_id": identifier}


@router.post("/collections/suggestions/{proposal_id}/reject")
def reject_suggestion(proposal_id: str, request: Request):
    _mutating(request)
    if _service(request).store.resolve_proposal(proposal_id, False) is None:
        raise HTTPException(404, {"code": "SUGGESTION_NOT_FOUND"})
    return {"rejected": True}


@router.get("/collections/jobs/{fetch_id}")
def get_job(fetch_id: str, request: Request):
    service = _service(request)
    with service.store.connect() as db:
        row = db.execute("SELECT * FROM fetch_jobs WHERE fetch_id=?", (fetch_id,)).fetchone()
        created = db.execute("SELECT name FROM collections WHERE created_by='AI' AND reason LIKE ? ORDER BY created_at",
                             (f"%{fetch_id}%",)).fetchall()
    if row is None:
        return {"status": "NOT_PROCESSED", "processed": 0, "available": 0}
    return {**dict(row), "created_collection_names": [item["name"] for item in created]}
