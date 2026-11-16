import os
import unittest
from functools import partial
from unittest.mock import patch

import scripts.run_google_live_local as local_launcher


class LocalWebLauncherTests(unittest.TestCase):
    def test_local_web_policy_ignores_environment_approval_switches(self):
        with patch.dict(os.environ, {
            "TREND_SCOUT_GOOGLE_TRENDS_APPROVED": "false",
            "TREND_SCOUT_GOOGLE_SERP_APPROVED": "true",
            "TREND_SCOUT_GOOGLE_RSS_APPROVED": "true",
            "TREND_SCOUT_BING_SERP_APPROVED": "true",
            "TREND_SCOUT_SERPAPI_ACCOUNT_API_APPROVED": "false",
        }, clear=False):
            settings = local_launcher.local_live_settings()

        self.assertTrue(settings.trends_approved)
        self.assertTrue(settings.account_api_approved)
        self.assertFalse(settings.serp_approved)
        self.assertFalse(settings.rss_approved)
        self.assertTrue(settings.bing_approved)
        self.assertTrue(settings.policy_migration_approved)

    def test_owner_acceptance_uses_one_query_but_preserves_schedule_checks(self):
        with patch.object(local_launcher, "create_normal_google_live_app", return_value=object()) as create_app, \
             patch.object(local_launcher, "run_normal_google_live") as run_app:
            local_launcher.main(["--port", "8123", "--youtube-owner-acceptance"])

        composition = create_app.call_args.kwargs["composition_factory"]
        self.assertIsInstance(composition, partial)
        self.assertNotIn("allow_initial_youtube_acceptance", composition.keywords)
        self.assertTrue(composition.keywords["preserve_expired_data"])
        self.assertEqual(composition.keywords["youtube_max_queries"], 1)
        run_app.assert_called_once_with(create_app.return_value, port=8123)

    def test_default_runtime_keeps_scheduled_youtube_limits(self):
        with patch.object(local_launcher, "create_normal_google_live_app", return_value=object()) as create_app, \
             patch.object(local_launcher, "run_normal_google_live"):
            local_launcher.main(["--port", "8124"])

        composition = create_app.call_args.kwargs["composition_factory"]
        self.assertIsInstance(composition, partial)
        self.assertNotIn("allow_initial_youtube_acceptance", composition.keywords)
        self.assertFalse(composition.keywords["preserve_expired_data"])
        self.assertEqual(composition.keywords["youtube_max_queries"], 2)


if __name__ == "__main__":
    unittest.main()
