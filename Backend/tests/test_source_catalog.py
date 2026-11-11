"""Source catalog exposes capabilities without inventing future adapters."""

import unittest
from unittest.mock import patch

from app import composition
from app.settings import Settings
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    SOURCE_CAPABILITIES,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
    source_capability,
)


class SourceCatalogTests(unittest.TestCase):
    def test_four_capabilities_have_central_endpoint_and_readiness_metadata(self):
        expected = {
            WIKIMEDIA_SOURCE_ID: ("wikimedia_aqs", "IMPLEMENTED", True),
            YOUTUBE_SOURCE_ID: ("youtube_data_api_v3", "PENDING_APPROVAL", False),
            GOOGLE_SOURCE_ID: ("serpapi_google", "PENDING_APPROVAL", False),
            BING_SOURCE_ID: ("serpapi_bing", "PENDING_APPROVAL", False),
        }

        self.assertEqual({item.source_id for item in SOURCE_CAPABILITIES}, set(expected))
        for source_id, wanted in expected.items():
            with self.subTest(source_id=source_id):
                item = source_capability(source_id)
                self.assertEqual((item.provider_id, item.readiness, item.default_enabled), wanted)
                self.assertEqual(item.capability_version, 1)
                self.assertTrue(item.endpoint.startswith("https://"))
                self.assertTrue(item.metric_name)
                self.assertTrue(item.metric_unit)

    def test_endpoint_and_credential_references_are_not_secrets(self):
        self.assertEqual(
            source_capability(WIKIMEDIA_SOURCE_ID).endpoint,
            "https://wikimedia.org/api/rest_v1/metrics/pageviews/top-per-country/US/all-access",
        )
        self.assertEqual(
            source_capability(YOUTUBE_SOURCE_ID).credential_reference, "YOUTUBE_API_KEY"
        )
        self.assertEqual(
            source_capability(GOOGLE_SOURCE_ID).credential_reference, "SERPAPI_API_KEY"
        )
        self.assertNotIn("key=", " ".join(item.endpoint for item in SOURCE_CAPABILITIES))

    def test_public_descriptors_keep_live_approval_separate_from_adapter_status(self):
        wikimedia = source_capability(WIKIMEDIA_SOURCE_ID)
        self.assertEqual(wikimedia.adapter_status, "IMPLEMENTED")
        self.assertEqual(wikimedia.approval_status, "PENDING_LIVE_APPROVAL")
        self.assertEqual(wikimedia.live_verification_status, "UNVERIFIED")
        youtube = source_capability(YOUTUBE_SOURCE_ID)
        self.assertEqual(youtube.adapter_status, "IMPLEMENTED_OFFLINE_VERIFIED")
        self.assertEqual(youtube.readiness, "PENDING_APPROVAL")
        self.assertEqual(youtube.approval_status, "PENDING_APPROVAL")
        self.assertEqual(youtube.configuration_status, "NOT_CONFIGURED")
        self.assertEqual(youtube.live_verification_status, "NOT_VERIFIED")
        self.assertEqual(youtube.supported_data_modes, ("SYNTHETIC",))
        self.assertFalse(youtube.default_enabled)
        for source_id in (GOOGLE_SOURCE_ID, BING_SOURCE_ID):
            capability = source_capability(source_id)
            self.assertEqual(capability.adapter_status, "NOT_IMPLEMENTED")
            self.assertEqual(capability.approval_status, "PENDING_APPROVAL")
            self.assertEqual(capability.configuration_status, "NOT_CONFIGURED")

    def test_default_registry_registers_only_enabled_implemented_sources(self):
        settings = Settings.from_environment({})
        registry = composition.source_registry(settings)

        self.assertEqual(registry.metadata(WIKIMEDIA_SOURCE_ID).source_id, WIKIMEDIA_SOURCE_ID)
        with patch("httpx.AsyncClient", side_effect=AssertionError("network client constructed")):
            for source_id in (YOUTUBE_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID):
                with self.subTest(source_id=source_id):
                    with self.assertRaisesRegex(ValueError, "Unsupported source"):
                        registry.create(source_id)

    def test_disabled_implemented_source_is_not_registered(self):
        settings = Settings.from_environment({"TREND_SCOUT_SOURCE_WIKIMEDIA_ENABLED": "false"})
        registry = composition.source_registry(settings)

        self.assertNotIn(WIKIMEDIA_SOURCE_ID, settings.enabled_source_ids)
        with self.assertRaisesRegex(ValueError, "Unsupported source"):
            registry.create(WIKIMEDIA_SOURCE_ID)

    def test_unknown_capability_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported source"):
            source_capability("unknown-source")


if __name__ == "__main__":
    unittest.main()
