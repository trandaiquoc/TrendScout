"""Legacy coordination staging with fake Firestore and isolated PostgreSQL."""

import hashlib
import json
import os
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from psycopg import sql

from app.storage.firestore_to_postgres import (
    LegacyCaptureError,
    capture_firestore_coordination,
    stage_firestore_coordination,
)
from app.storage.postgres_atomic_state import PostgresAtomicStateStore
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations
from app.storage.postgres_payloads import PostgresSourcePayloads

NOW = datetime.now(timezone.utc)


class Snapshot:
    def __init__(self, key, value):
        self.id, self.value, self.exists = key, value, value is not None

    def to_dict(self):
        return deepcopy(self.value)


class Collection:
    def __init__(self, values):
        self.values = values

    def stream(self, **_kwargs):
        return [Snapshot(key, value) for key, value in self.values.items()]

    def document(self, key):
        return type("Reference", (), {"get": lambda _self, **_kwargs:
                    Snapshot(key, self.values.get(key))})()


class Client:
    def __init__(self, documents, manifests):
        self.documents, self.manifests = documents, manifests

    def collection(self, name):
        return Collection(self.documents if name == "_ts_d5_coordination" else self.manifests)


class BlobReader:
    def __init__(self, values):
        self.values = values

    def get(self, reference, *, now):
        return deepcopy(self.values[reference["digest"]])


def fixture_client():
    records = [{"title": "synthetic", "zero": 0}]
    raw = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    ref = {"digest": digest, "raw_bytes": len(raw), "item_count": 1}
    expiry = NOW + timedelta(minutes=10)
    manifest = {"schema": 1, "digest": digest, "raw_bytes": len(raw), "item_count": 1,
                "chunk_count": 1, "retain_until": expiry.isoformat()}
    documents = {"collections-live-snapshot-v1": {"version": 1},
                 "live-state": {"quota": {"provider:day": {"requests": 3, "cost": 3}},
                                "jobs": {}, "inflight": {}, "payload_ref": ref}}
    return Client(documents, {digest: manifest}), BlobReader({digest: records}), ref, records, expiry


def test_capture_rejects_credentials_and_never_reads_expired_payload():
    client, reader, ref, _records, _expiry = fixture_client()
    client.manifests[ref["digest"]]["retain_until"] = (NOW - timedelta(seconds=1)).isoformat()
    captured = capture_firestore_coordination(client, now=NOW, blob_reader=reader)
    assert captured.expired_references == 1
    assert captured.payloads[0][2] is None
    client.documents["live-state"]["api_key"] = "synthetic-secret"
    with pytest.raises(LegacyCaptureError, match="credential-shaped"):
        capture_firestore_coordination(client, now=NOW, blob_reader=reader)


def test_capture_rejects_changed_source_and_active_leases_before_database_use():
    client, reader, ref, _records, expiry = fixture_client()
    captured = capture_firestore_coordination(client, now=NOW, blob_reader=reader)
    client.documents["live-state"]["quota"]["provider:day"]["requests"] = 4
    with pytest.raises(LegacyCaptureError, match="changed after capture"):
        stage_firestore_coordination(client, {}, captured, schema="test", now=NOW, clock=lambda: NOW)
    client.documents["live-state"]["inflight"] = {
        "attempt": {"expires_at": (NOW + timedelta(seconds=1)).isoformat()}}
    captured = capture_firestore_coordination(client, now=NOW, blob_reader=reader)
    with pytest.raises(LegacyCaptureError, match="active legacy reservation"):
        stage_firestore_coordination(client, {}, captured, schema="test", now=NOW, clock=lambda: NOW)
    client.documents["live-state"]["inflight"] = {}
    captured = capture_firestore_coordination(client, now=NOW, blob_reader=reader)
    client.manifests[ref["digest"]]["retain_until"] = (expiry + timedelta(seconds=1)).isoformat()
    with pytest.raises(LegacyCaptureError, match="manifest changed"):
        stage_firestore_coordination(client, {}, captured, schema="test", now=NOW, clock=lambda: NOW)


def test_stage_is_idempotent_and_preserves_quota_and_expiry_in_isolated_schema():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    client, reader, ref, records, expiry = fixture_client()
    captured = capture_firestore_coordination(client, now=NOW, blob_reader=reader)
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
            for _ in range(2):
                result = stage_firestore_coordination(client, environment, captured, schema=schema,
                                                      now=NOW, clock=lambda: NOW)
                assert result == {"documents": 2, "payloads": 1, "expired_payload_references": 0}
            store = PostgresAtomicStateStore(environment, schema=schema)
            assert store.read_many(["live-state"])["live-state"] == client.documents["live-state"]
            assert PostgresSourcePayloads(environment, schema=schema).get(ref, now=NOW) == records
            assert connection.execute(sql.SQL("SELECT retain_until FROM {}.source_payload_blobs")
                                      .format(sql.Identifier(schema))).fetchone()[0] == expiry
            assert connection.execute(sql.SQL("SELECT retain_until FROM {}.source_payload_references")
                                      .format(sql.Identifier(schema))).fetchone()[0] == expiry
            assert store.read_many(["collections-live-snapshot-v1"])["collections-live-snapshot-v1"] == {}
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}.legacy_firestore_imports")
                                      .format(sql.Identifier(schema))).fetchone()[0] == 2
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
