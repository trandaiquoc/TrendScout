"""Combined read for the first dashboard view."""

from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Query, Request

from app.api.analytics import (
    build_local_run_history_projection,
    refresh_analytics_projection_ages,
)
from app.api.analytics import (
    overview as analytics_overview,
)
from app.api.collections import collection_workbench_for_service
from app.api.datasets import latest_dataset, latest_keywords, latest_keywords_for_service
from app.api.fetches import validate_public_request_origin
from app.api.research import research_bootstrap_for_service

router = APIRouter()
_LATEST_SUMMARY_NOT_PROVIDED = object()


def build_default_dashboard_bootstrap(
    service, *, available_sources, latest_summary=_LATEST_SUMMARY_NOT_PROVIDED,
):
    """Build the exact default dashboard response from one restored store."""
    from app.api.algorithms import algorithm_results_for_service

    if service is None:
        raise ValueError("dashboard read model requires a collection service")
    latest = (service.store.latest_dataset()
              if latest_summary is _LATEST_SUMMARY_NOT_PROVIDED else latest_summary)
    request = SimpleNamespace(
        state=SimpleNamespace(
            collection_service=service,
            latest_dataset_summary_available=True,
            latest_dataset_summary=latest,
        ),
        app=SimpleNamespace(state=SimpleNamespace(
            collection_service=service,
            public_fetch_enabled=True,
            public_fetch_source_ids=tuple(sorted(available_sources)),
            settings=SimpleNamespace(environment_profile="vercel"),
        )),
    )
    keyword_page = latest_keywords_for_service(
        service, limit=100, offset=0, include_algorithm_results=False,
    )
    dashboard_keywords = {
        **keyword_page,
        "items": keyword_page.get("items", [])[:20],
        "limit": 20,
        "offset": 0,
        "next_offset": 20 if keyword_page.get("total", 0) > 20 else None,
    }
    collection_keyword_page = {
        **keyword_page,
        "items": keyword_page.get("items", [])[:25],
        "limit": 25,
        "offset": 0,
        "next_offset": 25 if keyword_page.get("total", 0) > 25 else None,
    }
    return {
        "item": latest_dataset(request).get("item"),
        "analytics": analytics_overview(request),
        "keywords": dashboard_keywords,
        "collections": collection_workbench_for_service(
            service, keyword_page=collection_keyword_page,
        ),
        "algorithm_results": algorithm_results_for_service(service, limit=100),
        "run_history": build_local_run_history_projection(service, limit=20),
        "research": research_bootstrap_for_service(
            service, keyword_page=keyword_page,
        ),
    }


def _refresh_projection_ages(payload):
    value = deepcopy(payload)
    now = datetime.now(timezone.utc)
    item = value.get("item")
    if isinstance(item, dict):
        timestamp = item.get("created_at") or item.get("fetched_at")
        try:
            observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if observed.tzinfo is None or observed.utcoffset() is None:
                raise ValueError("timezone missing")
        except (AttributeError, TypeError, ValueError):
            item["age_seconds"] = None
        else:
            item["age_seconds"] = max(
                0, int((now - observed.astimezone(timezone.utc)).total_seconds())
            )
    if isinstance(value.get("analytics"), dict):
        value["analytics"] = refresh_analytics_projection_ages(value["analytics"])
    return value


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
    is_default = (
        q == "" and state == "all" and classification == "all" and evidence == "all"
        and not collection_ids and source == "" and sort == "recent"
        and date_from is None and date_to is None and limit == 20 and offset == 0
        and include_algorithm_results is False
    )
    if (is_default and getattr(request.state, "dashboard_bootstrap_available", False)
            and isinstance(getattr(request.state, "dashboard_bootstrap", None), dict)):
        validate_public_request_origin(request, request.app.state.settings.environment_profile)
        projection = _refresh_projection_ages(request.state.dashboard_bootstrap)
        return {key: projection[key] for key in ("item", "analytics", "keywords")}

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
