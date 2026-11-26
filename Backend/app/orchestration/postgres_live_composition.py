"""Shared PostgreSQL LIVE runtime composition for Local and Vercel."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone


@asynccontextmanager
async def postgres_live_composition(
    *,
    runtime_scope: str,
    user_agent: str,
    postgres_environment: dict[str, str],
    postgres_schema: str = "trendscout",
    utcnow=lambda: datetime.now(timezone.utc),
    google_settings=None,
    credential_provider=None,
    force_refresh_trends: bool = False,
    allow_initial_google_acceptance: bool = False,
    allow_initial_bing_acceptance: bool = False,
    preserve_expired_data: bool = False,
    youtube_max_queries: int = 2,
    youtube_manual_live: bool = False,
    youtube_force_fresh_test_once: bool = False,
    youtube_allow_initial_acceptance: bool = False,
    capability_metadata_request: bool = False,
):
    """Build all application persistence services over one PostgreSQL schema.

    This function performs no provider work. Its scope controls credentials and
    deployment-specific admission, while every runtime store uses the same
    PostgreSQL environment and schema.
    """
    if (
        runtime_scope not in {"LOCAL", "VERCEL"}
        or not isinstance(user_agent, str)
        or not user_agent.strip()
        or not isinstance(postgres_environment, dict)
        or not isinstance(postgres_schema, str)
        or not callable(utcnow)
        or type(capability_metadata_request) is not bool
        or (runtime_scope == "VERCEL" and os.environ.get("VERCEL") != "1")
        or (runtime_scope == "LOCAL" and os.environ.get("VERCEL"))
    ):
        raise ValueError("PostgreSQL LIVE requires its configured runtime scope")

    from app.ai.openrouter import OpenRouterAdapter
    from app.ai.runtime import AIConfig, AIService
    from app.collections.entity_context import WikimediaEntityContextResolver
    from app.collections.postgres_store import PostgresCollectionStore
    from app.collections.service import CollectionService
    from app.google_live_settings import GoogleLiveSettings
    from app.orchestration.normal_google_live_composition import (
        normal_google_live_composition,
    )
    from app.scheduling.postgres import PostgresScheduleStore
    from app.storage.postgres_analysis import PostgresAnalysisStorage
    from app.storage.postgres_atomic_state import PostgresAtomicStateStore

    config = google_settings or GoogleLiveSettings.from_environment()
    if not isinstance(config, GoogleLiveSettings):
        raise ValueError("PostgreSQL LIVE requires validated Google runtime settings")

    composition_options = {}
    if runtime_scope == "VERCEL":
        from app.orchestration.vercel_credentials import VercelEnvironmentCredentialProvider

        credential_provider = VercelEnvironmentCredentialProvider()
        composition_options = {
            "force_refresh_trends": True,
            "allow_initial_google_acceptance": True,
            "allow_initial_bing_acceptance": True,
            "preserve_expired_data": True,
            "youtube_max_queries": 1,
            "youtube_allow_initial_acceptance": True,
            "vercel_shared_live": True,
            "capability_metadata_request": capability_metadata_request,
            "vercel_postgres_runtime": True,
        }
    else:
        composition_options = {
            "force_refresh_trends": force_refresh_trends,
            "allow_initial_google_acceptance": allow_initial_google_acceptance,
            "allow_initial_bing_acceptance": allow_initial_bing_acceptance,
            "preserve_expired_data": preserve_expired_data,
            "youtube_max_queries": youtube_max_queries,
            "youtube_manual_live": youtube_manual_live,
            "youtube_force_fresh_test_once": youtube_force_fresh_test_once,
            "youtube_allow_initial_acceptance": youtube_allow_initial_acceptance,
            "capability_metadata_request": False,
            "vercel_postgres_runtime": False,
        }

    async with normal_google_live_composition(
        user_agent=user_agent,
        utcnow=utcnow,
        google_settings=config,
        credential_provider=credential_provider,
        postgres_environment=postgres_environment,
        postgres_schema=postgres_schema,
        **composition_options,
    ) as facade:
        collection_store = getattr(facade, "collection_store", None)
        if not isinstance(collection_store, PostgresCollectionStore):
            raise RuntimeError("PostgreSQL runtime requires the PostgreSQL Collection store")

        ai_provider = OpenRouterAdapter(os.environ.get("OPENROUTER_API_KEY"))
        try:
            facade.ai_service = AIService(
                PostgresAtomicStateStore(postgres_environment, schema=postgres_schema),
                config=AIConfig.from_environment(),
                provider=ai_provider,
            )
            facade.collection_service = CollectionService(
                collection_store,
                entity_context_resolver=WikimediaEntityContextResolver(
                    collection_store,
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
            facade.postgres_runtime_scope = runtime_scope
            facade.runtime_persistence_backend = "POSTGRESQL"
            if runtime_scope == "VERCEL":
                facade.available_sources = frozenset(
                    facade.admission.policy.enabled_sources
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
                        if config.youtube_approval_configured
                        and facade.youtube_capabilities.get("configuration_status") == "PRESENT"
                        else "PENDING_LIVE_APPROVAL"
                    ),
                    "live_host_status": (
                        "VERCEL_POSTGRES_RUNTIME"
                        if facade.normal_youtube_live else "NOT_DESIGNATED"
                    ),
                    "runtime_mode": "VERCEL_OWNER_TRIGGERED",
                }
            yield facade
        finally:
            ai_provider.close()
