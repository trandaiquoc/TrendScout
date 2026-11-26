"""Persistent, user-authored product research notes; no market facts are inferred."""

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.collections import _mutating, _service
from app.api.fetches import validate_public_request_origin

router = APIRouter()
ResearchStatus = Literal["DRAFT", "RESEARCHING", "NEEDS_REVIEW", "VALIDATED", "ARCHIVED"]


class ResearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword_id: str | None = Field(default=None, max_length=128)
    subject: str = Field(min_length=1, max_length=180)
    hypothesis: str = Field(min_length=1, max_length=2000)
    product_category: str = Field(default="", max_length=160)
    niche: str = Field(default="", max_length=160)
    competitor_notes: str = Field(default="", max_length=4000)
    market_evidence: str = Field(default="", max_length=4000)
    ip_notes: str = Field(default="", max_length=3000)
    risk_notes: str = Field(default="", max_length=3000)
    findings: str = Field(default="", max_length=4000)
    status: ResearchStatus = "DRAFT"


class ResearchPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str | None = Field(default=None, min_length=1, max_length=180)
    hypothesis: str | None = Field(default=None, min_length=1, max_length=2000)
    product_category: str | None = Field(default=None, max_length=160)
    niche: str | None = Field(default=None, max_length=160)
    competitor_notes: str | None = Field(default=None, max_length=4000)
    status: ResearchStatus | None = None
    ip_notes: str | None = Field(default=None, max_length=3000)
    findings: str | None = Field(default=None, max_length=4000)
    risk_notes: str | None = Field(default=None, max_length=3000)
    market_evidence: str | None = Field(default=None, max_length=4000)


def list_research_for_service(service, *, q="", status=None, limit=50, offset=0):
    return service.store.list_research(q=q, status=status, limit=limit, offset=offset)


def research_bootstrap_for_service(service, *, q="", status=None, keyword_page=None):
    """Build the Research landing payload, keeping its keyword selector compact."""
    from app.api.datasets import latest_keywords_for_service

    keyword_page = keyword_page or latest_keywords_for_service(
        service, limit=100, offset=0, include_algorithm_results=False,
    )
    keyword_page = {
        **keyword_page,
        "items": [
            {
                key: item[key]
                for key in ("id", "keyword_id", "keyword", "source", "observed_at", "observation_date")
                if key in item
            }
            for item in keyword_page.get("items", []) if isinstance(item, dict)
        ],
    }
    return {
        "keywords": keyword_page,
        "research": list_research_for_service(
            service, q=q, status=status, limit=50, offset=0,
        ),
    }


@router.get("/research")
def list_research(
    request: Request,
    q: str = Query(default="", max_length=100),
    status: ResearchStatus | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
):
    return list_research_for_service(
        _service(request), q=q, status=status, limit=limit, offset=offset,
    )


@router.get("/research/bootstrap")
def research_bootstrap(
    request: Request,
    q: str = Query(default="", max_length=100),
    status: ResearchStatus | None = None,
):
    """Load the initial Research page through one snapshot-backed request."""
    request_state = getattr(request, "state", None)
    projection = getattr(request_state, "research_bootstrap", None)
    if (getattr(request_state, "research_bootstrap_available", False)
            and not q and not status and isinstance(projection, dict)):
        validate_public_request_origin(request, request.app.state.settings.environment_profile)
        return projection
    service = _service(request)
    return research_bootstrap_for_service(service, q=q, status=status)


@router.post("/research", status_code=201)
def create_research(value: ResearchInput, request: Request):
    _mutating(request)
    service = _service(request)
    now = datetime.now(timezone.utc).isoformat()
    identifier = uuid.uuid4().hex
    data = value.model_dump()
    record = {key: content.strip() if isinstance(content, str) else content
              for key, content in data.items()}
    record.update({"id": identifier, "created_at": now, "updated_at": now})
    try:
        return service.store.create_research(record)
    except ValueError as exc:
        if str(exc) == "RESEARCH_KEYWORD_NOT_FOUND":
            raise HTTPException(422, {"code": str(exc)}) from None
        raise


@router.patch("/research/{research_id}")
def update_research(research_id: str, value: ResearchPatch, request: Request):
    _mutating(request)
    service = _service(request)
    updates = value.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(422, {"code": "EMPTY_RESEARCH_UPDATE"})
    for field, content in updates.items():
        if content is None:
            raise HTTPException(422, {"code": "RESEARCH_FIELD_CANNOT_BE_NULL", "field": field})
        if isinstance(content, str):
            content = content.strip()
            if field in {"subject", "hypothesis"} and not content:
                raise HTTPException(422, {"code": "RESEARCH_FIELD_REQUIRED", "field": field})
            updates[field] = content
    updated = service.store.update_research(
        research_id, updates, datetime.now(timezone.utc).isoformat(),
    )
    if updated is None:
        raise HTTPException(404, {"code": "RESEARCH_NOT_FOUND"})
    return updated
