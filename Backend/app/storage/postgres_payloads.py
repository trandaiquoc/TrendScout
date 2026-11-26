"""Content-addressed source payloads for PostgreSQL cache/checkpoint adapters.

The stored bytes are individual source records encoded as JSON, never a
SQLite database snapshot. The PostgreSQL LIVE composition uses this adapter.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

import psycopg
from psycopg import sql

from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import _SCHEMA_NAME

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_MAX_BYTES = 5_700_000
_MAX_ITEMS = 5000


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(timezone.utc)


def _encode(records):
    if not isinstance(records, list) or len(records) > _MAX_ITEMS:
        raise ValueError("bounded source payload required")
    if any(isinstance(item, dict) and isinstance(item.get("encoding"), str)
           and item["encoding"].startswith("sqlite-") for item in records):
        raise ValueError("database snapshots are not source payloads")
    try:
        raw = json.dumps(records, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("source payload must be finite UTF-8 JSON") from None
    if len(raw) > _MAX_BYTES:
        raise ValueError("source payload exceeds size budget")
    return raw


def _valid_reference(reference):
    return (isinstance(reference, dict) and isinstance(reference.get("digest"), str)
            and _DIGEST.fullmatch(reference["digest"]) is not None
            and type(reference.get("raw_bytes")) is int
            and 0 <= reference["raw_bytes"] <= _MAX_BYTES
            and type(reference.get("item_count")) is int
            and 0 <= reference["item_count"] <= _MAX_ITEMS)


class PostgresSourcePayloads:
    def __init__(self, environment, *, schema="trendscout", preserve_expired=False):
        if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
            raise ValueError("invalid PostgreSQL schema name")
        if type(preserve_expired) is not bool:
            raise ValueError("expired payload preservation must be explicit")
        self._environment = dict(environment)
        self._schema = schema
        self._table = sql.SQL("{}.source_payload_blobs").format(sql.Identifier(schema))
        self._references = sql.SQL("{}.source_payload_references").format(sql.Identifier(schema))
        self.preserve_expired = preserve_expired

    def purge_expired(self, *, now, limit=500):
        """Delete a bounded batch of expired payloads, unless preservation is requested."""
        now = _utc(now)
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError("payload cleanup limit must be between 1 and 5000")
        if self.preserve_expired:
            return 0
        try:
            with postgres_connection(self._environment) as connection:
                with connection.transaction():
                    connection.execute("SELECT set_config('lock_timeout', '10s', true)")
                    connection.execute("SELECT set_config('statement_timeout', '30s', true)")
                    rows = connection.execute(sql.SQL("""SELECT payload.digest FROM {} AS payload
                        WHERE payload.retain_until<=%s AND NOT EXISTS (
                            SELECT 1 FROM {} AS reference WHERE reference.digest=payload.digest
                                AND (reference.retain_until IS NULL OR reference.retain_until>%s)
                        ) ORDER BY payload.retain_until,payload.digest
                        LIMIT %s FOR UPDATE OF payload SKIP LOCKED""").format(
                            self._table, self._references), (now, now, limit)).fetchall()
                    digests = [row[0] for row in rows]
                    if not digests:
                        return 0
                    connection.execute(sql.SQL("""DELETE FROM {} WHERE digest=ANY(%s)
                        AND retain_until IS NOT NULL AND retain_until<=%s""").format(
                            self._references), (digests, now))
                    cursor = connection.execute(sql.SQL("""DELETE FROM {} AS payload
                        WHERE payload.digest=ANY(%s) AND payload.retain_until<=%s
                            AND NOT EXISTS (SELECT 1 FROM {} AS reference
                                WHERE reference.digest=payload.digest)""").format(
                                    self._table, self._references), (digests, now))
                    return cursor.rowcount
        except psycopg.Error:
            raise RuntimeError("source payload cleanup unavailable") from None

    def delete_expired_unreferenced(self, digests, *, now):
        """Delete expired checkpoint candidates only after their state refs are gone."""
        now = _utc(now)
        if (not isinstance(digests, (set, frozenset, tuple, list))
                or len(digests) > 5000
                or any(not isinstance(value, str) or _DIGEST.fullmatch(value) is None
                       for value in digests)):
            raise ValueError("snapshot digest candidates must be bounded SHA-256 values")
        candidates = sorted(set(digests))
        if not candidates:
            return {"deleted": 0, "retained_referenced": 0}
        try:
            with postgres_connection(self._environment) as connection:
                with connection.transaction():
                    connection.execute("SELECT set_config('lock_timeout', '10s', true)")
                    connection.execute("SELECT set_config('statement_timeout', '30s', true)")
                    deletable = []
                    deleted = 0
                    if not self.preserve_expired:
                        rows = connection.execute(sql.SQL("""SELECT payload.digest FROM {} AS payload
                            WHERE payload.digest=ANY(%s) AND payload.retain_until<=%s
                                AND NOT EXISTS (SELECT 1 FROM {} AS reference
                                    WHERE reference.digest=payload.digest
                                        AND (reference.retain_until IS NULL OR reference.retain_until>%s))
                            ORDER BY payload.retain_until,payload.digest
                            LIMIT 5000 FOR UPDATE OF payload SKIP LOCKED""").format(
                                self._table, self._references),
                            (candidates, now, now)).fetchall()
                        deletable = [row[0] for row in rows]
                    if deletable:
                        connection.execute(sql.SQL("""DELETE FROM {} WHERE digest=ANY(%s)
                            AND retain_until IS NOT NULL AND retain_until<=%s""").format(
                                self._references), (deletable, now))
                        cursor = connection.execute(sql.SQL("""DELETE FROM {} AS payload
                            WHERE payload.digest=ANY(%s) AND payload.retain_until<=%s
                                AND NOT EXISTS (SELECT 1 FROM {} AS reference
                                    WHERE reference.digest=payload.digest)""").format(
                                        self._table, self._references), (deletable, now))
                        deleted = cursor.rowcount
                    retained = connection.execute(sql.SQL("""SELECT count(DISTINCT digest)
                        FROM {} WHERE digest=ANY(%s)
                            AND (retain_until IS NULL OR retain_until>%s)""").format(
                                self._references), (candidates, now)).fetchone()[0]
                    return {"deleted": deleted, "retained_referenced": retained}
        except psycopg.Error:
            raise RuntimeError("source payload cleanup unavailable") from None

    def put(self, records, *, now, retain_until):
        now, expiry = _utc(now), _utc(retain_until)
        if expiry <= now:
            raise ValueError("future retention deadline required")
        raw = _encode(records)
        reference = {"digest": hashlib.sha256(raw).hexdigest(),
                     "raw_bytes": len(raw), "item_count": len(records)}
        self._store(reference, raw, expiry, exact_retention=False)
        return reference

    def import_legacy(self, reference, records, *, now, retain_until):
        """Import verified legacy content without extending its retention."""
        now, expiry = _utc(now), _utc(retain_until)
        if expiry <= now or not _valid_reference(reference):
            raise ValueError("unexpired legacy payload reference required")
        raw = _encode(records)
        if (hashlib.sha256(raw).hexdigest() != reference["digest"]
                or len(raw) != reference["raw_bytes"] or len(records) != reference["item_count"]):
            raise ValueError("legacy payload does not match its reference")
        self._store(reference, raw, expiry, exact_retention=True)

    def _store(self, reference, raw, expiry, *, exact_retention):
        try:
            with postgres_connection(self._environment) as connection:
                with connection.transaction():
                    connection.execute("SELECT set_config('lock_timeout', '10s', true)")
                    connection.execute("SELECT set_config('statement_timeout', '30s', true)")
                    connection.execute(sql.SQL("""INSERT INTO {}(digest,raw_data,raw_bytes,item_count,retain_until)
                        VALUES (%s,%s,%s,%s,%s) ON CONFLICT(digest) DO NOTHING""").format(self._table),
                        (reference["digest"], raw, len(raw), reference["item_count"], expiry))
                    row = connection.execute(sql.SQL("""SELECT raw_data,raw_bytes,item_count,retain_until
                        FROM {} WHERE digest=%s FOR UPDATE""").format(self._table),
                        (reference["digest"],)).fetchone()
                    if (row is None or bytes(row[0]) != raw or row[1] != len(raw)
                            or row[2] != reference["item_count"]):
                        raise ValueError("source payload digest conflicts with stored content")
                    if exact_retention and row[3] != expiry:
                        raise ValueError("legacy payload retention conflicts with stored content")
                    if not exact_retention and row[3] < expiry:
                        connection.execute(sql.SQL("UPDATE {} SET retain_until=%s WHERE digest=%s")
                                           .format(self._table), (expiry, reference["digest"]))
        except psycopg.Error:
            raise RuntimeError("source payload storage unavailable") from None

    def get(self, reference, *, now):
        now = _utc(now)
        if not _valid_reference(reference):
            raise ValueError("invalid source payload reference")
        try:
            with postgres_connection(self._environment) as connection:
                row = connection.execute(sql.SQL("""SELECT raw_data,raw_bytes,item_count,retain_until
                    FROM {} WHERE digest=%s""").format(self._table), (reference["digest"],)).fetchone()
        except psycopg.Error:
            raise RuntimeError("source payload storage unavailable") from None
        if (row is None or row[3] <= now or row[1] != reference["raw_bytes"]
                or row[2] != reference["item_count"]):
            raise ValueError("source payload unavailable, expired or mismatched")
        raw = bytes(row[0])
        if len(raw) != row[1] or hashlib.sha256(raw).hexdigest() != reference["digest"]:
            raise ValueError("source payload integrity check failed")
        try:
            records = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError("source payload is invalid JSON") from None
        if not isinstance(records, list) or len(records) != row[2]:
            raise ValueError("source payload record count is invalid")
        return records

    def inspect(self, reference, *, now):
        """Validate a retained content-addressed blob without transferring its bytes."""
        now = _utc(now)
        if not _valid_reference(reference):
            raise ValueError("invalid source payload reference")
        try:
            with postgres_connection(self._environment) as connection:
                row = connection.execute(sql.SQL("""SELECT raw_bytes,item_count,retain_until
                    FROM {} WHERE digest=%s""").format(self._table),
                    (reference["digest"],)).fetchone()
        except psycopg.Error:
            raise RuntimeError("source payload storage unavailable") from None
        if (row is None or row[2] <= now or row[0] != reference["raw_bytes"]
                or row[1] != reference["item_count"]):
            raise ValueError("source payload unavailable, expired or mismatched")
        return {"raw_bytes": row[0], "item_count": row[1],
                "retain_until": row[2]}
