"""Vercel Wikimedia Fetch gate and request-scoped worker lifetime contracts."""

import json
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI
from starlette.requests import Request

from app.api.capabilities import _read_capability_metadata
from app.api.datasets import latest_dataset
from app.collections.service import CollectionService
from app.collections.store import CollectionStore
from app.jobs.public_admission import AdmissionDenied
from app.main import (
    VercelSharedLiveMiddleware,
    _is_vercel_shared_live_request,
    _persists_vercel_collections,
    _vercel_collection_read_composition_required,
    _vercel_collection_snapshot_required,
    _vercel_fetch_collection_recovery_read_context_required,
    _vercel_fetch_read_composition_required,
    _vercel_fetch_submission_read_context_required,
    _vercel_timing_route,
    create_app,
)
from app.settings import Settings
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)

_AUDIENCE = (
    "//iam.googleapis.com/projects/556422620769/locations/global/"
    "workloadIdentityPools/trendscout-vercel/providers/vercel-production"
)
_PROJECT = "trendscout-192e4"
_JOB_ID = "a" * 32


class VercelCollectionSnapshotPolicyTests(unittest.TestCase):
    def test_fetch_status_and_history_reads_use_the_provider_free_context(self):
        for path in (
            f"/api/v1/fetches/{_JOB_ID}",
            "/api/v1/runs/history",
            f"/api/v1/runs/history/{_JOB_ID}",
        ):
            with self.subTest(path=path):
                self.assertTrue(_vercel_fetch_read_composition_required("GET", path))
                self.assertFalse(_vercel_fetch_read_composition_required("POST", path))
        for method, path in (
            ("GET", "/api/v1/fetches/not-a-fetch-id"),
            ("GET", f"/api/v1/fetches/{_JOB_ID}/collections"),
            ("POST", "/api/v1/fetches"),
            ("GET", "/api/v1/capabilities"),
        ):
            with self.subTest(method=method, path=path):
                self.assertFalse(_vercel_fetch_read_composition_required(method, path))
        self.assertTrue(_vercel_fetch_collection_recovery_read_context_required(
            "POST", f"/api/v1/fetches/{_JOB_ID}/collections",
        ))
        self.assertFalse(_vercel_fetch_collection_recovery_read_context_required(
            "GET", f"/api/v1/fetches/{_JOB_ID}/collections",
        ))

    def test_initial_fetch_submission_uses_provider_free_admission_context(self):
        self.assertTrue(_vercel_fetch_submission_read_context_required(
            "POST", "/api/v1/fetches", [(b"idempotency-key", b"test")],
        ))
        self.assertFalse(_vercel_fetch_submission_read_context_required(
            "POST", "/api/v1/fetches",
            [(b"x-trendscout-fetch-continuation", b"1")],
        ))
        self.assertFalse(_vercel_fetch_submission_read_context_required(
            "POST", "/api/v1/fetches/history", [],
        ))

    def test_only_collection_data_routes_restore_the_sqlite_snapshot(self):
        for method, path in (
            ("GET", "/api/v1/runtime"),
            ("GET", "/api/v1/analytics/overview"),
            ("GET", "/api/v1/algorithms/results"),
            ("GET", "/api/v1/research"),
            ("GET", "/api/v1/collections"),
            ("GET", "/api/v1/collections/inbox"),
            ("GET", "/api/v1/datasets/latest"),
            ("GET", "/api/v1/datasets/latest/keywords"),
            ("POST", "/api/v1/collections"),
            ("PATCH", "/api/v1/research/record-1"),
            ("POST", f"/api/v1/fetches/{_JOB_ID}/collections"),
        ):
            with self.subTest(method=method, path=path):
                self.assertTrue(_vercel_collection_snapshot_required(method, path))

    def test_read_only_collection_routes_use_the_lightweight_context(self):
        for path in (
            "/api/v1/runtime",
            "/api/v1/analytics/overview",
            "/api/v1/algorithms/results",
            "/api/v1/research",
            "/api/v1/collections",
            "/api/v1/collections/jobs/" + _JOB_ID,
            "/api/v1/datasets/latest",
            "/api/v1/datasets/latest/keywords",
        ):
            with self.subTest(path=path):
                self.assertTrue(_vercel_collection_read_composition_required("GET", path))
        for method, path in (
            ("POST", "/api/v1/collections"),
            ("POST", "/api/v1/fetches"),
            ("GET", "/api/v1/capabilities"),
            ("GET", "/api/v1/runs/history"),
            ("GET", "/api/v1/fetches/" + _JOB_ID),
        ):
            with self.subTest(method=method, path=path):
                self.assertFalse(_vercel_collection_read_composition_required(method, path))

    def test_capabilities_history_and_fetch_status_skip_the_sqlite_snapshot(self):
        for method, path in (
            ("GET", "/api/v1/capabilities"),
            ("GET", "/api/v1/runs/history"),
            ("GET", f"/api/v1/runs/history/{_JOB_ID}"),
            ("GET", f"/api/v1/fetches/{_JOB_ID}"),
            ("POST", "/api/v1/fetches"),
        ):
            with self.subTest(method=method, path=path):
                self.assertFalse(_vercel_collection_snapshot_required(method, path))

    def test_latest_dataset_uses_available_summary_without_constructing_service(self):
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/api/v1/datasets/latest",
            "headers": [],
            "state": {
                "latest_dataset_summary_available": True,
                "latest_dataset_summary": {
                    "dataset_id": "cached-dataset",
                    "status": "PARTIAL",
                    "sources": [WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID,
                                BING_SOURCE_ID, YOUTUBE_SOURCE_ID],
                },
            },
        })

        with patch("app.api.datasets._service",
                   side_effect=AssertionError("summary path must not build a service")):
            response = latest_dataset(request)

        self.assertEqual(response["item"]["dataset_id"], "cached-dataset")
        self.assertEqual(response["item"]["status"], "PARTIAL")
        self.assertEqual(response["item"]["restoration_status"], "CACHED")
        self.assertIsNone(response["item"]["age_seconds"])


class VercelCollectionReadCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_capability_metadata_context_skips_unused_latest_dataset_read(self):
        from app.orchestration.vercel_wikimedia_live_composition import (
            vercel_shared_live_composition,
        )

        events = []

        class Client:
            def collection(self, _name):
                return object()

            def transaction(self, **_kwargs):
                return object()

        class Snapshot:
            def __init__(self, *_args, **_kwargs):
                pass

            def read_latest_dataset_summary(self):
                events.append("dataset_summary_read")
                return True, {"dataset_id": _JOB_ID}

        @asynccontextmanager
        async def metadata_only_normal_runtime(**_kwargs):
            yield SimpleNamespace(
                google_capabilities={}, youtube_capabilities={},
                normal_youtube_live=False,
            )

        with patch.dict("os.environ", {"VERCEL": "1", "VERCEL_ENV": "production"}), patch(
            "app.orchestration.vercel_wikimedia_live_composition._client_project_id",
            return_value=_PROJECT,
        ), patch(
            "app.collections.firestore_snapshot.FirestoreCollectionSnapshot", Snapshot,
        ), patch(
            "app.orchestration.normal_google_live_composition.normal_google_live_composition",
            new=metadata_only_normal_runtime,
        ):
            async with vercel_shared_live_composition(
                Client(), user_agent="TrendScoutTest/0.1",
                defer_collection_snapshot=True,
                capability_metadata_request=True,
            ) as context:
                self.assertFalse(context.latest_dataset_summary_available)
                self.assertIsNone(context.latest_dataset_summary)

        self.assertEqual(events, [])

    async def test_collections_projection_skips_sqlite_restore(self):
        from app.orchestration.vercel_collection_read_composition import (
            vercel_collection_read_composition,
        )

        projection = {
            "items": [{"collection_id": "c1", "name": "Home"}],
            "suggestions": [],
            "mode": "AUTO_CREATE",
            "summary": {"collections": 1},
            "keyword_page": {"items": [], "total": 0, "limit": 25, "offset": 0},
        }

        class Snapshot:
            def __init__(self, *_args, **_kwargs):
                pass

            def read_dashboard_bootstrap(self):
                return True, {
                    "item": None,
                    "analytics": {},
                    "keywords": {"items": []},
                    "collections": projection,
                }

            def load(self):
                raise AssertionError("current Collections projection must avoid SQLite restore")

        with patch.dict("os.environ", {"VERCEL": "1"}), patch(
            "app.orchestration.firestore_shared_live_composition._client_project_id",
            return_value="test-project",
        ), patch(
            "app.collections.firestore_snapshot.FirestoreCollectionSnapshot", Snapshot,
        ):
            async with vercel_collection_read_composition(
                object(), user_agent="TrendScoutTest/0.1",
                collection_workbench_only=True,
            ) as context:
                self.assertIsNone(context.collection_service)
                self.assertTrue(context.collection_workbench_available)
                self.assertEqual(context.collection_workbench, projection)

    async def test_dashboard_model_without_collections_falls_back_for_workbench(self):
        from app.orchestration.vercel_collection_read_composition import (
            vercel_collection_read_composition,
        )

        events = []
        replacements = []

        class Snapshot:
            def __init__(self, *_args, **_kwargs):
                pass

            def read_dashboard_bootstrap(self):
                return True, {"item": None, "analytics": {}, "keywords": {"items": []}}

            def load(self):
                events.append("snapshot_loaded")

            def backfill_dashboard_bootstrap(self, _value, **kwargs):
                replacements.append(kwargs.get("replace_existing", False))
                events.append("dashboard_backfilled")

        class Store:
            def __init__(self, *_args, **_kwargs):
                pass

        class Service:
            def __init__(self, store, *, fetch_model_inference_enabled):
                self.store = store
                self.fetch_model_inference_enabled = fetch_model_inference_enabled

        with patch.dict("os.environ", {"VERCEL": "1"}), patch(
            "app.orchestration.firestore_shared_live_composition._client_project_id",
            return_value="test-project",
        ), patch(
            "app.collections.firestore_snapshot.FirestoreCollectionSnapshot", Snapshot,
        ), patch("app.collections.store.CollectionStore", Store), patch(
            "app.collections.service.CollectionService", Service,
        ), patch("app.api.dashboard.build_default_dashboard_bootstrap", return_value={
            "item": None, "analytics": {}, "keywords": {"items": []},
            "collections": {"items": [], "suggestions": [], "mode": "AUTO_CREATE", "summary": {},
                             "keyword_page": {"items": [], "total": 0, "limit": 25, "offset": 0}},
        }):
            async with vercel_collection_read_composition(
                object(), user_agent="TrendScoutTest/0.1",
                collection_workbench_only=True,
            ) as context:
                self.assertIsInstance(context.collection_service, Service)
                self.assertTrue(context.collection_workbench_available)
                self.assertEqual(context.collection_workbench["items"], [])
        self.assertEqual(events, ["snapshot_loaded", "dashboard_backfilled"])
        self.assertEqual(replacements, [True])

    async def test_versioned_dashboard_model_skips_sqlite_restore(self):
        from app.orchestration.vercel_collection_read_composition import (
            vercel_collection_read_composition,
        )

        events = []
        projection = {
            "item": {"dataset_id": "cached-dataset"},
            "analytics": {"keyword_totals": {"unique": 1}},
            "keywords": {"items": [], "total": 1},
        }

        class Snapshot:
            def __init__(self, *_args, **_kwargs):
                events.append("snapshot_created")

            def read_dashboard_bootstrap(self):
                events.append("dashboard_read")
                return True, projection

            def load(self):
                raise AssertionError("a current dashboard model must avoid SQLite restore")

        with patch.dict("os.environ", {"VERCEL": "1"}), patch(
            "app.orchestration.firestore_shared_live_composition._client_project_id",
            return_value="test-project",
        ), patch(
            "app.collections.firestore_snapshot.FirestoreCollectionSnapshot", Snapshot,
        ):
            async with vercel_collection_read_composition(
                object(), user_agent="TrendScoutTest/0.1",
                dashboard_bootstrap_only=True,
            ) as context:
                self.assertIsNone(context.collection_service)
                self.assertTrue(context.dashboard_bootstrap_available)
                self.assertEqual(context.dashboard_bootstrap, projection)

        self.assertEqual(events, ["snapshot_created", "dashboard_read"])

    async def test_analytics_projection_skips_sqlite_restore(self):
        from app.orchestration.vercel_collection_read_composition import (
            vercel_collection_read_composition,
        )

        analytics = {"keyword_totals": {"unique": 42}}

        class Snapshot:
            def __init__(self, *_args, **_kwargs):
                pass

            def read_dashboard_bootstrap(self):
                return True, {
                    "item": None,
                    "analytics": analytics,
                    "keywords": {"items": []},
                }

            def load(self):
                raise AssertionError("current analytics projection must avoid SQLite restore")

        with patch.dict("os.environ", {"VERCEL": "1"}), patch(
            "app.orchestration.firestore_shared_live_composition._client_project_id",
            return_value="test-project",
        ), patch(
            "app.collections.firestore_snapshot.FirestoreCollectionSnapshot", Snapshot,
        ):
            async with vercel_collection_read_composition(
                object(), user_agent="TrendScoutTest/0.1",
                analytics_overview_only=True,
            ) as context:
                self.assertIsNone(context.collection_service)
                self.assertTrue(context.analytics_overview_available)
                self.assertEqual(context.analytics_overview, analytics)

    async def test_algorithm_results_projection_skips_sqlite_restore(self):
        from app.orchestration.vercel_collection_read_composition import (
            vercel_collection_read_composition,
        )

        results = {
            "summary": [{"algorithm_id": "observed-volume-rank", "completed": 1}],
            "items": [], "limit": 100,
        }

        class Snapshot:
            def __init__(self, *_args, **_kwargs):
                pass

            def read_dashboard_bootstrap(self):
                return True, {
                    "item": None, "analytics": {}, "keywords": {"items": []},
                    "algorithm_results": results,
                }

            def load(self):
                raise AssertionError("current algorithm results must avoid SQLite restore")

        with patch.dict("os.environ", {"VERCEL": "1"}), patch(
            "app.orchestration.firestore_shared_live_composition._client_project_id",
            return_value="test-project",
        ), patch(
            "app.collections.firestore_snapshot.FirestoreCollectionSnapshot", Snapshot,
        ):
            async with vercel_collection_read_composition(
                object(), user_agent="TrendScoutTest/0.1",
                algorithm_results_only=True,
            ) as context:
                self.assertIsNone(context.collection_service)
                self.assertTrue(context.algorithm_results_available)
                self.assertEqual(context.algorithm_results, results)

    async def test_research_projection_skips_sqlite_restore(self):
        from app.orchestration.vercel_collection_read_composition import (
            vercel_collection_read_composition,
        )

        research = {
            "keywords": {"items": [{"id": "kw-1"}], "total": 1,
                          "limit": 100, "offset": 0},
            "research": {"items": [{"id": "research-1"}], "total": 1,
                           "limit": 50, "offset": 0, "next_offset": None},
        }

        class Snapshot:
            def __init__(self, *_args, **_kwargs):
                pass

            def read_dashboard_bootstrap(self):
                return True, {
                    "item": None, "analytics": {}, "keywords": {"items": []},
                    "research": research,
                }

            def load(self):
                raise AssertionError("current Research projection must avoid SQLite restore")

        with patch.dict("os.environ", {"VERCEL": "1"}), patch(
            "app.orchestration.firestore_shared_live_composition._client_project_id",
            return_value="test-project",
        ), patch(
            "app.collections.firestore_snapshot.FirestoreCollectionSnapshot", Snapshot,
        ):
            async with vercel_collection_read_composition(
                object(), user_agent="TrendScoutTest/0.1",
                research_bootstrap_only=True,
            ) as context:
                self.assertIsNone(context.collection_service)
                self.assertTrue(context.research_bootstrap_available)
                self.assertEqual(context.research_bootstrap, research)

    async def test_missing_research_projection_falls_back_when_collections_model_exists(self):
        from app.orchestration.vercel_collection_read_composition import (
            vercel_collection_read_composition,
        )

        events = []
        collection_model = {
            "items": [], "suggestions": [], "mode": "AUTO_CREATE", "summary": {},
            "keyword_page": {"items": [], "total": 0, "limit": 25, "offset": 0},
        }
        rebuilt = {
            "item": None, "analytics": {}, "keywords": {"items": []},
            "collections": collection_model,
            "research": {
                "keywords": {"items": [], "total": 0, "limit": 100, "offset": 0},
                "research": {
                    "items": [], "total": 0, "limit": 50, "offset": 0,
                    "next_offset": None,
                },
            },
        }

        class Snapshot:
            def __init__(self, *_args, **_kwargs):
                pass

            def read_dashboard_bootstrap(self):
                return True, {
                    "item": None, "analytics": {}, "keywords": {"items": []},
                    "collections": collection_model,
                }

            def load(self):
                events.append("snapshot_loaded")

            def backfill_dashboard_bootstrap(self, _value, *, replace_existing=False):
                events.append(("backfilled", replace_existing))

        class Store:
            def __init__(self, *_args, **_kwargs):
                pass

        class Service:
            def __init__(self, store, *, fetch_model_inference_enabled):
                self.store = store
                self.fetch_model_inference_enabled = fetch_model_inference_enabled

        with patch.dict("os.environ", {"VERCEL": "1"}), patch(
            "app.orchestration.firestore_shared_live_composition._client_project_id",
            return_value="test-project",
        ), patch(
            "app.collections.firestore_snapshot.FirestoreCollectionSnapshot", Snapshot,
        ), patch("app.collections.store.CollectionStore", Store), patch(
            "app.collections.service.CollectionService", Service,
        ), patch(
            "app.api.dashboard.build_default_dashboard_bootstrap", return_value=rebuilt,
        ):
            async with vercel_collection_read_composition(
                object(), user_agent="TrendScoutTest/0.1",
                research_bootstrap_only=True,
            ) as context:
                self.assertIsInstance(context.collection_service, Service)
                self.assertTrue(context.research_bootstrap_available)
                self.assertEqual(context.research_bootstrap, rebuilt["research"])

        self.assertEqual(events, ["snapshot_loaded", ("backfilled", True)])

    async def test_missing_dashboard_model_restores_once_then_backfills(self):
        from app.orchestration.vercel_collection_read_composition import (
            vercel_collection_read_composition,
        )

        events = []
        projection = {
            "item": None,
            "analytics": {"keyword_totals": {"unique": 0}},
            "keywords": {"items": [], "total": 0},
        }

        class Snapshot:
            def __init__(self, *_args, **_kwargs):
                events.append("snapshot_created")

            def read_dashboard_bootstrap(self):
                events.append("dashboard_read")
                return False, None

            def load(self):
                events.append("snapshot_loaded")

            def backfill_dashboard_bootstrap(self, value, **_kwargs):
                self.assert_projection = value
                events.append("dashboard_backfilled")

        class Store:
            def __init__(self, *_args, **_kwargs):
                events.append("store_created")

        class Service:
            def __init__(self, store, *, fetch_model_inference_enabled):
                self.store = store
                self.fetch_model_inference_enabled = fetch_model_inference_enabled
                events.append("service_created")

        with patch.dict("os.environ", {"VERCEL": "1"}), patch(
            "app.orchestration.firestore_shared_live_composition._client_project_id",
            return_value="test-project",
        ), patch(
            "app.collections.firestore_snapshot.FirestoreCollectionSnapshot", Snapshot,
        ), patch("app.collections.store.CollectionStore", Store), patch(
            "app.collections.service.CollectionService", Service,
        ), patch(
            "app.api.dashboard.build_default_dashboard_bootstrap", return_value=projection,
        ) as build_projection:
            async with vercel_collection_read_composition(
                object(), user_agent="TrendScoutTest/0.1",
                dashboard_bootstrap_only=True,
            ) as context:
                self.assertIsInstance(context.collection_service, Service)
                self.assertTrue(context.dashboard_bootstrap_available)
                self.assertEqual(context.dashboard_bootstrap, projection)
            build_projection.assert_called_once()

        self.assertEqual(events, [
            "snapshot_created", "dashboard_read", "snapshot_loaded",
            "store_created", "service_created", "dashboard_backfilled",
        ])

    async def test_valid_summary_skips_sqlite_restore(self):
        from app.orchestration.vercel_collection_read_composition import (
            vercel_collection_read_composition,
        )

        events = []
        summary = {"dataset_id": "cached-dataset", "status": "PARTIAL"}

        class Snapshot:
            def __init__(self, *_args, **_kwargs):
                events.append("snapshot_created")

            def read_latest_dataset_summary(self):
                events.append("summary_read")
                return True, summary

            def load(self):
                events.append("snapshot_loaded")
                raise AssertionError("valid summary must avoid SQLite restore")

        with patch.dict("os.environ", {"VERCEL": "1"}), patch(
            "app.orchestration.firestore_shared_live_composition._client_project_id",
            return_value="test-project",
        ), patch(
            "app.collections.firestore_snapshot.FirestoreCollectionSnapshot", Snapshot,
        ):
            async with vercel_collection_read_composition(
                object(), user_agent="TrendScoutTest/0.1",
                latest_dataset_summary_only=True,
            ) as context:
                self.assertIsNone(context.collection_service)
                self.assertTrue(context.latest_dataset_summary_available)
                self.assertEqual(context.latest_dataset_summary, summary)
                self.assertEqual(context.available_sources, frozenset({
                    BING_SOURCE_ID, GOOGLE_SOURCE_ID,
                    WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID,
                }))

        self.assertEqual(events, ["snapshot_created", "summary_read"])

    async def test_missing_summary_falls_back_to_snapshot_restore(self):
        from app.orchestration.vercel_collection_read_composition import (
            vercel_collection_read_composition,
        )

        events = []

        class Snapshot:
            def __init__(self, *_args, **_kwargs):
                events.append("snapshot_created")

            def read_latest_dataset_summary(self):
                events.append("summary_read")
                return False, None

            def load(self):
                events.append("snapshot_loaded")
                return 1

        class Store:
            def __init__(self, *_args, **_kwargs):
                events.append("store_created")

        class Service:
            def __init__(self, store, *, fetch_model_inference_enabled):
                self.store = store
                self.fetch_model_inference_enabled = fetch_model_inference_enabled
                events.append("service_created")

        with patch.dict("os.environ", {"VERCEL": "1"}), patch(
            "app.orchestration.firestore_shared_live_composition._client_project_id",
            return_value="test-project",
        ), patch(
            "app.collections.firestore_snapshot.FirestoreCollectionSnapshot", Snapshot,
        ), patch("app.collections.store.CollectionStore", Store), patch(
            "app.collections.service.CollectionService", Service,
        ):
            async with vercel_collection_read_composition(
                object(), user_agent="TrendScoutTest/0.1",
                latest_dataset_summary_only=True,
            ) as context:
                self.assertIsInstance(context.collection_service, Service)
                self.assertEqual(context.collection_service.store.__class__, Store)
                self.assertFalse(context.collection_service.fetch_model_inference_enabled)
                self.assertFalse(getattr(context, "latest_dataset_summary_available", False))
                self.assertIsNone(getattr(context, "latest_dataset_summary", None))

        self.assertEqual(events, [
            "snapshot_created", "summary_read", "snapshot_loaded",
            "store_created", "service_created",
        ])


class VercelFetchReadCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_run_history_reads_projection_without_restoring_sqlite(self):
        from app.orchestration.vercel_fetch_read_composition import (
            vercel_fetch_read_composition,
        )

        sources = frozenset({
            BING_SOURCE_ID, GOOGLE_SOURCE_ID, WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID,
        })
        policy = SimpleNamespace(enabled_sources=sources, receipt_seconds=30)
        google = SimpleNamespace(
            serpapi_daily_budget=1, serpapi_monthly_budget=1,
            rss_daily_budget=1, rss_monthly_budget=1,
            fetch_starts_per_hour=1, paid_bing_approved=True,
            bing_daily_budget=1, youtube_policy_approved=True,
        )
        projection = {"items": [], "has_more": False, "limit": 20}
        facade = SimpleNamespace()

        class Snapshot:
            def __init__(self, *_args, **_kwargs):
                pass

            def read_latest_dataset_summary(self):
                return True, {"dataset_id": "snapshot-1"}

            def read_run_history_projection(self):
                return True, True, projection

            def load(self):
                raise AssertionError("current Run History projection must avoid SQLite restore")

        with patch.dict("os.environ", {"VERCEL": "1"}), patch(
            "app.orchestration.vercel_fetch_read_composition._client_project_id",
            return_value=_PROJECT,
        ), patch(
            "app.orchestration.vercel_fetch_read_composition.GoogleLiveSettings.from_environment",
            return_value=google,
        ), patch(
            "app.orchestration.vercel_fetch_read_composition.normal_google_live_policy",
            return_value=policy,
        ), patch(
            "app.orchestration.vercel_fetch_read_composition.FirestoreAtomicStateStore",
            return_value=object(),
        ), patch(
            "app.orchestration.vercel_fetch_read_composition.FirestoreSnapshotBlobs",
            return_value=object(),
        ), patch(
            "app.orchestration.vercel_fetch_read_composition.PublicAdmission",
            return_value=object(),
        ), patch(
            "app.orchestration.vercel_fetch_read_composition.DistributedFetchWorker",
            return_value=object(),
        ), patch(
            "app.orchestration.vercel_fetch_read_composition.DistributedPublicFetch",
            return_value=facade,
        ), patch(
            "app.collections.firestore_snapshot.FirestoreCollectionSnapshot",
            Snapshot,
        ):
            async with vercel_fetch_read_composition(
                object(),
                user_agent="TrendScoutTest/0.1",
                restore_collection_snapshot=False,
                allow_collection_restore=True,
                run_history_projection_only=True,
            ) as context:
                self.assertTrue(context.run_history_projection_available)
                self.assertEqual(context.run_history_projection, projection)
                self.assertTrue(context.latest_dataset_summary_available)


class VercelSharedLiveTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_combined_capability_metadata_uses_one_read_result(self):
        calls = []

        def read_combined():
            calls.append("combined")
            return (
                {"admission_status": "AVAILABLE"},
                {"sources": {WIKIMEDIA_SOURCE_ID: {"status": "AVAILABLE"}}},
            )

        runtime, schedule = await _read_capability_metadata(SimpleNamespace(
            capability_metadata_snapshot=read_combined,
            capability_runtime_metadata=lambda: self.fail("separate runtime read"),
            refresh_schedule_metadata=lambda: self.fail("separate schedule read"),
        ))

        self.assertEqual(calls, ["combined"])
        self.assertEqual(runtime["admission_status"], "AVAILABLE")
        self.assertEqual(schedule["sources"][WIKIMEDIA_SOURCE_ID]["status"], "AVAILABLE")

    async def test_failed_combined_capability_read_falls_back_to_parallel_readers(self):
        barrier = Barrier(2)

        def read_combined():
            raise RuntimeError("batch unavailable")

        def read_runtime():
            barrier.wait(timeout=1)
            return {"admission_status": "AVAILABLE"}

        def read_schedule():
            barrier.wait(timeout=1)
            return {"sources": {WIKIMEDIA_SOURCE_ID: {"status": "AVAILABLE"}}}

        runtime, schedule = await _read_capability_metadata(SimpleNamespace(
            capability_metadata_snapshot=read_combined,
            capability_runtime_metadata=read_runtime,
            refresh_schedule_metadata=read_schedule,
        ))

        self.assertEqual(runtime["admission_status"], "AVAILABLE")
        self.assertEqual(schedule["sources"][WIKIMEDIA_SOURCE_ID]["status"], "AVAILABLE")

    async def test_capability_quota_and_schedule_reads_run_concurrently(self):
        barrier = Barrier(2)

        def read_runtime():
            barrier.wait(timeout=1)
            return {"admission_status": "AVAILABLE"}

        def read_schedule():
            barrier.wait(timeout=1)
            return {"sources": {WIKIMEDIA_SOURCE_ID: {"status": "AVAILABLE"}}}

        runtime, schedule = await _read_capability_metadata(SimpleNamespace(
            capability_runtime_metadata=read_runtime,
            refresh_schedule_metadata=read_schedule,
        ))

        self.assertEqual(runtime["admission_status"], "AVAILABLE")
        self.assertEqual(schedule["sources"][WIKIMEDIA_SOURCE_ID]["status"], "AVAILABLE")

    async def test_request_timing_log_uses_route_template_and_excludes_request_data(self):
        settings = SimpleNamespace(
            environment_profile="vercel",
            vercel_shared_live_enabled=True,
            wikimedia_user_agent="TrendScoutTest/0.1",
        )

        @asynccontextmanager
        async def context_factory(_client, *, user_agent):
            self.assertEqual(user_agent, settings.wikimedia_user_agent)
            yield SimpleNamespace(available_sources=frozenset({WIKIMEDIA_SOURCE_ID}))

        async def endpoint(scope, _receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        middleware = VercelSharedLiveMiddleware(
            endpoint,
            settings=settings,
            enabled=True,
            client_factory=lambda _token: SimpleNamespace(close=lambda: None),
            context_factory=context_factory,
        )
        api = FastAPI()
        api.add_api_route("/api/v1/capabilities", lambda: {}, methods=["GET"])
        api.add_api_route(
            "/api/v1/fetches/{fetch_id}", lambda fetch_id: {"id": fetch_id}, methods=["GET"]
        )
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/capabilities",
            "headers": [(b"x-vercel-oidc-token", b"sensitive-test-token")],
            "app": api,
        }
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        with self.assertLogs("app.main", level="INFO") as captured:
            await middleware(scope, receive, send)

        self.assertEqual(messages[0]["status"], 200)
        timing = next(message for message in captured.output
                      if "Vercel Shared LIVE request timing" in message)
        self.assertIn("route=/api/v1/capabilities status_code=200", timing)
        self.assertRegex(
            timing,
            r"client_setup_ms=\d+ composition_ms=\d+ handler_ms=\d+ total_ms=\d+",
        )
        self.assertNotIn("sensitive-test-token", timing)
        dynamic_scope = {**scope, "path": "/api/v1/fetches/" + "a" * 32}
        self.assertEqual(
            _vercel_timing_route(dynamic_scope),
            "/api/v1/fetches/{fetch_id}",
        )
        self.assertEqual(
            _vercel_timing_route({"path": "/api/v1/capabilities"}),
            "/api/v1/capabilities",
        )

    async def test_postgres_request_timing_log_excludes_query_data(self):
        settings = SimpleNamespace(
            environment_profile="vercel",
            vercel_shared_live_enabled=False,
            vercel_postgres_runtime_enabled=True,
            wikimedia_user_agent="TrendScoutTest/0.1",
        )

        @asynccontextmanager
        async def context_factory(*, user_agent, postgres_environment, postgres_schema,
                                  capability_metadata_request):
            self.assertEqual(user_agent, settings.wikimedia_user_agent)
            self.assertIsNone(postgres_environment)
            self.assertEqual(postgres_schema, "trendscout")
            self.assertFalse(capability_metadata_request)
            yield SimpleNamespace(available_sources=frozenset())

        async def endpoint(scope, _receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        middleware = VercelSharedLiveMiddleware(
            endpoint,
            settings=settings,
            enabled=True,
            postgres_context_factory=context_factory,
        )
        api = FastAPI()
        api.add_api_route("/api/v1/datasets/latest", lambda: {}, methods=["GET"])
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/datasets/latest",
            "query_string": b"token=secret-query-value",
            "headers": [],
            "app": api,
        }
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        with self.assertLogs("app.main", level="INFO") as captured:
            await middleware(scope, receive, send)

        self.assertEqual(messages[0]["status"], 200)
        self.assertIn((b"cache-control", b"no-store"), messages[0]["headers"])
        timing = next(message for message in captured.output
                      if "Vercel PostgreSQL request timing" in message)
        self.assertIn("route=/api/v1/datasets/latest status_code=200", timing)
        self.assertRegex(timing, r"composition_ms=\d+ handler_ms=\d+ total_ms=\d+")
        self.assertNotIn("secret-query-value", timing)

    async def test_collection_get_uses_read_composition_without_distributed_worker(self):
        events = []
        service = object()
        sources = frozenset({WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID,
                             YOUTUBE_SOURCE_ID})
        settings = SimpleNamespace(
            environment_profile="vercel",
            vercel_shared_live_enabled=True,
            wikimedia_user_agent="TrendScoutTest/0.1",
        )

        @asynccontextmanager
        async def read_context(_client, *, user_agent, defer_collection_snapshot,
                               latest_dataset_summary_only, dashboard_bootstrap_only,
                               analytics_overview_only, collection_workbench_only,
                               algorithm_results_only,
                               research_bootstrap_only):
            self.assertEqual(user_agent, settings.wikimedia_user_agent)
            self.assertFalse(defer_collection_snapshot)
            self.assertTrue(latest_dataset_summary_only)
            self.assertFalse(dashboard_bootstrap_only)
            self.assertFalse(analytics_overview_only)
            self.assertFalse(collection_workbench_only)
            self.assertFalse(algorithm_results_only)
            self.assertFalse(research_bootstrap_only)
            events.append("read_context")
            yield SimpleNamespace(collection_service=service, available_sources=sources)

        async def unused_live_context(*_args, **_kwargs):
            raise AssertionError("collection GET must not construct LIVE workers")

        observed = {}

        async def endpoint(scope, _receive, send):
            observed.update(scope["state"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = VercelSharedLiveMiddleware(
            endpoint,
            settings=settings,
            enabled=True,
            client_factory=lambda _token: SimpleNamespace(close=lambda: None),
        )
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/datasets/latest",
            "headers": [(b"x-vercel-oidc-token", b"test-token")],
        }
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        with patch(
            "app.orchestration.vercel_collection_read_composition.vercel_collection_read_composition",
            new=read_context,
        ), patch(
            "app.orchestration.vercel_wikimedia_live_composition.vercel_shared_live_composition",
            new=unused_live_context,
        ):
            await middleware(scope, receive, send)

        self.assertEqual(events, ["read_context"])
        self.assertEqual(messages[0]["status"], 200)
        self.assertIs(observed["collection_service"], service)
        self.assertIsNone(observed["distributed_public_fetch"])
        self.assertEqual(observed["public_fetch_source_ids"], tuple(sorted(sources)))

    async def test_fetch_status_uses_provider_free_read_composition(self):
        events = []
        sources = frozenset({WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID,
                             YOUTUBE_SOURCE_ID})
        facade = SimpleNamespace(
            available_sources=sources,
            firestore_shared_live=True,
            latest_dataset_summary_available=True,
            latest_dataset_summary={"dataset_id": _JOB_ID},
        )
        settings = SimpleNamespace(
            environment_profile="vercel",
            vercel_shared_live_enabled=True,
            wikimedia_user_agent="TrendScoutTest/0.1",
        )

        @asynccontextmanager
        async def read_context(_client, *, user_agent, defer_collection_snapshot,
                               read_latest_dataset_summary, restore_collection_snapshot,
                               allow_collection_restore, run_history_projection_only):
            self.assertEqual(user_agent, settings.wikimedia_user_agent)
            self.assertTrue(defer_collection_snapshot)
            expected_restore = False
            expected_allow = len(events) in {1, 2}
            expected_projection = len(events) == 1
            self.assertEqual(restore_collection_snapshot, expected_restore)
            self.assertEqual(allow_collection_restore, expected_allow)
            self.assertEqual(run_history_projection_only, expected_projection)
            facade.latest_dataset_summary_available = read_latest_dataset_summary
            facade.latest_dataset_summary = ({"dataset_id": _JOB_ID}
                                             if read_latest_dataset_summary else None)
            facade.run_history_projection_available = run_history_projection_only
            facade.run_history_projection = (
                {"items": [], "limit": 20, "has_more": False}
                if run_history_projection_only else None
            )
            if restore_collection_snapshot:
                facade.collection_service = SimpleNamespace()
            events.append(read_latest_dataset_summary)
            yield facade

        async def unused_live_context(*_args, **_kwargs):
            raise AssertionError("Fetch status must not construct provider adapters")

        observed = []

        async def endpoint(scope, _receive, send):
            observed.append(dict(scope["state"]))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = VercelSharedLiveMiddleware(
            endpoint,
            settings=settings,
            enabled=True,
            client_factory=lambda _token: SimpleNamespace(close=lambda: None),
        )
        scope = {
            "type": "http",
            "method": "GET",
            "path": f"/api/v1/fetches/{_JOB_ID}",
            "headers": [(b"host", b"trend-scout.example"),
                        (b"origin", b"https://trend-scout.example"),
                        (b"x-vercel-oidc-token", b"test-token")],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        with patch(
            "app.orchestration.vercel_fetch_read_composition.vercel_fetch_read_composition",
            new=read_context,
        ), patch(
            "app.orchestration.vercel_wikimedia_live_composition.vercel_shared_live_composition",
            new=unused_live_context,
        ):
            await middleware(scope, receive, send)
            scope["path"] = "/api/v1/runs/history"
            await middleware(scope, receive, send)
            scope["method"] = "POST"
            scope["path"] = f"/api/v1/fetches/{_JOB_ID}/collections"
            with patch("app.main._save_vercel_collection_snapshot", new=AsyncMock(return_value=None)):
                await middleware(scope, receive, send)

        self.assertEqual(events, [True, True, True])
        self.assertIs(observed[0]["distributed_public_fetch"], facade)
        self.assertIsNone(observed[0]["collection_service"])
        self.assertTrue(observed[0]["latest_dataset_summary_available"])
        self.assertIs(observed[1]["distributed_public_fetch"], facade)
        self.assertTrue(observed[1]["latest_dataset_summary_available"])
        self.assertIsNone(observed[1]["collection_service"])
        self.assertTrue(observed[1]["run_history_projection_available"])
        self.assertEqual(observed[1]["run_history_projection"]["items"], [])

    async def test_initial_fetch_admission_uses_provider_free_composition(self):
        events = []
        facade = SimpleNamespace(
            available_sources=frozenset({WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID,
                                         BING_SOURCE_ID, YOUTUBE_SOURCE_ID}),
            firestore_shared_live=True,
            latest_dataset_summary_available=True,
            latest_dataset_summary={"dataset_id": _JOB_ID},
        )
        settings = SimpleNamespace(
            environment_profile="vercel",
            vercel_shared_live_enabled=True,
            wikimedia_user_agent="TrendScoutTest/0.1",
        )

        @asynccontextmanager
        async def submit_context(_client, *, user_agent, defer_collection_snapshot,
                                 read_latest_dataset_summary, restore_collection_snapshot,
                                 allow_collection_restore, run_history_projection_only):
            self.assertEqual(user_agent, settings.wikimedia_user_agent)
            self.assertTrue(defer_collection_snapshot)
            self.assertFalse(read_latest_dataset_summary)
            self.assertFalse(restore_collection_snapshot)
            self.assertFalse(allow_collection_restore)
            self.assertFalse(run_history_projection_only)
            events.append("provider_free_context")
            yield facade

        async def unused_live_context(*_args, **_kwargs):
            raise AssertionError("initial Fetch admission must not build provider adapters")

        async def endpoint(scope, _receive, send):
            await send({"type": "http.response.start", "status": 202, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        middleware = VercelSharedLiveMiddleware(
            endpoint,
            settings=settings,
            enabled=True,
            client_factory=lambda _token: SimpleNamespace(close=lambda: None),
        )
        scope = {
            "type": "http", "method": "POST", "path": "/api/v1/fetches",
            "headers": [(b"host", b"trend-scout.example"),
                        (b"origin", b"https://trend-scout.example"),
                        (b"x-vercel-oidc-token", b"test-token")],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        with patch(
            "app.orchestration.vercel_fetch_read_composition.vercel_fetch_read_composition",
            new=submit_context,
        ), patch(
            "app.orchestration.vercel_wikimedia_live_composition.vercel_shared_live_composition",
            new=unused_live_context,
        ):
            await middleware(scope, receive, send)
        self.assertEqual(events, ["provider_free_context"])


class _Client:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def close(self):
        self.closed = True
        self.events.append("client_closed")


class _FetchFacade:
    def __init__(self, events, client):
        self.events = events
        self.client = client
        self.available_sources = frozenset({WIKIMEDIA_SOURCE_ID})
        self.data_mode = "LIVE"
        self.firestore_shared_live = True
        self.vercel_wikimedia_live = True
        self.admission = SimpleNamespace(
            policy=SimpleNamespace(
                enabled_sources=self.available_sources,
                max_starts_per_window=3,
                window_seconds=3600,
                cooldown_seconds=60,
                max_status_polls=120,
                receipt_seconds=3600,
            ),
            operationally_enabled=lambda: True,
        )
        self.submitted_sources = None
        self.submitted_manual_source_refresh = None
        self.submitted_keys = []
        self.status_calls = 0
        self.terminal_projection_calls = 0
        self.collection_service = None
        self.submit_status = "QUEUED"
        self.force_replay = False
        self.slice_state = "PARTIAL"
        self.slice_calls = []

    @staticmethod
    def _history_item():
        return {
            "fetch_id": _JOB_ID,
            "run_type": "FETCH",
            "status": "COMPLETED",
            "outcome_status": "COMPLETED",
            "data_mode": "LIVE",
            "mode": "LIVE",
            "sources": [WIKIMEDIA_SOURCE_ID],
            "source_manifest": [{
                "source_id": WIKIMEDIA_SOURCE_ID,
                "status": "SUCCESS",
                "record_count": 1,
                "delivery_state": "FRESH",
                "error_code": None,
                "coverage": {},
            }],
            "received_record_count": 1,
            "retained_keyword_count": None,
            "rejected_record_count": None,
            "created_at": "2026-11-19T00:00:00+00:00",
            "updated_at": "2026-11-19T00:00:01+00:00",
            "run_at": "2026-11-19T00:00:01+00:00",
            "fetched_at": "2026-11-19T00:00:01+00:00",
            "duration_seconds": None,
            "progress": {"completed_sources": 1, "total_sources": 1},
        }

    def submit(self, idempotency_key, *, source_ids, manual_source_refresh=False):
        self.submitted_keys.append(idempotency_key)
        self.submitted_sources = source_ids
        self.submitted_manual_source_refresh = manual_source_refresh
        return {
            "job_id": _JOB_ID,
            "status": self.submit_status,
            "result": "REPLAY" if self.force_replay or len(self.submitted_keys) > 1 else "CREATED",
            "created_at": "2026-11-19T00:00:00+00:00",
        }

    async def run_once(self, _owner):
        entered = max((index for index, event in enumerate(self.events)
                       if event == "context_entered"), default=-1)
        exited = max((index for index, event in enumerate(self.events)
                      if event == "context_exited"), default=-1)
        if self.client.closed or entered < exited:
            raise AssertionError("request resources closed before the background worker finished")
        self.events.append("worker_finished")
        return None

    async def run_slice(self, _owner, fetch_id, *, max_sources):
        entered = max((index for index, event in enumerate(self.events)
                       if event == "context_entered"), default=-1)
        exited = max((index for index, event in enumerate(self.events)
                      if event == "context_exited"), default=-1)
        if self.client.closed or entered < exited:
            raise AssertionError("request resources closed before the source slice finished")
        self.slice_calls.append((fetch_id, max_sources))
        self.events.append("worker_finished")
        return {"job_id": fetch_id, "state": self.slice_state, "sources": {}}

    def status(self, _fetch_id, *, include_records=False, include_discovery=True):
        self.status_calls += 1
        result = self._history_item()
        result["terminal"] = True
        result["selected_sources"] = [WIKIMEDIA_SOURCE_ID]
        if include_records:
            result["sources"] = [{
                "source_id": WIKIMEDIA_SOURCE_ID,
                "data_mode": "LIVE",
                "status": "SUCCESS",
                "records": [{"title": "retained checkpoint record"}],
                "record_count": 1,
            }]
        return result

    def terminal_status_from_worker_result(self, worker_result, *, created_at=None):
        self.terminal_projection_calls += 1
        return {
            "fetch_id": worker_result["job_id"],
            "status": worker_result["state"],
            "terminal": True,
            "data_mode": "LIVE",
            "created_at": created_at,
            "sources": [{"source_id": WIKIMEDIA_SOURCE_ID}],
        }

    def history(self, *, limit=20):
        return [self._history_item()][:limit]


class VercelSharedLiveFetchTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, *, enabled):
        values = {
            "VERCEL": "1",
            "GOOGLE_CLOUD_PROJECT": _PROJECT,
            "GOOGLE_WIF_AUDIENCE": _AUDIENCE,
            "TREND_SCOUT_VERCEL_SHARED_LIVE_ENABLED": "true" if enabled else "false",
            "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false",
        }
        return Settings.from_environment(values)

    def app(self, *, enabled):
        events = []
        client = _Client(events)
        facade = _FetchFacade(events, client)

        class Snapshot:
            def set_latest_dataset_summary_provider(self, _provider):
                self.latest_summary_provider = _provider

            def set_dashboard_bootstrap_provider(self, _provider):
                self.dashboard_bootstrap_provider = _provider

            def save(self):
                events.append("snapshot_saved")

        facade.collection_snapshot = Snapshot()
        client_factory = Mock(return_value=client)

        @asynccontextmanager
        async def context_factory(_client, *, user_agent):
            self.assertTrue(user_agent)
            facade.client = _client
            events.append("context_entered")
            try:
                yield facade
            finally:
                events.append("context_exited")

        application = create_app(
            settings=self.settings(enabled=enabled),
            vercel_firestore_client_factory=client_factory,
            vercel_live_context_factory=context_factory,
        )
        return application, events, client, facade, client_factory

    async def test_first_vercel_fetch_post_returns_before_running_source_slice(self):
        app, events, client, facade, client_factory = self.app(enabled=True)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                response = await browser.post(
                    "/api/v1/fetches",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                        "Idempotency-Key": "vercel-wikimedia-test-0001",
                    },
                    json={},
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["sources"], [WIKIMEDIA_SOURCE_ID])
        self.assertEqual(facade.submitted_sources, [WIKIMEDIA_SOURCE_ID])
        self.assertEqual(events, ["context_entered", "context_exited", "client_closed"])
        self.assertEqual(facade.slice_calls, [])
        client_factory.assert_called_once_with("synthetic-test-token")
        self.assertTrue(client.closed)

    async def test_all_four_approved_sources_can_be_submitted_together(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        sources = [
            WIKIMEDIA_SOURCE_ID,
            GOOGLE_SOURCE_ID,
            BING_SOURCE_ID,
            YOUTUBE_SOURCE_ID,
        ]
        facade.available_sources = frozenset(sources)
        facade.admission.policy.enabled_sources = facade.available_sources
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                response = await browser.post(
                    "/api/v1/fetches",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                        "Idempotency-Key": "vercel-four-sources-test-0001",
                    },
                    json={"sources": sources},
                )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(set(response.json()["sources"]), set(sources))
        self.assertEqual(facade.submitted_sources, sorted(sources))
        self.assertTrue(facade.submitted_manual_source_refresh)
        self.assertNotIn("worker_finished", events)
        self.assertEqual(facade.slice_calls, [])

    async def test_same_key_submission_replay_waits_for_explicit_source_continuation(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        _client_factory.side_effect = lambda _token: _Client(events)
        facade.slice_state = "QUEUED"
        headers = {
            "Origin": "https://trend-scout.example",
            "x-vercel-oidc-token": "synthetic-test-token",
            "Idempotency-Key": "vercel-source-slice-test-0001",
        }
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                accepted = await browser.post("/api/v1/fetches", headers=headers, json={})
                retry = await browser.post("/api/v1/fetches", headers=headers, json={})
                response = await browser.post(
                    "/api/v1/fetches",
                    headers={**headers, "X-TrendScout-Fetch-Continuation": "1"},
                    json={},
                )

        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(accepted.json()["fetch_id"], retry.json()["fetch_id"])
        self.assertTrue(response.json()["continuation_ready"])
        self.assertEqual(retry.headers["idempotent-replay"], "true")
        self.assertEqual(facade.slice_calls, [(_JOB_ID, 1)])
        self.assertNotIn("collections_processed", events)
        self.assertNotIn("snapshot_saved", events)
        self.assertEqual(events.count("worker_finished"), 1)
        last_context_exit = max(index for index, event in enumerate(events)
                                if event == "context_exited")
        self.assertLess(events.index("worker_finished"), last_context_exit)

    async def test_terminal_slice_returns_validated_status_without_extra_poll(self):
        for delivery, request_count in (("CACHED", 0), ("FRESH", 1)):
            with self.subTest(delivery=delivery):
                app, _events, _client, facade, factory = self.app(enabled=True)
                factory.side_effect = lambda _token: _Client(_events)
                facade.force_replay = True

                async def run_slice(_owner, fetch_id, *, max_sources):
                    return {"job_id": fetch_id, "state": "COMPLETED", "sources": {
                        WIKIMEDIA_SOURCE_ID: {"delivery_state": delivery,
                                              "provider_request_count": request_count}}}

                facade.run_slice = run_slice
                headers = {"Origin": "https://trend-scout.example",
                           "x-vercel-oidc-token": "synthetic-test-token",
                           "Idempotency-Key": "vercel-cached-terminal-0001",
                           "X-TrendScout-Fetch-Continuation": "1"}
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                 base_url="https://trend-scout.example") as browser:
                        response = await browser.post("/api/v1/fetches", headers=headers, json={})
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json()["terminal_fetch"]["fetch_id"], _JOB_ID)
                self.assertEqual(facade.terminal_projection_calls, 1)
                self.assertEqual(facade.status_calls, 0)

    async def test_idempotent_recovery_post_dispatches_a_new_vercel_worker(self):
        app, events, _client, facade, client_factory = self.app(enabled=True)
        client_factory.side_effect = lambda _token: _Client(events)
        sources = [WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID]
        facade.available_sources = frozenset(sources)
        facade.admission.policy.enabled_sources = facade.available_sources
        headers = {
            "Origin": "https://trend-scout.example",
            "x-vercel-oidc-token": "synthetic-test-token",
            "Idempotency-Key": "vercel-recovery-test-0001",
            "X-YouTube-Policy-Version": "test-policy-v1",
        }
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                first = await browser.post(
                    "/api/v1/fetches", headers=headers, json={"sources": sources},
                )
                resumed = await browser.post(
                    "/api/v1/fetches",
                    headers={**headers, "X-TrendScout-Fetch-Continuation": "1"},
                    json={"sources": sources},
                )

        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(resumed.status_code, 202, resumed.text)
        self.assertEqual(first.json()["fetch_id"], resumed.json()["fetch_id"])
        self.assertEqual(resumed.headers["idempotent-replay"], "true")
        self.assertEqual(facade.submitted_keys, [headers["Idempotency-Key"]] * 2)
        self.assertEqual(events.count("worker_finished"), 1)
        self.assertEqual(events.count("context_entered"), 2)
        self.assertEqual(events.count("context_exited"), 2)

    async def test_feature_gate_off_never_constructs_identity_or_worker(self):
        app, events, _client, _facade, client_factory = self.app(enabled=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                response = await browser.post("/api/v1/fetches", json={})

        self.assertEqual(response.status_code, 503)
        client_factory.assert_not_called()
        self.assertEqual(events, [])

    async def test_missing_or_cross_origin_identity_fails_before_firestore(self):
        app, _events, _client, _facade, client_factory = self.app(enabled=True)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                missing_token = await browser.post(
                    "/api/v1/fetches",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "Idempotency-Key": "vercel-wikimedia-test-0002",
                    },
                    json={},
                )
                cross_origin = await browser.post(
                    "/api/v1/fetches",
                    headers={
                        "Origin": "https://attacker.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                        "Idempotency-Key": "vercel-wikimedia-test-0003",
                    },
                    json={},
                )

        self.assertEqual(missing_token.status_code, 401)
        self.assertEqual(cross_origin.status_code, 403)
        client_factory.assert_not_called()

    async def test_other_providers_are_not_selectable_in_wikimedia_slice(self):
        app, _events, _client, _facade, _client_factory = self.app(enabled=True)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                response = await browser.post(
                    "/api/v1/fetches",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                        "Idempotency-Key": "vercel-wikimedia-test-0004",
                    },
                    json={"sources": ["google_web_serp"]},
                )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn("worker_finished", _events)

    def test_checkpoint_recovery_uses_shared_live_middleware(self):
        settings = self.settings(enabled=True)
        self.assertTrue(_is_vercel_shared_live_request(
            settings, "POST", f"/api/v1/fetches/{_JOB_ID}/collections", enabled=True,
        ))
        self.assertFalse(_is_vercel_shared_live_request(
            settings, "POST", "/api/v1/fetches/not-a-fetch-id/collections", enabled=True,
        ))

    def test_research_routes_use_shared_live_middleware(self):
        settings = self.settings(enabled=True)
        for method, path in (
            ("GET", "/api/v1/research"),
            ("GET", "/api/v1/research/bootstrap"),
            ("POST", "/api/v1/research"),
            ("PATCH", "/api/v1/research/research-id"),
        ):
            with self.subTest(method=method, path=path):
                self.assertTrue(_is_vercel_shared_live_request(
                    settings, method, path, enabled=True,
                ))
        self.assertTrue(_persists_vercel_collections("POST", "/api/v1/research"))
        self.assertTrue(_vercel_collection_snapshot_required(
            "GET", "/api/v1/research/bootstrap",
        ))
        self.assertTrue(_vercel_collection_read_composition_required(
            "GET", "/api/v1/research/bootstrap",
        ))
        self.assertTrue(_persists_vercel_collections(
            "PATCH", "/api/v1/research/research-id",
        ))

    async def test_runtime_route_uses_request_scoped_collection_service(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        with TemporaryDirectory() as directory:
            facade.collection_service = CollectionService(
                CollectionStore(Path(directory) / "collections.sqlite", data_mode_scope="LIVE"),
                fetch_model_inference_enabled=False,
            )
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="https://trend-scout.example",
                ) as browser:
                    response = await browser.get(
                        "/api/v1/runtime",
                        headers={"x-vercel-oidc-token": "synthetic-test-token"},
                    )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["storage"]["status"], "AVAILABLE")
        self.assertEqual(response.json()["storage"]["keyword_records"], 0)
        self.assertEqual(response.json()["embedding"]["model_load_status"], "LEGACY_CACHE_ONLY")
        self.assertIn("context_entered", events)
        self.assertIn("context_exited", events)

    async def test_research_reads_and_mutations_use_and_persist_request_store(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        with TemporaryDirectory() as directory:
            facade.collection_service = CollectionService(CollectionStore(
                Path(directory) / "collections.sqlite", data_mode_scope="LIVE",
            ))
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="https://trend-scout.example",
                ) as browser:
                    headers = {"x-vercel-oidc-token": "synthetic-test-token"}
                    before = await browser.get("/api/v1/research", headers=headers)
                    created = await browser.post(
                        "/api/v1/research",
                        headers={**headers, "Origin": "https://trend-scout.example"},
                        json={"subject": "Fixture", "hypothesis": "A testable idea."},
                    )
                    updated = await browser.patch(
                        f"/api/v1/research/{created.json()['id']}",
                        headers={**headers, "Origin": "https://trend-scout.example"},
                        json={"status": "RESEARCHING"},
                    )
                    after = await browser.get("/api/v1/research", headers=headers)
                    bootstrap = await browser.get(
                        "/api/v1/research/bootstrap", headers=headers,
                    )

        self.assertEqual(before.status_code, 200, before.text)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(after.status_code, 200, after.text)
        self.assertEqual(after.json()["total"], 1)
        self.assertEqual(after.json()["items"][0]["status"], "RESEARCHING")
        self.assertEqual(bootstrap.status_code, 200, bootstrap.text)
        self.assertEqual(bootstrap.json()["research"]["total"], 1)
        self.assertEqual(events.count("snapshot_saved"), 2)

    async def test_bing_only_request_adds_google_dependency_before_admission(self):
        app, _events, _client, facade, _client_factory = self.app(enabled=True)
        facade.available_sources = frozenset({
            WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID,
        })
        facade.admission.policy.enabled_sources = facade.available_sources
        facade.data_mode = "LIVE"
        facade.normal_bing_live = True
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                response = await browser.post(
                    "/api/v1/fetches",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                        "Idempotency-Key": "vercel-bing-auto-google-0001",
                    },
                    json={"sources": [BING_SOURCE_ID]},
                )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["requested_sources"], [BING_SOURCE_ID])
        self.assertEqual(
            response.json()["sources"], sorted([GOOGLE_SOURCE_ID, BING_SOURCE_ID])
        )
        self.assertEqual(facade.submitted_sources,
                         sorted([GOOGLE_SOURCE_ID, BING_SOURCE_ID]))
        self.assertTrue(facade.submitted_manual_source_refresh)

    async def test_search_fetch_uses_platform_protection_without_browser_operator_token(self):
        app, _events, _client, facade, _client_factory = self.app(enabled=True)
        facade.available_sources = frozenset({
            WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID,
        })
        facade.admission.policy.enabled_sources = facade.available_sources
        headers = {
            "Origin": "https://trend-scout.example",
            "x-vercel-oidc-token": "synthetic-test-token",
            "Idempotency-Key": "vercel-platform-protected-0001",
        }
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                response = await browser.post(
                    "/api/v1/fetches", headers=headers,
                    json={"sources": [GOOGLE_SOURCE_ID]},
                )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(facade.submitted_sources, [GOOGLE_SOURCE_ID])
        self.assertTrue(facade.submitted_manual_source_refresh)

    async def test_shared_dashboard_history_and_detail_are_available_without_fabricated_totals(self):
        app, _events, _client, facade, _client_factory = self.app(enabled=True)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                headers = {"x-vercel-oidc-token": "synthetic-test-token"}
                history = await browser.get("/api/v1/runs/history", headers=headers)
                capabilities = await browser.get("/api/v1/capabilities", headers=headers)
                detail = await browser.get(
                    f"/api/v1/runs/history/{_JOB_ID}", headers=headers
                )
                overview = await browser.get(
                    "/api/v1/analytics/overview", headers=headers
                )

        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["history_source"], "local_sqlite_and_shared_firestore")
        self.assertEqual(history.json()["items"][0]["fetch_id"], _JOB_ID)
        self.assertEqual(capabilities.status_code, 200, capabilities.text)
        self.assertTrue(capabilities.json()["vercel_shared_live"])
        self.assertFalse(capabilities.json()["read_only"])
        self.assertEqual(capabilities.json()["data_mode"], "LIVE")
        self.assertEqual(
            [source["source_id"] for source in capabilities.json()["sources"]
             if source["enabled_for_current_operation"]],
            [WIKIMEDIA_SOURCE_ID],
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["item"]["received_record_count"], 1)
        self.assertEqual(facade.status_calls, 0)
        self.assertEqual(overview.status_code, 200, overview.text)
        self.assertIsNone(overview.json()["keyword_totals"]["unique"])
        self.assertEqual(overview.json()["shared_fetch_history"]["items"], 1)

    async def test_existing_terminal_fetch_defers_collections_to_recovery_endpoint(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        facade.submit_status = "PARTIAL"
        facade.force_replay = True
        processed = []

        class _Service:
            store = SimpleNamespace(data_mode_scope="LIVE")

            async def process_fetch(self, fetch_id, sources, *, cached_context_only=False):
                processed.append((fetch_id, sources, cached_context_only))
                events.append("collections_processed")
                return {"status": "COMPLETED"}

        facade.collection_service = _Service()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                response = await browser.post(
                    "/api/v1/fetches",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                        "Idempotency-Key": "vercel-terminal-postprocess-0001",
                    },
                    json={},
                )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "PARTIAL")
        self.assertEqual(processed, [])
        self.assertNotIn("worker_finished", events)
        self.assertNotIn("collections_processed", events)
        self.assertNotIn("snapshot_saved", events)

    async def test_checkpoint_recovery_endpoint_never_runs_source_worker(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        processed = []

        class _Service:
            store = SimpleNamespace(data_mode_scope="LIVE")

            async def process_fetch(self, fetch_id, sources, *, cached_context_only=False):
                processed.append((fetch_id, sources, cached_context_only))
                events.append("collections_processed")
                return {"status": "COMPLETED"}

        facade.collection_service = _Service()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                response = await browser.post(
                    f"/api/v1/fetches/{_JOB_ID}/collections",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "COMPLETED")
        self.assertEqual(processed[0][0], _JOB_ID)
        self.assertEqual(processed[0][1][0]["records"][0]["title"],
                         "retained checkpoint record")
        self.assertTrue(processed[0][2])
        self.assertNotIn("worker_finished", events)
        self.assertLess(events.index("collections_processed"), events.index("snapshot_saved"))

    async def test_recovery_resumes_only_fresh_unanalyzed_wikimedia_algorithms(self):
        app, _events, _client, facade, _client_factory = self.app(enabled=True)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        source = {
            "source_id": WIKIMEDIA_SOURCE_ID,
            "status": "SUCCESS",
            "data_mode": "LIVE",
            "delivery_state": "FRESH",
            "provider_request_count": 1,
            "record_count": 1,
            "coverage": {"provider_request_count": 1},
            "records": [{"article_original": "Recovery_Example", "views_ceil": 1000}],
        }
        checkpoint_source = {key: value for key, value in source.items()
                             if key != "source_id"}
        checkpoint = {"snapshot": {"data_mode": "LIVE", "result": checkpoint_source}}
        calls = []

        class _Store:
            data_mode_scope = "LIVE"

            def collection_job(self, _fetch_id):
                return ({"status": "MODEL_UNAVAILABLE",
                         "algorithm_processing_status": "CACHE_ONLY", "ai_jobs": []}, False)

            def fetch_collection_processing_result(self, _fetch_id):
                return {"status": "MODEL_UNAVAILABLE", "error_code": "AI_DISABLED"}

        class _Service:
            store = _Store()

            async def process_fetch(self, fetch_id, sources, *, cached_context_only=False,
                                    resume_model_unavailable=False):
                calls.append((fetch_id, sources, cached_context_only,
                              resume_model_unavailable))
                return {"status": "MODEL_UNAVAILABLE", "error_code": "AI_DISABLED"}

        facade.postgres_live = True
        facade.firestore_shared_live = False
        facade.data_mode = "LIVE"
        facade.collection_service = _Service()
        facade.collection_reuse_for_fetch = lambda _fetch_id: {
            "terminal": True, "collection_input_unchanged": False,
        }
        facade.utcnow = lambda: now
        facade.admission.read_job = lambda _fetch_id, now: {
            "state": "PARTIAL", "source_checkpoints": {WIKIMEDIA_SOURCE_ID: checkpoint},
        }
        facade.status = lambda _fetch_id, *, include_records, include_discovery: {
            "terminal": True, "data_mode": "LIVE", "sources": [source],
        }

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                response = await browser.post(
                    f"/api/v1/fetches/{_JOB_ID}/collections",
                    headers={"Origin": "https://trend-scout.example",
                             "x-vercel-oidc-token": "synthetic-test-token"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "MODEL_UNAVAILABLE")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], _JOB_ID)
        self.assertTrue(calls[0][2])
        self.assertTrue(calls[0][3])

    async def test_recovery_analyzes_persisted_rows_when_terminal_receipt_is_missing(self):
        app, _events, _client, facade, _client_factory = self.app(enabled=True)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        source = {
            "source_id": WIKIMEDIA_SOURCE_ID,
            "status": "SUCCESS",
            "data_mode": "LIVE",
            "delivery_state": "FRESH",
            "provider_request_count": 1,
            "record_count": 1,
            "coverage": {"provider_request_count": 1},
            "records": [{"article_original": "Retained_Receipt_Example", "views_ceil": 1000}],
        }
        checkpoint_source = {key: value for key, value in source.items()
                             if key != "source_id"}
        checkpoint = {"snapshot": {"data_mode": "LIVE", "result": checkpoint_source}}
        analyzed = []

        class _Store:
            data_mode_scope = "LIVE"

            def collection_job(self, _fetch_id):
                return ({"status": "MODEL_UNAVAILABLE",
                         "algorithm_processing_status": "CACHE_ONLY", "ai_jobs": []}, False)

            def fetch_collection_processing_result(self, _fetch_id):
                return {"status": "MODEL_UNAVAILABLE", "error_code": "AI_DISABLED"}

        class _Service:
            store = _Store()

            def analyze_retained_live_keywords(self, fetch_id, sources):
                analyzed.append((fetch_id, sources))
                return {"analysis_status": "COMPLETED", "executed_keywords": 1,
                        "persisted_algorithms": 6}

            async def process_fetch(self, *_args, **_kwargs):
                raise AssertionError("persisted recovery must not reload source records")

        facade.postgres_live = True
        facade.firestore_shared_live = False
        facade.data_mode = "LIVE"
        facade.collection_service = _Service()
        facade.collection_reuse_for_fetch = lambda _fetch_id: {
            "terminal": True, "collection_input_unchanged": False,
        }
        facade.utcnow = lambda: now
        facade.admission.read_job = lambda _fetch_id, now: {
            "state": "PARTIAL",
            "source_checkpoints": {WIKIMEDIA_SOURCE_ID: checkpoint},
        }
        facade.status = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("persisted recovery should not load source checkpoint blobs")
        )

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                response = await browser.post(
                    f"/api/v1/fetches/{_JOB_ID}/collections",
                    headers={"Origin": "https://trend-scout.example",
                             "x-vercel-oidc-token": "synthetic-test-token"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["algorithm_processing_status"], "COMPLETED")
        self.assertEqual(response.json()["algorithm_persisted_count"], 6)
        self.assertEqual(len(analyzed), 1)
        self.assertEqual(analyzed[0][0], _JOB_ID)
        self.assertEqual(analyzed[0][1], [])

    async def test_persisted_checkpoint_recovery_skips_records_and_snapshot_write(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        with TemporaryDirectory() as directory:
            store = CollectionStore(Path(directory) / "collections.sqlite", data_mode_scope="LIVE")
            with store.connect() as db:
                db.execute("INSERT INTO fetch_jobs(fetch_id,status,error_code,updated_at) VALUES(?,?,?,?)",
                           (_JOB_ID, "MODEL_UNAVAILABLE", "MODEL_INFERENCE_DEFERRED", "2026-11-24T00:00:00Z"))
                db.execute("""INSERT INTO dataset_snapshots(dataset_id,status,data_mode,source_manifest,
                    provider_record_count,received_record_count,retained_keyword_count,rejected_record_count,
                    quality_flags,processing_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (_JOB_ID, "PARTIAL", "LIVE", "[]", 1, 1, 1, 0, "[]",
                     "MODEL_UNAVAILABLE", "2026-11-24T00:00:00Z"))
            facade.collection_service = CollectionService(store, fetch_model_inference_enabled=False)
            facade.utcnow = lambda: "unused-clock"
            facade.admission.read_job = lambda fetch_id, now: {
                "state": "PARTIAL", "source_checkpoints": {
                    WIKIMEDIA_SOURCE_ID: {"snapshot": {"data_mode": "LIVE"}}
                },
            }
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="https://trend-scout.example",
                ) as browser:
                    response = await browser.post(
                        f"/api/v1/fetches/{_JOB_ID}/collections",
                        headers={"Origin": "https://trend-scout.example",
                                 "x-vercel-oidc-token": "synthetic-test-token"},
                    )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "MODEL_UNAVAILABLE")
            self.assertEqual(facade.status_calls, 0)
            self.assertNotIn("snapshot_saved", events)

    async def test_deferred_model_recovery_keeps_ai_work_resumable_without_rerunning_algorithms(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        facade.firestore_shared_live = False
        facade.vercel_postgres_live = False
        facade.postgres_live = True
        service = SimpleNamespace(
            store=SimpleNamespace(data_mode_scope="LIVE"),
            fetch_model_inference_enabled=True,
            process_fetch=AsyncMock(return_value={"status": "COMPLETED"}),
        )
        facade.collection_service = service
        facade.utcnow = lambda: "unused-clock"
        facade.admission.read_job = lambda fetch_id, now: {
            "state": "PARTIAL", "source_checkpoints": {
                WIKIMEDIA_SOURCE_ID: {"snapshot": {"data_mode": "LIVE"}}
            },
        }
        with TemporaryDirectory() as directory:
            store = CollectionStore(Path(directory) / "collections.sqlite", data_mode_scope="LIVE")
            with store.connect() as db:
                db.execute("INSERT INTO fetch_jobs(fetch_id,status,available,error_code,updated_at) "
                           "VALUES(?,?,?,?,?)",
                           (_JOB_ID, "MODEL_UNAVAILABLE", 1, "MODEL_INFERENCE_DEFERRED",
                            "2026-11-24T00:00:00Z"))
                db.execute("UPDATE fetch_jobs SET ai_jobs=? WHERE fetch_id=?",
                           (json.dumps([{"job_id": "ai_saved_job", "keyword_ids": ["keyword-1"]}]),
                            _JOB_ID))
                db.execute("""INSERT INTO dataset_snapshots(dataset_id,status,data_mode,source_manifest,
                    provider_record_count,received_record_count,retained_keyword_count,rejected_record_count,
                    quality_flags,processing_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (_JOB_ID, "PARTIAL", "LIVE", "[]", 1, 1, 1, 0, "[]",
                     "MODEL_UNAVAILABLE", "2026-11-24T00:00:00Z"))
            service.store = store
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="https://trend-scout.example",
                ) as browser:
                    response = await browser.post(
                        f"/api/v1/fetches/{_JOB_ID}/collections",
                        headers={"Origin": "https://trend-scout.example",
                                 "x-vercel-oidc-token": "synthetic-test-token"},
                    )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "MODEL_UNAVAILABLE")
        self.assertEqual(response.json()["ai_job_ids"], ["ai_saved_job"])
        service.process_fetch.assert_not_awaited()
        self.assertEqual(facade.status_calls, 0)
        self.assertNotIn("worker_finished", events)

    async def test_unchanged_collection_recovery_skips_sqlite_restore_and_snapshot_write(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        facade.collection_reuse_for_fetch = lambda fetch_id: {
            "fetch_id": fetch_id,
            "terminal": True,
            "collection_input_unchanged": True,
            "dataset_id": "b" * 32,
        }

        def reject_snapshot_restore():
            events.append("snapshot_restore_forbidden")
            raise AssertionError("unchanged inputs must not restore SQLite")

        facade.load_collection_snapshot = reject_snapshot_restore
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                response = await browser.post(
                    f"/api/v1/fetches/{_JOB_ID}/collections",
                    headers={"Origin": "https://trend-scout.example",
                             "x-vercel-oidc-token": "synthetic-test-token"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "REUSED")
        self.assertEqual(response.json()["dataset_id"], "b" * 32)
        self.assertTrue(response.json()["collection_input_unchanged"])
        self.assertEqual(facade.status_calls, 0)
        self.assertNotIn("snapshot_restore_forbidden", events)
        self.assertNotIn("snapshot_saved", events)

    async def test_expired_checkpoints_replay_only_retained_live_caches(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        source_ids = [WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID]
        facade.available_sources = frozenset(source_ids)
        facade.admission.policy.enabled_sources = facade.available_sources
        old_manifest = [
            {"source_id": WIKIMEDIA_SOURCE_ID, "status": "SUCCESS", "record_count": 1,
             "delivery_state": "FRESH", "coverage": {}},
            {"source_id": GOOGLE_SOURCE_ID, "status": "UNAVAILABLE", "record_count": 0,
             "error_code": "NOT_YET_ELIGIBLE", "coverage": {}},
            {"source_id": BING_SOURCE_ID, "status": "UNAVAILABLE", "record_count": 0,
             "error_code": "NO_SAVED_CANDIDATE", "coverage": {}},
            {"source_id": YOUTUBE_SOURCE_ID, "status": "PARTIAL", "record_count": 1,
             "delivery_state": "FRESH", "coverage": {}},
        ]
        facade.history = lambda limit=20: [{
            **facade._history_item(), "source_manifest": old_manifest,
        }][:limit]
        facade.status = lambda _fetch_id, include_records=False, include_discovery=True: {
            **facade._history_item(),
            "terminal": True,
            "data_mode": None,
            "selected_sources": source_ids,
            "sources": [{
                "source_id": source_id,
                "status": "UNAVAILABLE",
                "data_mode": "LIVE",
                "record_count": 0,
                "records": [],
                "error_code": ("FETCH_DEADLINE_EXCEEDED"
                               if source_id == WIKIMEDIA_SOURCE_ID
                               else "RESULT_UNAVAILABLE"),
                "coverage": {"checkpoint_status": "RESULT_UNAVAILABLE"},
            } for source_id in source_ids],
        }
        cache_reads = []
        cached_results = {
            WIKIMEDIA_SOURCE_ID: {
                "source_id": WIKIMEDIA_SOURCE_ID, "status": "SUCCESS", "data_mode": "LIVE",
                "delivery_state": "CACHED", "record_count": 1,
                "records": [{"title": "retained Wikimedia cache"}],
                "provider_request_count": 0,
                "coverage": {"cached_provider_request_count": 1},
            },
            YOUTUBE_SOURCE_ID: {
                "source_id": YOUTUBE_SOURCE_ID, "status": "PARTIAL", "data_mode": "LIVE",
                "delivery_state": "STALE_CACHE", "record_count": 1,
                "records": [{"video_id": "retained YouTube cache"}],
                "provider_request_count": 0,
                "coverage": {"cached_provider_request_count": 1},
            },
        }

        def read_cached_source_result(source_id):
            cache_reads.append(source_id)
            return cached_results.get(source_id)

        facade.read_cached_source_result = read_cached_source_result
        processed = []

        class _Service:
            store = SimpleNamespace(data_mode_scope="LIVE")

            async def process_fetch(self, fetch_id, sources, *, cached_context_only=False):
                processed.append((fetch_id, sources, cached_context_only))
                events.append("collections_processed")
                return {"status": "COMPLETED"}

        facade.collection_service = _Service()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                response = await browser.post(
                    f"/api/v1/fetches/{_JOB_ID}/collections",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "COMPLETED")
        self.assertEqual(processed[0][0], _JOB_ID)
        self.assertTrue(processed[0][2])
        self.assertEqual(set(cache_reads), set(source_ids))
        restored = {source["source_id"]: source for source in processed[0][1]}
        self.assertEqual(set(restored), set(source_ids))
        self.assertEqual(restored[WIKIMEDIA_SOURCE_ID]["records"][0]["title"],
                         "retained Wikimedia cache")
        self.assertEqual(restored[WIKIMEDIA_SOURCE_ID]["delivery_state"], "CACHED")
        self.assertEqual(restored[YOUTUBE_SOURCE_ID]["delivery_state"], "STALE_CACHE")
        self.assertEqual(restored[GOOGLE_SOURCE_ID]["error_code"], "NOT_YET_ELIGIBLE")
        self.assertEqual(restored[BING_SOURCE_ID]["error_code"], "NO_SAVED_CANDIDATE")
        self.assertTrue(all(source["data_mode"] == "LIVE" for source in restored.values()))
        self.assertNotIn("worker_finished", events)
        self.assertLess(events.index("collections_processed"), events.index("snapshot_saved"))

    async def test_recognized_policy_mismatch_exposes_explicit_sync_then_fetch(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        original_submit = facade.submit
        submit_calls = 0

        def submit_after_mismatch(idempotency_key, *, source_ids, manual_source_refresh=False):
            nonlocal submit_calls
            submit_calls += 1
            if submit_calls == 1:
                events.append("policy_mismatch")
                raise AdmissionDenied("POLICY_MISMATCH")
            events.append("fetch_submitted")
            return original_submit(
                idempotency_key, source_ids=source_ids,
                manual_source_refresh=manual_source_refresh,
            )

        def prepare_for_fetch():
            events.append("policy_reconciled")
            return "RECONCILED"

        facade.submit = submit_after_mismatch
        facade.prepare_for_wikimedia_fetch = prepare_for_fetch
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                headers = {"x-vercel-oidc-token": "synthetic-test-token"}
                response = await browser.post(
                    "/api/v1/fetches",
                    headers={**headers, "Origin": "https://trend-scout.example",
                             "Idempotency-Key": "vercel-wikimedia-sync-0001"},
                    json={},
                )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["shared_state_policy_sync"], "RECONCILED")
        self.assertEqual(submit_calls, 2)
        self.assertLess(events.index("policy_mismatch"), events.index("policy_reconciled"))
        self.assertLess(events.index("policy_reconciled"), events.index("fetch_submitted"))
        self.assertNotIn("worker_finished", events)

    async def test_recognized_policy_mismatch_is_actionable_in_capabilities(self):
        app, _events, _client, facade, _client_factory = self.app(enabled=True)
        facade.admission.operationally_enabled = lambda: False
        facade.vercel_wikimedia_policy_sync_status = lambda: "READY_TO_SYNC"
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                response = await browser.get(
                    "/api/v1/capabilities",
                    headers={"x-vercel-oidc-token": "synthetic-test-token"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["public_fetch_enabled"])
        self.assertTrue(response.json()["shared_policy_reconciliation_required"])
        self.assertTrue(response.json()["vercel_shared_live"])

    async def test_policy_sync_failure_stops_before_job_admission_or_provider_worker(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)

        def policy_mismatch(_key, *, source_ids, manual_source_refresh=False):
            events.append("policy_mismatch")
            raise AdmissionDenied("POLICY_MISMATCH")

        def reject_policy_sync():
            raise AdmissionDenied("POLICY_MISMATCH")

        facade.submit = policy_mismatch
        facade.prepare_for_wikimedia_fetch = reject_policy_sync
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                response = await browser.post(
                    "/api/v1/fetches",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                        "Idempotency-Key": "vercel-wikimedia-sync-failed-0001",
                    },
                    json={},
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "POLICY_MISMATCH")
        self.assertIn("policy_mismatch", events)
        self.assertIsNone(facade.submitted_sources)
        self.assertNotIn("worker_finished", events)

    async def test_unknown_policy_mismatch_stays_disabled(self):
        app, _events, _client, facade, _client_factory = self.app(enabled=True)
        facade.admission.operationally_enabled = lambda: False
        facade.vercel_wikimedia_policy_sync_status = lambda: "POLICY_MISMATCH"
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                response = await browser.get(
                    "/api/v1/capabilities",
                    headers={"x-vercel-oidc-token": "synthetic-test-token"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["public_fetch_enabled"])
        # Runtime identity drives UI wording even when policy currently blocks
        # new admissions; Vercel should never be described as local LIVE.
        self.assertTrue(response.json()["vercel_shared_live"])
        self.assertFalse(response.json()["shared_policy_reconciliation_required"])
        self.assertEqual(response.json()["fetch_block_reason"], "POLICY_MISMATCH")


class PostgresFetchCollectionsReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_worker_results_feed_collections_without_payload_reread(self):
        from app.api.fetches import _run_fetch_and_collections

        fetch_id = "a" * 32
        source_record = {"keyword": "cached trend", "volume": 42}
        worker_result = {
            "job_id": fetch_id,
            "state": "COMPLETED",
            "sources": {
                GOOGLE_SOURCE_ID: {
                    "status": "SUCCESS",
                    "data_mode": "LIVE",
                    "delivery_state": "CACHED",
                    "record_count": 1,
                    "records": [source_record],
                    "coverage": {},
                },
            },
        }
        processed = []

        class CollectionServiceProbe:
            async def process_fetch(self, current_fetch_id, sources, *,
                                    cached_context_only=False):
                processed.append((current_fetch_id, sources, cached_context_only))
                return {"status": "COMPLETED"}

        class Distributed:
            postgres_live = True
            firestore_shared_live = False
            collection_service = CollectionServiceProbe()

            async def run_once(self, _owner):
                return worker_result

            def collection_reuse_for_fetch(self, current_fetch_id):
                self.asserted_reuse_id = current_fetch_id
                return {"terminal": True, "collection_input_unchanged": False}

            def status(self, *_args, **_kwargs):
                raise AssertionError("completed worker results should avoid reloading payloads")

        request = SimpleNamespace(
            state=SimpleNamespace(collection_service=None),
            app=SimpleNamespace(state=SimpleNamespace(collection_service=None)),
        )
        distributed = Distributed()

        result = await _run_fetch_and_collections(
            request, distributed, fetch_id, "worker-owner", cached_context_only=True,
        )

        self.assertIs(result, worker_result)
        self.assertEqual(distributed.asserted_reuse_id, fetch_id)
        self.assertEqual(processed, [(
            fetch_id,
            [{**worker_result["sources"][GOOGLE_SOURCE_ID],
              "source_id": GOOGLE_SOURCE_ID}],
            True,
        )])

    async def test_recovery_without_worker_result_reads_durable_terminal_payload(self):
        from app.api.fetches import _run_fetch_and_collections

        fetch_id = "c" * 32
        source = {
            "source_id": GOOGLE_SOURCE_ID,
            "status": "SUCCESS",
            "data_mode": "LIVE",
            "delivery_state": "CACHED",
            "record_count": 1,
            "records": [{"keyword": "recovered trend", "volume": 21}],
            "coverage": {},
        }
        processed = []

        class CollectionServiceProbe:
            async def process_fetch(self, current_fetch_id, sources, *,
                                    cached_context_only=False):
                processed.append((current_fetch_id, sources, cached_context_only))
                return {"status": "COMPLETED"}

        class Distributed:
            postgres_live = True
            firestore_shared_live = False
            collection_service = CollectionServiceProbe()

            def collection_reuse_for_fetch(self, _fetch_id):
                return {"terminal": True, "collection_input_unchanged": False}

            def status(self, current_fetch_id, *, include_records, include_discovery):
                self.asserted_status_call = (
                    current_fetch_id, include_records, include_discovery
                )
                return {"terminal": True, "fetch_mode": "CACHE_ONLY", "sources": [source]}

        request = SimpleNamespace(
            state=SimpleNamespace(collection_service=None),
            app=SimpleNamespace(state=SimpleNamespace(collection_service=None)),
        )
        distributed = Distributed()

        result = await _run_fetch_and_collections(
            request, distributed, fetch_id, "recovery-owner", run_worker=False,
        )

        self.assertIsNone(result)
        self.assertEqual(distributed.asserted_status_call, (fetch_id, True, False))
        self.assertEqual(processed, [(fetch_id, [source], True)])

    async def test_unchanged_postgres_input_skips_collection_processing_without_snapshot_flags(self):
        from app.api.fetches import _process_fetch_collections

        class Distributed:
            firestore_shared_live = False
            postgres_live = True

            def __init__(self):
                self.reuse_calls = []

            def collection_reuse_for_fetch(self, fetch_id):
                self.reuse_calls.append(fetch_id)
                return {
                    "terminal": True,
                    "collection_input_unchanged": True,
                    "dataset_id": "b" * 32,
                }

            def status(self, *args, **kwargs):
                raise AssertionError("unchanged PostgreSQL input should skip status and processing")

        request = SimpleNamespace(
            state=SimpleNamespace(),
            app=SimpleNamespace(state=SimpleNamespace()),
        )
        distributed = Distributed()

        await _process_fetch_collections(request, distributed, "a" * 32)

        self.assertEqual(distributed.reuse_calls, ["a" * 32])
        self.assertFalse(hasattr(request.state, "vercel_fetch_snapshot_unchanged"))
        self.assertFalse(hasattr(request.state, "vercel_fetch_snapshot_required"))


if __name__ == "__main__":
    unittest.main()
