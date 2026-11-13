import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.collections import embeddings
from app.collections.service import CollectionService
from app.collections.store import CollectionStore, keyword_record
from app.main import create_app
from app.settings import Settings
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
    return [{"source_id": "wikimedia_pageviews_top_per_country", "data_mode": "LIVE", "records": rows}]


class SmartCollectionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CollectionStore(Path(self.temp.name) / "collections.sqlite")
        self.service = CollectionService(self.store)

    def tearDown(self):
        self.temp.cleanup()

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

    def test_partial_snapshot_is_not_used_for_snapshot_popularity(self):
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
            self.assertEqual(db.execute("SELECT count(*) FROM algorithm_results WHERE status='COMPLETED'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM algorithm_observations WHERE snapshot_status='PARTIAL'").fetchone()[0], 2)

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
        self.assertTrue(restarted.connect().execute("SELECT 1 FROM suppression").fetchone())

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

    def test_full_record_input_and_cache_reuse(self):
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
            with self.store.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM algorithm_results").fetchone()[0], 212 * 12)
                self.assertEqual(db.execute("SELECT count(*) FROM algorithm_observations").fetchone()[0], 212)
                self.assertEqual(db.execute("SELECT count(*) FROM algorithm_results WHERE algorithm_id='observed-volume-rank' AND status='COMPLETED'").fetchone()[0], 212)
                self.assertEqual(db.execute("SELECT count(*) FROM algorithm_results WHERE algorithm_id='observed-percentile-rank' AND status='COMPLETED'").fetchone()[0], 212)
            previous_calls = sum(calls)
            replay = self.service._process_fetch("full-set-again", source)
            self.assertEqual(replay["algorithms"]["executed_keywords"], 0)
            self.assertEqual(replay["algorithms"]["unchanged_keywords"], 212)
            self.assertEqual(sum(calls), previous_calls)

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
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('creation_mode','AUTO_CREATE')")
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
        self.assertEqual(self.store.list_collections(), [])

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
        self.assertEqual(tuple(saved), ("Mars Rover", 1))

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
