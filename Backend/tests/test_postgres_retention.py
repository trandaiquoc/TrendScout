"""Rolling retention cleanup against a disposable PostgreSQL schema."""

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
from psycopg import sql

from app.ai.runtime import STATE_KEY as AI_STATE_KEY
from app.collections.postgres_store import PostgresCollectionStore
from app.collections.source_lineage import build_source_evidence
from app.collections.store import keyword_record
from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from app.storage.postgres_atomic_state import PostgresAtomicStateStore
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations
from app.storage.postgres_retention import PostgresRetentionStore


def test_postgres_retention_is_bounded_audited_and_preserves_unclassified_evidence():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    created = False
    now = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
    expired_at = now - timedelta(hours=1)
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            apply_migrations(connection, schema=schema)
            created = True
            names = sql.Identifier(schema)
            connection.execute(sql.SQL("""INSERT INTO {}.sources(id,display_name)
                VALUES ('fixture-source','Synthetic fixture')""").format(names))
            connection.execute(sql.SQL("""INSERT INTO {}.source_evidence(
                id,source_id,content_digest,query_scope,collected_at,payload)
                VALUES ('old-evidence','fixture-source','synthetic-digest','{{}}'::jsonb,
                        %s,'{{"title":"synthetic provider text"}}'::jsonb)""").format(names),
                (now - timedelta(days=10),),
            )
            old_timestamp = now - timedelta(days=100)
            connection.execute(sql.SQL("""INSERT INTO {}.collections(
                id,name,created_by,created_at,updated_at)
                VALUES ('saved-collection','Saved fixture','USER',%s,%s)""").format(names),
                (old_timestamp, old_timestamp),
            )
            connection.execute(sql.SQL("""INSERT INTO {}.keywords(
                id,keyword,normalized,source,observed_at,data_mode,evidence)
                VALUES ('saved-keyword','Fixture','fixture','fixture-source','2026-08-01',
                        'LIVE',%s::jsonb)""").format(names),
                ('{"fetched_at":"2026-08-01T00:00:00Z","views_ceil":123}',),
            )
            connection.execute(sql.SQL("""INSERT INTO {}.memberships(
                collection_id,keyword_id,origin,created_at)
                VALUES ('saved-collection','saved-keyword','USER',%s)""").format(names),
                (old_timestamp,),
            )
            connection.execute(sql.SQL("""INSERT INTO {}.research_records(
                id,subject,hypothesis,status,created_at,updated_at)
                VALUES ('saved-research','Fixture research','Keep this user record','ARCHIVED',%s,%s)""").format(names),
                (old_timestamp, old_timestamp),
            )
            connection.execute(sql.SQL("INSERT INTO {}.settings(key,value) VALUES ('fixture','keep')")
                               .format(names))
            connection.execute(sql.SQL("""INSERT INTO {}.coordination_fields(
                scope,field,is_mapping,value) VALUES
                ('fixture-scope','quota',TRUE,NULL),
                ('fixture-scope','reservations',TRUE,NULL)""").format(names))
            connection.execute(sql.SQL("""INSERT INTO {}.coordination_entries(
                scope,field,item_key,value) VALUES
                ('fixture-scope','quota','provider-month','{{"used":7,"limit":9}}'::jsonb),
                ('fixture-scope','reservations','fetch-1','{{"reserved":2}}'::jsonb)""").format(names))
            for digest, reference_expiry in (
                ("a" * 64, None),
                ("b" * 64, expired_at),
                ("c" * 64, now + timedelta(hours=1)),
            ):
                connection.execute(sql.SQL("""INSERT INTO {}.source_payload_blobs(
                    digest,raw_data,raw_bytes,item_count,retain_until)
                    VALUES (%s,%s,2,0,%s)""").format(names), (digest, b"[]", expired_at))
                if reference_expiry is not None:
                    connection.execute(sql.SQL("""INSERT INTO {}.source_payload_references(
                        scope,reference_path,digest,retain_until)
                        VALUES (%s,'payload',%s,%s)""").format(names),
                        ("fixture-" + digest[0], digest, reference_expiry),
                    )

            retention = PostgresRetentionStore(environment, schema=schema)
            preview = retention.sweep(now=now, dry_run=True, limit=1)
            assert preview["status"] == "DRY_RUN"
            assert preview["payload_candidate_count"] == 1
            assert preview["payload_deleted_count"] == 0
            assert preview["payload_has_more"] is True
            assert preview["payload_blocked_count"] == 1
            assert preview["source_evidence_due_count"] == 1
            assert preview["source_evidence_deleted_count"] == 0

            first = retention.sweep(now=now, limit=1)
            second = retention.sweep(now=now, limit=1)
            third = retention.sweep(now=now, limit=1)
            assert first["payload_deleted_count"] == 1
            assert first["payload_has_more"] is True
            assert second["payload_deleted_count"] == 1
            assert third["status"] == "SKIPPED_RECENT_SWEEP"
            assert third["payload_deleted_count"] == 0
            assert third["payload_has_more"] is False
            later = retention.sweep(now=now + timedelta(hours=1))
            assert later["status"] == "COMPLETED"
            assert later["payload_candidate_count"] == 1
            assert later["payload_deleted_count"] == 1

            assert connection.execute(sql.SQL("SELECT count(*) FROM {}.source_payload_blobs")
                                      .format(names)).fetchone()[0] == 0
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}.source_payload_references")
                                      .format(names)).fetchone()[0] == 0
            # An active reference protects its payload; once that reference's
            # retention expires, both are removed. Legacy source evidence is
            # reported for later lineage reconciliation instead of deleted.
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}.source_evidence")
                                      .format(names)).fetchone()[0] == 1
            assert connection.execute(sql.SQL("SELECT created_at FROM {}.collections WHERE id='saved-collection'")
                                      .format(names)).fetchone()[0] == old_timestamp
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}.memberships WHERE collection_id='saved-collection'")
                                      .format(names)).fetchone()[0] == 1
            saved_evidence = connection.execute(sql.SQL("SELECT evidence->>'views_ceil' FROM {}.keywords WHERE id='saved-keyword'")
                                                 .format(names)).fetchone()[0]
            assert saved_evidence == "123"
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}.research_records WHERE id='saved-research'")
                                      .format(names)).fetchone()[0] == 1
            assert connection.execute(sql.SQL("SELECT value FROM {}.settings WHERE key='fixture'")
                                      .format(names)).fetchone()[0] == "keep"
            assert connection.execute(sql.SQL("SELECT value->>'used' FROM {}.coordination_entries WHERE field='quota'")
                                      .format(names)).fetchone()[0] == "7"
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}.coordination_entries WHERE field='reservations'")
                                      .format(names)).fetchone()[0] == 1
            audit = connection.execute(sql.SQL("""SELECT count(*),
                sum(payload_deleted_count),sum(source_evidence_deleted_count),
                bool_or(dry_run),bool_or(source_evidence_due_count=1)
                FROM {}.retention_sweep_runs""").format(names)).fetchone()
            assert audit == (4, 3, 0, True, True)
            preserved = PostgresRetentionStore({}, schema=schema, preserve_expired=True)
            assert preserved.sweep(now=now)["status"] == "PRESERVED"
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_retention_scrubs_lineage_and_preserves_user_state_at_date_boundary():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    created = False
    actual_now = datetime.now(timezone.utc).replace(microsecond=0)
    sweep_now = actual_now + timedelta(days=2)
    from app.retention.policy import rolling_retention_window

    window = rolling_retention_window(sweep_now)
    expired_collected = window.cutoff_at - timedelta(microseconds=1)
    retained_collected = window.cutoff_at
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            apply_migrations(connection, schema=schema)
            created = True
            store = PostgresCollectionStore(environment, schema=schema)
            state_store = PostgresAtomicStateStore(environment, schema=schema)

            expired_record = {
                "article_display": "Expired Provider Keyword",
                "article_original": "Expired_Provider_Keyword",
                "project": "en.wikipedia",
                "observation_date": (expired_collected.date()).isoformat(),
                "fetched_at": expired_collected.isoformat(),
                "provider_rank": 1,
                "views_ceil": 8800,
                "country_scope": "US",
                "access": "all-access",
                "source_url": "https://en.wikipedia.org/wiki/Expired_Provider_Keyword",
            }
            retained_record = {
                **expired_record,
                "article_display": "Retained Provider Keyword",
                "article_original": "Retained_Provider_Keyword",
                "observation_date": sweep_now.date().isoformat(),
                "fetched_at": retained_collected.isoformat(),
                "views_ceil": 9400,
                "source_url": "https://en.wikipedia.org/wiki/Retained_Provider_Keyword",
            }
            expired_keyword = keyword_record(expired_record, WIKIMEDIA_SOURCE_ID, "LIVE")
            retained_keyword = keyword_record(retained_record, WIKIMEDIA_SOURCE_ID, "LIVE")
            keywords = sql.Identifier(schema, "keywords")
            for item in (expired_keyword, retained_keyword):
                connection.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                    observed_at,data_mode,evidence,context) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""")
                    .format(keywords), tuple(item[key] for key in (
                        "id", "keyword", "normalized", "source", "project", "observed_at",
                        "data_mode", "evidence", "context",
                    )))

            expired_evidence = build_source_evidence([{
                "source_id": WIKIMEDIA_SOURCE_ID, "status": "SUCCESS", "data_mode": "LIVE",
                "records": [expired_record],
            }])["rows"]
            retained_evidence = build_source_evidence([{
                "source_id": WIKIMEDIA_SOURCE_ID, "status": "SUCCESS", "data_mode": "LIVE",
                "records": [retained_record],
            }])["rows"]

            def persist_dataset(dataset_id, record, evidence_rows):
                return store.persist_dataset_snapshot(
                    dataset_id, status="COMPLETED", data_mode="LIVE",
                    source_manifest=[{"source_id": WIKIMEDIA_SOURCE_ID,
                                      "record_count": 1, "received_record_count": 1}],
                    provider_record_count=1, received_record_count=1,
                    retained_keyword_count=1, rejected_record_count=0,
                    observation_start=record["observation_date"],
                    observation_end=record["observation_date"],
                    fetched_at=record["fetched_at"], quality_flags=[],
                    processing_status="COMPLETED", created_at=record["fetched_at"],
                    source_evidence_rows=evidence_rows,
                )

            persist_dataset("expired-fetch", expired_record, expired_evidence)
            persist_dataset("retained-fetch", retained_record, retained_evidence)
            store.persist_dataset_snapshot(
                "mixed-fetch", status="COMPLETED", data_mode="LIVE",
                source_manifest=[{"source_id": WIKIMEDIA_SOURCE_ID,
                                  "record_count": 2, "received_record_count": 2}],
                provider_record_count=2, received_record_count=2,
                retained_keyword_count=2, rejected_record_count=0,
                observation_start=expired_record["observation_date"],
                observation_end=retained_record["observation_date"],
                fetched_at=retained_record["fetched_at"], quality_flags=[],
                processing_status="COMPLETED", created_at=retained_record["fetched_at"],
                source_evidence_rows=[*expired_evidence, *retained_evidence],
            )
            expired_id = expired_evidence[0]["id"]

            context_stamp = expired_collected - timedelta(hours=1)
            context_row = (
                expired_keyword["id"], "Expired Provider Keyword", 44, "Q4455",
                "Provider entity description", 0, 0, "", "en", 0, 1, "concept",
                json.dumps(["Q35120"]), json.dumps(["Q35120"]), json.dumps(["Q35120"]),
                "https://en.wikipedia.org/wiki/Expired_Provider_Keyword",
                "WIKIPEDIA_PAGE_ID_AND_WIKIDATA_CLAIMS", "CLASSIFIED",
                "wikimedia-entity-context-v4", context_stamp.isoformat(),
            )
            store.upsert_entity_context([context_row])

            old_stamp = actual_now - timedelta(days=90)
            connection.execute(sql.SQL("""INSERT INTO {}(id,name,created_by,created_at,updated_at)
                VALUES ('saved-collection','Saved by user','USER',%s,%s)""").format(
                    sql.Identifier(schema, "collections")), (old_stamp, old_stamp))
            connection.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,origin,pinned,created_at)
                VALUES ('saved-collection',%s,'USER',1,%s)""").format(
                    sql.Identifier(schema, "memberships")), (expired_keyword["id"], old_stamp))
            connection.execute(sql.SQL("""INSERT INTO {}(id,keyword_id,subject,hypothesis,status,
                created_at,updated_at) VALUES ('saved-research',%s,'User research','Keep this','ARCHIVED',%s,%s)""")
                .format(sql.Identifier(schema, "research_records")),
                (expired_keyword["id"], old_stamp, old_stamp))
            connection.execute(sql.SQL("INSERT INTO {}(key,value) VALUES ('saved-setting','keep')")
                               .format(sql.Identifier(schema, "settings")))

            connection.execute(sql.SQL("""INSERT INTO {}(keyword_id,source,observation_date,country,
                access,project,metric,unit,status,value,rank,snapshot_status,input_digest)
                VALUES (%s,%s,%s,'US','all-access','en.wikipedia','views','views','OBSERVED',8800,1,
                'SUCCESS','old-input')""").format(sql.Identifier(schema, "algorithm_observations")),
                (expired_keyword["id"], WIKIMEDIA_SOURCE_ID, expired_record["observation_date"]))
            connection.execute(sql.SQL("""INSERT INTO {}(keyword_id,algorithm_id,version,status,
                value,metric,unit,result_json,input_digest,updated_at)
                VALUES (%s,'trend-score','v1','COMPLETED',0.8,'score','points','{{}}','old-input',%s)""")
                .format(sql.Identifier(schema, "algorithm_results")),
                (expired_keyword["id"], actual_now))
            connection.execute(sql.SQL("""INSERT INTO {}(keyword_id,entity_type,entity_description,
                classification_status,confidence_level,classification_method,classification_version,
                taxonomy_version,review_status,classified_at)
                VALUES (%s,'concept','provider description','CLASSIFIED','HIGH','rules','v1','v1','NEW',%s)""")
                .format(sql.Identifier(schema, "classification_results")),
                (expired_keyword["id"], actual_now))
            store.upsert_embedding_cache(
                [("expired-vector", "test-model", 0, "expired-text-hash", b"vector", 1)],
                evidence_ids_by_fingerprint={"expired-vector": [expired_id]},
            )

            def seed_ai_state(state):
                state.setdefault("jobs", {})
                state.setdefault("cache", {})
                state.setdefault("recommendations", {})
                state["attempts"] = [{"id": "quota-preserved", "epoch": actual_now.timestamp()}]
                state["jobs"]["ai-job"] = {
                    "status": "COMPLETED", "result": {"provider_text": "expired"},
                    "result_expires_at": expired_evidence[0]["expires_at"].isoformat(),
                    "lease_token": None, "lease_until": None,
                }
                state["cache"]["expired-ai-cache"] = {
                    "value": {"provider_text": "expired"},
                    "expires_at": expired_evidence[0]["expires_at"].isoformat(),
                }
                state["recommendations"]["expired-recommendation"] = {
                    "value": {"provider_text": "expired"},
                    "expires_at": expired_evidence[0]["expires_at"].isoformat(),
                }
                return state, None

            state_store.transact(AI_STATE_KEY, seed_ai_state)

            summary = PostgresRetentionStore(environment, schema=schema).sweep(
                now=sweep_now, limit=10,
            )
            assert summary["business_date"] == sweep_now.astimezone(
                ZoneInfo("Asia/Ho_Chi_Minh")
            ).date().isoformat()
            assert summary["source_evidence_deleted_count"] == 2
            assert summary["source_evidence_blocked_count"] == 0
            assert summary["dependent_cleanup_counts"]["keyword_observations"] == 1
            assert summary["dependent_cleanup_counts"]["algorithm_results"] == 1
            assert summary["dependent_cleanup_counts"]["classifications"] == 1
            assert summary["dependent_cleanup_counts"]["entity_context"] == 1
            assert summary["dependent_cleanup_counts"]["embedding_cache"] == 1
            assert summary["dependent_cleanup_counts"]["ai_cache"] == 1
            assert summary["dependent_cleanup_counts"]["ai_recommendations"] == 1
            assert summary["dependent_cleanup_counts"]["ai_job_results"] == 1
            assert summary["dependent_cleanup_counts"]["ai_content_untracked"] == 0

            saved_keyword = connection.execute(sql.SQL("""SELECT keyword,normalized,observed_at,
                context,evidence,eligible FROM {} WHERE id=%s""").format(keywords),
                (expired_keyword["id"],)).fetchone()
            assert saved_keyword == ("", "", "", "", {}, 0)
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE keyword_id=%s AND pinned=1")
                .format(sql.Identifier(schema, "memberships")), (expired_keyword["id"],)).fetchone()[0] == 1
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE id='saved-research'")
                .format(sql.Identifier(schema, "research_records"))).fetchone()[0] == 1
            assert connection.execute(sql.SQL("SELECT value FROM {} WHERE key='saved-setting'")
                .format(sql.Identifier(schema, "settings"))).fetchone()[0] == "keep"
            assert connection.execute(sql.SQL("SELECT keyword,evidence FROM {} WHERE id=%s")
                .format(keywords), (retained_keyword["id"],)).fetchone() == (
                    "Retained Provider Keyword", json.loads(retained_keyword["evidence"]),
                )
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE id=%s")
                .format(sql.Identifier(schema, "source_evidence")),
                (retained_evidence[0]["id"],)).fetchone()[0] == 1
            expired_snapshot = connection.execute(sql.SQL("""SELECT source_manifest,
                provider_record_count,received_record_count,retained_keyword_count,
                observation_start,observation_end,fetched_at,quality_flags
                FROM {} WHERE dataset_id='expired-fetch'""").format(
                    sql.Identifier(schema, "dataset_snapshots"))).fetchone()
            assert expired_snapshot[:7] == ([], 0, 0, 0, None, None, None)
            assert "RETENTION_PRUNED_SOURCE_EVIDENCE" in expired_snapshot[7]
            assert len(connection.execute(sql.SQL(
                "SELECT source_manifest FROM {} WHERE dataset_id='mixed-fetch'"
            ).format(sql.Identifier(schema, "dataset_snapshots"))).fetchone()[0]) == 1
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE fingerprint='expired-vector'")
                .format(sql.Identifier(schema, "cache"))).fetchone()[0] == 0
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE keyword_id=%s")
                .format(sql.Identifier(schema, "algorithm_observations")),
                (expired_keyword["id"],)).fetchone()[0] == 0
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE keyword_id=%s")
                .format(sql.Identifier(schema, "algorithm_results")),
                (expired_keyword["id"],)).fetchone()[0] == 0
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE keyword_id=%s")
                .format(sql.Identifier(schema, "classification_results")),
                (expired_keyword["id"],)).fetchone()[0] == 0
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE keyword_id=%s")
                .format(sql.Identifier(schema, "entity_context")),
                (expired_keyword["id"],)).fetchone()[0] == 0
            ai_state = state_store.read_many([AI_STATE_KEY])[AI_STATE_KEY]
            assert ai_state["attempts"] == [{"id": "quota-preserved", "epoch": actual_now.timestamp()}]
            assert ai_state["jobs"]["ai-job"]["result"] is None
            assert ai_state["cache"] == {}
            assert ai_state["recommendations"] == {}
            assert PostgresRetentionStore(environment, schema=schema).sweep(
                now=sweep_now, limit=10,
            )["status"] == "SKIPPED_RECENT_SWEEP"
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_retention_drains_backlog_and_serializes_fetch_with_cleanup():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    created = False
    now = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=2)
    from app.retention.policy import rolling_retention_window

    cutoff = rolling_retention_window(now).cutoff_at
    old_collected = cutoff - timedelta(days=2)
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            apply_migrations(connection, schema=schema)
            created = True
            store = PostgresCollectionStore(environment, schema=schema)
            connection.execute(sql.SQL("""INSERT INTO {}(id,display_name) VALUES
                ('fixture-source','Fixture source')""").format(sql.Identifier(schema, "sources")))
            for index in range(3):
                connection.execute(sql.SQL("""INSERT INTO {}(id,source_id,content_digest,query_scope,
                    collected_at,payload,expires_at) VALUES (%s,'fixture-source',%s,'{{}}',%s,NULL,%s)""")
                    .format(sql.Identifier(schema, "source_evidence")),
                    (f"backlog-{index}", str(index) * 64, old_collected, old_collected + timedelta(days=1)))
            retention = PostgresRetentionStore(environment, schema=schema)
            first = retention.sweep(now=now, limit=1)
            second = retention.sweep(now=now, limit=1)
            third = retention.sweep(now=now, limit=1)
            assert first["source_evidence_deleted_count"] == 1 and first["has_more"] is True
            assert second["source_evidence_deleted_count"] == 1 and second["has_more"] is True
            assert third["source_evidence_deleted_count"] == 1 and third["has_more"] is False

            record = {
                "article_display": "Concurrent New Keyword",
                "article_original": "Concurrent_New_Keyword",
                "project": "en.wikipedia",
                "observation_date": now.date().isoformat(),
                "fetched_at": now.isoformat(),
                "provider_rank": 1,
                "views_ceil": 500,
                "country_scope": "US",
                "access": "all-access",
                "source_url": "https://en.wikipedia.org/wiki/Concurrent_New_Keyword",
            }
            old_record = {
                **record,
                "observation_date": (old_collected.date()).isoformat(),
                "fetched_at": old_collected.isoformat(),
                "views_ceil": 100,
            }
            old_keyword = keyword_record(old_record, WIKIMEDIA_SOURCE_ID, "LIVE")
            keyword = keyword_record(record, WIKIMEDIA_SOURCE_ID, "LIVE")
            old_evidence_rows = build_source_evidence([{
                "source_id": WIKIMEDIA_SOURCE_ID, "status": "SUCCESS", "data_mode": "LIVE",
                "records": [old_record],
            }])["rows"]
            evidence_rows = build_source_evidence([{
                "source_id": WIKIMEDIA_SOURCE_ID, "status": "SUCCESS", "data_mode": "LIVE",
                "records": [record],
            }])["rows"]
            def seed_old_provider_row(db):
                db.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                    observed_at,data_mode,evidence,context) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""")
                    .format(sql.Identifier(schema, "keywords")), tuple(old_keyword[key] for key in (
                        "id", "keyword", "normalized", "source", "project", "observed_at",
                        "data_mode", "evidence", "context",
                    )))
                store._persist_source_evidence(db, None, old_evidence_rows)

            store._write(seed_old_provider_row)
            barrier = Barrier(2)

            def fetch_write():
                def operation(db):
                    db.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                        observed_at,data_mode,evidence,context) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(id) DO UPDATE SET keyword=EXCLUDED.keyword,
                        normalized=EXCLUDED.normalized,observed_at=EXCLUDED.observed_at,
                        evidence=EXCLUDED.evidence,context=EXCLUDED.context""").format(
                            sql.Identifier(schema, "keywords")), tuple(keyword[key] for key in (
                                "id", "keyword", "normalized", "source", "project", "observed_at",
                                "data_mode", "evidence", "context",
                            )))
                    store._persist_source_evidence(db, None, evidence_rows)
                barrier.wait()
                store._write(operation)

            def cleanup_write():
                barrier.wait()
                retention.sweep(now=now + timedelta(hours=1), limit=1)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = [executor.submit(fetch_write), executor.submit(cleanup_write)]
                for result in results:
                    result.result(timeout=30)
            retention.sweep(now=now + timedelta(hours=2), limit=1)
            assert connection.execute(sql.SQL("SELECT keyword FROM {} WHERE id=%s")
                .format(sql.Identifier(schema, "keywords")), (keyword["id"],)).fetchone()[0] == (
                    "Concurrent New Keyword"
                )
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE id=%s AND expires_at>%s")
                .format(sql.Identifier(schema, "source_evidence")),
                (evidence_rows[0]["id"], now)).fetchone()[0] == 1
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE id=%s")
                .format(sql.Identifier(schema, "source_evidence")),
                (old_evidence_rows[0]["id"],)).fetchone()[0] == 0
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
