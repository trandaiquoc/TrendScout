"""Vercel entry point for the shared PostgreSQL LIVE composition."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone


@asynccontextmanager
async def vercel_postgres_live_composition(
    *,
    user_agent: str,
    postgres_environment: dict[str, str],
    postgres_schema: str = "trendscout",
    utcnow=lambda: datetime.now(timezone.utc),
    capability_metadata_request: bool = False,
):
    """Compose Vercel's request-scoped PostgreSQL worker without startup fetches."""
    if os.environ.get("VERCEL") != "1":
        raise ValueError("Vercel PostgreSQL LIVE requires its configured deployment runtime")

    from app.google_live_settings import GoogleLiveSettings
    from app.orchestration.postgres_live_composition import postgres_live_composition

    async with postgres_live_composition(
        runtime_scope="VERCEL",
        user_agent=user_agent,
        postgres_environment=postgres_environment,
        postgres_schema=postgres_schema,
        utcnow=utcnow,
        google_settings=GoogleLiveSettings.from_environment(),
        capability_metadata_request=capability_metadata_request,
    ) as facade:
        yield facade
