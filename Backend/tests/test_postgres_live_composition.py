"""PostgreSQL LIVE composition stays explicit and does not fetch on startup."""

import os
import unittest
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from app.ai.openrouter import OpenRouterAdapter
from app.api.analytics import _history_storage
from app.api.runs import read_storage
from app.collections.postgres_store import PostgresCollectionStore
from app.google_live_settings import GoogleLiveSettings
from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.main import VercelSharedLiveMiddleware, create_app
from app.normal_wikimedia_live_policy import GOOGLE_STATE_KEY, normal_google_live_policy
from app.orchestration.normal_google_live_composition import normal_google_live_composition
from app.orchestration.postgres_live_composition import postgres_live_composition
from app.scheduling.postgres import PostgresScheduleStore
from app.settings import Settings
from app.sources.catalog import GOOGLE_SOURCE_ID, SOURCE_CAPABILITIES
from app.sources.serpapi.account import SerpApiAccountClient
from app.storage.postgres_analysis import PostgresAnalysisStorage
from app.storage.postgres_atomic_state import PostgresAtomicStateStore
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations


class _NeverReadCredentials:
    def resolve(self, _reference):
        raise AssertionError("composition must not read provider credentials")


def _seed_cutover_readiness(connection, environment, schema, *, now):
    namespace = sql.Identifier(schema)
    connection.execute(sql.SQL("""INSERT INTO {}.keywords
        (id, keyword, normalized, source, project, observed_at, data_mode, evidence)
        VALUES (%s,%s,%s,%s,%s,%s,'LIVE','{{}}'::jsonb)""").format(namespace),
        ("runtime-readiness", "runtime readiness fixture", "runtime readiness fixture",
         "fixture", "test", now.date().isoformat()),
    )
    connection.execute(sql.SQL("""INSERT INTO {}.dataset_snapshots
        (dataset_id,status,data_mode,source_manifest,provider_record_count,
         received_record_count,retained_keyword_count,rejected_record_count,
         observation_start,observation_end,fetched_at,quality_flags,processing_status,
         eligible_keyword_count,assigned_keyword_count,unassigned_keyword_count,created_at)
        VALUES (%s,'PARTIAL','LIVE','[]'::jsonb,0,0,1,0,%s,%s,%s,'[]'::jsonb,
                'MODEL_UNAVAILABLE',1,0,1,%s)""").format(namespace),
        ("runtime-readiness", now.date().isoformat(), now.date().isoformat(), now, now),
    )

    google = GoogleLiveSettings()
    policy = normal_google_live_policy(
        serpapi_daily_budget=google.serpapi_daily_budget,
        serpapi_monthly_budget=google.serpapi_monthly_budget,
        rss_daily_budget=google.rss_daily_budget,
        rss_monthly_budget=google.rss_monthly_budget,
        fetch_starts_per_hour=google.fetch_starts_per_hour,
        bing_enabled=google.paid_bing_approved,
        bing_daily_budget=google.bing_daily_budget,
        youtube_enabled=True,
    )
    store = PostgresAtomicStateStore(environment, schema=schema)
    admission = PublicAdmission(
        store, policy, state_key=GOOGLE_STATE_KEY, require_quota_evidence=True
    )
    admission.initialize_operational_state(now=now)
    admission.set_serpapi_billing_cycle(
        (now.date() + timedelta(days=15)).isoformat(), now=now
    )
    for pool_name in policy.pools:
        for period in ("daily", "monthly"):
            admission.record_quota_window_evidence(
                pool_name,
                period,
                requests=0,
                cost=0,
                evidence_source="isolated_postgres_test_seed",
                now=now,
            )


class PostgresLiveCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_postgres_unavailable_fails_closed_without_firestore_fallback(self):
        settings = Settings.from_environment({
            "VERCEL": "1",
            "TREND_SCOUT_RUN_MODE": "live",
            "TREND_SCOUT_POSTGRES_RUNTIME_ENABLED": "true",
            "DATABASE_URL": "postgresql://runtime.invalid/db",
            "DATABASE_URL_UNPOOLED": "postgresql://runtime.invalid/db",
        })
        firestore_calls = []

        @asynccontextmanager
        async def unavailable_context(**_kwargs):
            raise RuntimeError("connection unavailable")
            yield None

        async def firestore_factory(_token):
            firestore_calls.append(True)
            raise AssertionError("PostgreSQL runtime must not fall back to Firestore")

        async def app(_scope, _receive, _send):
            raise AssertionError("unavailable PostgreSQL request reached the app")

        middleware = VercelSharedLiveMiddleware(
            app,
            settings=settings,
            enabled=True,
            client_factory=firestore_factory,
            postgres_environment={
                "DATABASE_URL": "postgresql://runtime.invalid/db",
                "DATABASE_URL_UNPOOLED": "postgresql://runtime.invalid/db",
            },
            postgres_context_factory=unavailable_context,
        )
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await middleware({
            "type": "http",
            "method": "GET",
            "path": "/api/v1/collections",
            "headers": [],
            "state": {},
        }, receive, send)
        starts = [item for item in messages if item["type"] == "http.response.start"]
        bodies = [item.get("body", b"") for item in messages if item["type"] == "http.response.body"]
        self.assertEqual(starts[0]["status"], 503)
        self.assertIn(b"POSTGRES_RUNTIME_UNAVAILABLE", b"".join(bodies))
        self.assertEqual(firestore_calls, [])

    async def test_shared_local_and_vercel_composition_reads_and_writes_one_schema(self):
        url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
        if not url:
            self.skipTest("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
        environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
        schema = "ts_test_" + uuid.uuid4().hex
        namespace = sql.Identifier(schema)
        schema_created = False
        local_settings = Settings.from_environment({
            "TREND_SCOUT_ENVIRONMENT": "local",
            "TREND_SCOUT_RUN_MODE": "live",
            "TREND_SCOUT_READ_ONLY": "false",
            "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false",
            "TREND_SCOUT_POSTGRES_RUNTIME_ENABLED": "true",
            "DATABASE_URL": url,
            "DATABASE_URL_UNPOOLED": url,
            "WIKIMEDIA_USER_AGENT": "TrendScoutTest/1.0 (https://example.invalid)",
        })

        @asynccontextmanager
        async def local_context():
            async with postgres_live_composition(
                runtime_scope="LOCAL",
                user_agent=local_settings.wikimedia_user_agent,
                google_settings=GoogleLiveSettings(),
                credential_provider=_NeverReadCredentials(),
                postgres_environment=environment,
                postgres_schema=schema,
            ) as facade:
                yield facade

        safe_provider_environment = {
            "SERPAPI_API_KEY": "",
            "YOUTUBE_API_KEY": "",
            "OPENROUTER_API_KEY": "",
            "TRENDSCOUT_AI_DATA_PROFILE": "PUBLIC_DATA",
            "TREND_SCOUT_GOOGLE_TRENDS_APPROVED": "false",
            "TREND_SCOUT_GOOGLE_RSS_APPROVED": "false",
            "TREND_SCOUT_GOOGLE_SERP_APPROVED": "false",
            "TREND_SCOUT_BING_SERP_APPROVED": "false",
            "TREND_SCOUT_SERPAPI_BING_POLICY_MIGRATION_APPROVED": "false",
            "TREND_SCOUT_SERPAPI_ACCOUNT_API_APPROVED": "false",
            "TREND_SCOUT_YOUTUBE_LIVE_APPROVED": "false",
            "TREND_SCOUT_SCHEDULER_ENABLED": "false",
            "TREND_SCOUT_POSTGRES_SCHEMA": schema,
        }

        with postgres_connection(environment, direct=True) as connection:
            connection.autocommit = True
            self.assertFalse(connection.execute(
                "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
            ).fetchone()[0])
            try:
                schema_created = True
                apply_migrations(connection, schema=schema)
                with patch.dict(os.environ, safe_provider_environment, clear=False):
                    local_app = create_app(
                        settings=local_settings,
                        local_live_opt_in=True,
                        local_live_context_factory=local_context,
                        postgres_environment=environment,
                        postgres_schema=schema,
                    )
                    with patch(
                        "app.collections.store.CollectionStore.local",
                        side_effect=AssertionError("PostgreSQL runtime must not open SQLite"),
                    ), patch(
                        "app.orchestration.normal_google_live_composition."
                        "LocalSQLiteAtomicStateStore.for_local_live",
                        side_effect=AssertionError("PostgreSQL runtime must not open SQLite"),
                    ):
                        with TestClient(
                            local_app.app, base_url="http://127.0.0.1:8000"
                        ) as local_client:
                            self.assertEqual(
                                local_app.app.state.distributed_public_fetch.runtime_persistence_backend,
                                "POSTGRESQL",
                            )
                            self.assertIsInstance(
                                local_app.app.state.collection_service.store,
                                PostgresCollectionStore,
                            )
                            self.assertIsInstance(
                                local_app.app.state.ai_service.state_store,
                                PostgresAtomicStateStore,
                            )
                            self.assertEqual(
                                local_app.app.state.ai_service.config.data_profile,
                                "PUBLIC_DATA",
                            )
                            self.assertIsInstance(
                                local_app.app.state.postgres_analysis_storage,
                                PostgresAnalysisStorage,
                            )
                            self.assertIsInstance(
                                local_app.app.state.scheduler_store,
                                PostgresScheduleStore,
                            )
                            for table in ("keywords", "dataset_snapshots", "fetch_jobs"):
                                self.assertEqual(connection.execute(
                                    sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                                        namespace, sql.Identifier(table),
                                    )
                                ).fetchone()[0], 0)
                            latest = local_client.get("/api/v1/datasets/latest")
                            self.assertEqual(latest.status_code, 200, latest.text)
                            self.assertIsNone(latest.json()["item"])
                            keywords = local_client.get("/api/v1/datasets/latest/keywords")
                            self.assertEqual(keywords.status_code, 200, keywords.text)
                            self.assertEqual(keywords.json()["items"], [])
                            for path in (
                                "/api/v1/algorithms/results",
                                "/api/v1/analytics/overview",
                                "/api/v1/ai/capabilities",
                                "/api/v1/research/bootstrap",
                                "/api/v1/runs/history?limit=20",
                                "/api/v1/scheduler",
                            ):
                                read_response = local_client.get(path)
                                self.assertEqual(
                                    read_response.status_code, 200,
                                    f"{path}: {read_response.text}",
                                )
                            self.assertEqual(
                                local_client.get("/api/v1/collections").json()["items"],
                                [],
                            )
                            blocked_fetch = local_client.post(
                                "/api/v1/fetches",
                                json={"sources": [GOOGLE_SOURCE_ID]},
                                headers={
                                    "Origin": "http://127.0.0.1:8000",
                                    "Idempotency-Key": "db04-unknown-quota-blocked-001",
                                },
                            )
                            self.assertEqual(blocked_fetch.status_code, 503)
                            self.assertEqual(
                                blocked_fetch.json()["detail"]["code"], "QUOTA_UNKNOWN"
                            )
                            first = local_client.post(
                                "/api/v1/collections",
                                json={"name": "Local PostgreSQL write"},
                                headers={"Origin": "http://127.0.0.1:8000"},
                            )
                            self.assertEqual(first.status_code, 201, first.text)
                            local_store = local_app.app.state.collection_service.store

                vercel_settings = Settings.from_environment({
                    "VERCEL": "1",
                    "TREND_SCOUT_RUN_MODE": "live",
                    "TREND_SCOUT_POSTGRES_RUNTIME_ENABLED": "true",
                    "DATABASE_URL": url,
                    "DATABASE_URL_UNPOOLED": url,
                    "WIKIMEDIA_USER_AGENT": "TrendScoutTest/1.0 (https://example.invalid)",
                })
                with patch.dict(os.environ, {"VERCEL": "1", **safe_provider_environment}, clear=False):
                    vercel_app = create_app(
                        settings=vercel_settings,
                        vercel_postgres_environment=environment,
                        vercel_postgres_schema=schema,
                    )
                    vercel_profiles = []
                    original_adapter_init = OpenRouterAdapter.__init__

                    def capture_vercel_profile(adapter, api_key, *, client=None,
                                               data_profile="STRICT_PRIVATE"):
                        vercel_profiles.append(data_profile)
                        original_adapter_init(
                            adapter, api_key, client=client, data_profile=data_profile,
                        )

                    base_url = "https://trend-scout-mu.vercel.app"
                    with patch.object(OpenRouterAdapter, "__init__", capture_vercel_profile):
                        with TestClient(vercel_app, base_url=base_url) as vercel_client:
                            names = [item["name"] for item in vercel_client.get(
                                "/api/v1/collections"
                            ).json()["items"]]
                            self.assertIn("Local PostgreSQL write", names)
                            second = vercel_client.post(
                                "/api/v1/collections",
                                json={"name": "Vercel PostgreSQL write"},
                                headers={"Origin": base_url},
                            )
                            self.assertEqual(second.status_code, 201, second.text)
                    self.assertTrue(vercel_profiles)
                    self.assertEqual(set(vercel_profiles), {"PUBLIC_DATA"})
                self.assertIn(
                    "Vercel PostgreSQL write",
                    [item["name"] for item in local_store.list_collections()],
                )
                with patch.dict(os.environ, safe_provider_environment, clear=False):
                    with TestClient(
                        local_app.app, base_url="http://127.0.0.1:8000"
                    ) as local_client:
                        local_names = [item["name"] for item in local_client.get(
                            "/api/v1/collections"
                        ).json()["items"]]
                self.assertIn("Vercel PostgreSQL write", local_names)
            finally:
                if schema_created:
                    connection.execute(
                        sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(namespace)
                    )

    async def test_unknown_serpapi_cycle_is_reconciled_by_account_api_without_search(self):
        url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
        if not url:
            self.skipTest("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
        environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
        schema = "ts_test_" + uuid.uuid4().hex
        created = False
        account_requests = []

        async def account_handler(request):
            account_requests.append(request.url.path)
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={
                    "account_status": "Active",
                    "searches_per_month": 250,
                    "plan_searches_left": 208,
                    "total_searches_left": 208,
                    "this_month_usage": 42,
                    "this_hour_searches": 0,
                    "account_rate_limit_per_hour": 250,
                    "plan_renewal_date": "2026-12-11",
                },
            )

        account_client = SerpApiAccountClient.offline_fixture(
            "offline-account-key", transport=httpx.MockTransport(account_handler),
            clock=lambda: datetime(2026, 11, 26, 8, 5, tzinfo=timezone.utc),
        )

        async def fake_controlled_network(_cls, _key, **_kwargs):
            return account_client

        with postgres_connection(environment, direct=True) as connection:
            connection.autocommit = True
            try:
                apply_migrations(connection, schema=schema)
                created = True
                with patch.object(
                    SerpApiAccountClient,
                    "controlled_network",
                    classmethod(fake_controlled_network),
                ):
                    async with normal_google_live_composition(
                        user_agent="TrendScoutTest/1.0 (https://example.invalid)",
                        google_settings=GoogleLiveSettings(account_api_approved=True),
                        credential_provider=type(
                            "OfflineCredentials", (),
                            {"resolve": lambda self, reference:
                             "offline-serpapi-key" if reference == "SERPAPI_API_KEY" else None},
                        )(),
                        postgres_environment=environment,
                        postgres_schema=schema,
                        utcnow=lambda: datetime(2026, 11, 26, 8, 5, tzinfo=timezone.utc),
                    ) as facade:
                        state = facade.admission.store.read_many(
                            [GOOGLE_STATE_KEY]
                        )[GOOGLE_STATE_KEY]
                        self.assertEqual(facade.google_account_quota_state["status"], "AVAILABLE")
                        self.assertEqual(
                            state["quota"]["serpapi:cycle:2026-12-11"],
                            {"requests": 42, "cost": 42},
                        )
                        self.assertEqual(
                            state["quota_verification"]["serpapi"]["monthly"]["status"],
                            "VERIFIED",
                        )
                        self.assertEqual(
                            state["quota_verification"]["serpapi"]["daily"]["status"],
                            "UNKNOWN",
                        )
                        self.assertFalse(facade.normal_bing_live)
                        self.assertFalse(facade.normal_youtube_live)
                await_account_close = account_client.aclose()
                if hasattr(await_account_close, "__await__"):
                    await await_account_close
                self.assertEqual(account_requests, ["/account.json"])
            finally:
                if created:
                    connection.execute(
                        sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                    )

    async def test_empty_postgres_fresh_start_serves_empty_reads_and_blocks_live_fetch(self):
        url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
        if not url:
            self.skipTest("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
        environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
        schema = "ts_test_" + uuid.uuid4().hex
        namespace = sql.Identifier(schema)
        schema_created = False
        source_id = SOURCE_CAPABILITIES[0].source_id
        source_configuration = {"operator_setting": "keep"}
        settings = Settings.from_environment({
            "VERCEL": "1",
            "TREND_SCOUT_RUN_MODE": "live",
            "TREND_SCOUT_POSTGRES_RUNTIME_ENABLED": "true",
            "DATABASE_URL": url,
            "DATABASE_URL_UNPOOLED": url,
            "TREND_SCOUT_SCHEDULER_ENABLED": "false",
            "WIKIMEDIA_USER_AGENT": "TrendScoutTest/1.0 (https://example.invalid)",
        })
        safe_provider_environment = {
            "VERCEL": "1",
            "SERPAPI_API_KEY": "",
            "YOUTUBE_API_KEY": "",
            "OPENROUTER_API_KEY": "",
            "TREND_SCOUT_GOOGLE_TRENDS_APPROVED": "false",
            "TREND_SCOUT_GOOGLE_RSS_APPROVED": "false",
            "TREND_SCOUT_GOOGLE_SERP_APPROVED": "false",
            "TREND_SCOUT_BING_SERP_APPROVED": "false",
            "TREND_SCOUT_SERPAPI_BING_POLICY_MIGRATION_APPROVED": "false",
            "TREND_SCOUT_SERPAPI_ACCOUNT_API_APPROVED": "false",
            "TREND_SCOUT_YOUTUBE_LIVE_APPROVED": "false",
            "TREND_SCOUT_SCHEDULER_ENABLED": "false",
        }

        with postgres_connection(environment, direct=True) as connection:
            connection.autocommit = True
            self.assertFalse(connection.execute(
                "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
            ).fetchone()[0])
            try:
                apply_migrations(connection, schema=schema)
                schema_created = True
                with patch.dict(os.environ, safe_provider_environment, clear=False):
                    application = create_app(
                        settings=settings,
                        vercel_postgres_environment=environment,
                        vercel_postgres_schema=schema,
                    )
                    connection.execute(
                        sql.SQL("UPDATE {}.sources SET configuration=%s::jsonb WHERE id=%s")
                        .format(namespace), (Jsonb(source_configuration), source_id),
                    )
                    # A second startup must preserve source configuration and avoid
                    # duplicating the idempotently initialized source catalog.
                    create_app(
                        settings=settings,
                        vercel_postgres_environment=environment,
                        vercel_postgres_schema=schema,
                    )
                    self.assertEqual(connection.execute(
                        sql.SQL("SELECT COUNT(*) FROM {}.sources").format(namespace)
                    ).fetchone()[0], len(SOURCE_CAPABILITIES))
                    self.assertEqual(connection.execute(
                        sql.SQL("SELECT configuration FROM {}.sources WHERE id=%s")
                        .format(namespace), (source_id,),
                    ).fetchone()[0], source_configuration)

                    base_url = "https://trend-scout-mu.vercel.app"
                    with TestClient(application, base_url=base_url) as client:
                        latest = client.get("/api/v1/datasets/latest")
                        self.assertEqual(latest.status_code, 200, latest.text)
                        self.assertIsNone(latest.json()["item"])

                        keywords = client.get("/api/v1/datasets/latest/keywords")
                        self.assertEqual(keywords.status_code, 200, keywords.text)
                        self.assertEqual(keywords.json()["items"], [])
                        self.assertEqual(keywords.json()["total"], 0)

                        collections = client.get("/api/v1/collections")
                        self.assertEqual(collections.status_code, 200, collections.text)
                        self.assertEqual(collections.json()["items"], [])

                        for path in (
                            "/api/v1/runs/history?limit=20",
                            "/api/v1/analytics/overview",
                            "/api/v1/algorithms/results",
                            "/api/v1/research/bootstrap",
                            "/api/v1/dashboard/bootstrap",
                            "/api/v1/scheduler",
                        ):
                            read_response = client.get(path)
                            self.assertEqual(
                                read_response.status_code, 200,
                                f"{path}: {read_response.text}",
                            )

                        capabilities = client.get("/api/v1/capabilities")
                        self.assertEqual(
                            capabilities.status_code, 200, capabilities.text
                        )
                        self.assertFalse(capabilities.json()["fetch_enabled"])
                        self.assertEqual(
                            capabilities.json()["fetch_block_reason"],
                            "QUOTA_UNKNOWN",
                        )

                        blocked_fetch = client.post(
                            "/api/v1/fetches",
                            json={"sources": [GOOGLE_SOURCE_ID]},
                            headers={
                                "Origin": base_url,
                                "Idempotency-Key": "db02-fresh-start-blocked-001",
                            },
                        )
                        self.assertEqual(blocked_fetch.status_code, 503)
                        self.assertEqual(
                            blocked_fetch.json()["detail"]["code"],
                            "QUOTA_UNKNOWN",
                        )

                for table in ("keywords", "dataset_snapshots", "fetch_jobs"):
                    self.assertEqual(
                        connection.execute(
                            sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                                namespace, sql.Identifier(table),
                            )
                        ).fetchone()[0],
                        0,
                        f"Fresh Start must leave {table} empty",
                    )
                state = PostgresAtomicStateStore(
                    environment, schema=schema
                ).read_many([GOOGLE_STATE_KEY])[GOOGLE_STATE_KEY]
                self.assertEqual(state["jobs"], {})
                self.assertEqual(state["quota"], {})
                self.assertEqual(state["starts"], [])
                self.assertTrue(all(
                    value["daily"]["status"] == "UNKNOWN"
                    and value["monthly"]["status"] == "UNKNOWN"
                    for value in state["quota_verification"].values()
                ))
            finally:
                if schema_created:
                    connection.execute(
                        sql.SQL("DROP SCHEMA {} CASCADE").format(namespace)
                    )

    async def test_explicit_postgres_composition_constructs_without_provider_requests(self):
        url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
        if not url:
            self.skipTest("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
        environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
        schema = "ts_test_" + uuid.uuid4().hex
        clock = [datetime.now(timezone.utc)]
        created = False
        with postgres_connection(environment, direct=True) as connection:
            connection.autocommit = True
            self.assertFalse(connection.execute(
                "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
            ).fetchone()[0])
            try:
                apply_migrations(connection, schema=schema)
                created = True
                _seed_cutover_readiness(
                    connection, environment, schema, now=clock[0]
                )
                async with normal_google_live_composition(
                    user_agent="TrendScoutTest/1.0 (https://example.invalid)",
                    google_settings=GoogleLiveSettings(),
                    credential_provider=_NeverReadCredentials(),
                    postgres_environment=environment,
                    postgres_schema=schema,
                    utcnow=lambda: clock[0],
                ) as facade:
                    self.assertTrue(facade.normal_google_live)
                    self.assertTrue(facade.postgres_live)
                    self.assertEqual(facade.data_mode, "LIVE")
                    self.assertIs(type(facade.admission.store), PostgresAtomicStateStore)
                    self.assertIs(type(facade.collection_store), PostgresCollectionStore)
                    self.assertEqual(facade.postgres_schema, schema)
                    self.assertFalse(facade.google_capabilities["local_only"])

                    source_ids = sorted(facade.admission.policy.enabled_sources)
                    handler_calls = []

                    def cached_handler(context):
                        handler_calls.append(context.source_id)
                        return {
                            "status": "SUCCESS",
                            "data_mode": "LIVE",
                            "delivery_state": "CACHED",
                            "provider_request_count": 0,
                            "record_count": 1,
                            "records": [{"keyword": f"cached-{context.source_id}"}],
                            "quality_flags": [],
                            "coverage": {
                                "provider_request_count": 0,
                                "refresh_provider_request_count": 0,
                            },
                        }

                    cached_handler.__trendscout_data_mode__ = "LIVE"
                    facade.worker.handlers = {
                        source_id: cached_handler for source_id in source_ids
                    }
                    facade.worker.source_modes = {
                        source_id: "LIVE" for source_id in source_ids
                    }
                    accepted = facade.submit(
                        "postgres-cached-continuation-0001", source_ids=source_ids,
                    )
                    outcome = await facade.run_slice(
                        "postgres-cache-test-worker", accepted["job_id"], max_sources=1,
                    )
                    self.assertEqual(outcome["state"], "COMPLETED")
                    self.assertEqual(handler_calls, list(facade.worker.source_order))
                    self.assertEqual(set(outcome["sources"]), set(source_ids))
                    self.assertTrue(all(
                        result["delivery_state"] == "CACHED"
                        and result["provider_request_count"] == 0
                        for result in outcome["sources"].values()
                    ))
                    terminal = facade.status(accepted["job_id"], include_records=True)
                    self.assertEqual(terminal["status"], "COMPLETED")
                    self.assertEqual(terminal["fetch_mode"], "LIVE")
                    self.assertEqual(
                        {source["source_id"] for source in terminal["sources"]},
                        set(source_ids),
                    )

                    live_handler_calls = list(handler_calls)
                    cache_only = facade.submit(
                        "postgres-cache-only-0001", source_ids=source_ids,
                        cache_only=True,
                    )
                    cache_only_outcome = await facade.run_slice(
                        "postgres-cache-only-worker", cache_only["job_id"],
                        max_sources=len(source_ids),
                    )
                    self.assertIn(cache_only_outcome["state"], {"COMPLETED", "PARTIAL"})
                    self.assertEqual(handler_calls, live_handler_calls)
                    cache_only_status = facade.status(
                        cache_only["job_id"], include_records=True,
                    )
                    self.assertEqual(cache_only_status["fetch_mode"], "CACHE_ONLY")
                    self.assertTrue(cache_only_status["terminal"])
                    self.assertTrue(all(
                        (source.get("provider_request_count") or 0) == 0
                        for source in cache_only_status["sources"]
                    ))

                    class SimulatedWorkerInterruption(BaseException):
                        pass

                    handler_calls = []
                    provider_attempts = []
                    provider_result = {
                        "status": "SUCCESS",
                        "data_mode": "LIVE",
                        "delivery_state": "FRESH",
                        "provider_request_count": 1,
                        "record_count": 1,
                        "records": [{"keyword": "resumed provider result"}],
                        "quality_flags": [],
                        "coverage": {"provider_request_count": 1},
                    }

                    def interrupted_handler(context):
                        handler_calls.append(context.resume_snapshot is not None)
                        if context.resume_snapshot is None:
                            provider_attempts.append(context.source_id)
                            context.checkpoint({"provider_result": provider_result})
                            raise SimulatedWorkerInterruption()
                        return context.resume_snapshot["progress"]["provider_result"]

                    facade.worker.handlers[GOOGLE_SOURCE_ID] = interrupted_handler
                    recovery_job = facade.submit(
                        "postgres-worker-recovery-0001",
                        source_ids=[GOOGLE_SOURCE_ID],
                    )
                    with self.assertRaises(SimulatedWorkerInterruption):
                        await facade.run_slice(
                            "postgres-interrupted-worker", recovery_job["job_id"],
                            max_sources=1,
                        )
                    running = facade.status(recovery_job["job_id"])
                    self.assertEqual(running["status"], "RUNNING")
                    replay = facade.submit(
                        "postgres-worker-recovery-0001",
                        source_ids=[GOOGLE_SOURCE_ID],
                    )
                    self.assertEqual(replay["job_id"], recovery_job["job_id"])
                    self.assertEqual(replay["result"], "REPLAY")
                    with self.assertRaisesRegex(AdmissionDenied, "LEASE_BUSY"):
                        await facade.run_slice(
                            "postgres-duplicate-worker", recovery_job["job_id"],
                            max_sources=1,
                        )
                    self.assertEqual(provider_attempts, [GOOGLE_SOURCE_ID])
                    clock[0] += timedelta(seconds=facade.admission.policy.lease_seconds + 1)
                    recovered = await facade.run_slice(
                        "postgres-recovered-worker", recovery_job["job_id"],
                        max_sources=1,
                    )
                    self.assertEqual(recovered["state"], "COMPLETED")
                    self.assertEqual(handler_calls, [False, True])
                    self.assertEqual(provider_attempts, [GOOGLE_SOURCE_ID])
                    recovered_status = facade.status(
                        recovery_job["job_id"], include_records=True,
                    )
                    self.assertEqual(recovered_status["status"], "COMPLETED")
                    self.assertEqual(recovered_status["sources"][0]["records"],
                                     provider_result["records"])

                @asynccontextmanager
                async def runtime_context():
                    async with postgres_live_composition(
                        runtime_scope="LOCAL",
                        user_agent="TrendScoutTest/1.0 (https://example.invalid)",
                        google_settings=GoogleLiveSettings(),
                        credential_provider=_NeverReadCredentials(),
                        postgres_environment=environment,
                        postgres_schema=schema,
                        utcnow=lambda: clock[0],
                    ) as facade:
                        yield facade

                local_settings_environment = {
                    "TREND_SCOUT_ENVIRONMENT": "local",
                    "TREND_SCOUT_RUN_MODE": "live",
                    "TREND_SCOUT_READ_ONLY": "false",
                    "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false",
                    "TREND_SCOUT_POSTGRES_RUNTIME_ENABLED": "true",
                    "DATABASE_URL": url,
                    "DATABASE_URL_UNPOOLED": url,
                    "WIKIMEDIA_USER_AGENT": "TrendScoutTest/1.0 (https://example.invalid)",
                }
                local_settings = Settings.from_environment(local_settings_environment)
                local_provider_environment = {
                    "SERPAPI_API_KEY": "",
                    "YOUTUBE_API_KEY": "",
                    "OPENROUTER_API_KEY": "",
                    "TREND_SCOUT_GOOGLE_TRENDS_APPROVED": "false",
                    "TREND_SCOUT_GOOGLE_RSS_APPROVED": "false",
                    "TREND_SCOUT_GOOGLE_SERP_APPROVED": "false",
                    "TREND_SCOUT_BING_SERP_APPROVED": "false",
                    "TREND_SCOUT_SERPAPI_BING_POLICY_MIGRATION_APPROVED": "false",
                    "TREND_SCOUT_SERPAPI_ACCOUNT_API_APPROVED": "false",
                    "TREND_SCOUT_YOUTUBE_LIVE_APPROVED": "false",
                    "TREND_SCOUT_SCHEDULER_ENABLED": "false",
                    "TREND_SCOUT_POSTGRES_SCHEMA": schema,
                }
                with patch.dict(os.environ, local_provider_environment, clear=False):
                    guarded_app = create_app(
                        settings=local_settings,
                        local_live_opt_in=True,
                        local_live_context_factory=runtime_context,
                        postgres_environment=environment,
                        postgres_schema=schema,
                    )
                    app = guarded_app.app
                    local_store = None
                    with patch(
                        "app.collections.store.CollectionStore.local",
                        side_effect=AssertionError("PostgreSQL runtime must not open SQLite"),
                    ), patch(
                        "app.orchestration.normal_google_live_composition."
                        "LocalSQLiteAtomicStateStore.for_local_live",
                        side_effect=AssertionError("PostgreSQL runtime must not open SQLite"),
                    ):
                        with TestClient(
                            app, base_url="http://127.0.0.1:8000"
                        ) as local_client:
                            self.assertIsInstance(
                                app.state.collection_service.store, PostgresCollectionStore
                            )
                            local_store = app.state.collection_service.store
                            self.assertEqual(
                                app.state.distributed_public_fetch.runtime_persistence_backend,
                                "POSTGRESQL",
                            )
                            self.assertEqual(
                                app.state.distributed_public_fetch.postgres_runtime_scope,
                                "LOCAL",
                            )
                            self.assertIsInstance(
                                app.state.postgres_analysis_storage, PostgresAnalysisStorage
                            )
                            self.assertIsInstance(app.state.scheduler_store, PostgresScheduleStore)
                            self.assertIsInstance(
                                app.state.ai_service.state_store, PostgresAtomicStateStore
                            )
                            local_created = local_client.post(
                                "/api/v1/collections",
                                json={"name": "Created by Local PostgreSQL runtime"},
                                headers={"Origin": "http://127.0.0.1:8000"},
                            )
                            self.assertEqual(local_created.status_code, 201, local_created.text)
                            request = SimpleNamespace(app=app, headers={})
                            self.assertIs(read_storage(request), app.state.postgres_analysis_storage)
                            self.assertIs(_history_storage(request), app.state.postgres_analysis_storage)

                google = GoogleLiveSettings()
                policy = normal_google_live_policy(
                    serpapi_daily_budget=google.serpapi_daily_budget,
                    serpapi_monthly_budget=google.serpapi_monthly_budget,
                    rss_daily_budget=google.rss_daily_budget,
                    rss_monthly_budget=google.rss_monthly_budget,
                    fetch_starts_per_hour=google.fetch_starts_per_hour,
                    bing_enabled=google.paid_bing_approved,
                    bing_daily_budget=google.bing_daily_budget,
                    youtube_enabled=True,
                )
                admission = PublicAdmission(
                    PostgresAtomicStateStore(environment, schema=schema),
                    policy,
                    state_key=GOOGLE_STATE_KEY,
                )
                admission._transaction(lambda _state: None)

                vercel_settings = Settings.from_environment({
                    "VERCEL": "1",
                    "TREND_SCOUT_RUN_MODE": "live",
                    "TREND_SCOUT_POSTGRES_RUNTIME_ENABLED": "true",
                    "DATABASE_URL": url,
                    "DATABASE_URL_UNPOOLED": url,
                    "WIKIMEDIA_USER_AGENT": "TrendScoutTest/1.0 (https://example.invalid)",
                })
                with patch.dict(os.environ, {
                    "VERCEL": "1",
                    "SERPAPI_API_KEY": "",
                    "YOUTUBE_API_KEY": "",
                    "OPENROUTER_API_KEY": "",
                    "TREND_SCOUT_GOOGLE_TRENDS_APPROVED": "false",
                    "TREND_SCOUT_GOOGLE_RSS_APPROVED": "false",
                    "TREND_SCOUT_GOOGLE_SERP_APPROVED": "false",
                    "TREND_SCOUT_BING_SERP_APPROVED": "false",
                    "TREND_SCOUT_SERPAPI_BING_POLICY_MIGRATION_APPROVED": "false",
                    "TREND_SCOUT_SERPAPI_ACCOUNT_API_APPROVED": "false",
                    "TREND_SCOUT_YOUTUBE_LIVE_APPROVED": "false",
                    "TREND_SCOUT_SCHEDULER_ENABLED": "false",
                }, clear=False):
                    vercel_app = create_app(
                        settings=vercel_settings,
                        vercel_postgres_environment=environment,
                        vercel_postgres_schema=schema,
                    )
                    with TestClient(
                        vercel_app, base_url="https://trend-scout-mu.vercel.app"
                    ) as client:
                        response = client.get("/api/v1/collections")
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.headers.get("cache-control"), "no-store")
                        self.assertIn(
                            "Created by Local PostgreSQL runtime",
                            [item["name"] for item in response.json()["items"]],
                        )
                        for path in (
                            "/api/v1/datasets/latest",
                            "/api/v1/datasets/latest/keywords",
                            "/api/v1/runs/history?limit=20",
                            "/api/v1/analytics/overview",
                            "/api/v1/algorithms/results",
                            "/api/v1/research/bootstrap",
                            "/api/v1/dashboard/bootstrap",
                            "/api/v1/scheduler",
                        ):
                            read_response = client.get(path)
                            self.assertEqual(
                                read_response.status_code,
                                200,
                                f"{path}: {read_response.text}",
                            )
                        created = client.post(
                            "/api/v1/collections",
                            json={"name": "Created by Vercel PostgreSQL runtime"},
                            headers={"Origin": "https://trend-scout-mu.vercel.app"},
                        )
                        self.assertEqual(created.status_code, 201, created.text)
                self.assertIn(
                    "Created by Vercel PostgreSQL runtime",
                    [item["name"] for item in local_store.list_collections()],
                )
            finally:
                if created:
                    connection.execute(
                        sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                    )


if __name__ == "__main__":
    unittest.main()
