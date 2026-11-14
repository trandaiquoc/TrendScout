"""Persistent, user-authored product research notes; no market facts are inferred."""

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.collections import _mutating, _service

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


@router.get("/research")
def list_research(
    request: Request,
    q: str = Query(default="", max_length=100),
    status: ResearchStatus | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
):
    service = _service(request)
    query = " ".join(q.casefold().split())
    where = []
    parameters = []
    if status:
        where.append("r.status=?")
        parameters.append(status)
    if query:
        where.append(
            'lower(r.subject || " " || r.hypothesis || " " || r.product_category || " " || r.niche) LIKE ?'
        )
        parameters.append("%" + query + "%")
    clause = " WHERE " + " AND ".join(where) if where else ""
    with service.store.connect() as db:
        total = db.execute(
            "SELECT count(*) FROM research_records r" + clause, parameters
        ).fetchone()[0]
        rows = db.execute(
            """SELECT r.*,k.keyword,k.source,k.project,k.observed_at,k.data_mode
            FROM research_records r LEFT JOIN keywords k ON k.id=r.keyword_id"""
            + clause
            + " ORDER BY r.updated_at DESC,r.id LIMIT ? OFFSET ?",
            (*parameters, limit, offset),
        ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if offset + limit < total else None,
    }


@router.post("/research", status_code=201)
def create_research(value: ResearchInput, request: Request):
    _mutating(request)
    service = _service(request)
    now = datetime.now(timezone.utc).isoformat()
    identifier = uuid.uuid4().hex
    data = value.model_dump()
    if data["keyword_id"]:
        with service.store.connect() as db:
            if (
                db.execute("SELECT 1 FROM keywords WHERE id=?", (data["keyword_id"],)).fetchone()
                is None
            ):
                raise HTTPException(422, {"code": "RESEARCH_KEYWORD_NOT_FOUND"})
    with service.store.connect() as db:
        db.execute(
            """INSERT INTO research_records(id,keyword_id,subject,hypothesis,product_category,niche,
            competitor_notes,market_evidence,ip_notes,risk_notes,findings,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                data["keyword_id"],
                data["subject"].strip(),
                data["hypothesis"].strip(),
                data["product_category"].strip(),
                data["niche"].strip(),
                data["competitor_notes"].strip(),
                data["market_evidence"].strip(),
                data["ip_notes"].strip(),
                data["risk_notes"].strip(),
                data["findings"].strip(),
                data["status"],
                now,
                now,
            ),
        )
    return {"id": identifier, "status": data["status"], "created_at": now, "updated_at": now}


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
    with service.store.connect() as db:
        if (
            db.execute("SELECT 1 FROM research_records WHERE id=?", (research_id,)).fetchone()
            is None
        ):
            raise HTTPException(404, {"code": "RESEARCH_NOT_FOUND"})
        assignments = [key + "=?" for key in updates] + ["updated_at=?"]
        db.execute(
            "UPDATE research_records SET " + ",".join(assignments) + " WHERE id=?",
            (*updates.values(), datetime.now(timezone.utc).isoformat(), research_id),
        )
        row = db.execute(
            "SELECT id,status,updated_at FROM research_records WHERE id=?", (research_id,)
        ).fetchone()
    return dict(row)
