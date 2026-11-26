"""Rolling retention cleanup against a disposable PostgreSQL schema."""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from psycopg import sql

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
