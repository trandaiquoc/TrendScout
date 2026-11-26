"""Bounded PostgreSQL retention sweeps for canonical cache/checkpoint payloads."""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg import sql

from app.retention.policy import rolling_retention_window
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import _SCHEMA_NAME

_MAX_BATCH_SIZE = 500
_SWEEP_INTERVAL = timedelta(hours=1)
_SWEEP_CACHE_MAX = 128
_SWEEP_CACHE_LOCK = threading.Lock()
_SWEEP_CACHE: OrderedDict[str, tuple[datetime, bool]] = OrderedDict()


def _cache_identity(environment, schema):
    digest = hashlib.sha256()
    for value in (environment.get("DATABASE_URL"), environment.get("DATABASE_URL_UNPOOLED"), schema):
        encoded = value.encode("utf-8") if isinstance(value, str) else b""
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _cache_skip(identity, now):
    with _SWEEP_CACHE_LOCK:
        previous = _SWEEP_CACHE.get(identity)
        if previous is None:
            return False
        checked_at, has_more = previous
        # A full batch must allow the next invocation to keep draining its
        # backlog. Throttle only after a sweep confirms there is no more work.
        if not has_more and now - _SWEEP_INTERVAL < checked_at:
            _SWEEP_CACHE.move_to_end(identity)
            return True
        return False


def _remember_sweep(identity, now, has_more):
    with _SWEEP_CACHE_LOCK:
        _SWEEP_CACHE[identity] = (now, bool(has_more))
        _SWEEP_CACHE.move_to_end(identity)
        while len(_SWEEP_CACHE) > _SWEEP_CACHE_MAX:
            _SWEEP_CACHE.popitem(last=False)


class PostgresRetentionStore:
    """Purge expired payload blobs and audit unresolved source-evidence rows.

    Source-evidence deletion is deliberately withheld until every persisted
    copy has attributable lineage. This store only deletes content whose
    expiry is explicit and whose durable references are expired or absent.
    """

    def __init__(self, environment, *, schema="trendscout", preserve_expired=False):
        if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
            raise ValueError("invalid PostgreSQL schema name")
        if type(preserve_expired) is not bool:
            raise ValueError("expired-data preservation must be explicit")
        self._environment = dict(environment)
        self._schema = schema
        self._preserve_expired = preserve_expired
        self._payloads = sql.SQL("{}.source_payload_blobs").format(sql.Identifier(schema))
        self._references = sql.SQL("{}.source_payload_references").format(sql.Identifier(schema))
        self._evidence = sql.SQL("{}.source_evidence").format(sql.Identifier(schema))
        self._audit = sql.SQL("{}.retention_sweep_runs").format(sql.Identifier(schema))

    def sweep(self, *, now=None, dry_run=False, limit=500):
        """Run one bounded, idempotent cleanup batch without provider calls.

        Dry run persists only an aggregate audit row. Source evidence due by
        the rolling window is counted but retained because legacy keyword and
        classification rows can contain unlinked copies of that content.
        """
        now = datetime.now(timezone.utc) if now is None else now
        if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
                or type(dry_run) is not bool or type(limit) is not int
                or not 1 <= limit <= _MAX_BATCH_SIZE):
            raise ValueError("valid aware time, dry-run flag, and batch limit are required")
        now = now.astimezone(timezone.utc)
        window = rolling_retention_window(now)
        run_id = uuid.uuid4().hex
        if self._preserve_expired:
            return {
                "run_id": None,
                "status": "PRESERVED",
                "dry_run": dry_run,
                "business_date": window.business_date.isoformat(),
                "cutoff_at": window.cutoff_at.isoformat(),
                "payload_candidate_count": 0,
                "payload_deleted_count": 0,
                "payload_blocked_count": 0,
                "payload_has_more": False,
                "source_evidence_due_count": 0,
                "source_evidence_deleted_count": 0,
            }
        cache_identity = _cache_identity(self._environment, self._schema)
        if not dry_run and _cache_skip(cache_identity, now):
            return {
                "run_id": None,
                "status": "SKIPPED_RECENT_SWEEP",
                "dry_run": False,
                "business_date": window.business_date.isoformat(),
                "cutoff_at": window.cutoff_at.isoformat(),
                "payload_candidate_count": 0,
                "payload_deleted_count": 0,
                "payload_blocked_count": 0,
                "payload_has_more": False,
                "source_evidence_due_count": 0,
                "source_evidence_deleted_count": 0,
            }

        try:
            with postgres_connection(self._environment) as connection:
                with connection.transaction():
                    connection.execute("SELECT set_config('lock_timeout', '10s', true)")
                    connection.execute("SELECT set_config('statement_timeout', '30s', true)")
                    connection.execute("SELECT set_config('idle_in_transaction_session_timeout', '30s', true)")
                    locked = connection.execute(
                        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                        ("trendscout-retention:" + self._schema,),
                    ).fetchone()[0]
                    if not locked:
                        return {
                            "run_id": None,
                            "status": "SKIPPED_CONCURRENT_SWEEP",
                            "dry_run": dry_run,
                            "business_date": window.business_date.isoformat(),
                            "cutoff_at": window.cutoff_at.isoformat(),
                            "payload_candidate_count": 0,
                            "payload_deleted_count": 0,
                            "payload_blocked_count": 0,
                            "payload_has_more": True,
                            "source_evidence_due_count": 0,
                            "source_evidence_deleted_count": 0,
                        }

                    if not dry_run:
                        previous = connection.execute(sql.SQL("""SELECT finished_at,payload_has_more
                            FROM {} WHERE dry_run=FALSE
                            ORDER BY finished_at DESC,id DESC LIMIT 1""").format(self._audit)).fetchone()
                        if (previous is not None
                                and now - timedelta(hours=1) < previous[0] <= now
                                and not previous[1]):
                            _remember_sweep(cache_identity, previous[0], False)
                            return {
                                "run_id": None,
                                "status": "SKIPPED_RECENT_SWEEP",
                                "dry_run": False,
                                "business_date": window.business_date.isoformat(),
                                "cutoff_at": window.cutoff_at.isoformat(),
                                "payload_candidate_count": 0,
                                "payload_deleted_count": 0,
                                "payload_blocked_count": 0,
                                "payload_has_more": False,
                                "source_evidence_due_count": 0,
                                "source_evidence_deleted_count": 0,
                            }

                    source_evidence_due = connection.execute(sql.SQL("""SELECT count(*)
                        FROM {} WHERE collected_at < %s OR expires_at <= %s""").format(
                            self._evidence), (window.cutoff_at, now)).fetchone()[0]
                    blocked_payloads = connection.execute(sql.SQL("""SELECT count(*)
                        FROM {} AS payload WHERE payload.retain_until <= %s
                          AND EXISTS (SELECT 1 FROM {} AS reference
                            WHERE reference.digest=payload.digest
                              AND (reference.retain_until IS NULL OR reference.retain_until > %s))""").format(
                                  self._payloads, self._references), (now, now)).fetchone()[0]
                    selection_limit = limit + 1 if dry_run else limit
                    lock_clause = sql.SQL("") if dry_run else sql.SQL(" FOR UPDATE OF payload SKIP LOCKED")
                    candidates = connection.execute(sql.SQL("""SELECT payload.digest
                        FROM {} AS payload
                        WHERE payload.retain_until <= %s
                          AND NOT EXISTS (SELECT 1 FROM {} AS reference
                            WHERE reference.digest=payload.digest
                              AND (reference.retain_until IS NULL OR reference.retain_until > %s))
                        ORDER BY payload.retain_until,payload.digest
                        LIMIT %s""").format(self._payloads, self._references)
                        + lock_clause, (now, now, selection_limit)).fetchall()
                    has_more = len(candidates) > limit if dry_run else False
                    digests = [row[0] for row in candidates[:limit]]
                    deleted = 0
                    if digests and not dry_run:
                        connection.execute(sql.SQL("""DELETE FROM {} WHERE digest=ANY(%s)
                            AND retain_until <= %s""").format(self._references), (digests, now))
                        cursor = connection.execute(sql.SQL("""DELETE FROM {} AS payload
                            WHERE payload.digest=ANY(%s) AND payload.retain_until <= %s
                              AND NOT EXISTS (SELECT 1 FROM {} AS reference
                                WHERE reference.digest=payload.digest)""").format(
                                    self._payloads, self._references), (digests, now))
                        deleted = max(0, cursor.rowcount)
                    if not dry_run:
                        has_more = connection.execute(sql.SQL("""SELECT EXISTS (
                            SELECT 1 FROM {} AS payload
                            WHERE payload.retain_until <= %s
                              AND NOT EXISTS (SELECT 1 FROM {} AS reference
                                WHERE reference.digest=payload.digest
                                  AND (reference.retain_until IS NULL OR reference.retain_until > %s))
                            LIMIT 1)""").format(self._payloads, self._references),
                            (now, now)).fetchone()[0]

                    status = "DRY_RUN" if dry_run else "COMPLETED"
                    connection.execute(sql.SQL("""INSERT INTO {}(
                        id,policy_version,business_timezone,business_date,cutoff_at,evaluated_at,
                        dry_run,batch_limit,status,payload_candidate_count,payload_deleted_count,
                        payload_blocked_count,payload_has_more,source_evidence_due_count,
                        source_evidence_deleted_count,started_at,finished_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s)""").format(
                            self._audit),
                        (run_id, window.policy_version, window.business_timezone,
                         window.business_date, window.cutoff_at, now, dry_run, limit,
                         status, len(digests), deleted, blocked_payloads, has_more,
                         source_evidence_due, now, now),
                    )
                    result = {
                        "run_id": run_id,
                        "status": status,
                        "dry_run": dry_run,
                        "business_date": window.business_date.isoformat(),
                        "cutoff_at": window.cutoff_at.isoformat(),
                        "payload_candidate_count": len(digests),
                        "payload_deleted_count": deleted,
                        "payload_blocked_count": blocked_payloads,
                        "payload_has_more": bool(has_more),
                        "source_evidence_due_count": source_evidence_due,
                        "source_evidence_deleted_count": 0,
                    }
            if not dry_run:
                _remember_sweep(cache_identity, now, bool(has_more))
            return result
        except psycopg.Error:
            raise RuntimeError("PostgreSQL retention sweep unavailable") from None
