import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from app.collections.service import CollectionService
from app.collections.store import CollectionStore
from app.main import create_app
from app.settings import Settings
from app.storage.fake import FakeStorage
from app.taxonomy.registry import resolve as resolve_category


class KeywordWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CollectionStore(Path(self.temp.name) / "collections.sqlite")
        self.root = self.store.create({"name": "Research topics", "auto_match": False})
        self.child = self.store.create({"name": "Clothing", "parent_id": self.root, "auto_match": False})
        self.target = self.store.create({"name": "Review", "auto_match": False})
        self._seed_keywords()
        self.app = create_app(read_only=True, settings=Settings.from_environment({
            "TREND_SCOUT_ENVIRONMENT": "test", "TREND_SCOUT_READ_ONLY": "true",
        }))
        self.app.state.collection_service = CollectionService(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def _seed_keywords(self):
        rows = [
            ("k-classified", "Recycled denim", "2026-11-10", {"views_ceil": 0, "provider_rank": 4}, 1, "CLASSIFIED"),
            ("k-ambiguous", "Denim collection", "2026-11-11", {"views_ceil": 25, "provider_rank": 2}, 1, "AMBIGUOUS"),
            ("k-unknown", "A name without context", "2026-11-12", {}, 1, "UNKNOWN"),
            ("k-excluded", "Excluded record", "2026-11-13", {"views_ceil": 9}, 0, "UNKNOWN"),
        ]
        with self.store.connect() as db:
            for identifier, keyword, observed, evidence, eligible, status in rows:
                db.execute("""INSERT INTO keywords
                    (id,keyword,normalized,source,project,observed_at,data_mode,evidence,context,eligible,rejection_reason)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (identifier, keyword, keyword.casefold(), "wikimedia_pageviews_top_per_country",
                    "en.wikipedia", observed, "LIVE", json.dumps(evidence), "", eligible,
                    "USER_EXCLUDED" if not eligible else None))
                db.execute("""INSERT INTO classification_results
                    (keyword_id,entity_type,entity_id,entity_description,topic_category_ids,subcategory_ids,
                    classification_status,confidence_level,commercial_relevance,classification_method,
                    classification_version,taxonomy_version,rule_evidence,source_evidence,review_status,classified_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (identifier, "unknown", None, "", "[]", "[]", status,
                    "NONE", "NOT_ASSESSED", "TEST_FIXTURE", "fixture-v1", "topics-v1.3", "[]",
                    json.dumps({"source": "fixture-source", "scope": {"country_scope": "US"}}),
                    "NEEDS_REVIEW", "2026-11-13T00:00:00Z"))
            db.execute("INSERT INTO memberships VALUES(?,?, 'LOCAL_AI',0,0.91,'AI evidence',?)",
                       (self.child, "k-classified", "2026-11-13T00:00:00Z"))
            db.execute("INSERT INTO memberships VALUES(?,?, 'LOCAL_AI',0,0.72,'AI evidence',?)",
                       (self.child, "k-ambiguous", "2026-11-13T00:00:00Z"))
            db.execute("INSERT INTO memberships VALUES(?,?, 'USER',1,NULL,'User pin',?)",
                       (self.root, "k-ambiguous", "2026-11-13T00:00:00Z"))
            db.execute("INSERT OR REPLACE INTO keyword_decisions VALUES(?,'EXCLUDED','Owner decision',?)",
                       ("k-excluded", "2026-11-13T00:00:00Z"))
            db.execute("INSERT INTO algorithm_results VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       ("k-classified", "observed-volume-rank", "v1", "COMPLETED", None, 1.0,
                        "observed_volume_rank", "rank", json.dumps({"rank": 1}), "digest", "2026-11-13T00:00:00Z"))

    def _request(self, method, path, **kwargs):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver") as client:
                return await client.request(method, path, **kwargs)
        return __import__("asyncio").run(run())

    def test_unique_summary_and_tree_counts_do_not_multiply_memberships(self):
        summary = self.store.workbench_summary()
        self.assertEqual((summary["stored_keywords"], summary["eligible_keywords"], summary["excluded_keywords"]), (4, 3, 1))
        self.assertEqual((summary["classified_keywords"], summary["ambiguous_keywords"], summary["unknown_keywords"]), (1, 1, 1))
        self.assertEqual((summary["unclassified_keywords"], summary["ai_assigned_keywords"],
                          summary["manually_managed_keywords"], summary["mixed_origin_keywords"]), (1, 2, 1, 1))
        self.assertEqual(summary["assigned_keywords"], 2)
        tree = {item["id"]: item for item in self.store.list_collections()}
        self.assertEqual(tree[self.root]["direct_membership_count"], 1)
        self.assertEqual(tree[self.root]["subtree_unique_keyword_count"], 2)
        self.assertEqual(tree[self.child]["subtree_unique_keyword_count"], 2)

    def test_compact_keyword_list_keeps_filters_and_full_detail(self):
        full = self._request("GET", "/api/v1/datasets/latest/keywords?evidence=algorithm")
        compact = self._request("GET", "/api/v1/datasets/latest/keywords?evidence=algorithm&include_algorithm_results=false")
        self.assertEqual((full.status_code, compact.status_code), (200, 200))
        expected, actual = full.json(), compact.json()
        self.assertEqual(expected["total"], actual["total"])
        self.assertEqual([item["id"] for item in expected["items"]],
                         [item["id"] for item in actual["items"]])
        self.assertTrue(expected["items"][0]["algorithm_results"])
        self.assertNotIn("algorithm_results", actual["items"][0])
        self.assertEqual({k: v for k, v in expected["items"][0].items() if k != "algorithm_results"},
                         actual["items"][0])
        detail = self._request("GET", "/api/v1/collections/keywords/k-classified")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["algorithm_results"][0]["result"], {"rank": 1})

    def test_system_ineligible_is_distinct_from_user_exclusion_and_unknown_context(self):
        with self.store.connect() as db:
            db.execute("UPDATE classification_results SET classification_status='INELIGIBLE' WHERE keyword_id='k-unknown'")
        summary = self.store.workbench_summary()
        self.assertEqual(summary["ineligible_topic_keywords"], 1)
        self.assertEqual(summary["unknown_keywords"], 0)
        self.assertEqual(summary["excluded_keywords"], 1)
        self.assertEqual(summary["eligible_keywords"], 2)
        analytics = self._request("GET", "/api/v1/analytics/overview").json()
        self.assertEqual(analytics["keyword_totals"]["unclassified"], 1)

    def test_non_topic_is_not_counted_as_unassigned_or_actionable(self):
        with self.store.connect() as db:
            db.execute("UPDATE classification_results SET classification_status='INELIGIBLE',classification_reason='NON_TOPIC_SYSTEM_PAGE' WHERE keyword_id='k-unknown'")
        summary = self.store.workbench_summary()
        self.assertEqual(summary["unclassified_keywords"], 0)
        self.assertEqual(summary["unknown_keywords"], 0)
        self.assertEqual(summary["ineligible_topic_keywords"], 1)
        self.assertEqual(summary["eligible_keywords"], 2)
        self.assertEqual(summary["excluded_keywords"], 1)
        inbox = self.store.search_keywords(state="unassigned", limit=None)
        self.assertNotIn("k-unknown", {item["id"] for item in inbox["items"]})
        all_items = {item["id"]: item for item in self.store.search_keywords(state="all", limit=None)["items"]}
        self.assertIn("k-unknown", all_items)
        self.assertEqual(all_items["k-unknown"]["eligible"], 0)
        self.assertFalse(all_items["k-unknown"]["topic_eligible"])
        self.assertEqual({item["id"] for item in self.store.search_keywords(
            state="non_topic", limit=None)["items"]}, {"k-unknown"})
        self.assertNotIn("k-unknown", {item["id"] for item in self.store.search_keywords(
            state="excluded", limit=None)["items"]})
        filtered = self._request("GET", "/api/v1/datasets/latest/keywords?state=non_topic")
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual([item["id"] for item in filtered.json()["items"]], ["k-unknown"])
        analytics = self._request("GET", "/api/v1/analytics/overview").json()
        self.assertEqual(analytics["keyword_totals"]["eligible"], 2)
        self.assertEqual(analytics["keyword_totals"]["ineligible"], 1)

    def test_targeted_cached_reclassification_updates_results_and_taxonomy_memberships(self):
        ids = ("MediaWiki", "Istimewa:Pencarian", "Home", "Thank You/en", "YouTube", "Katie Miller")
        rows = [
            (ids[0], "mediawiki", "Q5296", "main page of a Wikimedia project", ["Q17442446"], 0, "https://www.mediawiki.org/wiki/MediaWiki"),
            (ids[1], "id.wiktionary", None, "", [], -1, "https://id.wiktionary.org/wiki/Istimewa%3APencarian"),
            (ids[2], "foundation.wikimedia", "Q5296", "main page of a Wikimedia project", ["Q17442446"], 0, "https://foundation.wikimedia.org/wiki/Home"),
            (ids[3], "thankyou.wikipedia", None, "", [], 0, "https://thankyou.wikipedia.org/wiki/Thank_You/en"),
            (ids[4], "en.wikipedia", "Q866", "American video-sharing platform owned by Alphabet Inc.", [], 0, "https://en.wikipedia.org/wiki/YouTube"),
            (ids[5], "en.wikipedia", "Q78337779", "American political advisor (born 1991)", [], 0, "https://en.wikipedia.org/wiki/Katie_Miller"),
        ]
        with self.store.connect() as db:
            for identifier, project, qid, description, types, namespace, url in rows:
                db.execute("""INSERT INTO keywords(id,keyword,normalized,source,project,observed_at,data_mode,evidence,context,eligible)
                    VALUES(?,?,?,?,?,?,?,?,?,1)""", (identifier, identifier, identifier.casefold(),
                    "wikimedia_pageviews_top_per_country", project, "2026-11-14", "LIVE",
                    json.dumps({"views_ceil": 17}), "",))
                db.execute("""INSERT INTO entity_context(keyword_id,title,entity_id,description,page_namespace,is_main_page,
                    entity_type,occupations,category_ids,instance_types,source_url,method,status,context_version,captured_at)
                    VALUES(?,?,?,?,?,?,'unknown','[]','[]',?,?,?,'CACHED','fixture-v1','2026-11-14')""",
                    (identifier, identifier, qid, description, namespace, int(identifier in {"MediaWiki", "Home"}),
                     json.dumps(types), url,
                     "WIKIPEDIA_PAGE_ID_AND_WIKIDATA_CLAIMS" if qid else "WIKIPEDIA_PAGE_WITHOUT_QID"))
            db.execute("INSERT INTO memberships VALUES(?,?, 'USER',1,NULL,'Preserve owner membership',?)",
                       (self.root, "MediaWiki", "2026-11-14"))
            db.execute("INSERT INTO memberships VALUES(?,?, 'USER',1,NULL,'Preserve system-page decision',?)",
                       (self.root, "Istimewa:Pencarian", "2026-11-14"))
        response = self._request("POST", "/api/v1/collections/reclassify-cached", json={"keyword_ids": ids})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["reclassified"], 6)
        outcomes = {item["keyword_id"]: item for item in result["items"]}
        self.assertEqual(outcomes["MediaWiki"]["classification_status"], "AMBIGUOUS")
        self.assertEqual(outcomes["MediaWiki"]["topic_category_ids"], [])
        self.assertEqual(outcomes["Istimewa:Pencarian"]["classification_reason"], "NON_TOPIC_SYSTEM_PAGE")
        self.assertFalse(outcomes["Istimewa:Pencarian"]["eligible"])
        self.assertEqual(outcomes["Home"]["classification_status"], "AMBIGUOUS")
        self.assertEqual(outcomes["Thank You/en"]["classification_status"], "UNKNOWN")
        self.assertEqual(outcomes["YouTube"]["topic_category_ids"],
                         ["technology.internet_services.video_platforms"])
        self.assertEqual(outcomes["Katie Miller"]["topic_category_ids"],
                         ["celebrity_public_figures.political_advisers"])
        unassigned = self.store.search_keywords(state="unassigned", limit=None)
        unassigned_ids = {item["id"] for item in unassigned["items"]}
        self.assertEqual(unassigned["total"], 3, unassigned_ids)
        self.assertIn("Home", unassigned_ids)
        self.assertIn("Thank You/en", unassigned_ids)
        self.assertNotIn("YouTube", unassigned_ids)
        self.assertNotIn("Katie Miller", unassigned_ids)
        self.assertIn("Istimewa:Pencarian", {item["id"] for item in self.store.search_keywords(
            state="non_topic", limit=None)["items"]})
        self.assertNotIn("Istimewa:Pencarian", {item["id"] for item in self.store.search_keywords(
            state="excluded", limit=None)["items"]})
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM algorithm_observations WHERE keyword_id IN (?,?,?,?,?,?)", ids).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM memberships WHERE keyword_id IN (?,?,?,?,?,?)", ids).fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT count(*) FROM feedback WHERE keyword_id IN (?,?)", ("YouTube", "MediaWiki")).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT classification_reason FROM classification_results WHERE keyword_id='Istimewa:Pencarian'").fetchone()[0], "NON_TOPIC_SYSTEM_PAGE")
            video = db.execute("SELECT id,parent_id FROM collections WHERE category_id='technology.internet_services.video_platforms'").fetchone()
            advisers = db.execute("SELECT id FROM collections WHERE category_id='celebrity_public_figures.political_advisers'").fetchone()
            internet = db.execute("SELECT id,parent_id FROM collections WHERE category_id='technology.internet_services'").fetchone()
            self.assertIsNotNone(video)
            self.assertEqual(video["parent_id"], internet["id"])
            self.assertIsNotNone(advisers)
            self.assertEqual(db.execute("SELECT collection_id FROM memberships WHERE keyword_id='YouTube' AND collection_id=?", (video["id"],)).fetchone()[0], video["id"])
            self.assertEqual(db.execute("SELECT collection_id FROM memberships WHERE keyword_id='Katie Miller' AND collection_id=?", (advisers["id"],)).fetchone()[0], advisers["id"])
            self.assertIsNotNone(db.execute("SELECT 1 FROM memberships WHERE keyword_id='MediaWiki' AND collection_id=? AND origin='USER' AND pinned=1", (self.root,)).fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM memberships WHERE keyword_id='Istimewa:Pencarian' AND collection_id=? AND origin='USER' AND pinned=1", (self.root,)).fetchone())
            self.assertEqual(tuple(db.execute("SELECT eligible,rejection_reason FROM keywords WHERE id='Istimewa:Pencarian'").fetchone()), (0, "CLASSIFICATION_INELIGIBLE"))

    def test_cached_taxonomy_assignment_respects_explicit_removal_feedback(self):
        keyword_id = "feedback-protected-youtube"
        with self.store.connect() as db:
            db.execute("""INSERT INTO keywords(id,keyword,normalized,source,project,observed_at,data_mode,evidence,context,eligible)
                VALUES(?,?,?,?,?,?,?,?,?,1)""", (keyword_id, "YouTube", "youtube",
                "wikimedia_pageviews_top_per_country", "en.wikipedia", "2026-11-14", "LIVE",
                json.dumps({"views_ceil": 17}), ""))
            db.execute("""INSERT INTO entity_context(keyword_id,title,entity_id,description,page_namespace,
                entity_type,occupations,category_ids,instance_types,source_url,method,status,context_version,captured_at)
                VALUES(?,?,?, ?,0,'unknown','[]','[]','[]',?,'WIKIPEDIA_PAGE_ID_AND_WIKIDATA_CLAIMS','CACHED','fixture-v1','2026-11-14')""",
                (keyword_id, "YouTube", "Q866", "American video-sharing platform owned by Alphabet Inc.",
                 "https://en.wikipedia.org/wiki/YouTube"))
        self.store.ensure_category_collection(resolve_category("technology.internet_services"))
        video_id = self.store.ensure_category_collection(resolve_category("technology.internet_services.video_platforms"))
        with self.store.connect() as db:
            db.execute("INSERT INTO feedback VALUES(?,?, 'REMOVE',?)", (video_id, keyword_id, "2026-11-14"))
        response = self._request("POST", "/api/v1/collections/reclassify-cached", json={"keyword_ids": [keyword_id]})
        self.assertEqual(response.status_code, 200, response.text)
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM memberships WHERE collection_id=? AND keyword_id=?", (video_id, keyword_id)).fetchone())

    def test_analytics_and_runtime_are_read_only_and_use_saved_evidence(self):
        analytics = self._request("GET", "/api/v1/analytics/overview").json()
        self.assertEqual(analytics["keyword_totals"]["unique"], 4)
        self.assertEqual(analytics["keyword_totals"]["classified"], 1)
        self.assertEqual(analytics["top_observed"][0]["keyword"], "Recycled denim")
        self.assertEqual(analytics["algorithm_effectiveness"]["status"], "NOT_YET_EVALUATED")
        runtime = self._request("GET", "/api/v1/runtime").json()
        self.assertEqual(runtime["llm"]["status"], "NOT_CONFIGURED")
        self.assertEqual(runtime["scheduler"]["status"], "DISABLED")
        self.assertEqual(runtime["system"]["status"], "AVAILABLE")
        self.assertEqual(runtime["system"]["environment_profile"], "test")
        self.assertTrue(runtime["system"]["read_only"])
        self.assertEqual(runtime["storage"]["engine"], "SQLITE")
        self.assertEqual(runtime["storage"]["integrity"], "OK")
        self.assertEqual(runtime["storage"]["keyword_records"], 4)
        self.assertGreater(runtime["storage"]["database_size_bytes"], 0)
        self.assertTrue(runtime["storage"]["read_only"])
        self.assertNotIn(str(self.store.path), str(runtime))

    def test_source_library_exposes_research_candidates_as_locked(self):
        response = self._request("GET", "/api/v1/sources/library")
        self.assertEqual(response.status_code, 200)
        catalog = response.json()
        self.assertEqual(len(catalog["items"]), 81)
        self.assertTrue(all(item["runtime_adapter"] is False for item in catalog["items"]))

    def test_run_history_reads_only_persisted_jobs(self):
        response = self._request("GET", "/api/v1/runs/history")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])
        self.assertFalse(response.json()["duration_available"])

    def test_run_history_distinguishes_fetch_outcome_from_completed_processing(self):
        fetch_id = "e" * 32
        manifest = [{"source_id": "youtube_video_search", "status": "FAILED",
                     "error_code": "SOURCE_EXECUTION_FAILED", "record_count": 0,
                     "source_input_digest": "a" * 64,
                     "source_input_digest_version": 2,
                     "legacy_source_input_digest": "b" * 64,
                     "legacy_source_input_digests": ["b" * 64, "c" * 64]}]
        with self.store.connect() as db:
            db.execute("INSERT INTO fetch_jobs(fetch_id,status,updated_at) VALUES(?,?,?)",
                       (fetch_id, "COMPLETED", "2026-11-17T17:41:25Z"))
            db.execute("""INSERT INTO dataset_snapshots
                (dataset_id,status,data_mode,source_manifest,provider_record_count,received_record_count,
                retained_keyword_count,rejected_record_count,quality_flags,processing_status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                    fetch_id, "FAILED", "LIVE", json.dumps(manifest), 0, 0, 0, 0,
                    "[]", "COMPLETED", "2026-11-17T17:41:25Z"))

        cursor_page = self._request("GET", "/api/v1/runs/history",
                                    params={"limit": 20, "cursor": ""}).json()
        item = next(row for row in cursor_page["items"] if row["fetch_id"] == fetch_id)
        self.assertEqual(item["status"], "COMPLETED")
        self.assertEqual(item["outcome_status"], "FAILED")
        self.assertEqual(item["error_code"], "SOURCE_EXECUTION_FAILED")

        offset_page = self._request("GET", "/api/v1/runs/history", params={"limit": 20}).json()
        offset_item = next(row for row in offset_page["items"] if row["fetch_id"] == fetch_id)
        self.assertEqual(offset_item["outcome_status"], "FAILED")
        self.assertEqual(offset_item["error_code"], "SOURCE_EXECUTION_FAILED")

        filtered = self._request("GET", "/api/v1/runs/history",
                                 params={"limit": 20, "cursor": "", "status": "FAILED"}).json()
        self.assertEqual([row["fetch_id"] for row in filtered["items"]], [fetch_id])

        detail = self._request("GET", "/api/v1/runs/history/" + fetch_id).json()["item"]
        self.assertEqual(detail["outcome_status"], "FAILED")
        self.assertEqual(detail["error_code"], "SOURCE_EXECUTION_FAILED")
        self.assertEqual(detail["dataset_status"], "FAILED")
        for item in (item, offset_item, detail):
            manifest_value = item["source_manifest"]
            manifest_value = (json.loads(manifest_value)
                              if isinstance(manifest_value, str) else manifest_value)
            self.assertTrue({"source_input_digest", "source_input_digest_version",
                             "legacy_source_input_digest",
                             "legacy_source_input_digests"}.isdisjoint(manifest_value[0]))

        latest = self._request("GET", "/api/v1/datasets/latest").json()["item"]
        self.assertTrue({"source_input_digest", "source_input_digest_version",
                         "legacy_source_input_digest", "legacy_source_input_digests"}.isdisjoint(
                             latest["source_manifest"][0]))
        overview = self._request("GET", "/api/v1/analytics/overview").json()
        self.assertTrue({"source_input_digest", "source_input_digest_version",
                         "legacy_source_input_digest", "legacy_source_input_digests"}.isdisjoint(
                             overview["latest_dataset"]["source_manifest"][0]))

    def test_run_history_restores_shared_fetch_when_collections_failed_before_snapshot(self):
        fetch_id = "c" * 32
        manifest = [{
            "source_id": "wikimedia_pageviews_top_per_country",
            "status": "SUCCESS",
            "record_count": 999,
            "delivery_state": "CACHED",
            "source_input_digest": "b" * 64,
            "source_input_digest_version": 2,
            "legacy_source_input_digest": "c" * 64,
            "legacy_source_input_digests": ["c" * 64],
        }, {
            "source_id": "google_web_serp",
            "status": "UNAVAILABLE",
            "record_count": 0,
            "error_code": "NOT_YET_ELIGIBLE",
        }, {
            "source_id": "bing_web_serp",
            "status": "UNAVAILABLE",
            "record_count": 0,
            "error_code": "NO_SAVED_CANDIDATE",
        }, {
            "source_id": "youtube_video_search",
            "status": "PARTIAL",
            "record_count": 47,
            "delivery_state": "STALE_CACHE",
        }]
        shared_run = {
            "fetch_id": fetch_id,
            "run_type": "FETCH",
            "status": "PARTIAL",
            "outcome_status": "PARTIAL",
            "mode": "LIVE",
            "data_mode": "LIVE",
            "sources": [source["source_id"] for source in manifest],
            "source_manifest": manifest,
            "received_record_count": 1046,
            "created_at": "2026-11-20T08:20:00+00:00",
            "updated_at": "2026-11-20T08:20:49+00:00",
            "run_at": "2026-11-20T08:20:49+00:00",
        }

        class _SharedHistory:
            firestore_shared_live = True

            @staticmethod
            def history(limit=20):
                return [shared_run][:limit]

        self.app.state.distributed_public_fetch = _SharedHistory()
        with self.store.connect() as db:
            db.execute("INSERT INTO fetch_jobs(fetch_id,status,error_code,updated_at,stage) "
                       "VALUES(?,'MODEL_UNAVAILABLE','LOCAL_MODEL_UNAVAILABLE',?,'MODEL_UNAVAILABLE')",
                       (fetch_id, "2026-11-20T10:19:22+00:00"))

        cursor_item = self._request("GET", "/api/v1/runs/history",
                                    params={"limit": 1, "cursor": ""}).json()["items"][0]
        offset_item = self._request("GET", "/api/v1/runs/history",
                                    params={"limit": 1}).json()["items"][0]
        detail = self._request("GET", "/api/v1/runs/history/" + fetch_id).json()["item"]

        for item in (cursor_item, offset_item, detail):
            self.assertEqual(item["run_type"], "FETCH")
            self.assertEqual(item["status"], "PARTIAL")
            self.assertEqual(item["outcome_status"], "PARTIAL")
            self.assertEqual(item["mode"], "LIVE")
            self.assertEqual(item["received_record_count"], 1046)
            source_manifest = item["source_manifest"]
            if isinstance(source_manifest, str):
                source_manifest = json.loads(source_manifest)
            self.assertEqual(len(source_manifest), 4)
            self.assertTrue({"source_input_digest", "source_input_digest_version",
                             "legacy_source_input_digest",
                             "legacy_source_input_digests"}.isdisjoint(source_manifest[0]))
            self.assertEqual(item["collection_processing_status"], "MODEL_UNAVAILABLE")
            self.assertEqual(item["collection_error_code"], "LOCAL_MODEL_UNAVAILABLE")

    def test_run_history_detail_preserves_saved_source_count_and_observation_window(self):
        fetch_id = "d" * 32
        with self.store.connect() as db:
            db.execute("INSERT INTO fetch_jobs(fetch_id,status,updated_at) VALUES(?,?,?)",
                       (fetch_id, "PARTIAL", "2026-11-14T02:00:00Z"))
            db.execute("""INSERT INTO dataset_snapshots
                (dataset_id,status,data_mode,source_manifest,provider_record_count,received_record_count,
                retained_keyword_count,rejected_record_count,observation_start,observation_end,fetched_at,
                quality_flags,processing_status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    fetch_id, "PARTIAL", "LIVE",
                    json.dumps([{"source_id":"wikimedia_pageviews_top_per_country","status":"PARTIAL","record_count":38}]),
                    40, 38, 35, 3, "2026-11-10", "2026-11-14", "2026-11-14T02:00:00Z",
                    json.dumps(["SOURCE_COVERAGE_PARTIAL"]), "COMPLETED", "2026-11-14T02:00:00Z"))
        response = self._request("GET", "/api/v1/runs/history/" + fetch_id)
        self.assertEqual(response.status_code, 200)
        item = response.json()["item"]
        self.assertEqual(item["provider_record_count"], 40)
        self.assertEqual(item["observation_start"], "2026-11-10")
        self.assertEqual(item["observation_end"], "2026-11-14")
        self.assertEqual(item["source_manifest"][0]["record_count"], 38)
        self.assertIsNone(item["duration_seconds"])

    def test_run_history_uses_job_time_when_snapshot_is_served_from_older_cache(self):
        cached_fetch_id = "f" * 32
        older_fetch_id = "e" * 32
        now = datetime.now(timezone.utc)
        snapshot_created_at = now.isoformat()
        source_fetched_at = (now - timedelta(hours=1)).isoformat()
        older_job_updated_at = (now - timedelta(minutes=30)).isoformat()
        with self.store.connect() as db:
            db.execute("INSERT INTO fetch_jobs(fetch_id,status,updated_at) VALUES(?,?,?)",
                       (cached_fetch_id, "PARTIAL", snapshot_created_at))
            db.execute("INSERT INTO fetch_jobs(fetch_id,status,updated_at) VALUES(?,?,?)",
                       (older_fetch_id, "PARTIAL", older_job_updated_at))
            db.execute("""INSERT INTO dataset_snapshots
                (dataset_id,status,data_mode,source_manifest,provider_record_count,received_record_count,
                retained_keyword_count,rejected_record_count,fetched_at,quality_flags,processing_status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    cached_fetch_id, "PARTIAL", "LIVE", json.dumps([]), 1, 1, 1, 0,
                    source_fetched_at, "[]", "COMPLETED", snapshot_created_at))

        page = self._request("GET", "/api/v1/runs/history", params={"limit": 1, "cursor": ""}).json()
        self.assertEqual([item["fetch_id"] for item in page["items"]], [cached_fetch_id])
        cached_item = page["items"][0]
        self.assertEqual(cached_item["run_at"], snapshot_created_at)
        self.assertEqual(cached_item["fetched_at"], source_fetched_at)
        next_page = self._request("GET", "/api/v1/runs/history", params={
            "limit": 1, "cursor": page["next_cursor"],
        }).json()
        self.assertEqual([item["fetch_id"] for item in next_page["items"]], [older_fetch_id])

        detail = self._request("GET", "/api/v1/runs/history/" + cached_fetch_id).json()["item"]
        self.assertEqual(detail["run_at"], snapshot_created_at)
        self.assertEqual(detail["fetched_at"], source_fetched_at)

        latest = self._request("GET", "/api/v1/datasets/latest").json()["item"]
        self.assertEqual(latest["created_at"], snapshot_created_at)
        self.assertLess(latest["age_seconds"], 5)
        overview = self._request("GET", "/api/v1/analytics/overview").json()
        self.assertEqual(overview["latest_dataset"]["created_at"], snapshot_created_at)
        self.assertEqual(overview["last_successful_fetch"]["created_at"], snapshot_created_at)
        self.assertLess(overview["last_successful_fetch"]["age_seconds"], 5)

    def test_run_history_includes_persisted_analysis_storage_runs(self):
        storage = FakeStorage()
        import asyncio
        asyncio.run(storage.create_run("a" * 32, {
            "run_id": "a" * 32, "status": "PARTIAL", "mode": "synthetic",
            "created_at": "2026-11-13T01:00:00+00:00", "keyword_count": 12,
            "scored_count": 8, "source_manifest": [{"source_id": "wikimedia_pageviews_top_per_country"}],
        }))
        self.app.state.read_storage_factory = lambda _token: storage
        response = self._request("GET", "/api/v1/runs/history")
        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]
        self.assertEqual(item["fetch_id"], "a" * 32)
        self.assertEqual(item["run_type"], "MANUAL_ANALYSIS")
        self.assertEqual(item["sources"], ["wikimedia_pageviews_top_per_country"])
        detail = self._request("GET", "/api/v1/runs/history/" + "a" * 32)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["item"]["status"], "PARTIAL")

    def test_run_history_cursor_pages_past_the_first_hundred_and_binds_filters(self):
        async def seed():
            storage = FakeStorage()
            for index in range(127):
                await storage.create_run(f"{index + 1:032x}", {
                    "run_id": f"{index + 1:032x}",
                    "status": "COMPLETED" if index % 2 == 0 else "PARTIAL",
                    "mode": "synthetic",
                    "created_at": f"2026-11-{(index % 14) + 1:02d}T{index:04d}:00+00:00",
                })
            self.app.state.read_storage_factory = lambda _token: storage

        __import__("asyncio").run(seed())
        seen = []
        cursor = None
        first_cursor = None
        while True:
            params = {"limit": 20, "cursor": cursor or ""}
            response = self._request("GET", "/api/v1/runs/history", params=params)
            self.assertEqual(response.status_code, 200)
            page = response.json()
            seen.extend(item["fetch_id"] for item in page["items"])
            cursor = page["next_cursor"]
            if first_cursor is None:
                first_cursor = cursor
            if cursor is None:
                break
        self.assertEqual(len(seen), 127)
        self.assertEqual(len(set(seen)), 127)
        filtered = self._request("GET", "/api/v1/runs/history", params={"cursor": first_cursor, "status": "COMPLETED"})
        self.assertEqual(filtered.status_code, 422)

    def test_run_history_cursor_merges_local_jobs_with_analysis_storage(self):
        async def seed():
            storage = FakeStorage()
            for index, created in enumerate(("2026-11-14T09:00:00+00:00", "2026-11-14T07:00:00+00:00")):
                run_id = f"{index + 1:032x}"
                await storage.create_run(run_id, {"run_id": run_id, "status": "COMPLETED", "created_at": created})
            self.app.state.read_storage_factory = lambda _token: storage

        __import__("asyncio").run(seed())
        with self.store.connect() as db:
            for index, updated in enumerate(("2026-11-14T08:00:00+00:00", "2026-11-14T06:00:00+00:00")):
                db.execute("INSERT INTO fetch_jobs(fetch_id,status,updated_at) VALUES(?,?,?)",
                           (f"{index + 3:032x}", "COMPLETED", updated))
        response = self._request("GET", "/api/v1/runs/history", params={"limit": 2, "cursor": ""})
        self.assertEqual(response.status_code, 200)
        first = response.json()
        self.assertEqual([item["fetch_id"] for item in first["items"]], [f"{1:032x}", f"{3:032x}"])
        second = self._request("GET", "/api/v1/runs/history", params={"limit": 2, "cursor": first["next_cursor"]}).json()
        self.assertEqual([item["fetch_id"] for item in second["items"]], [f"{2:032x}", f"{4:032x}"])

    def test_product_research_is_persisted_and_status_changes_are_auditable(self):
        created = self._request("POST", "/api/v1/research", json={
            "keyword_id": "k-classified", "subject": "Recycled denim",
            "hypothesis": "Investigate a repair and resale kit for denim owners.",
            "product_category": "Apparel care", "niche": "Repair kits",
            "competitor_notes": "Manual comparison pending", "market_evidence": "No commercial source entered yet",
            "ip_notes": "Trademark review not completed", "risk_notes": "Demand not measured",
            "findings": "Interview target users", "status": "DRAFT",
        })
        self.assertEqual(created.status_code, 201)
        identifier = created.json()["id"]
        self.assertEqual(self._request("GET", "/api/v1/research").json()["total"], 1)
        row = self._request("GET", "/api/v1/research").json()["items"][0]
        self.assertEqual(row["keyword"], "Recycled denim")
        self.assertEqual(row["source"], "wikimedia_pageviews_top_per_country")
        updated = self._request("PATCH", f"/api/v1/research/{identifier}", json={"status": "NEEDS_REVIEW"})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["status"], "NEEDS_REVIEW")
        edited = self._request("PATCH", f"/api/v1/research/{identifier}", json={
            "subject": "  Denim repair subscriptions  ",
            "hypothesis": "  Test whether owners prefer recurring repair supplies.  ",
            "product_category": " Circular apparel ", "niche": " Subscription kits ",
            "competitor_notes": " Compare repair clubs ", "market_evidence": " Search data pending ",
            "ip_notes": " Trademark review pending ", "risk_notes": " Retention unknown ",
            "findings": " Interview plan updated ",
        })
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.json()["status"], "NEEDS_REVIEW")
        persisted = self._request("GET", "/api/v1/research").json()["items"][0]
        self.assertEqual(persisted["subject"], "Denim repair subscriptions")
        self.assertEqual(persisted["hypothesis"], "Test whether owners prefer recurring repair supplies.")
        self.assertEqual(persisted["product_category"], "Circular apparel")
        blank = self._request("PATCH", f"/api/v1/research/{identifier}", json={"subject": "   "})
        self.assertEqual(blank.status_code, 422)
        null_value = self._request("PATCH", f"/api/v1/research/{identifier}", json={"findings": None})
        self.assertEqual(null_value.status_code, 422)

    def test_research_cannot_link_a_nonexistent_keyword(self):
        response = self._request("POST", "/api/v1/research", json={
            "keyword_id": "missing", "subject": "Idea", "hypothesis": "Test this idea",
        })
        self.assertEqual(response.status_code, 422)

    def test_search_filters_use_server_totals_and_zero_is_available_metric(self):
        ambiguous = self.store.search_keywords(classification="AMBIGUOUS", limit=1)
        self.assertEqual((ambiguous["total"], ambiguous["items"][0]["id"], ambiguous["next_offset"]),
                         (1, "k-ambiguous", None))
        parent = self.store.search_keywords(collection_ids=[self.root], limit=None)
        self.assertEqual({item["id"] for item in parent["items"]}, {"k-classified", "k-ambiguous"})
        zero_metric = self.store.search_keywords(evidence="metric", query="recycled")
        self.assertEqual(zero_metric["total"], 1)
        dates = self.store.search_keywords(date_from="2026-11-11", date_to="2026-11-12")
        self.assertEqual({item["id"] for item in dates["items"]}, {"k-ambiguous", "k-unknown"})
        with self.assertRaisesRegex(ValueError, "INVALID_CLASSIFICATION_FILTER"):
            self.store.search_keywords(classification="NOT_IN_TAXONOMY")

    def test_api_filtered_bulk_action_uses_same_filter_scope_and_detail_has_real_evidence(self):
        filtered = self._request("GET", "/api/v1/datasets/latest/keywords?classification=AMBIGUOUS&limit=1")
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual((filtered.json()["total"], filtered.json()["items"][0]["id"]), (1, "k-ambiguous"))
        changed = self._request("POST", "/api/v1/collections/bulk", json={
            "action": "ADD", "all_filtered": True, "classification": "AMBIGUOUS", "collection_id": self.target,
        })
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["updated"], 1)
        detail = self._request("GET", "/api/v1/collections/keywords/k-classified")
        self.assertEqual(detail.status_code, 200)
        item = detail.json()
        self.assertEqual(item["classification_status"], "CLASSIFIED")
        self.assertEqual(item["evidence"]["views_ceil"], 0)
        self.assertEqual(item["classification_source_evidence"]["scope"]["country_scope"], "US")
        self.assertEqual(item["algorithm_results"][0]["result"], {"rank": 1})
        self.assertEqual(detail.json()["user_decision"], None)

    def test_detail_returns_persisted_exclusion_decision_and_feedback(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO feedback VALUES(?,?, 'REMOVE',?)", (self.root, "k-excluded", "2026-11-13T01:00:00Z"))
        detail = self._request("GET", "/api/v1/collections/keywords/k-excluded")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["user_decision"]["decision"], "EXCLUDED")
        self.assertTrue(detail.json()["user_feedback"])
        self.assertEqual(self._request("GET", "/api/v1/collections/keywords/missing").status_code, 404)

    def test_invalid_dates_and_move_target_are_rejected(self):
        invalid = self._request("GET", "/api/v1/datasets/latest/keywords?date_from=2026-11-13&date_to=2026-11-01")
        self.assertEqual(invalid.status_code, 422)
        same_target = self._request("POST", "/api/v1/collections/bulk", json={
            "action": "MOVE", "keyword_ids": ["k-classified"], "collection_id": self.target,
            "source_collection_id": self.target,
        })
        self.assertEqual(same_target.status_code, 422)

    def test_merge_preserves_descendant_membership_origin_and_keyword_evidence(self):
        source = self.store.create({"name": "Source parent", "auto_match": False})
        child = self.store.create({"name": "Source child", "parent_id": source, "auto_match": False})
        with self.store.connect() as db:
            db.execute("INSERT INTO memberships VALUES(?,?, 'LOCAL_AI',0,0.83,'AI match',?)",
                       (child, "k-unknown", "2026-11-13T00:00:00Z"))
            db.execute("INSERT INTO memberships VALUES(?,?, 'USER',1,NULL,'Manual pin',?)",
                       (source, "k-classified", "2026-11-13T00:00:00Z"))
        merged = self.store.merge(source, self.target)
        self.assertEqual((merged["transferred"], merged["skipped"]), (2, 0))
        rows = {row["id"]: row for row in self.store.keywords_for(self.target)}
        self.assertEqual((rows["k-unknown"]["origin"], rows["k-unknown"]["pinned"]), ("LOCAL_AI", 0))
        self.assertEqual((rows["k-classified"]["origin"], rows["k-classified"]["pinned"]), ("USER", 1))
        self.assertEqual(self.store.search_keywords(keyword_id="k-classified")["items"][0]["evidence"]["views_ceil"], 0)
        with self.store.connect() as db:
            suppressed = {row["label"] for row in db.execute("SELECT label FROM suppression WHERE kind='DELETED_COLLECTION'")}
        self.assertTrue({"Source parent", "Source child"} <= suppressed)

    def test_unclassified_is_not_a_persisted_collection(self):
        names = {item["name"].casefold() for item in self.store.list_collections()}
        self.assertNotIn("unclassified", names)

    def test_collection_create_edit_mode_merge_and_delete_on_isolated_store(self):
        created = self._request("POST", "/api/v1/collections", json={
            "name": "Source parent", "description": "Fixture only", "parent_id": self.root,
            "examples": ["denim"], "exclusions": [], "auto_match": False,
        })
        self.assertEqual(created.status_code, 201)
        source = created.json()["id"]
        edited = self._request("PATCH", f"/api/v1/collections/{source}", json={
            "description": "Updated fixture", "auto_match": True,
        })
        self.assertEqual(edited.status_code, 200)
        mode = self._request("POST", "/api/v1/collections/mode", json={"mode": "SUGGEST_ONLY"})
        self.assertEqual(mode.json()["mode"], "SUGGEST_ONLY")
        merged = self._request("POST", f"/api/v1/collections/{source}/merge", json={"target_id": self.target})
        self.assertEqual(merged.status_code, 200)
        self.assertEqual(merged.json()["target_id"], self.target)
        removed = self._request("DELETE", f"/api/v1/collections/{self.target}")
        self.assertEqual(removed.status_code, 200)
        self.assertNotIn(self.target, {item["id"] for item in self.store.list_collections()})

    def test_reclassification_reuses_saved_evidence_without_provider_enrichment(self):
        service = self.app.state.collection_service
        service.process_fetch = AsyncMock(return_value={"status": "MODEL_UNAVAILABLE"})
        response = self._request("POST", "/api/v1/collections/reclassify")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "MODEL_UNAVAILABLE")
        args, kwargs = service.process_fetch.call_args
        self.assertTrue(kwargs["cached_context_only"])
        self.assertEqual(len(args[1]), 1)
        self.assertEqual(len(args[1][0]["records"]), 3)


if __name__ == "__main__":
    unittest.main()
