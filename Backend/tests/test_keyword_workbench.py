import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from app.collections.service import CollectionService
from app.collections.store import CollectionStore
from app.main import create_app
from app.settings import Settings


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
            ("k-excluded", "Excluded record", "2026-11-13", {"views_ceil": 9}, 0, "INELIGIBLE"),
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

    def test_system_ineligible_is_distinct_from_user_exclusion_and_unknown_context(self):
        with self.store.connect() as db:
            db.execute("UPDATE classification_results SET classification_status='INELIGIBLE' WHERE keyword_id='k-unknown'")
        summary = self.store.workbench_summary()
        self.assertEqual(summary["ineligible_topic_keywords"], 1)
        self.assertEqual(summary["unknown_keywords"], 0)
        self.assertEqual(summary["excluded_keywords"], 1)

    def test_analytics_and_runtime_are_read_only_and_use_saved_evidence(self):
        analytics = self._request("GET", "/api/v1/analytics/overview").json()
        self.assertEqual(analytics["keyword_totals"]["unique"], 4)
        self.assertEqual(analytics["keyword_totals"]["classified"], 1)
        self.assertEqual(analytics["top_observed"][0]["keyword"], "Recycled denim")
        self.assertEqual(analytics["algorithm_effectiveness"]["status"], "NOT_YET_EVALUATED")
        runtime = self._request("GET", "/api/v1/runtime").json()
        self.assertEqual(runtime["llm"]["status"], "NOT_CONFIGURED")
        self.assertEqual(runtime["scheduler"]["status"], "DISABLED")

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
