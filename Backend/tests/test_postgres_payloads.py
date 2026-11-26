"""Source-payload integrity and retention against isolated PostgreSQL."""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from psycopg import sql

from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.sources.catalog import GOOGLE_SOURCE_ID
from app.storage.postgres_atomic_state import PostgresAtomicStateStore
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations
from app.storage.postgres_payloads import PostgresSourcePayloads, _encode


def test_full_database_images_are_rejected():
    with pytest.raises(ValueError, match="database snapshots"):
        _encode([{"encoding": "sqlite-segment-gzip-base64-v1", "payload": "synthetic"}])


def test_payload_integrity_and_cache_retention_in_isolated_schema():
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
            payloads = PostgresSourcePayloads(environment, schema=schema)
            now = datetime.now(timezone.utc)
            expiry = now + timedelta(seconds=30)
            records = [{"value": 0, "title": "synthetic"}]
            reference = payloads.put(records, now=now, retain_until=expiry)
            assert payloads.get(reference, now=now) == records
            payloads.import_legacy(reference, records, now=now, retain_until=expiry)
            with pytest.raises(ValueError, match="retention conflicts"):
                payloads.import_legacy(reference, records, now=now, retain_until=expiry - timedelta(seconds=1))
            with pytest.raises(ValueError, match="expired"):
                payloads.get(reference, now=expiry)
            assert payloads.purge_expired(now=expiry - timedelta(seconds=1)) == 0
            assert payloads.get(reference, now=now) == records
            assert payloads.purge_expired(now=expiry, limit=1) == 1
            with pytest.raises(ValueError, match="unavailable, expired or mismatched"):
                payloads.get(reference, now=now)
            preserved = PostgresSourcePayloads({}, schema=schema, preserve_expired=True)
            assert preserved.purge_expired(now=expiry) == 0

            lifecycle_now = datetime.now(timezone.utc)
            checkpoint_expiry = lifecycle_now + timedelta(days=1)
            job_expiry = lifecycle_now + timedelta(hours=12)
            checkpoint_reference = payloads.put(
                [{"value": "checkpoint"}], now=lifecycle_now,
                retain_until=checkpoint_expiry,
            )
            store = PostgresAtomicStateStore(environment, schema=schema)

            def save_checkpoint(state):
                return ({"jobs": {"payload-job": {
                    "expires_at": job_expiry.isoformat(),
                    "source_checkpoints": {"youtube": {
                        "retain_until": checkpoint_expiry.isoformat(),
                        "snapshot_ref": {**checkpoint_reference,
                                         "path": ["result", "records"]},
                    }},
                }}}, None)

            store.transact("payload-lifecycle", save_checkpoint)
            stored_reference = connection.execute(sql.SQL("""SELECT retain_until
                FROM {}.source_payload_references WHERE scope=%s""").format(
                    sql.Identifier(schema)), ("payload-lifecycle",)).fetchone()
            assert stored_reference == (job_expiry,)
            connection.execute(sql.SQL("""UPDATE {}.source_payload_blobs
                SET retain_until=%s WHERE digest=%s""").format(sql.Identifier(schema)),
                (lifecycle_now - timedelta(seconds=1), checkpoint_reference["digest"]))
            assert payloads.delete_expired_unreferenced(
                {checkpoint_reference["digest"]}, now=lifecycle_now,
            ) == {"deleted": 0, "retained_referenced": 1}

            def remove_checkpoint(state):
                del state["jobs"]["payload-job"]["source_checkpoints"]["youtube"]
                return state, None

            store.transact("payload-lifecycle", remove_checkpoint)
            assert payloads.delete_expired_unreferenced(
                {checkpoint_reference["digest"]}, now=lifecycle_now,
            ) == {"deleted": 1, "retained_referenced": 0}

            cache_expiry = lifecycle_now + timedelta(minutes=5)
            cache_reference = payloads.put(
                [{"value": "cache"}], now=lifecycle_now, retain_until=cache_expiry,
            )
            store.transact("payload-cache", lambda _state: ({"cache": {
                "retain_until": cache_expiry.isoformat(),
                "payload_ref": cache_reference,
            }}, None))
            assert payloads.purge_expired(now=cache_expiry) == 1
            # Expired diagnostic state can outlive its blob; unrelated changes
            # must not fail while trying to re-index that stale reference.
            store.transact("payload-cache", lambda state: ({**state, "checked": True}, None))

            cache = SharedCache(PostgresAtomicStateStore(environment, schema=schema),
                                payload_blobs=payloads, max_inline_bytes=1)
            scope = CacheScope(GOOGLE_SOURCE_ID, "serpapi_google", "synthetic", "a" * 64,
                               "gl=us", "en", "7d", "v1", "policy-v1")
            cache_now = datetime.now(timezone.utc)
            grant = CacheGrant(GOOGLE_SOURCE_ID, "policy-v1", cache_now + timedelta(days=1),
                               max_fresh_seconds=10, max_stale_seconds=20,
                               max_retention_seconds=30, allowed=True)
            cache.put(scope, grant=grant, origin="SYNTHETIC", payload={"value": 0},
                      fetched_at=cache_now, now=cache_now)
            assert connection.execute(sql.SQL("""SELECT count(*)
                FROM {}.source_payload_references WHERE scope=%s""").format(
                    sql.Identifier(schema)), (scope.key,)).fetchone()[0] == 1
            assert cache.get(scope, grant=grant, now=cache_now + timedelta(seconds=9))["status"] == "CACHED"
            assert cache.get(scope, grant=grant, now=cache_now + timedelta(seconds=10))["status"] == "STALE"
            assert cache.get(scope, grant=grant, now=cache_now + timedelta(seconds=20))["status"] == "EXPIRED"
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
