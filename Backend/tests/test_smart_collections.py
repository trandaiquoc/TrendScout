import hashlib
import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import httpx

from app.api.collections import _mode
from app.collections import embeddings
from app.collections.entity_context import EntityContext
from app.collections.service import CollectionService
from app.collections.store import CollectionStore, fingerprint, keyword_record
from app.main import create_app
from app.settings import Settings
from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from app.sources.google_trends.trending_now import normalize_keyword
from app.sources.wikimedia.cleaning_rules import collection_technical_reason


def fake_embed(texts):
    result = []
    for text in texts:
        lowered = text.casefold()
        if any(word in lowered for word in ("fashion", "denim", "recycled", "thời trang", "áo khoác")):
            result.append([1.0, 0.0, 0.0])
        elif any(word in lowered for word in ("mars", "rover", "spacecraft")):
            result.append([0.0, 1.0, 0.0])
        else:
            result.append([0.0, 0.0, 1.0])
    return result


def live_source(rows):
    for rank, row in enumerate(rows, start=1):
        row.setdefault("provider_rank", rank)
        row.setdefault("views_ceil", 1000)
    return [{"source_id": "wikimedia_pageviews_top_per_country", "data_mode": "LIVE",
             "status": "SUCCESS", "record_count": len(rows), "records": rows}]


class SmartCollectionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CollectionStore(Path(self.temp.name) / "collections.sqlite")
        self.service = CollectionService(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_live_youtube_video_text_is_kept_out_of_keyword_and_collection_processing(self):
        live_store = CollectionStore(Path(self.temp.name) / "youtube-live-collections.sqlite",
                                     data_mode_scope="LIVE")
        service = CollectionService(live_store)
        source = {
            "source_id": "youtube_video_search",
            "status": "SUCCESS",
            "data_mode": "LIVE",
            "delivery_state": "FRESH",
            "record_count": 1,
            "records": [{
                "provider_video_id": "AbCdEf12345",
                "canonical_video_url": "https://www.youtube.com/watch?v=AbCdEf12345",
                "title": "Portable Solar Generator Review",
                "description": "Consumer product video evidence",
                "fetched_at": "2026-11-16T00:00:00+00:00",
                "data_mode": "LIVE",
            }],
            "coverage": {"provider_method": "search.list", "source_fetched_at":
                         "2026-11-16T00:00:00+00:00"},
        }

        result = service._process_fetch("youtube-evidence-policy-test", [source])

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["available"], 1)
        self.assertEqual(result["skipped"], 1)
        with live_store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM keywords").fetchone()[0], 0)
            snapshot = db.execute(
                "SELECT status,provider_record_count,received_record_count,retained_keyword_count,"
                "rejected_record_count,source_manifest,quality_flags FROM dataset_snapshots "
                "WHERE dataset_id='youtube-evidence-policy-test'"
            ).fetchone()
        self.assertEqual(tuple(snapshot[:5]), ("PARTIAL", 1, 1, 0, 0))
        self.assertIn("YOUTUBE_KEYWORD_EXTRACTION_DISABLED", snapshot["quality_flags"])
        manifest = json.loads(snapshot["source_manifest"])
        self.assertEqual(manifest[0]["source_id"], "youtube_video_search")
        self.assertEqual(manifest[0]["record_count"], 1)
        self.assertEqual(manifest[0]["received_record_count"], 1)

    def test_keyword_identity_is_nfc_exact_and_keeps_source_evidence(self):
        composed = {
            "article_display": "Café Equipment",
            "article_original": "Cafe\u0301_Equipment",
            "project": "en.wikipedia",
            "observation_date": "2026-11-10",
            "fetched_at": "2026-11-12T00:00:00Z",
            "provider_rank": 1,
            "views_ceil": 1000,
        }
        decomposed = {**composed, "article_display": "Cafe\u0301 Equipment"}
        first = keyword_record(composed, "wikimedia_pageviews_top_per_country", "LIVE")
        second = keyword_record(decomposed, "wikimedia_pageviews_top_per_country", "LIVE")
        distinct_title = keyword_record({**composed, "article_display": "Cafe Equipment"},
                                        "wikimedia_pageviews_top_per_country", "LIVE")
        case_distinct_title = keyword_record({**composed, "article_display": "CAFÉ Equipment"},
                                             "wikimedia_pageviews_top_per_country", "LIVE")
        other_project = keyword_record({**composed, "project": "fr.wikipedia"},
                                       "wikimedia_pageviews_top_per_country", "LIVE")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["keyword"], "Café Equipment")
        self.assertEqual(first["normalized"], "café equipment")
        self.assertNotEqual(first["id"], distinct_title["id"])
        self.assertNotEqual(first["id"], case_distinct_title["id"])
        self.assertNotEqual(first["id"], other_project["id"])
        self.assertEqual(json.loads(first["evidence"])["article_original"], "Cafe\u0301_Equipment")
        self.assertIn("keyword-identity-nfc-case-preserving-v2",
                      json.loads(first["evidence"])["normalization_provenance"])

    def test_evidence_source_filters_tolerate_malformed_legacy_json_and_return_zero_counts(self):
        with self.store.connect() as db:
            db.execute("""INSERT INTO keywords(id,keyword,normalized,source,project,observed_at,
                data_mode,evidence) VALUES(?,?,?,?,?,?,?,?)""",
                ("legacy-malformed", "Legacy candidate", "legacy candidate", "google_web_serp",
                 "google_trends:US:en", "2026-11-12T00:00:00Z", "LIVE", "not-json"))

        results = self.store.search_keywords(evidence_source="google_trends")
        overview = self.store.analytics_overview_inputs()
        self.assertEqual(results["total"], 0)
        self.assertEqual(overview["verification_rows"], {
            "google_trends_candidates": 0,
            "google_search_candidates": 0,
            "bing_search_candidates": 0,
        })

    def test_wikimedia_only_algorithms_and_nested_verification_roles(self):
        wiki_row = {
            "article_display": "Portable Solar Generator",
            "article_original": "Portable_Solar_Generator",
            "project": "en.wikipedia", "observation_date": "2026-11-14",
            "fetched_at": "2026-11-15T12:00:00Z", "provider_rank": 1,
            "views_ceil": 8400, "country_scope": "US", "source_url": "https://wikimedia.org/",
        }
        google_record = {
            "data_source": "GOOGLE_TRENDS", "provider_id": "google_trends_rss",
            "transport_id": "google_trends_rss_http", "discovery_mode": "RSS_LIMITED_DISCOVERY",
            "canonical_keyword": "Portable Solar Generator",
            "canonical_keyword_identity": hashlib.sha256(
                normalize_keyword("Portable Solar Generator")[1].encode("utf-8")
            ).hexdigest(),
            "region": "US", "language": "en", "time_window_hours": None,
            "provider_data_at": "2026-11-15T11:55:00+00:00", "provider_created_at": None,
            "fetched_at": "2026-11-15T12:01:00+00:00",
            "source_url": "https://trends.google.com/trending?geo=US&hl=en-US",
            "delivery_state": "FRESH", "quality_flags": [],
            "serp_verification": {"source_id": "google_web_serp", "query_scope": "candidate"},
            "bing_verification": {"source_id": "bing_web_serp", "query_scope": "candidate"},
            "trend_clusters": [{"trend_cluster_id": "cluster-1", "started_at": None,
                "ended_at": None, "active": None, "search_volume": None,
                "search_volume_display": "20K+", "increase_percentage": None,
                "categories": None, "related_queries": None}],
        }
        google_source = [{"source_id": "google_web_serp", "data_mode": "LIVE",
            "status": "SUCCESS", "record_count": 1, "records": [google_record]}]
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            self.service._process_fetch("source-role-mix", [*live_source([wiki_row]), *google_source])

        with self.store.connect() as db:
            rows = db.execute("SELECT id,source,evidence FROM keywords ORDER BY source").fetchall()
            results = db.execute("SELECT keyword_id,algorithm_id,result_json FROM algorithm_results").fetchall()
        self.assertEqual({row["source"] for row in rows}, {
            "wikimedia_pageviews_top_per_country", "google_web_serp",
        })
        self.assertEqual(len(results), 4)
        self.assertTrue(all(row["keyword_id"] != next(item["id"] for item in rows
                                                        if item["source"] == "google_web_serp")
                            for row in results))
        rank_payload = json.loads(next(row["result_json"] for row in results
                                       if row["algorithm_id"] == "observed-volume-rank"))
        self.assertEqual(rank_payload["source_id"], "wikimedia_pageviews_top_per_country")
        self.assertEqual(rank_payload["algorithm_version"], "v4")
        self.assertEqual(rank_payload["result_contract_version"], "algorithm-result-v2")
        self.assertEqual(rank_payload["input_metric"], "views_ceil")
        self.assertTrue(rank_payload["evidence_references"])
        self.assertTrue(all(reference["input_digest"] for reference in rank_payload["evidence_references"]))
        wikimedia_id = next(row["id"] for row in rows
                           if row["source"] == "wikimedia_pageviews_top_per_country")
        result_digests = self.store.run_algorithm_analysis(
            lambda session: session.result_digests_for([wikimedia_id])
        )
        self.assertEqual(set(result_digests[wikimedia_id]), {
            "growth-ratio", "median-baseline", "observed-volume-rank",
            "observed-percentile-rank",
        })

        app = create_app(read_only=True, settings=Settings.from_environment({
            "TREND_SCOUT_ENVIRONMENT": "test", "TREND_SCOUT_READ_ONLY": "true"}))
        app.state.collection_service = self.service

        async def source_views():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                overview = await client.get("/api/v1/analytics/overview")
                attached = await client.get("/api/v1/datasets/latest/keywords", params={
                    "source": "google_web_serp", "evidence_source": "bing_web_serp",
                })
                return overview, attached
        overview, attached = __import__("asyncio").run(source_views())
        profiles = {profile["source_id"]: profile for profile in overview.json()["source_roles"]}
        self.assertEqual(profiles["google_web_serp"]["keyword_records"], 1)
        self.assertEqual(profiles["google_web_serp"]["verification_candidate_records"], 1)
        self.assertEqual(profiles["bing_web_serp"]["keyword_records"], 0)
        self.assertEqual(overview.json()["multi_source_coverage"]["composite_score"], None)
        self.assertEqual(overview.json()["multi_source_coverage"]["shared_keyword_overlap"], None)
        self.assertEqual(attached.json()["total"], 1)
        self.assertEqual(attached.json()["items"][0]["source"], "google_web_serp")

    def test_cache_only_collection_reuse_requires_completed_matching_source_evidence(self):
        from app.collections.source_digest import reusable_collection_input

        live_store = CollectionStore(Path(self.temp.name) / "cache-reuse.sqlite",
                                     data_mode_scope="LIVE")
        service = CollectionService(live_store)
        source = {
            "source_id": "youtube_video_search",
            "status": "SUCCESS",
            "data_mode": "LIVE",
            "delivery_state": "FRESH",
            "provider_request_count": 1,
            "record_count": 1,
            "records": [{
                "provider_video_id": "AbCdEf12345",
                "canonical_video_url": "https://www.youtube.com/watch?v=AbCdEf12345",
                "title": "Portable Solar Generator Review",
                "description": "Consumer product evidence",
                "published_at": "2026-11-15T00:00:00Z",
                "result_rank": 1,
                "data_mode": "LIVE",
            }],
            "coverage": {
                "provider_method": "search.list",
                "query": "portable solar generator",
                "provider_request_count": 1,
                "attempted_calls": 1,
                "serp_verification_attempted": 1,
                "serp_verification_failed_count": 0,
                "serp_admission_blocked_count": 0,
                "source_fetched_at": "2026-11-16T00:00:00+00:00",
            },
        }
        first = service._process_fetch("cache-reuse-base", [source])
        self.assertEqual(first["status"], "COMPLETED")
        with live_store.connect() as db:
            saved = db.execute(
                "SELECT source_manifest,processing_status FROM dataset_snapshots "
                "WHERE dataset_id='cache-reuse-base'"
            ).fetchone()
            previous_manifest = json.loads(saved["source_manifest"])

        cached = deepcopy(source)
        cached.update(delivery_state="CACHED", provider_request_count=0)
        cached["records"][0]["delivery_state"] = "CACHED"
        cached["coverage"].update({
            "provider_request_count": 0,
            "cached_provider_request_count": 1,
            "cache_delivery_at": "2026-11-17T12:00:00+00:00",
            "cache_fetched_at": "2026-11-16T00:00:00+00:00",
            "cache_freshness": "CACHED",
            "cache_status": "CACHED",
            "cache_reason": "FRESH_ENTRY",
            "refresh_attempted_at": "2026-11-17T11:59:59+00:00",
            "attempted_calls": 0,
            "serp_verification_attempted": 0,
            "cached_serp_verification_attempted": 1,
        })
        self.assertTrue(reusable_collection_input(
            [cached], previous_manifest,
            processing_status=saved["processing_status"],
        ))
        self.assertFalse(reusable_collection_input(
            [cached], previous_manifest, processing_status="MODEL_UNAVAILABLE",
        ))
        changed_scope = deepcopy(cached)
        changed_scope["coverage"]["query"] = "different query"
        self.assertFalse(reusable_collection_input(
            [changed_scope], previous_manifest,
            processing_status=saved["processing_status"],
        ))
        provider_work = deepcopy(cached)
        provider_work["provider_request_count"] = 1
        self.assertFalse(reusable_collection_input(
            [provider_work], previous_manifest,
            processing_status=saved["processing_status"],
        ))
        inconsistent_provider_work = deepcopy(cached)
        inconsistent_provider_work["coverage"]["provider_request_count"] = 1
        self.assertFalse(reusable_collection_input(
            [inconsistent_provider_work], previous_manifest,
            processing_status=saved["processing_status"],
        ))
        stale = deepcopy(cached)
        stale.update({
            "delivery_state": "STALE_CACHE",
            "status": "PARTIAL",
            "error_code": "MANUAL_REFRESH_COOLDOWN",
            "quality_flags": ["STALE_SHARED_CACHE_REUSED"],
        })
        stale["records"][0]["quality_flags"] = ["STALE_SHARED_CACHE_REUSED"]
        stale["coverage"].update({
            "refresh_state": "MANUAL_REFRESH_COOLDOWN",
            "eligible_slot": None,
            "next_slots_timezone": "Asia/Ho_Chi_Minh",
        })
        self.assertTrue(reusable_collection_input(
            [stale], previous_manifest,
            processing_status=saved["processing_status"],
        ))
        stale_boolean_verification = deepcopy(stale)
        stale_boolean_verification["coverage"]["serp_verification_attempted"] = False
        self.assertTrue(reusable_collection_input(
            [stale_boolean_verification], previous_manifest,
            processing_status=saved["processing_status"],
        ))
        attempted_verification = deepcopy(stale_boolean_verification)
        attempted_verification["coverage"]["serp_verification_attempted"] = True
        self.assertFalse(reusable_collection_input(
            [attempted_verification], previous_manifest,
            processing_status=saved["processing_status"],
        ))
        changed_quality = deepcopy(cached)
        changed_quality["records"][0]["quality_flags"] = ["MISSING_TITLE"]
        self.assertFalse(reusable_collection_input(
            [changed_quality], previous_manifest,
            processing_status=saved["processing_status"],
        ))
        from app.collections.source_digest import legacy_source_input_digest
        legacy_manifest = [deepcopy(previous_manifest[0])]
        legacy_manifest[0].pop("source_input_digest_version", None)
        legacy_manifest[0]["source_input_digest"] = legacy_source_input_digest(
            cached["source_id"], cached, legacy_manifest[0],
        )
        self.assertTrue(reusable_collection_input(
            [stale], legacy_manifest, processing_status=saved["processing_status"],
        ))
        legacy_changed_scope = deepcopy(stale)
        legacy_changed_scope["coverage"]["query"] = "different query"
        self.assertFalse(reusable_collection_input(
            [legacy_changed_scope], legacy_manifest,
            processing_status=saved["processing_status"],
        ))
        legacy_changed_quality = deepcopy(stale)
        legacy_changed_quality["quality_flags"] = ["MISSING_TITLE"]
        self.assertFalse(reusable_collection_input(
            [legacy_changed_quality], legacy_manifest,
            processing_status=saved["processing_status"],
        ))
        legacy_changed_count = deepcopy(stale)
        legacy_changed_count["records"] = []
        legacy_changed_count["record_count"] = 0
        self.assertFalse(reusable_collection_input(
            [legacy_changed_count], legacy_manifest,
            processing_status=saved["processing_status"],
        ))

        reused = service._process_fetch(
            "cache-reuse-hit", [stale], cached_context_only=True
        )
        self.assertEqual(reused["status"], "REUSED")
        self.assertEqual(reused["dataset_id"], "cache-reuse-base")
        with live_store.connect() as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM fetch_jobs WHERE fetch_id='cache-reuse-hit'"
            ).fetchone())
            self.assertEqual(db.execute("SELECT count(*) FROM dataset_snapshots").fetchone()[0], 1)

        changed = deepcopy(cached)
        changed["records"][0]["description"] = "Changed source evidence"
        processed = service._process_fetch(
            "cache-reuse-changed", [changed], cached_context_only=True
        )
        self.assertEqual(processed["status"], "COMPLETED")
        with live_store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM dataset_snapshots").fetchone()[0], 2)

    def test_live_unavailable_source_is_preserved_in_run_history_manifest(self):
        live_store = CollectionStore(Path(self.temp.name) / "unavailable-live-collections.sqlite",
                                     data_mode_scope="LIVE")
        service = CollectionService(live_store)
        source = {
            "source_id": "youtube_video_search",
            "status": "UNAVAILABLE",
            "data_mode": "LIVE",
            "delivery_state": "NOT_ELIGIBLE",
            "record_count": 0,
            "records": [],
            "error_code": "NO_CACHE_AVAILABLE",
            "provider_request_count": 0,
            "coverage": {
                "provider_request_count": 0,
                "cache_status": "UNAVAILABLE",
                "cache_reason": "CACHE_MISS",
                "next_eligible_at": "2026-11-17T08:00:00+00:00",
            },
        }

        service._process_fetch("live-unavailable-youtube-manifest", [source])

        with live_store.connect() as db:
            row = db.execute(
                "SELECT data_mode,source_manifest FROM dataset_snapshots "
                "WHERE dataset_id='live-unavailable-youtube-manifest'"
            ).fetchone()
        self.assertEqual(row["data_mode"], "LIVE")
        manifest = json.loads(row["source_manifest"])
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["status"], "UNAVAILABLE")
        self.assertEqual(manifest[0]["error_code"], "NO_CACHE_AVAILABLE")
        self.assertEqual(manifest[0]["delivery_state"], "NOT_ELIGIBLE")
        self.assertEqual(manifest[0]["coverage"]["next_eligible_at"],
                         "2026-11-17T08:00:00+00:00")

    def test_source_center_keeps_unavailable_error_without_losing_prior_observation(self):
        from types import SimpleNamespace

        from app.api.capabilities import _saved_source_activity

        prior = {
            "source_id": "youtube_video_search", "status": "SUCCESS",
            "data_mode": "LIVE", "delivery_state": "FRESH", "record_count": 1,
            "records": [{
                "provider_video_id": "AbCdEf12345",
                "canonical_video_url": "https://www.youtube.com/watch?v=AbCdEf12345",
                "title": "Existing observation", "data_mode": "LIVE",
                "fetched_at": "2026-11-16T00:00:00+00:00",
            }],
            "coverage": {"provider_method": "search.list",
                         "provider_request_count": 1,
                         "source_fetched_at": "2026-11-16T00:00:00+00:00"},
        }
        unavailable = {
            "source_id": "youtube_video_search", "status": "UNAVAILABLE",
            "data_mode": "LIVE", "delivery_state": "NOT_ELIGIBLE", "record_count": 0,
            "records": [], "error_code": "NO_CACHE_AVAILABLE",
            "provider_request_count": 0,
            "coverage": {"provider_request_count": 0, "cache_status": "UNAVAILABLE",
                         "cache_reason": "CACHE_MISS",
                         "next_eligible_at": "2026-11-17T08:00:00+00:00"},
        }
        self.service._process_fetch("youtube-activity-prior", [prior])
        self.service._process_fetch("youtube-activity-unavailable", [unavailable])
        request = SimpleNamespace(app=SimpleNamespace(
            state=SimpleNamespace(collection_service=self.service)
        ))

        activity = _saved_source_activity(request)["youtube_video_search"]

        self.assertEqual(activity["status"], "UNAVAILABLE")
        self.assertEqual(activity["error_code"], "NO_CACHE_AVAILABLE")
        self.assertEqual(activity["delivery_state"], "NOT_ELIGIBLE")
        self.assertEqual(activity["last_successful_observation_at"],
                         "2026-11-16T00:00:00+00:00")
        self.assertEqual(activity["youtube_coverage"]["next_eligible_at"],
                         "2026-11-17T08:00:00+00:00")

    def test_youtube_capability_keeps_observation_and_cache_delivery_times_separate(self):
        from types import SimpleNamespace

        from app.api.capabilities import _saved_source_activity

        source = {
            "source_id": "youtube_video_search",
            "status": "SUCCESS",
            "data_mode": "LIVE",
            "delivery_state": "CACHED",
            "record_count": 1,
            "records": [{
                "provider_video_id": "AbCdEf12345",
                "canonical_video_url": "https://www.youtube.com/watch?v=AbCdEf12345",
                "title": "Portable Solar Generator Review",
                "fetched_at": "2026-11-15T08:00:00+00:00",
                "data_mode": "LIVE",
            }],
            "coverage": {
                "provider_method": "search.list",
                "source_fetched_at": "2026-11-15T08:00:00+00:00",
                "cache_delivery_at": "2026-11-16T08:00:00+00:00",
            },
        }
        self.service._process_fetch("youtube-cache-delivery-capability", [source])
        request = SimpleNamespace(app=SimpleNamespace(
            state=SimpleNamespace(collection_service=self.service)
        ))

        activity = _saved_source_activity(request)["youtube_video_search"]

        self.assertEqual(activity["last_successful_observation_at"],
                         "2026-11-15T08:00:00+00:00")
        self.assertEqual(activity["last_cache_delivery_at"],
                         "2026-11-16T08:00:00+00:00")
        self.assertEqual(activity["delivery_state"], "CACHED")

    def test_configured_local_store_directory_is_respected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"TRENDSCOUT_COLLECTIONS_DIR": directory}
        ):
            store = CollectionStore.local()
            self.assertEqual(store.path, Path(directory) / "collections.sqlite")

    def test_algorithm_catalog_and_persisted_results_api(self):
        row = {"article_display": "Robot vacuum", "article_original": "Robot_vacuum",
            "project": "en.wikipedia", "observation_date": "2026-11-10",
            "fetched_at": "2026-11-12T00:00:00Z", "provider_rank": 1, "views_ceil": 1000}
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            self.service._process_fetch("api-catalog", live_source([row, {**row,
                "article_display": "Mars Rover", "article_original": "Mars_Rover", "views_ceil": 500}]))
        app = create_app(read_only=True, settings=Settings.from_environment({
            "TREND_SCOUT_ENVIRONMENT": "test", "TREND_SCOUT_READ_ONLY": "true"}))
        app.state.collection_service = self.service

        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                return await client.get("/api/v1/algorithms"), await client.get("/api/v1/algorithms/results")

        catalog_response, results_response = __import__("asyncio").run(request())
        self.assertEqual(catalog_response.status_code, 200)
        self.assertEqual(len(catalog_response.json()["algorithms"]), 13)
        self.assertEqual(results_response.status_code, 200)
        self.assertEqual(len(results_response.json()["catalog"]["algorithms"]), 13)
        summaries = {row["algorithm_id"]: row for row in results_response.json()["summary"]}
        self.assertEqual(summaries["observed-volume-rank"]["completed"], 2)
        self.assertIn("NOT_YET_EVALUATED", results_response.json()["evaluation_status"])

        async def filtered_results():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                return await client.get("/api/v1/algorithms/results", params={
                    "limit": 1, "offset": 0,
                    "source": "wikimedia_pageviews_top_per_country",
                    "algorithm_id": "observed-volume-rank", "status": "COMPLETED",
                })
        filtered = __import__("asyncio").run(filtered_results())
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.json()["total"], 2)
        self.assertEqual(len(filtered.json()["items"]), 1)
        self.assertEqual(filtered.json()["items"][0]["algorithm_id"], "observed-volume-rank")
        self.assertTrue(filtered.json()["items"][0]["input_digest"])
        async def read_dataset():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                summary = await client.get("/api/v1/datasets/latest")
                page = await client.get("/api/v1/datasets/latest/keywords?q=robot&state=all&sort=keyword")
                return summary, page
        dataset_response, keyword_response = __import__("asyncio").run(read_dataset())
        self.assertEqual(dataset_response.json()["item"]["status"], "COMPLETED")
        self.assertEqual(dataset_response.json()["item"]["restoration_status"], "CACHED")
        self.assertEqual(keyword_response.json()["total"], 1)
        self.assertEqual(keyword_response.json()["items"][0]["keyword"], "Robot vacuum")
        classification_response = __import__("asyncio").run(self._classification_request(app, keyword_response.json()["items"][0]["id"]))
        self.assertEqual(classification_response.status_code, 200)
        self.assertEqual(classification_response.json()["classification_status"], "UNKNOWN")
        self.assertEqual(classification_response.json()["taxonomy_version"], "topics-v1.5")

    def test_live_scope_keeps_legacy_synthetic_rows_and_excludes_them_from_product_counts(self):
        import asyncio
        import json

        path = Path(self.temp.name) / "mixed.sqlite"
        legacy_store = CollectionStore(path)
        legacy_service = CollectionService(legacy_store)
        row = {"article_display": "Robot vacuum", "article_original": "Robot_vacuum",
            "project": "en.wikipedia", "observation_date": "2026-11-10",
            "fetched_at": "2026-11-12T00:00:00Z", "provider_rank": 1, "views_ceil": 1000}
        synthetic = live_source([dict(row)])
        synthetic[0]["data_mode"] = "SYNTHETIC"
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            legacy_service._process_fetch("legacy-synthetic", synthetic)
        legacy_item = legacy_store.search_keywords(query="robot")["items"][0]
        legacy_id = legacy_item["id"]
        archive_id = legacy_store.create({"name": "Legacy synthetic archive", "auto_match": False})
        legacy_store.feedback_action(archive_id, {
            "id": legacy_id, "keyword": legacy_item["keyword"], "normalized": legacy_item["normalized"],
            "source": legacy_item["source"], "project": legacy_item["project"],
            "observed_at": legacy_item["observed_at"], "data_mode": "SYNTHETIC",
            "evidence": json.dumps(legacy_item["evidence"]), "context": legacy_item["context"],
        }, "ADD")
        with legacy_store.connect() as db:
            before = {
                "keyword": tuple(db.execute("SELECT data_mode,eligible,evidence FROM keywords WHERE id=?", (legacy_id,)).fetchone()),
                "classification": tuple(db.execute("SELECT classification_status,topic_category_ids,classified_at FROM classification_results WHERE keyword_id=?", (legacy_id,)).fetchone()),
                "observations": db.execute("SELECT count(*) FROM algorithm_observations WHERE keyword_id=?", (legacy_id,)).fetchone()[0],
                "memberships": db.execute("SELECT count(*) FROM memberships WHERE keyword_id=?", (legacy_id,)).fetchone()[0],
            }

        live_store = CollectionStore(path, data_mode_scope="LIVE")
        live_service = CollectionService(live_store)
        outcome = live_service._process_fetch("current-live", live_source([dict(row)]))
        self.assertEqual((outcome["status"], outcome["error_code"]),
                         ("MODEL_UNAVAILABLE", "AI_DISABLED"))
        with live_store.connect() as db:
            after = {
                "keyword": tuple(db.execute("SELECT data_mode,eligible,evidence FROM keywords WHERE id=?", (legacy_id,)).fetchone()),
                "classification": tuple(db.execute("SELECT classification_status,topic_category_ids,classified_at FROM classification_results WHERE keyword_id=?", (legacy_id,)).fetchone()),
                "observations": db.execute("SELECT count(*) FROM algorithm_observations WHERE keyword_id=?", (legacy_id,)).fetchone()[0],
                "memberships": db.execute("SELECT count(*) FROM memberships WHERE keyword_id=?", (legacy_id,)).fetchone()[0],
            }
            live_rows = db.execute("SELECT id,data_mode FROM keywords WHERE data_mode='LIVE'").fetchall()
        self.assertEqual(after, before)
        self.assertEqual(len(live_rows), 1)
        self.assertNotEqual(live_rows[0]["id"], legacy_id)
        self.assertEqual(live_rows[0]["data_mode"], "LIVE")
        self.assertEqual(live_store.workbench_summary()["stored_keywords"], 1)
        self.assertEqual(live_store.search_keywords(query="robot")["total"], 1)
        self.assertEqual(live_store.latest_dataset()["data_mode"], "LIVE")
        self.assertEqual(live_store.keywords_for(archive_id), [])
        self.assertEqual(next(item for item in live_store.list_collections() if item["id"] == archive_id)["membership_count"], 0)

        app = create_app(read_only=True, settings=Settings.from_environment({
            "TREND_SCOUT_ENVIRONMENT": "test", "TREND_SCOUT_READ_ONLY": "true"}))
        app.state.collection_service = live_service

        async def read_overview():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                return await client.get("/api/v1/analytics/overview"), await client.get("/api/v1/algorithms/results")

        overview, algorithms = asyncio.run(read_overview())
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.json()["keyword_totals"]["unique"], 1)
        self.assertEqual(overview.json()["latest_dataset"]["data_mode"], "LIVE")
        self.assertEqual(algorithms.status_code, 200)
        self.assertTrue(all(item["keyword_id"] != legacy_id for item in algorithms.json()["items"]))

    async def _classification_request(self, app, keyword_id):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            return await client.get(f"/api/v1/collections/classifications/{keyword_id}")

    def test_partial_snapshot_executes_descriptive_snapshot_popularity(self):
        rows = [{"article_display": "Robot vacuum", "article_original": "Robot_vacuum",
            "project": "en.wikipedia", "observation_date": "2026-11-10",
            "fetched_at": "2026-11-12T00:00:00Z", "provider_rank": 1, "views_ceil": 1000},
            {"article_display": "Mars Rover", "article_original": "Mars_Rover",
             "project": "en.wikipedia", "observation_date": "2026-11-10",
             "fetched_at": "2026-11-12T00:00:00Z", "provider_rank": 2, "views_ceil": 500}]
        source = live_source(rows)
        source[0]["status"] = "PARTIAL"
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            self.service._process_fetch("partial-snapshot", source)
        with self.store.connect() as db:
            completed = db.execute("SELECT algorithm_id,value,result_json FROM algorithm_results WHERE status='COMPLETED'").fetchall()
            self.assertEqual(len(completed), 4)
            rankings = [row for row in completed if row["algorithm_id"] in {
                "observed-volume-rank", "observed-percentile-rank"}]
            self.assertEqual(len(rankings), 4)
            self.assertTrue(all(row["value"] is not None for row in rankings))
            for row in rankings:
                flags = __import__("json").loads(row["result_json"])["quality_flags"]
                self.assertTrue({"CENSORED_TOP_LIST", "PARTIAL_SAMPLE", "VIEWS_APPROXIMATE"} <= set(flags))
            self.assertEqual(db.execute("SELECT count(*) FROM algorithm_observations WHERE snapshot_status='PARTIAL'").fetchone()[0], 2)
        self.assertEqual(self.store.latest_dataset()["status"], "PARTIAL")

    def test_complete_and_partial_snapshots_store_absence_as_missing_not_zero(self):
        first_rows = [
            {"article_display": "Robot vacuum", "article_original": "Robot_vacuum",
             "project": "en.wikipedia", "observation_date": "2026-11-10",
             "fetched_at": "2026-11-11T12:00:00Z", "views_ceil": 1000},
            {"article_display": "Mars Rover", "article_original": "Mars_Rover",
             "project": "en.wikipedia", "observation_date": "2026-11-10",
             "fetched_at": "2026-11-11T12:00:00Z", "views_ceil": 500},
        ]
        second_source = live_source([{
            "article_display": "Mars Rover", "article_original": "Mars_Rover",
            "project": "en.wikipedia", "observation_date": "2026-11-11",
            "fetched_at": "2026-11-12T12:00:00Z", "views_ceil": 700,
        }])
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            self.service._process_fetch("absence-first", live_source(first_rows))
            self.service._process_fetch("absence-complete", second_source)
        robot = self.store.search_keywords(query="Robot vacuum")["items"][0]
        with self.store.connect() as db:
            observation = db.execute("SELECT status,value,snapshot_status FROM algorithm_observations "
                "WHERE keyword_id=? AND observation_date='2026-11-11'", (robot["id"],)).fetchone()
            results = db.execute("SELECT status,reason_code,value FROM algorithm_results WHERE keyword_id=?",
                                 (robot["id"],)).fetchall()
        self.assertEqual(tuple(observation), ("NOT_IN_TOP", None, "SUCCESS"))
        self.assertTrue(results)
        self.assertTrue(all(row["status"] == "INSUFFICIENT_DATA" and row["value"] is None
                            for row in results))

        partial_store = CollectionStore(Path(self.temp.name) / "partial-absence.sqlite")
        partial_service = CollectionService(partial_store, fetch_model_inference_enabled=False)
        partial_second = live_source([{
            "article_display": "Mars Rover", "article_original": "Mars_Rover",
            "project": "en.wikipedia", "observation_date": "2026-11-11",
            "fetched_at": "2026-11-12T12:00:00Z", "views_ceil": 700,
        }])
        partial_second[0]["status"] = "PARTIAL"
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            partial_service._process_fetch("partial-absence-first", live_source([dict(row) for row in first_rows]))
            partial_service._process_fetch("partial-absence-second", partial_second)
        partial_robot = partial_store.search_keywords(query="Robot vacuum")["items"][0]
        with partial_store.connect() as db:
            partial_observation = db.execute("SELECT status,value,snapshot_status FROM algorithm_observations "
                "WHERE keyword_id=? AND observation_date='2026-11-11'", (partial_robot["id"],)).fetchone()
        self.assertEqual(tuple(partial_observation), ("PARTIAL_SNAPSHOT", None, "PARTIAL"))

    def test_cache_only_analysis_does_not_open_analysis_transaction(self):
        record = {"source": "wikimedia_pageviews_top_per_country"}
        with patch.object(self.store, "run_algorithm_analysis",
                          side_effect=AssertionError("cache-only replay must not scan or write")):
            result = self.service._analyze_persisted_keywords(
                [record],
                sources=[{"source_id": "wikimedia_pageviews_top_per_country",
                          "coverage": {"cache_only": True}}],
                cache_only=True,
            )
        self.assertEqual(result["analysis_status"], "CACHE_ONLY")
        self.assertEqual(result["recomputed_algorithms"], 0)

    def test_observation_history_is_bounded_to_the_full_source_scope(self):
        row = keyword_record({
            "article_display": "Scope Example", "article_original": "Scope_Example",
            "project": "en.wikipedia", "observation_date": "2026-11-10",
            "fetched_at": "2026-11-11T00:00:00Z", "provider_rank": 1, "views_ceil": 1000,
        }, "wikimedia_pageviews_top_per_country", "LIVE")
        keyword_id = self.store.persist_cleaned_keywords([row])["records"][0]["id"]
        source = "wikimedia_pageviews_top_per_country"
        metric, unit = "views_ceil", "approx_pageviews_upper_bound"

        def observation(day, country, access, project, value):
            return (keyword_id, source, day, country, access, project, metric, unit,
                    "OBSERVED", value, 1, "SUCCESS", f"digest-{day}-{country}-{access}-{project}")

        self.store.run_algorithm_analysis(lambda session: session.upsert_observations([
            observation("2026-11-09", "US", "all-access", "en.wikipedia", 100),
        ]))
        self.store.run_algorithm_analysis(lambda session: session.upsert_observations([
            observation("2026-11-09", "CA", "desktop", "fr.wikipedia", 200),
            observation("2026-11-10", "US", "all-access", "en.wikipedia", 300),
        ]))
        def read(session):
            old_us = session.history(keyword_id, source, "2026-11-11", metric, unit,
                country="US", access="all-access", project="en.wikipedia")
            old_ca = session.history(keyword_id, source, "2026-11-10", metric, unit,
                country="CA", access="desktop", project="fr.wikipedia")
            batch_us = session.histories([keyword_id, keyword_id], source, "2026-11-11", metric, unit,
                after_date="2026-11-01", country="US", access="all-access", project="en.wikipedia")
            batch_ca = session.histories([keyword_id], source, "2026-11-10", metric, unit,
                country="CA", access="desktop", project="fr.wikipedia")
            return old_us, old_ca, batch_us, batch_ca
        us_rows, ca_rows, batch_us, batch_ca = self.store.run_algorithm_analysis(read)
        self.assertEqual([row["observation_date"] for row in us_rows], ["2026-11-10"])
        self.assertEqual([row["value"] for row in us_rows], [300])
        self.assertEqual([row["observation_date"] for row in ca_rows], ["2026-11-09"])
        self.assertEqual([row["value"] for row in ca_rows], [200])
        self.assertEqual([row["observation_date"] for row in batch_us], ["2026-11-10"])
        self.assertEqual([row["observation_date"] for row in batch_ca], ["2026-11-09"])

    def test_user_exclusion_survives_repeated_fetch_and_can_be_restored(self):
        import asyncio

        row = {"article_display": "Robot vacuum", "article_original": "Robot_vacuum",
               "project": "en.wikipedia", "observation_date": "2026-11-10",
               "fetched_at": "2026-11-12T00:00:00Z", "views_ceil": 1000}
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            asyncio.run(self.service.process_fetch("first-exclude", live_source([row])))
            self.assertEqual(self.store.search_keywords(query="robot")["total"], 1)
            app = create_app(read_only=True, settings=Settings.from_environment({
                "TREND_SCOUT_ENVIRONMENT": "test", "TREND_SCOUT_READ_ONLY": "true"}))
            app.state.collection_service = self.service

            async def bulk(action, state):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                    return await client.post("/api/v1/collections/bulk", json={
                        "action": action, "all_filtered": True, "query": "robot", "state": state,
                    })

            excluded = asyncio.run(bulk("EXCLUDE", "all"))
            self.assertEqual(excluded.status_code, 200)
            self.assertEqual(excluded.json()["updated"], 1)
            asyncio.run(self.service.process_fetch("repeated-exclude", live_source([row])))
        self.assertEqual(self.store.search_keywords(state="excluded")["total"], 1)
        self.assertEqual(self.store.search_keywords(state="all")["items"][0]["eligible"], 0)
        restored = asyncio.run(bulk("RESTORE", "excluded"))
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["updated"], 1)
        self.assertEqual(self.store.search_keywords(state="all")["items"][0]["eligible"], 1)

    def test_latest_dataset_separates_latest_fetch_batch_from_full_ledger(self):
        import asyncio

        first = {"article_display": "Mars Rover", "article_original": "Mars_Rover",
                 "project": "en.wikipedia", "observation_date": "2026-11-10",
                 "fetched_at": "2026-11-12T00:00:00Z"}
        second = {"article_display": "Robot Vacuum", "article_original": "Robot_Vacuum",
                  "project": "en.wikipedia", "observation_date": "2026-11-11",
                  "fetched_at": "2026-11-12T01:00:00Z"}
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            asyncio.run(self.service.process_fetch("batch-two", live_source([first, second])))
            asyncio.run(self.service.process_fetch("batch-one", live_source([second])))
        snapshot = self.store.latest_dataset()
        self.assertEqual(snapshot["snapshot_retained_keyword_count"], 1)
        self.assertEqual(snapshot["retained_keyword_count"], 2)

    def test_empty_failed_fetch_keeps_existing_analysis_and_memberships_unchanged(self):
        collection_id = self.store.create({
            "name": "Sustainable fashion",
            "description": "Recycled denim and eco clothing",
        })
        row = {
            "article_display": "Upcycled Denim Jacket",
            "article_original": "Upcycled_Denim_Jacket",
            "project": "en.wikipedia",
            "observation_date": "2026-11-10",
            "fetched_at": "2026-11-12T00:00:00Z",
        }
        seeded = self.service._process_fetch("existing-google-safe-baseline", live_source([row]))
        self.assertEqual((seeded["status"], seeded["error_code"], seeded["available"]),
                         ("MODEL_UNAVAILABLE", "AI_DISABLED", 1))
        candidate = keyword_record(row, "wikimedia_pageviews_top_per_country", "LIVE")
        self.store.feedback_action(collection_id, candidate, "ADD")
        self.assertEqual(len(self.store.keywords_for(collection_id)), 1)

        table_rows = (
            "classification_results", "memberships", "algorithm_results",
            "algorithm_observations", "membership_reconciliation_runs",
        )
        before = {}
        with self.store.connect() as db:
            for table in table_rows:
                before[table] = [tuple(item) for item in db.execute(f"SELECT * FROM {table} ORDER BY 1,2")]

        failed_google = [{
            "source_id": "google_web_serp", "data_mode": "LIVE", "status": "FAILED",
            "record_count": 0, "records": [], "error_code": "UPSTREAM_ERROR",
            "coverage": {"attempted_calls": 1, "discovery_mode": "SERPAPI_TRENDING_NOW"},
        }]
        with patch.object(embeddings, "embed", side_effect=AssertionError("empty fetch must skip reanalysis")):
            result = self.service._process_fetch("failed-google-no-observations", failed_google)

        self.assertEqual((result["status"], result["available"], result["processed"]),
                         ("COMPLETED", 0, 0))
        with self.store.connect() as db:
            for table in table_rows:
                after = [tuple(item) for item in db.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
                self.assertEqual(after, before[table], table)
            snapshot = db.execute(
                "SELECT status,data_mode,provider_record_count,received_record_count,"
                "retained_keyword_count,processing_status FROM dataset_snapshots WHERE dataset_id=?",
                ("failed-google-no-observations",),
            ).fetchone()
            job = db.execute(
                "SELECT status,available,processed,assigned,unassigned FROM fetch_jobs WHERE fetch_id=?",
                ("failed-google-no-observations",),
            ).fetchone()
        self.assertEqual(tuple(snapshot), ("FAILED", "LIVE", 0, 0, 0, "COMPLETED"))
        self.assertEqual(tuple(job), ("COMPLETED", 0, 0, 0, 0))

    def test_google_only_live_fetch_does_not_reprocess_wikimedia_or_user_state(self):
        live_store = CollectionStore(Path(self.temp.name) / "live-collections.sqlite",
                                     data_mode_scope="LIVE")
        service = CollectionService(live_store)
        wiki_row = {
            "article_display": "Portable Solar Generator",
            "article_original": "Portable_Solar_Generator",
            "project": "en.wikipedia", "observation_date": "2026-11-14",
            "fetched_at": "2026-11-15T12:00:00Z", "provider_rank": 1,
            "views_ceil": 8400, "country_scope": "US",
            "source_url": "https://wikimedia.org/",
        }
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            service._process_fetch("wiki-before-google", live_source([wiki_row]))
        wiki_keyword = keyword_record(
            wiki_row, "wikimedia_pageviews_top_per_country", "LIVE"
        )
        collection_id = live_store.create({"name": "User research topics"})
        stamp = "2026-11-15T12:30:00+00:00"
        with live_store.connect() as db:
            db.execute("INSERT INTO keyword_decisions VALUES(?,?,?,?)",
                       (wiki_keyword["id"], "RESTORED", "Kept by user", stamp))
            db.execute("INSERT INTO memberships(collection_id,keyword_id,origin,pinned,score,reason,created_at) "
                       "VALUES(?,?,'USER',1,NULL,'User kept this keyword',?)",
                       (collection_id, wiki_keyword["id"], stamp))
            db.execute("INSERT INTO feedback VALUES(?,?, 'ADD',?)",
                       (collection_id, wiki_keyword["id"], stamp))
            db.execute("""INSERT INTO research_records(
                id,keyword_id,subject,hypothesis,status,created_at,updated_at
                ) VALUES('wiki-research',?,'Solar generators','Retain existing research',
                         'DRAFT',?,?)""", (wiki_keyword["id"], stamp, stamp))

        integrity_queries = {
            "wikimedia_keywords": "SELECT * FROM keywords WHERE source='wikimedia_pageviews_top_per_country' ORDER BY id",
            "wikimedia_classifications": "SELECT cr.* FROM classification_results cr JOIN keywords k "
                "ON k.id=cr.keyword_id WHERE k.source='wikimedia_pageviews_top_per_country' ORDER BY cr.keyword_id",
            "wikimedia_algorithm_observations": "SELECT ao.* FROM algorithm_observations ao JOIN keywords k "
                "ON k.id=ao.keyword_id WHERE k.source='wikimedia_pageviews_top_per_country' ORDER BY 1,2,3,8",
            "wikimedia_algorithm_results": "SELECT ar.* FROM algorithm_results ar JOIN keywords k "
                "ON k.id=ar.keyword_id WHERE k.source='wikimedia_pageviews_top_per_country' ORDER BY 1,2",
            "wikimedia_memberships": "SELECT m.* FROM memberships m JOIN keywords k ON k.id=m.keyword_id "
                "WHERE k.source='wikimedia_pageviews_top_per_country' ORDER BY 1,2",
            "keyword_decisions": "SELECT * FROM keyword_decisions ORDER BY keyword_id",
            "feedback": "SELECT * FROM feedback ORDER BY collection_id,keyword_id",
            "research_records": "SELECT * FROM research_records ORDER BY id",
            "reconciliation_runs": "SELECT * FROM membership_reconciliation_runs ORDER BY id",
        }

        def read_integrity():
            with live_store.connect() as db:
                return {
                    name: [tuple(row) for row in db.execute(query).fetchall()]
                    for name, query in integrity_queries.items()
                }

        before = read_integrity()
        title, identity = normalize_keyword("Portable Outdoor Blanket")
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        google_record = {
            "data_source": "GOOGLE_TRENDS", "provider_id": "google_trends_rss",
            "transport_id": "google_trends_rss_http", "discovery_mode": "RSS_LIMITED_DISCOVERY",
            "canonical_keyword": title, "canonical_keyword_identity": digest,
            "region": "US", "language": "en", "time_window_hours": None,
            "provider_data_at": "2026-11-15T11:55:00+00:00", "provider_created_at": None,
            "fetched_at": "2026-11-15T12:01:00+00:00",
            "source_url": "https://trends.google.com/trending?geo=US&hl=en-US",
            "delivery_state": "FRESH", "quality_flags": ["RSS_LIMITED_COVERAGE"],
            "trend_clusters": [{"trend_cluster_id": "cluster-1", "started_at": None,
                "ended_at": None, "active": None, "search_volume": None,
                "search_volume_display": "20K+", "increase_percentage": None,
                "categories": None, "related_queries": None}],
        }
        google_source = [{"source_id": "google_web_serp", "data_mode": "LIVE",
            "status": "SUCCESS", "record_count": 1, "records": [google_record]}]
        outcome = service._process_fetch("google-only-live", google_source)

        self.assertEqual((outcome["status"], outcome["error_code"], outcome["available"]),
                         ("MODEL_UNAVAILABLE", "AI_DISABLED", 1))
        self.assertEqual(read_integrity(), before)
        with live_store.connect() as db:
            self.assertEqual(db.execute(
                "SELECT count(*) FROM keywords WHERE source='google_web_serp' AND data_mode='LIVE'"
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT decision FROM keyword_decisions WHERE keyword_id=?", (wiki_keyword["id"],)
            ).fetchone()[0], "RESTORED")

    def test_crud_feedback_and_restart_persistence(self):
        collection_id = self.store.create({"name": "Sustainable fashion", "examples": ["circular clothing"]})
        row = keyword_record({"article_display": "Upcycled Denim", "article_original": "Upcycled_Denim",
            "project": "en.wikipedia", "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z",
            "provider_rank": 1, "views_ceil": 1000},
            "wikimedia_pageviews_top_per_country", "LIVE")
        self.store.feedback_action(collection_id, row, "ADD")
        self.assertEqual(self.store.keywords_for(collection_id)[0]["origin"], "USER")
        self.store.feedback_action(collection_id, row, "REMOVE")
        self.assertEqual(self.store.keywords_for(collection_id), [])
        restarted = CollectionStore(Path(self.temp.name) / "collections.sqlite")
        self.assertEqual(restarted.list_collections()[0]["name"], "Sustainable fashion")
        self.assertEqual(restarted.list_collections()[0]["version"], 3)
        self.assertFalse(restarted.delete(collection_id) is False)
        with restarted.connect() as db:
            self.assertTrue(db.execute("SELECT 1 FROM suppression").fetchone())

    def test_merge_transfers_keywords_and_preserves_target_pins_and_rejections(self):
        source = self.store.create({"name": "TV topics"})
        target = self.store.create({"name": "Screen topics"})
        records = [keyword_record({"article_display": name, "article_original": name.replace(" ", "_"),
            "project": "en.wikipedia", "observation_date": "2026-11-10",
            "fetched_at": "2026-11-12T00:00:00Z", "provider_rank": index, "views_ceil": 1000},
            "wikimedia_pageviews_top_per_country", "LIVE") for index, name in enumerate(
                ("TV series one", "TV series two", "TV series three"), start=1)]
        for record in records:
            self.store.feedback_action(source, record, "ADD")
        self.store.feedback_action(target, records[0], "ADD")
        self.store.feedback_action(target, records[1], "REMOVE")
        result = self.store.merge(source, target)
        self.assertEqual(result, {"merged": True, "transferred": 1, "skipped": 2, "target_id": target})
        self.assertFalse(any(item["id"] == source for item in self.store.list_collections()))
        target_rows = self.store.keywords_for(target)
        self.assertEqual({item["id"] for item in target_rows}, {records[0]["id"], records[2]["id"]})
        self.assertTrue(all(item["origin"] == "USER" and item["pinned"] for item in target_rows))
        self.assertEqual(self.store.search_keywords()["total"], 3)
        with self.store.connect() as db:
            self.assertTrue(db.execute("SELECT 1 FROM suppression WHERE fingerprint=?",
                                       (fingerprint("TV topics"),)).fetchone())

    def test_mode_default_and_legacy_setting_migration(self):
        self.assertEqual(_mode(self.service), "AUTO_CREATE")
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('creation_mode','SUGGEST')")
        self.assertEqual(_mode(self.service), "SUGGEST_ONLY")
        self.assertEqual(_mode(self.service), "SUGGEST_ONLY")

    def test_duplicate_collection_name_is_rejected(self):
        self.store.create({"name": "Smart Home"})
        with self.assertRaisesRegex(ValueError, "COLLECTION_ALREADY_EXISTS"):
            self.store.create({"name": "smart home"})

    def test_verified_entity_context_creates_hierarchical_memberships_and_unknown_people_stay_unassigned(self):
        class ContextResolver:
            def __init__(self, store):
                self.store = store
                self.contexts = {}

            def enrich(self, records):
                for row in records:
                    recognized = row["keyword"] == "Travis Kelce"
                    self.contexts[row["id"]] = EntityContext(
                        row["id"], row["keyword"], 10, "Q123", "American football player" if recognized else "Person",
                        "person", ("Q2066131",) if recognized else (),
                        ("celebrity_public_figures.athletes",) if recognized else (),
                        "https://en.wikipedia.org/wiki/" + row["keyword"].replace(" ", "_"),
                        "TEST_VERIFIED_CONTEXT", "CLASSIFIED" if recognized else "UNKNOWN", "2026-11-13T00:00:00Z")
                with self.store.connect() as db:
                    db.executemany("""INSERT OR REPLACE INTO entity_context(keyword_id,title,page_id,entity_id,
                    description,page_namespace,is_disambiguation,redirected_from,page_language,is_main_page,is_main_page_known,entity_type,
                        occupations,category_ids,instance_types,source_url,method,status,context_version,captured_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [value.as_row() for value in self.contexts.values()])
                return self.contexts

            def cached(self, keyword_ids):
                return {key: self.contexts[key] for key in keyword_ids if key in self.contexts}

        resolver = ContextResolver(self.store)
        self.service = CollectionService(self.store, entity_context_resolver=resolver)
        rows = []
        for name in ("Travis Kelce", "Unclassified Person Alpha", "Unclassified Person Beta",
                     "Unclassified Person Gamma"):
            rows.append({"article_display": name, "article_original": name.replace(" ", "_"),
                "project": "en.wikipedia", "observation_date": "2026-11-10",
                "fetched_at": "2026-11-12T00:00:00Z", "views_ceil": 1000})
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            self.service._process_fetch("entity-hierarchy", live_source(rows))
        collections = {item["category_id"]: item for item in self.store.list_collections() if item["category_id"]}
        parent = collections["celebrity_public_figures"]
        athlete = collections["celebrity_public_figures.athletes"]
        self.assertEqual(athlete["parent_id"], parent["id"])
        for category_id in ("fashion", "home_living", "technology", "entertainment", "sports", "gaming"):
            self.assertIn(category_id, collections)
        self.assertEqual(collections["fashion"]["membership_count"], 0)
        self.assertEqual(collections["home_living"]["membership_count"], 0)
        member_rows = self.store.keywords_for(athlete["id"])
        self.assertEqual([row["keyword"] for row in member_rows], ["Travis Kelce"])
        self.assertEqual(parent["membership_count"], 1)
        self.assertFalse(any("Unclassified Person" in item["name"] for item in self.store.list_collections()))
        unassigned = self.store.search_keywords(query="Unclassified Person", state="unassigned")
        self.assertEqual(unassigned["total"], 3)
        self.assertEqual(member_rows[0]["entity_id"], "Q123")

    def test_reconciliation_archives_only_unpinned_ai_and_rolls_back(self):
        collection = self.store.create({"name": "Fashion"})
        first = keyword_record({"article_display": "Denim jacket", "article_original": "Denim_jacket",
            "project": "en.wikipedia", "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z",
            "provider_rank": 1, "views_ceil": 1000}, "wikimedia_pageviews_top_per_country", "LIVE")
        second = {**first, "id": "second", "keyword": "Manual keyword"}
        with self.store.connect() as db:
            for row in (first, second):
                db.execute("INSERT INTO keywords VALUES(?,?,?,?,?,?,?,?,?,1,NULL)", tuple(row[key] for key in (
                    "id", "keyword", "normalized", "source", "project", "observed_at", "data_mode", "evidence", "context")))
            db.execute("INSERT INTO memberships VALUES(?,?, 'LOCAL_AI',0,0.8,'old match',?)", (collection, first["id"], "old"))
            db.execute("INSERT INTO memberships VALUES(?,?, 'USER',1,NULL,'manual pin',?)", (collection, second["id"], "old"))
        self.assertEqual(self.store.archive_unpinned_ai_memberships("reconcile-test", "threshold update"), 1)
        self.assertEqual(len(self.store.keywords_for(collection)), 1)
        self.assertEqual(self.store.keywords_for(collection)[0]["origin"], "USER")
        with self.store.connect() as db:
            db.execute("INSERT INTO memberships VALUES(?,?, 'LOCAL_AI',0,0.95,'new replay',?)",
                       (collection, first["id"], "2026-11-14T00:00:00+00:00"))
        self.assertTrue(self.store.rollback_membership_reconciliation("reconcile-test"))
        restored = {row["origin"]: row for row in self.store.keywords_for(collection)}
        self.assertEqual(set(restored), {"LOCAL_AI", "USER"})
        self.assertEqual((restored["LOCAL_AI"]["score"], restored["LOCAL_AI"]["reason"]), (0.8, "old match"))

    def test_ai_unavailable_preserves_saved_keywords_and_manual_feedback(self):
        first = self.store.create({"name": "Sustainable fashion", "description": "Recycled denim and eco clothing"})
        second = self.store.create({"name": "Fashion", "description": "Denim clothing", "auto_match": False})
        source = "wikimedia_pageviews_top_per_country"
        rows = [
            {"article_display": "Upcycled Denim Jacket", "article_original": "Upcycled_Denim_Jacket",
             "project": "en.wikipedia", "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z"},
            {"article_display": "Mars Rover Mission", "article_original": "Mars_Rover_Mission",
             "project": "en.wikipedia", "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z"},
        ]
        sources = live_source(rows)
        result = self.service._process_fetch("live-fetch-1", sources)
        self.assertEqual(result["status"], "MODEL_UNAVAILABLE")
        self.assertEqual(result["available"], 2)
        self.assertEqual(result["assigned"], 0)
        candidate = keyword_record(rows[0], source, "LIVE")
        self.store.feedback_action(second, candidate, "ADD")
        self.store.feedback_action(first, candidate, "REMOVE")
        self.service._process_fetch("live-fetch-2", sources)
        self.assertEqual(self.store.keywords_for(first), [])
        self.assertEqual(len(self.store.keywords_for(second)), 1)
        replay = self.service._process_fetch("live-fetch-1", sources)
        self.assertEqual(replay["assigned"], 0)

    def test_fetch_does_not_load_the_legacy_local_embedding_model(self):
        source = live_source([{
            "article_display": "Outdoor equipment", "article_original": "Outdoor_equipment",
            "project": "en.wikipedia", "observation_date": "2026-11-10",
            "fetched_at": "2026-11-12T00:00:00Z",
        }])
        with patch.object(embeddings, "embed", side_effect=AssertionError("legacy model must stay inactive")):
            result = self.service._process_fetch("no-local-embedding", source)
        self.assertEqual(result["status"], "MODEL_UNAVAILABLE")
        self.assertEqual(result["available"], 1)
        self.assertEqual(self.store.search_keywords(query="Outdoor equipment")["total"], 1)

    def test_full_record_input_and_cache_reuse(self):
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('creation_mode','OFF')")
        records = [{"article_display": f"Mars Rover {i}", "article_original": f"Mars_Rover_{i}",
            "project": "en.wikipedia", "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z"}
            for i in range(212)]
        source = live_source(records)
        result = self.service._process_fetch("full-set", source)
        self.assertEqual(result["status"], "MODEL_UNAVAILABLE")
        self.assertEqual(result["available"], 212)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["algorithms"]["eligible_keywords"], 212)
        self.assertEqual(result["algorithms"]["executed_keywords"], 212)
        self.assertEqual(result["algorithms"]["registered_algorithms"], 13)
        self.assertEqual(self.store.search_keywords(limit=None, ids_only=True)["total"], 212)
        self.assertEqual(len(self.store.search_keywords(limit=None, ids_only=True)["ids"]), 212)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM algorithm_results").fetchone()[0], 212 * 4)
            self.assertEqual(db.execute("SELECT count(*) FROM algorithm_observations").fetchone()[0], 212)
            self.assertEqual(db.execute("SELECT count(*) FROM algorithm_results WHERE algorithm_id='observed-volume-rank' AND status='COMPLETED'").fetchone()[0], 212)
            self.assertEqual(db.execute("SELECT count(*) FROM algorithm_results WHERE algorithm_id='observed-percentile-rank' AND status='COMPLETED'").fetchone()[0], 212)
        with patch("app.collections.service._algorithm_digest_input",
                   side_effect=AssertionError("unchanged ledger should reuse analysis")):
            replay = self.service._process_fetch("full-set-again", source)
        self.assertEqual(replay["algorithms"]["executed_keywords"], 0)
        self.assertEqual(replay["algorithms"]["unchanged_keywords"], 212)

    def test_analysis_cache_invalidates_for_observation_result_and_registry_changes(self):
        from dataclasses import replace

        from app.composition import algorithm_engine

        service = CollectionService(self.store, fetch_model_inference_enabled=False)
        rows = [{"article_display": "Mars Rover", "article_original": "Mars_Rover",
                 "project": "en.wikipedia", "observation_date": "2026-11-10",
                 "fetched_at": "2026-11-12T00:00:00Z"}]
        service._process_fetch("cache-first", live_source(rows))
        with self.store.connect() as db:
            record = dict(db.execute("SELECT * FROM keywords").fetchone())
        with patch("app.collections.service._algorithm_digest_input",
                   side_effect=AssertionError("expected cache hit")):
            cached = service._analyze_persisted_keywords([record])
        self.assertEqual((cached["unchanged_keywords"], cached["executed_keywords"]), (1, 0))
        changed_record = keyword_record({
            "article_display": "Mars Rover", "article_original": "Mars_Rover",
            "project": "en.wikipedia", "observation_date": "2026-11-10",
            "fetched_at": "2026-11-12T00:00:00Z", "provider_rank": 1, "views_ceil": 1200,
        }, "wikimedia_pageviews_top_per_country", "LIVE")
        changed = self.store.persist_cleaned_keywords([changed_record])["records"]
        with patch("app.collections.service._algorithm_digest_input", wraps=__import__(
                "app.collections.service", fromlist=["_algorithm_digest_input"])._algorithm_digest_input) as encode:
            recomputed = service._analyze_persisted_keywords(changed)
        self.assertGreater(encode.call_count, 0)
        self.assertEqual(recomputed["recomputed_algorithms"], 4)
        with self.store.connect() as db:
            db.execute("DELETE FROM algorithm_results WHERE algorithm_id=(SELECT algorithm_id FROM algorithm_results LIMIT 1)")
        result = service._analyze_persisted_keywords(changed)
        self.assertEqual(result["recomputed_algorithms"], 1)
        registry = algorithm_engine().registry
        metadata = {key: registry.get(key).metadata for key in registry.keys()}
        first_key = next(iter(metadata))
        changed = dict(metadata)
        changed[first_key] = replace(metadata[first_key], version="future-test-version")
        with self.store.connect() as db:
            records = [dict(row) for row in db.execute("SELECT * FROM keywords WHERE eligible=1 ORDER BY id")]
        original_digest, changed_digest = self.store.run_algorithm_analysis(
            lambda session: (session.state_digest(records, metadata),
                             session.state_digest(records, changed))
        )
        self.assertNotEqual(original_digest, changed_digest)

    def test_repeated_observation_avoids_writes_but_changed_evidence_is_saved(self):
        live_store = CollectionStore(Path(self.temp.name) / "unchanged.sqlite", data_mode_scope="LIVE")
        service = CollectionService(live_store, fetch_model_inference_enabled=False)
        rows = [{"article_display": "Mars Rover", "article_original": "Mars_Rover",
                 "project": "en.wikipedia", "observation_date": "2026-11-10",
                 "fetched_at": "2026-11-12T00:00:00Z", "views_ceil": 1000,
                 "provider_rank": 1}]
        source = live_source(rows)
        self.assertEqual(service._process_fetch("unchanged-first", source)["status"],
                         "MODEL_UNAVAILABLE")
        with live_store.connect() as db:
            db.executescript("""
                CREATE TABLE update_audit(kind TEXT NOT NULL);
                CREATE TRIGGER audit_keyword_update AFTER UPDATE ON keywords BEGIN
                    INSERT INTO update_audit VALUES('keyword'); END;
                CREATE TRIGGER audit_observation_update AFTER UPDATE ON algorithm_observations BEGIN
                    INSERT INTO update_audit VALUES('observation'); END;
            """)
        service._process_fetch("unchanged-second", source)
        with live_store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM update_audit").fetchone()[0], 0)
        rows[0]["views_ceil"] = 1100
        service._process_fetch("changed-third", source)
        with live_store.connect() as db:
            self.assertEqual(dict(db.execute("SELECT kind,count(*) FROM update_audit GROUP BY kind")),
                             {"keyword": 1, "observation": 1})
            self.assertEqual(db.execute("SELECT value FROM algorithm_observations").fetchone()[0], 1100)
            identifier = db.execute("SELECT id FROM keywords").fetchone()[0]
            db.execute("INSERT INTO keyword_decisions VALUES(?,'EXCLUDED','test','2026-11-13')",
                       (identifier,))
        service._process_fetch("excluded-fourth", source)
        with live_store.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT eligible,rejection_reason FROM keywords").fetchone()),
                             (0, "USER_EXCLUDED"))
            db.execute("UPDATE keyword_decisions SET decision='RESTORED' WHERE keyword_id=?",
                       (identifier,))
        service._process_fetch("restored-fifth", source)
        with live_store.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT eligible,rejection_reason FROM keywords").fetchone()),
                             (1, None))

    def test_cohort_digest_encoding_matches_original_for_each_excluded_keyword(self):
        from types import SimpleNamespace

        from app.collections.service import (
            _COHORT_MARKER,
            _COHORT_MARKER_JSON,
            _cohort_json_index,
            _cohort_json_without,
            _serialized_cohort,
        )
        peers = [
            (identifier, SimpleNamespace(scope=SimpleNamespace(project=project, article=title),
                                         value=value, rank=rank))
            for identifier, project, title, value, rank in [
                ("a", "en.wikipedia", "Café", 10, 1),
                ("b", "en.wikipedia", "Mars", 20, 2),
                ("c", "en.wikipedia", "Mars", 20, 2),
            ]
        ]
        encoded_peers = _serialized_cohort(peers)
        for excluded in ("a", "b", "c", "absent"):
            original = sorted((point.scope.project, point.scope.article, point.value, point.rank)
                              for identifier, point in peers if identifier != excluded)
            template = {"algorithm": {"family": "popularity"},
                        "input": {"scope": {"article": "Café"}, "cohort": _COHORT_MARKER}}
            actual = json.dumps(template, sort_keys=True, separators=(",", ":")).encode().replace(
                b'"cohort":' + _COHORT_MARKER_JSON,
                b'"cohort":' + _cohort_json_without(encoded_peers, excluded), 1)
            expected = json.dumps({"algorithm": template["algorithm"],
                                   "input": {"scope": {"article": "Café"}, "cohort": original}},
                                  sort_keys=True, separators=(",", ":")).encode()
            self.assertEqual(actual, expected)
            self.assertEqual(_cohort_json_without(encoded_peers, excluded,
                                                   _cohort_json_index(encoded_peers)),
                             _cohort_json_without(encoded_peers, excluded))
        duplicated = encoded_peers + [encoded_peers[0]]
        self.assertIsNone(_cohort_json_index(duplicated))
        self.assertEqual(_cohort_json_without(duplicated, "a", _cohort_json_index(duplicated)),
                         _cohort_json_without(duplicated, "a"))
        self.assertEqual(_cohort_json_without([], "absent", _cohort_json_index([])), b"[]")

    def test_algorithm_digest_encoding_matches_original_registry_inputs(self):
        from dataclasses import asdict

        from app.collections.service import (
            _COHORT_MARKER,
            _COHORT_MARKER_JSON,
            _algorithm_digest_input,
            _algorithm_digest_prefix,
        )
        from app.composition import algorithm_engine

        registry = algorithm_engine().registry
        for cohort, encoded_cohort in [(_COHORT_MARKER, b'[["Caf\\u00e9",2]]'),
                                       ([('Café', 2)], None)]:
            input_data = {"scope": {"article": "Café"}, "date": "2026-11-20",
                          "current": 3, "history": [("2026-11-19", "OBSERVED", 2)],
                          "current_snapshot_status": "PARTIAL", "cohort": cohort}
            for key in registry.keys():
                metadata = registry.get(key).metadata
                metadata_dict = asdict(metadata)
                original_input = dict(input_data)
                popularity = metadata.family == "popularity"
                if not popularity:
                    original_input.pop("cohort")
                original = json.dumps({"input": original_input, "algorithm": metadata_dict},
                                      sort_keys=True, separators=(",", ":")).encode()
                if popularity and encoded_cohort is not None:
                    original = original.replace(b'"cohort":' + _COHORT_MARKER_JSON,
                                                b'"cohort":' + encoded_cohort, 1)
                optimized = (_algorithm_digest_prefix(metadata_dict)
                             + _algorithm_digest_input(input_data, popularity=popularity,
                                                       cohort_bytes=encoded_cohort) + b"}")
                self.assertEqual(optimized, original, key)

    def test_localized_wikimedia_special_and_main_pages_are_filtered(self):
        for project, title in (
            ("el.wikisource", "Ειδικό:Αναζήτηση"),
            ("la.wiktionary", "Specialis:Quaerere"),
            ("et.wiktionary", "Eri:Otsimine"),
            ("sl.wikibooks", "Posebno:Iskanje"),
            ("nl.wikipedia", "Hoofdpagina"),
            ("en.wiktionary", "Wiktionary:Main Page"),
            ("wikifunctions", "Wikifunctions:Main Page"),
        ):
            with self.subTest(title=title):
                self.assertIsNotNone(collection_technical_reason(project, title))

    def test_no_local_similarity_pipeline_creates_collection_proposals(self):
        source = live_source([
            {"article_display": f"Outdoor topic {index}", "article_original": f"Outdoor_topic_{index}",
             "project": "en.wikipedia", "observation_date": "2026-11-10",
             "fetched_at": "2026-11-12T00:00:00Z"}
            for index in range(4)
        ])
        result = self.service._process_fetch("no-local-proposals", source)
        self.assertEqual(result["status"], "MODEL_UNAVAILABLE")
        self.assertEqual(result["suggestions"], 0)
        self.assertEqual(self.store.suggestions(), [])

    def test_suggestion_acceptance_and_deleted_name_suppression(self):
        rows = [{"article_display": f"Sustainable fashion item {i}", "article_original": f"Sustainable_fashion_item_{i}",
            "project": "en.wikipedia", "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z"}
            for i in range(3)]
        source = live_source(rows)
        result = self.service._process_fetch("suggestion-fetch", source)
        self.assertEqual(result["suggestions"], 0)
        keyword_ids = self.store.search_keywords(query="Sustainable fashion item", limit=None, ids_only=True)["ids"]
        self.assertEqual(len(keyword_ids), 3)
        stamp = "2026-11-26T00:00:00+00:00"
        self.store.create_proposal("legacy-proposal", "Sustainable fashion", "Legacy local proposal",
                                   keyword_ids, "legacy compatibility", stamp, stamp)
        proposal = self.store.suggestions()[0]
        self.store.patch_proposal(
            proposal["id"], support_ids=keyword_ids[:2],
            allowed_sources={WIKIMEDIA_SOURCE_ID},
        )
        self.assertEqual(self.store.suggestions()[0]["support_ids"], keyword_ids[:2])
        self.store.patch_proposal(
            proposal["id"], support_ids=keyword_ids,
            allowed_sources={WIKIMEDIA_SOURCE_ID},
        )
        self.assertEqual(self.store.suggestions()[0]["support_ids"], keyword_ids)
        accepted = self.store.resolve_proposal(
            proposal["id"], True, name="Reviewed sustainable fashion", description="Reviewed evidence context",
        )
        self.assertEqual(self.store.resolve_proposal(proposal["id"], True), accepted)
        created = next(item for item in self.store.list_collections() if item["id"] == accepted)
        self.assertEqual(created["name"], "Reviewed sustainable fashion")
        self.assertEqual(created["description"], "Reviewed evidence context")
        self.assertEqual(len(self.store.keywords_for(accepted)), 3)
        self.assertEqual(self.store.resolve_proposal(proposal["id"], False), None)

    def test_fetch_without_postgres_ai_does_not_auto_create_collections(self):
        rows = [{"article_display": f"Sustainable fashion item {i}", "article_original": f"Sustainable_fashion_item_{i}",
            "project": "en.wikipedia", "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z"}
            for i in range(3)]
        source = live_source(rows)
        result = self.service._process_fetch("auto-create-fetch", source)
        self.assertEqual(result["status"], "MODEL_UNAVAILABLE")
        self.assertEqual(result["created_collections"], 0)
        self.assertEqual(result["suggestions"], 0)
        self.assertFalse(any(item["name"] == "Sustainable fashion" for item in self.store.list_collections()))

    def test_ambiguous_equal_similarity_stays_unassigned(self):
        first = self.store.create({"name": "Fashion and clothing", "description": "denim"})
        second = self.store.create({"name": "Sustainable fashion", "description": "recycled clothing"})
        source = live_source([
            {"article_display": "Upcycled denim jacket", "article_original": "Upcycled_denim_jacket",
             "project": "en.wikipedia", "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z"}])
        result = self.service._process_fetch("ambiguous-fetch", source)
        self.assertEqual(result["status"], "MODEL_UNAVAILABLE")
        self.assertEqual(result["assigned"], 0)
        self.assertEqual(self.store.keywords_for(first), [])
        self.assertEqual(self.store.keywords_for(second), [])

    def test_model_unavailable_does_not_break_fetch_job(self):
        source = live_source([
            {"article_display": "Mars Rover", "article_original": "Mars_Rover", "project": "en.wikipedia",
             "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z"}])
        result = self.service._process_fetch("offline", source)
        self.assertEqual(result["status"], "MODEL_UNAVAILABLE")
        self.assertEqual(result["available"], 1)
        self.assertEqual(result["unassigned"], 1)
        with self.store.connect() as db:
            saved = db.execute("SELECT keyword,eligible FROM keywords").fetchone()
            snapshot = db.execute("SELECT data_mode,received_record_count,processing_status "
                                  "FROM dataset_snapshots WHERE dataset_id='offline'").fetchone()
        self.assertEqual(tuple(saved), ("Mars Rover", 1))
        self.assertEqual(tuple(snapshot), ("LIVE", 1, "MODEL_UNAVAILABLE"))
        self.assertEqual(self.store.latest_dataset()["dataset_id"], "offline")

    def test_fetch_without_ai_engine_reports_disabled_and_skips_embedding(self):
        source = live_source([
            {"article_display": "Mars Rover", "article_original": "Mars_Rover", "project": "en.wikipedia",
             "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z"}])
        service = CollectionService(self.store, fetch_model_inference_enabled=False)
        with patch.object(embeddings, "embed", side_effect=AssertionError("legacy model must not run")):
            result = service._process_fetch("fetch-disabled-model", source)

        self.assertEqual(result["status"], "MODEL_UNAVAILABLE")
        self.assertEqual(result["error_code"], "AI_DISABLED")
        self.assertEqual(result["available"], 1)
        self.assertEqual(result["unassigned"], 1)
        with self.store.connect() as db:
            keyword = db.execute(
                "SELECT keyword,eligible FROM keywords WHERE keyword='Mars Rover'"
            ).fetchone()
            job = db.execute(
                "SELECT status,error_code FROM fetch_jobs WHERE fetch_id='fetch-disabled-model'"
            ).fetchone()
            snapshot = db.execute(
                "SELECT data_mode,received_record_count,source_manifest,processing_status "
                "FROM dataset_snapshots "
                "WHERE dataset_id='fetch-disabled-model'"
            ).fetchone()
        self.assertEqual(tuple(keyword), ("Mars Rover", 1))
        self.assertEqual(tuple(job), ("MODEL_UNAVAILABLE", "AI_DISABLED"))
        self.assertEqual(tuple(snapshot[:2]), ("LIVE", 1))
        self.assertEqual(json.loads(snapshot["source_manifest"])[0]["status"], "SUCCESS")
        self.assertEqual(snapshot["processing_status"], "MODEL_UNAVAILABLE")

    def test_model_unavailable_keeps_keyword_and_algorithm_ledger_for_retry(self):
        live_store = CollectionStore(Path(self.temp.name) / "deferred-resume.sqlite",
                                     data_mode_scope="LIVE")
        source = live_source([
            {"article_display": "Mars Rover", "article_original": "Mars_Rover", "project": "en.wikipedia",
             "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z"}])
        first = CollectionService(live_store)._process_fetch("deferred-resume", source)
        self.assertEqual(first["status"], "MODEL_UNAVAILABLE")
        self.assertGreater(first["algorithms"]["executed_keywords"], 0)
        with live_store.connect() as db:
            original_manifest = db.execute(
                "SELECT source_manifest FROM dataset_snapshots WHERE dataset_id='deferred-resume'"
            ).fetchone()[0]
            original_results = db.execute("SELECT count(*) FROM algorithm_results").fetchone()[0]

        resumed = CollectionService(live_store)._process_fetch(
            "deferred-resume", [], cached_context_only=True, resume_model_unavailable=True,
        )
        self.assertEqual(resumed["status"], "MODEL_UNAVAILABLE")
        with live_store.connect() as db:
            snapshot = db.execute(
                "SELECT source_manifest,processing_status FROM dataset_snapshots "
                "WHERE dataset_id='deferred-resume'"
            ).fetchone()
            keywords = db.execute("SELECT count(*) FROM keywords WHERE data_mode='LIVE'").fetchone()[0]
            algorithm_results = db.execute("SELECT count(*) FROM algorithm_results").fetchone()[0]
        self.assertEqual(snapshot["source_manifest"], original_manifest)
        self.assertEqual(keywords, 1)
        self.assertEqual(algorithm_results, original_results)

    def test_model_resume_does_not_start_a_second_active_processing_attempt(self):
        live_store = CollectionStore(Path(self.temp.name) / "deferred-active.sqlite",
                                     data_mode_scope="LIVE")
        source = live_source([
            {"article_display": "Mars Rover", "article_original": "Mars_Rover", "project": "en.wikipedia",
             "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z"}])
        deferred = CollectionService(live_store, fetch_model_inference_enabled=False)
        deferred._process_fetch("deferred-active", source)
        with live_store.connect() as db:
            db.execute("UPDATE fetch_jobs SET status='PROCESSING' WHERE fetch_id='deferred-active'")

        with patch.object(embeddings, "embed", side_effect=AssertionError("duplicate inference must not run")):
            result = CollectionService(live_store)._process_fetch(
                "deferred-active", [], cached_context_only=True,
                resume_model_unavailable=True,
            )
        self.assertEqual(result["status"], "PROCESSING")
        with live_store.connect() as db:
            snapshot = db.execute(
                "SELECT processing_status FROM dataset_snapshots WHERE dataset_id='deferred-active'"
            ).fetchone()[0]
        self.assertEqual(snapshot, "MODEL_UNAVAILABLE")

    def test_bulk_feedback_actions_are_atomic_and_durable(self):
        target = self.store.create({"name": "Space", "auto_match": False})
        rows = [{"article_display": name, "article_original": name.replace(" ", "_"),
                 "project": "en.wikipedia", "observation_date": "2026-11-10",
                 "fetched_at": "2026-11-12T00:00:00Z", "provider_rank": index,
                 "views_ceil": 1000}
                for index, name in enumerate(("Mars Rover Mission", "Lunar Gateway"), start=1)]
        source = live_source(rows)
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            self.service._process_fetch("bulk-seed", source)
        ids = [keyword_record(row, source[0]["source_id"], "LIVE")["id"] for row in rows]
        result = self.store.bulk_membership_action(ids + ["missing"], "ADD", collection_id=target)
        self.assertEqual(result, {"updated": 2, "skipped": 1, "failed": 0})
        approved = self.store.bulk_membership_action(ids, "APPROVE", collection_id=target)
        self.assertEqual(approved["updated"], 2)
        self.assertTrue(all(item["pinned"] and item["origin"] == "USER"
                            for item in self.store.keywords_for(target)))
        rejected = self.store.bulk_membership_action(ids, "REJECT", collection_id=target)
        self.assertEqual(rejected["updated"], 2)
        self.assertEqual(self.store.keywords_for(target), [])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM feedback WHERE action='REMOVE'").fetchone()[0], 2)

    def test_ai_unavailable_does_not_propose_similar_person_names(self):
        source = live_source([
            {"article_display": name, "article_original": name.replace(" ", "_"),
             "project": "en.wikipedia", "observation_date": "2026-11-10",
             "fetched_at": "2026-11-12T00:00:00Z"}
            for name in ("Harry R. Truman", "Harry S. Truman")
        ])
        result = self.service._process_fetch("no-person-proposal", source)
        self.assertEqual(result["status"], "MODEL_UNAVAILABLE")
        self.assertEqual(self.store.suggestions(), [])

    def test_ai_unavailable_does_not_propose_platform_names(self):
        source = live_source([
            {"article_display": name, "article_original": name.replace(" ", "_"),
             "project": "en.wikipedia", "observation_date": "2026-11-10",
             "fetched_at": "2026-11-12T00:00:00Z"}
            for name in ("MediaWiki", "Wikimedia APIs", "Wikipedia")
        ])
        result = self.service._process_fetch("no-platform-proposal", source)
        self.assertEqual(result["status"], "MODEL_UNAVAILABLE")
        self.assertEqual(self.store.suggestions(), [])
        self.assertEqual(self.store.suggestions(), [])


if __name__ == "__main__":
    unittest.main()
