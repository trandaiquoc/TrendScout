"""Offline contracts for provider evidence lineage metadata."""

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from psycopg import sql

from app.collections.postgres_store import PostgresCollectionStore
from app.collections.service import CollectionService
from app.collections.source_lineage import build_source_evidence
from app.collections.store import keyword_record
from app.retention.policy import effective_retention_deadline
from app.retention.source_policy import (
    SOURCE_RETENTION_POLICY_VERSION,
    effective_source_retention_deadline,
    source_retention_policy,
)
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    SOURCE_CAPABILITIES,
    WIKIMEDIA_CONTEXT_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations


def test_wikimedia_lineage_preserves_date_scope_without_inventing_observation_time():
    raw = {
        "article_display": "Portable Solar Generator",
        "article_original": "Portable_Solar_Generator",
        "project": "en.wikipedia",
        "observation_date": "2026-11-25",
        "fetched_at": "2026-11-26T12:00:00+07:00",
        "provider_rank": 1,
        "views_ceil": 8400,
        "country_scope": "US",
        "access": "all-access",
        "source_url": "https://en.wikipedia.org/wiki/Portable_Solar_Generator",
    }
    result = build_source_evidence([{
        "source_id": WIKIMEDIA_SOURCE_ID,
        "status": "SUCCESS",
        "data_mode": "LIVE",
        "records": [raw],
    }])

    assert result["untracked_record_count"] == 0
    assert len(result["rows"]) == 1
    evidence = result["rows"][0]
    keyword = keyword_record(raw, WIKIMEDIA_SOURCE_ID, "LIVE")
    assert evidence["keyword_ids"] == (keyword["id"],)
    assert evidence["source_id"] == WIKIMEDIA_SOURCE_ID
    assert evidence["collected_at"] == datetime(2026, 11, 26, 5, tzinfo=timezone.utc)
    assert evidence["observed_at"] is None
    assert evidence["query_scope"]["observation_date"] == "2026-11-25"
    assert evidence["geography"] == {
        "field": "country_scope",
        "value": "US",
        "meaning": "observation_geography",
    }
    assert evidence["expires_at"] == effective_retention_deadline(evidence["collected_at"])
    assert evidence["retention_policy"]["version"] == SOURCE_RETENTION_POLICY_VERSION
    assert evidence["retention_policy"]["provider_max_age_days"] is None
    assert evidence["retention_policy"]["review_status"] == "reviewed"
    keyword_evidence = json.loads(keyword["evidence"])
    field_lineage = evidence["payload"]["retention_lineage"]["keyword_fields"]
    assert evidence["payload"]["retention_lineage"]["version"] == 1
    assert {entry["path"][0] for entry in field_lineage} == {
        "article_original", "article_display", "provider_rank", "views_ceil",
        "country_scope", "access", "observation_date", "fetched_at",
        "source_url",
    }
    assert all(entry["keyword_id"] == keyword["id"] for entry in field_lineage)
    expected_digests = {
        field: hashlib.sha256(json.dumps(
            keyword_evidence[field], ensure_ascii=False, sort_keys=True,
            allow_nan=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        for field in (entry["path"][0] for entry in field_lineage)
    }
    assert {entry["path"][0]: entry["value_sha256"] for entry in field_lineage} == (
        expected_digests
    )
    assert "Portable Solar Generator" not in json.dumps(evidence["payload"])
    assert len(evidence["content_digest"]) == 64
    assert "Portable Solar Generator" not in str(evidence["query_scope"])


def test_missing_collection_time_is_untracked_even_when_snapshot_time_exists():
    result = build_source_evidence([{
        "source_id": WIKIMEDIA_SOURCE_ID,
        "status": "SUCCESS",
        "data_mode": "LIVE",
        "coverage": {"fetched_at": "2026-11-26T12:00:00Z"},
        "records": [{
            "article_display": "Portable Solar Generator",
            "project": "en.wikipedia",
            "observation_date": "2026-11-25",
        }],
    }])

    assert result == {"rows": [], "untracked_record_count": 1}


def test_naive_collection_timestamp_is_not_accepted():
    result = build_source_evidence([{
        "source_id": WIKIMEDIA_SOURCE_ID,
        "status": "SUCCESS",
        "data_mode": "LIVE",
        "records": [{
            "article_display": "Portable Solar Generator",
            "project": "en.wikipedia",
            "observation_date": "2026-11-25",
            "fetched_at": "2026-11-26T12:00:00",
        }],
    }])

    assert result["rows"] == []
    assert result["untracked_record_count"] == 1


def test_distinct_queries_keep_distinct_lineage_and_are_idempotent():
    first = {
        "fetched_at": "2026-11-26T12:00:00Z",
        "provider_id": "serpapi_google",
        "query": "portable solar generator",
        "market": "US",
        "language": "en",
        "results": [{"title": "Example", "rank": 1}],
    }
    second = {**first, "query": "portable power station"}
    source = {
        "source_id": BING_SOURCE_ID,
        "status": "SUCCESS",
        "data_mode": "LIVE",
        "records": [
            {"bing_verification": first, "candidate_keyword_id": "candidate-one"},
            {"bing_verification": second, "candidate_keyword_id": "candidate-one"},
        ],
    }

    result = build_source_evidence([source])
    replay = build_source_evidence([source])

    assert result["untracked_record_count"] == 0
    assert [row["id"] for row in result["rows"]] == [row["id"] for row in replay["rows"]]
    assert len(result["rows"]) == 2
    assert {row["query_scope"]["query"] for row in result["rows"]} == {
        "portable solar generator", "portable power station",
    }
    assert all(row["keyword_ids"] == ("candidate-one",) for row in result["rows"])
    assert all(row["geography"]["meaning"] == "provider_market_localization"
               for row in result["rows"])


def test_bing_history_and_current_record_deduplicate_by_evidence_identity():
    verification = {
        "status": "VERIFIED",
        "provider_id": "serpapi_bing",
        "verification_mode": "BING_SERP",
        "query": "portable solar generator",
        "market": "US",
        "language": "en-US",
        "fetched_at": "2026-11-26T12:00:00Z",
        "results": [],
    }
    google_record = {
        "data_source": "GOOGLE_TRENDS",
        "canonical_keyword": "portable solar generator",
        "canonical_keyword_identity": hashlib.sha256(
            b"portable solar generator"
        ).hexdigest(),
        "provider_id": "serpapi_google_trends_trending_now",
        "transport_id": "serpapi",
        "discovery_mode": "SERPAPI_TRENDING_NOW",
        "region": "US",
        "language": "en",
        "time_window_hours": 24,
        "fetched_at": "2026-11-26T12:00:00Z",
        "delivery_state": "FRESH",
        "source_url": "https://trends.google.com/trending?geo=US",
        "quality_flags": [],
        "trend_clusters": [{"started_at": "2026-11-26T10:00:00Z"}],
        "bing_verification": verification,
        "bing_verification_history": [verification],
    }
    result = build_source_evidence([{
        "source_id": "google_web_serp",
        "status": "SUCCESS",
        "data_mode": "LIVE",
        "records": [google_record],
    }])

    bing_rows = [row for row in result["rows"] if row["source_id"] == BING_SOURCE_ID]
    google_rows = [row for row in result["rows"] if row["source_id"] == "google_web_serp"]
    assert len(google_rows) == 1
    assert len(bing_rows) == 1
    expected_keyword = keyword_record(google_record, "google_web_serp", "LIVE")
    assert bing_rows[0]["keyword_ids"] == (expected_keyword["id"],)
    assert bing_rows[0]["query_scope"]["evidence_kind"] == "search_verification"
    assert bing_rows[0]["payload"]["retention_lineage"]["keyword_fields"][0]["path"] == [
        "bing_verification"
    ]
    assert google_rows[0]["payload"]["retention_lineage"]["keyword_fields"]
    assert all(entry["path"][0] not in {
        "snapshot_status", "metric_definition", "bing_verification_history",
    } for entry in google_rows[0]["payload"]["retention_lineage"]["keyword_fields"])


def test_collection_service_marks_missing_postgres_lineage_in_snapshot_quality():
    class SnapshotStore:
        data_mode_scope = None
        supports_source_evidence_lineage = True

        def persist_dataset_snapshot(self, dataset_id, **snapshot):
            self.saved = (dataset_id, snapshot)

    store = SnapshotStore()
    service = CollectionService(store)
    service._persist_dataset_snapshot(
        "lineage-quality", [{
            "source_id": WIKIMEDIA_SOURCE_ID,
            "status": "SUCCESS",
            "data_mode": "LIVE",
            "records": [{
                "article_display": "Portable Solar Generator",
                "project": "en.wikipedia",
                "observation_date": "2026-11-25",
            }],
        }], [], available=1, skipped=0,
    )

    assert store.saved[0] == "lineage-quality"
    assert store.saved[1]["status"] == "PARTIAL"
    assert "SOURCE_EVIDENCE_LINEAGE_INCOMPLETE" in store.saved[1]["quality_flags"]
    assert store.saved[1]["source_evidence_rows"] == []


def test_wikimedia_entity_context_is_indexed_as_provider_derived_lineage():
    captured_at = "2026-11-26T12:00:00+00:00"
    row = (
        "wikimedia-keyword-id", "Example article", 42, "Q123", "Provider description",
        0, 0, "", "en", 0, 1, "person", json.dumps(["Q33999"]),
        json.dumps(["celebrity_public_figures.performers"]), json.dumps(["Q5"]),
        "https://en.wikipedia.org/wiki/Example_article", "WIKIPEDIA_PAGE_ID_AND_WIKIDATA_CLAIMS",
        "CLASSIFIED", "wikimedia-entity-context-v4", captured_at,
    )

    result = build_source_evidence([], entity_context_rows=[row])
    replay = build_source_evidence([], entity_context_rows=[row])

    assert result["untracked_record_count"] == 0
    assert len(result["rows"]) == 1
    evidence = result["rows"][0]
    assert evidence["source_id"] == WIKIMEDIA_CONTEXT_SOURCE_ID
    assert evidence["source_display_name"] == "Wikimedia entity context"
    assert evidence["keyword_ids"] == ("wikimedia-keyword-id",)
    assert evidence["collected_at"] == datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
    assert evidence["expires_at"] == effective_retention_deadline(evidence["collected_at"])
    context_lineage = evidence["payload"]["retention_lineage"]["entity_context_rows"]
    assert evidence["payload"]["retention_lineage"]["keyword_fields"] == []
    assert context_lineage[0]["keyword_id"] == "wikimedia-keyword-id"
    assert context_lineage[0]["context_version"] == "wikimedia-entity-context-v4"
    assert context_lineage[0]["collected_at"] == captured_at
    assert len(context_lineage[0]["value_sha256"]) == 64
    assert "Provider description" not in json.dumps(evidence["payload"])
    assert evidence["id"] == replay["rows"][0]["id"]
    assert evidence["query_scope"]["entity_id"] == "Q123"


def test_source_policy_registry_covers_catalog_and_keeps_the_stricter_deadline():
    catalog_source_ids = {item.source_id for item in SOURCE_CAPABILITIES}
    assert catalog_source_ids == {
        WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID,
    }
    for source_id in (*catalog_source_ids, WIKIMEDIA_CONTEXT_SOURCE_ID):
        assert source_retention_policy(source_id).source_id == source_id

    collected = datetime(2026, 11, 26, 4, 0, tzinfo=timezone.utc)
    youtube = source_retention_policy(YOUTUBE_SOURCE_ID)
    assert youtube.provider_max_age_days == 30
    assert effective_source_retention_deadline(YOUTUBE_SOURCE_ID, collected) == (
        effective_retention_deadline(collected)
    )
    assert source_retention_policy(GOOGLE_SOURCE_ID).review_status == "partial"
    assert source_retention_policy(WIKIMEDIA_CONTEXT_SOURCE_ID).review_status == "partial"
    with pytest.raises(ValueError, match="not registered"):
        source_retention_policy("unreviewed-provider")


def test_postgres_snapshot_persists_lineage_and_relationships_idempotently():
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
            store = PostgresCollectionStore(environment, schema=schema)
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
            keyword = keyword_record(raw, WIKIMEDIA_SOURCE_ID, "LIVE")
            keyword_table = sql.Identifier(schema, "keywords")
            connection.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                observed_at,data_mode,evidence,context) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""")
                .format(keyword_table), tuple(keyword[key] for key in (
                    "id", "keyword", "normalized", "source", "project", "observed_at",
                    "data_mode", "evidence", "context",
                )))
            context_row = (
                keyword["id"], "Portable Solar Generator", 42, "Q123", "Provider description",
                0, 0, "", "en", 0, 1, "person", json.dumps(["Q33999"]),
                json.dumps(["celebrity_public_figures.performers"]), json.dumps(["Q5"]),
                "https://en.wikipedia.org/wiki/Portable_Solar_Generator",
                "WIKIPEDIA_PAGE_ID_AND_WIKIDATA_CLAIMS", "CLASSIFIED",
                "wikimedia-entity-context-v4", "2026-11-26T12:01:00+00:00",
            )
            store.upsert_entity_context([context_row])
            evidence_rows = build_source_evidence([{
                "source_id": WIKIMEDIA_SOURCE_ID,
                "status": "SUCCESS",
                "data_mode": "LIVE",
                "records": [raw],
            }], entity_context_rows=[context_row])["rows"]
            snapshot = {
                "status": "COMPLETED", "data_mode": "LIVE", "source_manifest": [],
                "provider_record_count": 1, "received_record_count": 1,
                "retained_keyword_count": 1, "rejected_record_count": 0,
                "observation_start": "2026-11-25", "observation_end": "2026-11-25",
                "fetched_at": "2026-11-26T12:00:00+00:00", "quality_flags": [],
                "processing_status": "PROCESSING",
                "created_at": "2026-11-26T12:01:00+00:00",
                "source_evidence_rows": evidence_rows,
            }
            store.persist_dataset_snapshot("lineage-fetch", **snapshot)
            store.persist_dataset_snapshot("lineage-fetch", **snapshot)
            extended_rows = [dict(row) for row in evidence_rows]
            extended_rows[0]["expires_at"] += timedelta(days=2)
            store.persist_dataset_snapshot(
                "lineage-fetch", **{**snapshot, "source_evidence_rows": extended_rows}
            )

            source_evidence_table = sql.Identifier(schema, "source_evidence")
            dataset_evidence_table = sql.Identifier(schema, "dataset_evidence")
            evidence_keywords_table = sql.Identifier(schema, "evidence_keywords")
            saved = connection.execute(sql.SQL("""SELECT payload,content_digest,expires_at
                FROM {} WHERE id=%s""").format(source_evidence_table),
                (evidence_rows[0]["id"],)).fetchone()
            assert saved[0] == evidence_rows[0]["payload"]
            assert saved[1] == evidence_rows[0]["content_digest"]
            assert saved[2] == evidence_rows[0]["expires_at"]
            saved_policy = connection.execute(sql.SQL("""SELECT configuration
                FROM {} WHERE id=%s""").format(sql.Identifier(schema, "sources")),
                (evidence_rows[0]["source_id"],)).fetchone()[0]["retention_policy"]
            assert saved_policy["version"] == SOURCE_RETENTION_POLICY_VERSION
            assert saved_policy["provider_max_age_days"] is None
            wikimedia_row = next(row for row in evidence_rows
                                 if row["source_id"] == WIKIMEDIA_SOURCE_ID)
            saved_wikimedia = connection.execute(sql.SQL("""SELECT payload
                FROM {} WHERE id=%s""").format(source_evidence_table),
                (wikimedia_row["id"],)).fetchone()[0]
            assert saved_wikimedia["retention_lineage"]["version"] == 1
            assert saved_wikimedia["retention_lineage"]["keyword_fields"]
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE dataset_id=%s")
                .format(dataset_evidence_table), ("lineage-fetch",)).fetchone()[0] == 2
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE evidence_id=%s")
                .format(evidence_keywords_table), (evidence_rows[0]["id"],)).fetchone()[0] == 1
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
