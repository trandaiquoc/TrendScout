"""Combined read for the first dashboard view."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Query, Request

from app.api.analytics import overview as analytics_overview
from app.api.datasets import latest_dataset, latest_keywords

router = APIRouter()


@router.get("/dashboard/bootstrap")
def dashboard_bootstrap(
    request: Request,
    q: str = Query(default="", max_length=100),
    state: str = Query(
        default="all",
        pattern="^(all|unassigned|ai|manual|mixed|needs_review|excluded|non_topic)$",
    ),
    classification: Literal["all", "CLASSIFIED", "AMBIGUOUS", "UNKNOWN", "INELIGIBLE"] = "all",
    evidence: Literal["all", "metric", "algorithm", "missing"] = "all",
    collection_ids: list[str] = Query(default=[]),
    source: str = Query(default="", max_length=100),
    sort: str = Query(default="recent", pattern="^(recent|keyword|views|rank)$"),
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    include_algorithm_results: bool = False,
):
    """Read initial dashboard data from one restored SQLite snapshot."""
    dataset = latest_dataset(request)
    analytics = analytics_overview(request)
    keywords = latest_keywords(
        request,
        q=q,
        state=state,
        classification=classification,
        evidence=evidence,
        collection_ids=collection_ids,
        source=source,
        sort=sort,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
        include_algorithm_results=include_algorithm_results,
    )
    return {
        "item": dataset.get("item"),
        "analytics": analytics,
        "keywords": keywords,
    }
