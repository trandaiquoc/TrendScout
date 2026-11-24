import hashlib
import json
import os
import tempfile
import unittest
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
        self.assertEqual(len(catalog_response.json()["algorithms"]), 12)
        self.assertEqual(results_response.status_code, 200)
        summaries = {row["algorithm_id"]: row for row in results_response.json()["summary"]}
        self.assertEqual(summaries["observed-volume-rank"]["completed"], 2)
        self.assertIn("NOT_YET_EVALUATED", results_response.json()["evaluation_status"])
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
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            outcome = live_service._process_fetch("current-live", live_source([dict(row)]))
        self.assertEqual(outcome["status"], "COMPLETED")
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
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            seeded = self.service._process_fetch("existing-google-safe-baseline", live_source([row]))
        self.assertEqual(seeded["processed"], 1)
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
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            outcome = service._process_fetch("google-only-live", google_source)

        self.assertEqual(outcome["status"], "COMPLETED")
        self.assertEqual(outcome["available"], 1)
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

    def test_multiple_membership_semantic_match_negative_feedback_and_replay(self):
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
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            result = self.service._process_fetch("live-fetch-1", sources)
            self.assertEqual(result["available"], 2)
            self.assertEqual(result["processed"], 2)
            self.assertEqual(result["assigned"], 1)
            self.assertEqual(len(self.store.keywords_for(first)), 1)
            candidate = keyword_record(rows[0], source, "LIVE")
            self.store.feedback_action(second, candidate, "ADD")
            self.store.feedback_action(first, candidate, "REMOVE")
            self.service._process_fetch("live-fetch-2", sources)
            self.assertEqual(self.store.keywords_for(first), [])
            self.assertEqual(len(self.store.keywords_for(second)), 1)
            replay = self.service._process_fetch("live-fetch-1", sources)
            self.assertEqual(replay["assigned"], 1)

    def test_local_embedding_input_is_bounded_without_changing_source_text(self):
        calls = []

        def measured(texts):
            calls.append(texts)
            return [[1.0, 0.0, 0.0] for _ in texts]

        self.service._thread_state.embedding_counts = {
            "cached_embeddings": 0, "inferred_embeddings": 0,
        }
        long_context = "verified context " * 100
        with patch.object(embeddings, "embed", side_effect=measured):
            vectors = self.service._embed_cached([long_context], [0])

        self.assertEqual(len(vectors), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][0]), embeddings.MAX_INPUT_CHARS)

    def test_full_record_input_and_cache_reuse(self):
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('creation_mode','OFF')")
        records = [{"article_display": f"Mars Rover {i}", "article_original": f"Mars_Rover_{i}",
            "project": "en.wikipedia", "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z"}
            for i in range(212)]
        calls = []
        def measured(texts):
            calls.append(len(texts))
            return fake_embed(texts)
        source = live_source(records)
        with patch.object(embeddings, "embed", side_effect=measured):
            result = self.service._process_fetch("full-set", source)
            self.assertEqual(result["available"], 212)
            self.assertEqual(result["processed"], 212)
            self.assertEqual(result["skipped"], 0)
            self.assertEqual(result["algorithms"]["eligible_keywords"], 212)
            self.assertEqual(result["algorithms"]["executed_keywords"], 212)
            self.assertEqual(result["algorithms"]["registered_algorithms"], 12)
            self.assertTrue(result["membership_reconciliation_id"].startswith("reconcile-"))
            self.assertEqual(self.store.search_keywords(limit=None, ids_only=True)["total"], 212)
            self.assertEqual(len(self.store.search_keywords(limit=None, ids_only=True)["ids"]), 212)
            with self.store.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM algorithm_results").fetchone()[0], 212 * 12)
                self.assertEqual(db.execute("SELECT count(*) FROM algorithm_observations").fetchone()[0], 212)
                self.assertEqual(db.execute("SELECT status FROM membership_reconciliation_runs WHERE id=?",
                    (result["membership_reconciliation_id"],)).fetchone()[0], "APPLIED")
                self.assertEqual(db.execute("SELECT count(*) FROM algorithm_results WHERE algorithm_id='observed-volume-rank' AND status='COMPLETED'").fetchone()[0], 212)
                self.assertEqual(db.execute("SELECT count(*) FROM algorithm_results WHERE algorithm_id='observed-percentile-rank' AND status='COMPLETED'").fetchone()[0], 212)
            previous_calls = sum(calls)
            replay = self.service._process_fetch("full-set-again", source)
            self.assertEqual(replay["algorithms"]["executed_keywords"], 0)
            self.assertEqual(replay["algorithms"]["unchanged_keywords"], 212)
            self.assertEqual(sum(calls), previous_calls)

    def test_cohort_digest_encoding_matches_original_for_each_excluded_keyword(self):
        from types import SimpleNamespace
        from app.collections.service import (
            _COHORT_MARKER, _COHORT_MARKER_JSON, _serialized_cohort, _cohort_json_without,
            _cohort_json_index,
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
            _COHORT_MARKER, _COHORT_MARKER_JSON,
            _algorithm_digest_input, _algorithm_digest_prefix,
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

    def test_proposals_require_pairwise_cohesion(self):
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('creation_mode','SUGGEST_ONLY')")
        titles = ["A topic", "B topic", "C topic", "D topic"]
        records = [("id-" + title[0], title) for title in titles]
        vectors = [[float(index)] for index in range(4)]
        similarity = {
            (0, 1): 0.90, (0, 2): 0.90, (1, 2): 0.40,
            (2, 3): 0.90, (0, 3): 0.10, (1, 3): 0.10,
        }

        def pair_score(left, right):
            a, b = sorted((int(left[0]), int(right[0])))
            return 1.0 if a == b else similarity.get((a, b), 0.0)

        with patch("app.collections.service.cosine", side_effect=pair_score):
            result = self.service._propose(
                "cohesion-test",
                [({"id": identifier, "keyword": title}, vector)
                 for (identifier, title), vector in zip(records, vectors)],
                [],
            )
        self.assertEqual(result, (0, 0))
        self.assertEqual(self.store.suggestions(), [])

    def test_suggestion_acceptance_and_deleted_name_suppression(self):
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('creation_mode','SUGGEST_ONLY')")
        rows = [{"article_display": f"Sustainable fashion item {i}", "article_original": f"Sustainable_fashion_item_{i}",
            "project": "en.wikipedia", "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z"}
            for i in range(3)]
        source = live_source(rows)
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            result = self.service._process_fetch("suggestion-fetch", source)
            self.assertEqual(result["suggestions"], 1)
        proposal = self.store.suggestions()[0]
        accepted = self.store.resolve_proposal(proposal["id"], True)
        self.assertEqual(len(self.store.keywords_for(accepted)), 3)
        self.assertEqual(self.store.resolve_proposal(proposal["id"], False), None)

    def test_auto_create_requires_three_supports_and_delete_is_suppressed(self):
        rows = [{"article_display": f"Sustainable fashion item {i}", "article_original": f"Sustainable_fashion_item_{i}",
            "project": "en.wikipedia", "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z"}
            for i in range(3)]
        source = live_source(rows)
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            result = self.service._process_fetch("auto-create-fetch", source)
            self.assertEqual(result["created_collections"], 1)
            created = self.store.list_collections()[0]
            self.assertEqual(created["created_by"], "AI")
            self.assertEqual(len(self.store.keywords_for(created["id"])), 3)
            self.store.delete(created["id"])
            result = self.service._process_fetch("after-delete-fetch", source)
        self.assertEqual(result["created_collections"], 0)
        self.assertTrue(all(item["category_id"] for item in self.store.list_collections()))
        self.assertFalse(any(item["name"] == created["name"] for item in self.store.list_collections()))

    def test_ambiguous_equal_similarity_stays_unassigned(self):
        first = self.store.create({"name": "Fashion and clothing", "description": "denim"})
        second = self.store.create({"name": "Sustainable fashion", "description": "recycled clothing"})
        source = live_source([
            {"article_display": "Upcycled denim jacket", "article_original": "Upcycled_denim_jacket",
             "project": "en.wikipedia", "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z"}])
        with patch.object(embeddings, "embed", side_effect=fake_embed):
            result = self.service._process_fetch("ambiguous-fetch", source)
        self.assertEqual(result["assigned"], 0)
        self.assertEqual(self.store.keywords_for(first), [])
        self.assertEqual(self.store.keywords_for(second), [])

    def test_model_unavailable_does_not_break_fetch_job(self):
        source = live_source([
            {"article_display": "Mars Rover", "article_original": "Mars_Rover", "project": "en.wikipedia",
             "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z"}])
        with patch.object(embeddings, "embed", side_effect=RuntimeError("offline model missing")):
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

    def test_deferred_fetch_model_keeps_raw_keyword_and_skips_embedding(self):
        source = live_source([
            {"article_display": "Mars Rover", "article_original": "Mars_Rover", "project": "en.wikipedia",
             "observation_date": "2026-11-10", "fetched_at": "2026-11-12T00:00:00Z"}])
        service = CollectionService(self.store, fetch_model_inference_enabled=False)
        with patch.object(embeddings, "embed", side_effect=AssertionError("model inference must not run")):
            result = service._process_fetch("deferred-model", source)

        self.assertEqual(result["status"], "MODEL_UNAVAILABLE")
        self.assertEqual(result["error_code"], "MODEL_INFERENCE_DEFERRED")
        self.assertEqual(result["available"], 1)
        self.assertEqual(result["unassigned"], 1)
        with self.store.connect() as db:
            keyword = db.execute(
                "SELECT keyword,eligible FROM keywords WHERE keyword='Mars Rover'"
            ).fetchone()
            job = db.execute(
                "SELECT status,error_code FROM fetch_jobs WHERE fetch_id='deferred-model'"
            ).fetchone()
            snapshot = db.execute(
                "SELECT data_mode,received_record_count,processing_status FROM dataset_snapshots "
                "WHERE dataset_id='deferred-model'"
            ).fetchone()
        self.assertEqual(tuple(keyword), ("Mars Rover", 1))
        self.assertEqual(tuple(job), ("MODEL_UNAVAILABLE", "MODEL_INFERENCE_DEFERRED"))
        self.assertEqual(tuple(snapshot), ("LIVE", 1, "MODEL_UNAVAILABLE"))

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

    def test_auto_proposal_rejects_single_word_name_from_distinct_titles(self):
        unassigned = [
            ({"id": "harry-r", "keyword": "Harry R. Truman"}, [1.0, 0.0]),
            ({"id": "harry-s", "keyword": "Harry S. Truman"}, [1.0, 0.0]),
        ]
        with patch("app.collections.service.cosine", return_value=0.99):
            created, suggestions = self.service._propose("test-single-word-name", unassigned, [])

        self.assertEqual((created, suggestions), (0, 0))
        self.assertEqual(self.store.list_collections(), [])

    def test_platform_titles_do_not_form_auto_collection_proposals(self):
        records = [
            {"id": "a", "keyword": "MediaWiki", "normalized": "mediawiki"},
            {"id": "b", "keyword": "Wikimedia APIs", "normalized": "wikimedia apis"},
            {"id": "c", "keyword": "Wikipedia", "normalized": "wikipedia"},
        ]
        with patch("app.collections.service.cosine", return_value=0.95):
            result = self.service._propose("platform-noise", [(row, [1.0]) for row in records], [])
        self.assertEqual(result, (0, 0))
        self.assertEqual(self.store.suggestions(), [])


if __name__ == "__main__":
    unittest.main()
