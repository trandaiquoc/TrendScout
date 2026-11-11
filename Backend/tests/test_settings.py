"""Typed runtime configuration must preserve existing environment contracts."""

import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

import httpx

from app.main import create_app
from app.settings import Settings

WIKIMEDIA_SOURCE_ID = "wikimedia_pageviews_top_per_country"


class SettingsTests(unittest.TestCase):
    def test_defaults_are_typed_and_future_paid_sources_stay_disabled(self):
        settings = Settings.from_environment({})

        self.assertEqual(settings.app_name, "TrendScout API")
        self.assertEqual(settings.environment_profile, "local")
        self.assertFalse(settings.read_only)
        self.assertEqual(settings.enabled_source_ids, frozenset({WIKIMEDIA_SOURCE_ID}))
        self.assertEqual(settings.query_plan_id, "demo-us-en")
        self.assertEqual(settings.query_plan_version, 1)
        self.assertEqual(settings.query_request_budget_per_source, 14)
        self.assertEqual(settings.run_mode, "live")
        self.assertEqual(settings.run_cooldown_seconds, 60)
        self.assertEqual(settings.http_policy.max_retries, 2)
        self.assertEqual(settings.allowed_fetch_operations, frozenset())
        self.assertFalse(settings.public_fetch_enabled)
        self.assertTrue(Path(settings.lock_path).is_absolute())
        self.assertNotIn("youtube_video_search", settings.enabled_source_ids)

    def test_existing_environment_names_remain_supported(self):
        token = "synthetic-operator-token-never-log"
        password = "synthetic-read-password-never-log"
        audience = "synthetic-audience-never-log"
        settings = Settings.from_environment(
            {
                "TREND_SCOUT_APP_NAME": "TrendScout test API",
                "VERCEL": "1",
                "TREND_SCOUT_READ_PASSWORD": password,
                "TREND_SCOUT_OPERATOR_TOKEN": token,
                "GOOGLE_CLOUD_PROJECT": "synthetic-project",
                "GOOGLE_WIF_AUDIENCE": audience,
                "TREND_SCOUT_LOCK_PATH": "/tmp/trendscout-test.lock",
                "TREND_SCOUT_RUN_COOLDOWN_SECONDS": "75",
                "WIKIMEDIA_USER_AGENT": "TrendScout test (mailto:test@example.invalid)",
            }
        )

        self.assertEqual(settings.app_name, "TrendScout test API")
        self.assertEqual(settings.environment_profile, "vercel")
        self.assertTrue(settings.read_only)
        self.assertEqual(settings.operator_token, token)
        self.assertEqual(settings.read_password, password)
        self.assertEqual(settings.google_cloud_project, "synthetic-project")
        self.assertEqual(settings.google_wif_audience, audience)
        self.assertEqual(settings.lock_path, "/tmp/trendscout-test.lock")
        self.assertEqual(settings.run_cooldown_seconds, 75)
        self.assertEqual(
            settings.wikimedia_user_agent, "TrendScout test (mailto:test@example.invalid)"
        )

    def test_manual_run_operation_requires_non_read_only_operator_configuration(self):
        settings = Settings.from_environment(
            {
                "TREND_SCOUT_OPERATOR_TOKEN": "synthetic-operator-token-long-enough",
            }
        )
        self.assertEqual(settings.allowed_fetch_operations, frozenset({"manual_run"}))

    def test_secrets_are_hidden_and_settings_are_immutable(self):
        token = "synthetic-server-token-only"
        password = "synthetic-server-password-only"
        settings = Settings.from_environment(
            {
                "TREND_SCOUT_OPERATOR_TOKEN": token,
                "TREND_SCOUT_READ_PASSWORD": password,
            }
        )

        self.assertNotIn(token, repr(settings))
        self.assertNotIn(password, repr(settings))
        with self.assertRaises(FrozenInstanceError):
            settings.app_name = "changed"

    def test_invalid_or_unapproved_source_settings_fail_closed(self):
        invalid = (
            {"TREND_SCOUT_READ_ONLY": "perhaps"},
            {"TREND_SCOUT_RUN_COOLDOWN_SECONDS": "-1"},
            {"TREND_SCOUT_HTTP_TOTAL_TIMEOUT_SECONDS": "0"},
            {"TREND_SCOUT_SOURCE_YOUTUBE_ENABLED": "true"},
        )
        for environment in invalid:
            with self.subTest(environment=tuple(environment)):
                with self.assertRaises(ValueError):
                    Settings.from_environment(environment)

    def test_supported_profile_and_existing_read_only_flag(self):
        settings = Settings.from_environment(
            {
                "TREND_SCOUT_ENVIRONMENT": "production",
                "TREND_SCOUT_READ_ONLY": "true",
            }
        )
        self.assertEqual(settings.environment_profile, "production")
        self.assertTrue(settings.read_only)

        vercel_profile = Settings.from_environment({"TREND_SCOUT_ENVIRONMENT": "vercel"})
        self.assertEqual(vercel_profile.environment_profile, "vercel")
        self.assertTrue(vercel_profile.read_only)

    def test_public_fetch_flag_requires_local_synthetic_non_cloud_runtime(self):
        settings = Settings.from_environment(
            {
                "TREND_SCOUT_ENVIRONMENT": "test",
                "TREND_SCOUT_RUN_MODE": "synthetic",
                "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "true",
            }
        )
        self.assertTrue(settings.public_fetch_enabled)

        unsafe = (
            {
                "TREND_SCOUT_ENVIRONMENT": "production",
                "TREND_SCOUT_RUN_MODE": "synthetic",
                "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "true",
            },
            {
                "TREND_SCOUT_ENVIRONMENT": "test",
                "TREND_SCOUT_RUN_MODE": "live",
                "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "true",
            },
            {
                "TREND_SCOUT_ENVIRONMENT": "test",
                "TREND_SCOUT_RUN_MODE": "synthetic",
                "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "true",
                "TREND_SCOUT_READ_ONLY": "true",
            },
            {
                "TREND_SCOUT_ENVIRONMENT": "test",
                "TREND_SCOUT_RUN_MODE": "synthetic",
                "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "true",
                "GOOGLE_CLOUD_PROJECT": "must-not-be-used",
            },
        )
        for environ in unsafe:
            with self.subTest(environ=environ), self.assertRaises(ValueError):
                Settings.from_environment(environ)


class SettingsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_response_does_not_expose_server_settings_or_credentials(self):
        token = "synthetic-operator-token-not-for-api"
        password = "synthetic-read-password-not-for-api"
        settings = Settings.from_environment(
            {
                "VERCEL": "1",
                "TREND_SCOUT_OPERATOR_TOKEN": token,
                "TREND_SCOUT_READ_PASSWORD": password,
                "GOOGLE_CLOUD_PROJECT": "synthetic-project-id",
            }
        )
        app = create_app(settings=settings)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"read_only": True})
        self.assertNotIn(token, response.text)
        self.assertNotIn(password, response.text)
        self.assertNotIn("synthetic-project-id", response.text)

    async def test_existing_operator_token_setting_still_protects_manual_runs(self):
        token = "synthetic-operator-token-long-enough"
        settings = Settings.from_environment({"TREND_SCOUT_OPERATOR_TOKEN": token})
        app = create_app(settings=settings)

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                denied = await client.post("/api/runs", headers={"Authorization": "Bearer wrong"})
                allowed_past_auth = await client.post(
                    "/api/runs", headers={"Authorization": f"Bearer {token}"}
                )

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed_past_auth.status_code, 503)
        self.assertEqual(allowed_past_auth.json(), {"detail": "Storage is not configured"})
        self.assertNotIn(token, denied.text + allowed_past_auth.text)


if __name__ == "__main__":
    unittest.main()
