"""Dashboard bootstrap reads its datasets through one SQLite-backed request."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.api import dashboard
from app.main import (
    _is_vercel_shared_live_request,
    _vercel_collection_read_composition_required,
)


class DashboardBootstrapTests(unittest.TestCase):
    def test_bootstrap_combines_latest_dataset_analytics_and_first_keyword_page(self):
        request = SimpleNamespace()
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


if __name__ == "__main__":
    unittest.main()
