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
                        description,entity_type,occupations,category_ids,source_url,method,status,context_version,captured_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", [value.as_row() for value in self.contexts.values()])
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
        self.assertTrue(self.store.rollback_membership_reconciliation("reconcile-test"))
        self.assertEqual({row["origin"] for row in self.store.keywords_for(collection)}, {"LOCAL_AI", "USER"})

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
            self.assertEqual(self.store.search_keywords(limit=None, ids_only=True)["total"], 212)
            self.assertEqual(len(self.store.search_keywords(limit=None, ids_only=True)["ids"]), 212)
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
