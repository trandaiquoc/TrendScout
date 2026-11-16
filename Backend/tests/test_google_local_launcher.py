import os
import unittest
from unittest.mock import patch

from scripts.run_google_live_local import local_live_settings


class LocalWebLauncherTests(unittest.TestCase):
    def test_local_web_policy_ignores_environment_approval_switches(self):
        with patch.dict(os.environ, {
            "TREND_SCOUT_GOOGLE_TRENDS_APPROVED": "false",
            "TREND_SCOUT_GOOGLE_SERP_APPROVED": "true",
            "TREND_SCOUT_GOOGLE_RSS_APPROVED": "true",
            "TREND_SCOUT_BING_SERP_APPROVED": "true",
            "TREND_SCOUT_SERPAPI_ACCOUNT_API_APPROVED": "false",
        }, clear=False):
            settings = local_live_settings()

        self.assertTrue(settings.trends_approved)
        self.assertTrue(settings.account_api_approved)
        self.assertFalse(settings.serp_approved)
        self.assertFalse(settings.rss_approved)
        self.assertTrue(settings.bing_approved)
        self.assertTrue(settings.policy_migration_approved)


if __name__ == "__main__":
    unittest.main()
