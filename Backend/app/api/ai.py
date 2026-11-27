"""Public AI workflow endpoints; requests contain record IDs, never provider text."""

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.ai.runtime import AIExecutionError
from app.api.fetches import validate_public_request_origin

router = APIRouter()


class ClassificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword_ids: list[str] = Field(default_factory=list, max_length=200)
    collection_ids: list[str] = Field(default_factory=list, max_length=100)
    include_unassigned: bool = False
    preview: bool = False

    @field_validator("keyword_ids", "collection_ids")
    @classmethod
    def bounded_ids(cls, values):
        if any(not value.strip() or len(value) > 64 for value in values):
            raise ValueError("selector IDs must be non-empty and at most 64 characters")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def has_scope(self):
        if not (self.keyword_ids or self.collection_ids or self.include_unassigned):
            raise ValueError("at least one reclassification scope selector is required")
        return self


class RecommendationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword_ids: list[str] = Field(min_length=1, max_length=20)
    research_constraints: str = Field(default="", max_length=500)
    regenerate: bool = False

    @field_validator("keyword_ids")
    @classmethod
    def bounded_ids(cls, values):
        if any(not value.strip() or len(value) > 64 for value in values):
            raise ValueError("keyword IDs must be non-empty and at most 64 characters")
        return list(dict.fromkeys(values))


class ProposalPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=1200)
    keyword_ids: list[str] | None = Field(default=None, min_length=2, max_length=200)

    @field_validator("keyword_ids")
    @classmethod
    def bounded_ids(cls, values):
        if values is not None and (any(not value.strip() or len(value) > 64 for value in values)
                                   or len(values) != len(set(values))):
            raise ValueError("proposal keyword IDs must be unique valid IDs")
        return values

    @model_validator(mode="after")
    def non_empty(self):
        if self.name is None and self.description is None and self.keyword_ids is None:
            raise ValueError("at least one proposal field must be changed")
        return self


def _origin(request):
    validate_public_request_origin(request, request.app.state.settings.environment_profile)


def _service(request, *, write=False):
    _origin(request)
    if write and bool(getattr(request.app.state, "read_only", False)):
        raise HTTPException(405, {"code": "AI_STORAGE_UNAVAILABLE"})
    service = (getattr(request.state, "ai_service", None)
               or getattr(request.app.state, "ai_service", None))
    if service is None and write:
        raise HTTPException(503, {"code": "AI_STORAGE_UNAVAILABLE"})
    return service


def _collection_service(request):
    ai_service = (getattr(request.state, "ai_service", None)
                  or getattr(request.app.state, "ai_service", None))
    if getattr(getattr(ai_service, "config", None), "data_profile", None) == "PUBLIC_DEMO":
        return (getattr(request.state, "ai_collection_service", None)
                or getattr(request.app.state, "ai_collection_service", None))
    return (getattr(request.state, "collection_service", None)
            or getattr(request.app.state, "collection_service", None))


def _error(exc):
    code = exc.code if isinstance(exc, AIExecutionError) else "AI_STORAGE_UNAVAILABLE"
    status = {
        "AI_KEYWORD_NOT_FOUND": 404,
        "AI_COLLECTION_NOT_FOUND": 404,
        "AI_JOB_NOT_RESUMABLE": 409,
        "AI_INPUT_CHANGED": 409,
        "AI_PREVIEW_NOT_APPLICABLE": 409,
        "AI_PREVIEW_EXPIRED": 409,
        "AI_PREVIEW_INVALID": 409,
        "AI_PROPOSAL_EXPIRED": 409,
        "COLLECTION_NAME_CONFLICT": 409,
        "COLLECTION_CONTENT_CONFLICT": 409,
        "AI_STORAGE_UNAVAILABLE": 503,
        "AI_DISABLED": 503,
        "AI_POLICY_DENIED": 403,
    }.get(code, 422)
    return HTTPException(status, {"code": code})


@router.get("/ai/capabilities")
def ai_capabilities(request: Request):
    service = _service(request)
    if service is None:
        return {"enabled": False, "storage": "UNAVAILABLE", "models": {
            "classification": None, "recommendation": None}, "metadata_verification": {
                "classification": {"status": "NOT_CHECKED", "verified_at": None},
                "recommendation": {"status": "NOT_CHECKED", "verified_at": None}},
            "quota": {"daily_used": None, "daily_limit": 40, "minute_used": None,
                      "minute_limit": 20}, "provider_retry_at": None,
            "source_policy": None, "provider_quota": None,
            "error_code": "AI_STORAGE_UNAVAILABLE"}
    try:
        return service.capabilities()
    except Exception:
        return {"enabled": False, "storage": "UNAVAILABLE", "models": {
            "classification": None, "recommendation": None}, "metadata_verification": {
                "classification": {"status": "NOT_CHECKED", "verified_at": None},
                "recommendation": {"status": "NOT_CHECKED", "verified_at": None}},
            "quota": {"daily_used": None, "daily_limit": 40, "minute_used": None,
                      "minute_limit": 20}, "provider_retry_at": None,
            "source_policy": None, "provider_quota": None,
            "error_code": "AI_STORAGE_UNAVAILABLE"}


@router.post("/ai/classifications/reclassify")
def reclassify(value: ClassificationInput, request: Request,
               idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    service = _service(request, write=True)
    if idempotency_key is None:
        raise HTTPException(422, {"code": "AI_IDEMPOTENCY_REQUIRED"})
    collection_service = _collection_service(request)
    if collection_service is None:
        raise HTTPException(503, {"code": "AI_STORAGE_UNAVAILABLE"})
    scope = {"keyword_ids": value.keyword_ids, "collection_ids": value.collection_ids,
             "include_unassigned": value.include_unassigned}
    try:
        if value.collection_ids or value.include_unassigned:
            resolver = getattr(collection_service.store, "resolve_ai_reclassification_scope", None)
            if resolver is None:
                raise HTTPException(503, {"code": "AI_STORAGE_UNAVAILABLE"})
            selected_ids = resolver(**scope)
        else:
            selected_ids = value.keyword_ids
        return service.classify_keyword_ids(
            collection_service.store, selected_ids,
            idempotency_key=idempotency_key, preview=value.preview, scope=scope,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        code = str(exc) if str(exc).startswith("AI_") else "AI_STORAGE_UNAVAILABLE"
        raise HTTPException(422 if code.startswith("AI_RECLASSIFICATION_SCOPE_") else 404,
                            {"code": code}) from None
    except AIExecutionError as exc:
        raise _error(exc) from None
    except Exception:
        raise HTTPException(503, {"code": "AI_STORAGE_UNAVAILABLE"}) from None


@router.post("/ai/classifications/previews/{preview_job_id}/apply")
def apply_classification_preview(preview_job_id: str, request: Request):
    service = _service(request, write=True)
    collection_service = _collection_service(request)
    if collection_service is None:
        raise HTTPException(503, {"code": "AI_STORAGE_UNAVAILABLE"})
    try:
        return service.apply_classification_preview(collection_service.store, preview_job_id)
    except AIExecutionError as exc:
        raise _error(exc) from None
    except Exception:
        raise HTTPException(503, {"code": "AI_STORAGE_UNAVAILABLE"}) from None


@router.post("/ai/recommendations")
def recommendations(value: RecommendationInput, request: Request,
                    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    service = _service(request, write=True)
    if idempotency_key is None:
        raise HTTPException(422, {"code": "AI_IDEMPOTENCY_REQUIRED"})
    collection_service = _collection_service(request)
    if collection_service is None:
        raise HTTPException(503, {"code": "AI_STORAGE_UNAVAILABLE"})
    try:
        return service.recommend(
            collection_service.store, value.keyword_ids,
            research_constraints=value.research_constraints,
            idempotency_key=idempotency_key, regenerate=value.regenerate,
        )
    except AIExecutionError as exc:
        raise _error(exc) from None
    except Exception:
        raise HTTPException(503, {"code": "AI_STORAGE_UNAVAILABLE"}) from None


@router.get("/ai/jobs/{job_id}")
def get_ai_job(job_id: str, request: Request):
    service = _service(request)
    if service is None:
        raise HTTPException(503, {"code": "AI_STORAGE_UNAVAILABLE"})
    try:
        result = service.get_job(job_id)
    except AIExecutionError as exc:
        raise _error(exc) from None
    if result is None:
        raise HTTPException(404, {"code": "AI_JOB_NOT_RESUMABLE"})
    return result


@router.post("/ai/jobs/{job_id}/resume")
def resume_ai_job(job_id: str, request: Request):
    service = _service(request, write=True)
    collection_service = _collection_service(request)
    if collection_service is None:
        raise HTTPException(503, {"code": "AI_STORAGE_UNAVAILABLE"})
    try:
        return service.resume_job(collection_service.store, job_id)
    except AIExecutionError as exc:
        raise _error(exc) from None
    except Exception:
        raise HTTPException(503, {"code": "AI_STORAGE_UNAVAILABLE"}) from None


@router.patch("/ai/proposals/{proposal_id}")
def patch_ai_proposal(proposal_id: str, value: ProposalPatch, request: Request):
    ai_service = _service(request, write=True)
    collection_service = _collection_service(request)
    if collection_service is None:
        raise HTTPException(503, {"code": "AI_STORAGE_UNAVAILABLE"})
    try:
        changed = collection_service.store.patch_proposal(
            proposal_id, name=value.name, description=value.description,
            support_ids=value.keyword_ids,
            allowed_sources=getattr(ai_service, "_allowed_sources", None),
        )
    except ValueError as exc:
        code = str(exc)
        if (not code.startswith(("AI_", "INVALID_"))
                and code not in {"COLLECTION_NAME_CONFLICT", "COLLECTION_CONTENT_CONFLICT"}):
            code = "AI_KEYWORD_NOT_FOUND"
        status = (404 if code == "AI_KEYWORD_NOT_FOUND" else
                  403 if code == "AI_POLICY_DENIED" else
                  409 if code in {"AI_PROPOSAL_EXPIRED", "AI_INPUT_CHANGED",
                                  "COLLECTION_NAME_CONFLICT", "COLLECTION_CONTENT_CONFLICT"} else 422)
        raise HTTPException(status, {"code": code}) from None
    except Exception:
        raise HTTPException(503, {"code": "AI_STORAGE_UNAVAILABLE"}) from None
    if not changed:
        raise HTTPException(404, {"code": "AI_JOB_NOT_RESUMABLE"})
    return {"proposal_id": proposal_id, "updated": True}
