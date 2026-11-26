"""PostgreSQL implementation of the existing atomic coordination port.

This compatibility boundary preserves the domain callback contract while
persisting changed map entries and source checkpoints separately. It is used
by explicit PostgreSQL compositions; selecting it as the sole runtime
authority still requires legacy coordination migration and writer fencing.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from app.ports.atomic_state import CommitOutcomeUnknown
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import _SCHEMA_NAME

_KEY = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_REFERENCE_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_MAX_BYTES = 8_000_000
_MAX_REFERENCES = 10_000


class PostgresCoordinationError(RuntimeError):
    """A coordination transaction could not be completed."""


def _valid_key(key):
    return (isinstance(key, str) and _KEY.fullmatch(key) is not None
            and key not in {".", ".."} and not (key.startswith("__") and key.endswith("__")))


def _validate(value):
    if not isinstance(value, dict):
        raise ValueError("coordination state must be a mapping")

    def keys_are_strings(item):
        if isinstance(item, dict):
            return all(isinstance(key, str) and "\x00" not in key and keys_are_strings(child)
                       for key, child in item.items())
        if isinstance(item, list):
            return all(keys_are_strings(child) for child in item)
        return True

    try:
        if not keys_are_strings(value):
            raise ValueError("invalid JSON object key")
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("coordination state must be finite UTF-8 JSON") from None
    if len(encoded) > _MAX_BYTES:
        raise ValueError("coordination state exceeds storage budget")


def _flatten(state):
    fields, entries, checkpoints = {}, {}, {}
    for field, value in state.items():
        mapping = isinstance(value, dict)
        fields[field] = (mapping, None if mapping else value)
        if not mapping:
            continue
        for item_key, item in value.items():
            has_checkpoints = (field == "jobs" and isinstance(item, dict)
                               and isinstance(item.get("source_checkpoints"), dict))
            if has_checkpoints:
                item = dict(item)
                for source_id, checkpoint in item.pop("source_checkpoints").items():
                    checkpoints[(item_key, source_id)] = checkpoint
            entries[(field, item_key)] = (item, has_checkpoints)
    return fields, entries, checkpoints


def _same(left, right):
    # Python considers True == 1; the coordination contract must retain JSON
    # types because quota and provider-request validation distinguish them.
    return json.dumps(left, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")) == json.dumps(
                          right, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":"))


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _payload_references(state):
    references = []

    def visit(value, path=(), job_expiry=None):
        if isinstance(value, dict):
            if len(path) == 2 and path[0] == "jobs":
                job_expiry = _timestamp(value.get("expires_at"))
            for key, child in value.items():
                if (key in {"payload_ref", "snapshot_ref"} and isinstance(child, dict)
                        and isinstance(child.get("digest"), str)
                        and _REFERENCE_DIGEST.fullmatch(child["digest"]) is not None
                        and type(child.get("raw_bytes")) is int
                        and 0 <= child["raw_bytes"] <= 5_700_000
                        and type(child.get("item_count")) is int
                        and 0 <= child["item_count"] <= 5000):
                    expiry = _timestamp(value.get("retain_until"))
                    if job_expiry is not None:
                        expiry = min(expiry, job_expiry) if expiry is not None else job_expiry
                    reference_path = json.dumps(
                        [*path, key], ensure_ascii=False, separators=(",", ":")
                    )
                    references.append((reference_path, child["digest"], expiry))
                    if len(references) > _MAX_REFERENCES:
                        raise ValueError("coordination payload reference limit exceeded")
                visit(child, (*path, key), job_expiry)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, (*path, index), job_expiry)

    visit(state)
    return references


class PostgresAtomicStateStore:
    def __init__(self, environment, *, schema="trendscout"):
        if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
            raise ValueError("invalid PostgreSQL schema name")
        self._environment = dict(environment)
        self._schema = schema

    def _table(self, name):
        return sql.SQL("{}.{}").format(sql.Identifier(self._schema), sql.Identifier(name))

    def _load(self, connection, keys):
        state = {key: {} for key in keys}
        query = sql.SQL("SELECT scope, field, is_mapping, value FROM {} WHERE scope=ANY(%s)")
        for scope, field, mapping, value in connection.execute(
            query.format(self._table("coordination_fields")), (keys,)
        ):
            state[scope][field] = {} if mapping else value
        query = sql.SQL("SELECT scope, field, item_key, value, has_checkpoints FROM {} WHERE scope=ANY(%s)")
        for scope, field, item_key, value, has_checkpoints in connection.execute(
            query.format(self._table("coordination_entries")), (keys,)
        ):
            if (not isinstance(state[scope].get(field), dict)
                    or has_checkpoints and (field != "jobs" or not isinstance(value, dict))):
                raise PostgresCoordinationError("stored coordination structure is invalid")
            if has_checkpoints:
                value["source_checkpoints"] = {}
            state[scope][field][item_key] = value
        query = sql.SQL("SELECT scope, job_id, source_id, value FROM {} WHERE scope=ANY(%s)")
        for scope, job_id, source_id, value in connection.execute(
            query.format(self._table("coordination_checkpoints")), (keys,)
        ):
            jobs = state[scope].get("jobs")
            job = jobs.get(job_id) if isinstance(jobs, dict) else None
            if not isinstance(job, dict) or not isinstance(job.get("source_checkpoints"), dict):
                raise PostgresCoordinationError("stored checkpoint structure is invalid")
            job["source_checkpoints"][source_id] = value
        return state

    def _write(self, connection, scope, previous, updated):
        old_fields, old_entries, old_checkpoints = _flatten(previous)
        fields, entries, checkpoints = _flatten(updated)
        with connection.cursor() as cursor:
            for (job_id, source_id) in old_checkpoints.keys() - checkpoints.keys():
                cursor.execute(sql.SQL("DELETE FROM {} WHERE scope=%s AND job_id=%s AND source_id=%s")
                               .format(self._table("coordination_checkpoints")), (scope, job_id, source_id))
            for (field, item_key) in old_entries.keys() - entries.keys():
                cursor.execute(sql.SQL("DELETE FROM {} WHERE scope=%s AND field=%s AND item_key=%s")
                               .format(self._table("coordination_entries")), (scope, field, item_key))
            for field in old_fields.keys() - fields.keys():
                cursor.execute(sql.SQL("DELETE FROM {} WHERE scope=%s AND field=%s")
                               .format(self._table("coordination_fields")), (scope, field))
            for field, (mapping, value) in fields.items():
                if field in old_fields and _same(old_fields[field], (mapping, value)):
                    continue
                cursor.execute(sql.SQL("""INSERT INTO {}(scope,field,is_mapping,value) VALUES (%s,%s,%s,%s)
                    ON CONFLICT(scope,field) DO UPDATE SET is_mapping=EXCLUDED.is_mapping,value=EXCLUDED.value""")
                    .format(self._table("coordination_fields")),
                    (scope, field, mapping, None if mapping else Jsonb(value)))
            for (field, item_key), (value, has_checkpoints) in entries.items():
                if (field, item_key) in old_entries and _same(
                    old_entries[(field, item_key)], (value, has_checkpoints)
                ):
                    continue
                cursor.execute(sql.SQL("""INSERT INTO {}(scope,field,item_key,value,has_checkpoints)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT(scope,field,item_key) DO UPDATE
                    SET value=EXCLUDED.value,has_checkpoints=EXCLUDED.has_checkpoints""")
                    .format(self._table("coordination_entries")),
                    (scope, field, item_key, Jsonb(value), has_checkpoints))
            for (job_id, source_id), value in checkpoints.items():
                if ((job_id, source_id) in old_checkpoints
                        and _same(old_checkpoints[(job_id, source_id)], value)):
                    continue
                cursor.execute(sql.SQL("""INSERT INTO {}(scope,job_id,source_id,value) VALUES (%s,%s,%s,%s)
                    ON CONFLICT(scope,job_id,source_id) DO UPDATE SET value=EXCLUDED.value""")
                    .format(self._table("coordination_checkpoints")),
                    (scope, job_id, source_id, Jsonb(value)))
            cursor.execute(sql.SQL("DELETE FROM {} WHERE scope=%s")
                           .format(self._table("source_payload_references")), (scope,))
            references = _payload_references(updated)
            if references:
                # State can retain expired diagnostics after its blob is gone;
                # only index present, still-readable content. The FK serializes
                # a live insert against a concurrent payload deletion. One
                # UNNEST insert keeps large reference sets to one round trip.
                payload = self._table("source_payload_blobs")
                cursor.execute(sql.SQL("""INSERT INTO {}(scope,reference_path,digest,retain_until)
                    SELECT %s,reference.reference_path,payload.digest,
                        LEAST(COALESCE(reference.retain_until,payload.retain_until),
                              payload.retain_until)
                    FROM unnest(%s::text[],%s::text[],%s::timestamptz[])
                        AS reference(reference_path,digest,retain_until)
                    JOIN {} AS payload ON payload.digest=reference.digest::char(64)
                    WHERE LEAST(COALESCE(reference.retain_until,payload.retain_until),
                                payload.retain_until)>clock_timestamp()""").format(
                            self._table("source_payload_references"), payload),
                    (scope, [item[0] for item in references],
                     [item[1] for item in references], [item[2] for item in references]))

    def transact(self, key, operation):
        if not _valid_key(key) or not callable(operation):
            raise ValueError("valid state key and operation required")
        wrote = False
        try:
            with postgres_connection(self._environment) as connection:
                with connection.transaction():
                    connection.execute("SELECT set_config('lock_timeout', '10s', true)")
                    connection.execute("SELECT set_config('statement_timeout', '30s', true)")
                    connection.execute("SELECT set_config('idle_in_transaction_session_timeout', '30s', true)")
                    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                                       ("trendscout-coordination:" + self._schema + ":" + key,))
                    current = self._load(connection, [key])[key]
                    updated, result = operation(deepcopy(current))
                    _validate(updated)
                    if not _same(updated, current):
                        wrote = True
                        self._write(connection, key, current, updated)
            return result
        except psycopg.Error:
            if wrote:
                raise CommitOutcomeUnknown("coordination commit outcome unknown") from None
            raise PostgresCoordinationError("coordination transaction failed") from None

    def read_many(self, keys):
        if (not isinstance(keys, (tuple, list)) or not keys or len(keys) > 1000
                or any(not _valid_key(key) for key in keys) or len(keys) != len(set(keys))):
            raise ValueError("unique valid state keys required")
        try:
            with postgres_connection(self._environment) as connection:
                with connection.transaction():
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    connection.execute("SELECT set_config('statement_timeout', '30s', true)")
                    return self._load(connection, list(keys))
        except psycopg.Error:
            raise PostgresCoordinationError("coordination read failed") from None
