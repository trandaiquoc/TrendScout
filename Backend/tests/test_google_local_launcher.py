import os
import sys
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

import scripts.run_google_live_local as local_launcher


class LocalWebLauncherTests(unittest.TestCase):
    def setUp(self):
        loader = patch.object(local_launcher, "load_local_environment")
        self.addCleanup(loader.stop)
        self.local_env_loader = loader.start()

    def test_launcher_uses_explicit_repository_root_env_file(self):
        env_file = Path(tempfile.gettempdir()) / "trendscout-dummy-root.env"
        with patch.object(local_launcher, "create_normal_google_live_app", return_value=object()), \
             patch.object(local_launcher, "run_normal_google_live"):
            local_launcher.main(["--root-env-file", str(env_file), "--port", "8125"])

        self.local_env_loader.assert_called_once_with(env_file)

    def test_vercel_runtime_does_not_load_local_env_file(self):
        with patch.dict(os.environ, {"VERCEL": "1"}, clear=False), \
             patch.object(local_launcher, "create_normal_google_live_app", return_value=object()), \
             patch.object(local_launcher, "run_normal_google_live"):
            local_launcher.main(["--port", "8125"])

        self.local_env_loader.assert_not_called()

    def test_owner_manual_runtime_is_limited_to_linux_or_windows_2(self):
        self.assertTrue(local_launcher.owner_manual_runtime_supported("win32", "DESKTOP-ICECOET"))
        self.assertTrue(local_launcher.owner_manual_runtime_supported("linux", "another-linux-host"))
        self.assertFalse(local_launcher.owner_manual_runtime_supported("win32", "DESKTOP-OTHER"))
        self.assertFalse(local_launcher.owner_manual_runtime_supported("darwin", "owner-mac"))

    def test_local_web_policy_uses_the_windows_source_profile(self):
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
        self.assertTrue(settings.serp_approved)
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
        self.assertFalse(composition.keywords["youtube_manual_live"])
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
        self.assertFalse(composition.keywords["youtube_manual_live"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux owner-host compatibility test")
    def test_owner_manual_live_designates_only_the_detected_linux_host(self):
        with patch.dict(os.environ, {"VERCEL": ""}, clear=False), \
             patch.object(local_launcher.socket, "gethostname", return_value="linux-owner"), \
             patch.object(local_launcher, "create_normal_google_live_app", return_value=object()) as create_app, \
             patch.object(local_launcher, "run_normal_google_live"):
            local_launcher.main(["--youtube-owner-manual-live"])

        composition = create_app.call_args.kwargs["composition_factory"]
        self.assertIsInstance(composition, partial)
        self.assertTrue(composition.keywords["youtube_manual_live"])
        self.assertTrue(composition.keywords["preserve_expired_data"])
        self.assertEqual(composition.keywords["youtube_max_queries"], 1)
        self.assertFalse(composition.keywords["youtube_force_fresh_test_once"])
        settings = composition.keywords["google_settings"]
        self.assertTrue(settings.youtube_live_approved)
        self.assertEqual(settings.youtube_live_host, "linux-owner")

    def test_fresh_test_requires_manual_mode_and_manual_mode_rejects_vercel(self):
        with self.assertRaises(SystemExit):
            local_launcher.main(["--youtube-owner-force-fresh-test"])

        with patch.dict(os.environ, {"VERCEL": "1"}, clear=False), \
             self.assertRaises(SystemExit):
            local_launcher.main(["--youtube-owner-manual-live"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "OWNER_MANUAL_LIVE is Linux-only")
    def test_owner_manual_fresh_test_is_one_shot_composition_flag(self):
        with patch.dict(os.environ, {"VERCEL": ""}, clear=False), \
             patch.object(local_launcher, "create_normal_google_live_app", return_value=object()) as create_app, \
             patch.object(local_launcher, "run_normal_google_live"):
            local_launcher.main([
                "--youtube-owner-manual-live", "--youtube-owner-force-fresh-test",
            ])
        composition = create_app.call_args.kwargs["composition_factory"]
        self.assertTrue(composition.keywords["youtube_force_fresh_test_once"])

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows 2 owner-host test")
    def test_windows_2_manual_mode_is_explicit_and_uses_designated_host(self):
        with patch.dict(os.environ, {"VERCEL": ""}, clear=False), \
             patch.object(local_launcher.socket, "gethostname", return_value="DESKTOP-ICECOET"), \
             patch.object(local_launcher, "create_normal_google_live_app", return_value=object()) as create_app, \
             patch.object(local_launcher, "run_normal_google_live"):
            local_launcher.main(["--youtube-owner-manual-live"])

        composition = create_app.call_args.kwargs["composition_factory"]
        self.assertTrue(composition.keywords["youtube_manual_live"])
        self.assertEqual(composition.keywords["youtube_max_queries"], 1)
        self.assertEqual(composition.keywords["google_settings"].youtube_live_host,
                         "DESKTOP-ICECOET")

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows manual host guard")
    def test_windows_rejects_other_hosts_for_owner_manual_live(self):
        with patch.object(local_launcher.socket, "gethostname", return_value="DESKTOP-OTHER"), \
             self.assertRaises(SystemExit):
            local_launcher.main(["--youtube-owner-manual-live"])

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows 2 owner-host test")
    def test_windows_2_manual_mode_rejects_vercel(self):
        with patch.dict(os.environ, {"VERCEL": "1"}, clear=False), \
             patch.object(local_launcher.socket, "gethostname", return_value="DESKTOP-ICECOET"), \
             self.assertRaises(SystemExit):
            local_launcher.main(["--youtube-owner-manual-live"])

if __name__ == "__main__":
    unittest.main()
