"""Dashboard bootstrap reads its datasets through one SQLite-backed request."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.api import collections, dashboard
from app.main import (
    _default_dashboard_bootstrap_request,
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

    def test_dashboard_projection_reuses_the_snapshot_summary_query(self):
        summary = {
            "dataset_id": "dataset-1",
            "source_manifest": [{"source_input_digest": "a" * 64}],
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
            patch.object(dashboard, "latest_keywords_for_service", return_value={"items": []}),
            patch.object(dashboard, "collection_workbench_for_service", return_value={}),
        ):
            result = dashboard.build_default_dashboard_bootstrap(
                service, available_sources=(), latest_summary=summary,
            )

        self.assertEqual(result["item"], {"dataset_id": "dataset-1"})

    def test_default_dashboard_projection_skips_all_sqlite_handlers(self):
        projection = {
            "item": {"dataset_id": "dataset-1"},
            "analytics": {"keyword_totals": {"unique": 12}},
            "keywords": {"items": [{"id": "keyword-1"}], "total": 12},
        }
        request = SimpleNamespace(state=SimpleNamespace(
            dashboard_bootstrap_available=True,
            dashboard_bootstrap=projection,
        ))
        with (
            patch.object(dashboard, "latest_dataset", side_effect=AssertionError),
            patch.object(dashboard, "analytics_overview", side_effect=AssertionError),
            patch.object(dashboard, "latest_keywords", side_effect=AssertionError),
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

    def test_collections_landing_page_uses_versioned_workbench_projection(self):
        projection = {
            "items": [{"collection_id": "c1", "name": "Home"}],
            "suggestions": [],
            "mode": "AUTO_CREATE",
            "summary": {"collections": 1},
        }
        request = SimpleNamespace(state=SimpleNamespace(
            collection_workbench_available=True,
            collection_workbench=projection,
        ))
        with patch.object(collections, "_service", side_effect=AssertionError):
            self.assertEqual(collections.list_collections(request), projection)


if __name__ == "__main__":
    unittest.main()
