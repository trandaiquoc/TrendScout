"""Compare PostgreSQL summaries with the existing SQLite behavior on fixtures."""

import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import pytest
from psycopg import sql

from app.collections.postgres_reads import PostgresCollectionReader
from app.collections.store import CollectionStore
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations
from app.storage.sqlite_to_postgres import import_backup


def test_summary_and_tree_match_legacy_on_isolated_postgres(tmp_path):
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    path = Path(tmp_path) / "source.sqlite"
    legacy = CollectionStore(path, data_mode_scope="LIVE")
    root = legacy.create({"name": "Root", "auto_match": False})
    child = legacy.create({"name": "Child", "parent_id": root, "auto_match": False})
    timestamp = "2026-11-25T10:00:00+00:00"
    with legacy.connect() as db:
        for key, eligible, evidence in (("k1", 1, {"views_ceil": 0, "provider_rank": 4}),
                                        ("k2", 1, {"views_ceil": 25, "provider_rank": 2}),
                                        ("k3", 0, {}),
                                        ("k4", 1, {"views_ceil": " +3 items", "provider_rank": "-1x"}),
                                        ("k5", 1, {"views_ceil": False, "provider_rank": True})):
            db.execute("""INSERT INTO keywords(id,keyword,normalized,source,observed_at,data_mode,evidence,eligible)
                VALUES(?,?,?,?,?,?,?,?)""", (key, key, key, "synthetic", "2026-11-25", "LIVE",
                                              json.dumps(evidence), eligible))
        for key, project, mode, eligible in (
            ("candidate-live", "google_trends:daily", "LIVE", 1),
            ("candidate-ineligible", "google_trends:daily", "LIVE", 0),
            ("candidate-prefix-miss", "googleXtrends:daily", "LIVE", 1),
            ("candidate-demo", "google_trends:daily", "DEMO", 1),
        ):
            db.execute("""INSERT INTO keywords(id,keyword,normalized,source,project,observed_at,
                data_mode,evidence,eligible) VALUES(?,?,?,?,?,?,?,?,?)""",
                (key, key, key, "synthetic", project, "2026-11-26T10:00:00+00:00", mode,
                 json.dumps({"candidate": key}), eligible))
        for collection, key in [(item, "k1") for item in sorted((root, child), reverse=True)] + [(child, "k2")]:
            db.execute("""INSERT INTO memberships(collection_id,keyword_id,origin,pinned,created_at)
                VALUES(?,?,'USER',1,?)""", (collection, key, timestamp))
        db.execute("""INSERT INTO dataset_snapshots(dataset_id,status,data_mode,source_manifest,
            provider_record_count,received_record_count,retained_keyword_count,rejected_record_count,
            observation_start,observation_end,fetched_at,quality_flags,processing_status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", ("dataset", "PARTIAL", "LIVE",
            json.dumps([{"source_id": "synthetic"}]), 3, 3, 2, 0, "2026-11-25", "2026-11-25",
            timestamp, "[]", "COMPLETED", timestamp))
        db.execute("INSERT INTO fetch_jobs(fetch_id,status,updated_at) VALUES(?,?,?)",
                   ("dataset", "COMPLETED", timestamp))
        db.execute("INSERT INTO fetch_jobs(fetch_id,status,updated_at) VALUES(?,?,?)",
                   ("fetch-only", "COMPLETED", "2026-11-25T11:00:00+00:00"))
        db.execute("""INSERT INTO algorithm_results(keyword_id,algorithm_id,version,status,value,metric,unit,
            result_json,input_digest,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""", ("k1", "score", "1", "COMPLETED",
            0, "metric", "unit", '{"b": 2, "a": 1}', "digest", timestamp))
    backup = Path(tmp_path) / "backup.sqlite"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(backup)) as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
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
            reader = PostgresCollectionReader(environment, schema=schema, data_mode_scope="LIVE")
            assert reader.latest_dataset() is None
            assert reader.latest_dataset_reuse_summary() is None
            assert reader.list_collections() == []
            import_backup(connection, backup, schema=schema)
            assert reader.latest_dataset() == legacy.latest_dataset()
            assert reader.latest_dataset_reuse_summary() == {
                "dataset_id": "dataset",
                "processing_status": "COMPLETED",
                "source_manifest": [{"source_id": "synthetic"}],
            }
            query = {"source_id": "synthetic", "project_prefix": "google_trends:",
                     "data_mode": "LIVE", "eligible_only": True, "limit": 10}
            assert reader.keyword_evidence_rows(**query) == legacy.keyword_evidence_rows(**query)
            assert [item["id"] for item in reader.keyword_evidence_rows(**query)] == ["candidate-live"]
            assert reader.fetch_collection_processing_result("dataset") == (
                legacy.fetch_collection_processing_result("dataset")
            ) == {"status": "COMPLETED", "error_code": None}
            assert reader.fetch_collection_processing_result("fetch-only") is None
            assert reader.algorithm_results_overview() == legacy.algorithm_results_overview()
            assert reader.analytics_overview_inputs() == legacy.analytics_overview_inputs()
            pg_metrics = reader.runtime_storage_metrics("unused-model")
            assert pg_metrics["engine"] == "POSTGRESQL"
            assert pg_metrics["integrity"] is None
            assert pg_metrics["keyword_records"] == 8
            assert pg_metrics["cache_vectors"] == 0 and pg_metrics["database_size_bytes"] > 0
            def canonical_snapshots(rows):
                for row in rows:
                    if isinstance(row.get("source_manifest"), str):
                        row["source_manifest"] = json.loads(row["source_manifest"])
                return rows

            assert canonical_snapshots(reader.recent_dataset_snapshots()) == canonical_snapshots(
                legacy.recent_dataset_snapshots())
            def canonical_run_rows(rows):
                for row in rows:
                    for field in ("source_manifest", "quality_flags"):
                        if isinstance(row.get(field), str):
                            row[field] = json.loads(row[field])
                return rows

            assert canonical_run_rows(reader.local_run_history_rows()) == canonical_run_rows(
                legacy.local_run_history_rows())
            first_run = legacy.local_run_history_rows(limit=1)[0]
            cursor = (first_run["history_sort_at"], first_run["fetch_id"])
            assert canonical_run_rows(reader.local_run_history_rows(cursor=cursor)) == canonical_run_rows(
                legacy.local_run_history_rows(cursor=cursor))
            assert canonical_run_rows(reader.local_run_history_rows(mode="LIVE")) == canonical_run_rows(
                legacy.local_run_history_rows(mode="LIVE"))
            assert canonical_run_rows(reader.local_run_history_rows(status="PARTIAL", mode="LIVE")) == (
                canonical_run_rows(legacy.local_run_history_rows(status="PARTIAL", mode="LIVE")))
            assert canonical_run_rows(reader.local_run_history_rows(fetch_id="dataset", limit=1)) == (
                canonical_run_rows(legacy.local_run_history_rows(fetch_id="dataset", limit=1)))
            assert sorted(reader.list_collections(), key=lambda item: item["id"]) == sorted(
                legacy.list_collections(), key=lambda item: item["id"])
            assert reader.workbench_summary() == legacy.workbench_summary()
            for options in ({}, {"sort": "views"}, {"sort": "rank"}, {"sort": "keyword", "limit": 2},
                            {"state": "unassigned"}, {"state": "manual"}, {"state": "excluded"},
                            {"evidence": "metric"}, {"evidence": "missing"}, {"evidence": "algorithm"},
                            {"collection_ids": [root]}, {"query": "K1"}, {"keyword_id": "k1"},
                            {"ids_only": True, "state": "manual"}, {"include_algorithm_results": False}):
                actual = reader.search_keywords(**options)
                expected = legacy.search_keywords(**options)
                # The legacy membership aggregation has no ORDER BY; compare its
                # members as a set while preserving all other response ordering.
                for result in (actual, expected):
                    for item in result.get("items", []):
                        item["memberships"].sort(key=lambda membership: membership["id"])
                assert actual == expected
            connection.execute(sql.SQL("DELETE FROM {}.dataset_snapshots").format(sql.Identifier(schema)))
            with legacy.connect() as source:
                source.execute("DELETE FROM dataset_snapshots")
            assert reader.latest_dataset() == legacy.latest_dataset()
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
