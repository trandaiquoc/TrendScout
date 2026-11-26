"""Vercel LIVE composition backed by the shared PostgreSQL database."""

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
    """Compose a request-scoped PostgreSQL worker without OIDC or startup fetches."""
    if (
        os.environ.get("VERCEL") != "1"
        or not isinstance(user_agent, str)
        or not user_agent.strip()
        or not isinstance(postgres_environment, dict)
        or not isinstance(postgres_schema, str)
        or not callable(utcnow)
        or type(capability_metadata_request) is not bool
    ):
        raise ValueError("Vercel PostgreSQL LIVE requires its configured deployment runtime")

    from app.collections.entity_context import WikimediaEntityContextResolver
    from app.collections.service import CollectionService
    from app.ai.openrouter import OpenRouterAdapter
    from app.ai.runtime import AIConfig, AIService
    from app.google_live_settings import GoogleLiveSettings
    from app.orchestration.normal_google_live_composition import (
        normal_google_live_composition,
    )
    from app.orchestration.vercel_credentials import VercelEnvironmentCredentialProvider
    from app.scheduling.postgres import PostgresScheduleStore
    from app.storage.postgres_atomic_state import PostgresAtomicStateStore
    from app.storage.postgres_analysis import PostgresAnalysisStorage

    google = GoogleLiveSettings.from_environment()
    async with normal_google_live_composition(
        user_agent=user_agent,
        utcnow=utcnow,
        google_settings=google,
        credential_provider=VercelEnvironmentCredentialProvider(),
        force_refresh_trends=True,
        allow_initial_google_acceptance=True,
        allow_initial_bing_acceptance=True,
        preserve_expired_data=True,
        youtube_max_queries=1,
        youtube_allow_initial_acceptance=True,
        vercel_shared_live=True,
        capability_metadata_request=capability_metadata_request,
        vercel_postgres_runtime=True,
        postgres_environment=postgres_environment,
        postgres_schema=postgres_schema,
    ) as facade:
        facade.available_sources = frozenset(facade.admission.policy.enabled_sources)
        ai_config = AIConfig.from_environment()
        ai_provider = OpenRouterAdapter(os.environ.get("OPENROUTER_API_KEY"))
        facade.ai_service = AIService(
            PostgresAtomicStateStore(postgres_environment, schema=postgres_schema),
            config=ai_config,
            provider=ai_provider,
        )
        facade.collection_service = CollectionService(
            facade.collection_store,
            entity_context_resolver=WikimediaEntityContextResolver(
                facade.collection_store,
                before_request=facade.entity_lookup_before_request,
            ),
            fetch_model_inference_enabled=False,
            ai_engine=facade.ai_service,
        )
        facade.analysis_storage = PostgresAnalysisStorage(
            postgres_environment, schema=postgres_schema
        )
        facade.scheduler_store = PostgresScheduleStore(
            postgres_environment, schema=postgres_schema
        )
        facade.google_capabilities = {
            **facade.google_capabilities,
            "runtime_scope": "VERCEL_POSTGRES_LIVE",
            "local_only": False,
        }
        facade.youtube_capabilities = {
            **facade.youtube_capabilities,
            "approval_status": (
                "APPROVED_FOR_VERCEL_POSTGRES_RUNTIME"
                if google.youtube_approval_configured
                and facade.youtube_capabilities.get("configuration_status") == "PRESENT"
                else "PENDING_LIVE_APPROVAL"
            ),
            "live_host_status": (
                "VERCEL_POSTGRES_RUNTIME" if facade.normal_youtube_live else "NOT_DESIGNATED"
            ),
            "runtime_mode": "VERCEL_OWNER_TRIGGERED",
        }
        try:
            yield facade
        finally:
            ai_provider.close()
