"""Concurrent collection mutations on an isolated PostgreSQL schema."""

import asyncio
import hashlib
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from app.collections.entity_context import EntityContext, WikimediaEntityContextResolver
from app.collections.identity import fingerprint
from app.collections.postgres_store import PostgresCollectionStore
from app.collections.service import CollectionService
from app.collections.source_lineage import build_source_evidence
from app.collections.store import keyword_record
from app.discovery.contracts import ProcessingGrant
from app.sources.catalog import GOOGLE_SOURCE_ID, WIKIMEDIA_SOURCE_ID
from app.sources.google_trends.trending_now import normalize_keyword
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations


def test_collection_mutations_are_serialized_and_preserve_legacy_rules():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    created = False
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            apply_migrations(connection, schema=schema)
            created = True
            store = PostgresCollectionStore(environment, schema=schema)
            assert store.get_setting("creation_mode", "AUTO_CREATE") == "AUTO_CREATE"
            store.set_setting("creation_mode", "OFF")
            assert store.get_setting("creation_mode", "AUTO_CREATE") == "OFF"

            def create(name):
                try:
                    return store.create({"name": name})
                except ValueError as exc:
                    return str(exc)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(create, ["  Example Name", "example   name  "]))
            assert results.count("COLLECTION_ALREADY_EXISTS") == 1
            root = next(value for value in results if value != "COLLECTION_ALREADY_EXISTS")
            child = store.create({"name": "Child", "parent_id": root})
            category = SimpleNamespace(id="example.category", display_name="Child")
            assert store.ensure_category_collection(category) == child
            cache_table = sql.Identifier(schema, "cache")
            connection.execute(sql.SQL("""INSERT INTO {}(fingerprint,model_version,collection_version,
                text_hash,vector,dimension) VALUES ('shared','v',0,'h',%s,1),('old','v',1,'h',%s,1)""")
                .format(cache_table), (b"a", b"b"))
            cache_hits = store.get_embedding_cache([
                ("shared", "v", 0, "h"), ("old", "v", 0, "h"), ("missing", "v", 0, "h"),
            ])
            assert set(cache_hits) == {"shared"}
            assert cache_hits["shared"] == {"vector": b"a", "dimension": 1}
            store.upsert_embedding_cache([("shared", "v", 0, "h", b"c", 1)])
            assert store.get_embedding_cache([("shared", "v", 0, "h")])["shared"]["vector"] == b"c"
            connection.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                observed_at,data_mode,evidence) VALUES ('member-topic','Topic','topic','test','test',
                '2026-11-25','LIVE',%s)""").format(sql.Identifier(schema, "keywords")), (Jsonb({}),))
            store.add_local_ai_memberships(root, ["member-topic"], "semantic group")
            assert store.has_membership(root, "member-topic")
            assert store.count_ai_assigned_keywords() == 1
            connection.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,origin,pinned,
                score,reason,created_at) VALUES (%s,'member-topic','USER',1,NULL,'user example',now())""")
                .format(sql.Identifier(schema, "memberships")), (child,))
            assert store.user_member_examples([root, child]) == {root: [], child: ["Topic"]}
            connection.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                observed_at,data_mode,evidence) VALUES
                ('taxonomy-topic','Taxonomy topic','taxonomy topic','test','test','2026-11-25','LIVE',%s),
                ('removed-taxonomy-topic','Removed topic','removed topic','test','test','2026-11-25','LIVE',%s)""")
                .format(sql.Identifier(schema, "keywords")), (Jsonb({}), Jsonb({})))
            connection.execute(sql.SQL("INSERT INTO {}(collection_id,keyword_id,action,created_at) "
                "VALUES (%s,'removed-taxonomy-topic','REMOVE',now())").format(sql.Identifier(schema, "feedback")),
                (child,))
            store.add_taxonomy_memberships_if_allowed([
                (root, "taxonomy-topic", "verified taxonomy"),
                (child, "removed-taxonomy-topic", "verified taxonomy"),
            ])
            taxonomy_members = connection.execute(sql.SQL("SELECT collection_id,keyword_id FROM {} "
                "WHERE keyword_id IN ('taxonomy-topic','removed-taxonomy-topic')")
                .format(sql.Identifier(schema, "memberships"))).fetchall()
            assert taxonomy_members == [(root, "taxonomy-topic")]
            guardrails = store.membership_guardrails(
                ["member-topic", "taxonomy-topic", "removed-taxonomy-topic"], [child],
            )
            assert (child, "member-topic") in guardrails["pinned_pairs"]
            assert (child, "removed-taxonomy-topic") in guardrails["removed_pairs"]
            assert "removed-taxonomy-topic" in guardrails["removed_keyword_ids"]
            assert guardrails["active_category_ids"] == {child}
            assigned = store.replace_local_ai_memberships([
                (root, "member-topic", 0.7, "rebuilt match"),
                (root, "taxonomy-topic", None, "rebuilt taxonomy"),
                (child, "member-topic", 0.9, "pinned should stay"),
                (child, "removed-taxonomy-topic", 0.9, "removed should stay out"),
            ], source_ids=["member-topic", "taxonomy-topic", "removed-taxonomy-topic"])
            assert assigned == 2
            rebuilt = connection.execute(sql.SQL("SELECT collection_id,keyword_id,origin,pinned FROM {} "
                "WHERE keyword_id IN ('member-topic','taxonomy-topic','removed-taxonomy-topic') "
                "ORDER BY collection_id,keyword_id").format(sql.Identifier(schema, "memberships"))).fetchall()
            assert set(rebuilt) == {(root, "member-topic", "LOCAL_AI", 0),
                                    (child, "member-topic", "USER", 1),
                                    (root, "taxonomy-topic", "LOCAL_AI", 0)}
            store.create_proposal("proposal-test", "Topic group", "Description", ["member-topic"],
                                  "cohesive support", "2026-11-25T12:00:00+00:00",
                                  "2026-11-25T12:00:00+00:00")
            proposals = store.suggestions()
            assert len(proposals) == 1 and proposals[0]["support_ids"] == ["member-topic"]
            candidates = store.collection_name_candidates(fingerprint("Topic group"))
            assert candidates["suppressed"] is False
            assert {"Topic group", "Child"}.issubset(set(candidates["labels"]))
            assert any(fingerprint(label) == fingerprint("Example Name") for label in candidates["labels"])
            assert store.update(root, {"description": "updated"}) is True
            assert connection.execute(sql.SQL("SELECT fingerprint FROM {} ORDER BY fingerprint")
                                      .format(cache_table)).fetchall() == [("shared",)]
            assert store.delete(root) is True
            assert store.delete(root) is False
            assert store.collection_name_candidates(fingerprint("Example Name"))["suppressed"] is True
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}.collections")
                                      .format(sql.Identifier(schema))).fetchone()[0] == 0
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}.suppression")
                                      .format(sql.Identifier(schema))).fetchone()[0] == 2
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_ensure_category_collections_batches_and_is_idempotent():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    created = False
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            apply_migrations(connection, schema=schema)
            created = True
            store = PostgresCollectionStore(environment, schema=schema)
            existing = store.create({"name": "Existing category name"})
            categories = [
                SimpleNamespace(id="batch.category", display_name="Batch Category"),
                SimpleNamespace(id="existing.category", display_name="Existing category name"),
            ]
            expected = {
                "batch.category": "topic-" + fingerprint("batch.category")[:32],
                "existing.category": existing,
            }
            assert store.ensure_category_collections(categories) == expected
            assert store.ensure_category_collections(categories) == expected
            assert store.ensure_category_collection(categories[0]) == expected["batch.category"]
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE category_id=ANY(%s)")
                .format(sql.Identifier(schema, "collections")),
                ([category.id for category in categories],)).fetchone()[0] == 1
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_cleaned_keyword_ingestion_is_batched_idempotent_and_scope_safe():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    created = False
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            created = True
            apply_migrations(connection, schema=schema)
            store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
            keyword_table = sql.Identifier(schema, "keywords")
            decisions_table = sql.Identifier(schema, "keyword_decisions")

            excluded_id = "a" * 64
            connection.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                observed_at,data_mode,evidence,context) VALUES (%s,'Excluded topic','excluded topic',
                'google_web_serp','google_trends:US:en','2026-11-25T12:00:00+00:00','LIVE',%s,'old')""")
                .format(keyword_table), (excluded_id, Jsonb({"canonical_keyword_identity": "a" * 64})))
            connection.execute(sql.SQL("INSERT INTO {}(keyword_id,decision,updated_at) "
                                       "VALUES (%s,'EXCLUDED',now())").format(decisions_table),
                               (excluded_id,))

            collision_id = "b" * 64
            connection.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                observed_at,data_mode,evidence,context) VALUES (%s,'Legacy topic','legacy topic',
                'google_web_serp','google_trends:US:en','2026-11-25','SYNTHETIC',%s,'legacy')""")
                .format(keyword_table), (collision_id, Jsonb({"legacy": True})))

            rejected_id = "c" * 64
            connection.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                observed_at,data_mode,evidence,context) VALUES (%s,'Old topic','old topic',
                'wikimedia_pageviews_top_per_country','en.wikipedia','2026-11-24','LIVE',%s,'old')""")
                .format(keyword_table), (rejected_id, Jsonb({"provider_rank": 2})))
            bing_id = "d" * 64
            bing_identity = "e" * 64
            records = [
                {"id": excluded_id, "keyword": "Excluded topic", "normalized": "excluded topic",
                 "source": "google_web_serp", "project": "google_trends:US:en",
                 "observed_at": "2026-11-25T12:00:00+00:00", "data_mode": "LIVE",
                 "evidence": json.dumps({"canonical_keyword_identity": excluded_id}), "context": "new"},
                {"id": collision_id, "keyword": "Live topic", "normalized": "live topic",
                 "source": "google_web_serp", "project": "google_trends:US:en",
                 "observed_at": "2026-11-25T12:00:00+00:00", "data_mode": "LIVE",
                 "evidence": json.dumps({"canonical_keyword_identity": collision_id}), "context": "new"},
                {"id": rejected_id, "keyword": "Recovered topic", "normalized": "recovered topic",
                 "source": "wikimedia_pageviews_top_per_country", "project": "en.wikipedia",
                 "observed_at": "2026-11-25", "data_mode": "LIVE",
                 "evidence": json.dumps({"provider_rank": 1}), "context": "new"},
                {"id": bing_id, "keyword": "Solar generator", "normalized": "solar generator",
                 "source": "google_web_serp", "project": "google_trends:US:en",
                 "observed_at": "2026-11-25T12:00:00+00:00", "data_mode": "LIVE",
                 "evidence": json.dumps({"canonical_keyword_identity": bing_identity}), "context": "new"},
            ]
            verification = {"fetched_at": "2026-11-25T12:05:00Z", "query_fingerprint": "fp-1",
                            "status": "VERIFIED", "results": []}
            bing_update = {"candidate_id": bing_id, "identity": bing_identity,
                           "observed_at": "2026-11-25T12:00:00+00:00", "verification": verification}
            collision_verification = {
                "fetched_at": "2026-11-25T12:06:00Z",
                "query_fingerprint": "fp-collision", "status": "VERIFIED", "results": [],
            }
            collision_bing_update = {
                "candidate_id": collision_id, "identity": collision_id,
                "observed_at": "2026-11-25T12:00:00+00:00",
                "verification": collision_verification,
            }
            bulk_ids = [hashlib.sha256(f"bulk:{index}".encode()).hexdigest()
                        for index in range(501)]
            bulk_records = [{"id": identifier, "keyword": f"Bulk topic {index}",
                             "normalized": f"bulk topic {index}", "source": "wikimedia_pageviews_top_per_country",
                             "project": "en.wikipedia", "observed_at": "2026-11-25",
                             "data_mode": "LIVE", "evidence": json.dumps({"provider_rank": index + 1}),
                             "context": "bulk"}
                            for index, identifier in enumerate(bulk_ids)]
            result = store.persist_cleaned_keywords(
                [*records, *bulk_records], rejected=[(rejected_id, "INVALID_SOURCE_RECORD")],
                bing_updates=[bing_update, collision_bing_update],
            )

            mapped_id = hashlib.sha256(f"LIVE:{collision_id}".encode()).hexdigest()
            loaded = store.load_keywords_for_fetch()
            assert loaded["eligible"] == 504 and len(loaded["records"]) == 504
            assert len(loaded["excluded_records"]) == 1
            assert isinstance(loaded["records"][0]["evidence"], str)
            source_limited = store.load_keywords_for_fetch([excluded_id])
            assert source_limited["records"] == []
            assert [item["id"] for item in source_limited["excluded_records"]] == [excluded_id]
            cached_rows = store.load_cached_keywords_for_reclassification([rejected_id])
            assert len(cached_rows) == 1 and cached_rows[0]["evidence"]
            assert isinstance(cached_rows[0]["evidence"], str)
            with pytest.raises(ValueError, match="CACHED_KEYWORD_NOT_FOUND"):
                store.load_cached_keywords_for_reclassification([collision_id])
            assert result["skipped_bing_updates"] == 0
            assert len(result["records"]) == len(records) + len(bulk_records)
            assert result["records"][0]["id"] == excluded_id
            assert next(row["id"] for row in result["records"] if row["keyword"] == "Live topic") == mapped_id
            assert result["id_remap"] == {collision_id: mapped_id}
            excluded = connection.execute(sql.SQL("SELECT eligible,rejection_reason FROM {} WHERE id=%s")
                                          .format(keyword_table), (excluded_id,)).fetchone()
            assert excluded == (0, "USER_EXCLUDED")
            collision_rows = connection.execute(sql.SQL("SELECT id,data_mode,keyword FROM {} "
                "WHERE id IN (%s,%s) ORDER BY data_mode").format(keyword_table),
                (collision_id, mapped_id)).fetchall()
            assert collision_rows == [(mapped_id, "LIVE", "Live topic"),
                                      (collision_id, "SYNTHETIC", "Legacy topic")]
            recovered = connection.execute(sql.SQL("SELECT eligible,rejection_reason FROM {} WHERE id=%s")
                                           .format(keyword_table), (rejected_id,)).fetchone()
            assert recovered == (1, None)
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE id=ANY(%s)")
                                       .format(keyword_table), (bulk_ids,)).fetchone()[0] == len(bulk_ids)
            bing_saved = connection.execute(sql.SQL("SELECT evidence FROM {} WHERE id=%s")
                                             .format(keyword_table), (bing_id,)).fetchone()[0]
            assert bing_saved["bing_verification"] == verification
            assert bing_saved["bing_verification_history"] == [verification]
            collision_saved = connection.execute(sql.SQL("SELECT evidence FROM {} WHERE id=%s")
                .format(keyword_table), (mapped_id,)).fetchone()[0]
            assert collision_saved["bing_verification"] == collision_verification
            legacy_saved = connection.execute(sql.SQL("SELECT evidence FROM {} WHERE id=%s")
                .format(keyword_table), (collision_id,)).fetchone()[0]
            assert legacy_saved == {"legacy": True}

            replay = store.persist_cleaned_keywords([records[-1]], bing_updates=[bing_update])
            assert replay["skipped_bing_updates"] == 0
            replayed_evidence = connection.execute(sql.SQL("SELECT evidence FROM {} WHERE id=%s")
                                                   .format(keyword_table), (bing_id,)).fetchone()[0]
            assert replayed_evidence["bing_verification_history"] == [verification]

            invalid = {**bing_update, "identity": "f" * 64}
            skipped = store.persist_cleaned_keywords([], bing_updates=[invalid])
            assert skipped["skipped_bing_updates"] == 1
            with pytest.raises(ValueError, match="outside the store's data mode scope"):
                store.persist_cleaned_keywords([{**records[0], "data_mode": "SYNTHETIC"}])

            counts = store.persist_dataset_snapshot(
                "ingestion-snapshot", status="PARTIAL", data_mode="LIVE",
                source_manifest=[{"source_id": "google_web_serp", "status": "PARTIAL"}],
                provider_record_count=510, received_record_count=504,
                retained_keyword_count=504, rejected_record_count=2,
                observation_start="2026-11-25", observation_end="2026-11-25",
                fetched_at="2026-11-25T12:05:00Z", quality_flags=["PARTIAL"],
                processing_status="PROCESSING", created_at="2026-11-25T12:06:00+00:00",
            )
            assert counts == {"eligible": 504, "assigned": 0, "unassigned": 504}
            snapshot_table = sql.Identifier(schema, "dataset_snapshots")
            saved_snapshot = connection.execute(sql.SQL("SELECT status,source_manifest,quality_flags,"
                "eligible_keyword_count,assigned_keyword_count,unassigned_keyword_count "
                "FROM {} WHERE dataset_id=%s").format(snapshot_table), ("ingestion-snapshot",)).fetchone()
            assert saved_snapshot == (
                "PARTIAL", [{"source_id": "google_web_serp", "status": "PARTIAL"}], ["PARTIAL"], 504, 0, 504
            )

            job_table = sql.Identifier(schema, "fetch_jobs")
            connection.execute(sql.SQL("INSERT INTO {}(fetch_id,status,updated_at) VALUES (%s,'QUEUED',now())")
                               .format(job_table), ("ingestion-snapshot",))
            store.update_fetch_job("ingestion-snapshot", {
                "status": "PROCESSING", "stage": "ANALYZING", "available": 504,
                "ignored": "not a column",
            })
            saved_job = connection.execute(sql.SQL("SELECT status,stage,available FROM {} WHERE fetch_id=%s")
                                           .format(job_table), ("ingestion-snapshot",)).fetchone()
            assert saved_job == ("PROCESSING", "ANALYZING", 504)

            restore_id = "f" * 64
            connection.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                observed_at,data_mode,evidence,eligible,rejection_reason)
                VALUES (%s,'Restore topic','restore topic','test','test','2026-11-25','LIVE',%s,0,
                'CLASSIFICATION_INELIGIBLE')""").format(keyword_table), (restore_id, Jsonb({})))

            def classification(status, categories):
                return {
                    "entity_type": "topic", "entity_id": None, "entity_description": "",
                    "topic_category_ids": categories, "subcategory_ids": [],
                    "classification_status": status, "confidence_level": "LOW",
                    "commercial_relevance": "NOT_ASSESSED", "classification_method": "RULES",
                    "classification_version": "v1", "taxonomy_version": "v1",
                    "rule_evidence": [], "source_evidence": {}, "review_status": "UNREVIEWED",
                    "classified_at": "2026-11-25T12:10:00+00:00", "classification_reason": "test",
                }

            classified = [
                {"id": excluded_id, "eligible": 0, "classification_result": classification("CLASSIFIED", [])},
                {"id": rejected_id, "eligible": 1, "rejection_reason": None,
                 "classification_result": classification("INELIGIBLE", [])},
                {"id": restore_id, "eligible": 0, "rejection_reason": "CLASSIFICATION_INELIGIBLE",
                 "classification_result": classification("CLASSIFIED", ["test.topic"])},
            ]
            assert store.persist_classifications(classified) == 504
            assert [(item["eligible"], item.get("rejection_reason")) for item in classified] == [
                (0, None), (0, None), (1, "CLASSIFICATION_INELIGIBLE"),
            ]
            saved_classifications = connection.execute(sql.SQL("SELECT keyword_id,classification_status "
                "FROM {} WHERE keyword_id=ANY(%s) ORDER BY keyword_id")
                .format(sql.Identifier(schema, "classification_results")),
                ([excluded_id, rejected_id, restore_id],)).fetchall()
            assert saved_classifications == [
                (excluded_id, "CLASSIFIED"), (rejected_id, "INELIGIBLE"), (restore_id, "CLASSIFIED"),
            ]
            states = connection.execute(sql.SQL("SELECT id,eligible,rejection_reason FROM {} "
                "WHERE id=ANY(%s) ORDER BY id").format(keyword_table),
                ([excluded_id, rejected_id, restore_id],)).fetchall()
            assert states == [
                (excluded_id, 0, "USER_EXCLUDED"),
                (rejected_id, 0, "CLASSIFICATION_INELIGIBLE"),
                (restore_id, 1, None),
            ]
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_incremental_source_persistence_remaps_lineage_to_live_keyword_id():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    created = False
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            created = True
            apply_migrations(connection, schema=schema)
            store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
            raw = {
                "article_display": "Portable Solar Generator",
                "article_original": "Portable_Solar_Generator",
                "project": "en.wikipedia",
                "observation_date": "2026-11-25",
                "fetched_at": "2026-11-26T12:00:00Z",
                "provider_rank": 1,
                "views_ceil": 8400,
                "country_scope": "US",
                "access": "all-access",
            }
            synthetic = keyword_record(raw, WIKIMEDIA_SOURCE_ID, "SYNTHETIC")
            live = keyword_record(raw, WIKIMEDIA_SOURCE_ID, "LIVE")
            assert synthetic["id"] == live["id"]
            keyword_table = sql.Identifier(schema, "keywords")
            synthetic_values = [synthetic[key] for key in (
                "id", "keyword", "normalized", "source", "project", "observed_at",
                "data_mode", "evidence", "context",
            )]
            synthetic_values[7] = Jsonb(json.loads(synthetic_values[7]))
            connection.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                observed_at,data_mode,evidence,context) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""")
                .format(keyword_table), synthetic_values)
            source_evidence_rows = build_source_evidence([{
                "source_id": WIKIMEDIA_SOURCE_ID,
                "status": "SUCCESS",
                "data_mode": "LIVE",
                "records": [raw],
            }])["rows"]

            persisted = store.persist_cleaned_keywords(
                [live], source_evidence_rows=source_evidence_rows,
            )

            expected_live_id = hashlib.sha256(
                f"LIVE:{live['id']}".encode("utf-8")
            ).hexdigest()
            assert persisted["id_remap"] == {live["id"]: expected_live_id}
            assert persisted["records"][0]["id"] == expected_live_id
            evidence_id = source_evidence_rows[0]["id"]
            evidence_links = connection.execute(sql.SQL("SELECT keyword_id FROM {} "
                "WHERE evidence_id=%s").format(sql.Identifier(schema, "evidence_keywords")),
                (evidence_id,)).fetchall()
            assert evidence_links == [(expected_live_id,)]
            payload = connection.execute(sql.SQL("SELECT payload FROM {} WHERE id=%s")
                .format(sql.Identifier(schema, "source_evidence")), (evidence_id,)).fetchone()[0]
            field_ids = {entry["keyword_id"] for entry in
                         payload["retention_lineage"]["keyword_fields"]}
            assert field_ids == {expected_live_id}
            legacy_evidence = connection.execute(sql.SQL("SELECT evidence FROM {} WHERE id=%s")
                .format(keyword_table), (live["id"],)).fetchone()[0]
            assert legacy_evidence == json.loads(synthetic["evidence"])
            replay = store.persist_cleaned_keywords(
                [live], source_evidence_rows=source_evidence_rows,
            )
            assert replay["id_remap"] == {live["id"]: expected_live_id}
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE evidence_id=%s")
                .format(sql.Identifier(schema, "evidence_keywords")),
                (evidence_id,)).fetchone()[0] == 1
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE id=%s")
                .format(sql.Identifier(schema, "source_evidence")),
                (evidence_id,)).fetchone()[0] == 1

            assert persisted["source_evidence_ids"] == (evidence_id,)
            source_evidence_writer = store._persist_source_evidence
            duplicate_source_writes = []

            def record_source_write(db, dataset_id, rows):
                duplicate_source_writes.extend(rows)
                return source_evidence_writer(db, dataset_id, rows)

            store._persist_source_evidence = record_source_write
            try:
                store.persist_dataset_snapshot(
                    "lineage-snapshot", status="SUCCESS", data_mode="LIVE",
                    source_manifest=[{"source_id": WIKIMEDIA_SOURCE_ID, "status": "SUCCESS"}],
                    provider_record_count=1, received_record_count=1,
                    retained_keyword_count=1, rejected_record_count=0,
                    observation_start="2026-11-25", observation_end="2026-11-25",
                    fetched_at="2026-11-26T12:00:00+00:00", quality_flags=[],
                    processing_status="PROCESSING", created_at="2026-11-26T12:01:00+00:00",
                    source_evidence_rows=source_evidence_rows,
                    source_evidence_ids_already_persisted=persisted["source_evidence_ids"],
                )
            finally:
                store._persist_source_evidence = source_evidence_writer

            assert duplicate_source_writes == []
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE dataset_id=%s")
                .format(sql.Identifier(schema, "dataset_evidence")),
                ("lineage-snapshot",)).fetchone()[0] == 1
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE evidence_id=%s")
                .format(sql.Identifier(schema, "evidence_keywords")),
                (evidence_id,)).fetchone()[0] == 1
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_incremental_keyword_persistence_skips_rows_but_keeps_new_observations_and_evidence():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    created = False
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            created = True
            apply_migrations(connection, schema=schema)
            store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")

            def raw(title, observation_date, *, views=8400):
                return {
                    "article_display": title,
                    "article_original": title.replace(" ", "_"),
                    "project": "en.wikipedia",
                    "observation_date": observation_date,
                    "fetched_at": "2026-11-26T12:00:00Z",
                    "provider_rank": 1,
                    "views_ceil": views,
                    "country_scope": "US",
                    "access": "all-access",
                }

            initial_raw = [raw("Unchanged topic", "2026-11-25"),
                           raw("Changed topic", "2026-11-25"),
                           raw("Observation topic", "2026-11-25")]
            initial_records = [keyword_record(item, WIKIMEDIA_SOURCE_ID, "LIVE")
                               for item in initial_raw]
            initial_evidence = build_source_evidence([{
                "source_id": WIKIMEDIA_SOURCE_ID,
                "status": "SUCCESS",
                "data_mode": "LIVE",
                "records": initial_raw,
            }])["rows"]
            first = store.persist_cleaned_keywords(
                initial_records, source_evidence_rows=initial_evidence,
            )
            assert first["persistence_stats"]["keyword_new_rows"] == 3
            assert first["persistence_stats"]["source_evidence_new_rows"] == 3

            changed_raw = raw("Changed topic", "2026-11-26", views=9200)
            new_raw = raw("New topic", "2026-11-26", views=16000)
            changed_record = {
                **initial_records[1],
                "observed_at": "2026-11-26",
                "evidence": json.dumps({**json.loads(initial_records[1]["evidence"]),
                                        "views_ceil": 9200}),
            }
            observation_only_record = {
                **initial_records[2],
                "observed_at": "2026-11-26",
            }
            new_record = keyword_record(new_raw, WIKIMEDIA_SOURCE_ID, "LIVE")
            changed_evidence = build_source_evidence([{
                "source_id": WIKIMEDIA_SOURCE_ID,
                "status": "SUCCESS",
                "data_mode": "LIVE",
                "records": [initial_raw[0], changed_raw, initial_raw[2], new_raw],
            }])["rows"]

            second = store.persist_cleaned_keywords(
                [initial_records[0], changed_record, observation_only_record, new_record],
                source_evidence_rows=changed_evidence,
            )
            stats = second["persistence_stats"]
            assert stats["keyword_new_rows"] == 1
            assert stats["keyword_content_changed_rows"] == 1
            assert stats["keyword_observation_only_rows"] == 1
            assert stats["keyword_unchanged_rows"] == 1
            assert stats["keyword_new_observation_rows"] == 2
            assert stats["source_evidence_new_rows"] == 2
            assert stats["source_evidence_existing_rows"] == 2
            assert stats["evidence_keyword_links_inserted"] == 2

            keyword_table = sql.Identifier(schema, "keywords")
            evidence_table = sql.Identifier(schema, "source_evidence")
            links_table = sql.Identifier(schema, "evidence_keywords")
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE id=ANY(%s)")
                .format(keyword_table), ([record["id"] for record in second["records"]],)).fetchone()[0] == 4
            assert connection.execute(sql.SQL("SELECT observed_at FROM {} WHERE id=%s")
                .format(keyword_table), (initial_records[2]["id"],)).fetchone()[0] == "2026-11-26"
            assert connection.execute(sql.SQL("SELECT evidence FROM {} WHERE id=%s")
                .format(keyword_table), (initial_records[1]["id"],)).fetchone()[0]["views_ceil"] == 9200
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}")
                .format(evidence_table)).fetchone()[0] == 5
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}")
                .format(links_table)).fetchone()[0] == 5
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_failed_source_batch_retry_reuses_committed_postgres_evidence():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    created = False
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            created = True
            apply_migrations(connection, schema=schema)

            class InterruptOnceStore(PostgresCollectionStore):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.persistence_calls = 0
                    self.interrupt_second_source = True

                def persist_cleaned_keywords(self, *args, **kwargs):
                    self.persistence_calls += 1
                    if self.interrupt_second_source and self.persistence_calls == 2:
                        self.interrupt_second_source = False
                        raise RuntimeError("simulated interruption after first source commit")
                    return super().persist_cleaned_keywords(*args, **kwargs)

            store = InterruptOnceStore(environment, schema=schema, data_mode_scope="LIVE")
            service = CollectionService(store, fetch_model_inference_enabled=False)
            fetch_id = "retry-" + uuid.uuid4().hex
            raw = {
                "article_display": "Portable Solar Generator",
                "article_original": "Portable_Solar_Generator",
                "project": "en.wikipedia",
                "observation_date": "2026-11-25",
                "fetched_at": "2026-11-26T12:00:00Z",
                "provider_rank": 1,
                "views_ceil": 8400,
                "country_scope": "US",
                "access": "all-access",
            }
            sources = [
                {
                    "source_id": WIKIMEDIA_SOURCE_ID,
                    "data_mode": "LIVE",
                    "status": "SUCCESS",
                    "records": [raw],
                },
                {
                    "source_id": "google_web_serp",
                    "data_mode": "LIVE",
                    "status": "SUCCESS",
                    "records": [],
                },
            ]

            interrupted = asyncio.run(service.process_fetch(fetch_id, sources))
            keyword_id = keyword_record(raw, WIKIMEDIA_SOURCE_ID, "LIVE")["id"]
            assert interrupted["status"] == "FAILED"
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE id=%s")
                .format(sql.Identifier(schema, "keywords")), (keyword_id,)).fetchone()[0] == 1
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}")
                .format(sql.Identifier(schema, "source_evidence"))).fetchone()[0] == 1
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE keyword_id=%s")
                .format(sql.Identifier(schema, "evidence_keywords")), (keyword_id,)).fetchone()[0] == 1
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE dataset_id=%s")
                .format(sql.Identifier(schema, "dataset_snapshots")), (fetch_id,)).fetchone()[0] == 0

            recovered = asyncio.run(service.process_fetch(fetch_id, sources))

            assert recovered["status"] == "MODEL_UNAVAILABLE"
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE id=%s")
                .format(sql.Identifier(schema, "keywords")), (keyword_id,)).fetchone()[0] == 1
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}")
                .format(sql.Identifier(schema, "source_evidence"))).fetchone()[0] == 1
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE keyword_id=%s")
                .format(sql.Identifier(schema, "evidence_keywords")), (keyword_id,)).fetchone()[0] == 1
            snapshot = connection.execute(sql.SQL("SELECT processing_status FROM {} WHERE dataset_id=%s")
                .format(sql.Identifier(schema, "dataset_snapshots")), (fetch_id,)).fetchone()
            assert snapshot == ("MODEL_UNAVAILABLE",)
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_begin_fetch_processing_preserves_replay_and_mode_rules():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            apply_migrations(connection, schema=schema)
            store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
            job_table = sql.Identifier(schema, "fetch_jobs")

            first = store.begin_fetch_processing("active-fetch", [])
            assert first == {
                "action": "PROCESS", "retry_missing_snapshot": False,
                "retry_existing_snapshot": False, "replay_available": 0, "replay_skipped": 0,
            }
            active = connection.execute(sql.SQL("SELECT status,stage FROM {} WHERE fetch_id=%s")
                                        .format(job_table), ("active-fetch",)).fetchone()
            assert active == ("PROCESSING", "CLEANING")
            duplicate = store.begin_fetch_processing("active-fetch", [])
            assert duplicate == {"action": "RETURN", "result": {
                "fetch_id": "active-fetch", "status": "FAILED",
                "error_code": "FETCH_ID_MODE_CONFLICT",
            }}

            retry_id = "failed-without-snapshot"
            connection.execute(sql.SQL("INSERT INTO {}(fetch_id,status,updated_at) "
                                       "VALUES (%s,'FAILED',now())").format(job_table), (retry_id,))
            retry = store.begin_fetch_processing(retry_id, [{
                "data_mode": "LIVE", "status": "UNAVAILABLE", "records": [],
            }])
            assert retry == {
                "action": "PROCESS", "retry_missing_snapshot": True,
                "retry_existing_snapshot": False, "replay_available": 0, "replay_skipped": 0,
            }

            replay_id = "completed-snapshot"
            connection.execute(sql.SQL("INSERT INTO {}(fetch_id,status,available,skipped,updated_at) "
                                       "VALUES (%s,'COMPLETED',9,2,now())").format(job_table), (replay_id,))
            store.persist_dataset_snapshot(
                replay_id, status="SUCCESS", data_mode="LIVE", source_manifest=[],
                provider_record_count=11, received_record_count=9, retained_keyword_count=9,
                rejected_record_count=2, observation_start="2026-11-25", observation_end="2026-11-25",
                fetched_at="2026-11-25T12:00:00+00:00", quality_flags=[],
                processing_status="COMPLETED", created_at="2026-11-25T12:00:00+00:00",
            )
            replay = store.begin_fetch_processing(replay_id, [])
            assert replay == {
                "action": "PROCESS", "retry_missing_snapshot": False,
                "retry_existing_snapshot": True, "replay_available": 9, "replay_skipped": 2,
            }
            reset = connection.execute(sql.SQL("SELECT status,stage,available,skipped FROM {} "
                                               "WHERE fetch_id=%s").format(job_table), (replay_id,)).fetchone()
            assert reset == ("PROCESSING", "CLEANING", 0, 0)

            mismatch_id = "wrong-mode-snapshot"
            connection.execute(sql.SQL("INSERT INTO {}(fetch_id,status,updated_at) "
                                       "VALUES (%s,'COMPLETED',now())").format(job_table), (mismatch_id,))
            store.persist_dataset_snapshot(
                mismatch_id, status="SUCCESS", data_mode="SYNTHETIC", source_manifest=[],
                provider_record_count=0, received_record_count=0, retained_keyword_count=0,
                rejected_record_count=0, observation_start=None, observation_end=None,
                fetched_at=None, quality_flags=[], processing_status="COMPLETED",
                created_at="2026-11-25T12:00:00+00:00",
            )
            mismatch = store.begin_fetch_processing(mismatch_id, [])
            assert mismatch == {"action": "RETURN", "result": {
                "fetch_id": mismatch_id, "status": "FAILED",
                "error_code": "FETCH_ID_MODE_CONFLICT",
            }}

            store.update_fetch_job("active-fetch", {"available": 7})
            store.mark_fetch_failed("active-fetch", "COLLECTION_PROCESSING_FAILED")
            failed = connection.execute(sql.SQL("SELECT status,available,error_code FROM {} "
                                                "WHERE fetch_id=%s").format(job_table),
                                        ("active-fetch",)).fetchone()
            assert failed == ("FAILED", 7, "COLLECTION_PROCESSING_FAILED")

            empty_id = "empty-fetch"
            store.begin_fetch_processing(empty_id, [])
            connection.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                observed_at,data_mode,evidence) VALUES (%s,'Eligible topic','eligible topic',
                'wikimedia_pageviews_top_per_country','en.wikipedia','2026-11-25','LIVE',%s)""")
                .format(sql.Identifier(schema, "keywords")), ("eligible-topic", Jsonb({})))
            assert store.unassigned_keyword_count() == 1
            store.persist_dataset_snapshot(
                empty_id, status="FAILED", data_mode="LIVE", source_manifest=[],
                provider_record_count=0, received_record_count=0, retained_keyword_count=0,
                rejected_record_count=0, observation_start=None, observation_end=None,
                fetched_at=None, quality_flags=["NO_RECORDS"], processing_status="PROCESSING",
                created_at="2026-11-25T12:00:00+00:00",
            )
            eligible = store.complete_empty_fetch(empty_id, {
                "status": "COMPLETED", "stage": "COMPLETED", "available": 0,
                "processed": 0, "assigned": 0, "unassigned": 0,
                "created_collections": 0, "suggestions": 0, "skipped": 2,
                "cached_embeddings": 0, "inferred_embeddings": 0,
            })
            assert eligible == 1
            empty_state = connection.execute(sql.SQL("SELECT processing_status FROM {} "
                "WHERE dataset_id=%s").format(sql.Identifier(schema, "dataset_snapshots")),
                (empty_id,)).fetchone()
            empty_job = connection.execute(sql.SQL("SELECT status,stage,skipped,eligible FROM {} "
                "WHERE fetch_id=%s").format(job_table), (empty_id,)).fetchone()
            assert empty_state == ("COMPLETED",)
            assert empty_job == ("COMPLETED", "COMPLETED", 2, 1)

            refresh_id = "refresh-topic-counts"
            store.begin_fetch_processing(refresh_id, [])
            store.persist_dataset_snapshot(
                refresh_id, status="SUCCESS", data_mode="LIVE", source_manifest=[],
                provider_record_count=1, received_record_count=1, retained_keyword_count=1,
                rejected_record_count=0, observation_start="2026-11-25", observation_end="2026-11-25",
                fetched_at="2026-11-25T23:59:00+00:00", quality_flags=[],
                processing_status="COMPLETED", created_at="2026-11-25T23:59:00+00:00",
            )
            collection_id = store.create({"name": "Count Refresh", "description": ""})
            connection.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,origin,pinned,
                score,reason,created_at) VALUES (%s,'eligible-topic','USER',1,NULL,'test',now())""")
                .format(sql.Identifier(schema, "memberships")), (collection_id,))
            assert store.user_member_examples([collection_id]) == {collection_id: ["Eligible topic"]}
            assert store.count_ai_assigned_keywords() == 0
            refreshed = store.refresh_latest_topic_counts()
            assert refreshed == {"dataset_id": refresh_id, "eligible": 1, "assigned": 1,
                                 "ai_assigned": 0, "unassigned": 0}
            refreshed_snapshot = connection.execute(sql.SQL("""SELECT eligible_keyword_count,
                assigned_keyword_count,unassigned_keyword_count FROM {} WHERE dataset_id=%s""")
                .format(sql.Identifier(schema, "dataset_snapshots")), (refresh_id,)).fetchone()
            refreshed_job = connection.execute(sql.SQL("SELECT eligible,assigned,unassigned FROM {} "
                "WHERE fetch_id=%s").format(job_table), (refresh_id,)).fetchone()
            assert refreshed_snapshot == (1, 1, 0)
            assert refreshed_job == (1, 0, 0)
            assert store.complete_fetch_dataset_snapshot(refresh_id) == {
                "eligible": 1, "assigned": 1, "unassigned": 0,
            }

            unavailable_id = "model-unavailable-fetch"
            store.begin_fetch_processing(unavailable_id, [])
            store.persist_dataset_snapshot(
                unavailable_id, status="PARTIAL", data_mode="LIVE", source_manifest=[],
                provider_record_count=14, received_record_count=12, retained_keyword_count=12,
                rejected_record_count=2, observation_start=None, observation_end=None,
                fetched_at=None, quality_flags=["PARTIAL"], processing_status="PROCESSING",
                created_at="2026-11-25T12:00:00+00:00",
            )
            store.mark_fetch_model_unavailable(
                unavailable_id, available=12, skipped=3, eligible=10, cached_embeddings=4,
                inferred_embeddings=0, error_code="MODEL_INFERENCE_DEFERRED",
            )
            unavailable_snapshot = connection.execute(sql.SQL("SELECT processing_status FROM {} "
                "WHERE dataset_id=%s").format(sql.Identifier(schema, "dataset_snapshots")),
                (unavailable_id,)).fetchone()
            unavailable_job = connection.execute(sql.SQL("""SELECT status,stage,available,processed,
                skipped,error_code,eligible,cached_embeddings,inferred_embeddings FROM {}
                WHERE fetch_id=%s""").format(job_table), (unavailable_id,)).fetchone()
            assert unavailable_snapshot == ("MODEL_UNAVAILABLE",)
            assert unavailable_job == ("MODEL_UNAVAILABLE", "MODEL_UNAVAILABLE", 12, 0, 3,
                                       "MODEL_INFERENCE_DEFERRED", 10, 4, 0)
        finally:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_algorithm_analysis_uses_atomic_postgres_store_session():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            apply_migrations(connection, schema=schema)
            store = PostgresCollectionStore(environment, schema=schema)
            record = {
                "id": "analysis-" + uuid.uuid4().hex,
                "keyword": "Solar generator",
                "normalized": "solar generator",
                "source": "wikimedia_pageviews_top_per_country",
                "project": "en.wikipedia",
                "observed_at": "2026-11-25T00:00:00+00:00",
                "data_mode": "LIVE",
                "evidence": json.dumps({
                    "article_original": "Solar_generator",
                    "observation_date": "2026-11-25",
                    "fetched_at": "2026-11-26T01:00:00+00:00",
                    "views_ceil": 900,
                    "provider_rank": 4,
                    "snapshot_status": "SUCCESS",
                }),
                "context": "",
            }
            changed = store.persist_cleaned_keywords([record])["records"]
            service = CollectionService(store, fetch_model_inference_enabled=False)
            first = service._analyze_persisted_keywords(changed)
            assert first["eligible_keywords"] == 1
            assert first["executed_keywords"] == 1
            assert first["registered_algorithms"] == 13
            assert first["persisted_algorithms"] == 4
            from unittest.mock import patch
            with patch("app.collections.service._algorithm_digest_input",
                       side_effect=AssertionError("PostgreSQL state cache should hit")):
                replay = service._analyze_persisted_keywords(changed)
            assert replay["executed_keywords"] == 0
            assert replay["unchanged_keywords"] == 1
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE keyword_id=%s")
                                      .format(sql.Identifier(schema, "algorithm_observations")),
                                      (record["id"],)).fetchone()[0] == 1
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE keyword_id=%s")
                                      .format(sql.Identifier(schema, "algorithm_results")),
                                      (record["id"],)).fetchone()[0] == 4
            persisted_results = connection.execute(sql.SQL("""SELECT algorithm_id,version,status,
                metric,unit,result_json,input_digest FROM {} WHERE keyword_id=%s ORDER BY algorithm_id""")
                .format(sql.Identifier(schema, "algorithm_results")),
                (record["id"],)).fetchall()
            observation = connection.execute(sql.SQL("""SELECT source,observation_date,country,
                metric,unit,input_digest FROM {} WHERE keyword_id=%s""")
                .format(sql.Identifier(schema, "algorithm_observations")),
                (record["id"],)).fetchone()
            for result_row in persisted_results:
                payload = json.loads(result_row[5])
                assert payload["algorithm_id"] == result_row[0]
                assert payload["algorithm_version"] == result_row[1]
                assert payload["source_id"] == observation[0]
                assert payload["observation_date"] == observation[1]
                assert payload["input_metric"] == observation[3]
                assert payload["input_unit"] == observation[4]
                assert payload["input_scope"]["country"] == observation[2]
                assert payload["window_days"] == 6
                assert payload["input_digest"] == result_row[6]
                assert payload["evidence_references"] == [{
                    "source_id": observation[0],
                    "keyword_id": record["id"],
                    "observation_date": observation[1],
                    "metric": observation[3],
                    "unit": observation[4],
                    "input_digest": observation[5],
                }]
            digests = store.run_algorithm_analysis(
                lambda session: session.result_digests_for([record["id"]])
            )
            assert set(digests[record["id"]]) == {
                "growth-ratio", "median-baseline", "observed-volume-rank",
                "observed-percentile-rank",
            }
        finally:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_google_trends_increase_rank_persists_lineage_and_skips_unchanged_inputs():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            apply_migrations(connection, schema=schema)
            store = PostgresCollectionStore(environment, schema=schema)
            service = CollectionService(store, fetch_model_inference_enabled=False)
            fetched_at = "2026-11-26T12:00:00+00:00"
            provider_data_at = "2026-11-26T11:30:00+00:00"
            rows = []
            for phrase, increase, volume in (
                    ("linen pants", 300.0, 20000),
                    ("portable solar generator", 100.0, 5000),
                    ("quiet coffee maker", 300.0, 10000)):
                _, identity = normalize_keyword(phrase)
                rows.append({
                    "data_source": "GOOGLE_TRENDS",
                    "provider_id": "serpapi_google_trends_trending_now",
                    "transport_id": "serpapi",
                    "discovery_mode": "SERPAPI_TRENDING_NOW",
                    "canonical_keyword": phrase,
                    "canonical_keyword_identity": hashlib.sha256(
                        identity.encode("utf-8")
                    ).hexdigest(),
                    "region": "US", "language": "en", "time_window_hours": 24,
                    "provider_data_at": provider_data_at,
                    "provider_created_at": provider_data_at,
                    "fetched_at": fetched_at,
                    "source_url": "https://trends.google.com/trending?geo=US&hl=en-US",
                    "delivery_state": "FRESH", "quality_flags": [],
                    "trend_clusters": [{
                        "trend_cluster_id": "cluster-" + hashlib.sha256(
                            phrase.encode("utf-8")
                        ).hexdigest()[:12],
                        "increase_percentage": increase,
                        "search_volume": volume,
                        "search_volume_display": f"{volume // 1000}K+",
                        "search_volume_semantics": "PROVIDER_REPORTED_APPROXIMATE_BUCKET",
                        "started_at": provider_data_at,
                        "ended_at": fetched_at,
                    }],
                })
            source = {
                "source_id": GOOGLE_SOURCE_ID, "status": "SUCCESS",
                "data_mode": "SYNTHETIC", "record_count": len(rows), "records": rows,
            }
            cleaned = [keyword_record(row, GOOGLE_SOURCE_ID, "SYNTHETIC") for row in rows]
            assert all(cleaned)
            lineage = build_source_evidence([source])["rows"]
            persisted = store.persist_cleaned_keywords(
                cleaned, source_evidence_rows=lineage,
            )
            first = service._analyze_google_trends_keywords(
                persisted["records"], sources=[source],
                keyword_id_map=persisted["id_remap"],
                persisted_source_evidence_ids=persisted["source_evidence_ids"],
            )
            assert first["executed_keywords"] == 3
            assert first["recomputed_algorithms"] == 3
            assert first["rankable_cohort_count"] == 1

            result_rows = connection.execute(sql.SQL("""SELECT k.keyword,r.status,r.value,
                r.reason_code,r.result_json,r.input_digest FROM {} r JOIN {} k ON k.id=r.keyword_id
                WHERE r.algorithm_id='google-trends-increase-rank' ORDER BY k.keyword""").format(
                    sql.Identifier(schema, "algorithm_results"),
                    sql.Identifier(schema, "keywords"))).fetchall()
            ranks = {row[0]: row[2] for row in result_rows}
            assert ranks == {"linen pants": 1, "portable solar generator": 3,
                             "quiet coffee maker": 1}
            completed_payload = json.loads(result_rows[0][4])
            assert completed_payload["input_metric"] == "provider_increase_percentage"
            assert completed_payload["provider_reported_increase_percentage"] in {100, 300}
            assert completed_payload["approximate_search_volume_bucket"]["semantics"] == (
                "PROVIDER_REPORTED_APPROXIMATE_BUCKET"
            )
            assert completed_payload["geography"] == {
                "provider_region_filter": "US", "language_filter": "en",
                "market_evidence": "US_FILTER_ONLY", "audience_residence_claimed": False,
            }
            assert completed_payload["evidence_references"]
            reference_id = completed_payload["evidence_references"][0]["source_evidence_id"]
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE id=%s")
                                      .format(sql.Identifier(schema, "source_evidence")),
                                      (reference_id,)).fetchone()[0] == 1
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE evidence_id=%s")
                                      .format(sql.Identifier(schema, "evidence_keywords")),
                                      (reference_id,)).fetchone()[0] >= 1
            exposed = store.algorithm_results_overview(
                algorithm_id="google-trends-increase-rank", status="COMPLETED",
            )
            assert exposed["total"] == 3

            import httpx

            from app.main import create_app
            from app.settings import Settings

            app = create_app(read_only=True, settings=Settings.from_environment({
                "TREND_SCOUT_ENVIRONMENT": "test", "TREND_SCOUT_READ_ONLY": "true",
            }))
            app.state.collection_service = service

            async def read_algorithm_results():
                async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url="http://testserver") as client:
                    return await client.get("/api/v1/algorithms/results", params={
                        "algorithm_id": "google-trends-increase-rank",
                        "status": "COMPLETED",
                    })

            api_response = asyncio.run(read_algorithm_results())
            assert api_response.status_code == 200
            assert api_response.json()["total"] == 3
            assert all(item["algorithm_id"] == "google-trends-increase-rank"
                       for item in api_response.json()["items"])

            from unittest.mock import patch
            with patch("app.composition.algorithm_engine") as engine_factory:
                second = service._analyze_google_trends_keywords(
                    persisted["records"], sources=[source],
                    keyword_id_map=persisted["id_remap"],
                    persisted_source_evidence_ids=persisted["source_evidence_ids"],
                )
                engine_factory.return_value.run.assert_not_called()
            assert second["unchanged_keywords"] == 3
            assert second["recomputed_algorithms"] == 0

            refreshed_rows = [dict(row) for row in rows]
            refreshed_rows[0] = {
                **refreshed_rows[0],
                "fetched_at": "2026-11-26T12:05:00+00:00",
                "trend_clusters": [{
                    **refreshed_rows[0]["trend_clusters"][0],
                    "search_volume": 21000,
                    "search_volume_display": "21K+",
                }],
            }
            refreshed_source = {**source, "records": refreshed_rows}
            refreshed_cleaned = [keyword_record(row, GOOGLE_SOURCE_ID, "SYNTHETIC")
                                 for row in refreshed_rows]
            refreshed_persisted = store.persist_cleaned_keywords(
                refreshed_cleaned,
                source_evidence_rows=build_source_evidence([refreshed_source])["rows"],
            )
            with patch("app.composition.algorithm_engine") as engine_factory:
                metadata_refresh = service._analyze_google_trends_keywords(
                    refreshed_persisted["records"], sources=[refreshed_source],
                    keyword_id_map=refreshed_persisted["id_remap"],
                    persisted_source_evidence_ids=refreshed_persisted["source_evidence_ids"],
                )
                engine_factory.return_value.run.assert_not_called()
            assert metadata_refresh["unchanged_keywords"] == 3
            assert metadata_refresh["recomputed_algorithms"] == 0
            assert metadata_refresh["metadata_updated_results"] == 1
            refreshed_payload = connection.execute(sql.SQL("""SELECT r.result_json FROM {} r
                JOIN {} k ON k.id=r.keyword_id WHERE k.keyword='linen pants'
                AND r.algorithm_id='google-trends-increase-rank'""").format(
                    sql.Identifier(schema, "algorithm_results"),
                    sql.Identifier(schema, "keywords"))).fetchone()[0]
            refreshed_payload = (json.loads(refreshed_payload)
                                 if isinstance(refreshed_payload, str) else refreshed_payload)
            assert refreshed_payload["approximate_search_volume_bucket"]["value"] == 21000
            assert refreshed_payload["freshness"]["fetched_at"] == (
                "2026-11-26T12:05:00+00:00"
            )
            linked_evidence_ids = {
                row[0] for row in connection.execute(sql.SQL("""SELECT evidence_id FROM {}
                    WHERE keyword_id=%s""").format(sql.Identifier(schema, "evidence_keywords")),
                    (refreshed_payload["keyword_id"],)).fetchall()
            }
            expected_evidence_ids = sorted(
                linked_evidence_ids.intersection(refreshed_persisted["source_evidence_ids"])
            )
            assert [ref["source_evidence_id"]
                    for ref in refreshed_payload["evidence_references"]] == expected_evidence_ids

            changed_rows = [dict(row) for row in rows]
            changed_rows[0]["trend_clusters"] = [{
                **changed_rows[0]["trend_clusters"][0], "increase_percentage": 50.0,
            }]
            changed_source = {**source, "records": changed_rows}
            changed_cleaned = [keyword_record(row, GOOGLE_SOURCE_ID, "SYNTHETIC")
                               for row in changed_rows]
            changed_persisted = store.persist_cleaned_keywords(
                changed_cleaned,
                source_evidence_rows=build_source_evidence([changed_source])["rows"],
            )
            changed = service._analyze_google_trends_keywords(
                changed_persisted["records"], sources=[changed_source],
                keyword_id_map=changed_persisted["id_remap"],
                persisted_source_evidence_ids=changed_persisted["source_evidence_ids"],
            )
            assert changed["executed_keywords"] == 3
            assert changed["recomputed_algorithms"] == 3

            live_phrase = "live trend sample"
            _, live_identity = normalize_keyword(live_phrase)
            live_row = {
                **rows[0],
                "canonical_keyword": live_phrase,
                "canonical_keyword_identity": hashlib.sha256(
                    live_identity.encode("utf-8")
                ).hexdigest(),
                "trend_clusters": [{**rows[0]["trend_clusters"][0],
                                    "trend_cluster_id": "live-cluster"}],
            }
            live_source = {**source, "data_mode": "LIVE", "records": [live_row],
                           "record_count": 1}
            live_cleaned = keyword_record(live_row, GOOGLE_SOURCE_ID, "LIVE")
            live_lineage = build_source_evidence([live_source])["rows"]
            live_persisted = store.persist_cleaned_keywords(
                [live_cleaned], source_evidence_rows=live_lineage,
            )
            denied = ProcessingGrant(
                GOOGLE_SOURCE_ID, True, False,
                datetime.now(timezone.utc) + timedelta(minutes=5), "test-policy",
            )
            policy_result = service._analyze_google_trends_keywords(
                live_persisted["records"], sources=[live_source],
                processing_grants=(denied,),
                keyword_id_map=live_persisted["id_remap"],
                persisted_source_evidence_ids=live_persisted["source_evidence_ids"],
            )
            assert policy_result["skipped_keywords"] == 1
            policy_row = connection.execute(sql.SQL("SELECT status,reason_code,value FROM {} "
                "WHERE keyword_id=%s AND algorithm_id='google-trends-increase-rank'").format(
                    sql.Identifier(schema, "algorithm_results")),
                (live_persisted["records"][0]["id"],)).fetchone()
            assert policy_row == ("SKIPPED", "POLICY_RESTRICTED", None)
        finally:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_collection_service_processes_fixture_through_postgres_without_model_or_provider_calls():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            apply_migrations(connection, schema=schema)
            store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
            def unexpected_inference(*_args, **_kwargs):
                raise AssertionError("PostgreSQL Fetch must not wait for AI inference")

            service = CollectionService(
                store,
                fetch_model_inference_enabled=False,
                ai_engine=SimpleNamespace(classify_keyword_ids=unexpected_inference),
            )
            fetch_id = "pg-fixture-" + uuid.uuid4().hex
            source = {
                "source_id": "wikimedia_pageviews_top_per_country",
                "status": "SUCCESS",
                "data_mode": "LIVE",
                "delivery_state": "CACHED",
                "record_count": 1,
                "records": [{
                    "article_display": "Solar generator",
                    "article_original": "Solar_generator",
                    "project": "en.wikipedia",
                    "observation_date": "2026-11-25",
                    "fetched_at": "2026-11-25T12:00:00+00:00",
                    "views_ceil": 900,
                    "provider_rank": 4,
                }],
            }
            result = service._process_fetch(fetch_id, [source])

            assert result["status"] == "MODEL_UNAVAILABLE"
            assert result["error_code"] == "MODEL_INFERENCE_DEFERRED"
            assert result["available"] == 1 and result["unassigned"] == 1
            rows = store.search_keywords(query="Solar generator")["items"]
            assert len(rows) == 1 and rows[0]["data_mode"] == "LIVE"
            assert len(rows[0]["algorithm_results"]) == 4
            assert store.fetch_collection_processing_result(fetch_id) == {
                "status": "MODEL_UNAVAILABLE", "error_code": "MODEL_INFERENCE_DEFERRED",
            }
            history = store.local_run_history_rows(fetch_id=fetch_id)
            assert len(history) == 1
            assert history[0]["processing_status"] == "MODEL_UNAVAILABLE"
            assert store.latest_dataset()["dataset_id"] == fetch_id

            retry = store.begin_fetch_processing(
                fetch_id, [source], resume_model_unavailable=True,
            )
            assert retry == {
                "action": "PROCESS", "retry_missing_snapshot": False,
                "retry_existing_snapshot": True, "replay_available": 1,
                "replay_skipped": 0,
            }
            selected_keywords = store.load_keywords_for_fetch([rows[0]["id"]])
            empty_keywords = store.load_keywords_for_fetch([])
            assert [item["keyword"] for item in selected_keywords["records"]] == ["Solar generator"]
            assert empty_keywords["records"] == []
            concurrent_retry = store.begin_fetch_processing(
                fetch_id, [], resume_model_unavailable=True,
            )
            assert concurrent_retry["action"] == "RETURN"
            assert concurrent_retry["result"]["status"] == "PROCESSING"
        finally:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_research_repository_preserves_scope_filters_and_crud():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            apply_migrations(connection, schema=schema)
            store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
            keyword_id = "research-keyword-" + uuid.uuid4().hex
            store.persist_cleaned_keywords([{
                "id": keyword_id, "keyword": "Solar generator", "normalized": "solar generator",
                "source": "wikimedia_pageviews_top_per_country", "project": "en.wikipedia",
                "observed_at": "2026-11-25T00:00:00+00:00", "data_mode": "LIVE",
                "evidence": "{}", "context": "",
            }])
            processing_records = store.load_keywords_for_collection_processing()
            assert [item["id"] for item in processing_records] == [keyword_id]
            assert isinstance(processing_records[0]["evidence"], str)
            record = {
                "id": "research-" + uuid.uuid4().hex,
                "keyword_id": keyword_id,
                "subject": "Solar generator hypothesis",
                "hypothesis": "Test demand among off-grid households.",
                "product_category": "Energy", "niche": "Off-grid homes",
                "competitor_notes": "Pending", "market_evidence": "Not measured",
                "ip_notes": "Pending", "risk_notes": "Pending", "findings": "",
                "status": "DRAFT", "created_at": "2026-11-25T12:00:00+00:00",
                "updated_at": "2026-11-25T12:00:00+00:00",
            }
            created = store.create_research(record)
            assert created == {key: record[key] for key in ("id", "status", "created_at", "updated_at")}
            listed = store.list_research(q=" OFF-GRID ", status="DRAFT")
            assert listed["total"] == 1 and listed["next_offset"] is None
            assert listed["items"][0]["keyword"] == "Solar generator"
            assert listed["items"][0]["data_mode"] == "LIVE"
            updated = store.update_research(record["id"], {"status": "RESEARCHING"},
                                            "2026-11-25T13:00:00+00:00")
            assert updated == {"id": record["id"], "status": "RESEARCHING",
                               "updated_at": "2026-11-25T13:00:00+00:00"}
            assert store.list_research(status="DRAFT")["total"] == 0
            assert store.update_research("missing", {"status": "ARCHIVED"},
                                         "2026-11-25T13:00:00+00:00") is None
            with pytest.raises(ValueError, match="RESEARCH_KEYWORD_NOT_FOUND"):
                store.create_research({**record, "id": "research-missing-keyword",
                                       "keyword_id": "missing"})
        finally:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_entity_context_repository_round_trips_json_and_cache_version():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            apply_migrations(connection, schema=schema)
            store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
            keyword_id = "entity-context-" + uuid.uuid4().hex
            store.persist_cleaned_keywords([{
                "id": keyword_id, "keyword": "MediaWiki", "normalized": "mediawiki",
                "source": "wikimedia_pageviews_top_per_country", "project": "mediawiki",
                "observed_at": "2026-11-25T00:00:00+00:00", "data_mode": "LIVE",
                "evidence": "{}", "context": "",
            }])
            context = EntityContext(
                keyword_id, "MediaWiki", 1, "Q5296", "A free wiki software.", "software_product",
                (), ("technology.software",), "https://www.mediawiki.org/wiki/MediaWiki",
                "WIKIPEDIA_PAGE_ID_AND_WIKIDATA_CLAIMS", "CLASSIFIED",
                "2026-11-25T12:30:00+00:00", ("Q7397",), 0, False, "", "en", True, True,
            )
            store.upsert_entity_context([context.as_row()])
            resolver = WikimediaEntityContextResolver(store)
            restored = resolver.cached([keyword_id])[keyword_id]
            assert restored == context
            assert store.entity_context_keyword_ids([keyword_id], "not-current-context") == set()
            assert store.entity_context_keyword_ids([keyword_id], "wikimedia-entity-context-v4") == {keyword_id}
            assert resolver._uncached_records([{
                "id": keyword_id, "keyword": "MediaWiki", "project": "mediawiki",
                "source": "wikimedia_pageviews_top_per_country",
            }]) == []
        finally:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
