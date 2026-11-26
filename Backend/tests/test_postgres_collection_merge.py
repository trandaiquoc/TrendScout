"""Compare synthetic merge outcomes with the existing SQLite implementation."""

import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import pytest
from psycopg import sql

from app.collections.postgres_store import PostgresCollectionStore
from app.collections.store import CollectionStore
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations
from app.storage.sqlite_to_postgres import import_backup


def test_merge_preserves_membership_and_feedback_rules(tmp_path):
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    path = Path(tmp_path) / "source.sqlite"
    legacy = CollectionStore(path)
    source = legacy.create({"name": "Source"})
    child = legacy.create({"name": "Child", "parent_id": source})
    target = legacy.create({"name": "Target"})
    timestamp = "2026-11-25T10:00:00+00:00"
    with legacy.connect() as db:
        for key in ("k1", "k2", "k3", "k4"):
            db.execute("""INSERT INTO keywords(id,keyword,normalized,source,observed_at,data_mode,evidence)
                VALUES(?,?,?,?,?,?,?)""", (key, key, key, "synthetic", "2026-11-25", "LIVE", "{}"))
        for collection, key, origin, pinned in ((source,"k1","USER",0),(source,"k2","LOCAL_AI",0),
                (child,"k3","USER",0),(child,"k4","USER",0),(target,"k1","LOCAL_AI",0),
                (target,"k2","LOCAL_AI",0),(target,"k3","USER",1)):
            db.execute("""INSERT INTO memberships(collection_id,keyword_id,origin,pinned,reason,created_at)
                VALUES(?,?,?,?,?,?)""", (collection, key, origin, pinned, "original", timestamp))
        db.execute("INSERT INTO feedback VALUES(?,?,?,?)", (target, "k4", "REMOVE", timestamp))
    backup = Path(tmp_path) / "backup.sqlite"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as old:
        with closing(sqlite3.connect(backup)) as copy:
            old.backup(copy)
            copy.execute("PRAGMA journal_mode=DELETE")
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
            import_backup(connection, backup, schema=schema)
            store = PostgresCollectionStore(environment, schema=schema)
            with pytest.raises(ValueError, match="COLLECTION_MERGE_TARGET_DESCENDANT"):
                store.merge(source, child)
            assert store.merge(source, target) == legacy.merge(source, target)
            with legacy.connect() as old:
                expected = [tuple(row) for row in old.execute("""SELECT collection_id,keyword_id,origin,pinned,
                    score,reason FROM memberships ORDER BY collection_id,keyword_id""").fetchall()]
                expected_feedback = [tuple(row) for row in old.execute(
                    "SELECT collection_id,keyword_id,action FROM feedback ORDER BY collection_id,keyword_id").fetchall()]
                expected_suppression = [tuple(row) for row in old.execute(
                    "SELECT fingerprint,kind,label FROM suppression ORDER BY fingerprint").fetchall()]
            actual = connection.execute(sql.SQL("""SELECT collection_id,keyword_id,origin,pinned,score,reason
                FROM {}.memberships ORDER BY collection_id,keyword_id""").format(sql.Identifier(schema))).fetchall()
            actual_feedback = connection.execute(sql.SQL("SELECT collection_id,keyword_id,action FROM {}.feedback "
                "ORDER BY collection_id,keyword_id").format(sql.Identifier(schema))).fetchall()
            actual_suppression = connection.execute(sql.SQL("SELECT fingerprint,kind,label FROM {}.suppression "
                "ORDER BY fingerprint").format(sql.Identifier(schema))).fetchall()
            assert actual == expected
            assert actual_feedback == expected_feedback
            assert actual_suppression == expected_suppression
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
