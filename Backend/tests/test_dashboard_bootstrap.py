"""Dashboard bootstrap reads its datasets through one SQLite-backed request."""

import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.api import analytics, collections, dashboard, research
from app.main import (
    _algorithm_results_projection_request,
    _default_dashboard_bootstrap_request,
    _default_research_bootstrap_request,
    _default_run_history_projection_request,
    _is_vercel_shared_live_request,
    _vercel_collection_read_composition_required,
)


class DashboardBootstrapTests(unittest.TestCase):
    def test_bootstrap_combines_latest_dataset_analytics_and_first_keyword_page(self):
        request = SimpleNamespace(state=SimpleNamespace())
        dataset = {"item": {"dataset_id": "dataset-1"}}
        analytics = {"keyword_totals": {"unique": 12}}
        keywords = {"items": [{"id": "keyword-1"}], "total": 12, "next_offset": 20}

        with (
            patch.object(dashboard, "latest_dataset", return_value=dataset) as read_dataset,
            patch.object(dashboard, "analytics_overview", return_value=analytics) as read_analytics,
            patch.object(dashboard, "latest_keywords", return_value=keywords) as read_keywords,
        ):
            result = dashboard.dashboard_bootstrap(
                request,
                q="",
                state="all",
                classification="all",
                evidence="all",
                collection_ids=[],
                source="",
                sort="recent",
                date_from=None,
                date_to=None,
                limit=20,
                offset=0,
                include_algorithm_results=False,
            )

        self.assertEqual(result, {
            "item": dataset["item"],
            "analytics": analytics,
            "keywords": keywords,
        })
        read_dataset.assert_called_once_with(request)
        read_analytics.assert_called_once_with(request)
        read_keywords.assert_called_once_with(
            request,
            q="",
            state="all",
            classification="all",
            evidence="all",
            collection_ids=[],
            source="",
            sort="recent",
            date_from=None,
            date_to=None,
            limit=20,
            offset=0,
            include_algorithm_results=False,
        )

    def test_bootstrap_uses_provider_free_collection_read_composition(self):
        settings = SimpleNamespace(
            environment_profile="vercel",
            vercel_shared_live_enabled=True,
        )
        self.assertTrue(_is_vercel_shared_live_request(
            settings,
            "GET",
            "/api/v1/dashboard/bootstrap",
            enabled=True,
        ))
        self.assertTrue(_vercel_collection_read_composition_required(
            "GET", "/api/v1/dashboard/bootstrap",
        ))
        self.assertTrue(_is_vercel_shared_live_request(
            settings, "GET", "/api/v1/algorithms/results", enabled=True,
        ))
        self.assertTrue(_vercel_collection_read_composition_required(
            "GET", "/api/v1/algorithms/results",
        ))
        self.assertTrue(_vercel_collection_read_composition_required(
            "GET", "/api/v1/analytics/overview",
        ))

    def test_dashboard_projection_reuses_the_snapshot_summary_query(self):
        summary = {
            "dataset_id": "dataset-1",
            "source_manifest": [{"source_input_digest": "a" * 64}],
        }
        keyword_rows = [{"id": f"kw-{index}"} for index in range(25)]
        keyword_page = {
            "items": keyword_rows, "total": 22, "limit": 25, "offset": 0,
        }
        service = SimpleNamespace(store=SimpleNamespace(
            latest_dataset=lambda: self.fail("the supplied summary should be reused"),
        ))

        def public_latest(request):
            self.assertIs(request.state.latest_dataset_summary, summary)
            return {"item": {"dataset_id": summary["dataset_id"]}}

        with (
            patch.object(dashboard, "latest_dataset", side_effect=public_latest),
            patch.object(dashboard, "analytics_overview", return_value={}),
            patch.object(dashboard, "latest_keywords_for_service", return_value=keyword_page) as read_keywords,
            patch.object(dashboard, "collection_workbench_for_service", return_value={}) as read_collections,
            patch.object(dashboard, "build_local_run_history_projection", return_value={
                "items": [], "has_more": False, "limit": 20,
            }) as read_runs,
            patch.object(dashboard, "research_bootstrap_for_service", return_value={
                "keywords": {"items": keyword_rows}, "research": {"items": []},
            }) as read_research,
            patch("app.api.algorithms.algorithm_results_for_service", return_value={
                "summary": [], "items": [], "limit": 100,
            }) as read_algorithms,
        ):
            result = dashboard.build_default_dashboard_bootstrap(
                service, available_sources=(), latest_summary=summary,
            )

        self.assertEqual(result["item"], {"dataset_id": "dataset-1"})
        self.assertEqual(result["keywords"]["items"], keyword_rows[:20])
        self.assertEqual(result["keywords"]["limit"], 20)
        self.assertEqual(result["keywords"]["next_offset"], 20)
        self.assertEqual(result["algorithm_results"]["limit"], 100)
        read_algorithms.assert_called_once_with(service, limit=100)
        read_research.assert_called_once_with(service, keyword_page=keyword_page)
        read_keywords.assert_called_once_with(
            service, limit=100, offset=0, include_algorithm_results=False,
        )
        self.assertEqual(
            read_collections.call_args.kwargs["keyword_page"]["items"], keyword_rows[:25],
        )
        self.assertEqual(read_collections.call_args.kwargs["keyword_page"]["limit"], 25)
        read_runs.assert_called_once_with(service, limit=20)

    def test_default_dashboard_projection_skips_all_sqlite_handlers(self):
        projection = {
            "item": {"dataset_id": "dataset-1"},
            "analytics": {"keyword_totals": {"unique": 12}},
            "keywords": {"items": [{"id": "keyword-1"}], "total": 12},
        }
        request = SimpleNamespace(state=SimpleNamespace(
            dashboard_bootstrap_available=True,
            dashboard_bootstrap=projection,
        ), app=SimpleNamespace(state=SimpleNamespace(
            settings=SimpleNamespace(environment_profile="test"),
        )))
        with (
            patch.object(dashboard, "latest_dataset", side_effect=AssertionError),
            patch.object(dashboard, "analytics_overview", side_effect=AssertionError),
            patch.object(dashboard, "latest_keywords", side_effect=AssertionError),
            patch.object(dashboard, "validate_public_request_origin"),
        ):
            result = dashboard.dashboard_bootstrap(
                request,
                q="",
                state="all",
                classification="all",
                evidence="all",
                collection_ids=[],
                source="",
                sort="recent",
                date_from=None,
                date_to=None,
                limit=20,
                offset=0,
                include_algorithm_results=False,
            )
        self.assertEqual(result, {
            **projection,
            "item": {**projection["item"], "age_seconds": None},
        })

    def test_analytics_projection_skips_sqlite_and_refreshes_display_age(self):
        observed = datetime.now(timezone.utc) - timedelta(minutes=2)
        timestamp = observed.isoformat()
        projection = {"last_successful_fetch": {
            "created_at": timestamp, "age_seconds": 0,
        }}
        request = SimpleNamespace(state=SimpleNamespace(
            analytics_overview_available=True,
            analytics_overview=projection,
        ))

        result = analytics.overview(request)

        self.assertGreaterEqual(result["last_successful_fetch"]["age_seconds"], 120)
        self.assertEqual(projection["last_successful_fetch"]["age_seconds"], 0)

    def test_research_bootstrap_preserves_list_filters_in_one_read(self):
        service = object()
        request = SimpleNamespace(state=SimpleNamespace())
        keywords = {"items": [{"id": "keyword-1"}], "total": 1}
        records = {"items": [{"id": "research-1"}], "total": 1}
        with (
            patch.object(research, "_service", return_value=service),
            patch("app.api.datasets.latest_keywords_for_service", return_value=keywords) as read_keywords,
            patch.object(research, "list_research_for_service", return_value=records) as read_records,
        ):
            result = research.research_bootstrap(request, q="solar", status="DRAFT")

        self.assertEqual(result, {"keywords": keywords, "research": records})
        read_keywords.assert_called_once_with(
            service, limit=100, offset=0, include_algorithm_results=False,
        )
        read_records.assert_called_once_with(
            service, q="solar", status="DRAFT", limit=50, offset=0,
        )

    def test_research_bootstrap_uses_saved_default_projection_without_sqlite(self):
        projection = {
            "keywords": {"items": [{"id": "keyword-1", "keyword": "Solar"}]},
            "research": {"items": [{"id": "research-1"}], "total": 1},
        }
        request = SimpleNamespace(state=SimpleNamespace(
            research_bootstrap_available=True,
            research_bootstrap=projection,
        ), app=SimpleNamespace(state=SimpleNamespace(
            settings=SimpleNamespace(environment_profile="test"),
        )))
        with patch.object(research, "_service", side_effect=AssertionError), patch.object(
            research, "validate_public_request_origin",
        ) as validate:
            self.assertEqual(research.research_bootstrap(request, q="", status=None), projection)
        validate.assert_called_once_with(request, "test")

    def test_fast_dashboard_composition_only_accepts_the_unfiltered_default_page(self):
        base = {
            "q": "", "state": "all", "sort": "recent", "limit": "20",
            "offset": "0", "include_algorithm_results": "false",
        }
        encoded = "&".join(f"{key}={value}" for key, value in base.items()).encode()
        self.assertTrue(_default_dashboard_bootstrap_request({
            "path": "/api/v1/dashboard/bootstrap", "query_string": encoded,
        }))
        filtered = encoded + b"&q=solar"
        self.assertFalse(_default_dashboard_bootstrap_request({
            "path": "/api/v1/dashboard/bootstrap", "query_string": filtered,
        }))
        duplicate = encoded + b"&state=all"
        self.assertFalse(_default_dashboard_bootstrap_request({
            "path": "/api/v1/dashboard/bootstrap", "query_string": duplicate,
        }))

    def test_algorithm_projection_only_serves_its_bounded_unfiltered_page(self):
        self.assertTrue(_algorithm_results_projection_request({
            "path": "/api/v1/algorithms/results", "query_string": b"",
        }))
        self.assertTrue(_algorithm_results_projection_request({
            "path": "/api/v1/algorithms/results", "query_string": b"limit=50",
        }))
        self.assertFalse(_algorithm_results_projection_request({
            "path": "/api/v1/algorithms/results", "query_string": b"limit=500",
        }))
        self.assertFalse(_algorithm_results_projection_request({
            "path": "/api/v1/algorithms/results", "query_string": b"limit=50&limit=50",
        }))

    def test_run_history_projection_only_serves_the_initial_unfiltered_cursor_page(self):
        self.assertTrue(_default_run_history_projection_request({
            "path": "/api/v1/runs/history", "query_string": b"",
        }))
        self.assertTrue(_default_run_history_projection_request({
            "path": "/api/v1/runs/history", "query_string": b"limit=20",
        }))
        self.assertTrue(_default_run_history_projection_request({
            "path": "/api/v1/runs/history",
            "query_string": b"limit=20&offset=0&q=&status=&mode=&cursor=",
        }))
        self.assertFalse(_default_run_history_projection_request({
            "path": "/api/v1/runs/history",
            "query_string": b"limit=20&offset=0&q=fetch&status=&mode=&cursor=",
        }))
        self.assertFalse(_default_run_history_projection_request({
            "path": "/api/v1/runs/history",
            "query_string": b"limit=20&offset=0&q=&status=&mode=&cursor=token",
        }))
        self.assertFalse(_default_run_history_projection_request({
            "path": "/api/v1/runs/history",
            "query_string": b"limit=20&offset=20",
        }))

    def test_research_projection_only_serves_the_unfiltered_initial_page(self):
        self.assertTrue(_default_research_bootstrap_request({
            "path": "/api/v1/research/bootstrap", "query_string": b"",
        }))
        self.assertTrue(_default_research_bootstrap_request({
            "path": "/api/v1/research/bootstrap", "query_string": b"q=&status=",
        }))
        self.assertFalse(_default_research_bootstrap_request({
            "path": "/api/v1/research/bootstrap", "query_string": b"q=solar",
        }))
        self.assertFalse(_default_research_bootstrap_request({
            "path": "/api/v1/research/bootstrap", "query_string": b"status=DRAFT",
        }))

    def test_collections_landing_page_uses_versioned_workbench_projection(self):
        projection = {
            "items": [{"collection_id": "c1", "name": "Home"}],
            "suggestions": [],
            "mode": "AUTO_CREATE",
            "summary": {"collections": 1},
            "keyword_page": {"items": [], "total": 0, "limit": 25, "offset": 0},
        }
        request = SimpleNamespace(state=SimpleNamespace(
            collection_workbench_available=True,
            collection_workbench=projection,
        ), app=SimpleNamespace(state=SimpleNamespace(
            settings=SimpleNamespace(environment_profile="test"),
        )))
        with patch.object(collections, "_service", side_effect=AssertionError), patch.object(
            collections, "validate_public_request_origin",
        ) as validate:
            self.assertEqual(collections.list_collections(request), projection)
        validate.assert_called_once_with(request, "test")

    def test_collections_workbench_includes_its_default_keyword_page(self):
        service = SimpleNamespace(store=SimpleNamespace(
            list_collections=lambda: [{"id": "c1"}],
            suggestions=lambda: [],
            workbench_summary=lambda: {"eligible_keywords": 2},
            connect=lambda: None,
        ))
        with (
            patch.object(collections, "_mode", return_value="AUTO_CREATE"),
            patch("app.api.datasets.latest_keywords_for_service", return_value={
                "items": [{"id": "kw1"}], "total": 1, "limit": 25, "offset": 0,
            }) as read_keywords,
        ):
            result = collections.collection_workbench_for_service(service)

        self.assertEqual(result["keyword_page"]["items"][0]["id"], "kw1")
        read_keywords.assert_called_once_with(
            service, limit=25, offset=0, include_algorithm_results=False,
        )


class RunHistoryProjectionTests(unittest.IsolatedAsyncioTestCase):
    def test_local_projection_is_bounded_and_marks_continuation(self):
        rows = [{"fetch_id": f"{index:032x}"} for index in range(21)]
        queries = []

        class Cursor:
            def fetchall(self):
                return rows

        class Database:
            def execute(self, query, parameters):
                queries.append((query, parameters))
                return Cursor()

        service = SimpleNamespace(store=SimpleNamespace(
            connect=lambda: nullcontext(Database()),
        ))
        with patch.object(
            analytics, "_history_item_from_job",
            side_effect=lambda row: {"fetch_id": row["fetch_id"]},
        ) as map_item:
            result = analytics.build_local_run_history_projection(service)

        self.assertEqual(len(result["items"]), 20)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["limit"], 20)
        self.assertEqual(map_item.call_count, 20)
        self.assertEqual(queries[0][1], (21,))

    async def test_initial_history_page_uses_bounded_projection_and_keeps_cursor(self):
        run_id = "a" * 32
        timestamp = "2026-11-25T12:00:00+00:00"
        projection = {
            "items": [{
                "fetch_id": run_id,
                "run_type": "FETCH",
                "status": "PARTIAL",
                "outcome_status": "PARTIAL",
                "mode": "LIVE",
                "source_manifest": "[]",
                "sources": [],
                "run_at": timestamp,
                "created_at": timestamp,
                "updated_at": timestamp,
                "duration_seconds": None,
                "_cursor_local": [timestamp, run_id],
            }],
            "has_more": True,
            "limit": 20,
        }
        request = SimpleNamespace(
            state=SimpleNamespace(
                run_history_projection_available=True,
                run_history_projection=projection,
                distributed_public_fetch=None,
                collection_service=None,
            ),
            app=SimpleNamespace(state=SimpleNamespace(
                read_only=True,
                settings=SimpleNamespace(environment_profile="vercel"),
                read_storage_factory=None,
                distributed_public_fetch=None,
            )),
            headers=SimpleNamespace(get=lambda _name: None),
        )

        result = await analytics._run_history_cursor_page(
            request, "", "", "", 20, "",
        )

        self.assertEqual([item["fetch_id"] for item in result["items"]], [run_id])
        self.assertTrue(result["next_cursor"])
        self.assertNotIn("_cursor_local", result["items"][0])
        self.assertEqual(
            analytics._history_cursor_decode(result["next_cursor"], {
                "q": "", "status": "", "mode": "",
            })["local"],
            [timestamp, run_id],
        )


if __name__ == "__main__":
    unittest.main()
