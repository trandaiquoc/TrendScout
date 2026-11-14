"""Source catalog exposes capabilities without inventing future adapters."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import composition
from app.api.capabilities import _saved_source_activity
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
    def test_capabilities_project_latest_saved_source_fetch_without_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshots.sqlite"
            db = sqlite3.connect(path)
            db.row_factory = sqlite3.Row
            db.execute("""CREATE TABLE dataset_snapshots(
                status TEXT,data_mode TEXT,source_manifest TEXT,fetched_at TEXT,
                observation_start TEXT,observation_end TEXT,created_at TEXT)""")
            db.execute("""INSERT INTO dataset_snapshots VALUES(?,?,?,?,?,?,?)""", (
                "PARTIAL", "SYNTHETIC", json.dumps([{
                    "source_id": WIKIMEDIA_SOURCE_ID, "status": "PARTIAL",
                    "record_count": 7, "error_code": "PRIVATE_PROVIDER_DETAIL",
                }]), "2026-11-14T01:00:00Z", "2026-11-12", "2026-11-13", "2",
            ))
            db.execute("""INSERT INTO dataset_snapshots VALUES(?,?,?,?,?,?,?)""", (
                "COMPLETED", "SYNTHETIC", json.dumps([{
                    "source_id": WIKIMEDIA_SOURCE_ID, "status": "SUCCESS",
                    "record_count": 3,
                }]), "2026-11-13T01:00:00Z", "2026-11-11", "2026-11-11", "1",
            ))
            db.commit()
            db.close()

            class Store:
                @contextmanager
                def connect(self):
                    db = sqlite3.connect(path)
                    db.row_factory = sqlite3.Row
                    try:
                        yield db
                    finally:
                        db.close()

            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
                collection_service=SimpleNamespace(store=Store())
            )))
            activity = _saved_source_activity(request)[WIKIMEDIA_SOURCE_ID]

        self.assertEqual(activity, {
            "status": "PARTIAL", "data_mode": "SYNTHETIC", "record_count": 7,
            "fetched_at": "2026-11-14T01:00:00Z", "observation_start": "2026-11-12",
            "observation_end": "2026-11-13",
        })
        self.assertNotIn("PRIVATE_PROVIDER_DETAIL", json.dumps(activity))

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

    def test_focused_official_rechecks_keep_candidates_locked(self):
        path = Path(__file__).resolve().parents[1] / "app" / "sources" / "research_catalog.json"
        catalog = json.loads(path.read_text(encoding="utf-8"))
        reviewed = {item["source_id"]: item for item in catalog["items"]
                    if item.get("revalidated_on") == "2026-11-14"}
        self.assertEqual(set(reviewed), {
            "youtube", "google_trends", "wikimedia_pageviews", "google_serp", "bing_serp",
            "facebook", "instagram", "tiktok", "reddit", "pinterest",
            "bluesky", "mastodon", "stack_exchange", "hacker_news", "github",
            "x", "threads", "snapchat", "linkedin", "whatsapp",
            "nextdoor", "truth_social", "bereal", "discord", "telegram",
            "tumblr", "quora", "slack", "lemmy", "discourse",
            "fandom", "twitch", "kick", "rumble", "vimeo",
            "dailymotion", "lemon8", "patreon", "etsy", "poshmark",
        })
        locale_root = Path(__file__).resolve().parents[2] / "Frontend" / "public" / "modules" / "i18n"
        catalogs = {locale: (locale_root / f"{locale}.js").read_text(encoding="utf-8")
                    for locale in ("en", "vi")}
        for source_id, item in reviewed.items():
            with self.subTest(source_id=source_id):
                self.assertFalse(item["runtime_adapter"])
                self.assertEqual(item["research_status"], "LOCKED_RESEARCH_CANDIDATE")
                self.assertEqual(item["approval_status"], "NOT_APPROVED")
                self.assertTrue(item["revalidation_note"])
                self.assertIn("sources.review.", item["revalidation_note_key"])
                self.assertTrue(item["revalidation_links"])
                self.assertTrue(all(link["url"].startswith("https://")
                                    for link in item["revalidation_links"]))
                for locale, content in catalogs.items():
                    with self.subTest(source_id=source_id, locale=locale):
                        self.assertIn(f"'{item['revalidation_note_key']}':", content)

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
        self.assertEqual(wikimedia.live_verification_status, "LOCAL_LIVE_VERIFIED")
        self.assertEqual(wikimedia.local_live_verification_status, "LOCAL_LIVE_VERIFIED")
        self.assertEqual(wikimedia.production_readiness, "NOT_PRODUCTION_READY")
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
            self.assertEqual(capability.adapter_status, "IMPLEMENTED_OFFLINE_VERIFIED")
            self.assertEqual(capability.approval_status, "PENDING_APPROVAL")
            self.assertEqual(capability.configuration_status, "NOT_CONFIGURED")
            self.assertEqual(capability.live_verification_status, "NOT_VERIFIED")
            self.assertEqual(capability.supported_data_modes, ("SYNTHETIC",))
            self.assertFalse(capability.default_enabled)

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
