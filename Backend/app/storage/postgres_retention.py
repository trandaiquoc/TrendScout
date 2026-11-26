"""Bounded PostgreSQL retention sweeps for source and derived content."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from app.ai.runtime import STATE_KEY as AI_STATE_KEY
from app.ai.runtime import AIService
from app.retention.policy import rolling_retention_window
from app.storage.postgres_atomic_state import PostgresAtomicStateStore
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import _SCHEMA_NAME

_MAX_BATCH_SIZE = 500
_SWEEP_INTERVAL = timedelta(hours=1)
_SWEEP_CACHE_MAX = 128
_SWEEP_CACHE_LOCK = threading.Lock()
_SWEEP_CACHE: OrderedDict[str, tuple[datetime, bool]] = OrderedDict()
_KEYWORD_COLUMNS = {"keyword", "normalized", "observed_at", "context", "evidence"}


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


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _read_path(value, path):
    current = value
    try:
        for part in path:
            if isinstance(current, list):
                current = current[int(part)]
            else:
                current = current[part]
        return current, True
    except (IndexError, KeyError, TypeError, ValueError):
        return None, False


def _remove_path(value, path):
    if not path:
        return {}
    parent, found = _read_path(value, path[:-1])
    if not found:
        return value
    leaf = path[-1]
    if isinstance(parent, list):
        try:
            del parent[int(leaf)]
        except (IndexError, TypeError, ValueError):
            return value
    elif isinstance(parent, dict):
        parent.pop(leaf, None)
    return value


class PostgresRetentionStore:
    """Remove expired content whose persisted lineage proves its ownership.

    Every relational delete and audit update is one PostgreSQL transaction.
    Evidence without sufficient lineage is retained and reported as blocked.
    """

    def __init__(self, environment, *, schema="trendscout", preserve_expired=False):
        if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
            raise ValueError("invalid PostgreSQL schema name")
        if type(preserve_expired) is not bool:
            raise ValueError("expired-data preservation must be explicit")
        self._environment = dict(environment)
        self._schema = schema
        self._preserve_expired = preserve_expired
        self._payloads = self._table("source_payload_blobs")
        self._references = self._table("source_payload_references")
        self._evidence = self._table("source_evidence")
        self._evidence_keywords = self._table("evidence_keywords")
        self._dataset_evidence = self._table("dataset_evidence")
        self._datasets = self._table("dataset_snapshots")
        self._keywords = self._table("keywords")
        self._contexts = self._table("entity_context")
        self._observations = self._table("algorithm_observations")
        self._algorithm_results = self._table("algorithm_results")
        self._classifications = self._table("classification_results")
        self._cache = self._table("cache")
        self._cache_evidence = self._table("cache_evidence")
        self._audit = self._table("retention_sweep_runs")

    def _table(self, name):
        return sql.SQL("{}.{}").format(sql.Identifier(self._schema), sql.Identifier(name))

    def _has_active_keyword_lineage(self, connection, *, keyword_id, source_id, column, path,
                                    value_sha256, now, cutoff_at):
        return connection.execute(sql.SQL("""SELECT EXISTS(
            SELECT 1 FROM {} ek JOIN {} evidence ON evidence.id=ek.evidence_id
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(evidence.payload #> '{{retention_lineage,keyword_fields}}')='array'
                     THEN evidence.payload #> '{{retention_lineage,keyword_fields}}'
                     ELSE '[]'::jsonb END
            ) AS lineage(item)
            WHERE ek.keyword_id=%s AND evidence.expires_at>%s AND evidence.collected_at>=%s
              AND lineage.item->>'keyword_id'=%s
              AND COALESCE(lineage.item->>'source_id',evidence.source_id)=%s
              AND COALESCE(lineage.item->>'column','evidence')=%s
              AND COALESCE(lineage.item->'path','[]'::jsonb)=%s::jsonb
              AND lineage.item->>'value_sha256'=%s)""").format(
                  self._evidence_keywords, self._evidence),
            (keyword_id, now, cutoff_at, keyword_id, source_id, column, Jsonb(path), value_sha256),
        ).fetchone()[0]

    def _scrub_keyword_lineage(self, connection, lineage, *, source_id, now, cutoff_at):
        keyword_id = lineage.get("keyword_id")
        column = lineage.get("column", "evidence")
        path = lineage.get("path", [])
        value_sha256 = lineage.get("value_sha256")
        if (not isinstance(keyword_id, str) or column not in _KEYWORD_COLUMNS
                or not isinstance(path, list) or any(not isinstance(part, str) for part in path)
                or not isinstance(value_sha256, str) or len(value_sha256) != 64):
            return None
        if self._has_active_keyword_lineage(
                connection, keyword_id=keyword_id,
                source_id=lineage.get("source_id", source_id), column=column, path=path,
                value_sha256=value_sha256, now=now, cutoff_at=cutoff_at):
            return None

        row = connection.execute(sql.SQL("SELECT {} FROM {} WHERE id=%s FOR UPDATE").format(
            sql.Identifier(column), self._keywords), (keyword_id,)).fetchone()
        if row is None:
            return None
        value = row[0]
        if column == "evidence":
            if not path:
                current = value
                found = isinstance(current, dict)
            else:
                current, found = _read_path(value, path)
            if not found or _digest(current) != value_sha256:
                return None
            updated = _remove_path(deepcopy(value), path)
            connection.execute(sql.SQL("UPDATE {} SET evidence=%s WHERE id=%s").format(
                self._keywords), (Jsonb(updated), keyword_id))
        else:
            if path or not isinstance(value, str) or _digest(value) != value_sha256:
                return None
            connection.execute(sql.SQL("UPDATE {} SET {}='' WHERE id=%s").format(
                self._keywords, sql.Identifier(column)), (keyword_id,))
            if column == "keyword":
                connection.execute(sql.SQL("""UPDATE {} SET eligible=0,
                    rejection_reason='SOURCE_EVIDENCE_EXPIRED' WHERE id=%s AND eligible<>0""")
                    .format(self._keywords), (keyword_id,))
        return column

    def _has_active_context_lineage(self, connection, target, *, now, cutoff_at):
        return connection.execute(sql.SQL("""SELECT EXISTS(
            SELECT 1 FROM {} ek JOIN {} evidence ON evidence.id=ek.evidence_id
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(evidence.payload #> '{{retention_lineage,entity_context_rows}}')='array'
                     THEN evidence.payload #> '{{retention_lineage,entity_context_rows}}'
                     ELSE '[]'::jsonb END
            ) AS lineage(item)
            WHERE ek.keyword_id=%s AND evidence.expires_at>%s AND evidence.collected_at>=%s
              AND lineage.item->>'keyword_id'=%s
              AND lineage.item->>'context_version' IS NOT DISTINCT FROM %s
              AND lineage.item->>'collected_at'=%s
              AND lineage.item->>'value_sha256'=%s)""").format(
                  self._evidence_keywords, self._evidence),
            (target["keyword_id"], now, cutoff_at, target["keyword_id"],
             target.get("context_version"), target["collected_at"], target["value_sha256"]),
        ).fetchone()[0]

    def _delete_expired_context(self, connection, target, *, now, cutoff_at):
        if self._has_active_context_lineage(connection, target, now=now, cutoff_at=cutoff_at):
            return False
        columns = ("keyword_id", "title", "page_id", "entity_id", "description",
                   "page_namespace", "is_disambiguation", "redirected_from",
                   "page_language", "is_main_page", "is_main_page_known", "entity_type",
                   "occupations", "category_ids", "instance_types", "source_url", "method",
                   "status", "context_version", "captured_at")
        row_values = connection.execute(sql.SQL("SELECT {} FROM {} WHERE keyword_id=%s FOR UPDATE")
            .format(sql.SQL(",").join(map(sql.Identifier, columns)), self._contexts),
            (target["keyword_id"],)).fetchone()
        if row_values is None:
            return False
        row = dict(zip(columns, row_values))
        if row["context_version"] != target.get("context_version"):
            return False
        if row["captured_at"].astimezone(timezone.utc).isoformat() != target["collected_at"]:
            return False
        content = dict(row)
        content["is_disambiguation"] = bool(content["is_disambiguation"])
        content["is_main_page"] = bool(content["is_main_page"])
        content["is_main_page_known"] = bool(content["is_main_page_known"])
        content["fetched_at"] = content.pop("captured_at").astimezone(timezone.utc).isoformat()
        if _digest(content) != target.get("value_sha256"):
            return False
        connection.execute(sql.SQL("DELETE FROM {} WHERE keyword_id=%s").format(self._contexts),
                           (target["keyword_id"],))
        return True

    def _prune_ai_content(self, connection, now):
        state_store = PostgresAtomicStateStore(self._environment, schema=self._schema)
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                           ("trendscout-coordination:" + self._schema + ":" + AI_STATE_KEY,))
        current = state_store._load(connection, [AI_STATE_KEY])[AI_STATE_KEY]
        if not current:
            return {"ai_cache": 0, "ai_recommendations": 0,
                    "ai_job_results": 0, "ai_content_untracked": 0}
        updated = deepcopy(current)
        counts = AIService.prune_expired_content(updated, now)
        if updated != current:
            state_store._write(connection, AI_STATE_KEY, current, updated)
        return counts

    def _clean_evidence(self, connection, evidence, *, now, cutoff_at, counts,
                        cache_budget):
        evidence_id, source_id, query_scope, payload = evidence
        linked = connection.execute(sql.SQL("SELECT keyword_id FROM {} WHERE evidence_id=%s ORDER BY keyword_id")
                                     .format(self._evidence_keywords), (evidence_id,)).fetchall()
        keyword_ids = [row[0] for row in linked]
        lineage = payload.get("retention_lineage") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and set(payload) - {"retention_lineage"}:
            return False, cache_budget
        if lineage is not None and (not isinstance(lineage, dict) or lineage.get("version") != 1):
            return False, cache_budget
        lineage = lineage if isinstance(lineage, dict) else {}
        keyword_fields = lineage.get("keyword_fields", [])
        context_rows = lineage.get("entity_context_rows", [])
        if not isinstance(keyword_fields, list) or not isinstance(context_rows, list):
            return False, cache_budget
        if any(not isinstance(item, dict)
               or not isinstance(item.get("keyword_id"), str)
               or item.get("column", "evidence") not in _KEYWORD_COLUMNS
               or not isinstance(item.get("path", []), list)
               or any(not isinstance(part, str) for part in item.get("path", []))
               or not isinstance(item.get("value_sha256"), str)
               or len(item["value_sha256"]) != 64 for item in keyword_fields):
            return False, cache_budget
        if any(not isinstance(item, dict)
               or not isinstance(item.get("keyword_id"), str)
               or not isinstance(item.get("context_version"), str)
               or not isinstance(item.get("collected_at"), str)
               or not isinstance(item.get("value_sha256"), str)
               or len(item["value_sha256"]) != 64 for item in context_rows):
            return False, cache_budget
        covered = {item.get("keyword_id") for item in keyword_fields + context_rows
                   if isinstance(item, dict)}
        if (not isinstance(query_scope, dict)
                or set(keyword_ids) - covered):
            return False, cache_budget

        cache_rows = connection.execute(sql.SQL("""SELECT fingerprint FROM {} WHERE evidence_id=%s
            ORDER BY fingerprint LIMIT %s FOR UPDATE""").format(self._cache_evidence),
            (evidence_id, cache_budget + 1)).fetchall()
        if len(cache_rows) > cache_budget:
            cache_rows = cache_rows[:cache_budget]
        fingerprints = [row[0] for row in cache_rows]
        if fingerprints:
            cursor = connection.execute(sql.SQL("DELETE FROM {} WHERE fingerprint=ANY(%s)")
                                        .format(self._cache), (fingerprints,))
            counts["embedding_cache"] += max(0, cursor.rowcount)
            cache_budget -= max(0, cursor.rowcount)
        cache_more = connection.execute(sql.SQL("SELECT EXISTS(SELECT 1 FROM {} WHERE evidence_id=%s)")
                                        .format(self._cache_evidence), (evidence_id,)).fetchone()[0]
        if cache_more:
            return False, cache_budget

        impacted = set()
        for item in keyword_fields:
            if not isinstance(item, dict):
                continue
            column = self._scrub_keyword_lineage(
                connection, item, source_id=source_id, now=now, cutoff_at=cutoff_at
            )
            if column is not None:
                counts["keyword_content"] += int(column != "evidence")
                counts["keyword_evidence_fields"] += int(column == "evidence")
            if isinstance(item.get("keyword_id"), str):
                impacted.add(item["keyword_id"])
        for item in context_rows:
            if isinstance(item, dict) and self._delete_expired_context(
                    connection, item, now=now, cutoff_at=cutoff_at):
                counts["entity_context"] += 1
                if isinstance(item.get("keyword_id"), str):
                    impacted.add(item["keyword_id"])
        impacted.update(keyword_ids)

        observation_date = query_scope.get("observation_date")
        if impacted:
            if observation_date is None:
                cursor = connection.execute(sql.SQL("DELETE FROM {} WHERE keyword_id=ANY(%s) AND source=%s")
                                            .format(self._observations), (sorted(impacted), source_id))
            else:
                cursor = connection.execute(sql.SQL("DELETE FROM {} WHERE keyword_id=ANY(%s) "
                    "AND source=%s AND observation_date=%s").format(self._observations),
                    (sorted(impacted), source_id, str(observation_date)))
            counts["keyword_observations"] += max(0, cursor.rowcount)
            cursor = connection.execute(sql.SQL("DELETE FROM {} WHERE keyword_id=ANY(%s)")
                                        .format(self._algorithm_results), (sorted(impacted),))
            counts["algorithm_results"] += max(0, cursor.rowcount)
            cursor = connection.execute(sql.SQL("DELETE FROM {} WHERE keyword_id=ANY(%s)")
                                        .format(self._classifications), (sorted(impacted),))
            counts["classifications"] += max(0, cursor.rowcount)

        dataset_ids = [row[0] for row in connection.execute(
            sql.SQL("SELECT dataset_id FROM {} WHERE evidence_id=%s ORDER BY dataset_id")
            .format(self._dataset_evidence), (evidence_id,)).fetchall()]
        if dataset_ids:
            # Source manifests are aggregate descriptors; remove the expired
            # provider entry and mark the saved snapshot as retention-pruned.
            for dataset_id in dataset_ids:
                snapshot = connection.execute(sql.SQL("SELECT source_manifest,quality_flags FROM {} "
                    "WHERE dataset_id=%s FOR UPDATE").format(self._datasets), (dataset_id,)).fetchone()
                active_source = connection.execute(sql.SQL("""SELECT EXISTS(
                    SELECT 1 FROM {} de JOIN {} active ON active.id=de.evidence_id
                    WHERE de.dataset_id=%s AND active.id<>%s AND active.source_id=%s
                      AND active.expires_at>%s AND active.collected_at>=%s)""").format(
                          self._dataset_evidence, self._evidence),
                    (dataset_id, evidence_id, source_id, now, cutoff_at)).fetchone()[0]
                if snapshot and not active_source and isinstance(snapshot[0], list):
                    manifest = [entry for entry in snapshot[0]
                                if not isinstance(entry, dict)
                                or entry.get("source_id") != source_id]
                    if manifest != snapshot[0]:
                        metrics = connection.execute(sql.SQL("""SELECT
                            count(DISTINCT ek.keyword_id),
                            min(COALESCE(active.query_scope->>'observation_date',
                                         active.observed_at::date::text)),
                            max(COALESCE(active.query_scope->>'observation_date',
                                         active.observed_at::date::text)),
                            max(active.collected_at)
                            FROM {} de JOIN {} active ON active.id=de.evidence_id
                            LEFT JOIN {} ek ON ek.evidence_id=active.id
                            WHERE de.dataset_id=%s AND active.id<>%s
                              AND active.expires_at>%s AND active.collected_at>=%s""").format(
                                  self._dataset_evidence, self._evidence, self._evidence_keywords),
                            (dataset_id, evidence_id, now, cutoff_at)).fetchone()
                        record_count = sum(
                            entry.get("record_count", 0) for entry in manifest
                            if isinstance(entry, dict)
                            and type(entry.get("record_count", 0)) is int
                            and entry.get("record_count", 0) >= 0
                        )
                        received_count = sum(
                            entry.get("received_record_count", 0) for entry in manifest
                            if isinstance(entry, dict)
                            and type(entry.get("received_record_count", 0)) is int
                            and entry.get("received_record_count", 0) >= 0
                        )
                        flags = snapshot[1] if isinstance(snapshot[1], list) else []
                        flags = sorted(set(flags) | {"RETENTION_PRUNED_SOURCE_EVIDENCE"})
                        connection.execute(sql.SQL("""UPDATE {} SET source_manifest=%s,
                            quality_flags=%s,provider_record_count=%s,received_record_count=%s,
                            retained_keyword_count=%s,observation_start=%s,observation_end=%s,
                            fetched_at=%s WHERE dataset_id=%s""").format(self._datasets),
                            (Jsonb(manifest), Jsonb(flags), record_count, received_count,
                             metrics[0], metrics[1], metrics[2], metrics[3], dataset_id))
                        counts["dataset_summaries"] += 1
            cursor = connection.execute(sql.SQL("DELETE FROM {} WHERE evidence_id=%s")
                                        .format(self._dataset_evidence), (evidence_id,))
            counts["dataset_evidence"] += max(0, cursor.rowcount)
        cursor = connection.execute(sql.SQL("DELETE FROM {} WHERE evidence_id=%s")
                                    .format(self._evidence_keywords), (evidence_id,))
        counts["evidence_keywords"] += max(0, cursor.rowcount)
        cursor = connection.execute(sql.SQL("DELETE FROM {} WHERE id=%s")
                                    .format(self._evidence), (evidence_id,))
        return cursor.rowcount == 1, cache_budget

    def _summary(self, window, *, status, dry_run=False, run_id=None, **counts):
        defaults = {
            "payload_candidate_count": 0, "payload_deleted_count": 0,
            "payload_blocked_count": 0, "payload_has_more": False,
            "source_evidence_due_count": 0, "source_evidence_deleted_count": 0,
            "source_evidence_blocked_count": 0, "dependent_cleanup_counts": {},
            "has_more": False,
        }
        defaults.update(counts)
        return {
            "run_id": run_id, "status": status, "dry_run": dry_run,
            "business_date": window.business_date.isoformat(),
            "cutoff_at": window.cutoff_at.isoformat(), **defaults,
        }

    def sweep(self, *, now=None, dry_run=False, limit=500):
        """Run one bounded, transactional cleanup batch without provider calls."""
        now = datetime.now(timezone.utc) if now is None else now
        if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
                or type(dry_run) is not bool or type(limit) is not int
                or not 1 <= limit <= _MAX_BATCH_SIZE):
            raise ValueError("valid aware time, dry-run flag, and batch limit are required")
        now = now.astimezone(timezone.utc)
        window = rolling_retention_window(now)
        run_id = uuid.uuid4().hex
        if self._preserve_expired:
            return self._summary(window, status="PRESERVED", dry_run=dry_run)
        cache_identity = _cache_identity(self._environment, self._schema)
        if not dry_run and _cache_skip(cache_identity, now):
            return self._summary(window, status="SKIPPED_RECENT_SWEEP")

        dependent = {
            "keyword_content": 0, "keyword_evidence_fields": 0,
            "keyword_observations": 0, "algorithm_results": 0,
            "classifications": 0, "entity_context": 0,
            "embedding_cache": 0, "dataset_evidence": 0,
            "evidence_keywords": 0, "dataset_summaries": 0,
            "ai_cache": 0, "ai_recommendations": 0,
            "ai_job_results": 0, "ai_content_untracked": 0,
        }
        try:
            with postgres_connection(self._environment) as connection:
                with connection.transaction():
                    connection.execute("SELECT set_config('lock_timeout', '10s', true)")
                    connection.execute("SELECT set_config('statement_timeout', '30s', true)")
                    connection.execute("SELECT set_config('idle_in_transaction_session_timeout', '30s', true)")
                    if not dry_run:
                        # Match collection-writer lock order so a concurrent
                        # Fetch either commits before cleanup or after it.
                        collection_locked = connection.execute(
                            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))",
                            ("trendscout-collections:" + self._schema,))
                        if not collection_locked.fetchone()[0]:
                            return self._summary(window, status="SKIPPED_CONCURRENT_FETCH",
                                                 has_more=True)
                    locked = connection.execute(
                        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                        ("trendscout-retention:" + self._schema,),
                    ).fetchone()[0]
                    if not locked:
                        return self._summary(window, status="SKIPPED_CONCURRENT_SWEEP",
                                             dry_run=dry_run, has_more=True)

                    if not dry_run:
                        previous = connection.execute(sql.SQL("""SELECT finished_at,has_more
                            FROM {} WHERE dry_run=FALSE ORDER BY finished_at DESC,id DESC LIMIT 1""")
                            .format(self._audit)).fetchone()
                        if (previous is not None and now - _SWEEP_INTERVAL < previous[0] <= now
                                and not previous[1]):
                            _remember_sweep(cache_identity, previous[0], False)
                            return self._summary(window, status="SKIPPED_RECENT_SWEEP")

                    due_count = connection.execute(sql.SQL("""SELECT count(*) FROM {}
                        WHERE collected_at < %s OR expires_at <= %s""").format(self._evidence),
                        (window.cutoff_at, now)).fetchone()[0]
                    blocked_payloads = connection.execute(sql.SQL("""SELECT count(*)
                        FROM {} AS payload WHERE payload.retain_until <= %s
                          AND EXISTS (SELECT 1 FROM {} AS reference
                            WHERE reference.digest=payload.digest
                              AND (reference.retain_until IS NULL OR reference.retain_until > %s))""")
                        .format(self._payloads, self._references), (now, now)).fetchone()[0]
                    selection_limit = limit + 1 if dry_run else limit
                    lock_clause = sql.SQL("") if dry_run else sql.SQL(" FOR UPDATE SKIP LOCKED")
                    evidence_rows = connection.execute(sql.SQL("""SELECT id,source_id,query_scope,payload
                        FROM {} WHERE collected_at < %s OR expires_at <= %s
                        ORDER BY COALESCE(expires_at,collected_at),id LIMIT %s""").format(self._evidence)
                        + lock_clause, (window.cutoff_at, now, selection_limit)).fetchall()
                    evidence_has_more = len(evidence_rows) > limit if dry_run else due_count > limit
                    evidence_rows = evidence_rows[:limit]

                    payload_candidates = connection.execute(sql.SQL("""SELECT payload.digest
                        FROM {} AS payload
                        WHERE payload.retain_until <= %s
                          AND NOT EXISTS (SELECT 1 FROM {} AS reference
                            WHERE reference.digest=payload.digest
                              AND (reference.retain_until IS NULL OR reference.retain_until > %s))
                        ORDER BY payload.retain_until,payload.digest
                        LIMIT %s""").format(self._payloads, self._references),
                        (now, now, limit + 1 if dry_run else limit)).fetchall()
                    payload_has_more = len(payload_candidates) > limit if dry_run else False
                    digests = [row[0] for row in payload_candidates[:limit]]
                    payload_deleted = 0
                    source_deleted = 0
                    source_blocked = 0
                    child_cache_budget = limit
                    if not dry_run:
                        for evidence in evidence_rows:
                            deleted, child_cache_budget = self._clean_evidence(
                                connection, evidence, now=now, cutoff_at=window.cutoff_at,
                                counts=dependent, cache_budget=child_cache_budget,
                            )
                            if deleted:
                                source_deleted += 1
                            else:
                                source_blocked += 1
                        if digests:
                            connection.execute(sql.SQL("""WITH expired_references AS (
                                SELECT ctid FROM {} WHERE digest=ANY(%s) AND retain_until<=%s
                                ORDER BY digest,scope,reference_path LIMIT %s)
                                DELETE FROM {} AS reference USING expired_references
                                WHERE reference.ctid=expired_references.ctid""").format(
                                    self._references, self._references), (digests, now, limit))
                            cursor = connection.execute(sql.SQL("""DELETE FROM {} AS payload
                                WHERE payload.digest=ANY(%s) AND payload.retain_until<=%s
                                  AND NOT EXISTS (SELECT 1 FROM {} AS reference
                                    WHERE reference.digest=payload.digest)""").format(
                                        self._payloads, self._references), (digests, now))
                            payload_deleted = max(0, cursor.rowcount)
                        payload_has_more = connection.execute(sql.SQL("""SELECT EXISTS (
                            SELECT 1 FROM {} AS payload WHERE payload.retain_until<=%s
                              AND NOT EXISTS (SELECT 1 FROM {} AS reference
                                WHERE reference.digest=payload.digest
                                  AND (reference.retain_until IS NULL OR reference.retain_until>%s))
                            LIMIT 1)""").format(self._payloads, self._references),
                            (now, now)).fetchone()[0]
                        ai_counts = self._prune_ai_content(connection, now)
                        dependent.update(ai_counts)

                    cache_has_more = connection.execute(sql.SQL("""SELECT EXISTS(
                        SELECT 1 FROM {} ce JOIN {} se ON se.id=ce.evidence_id
                        WHERE (se.collected_at<%s OR se.expires_at<=%s)
                        LIMIT 1)""").format(self._cache_evidence, self._evidence),
                        (window.cutoff_at, now)).fetchone()[0] if not dry_run else False
                    has_more = bool(evidence_has_more or payload_has_more or cache_has_more)
                    status = "DRY_RUN" if dry_run else "COMPLETED"
                    connection.execute(sql.SQL("""INSERT INTO {}(
                        id,policy_version,business_timezone,business_date,cutoff_at,evaluated_at,
                        dry_run,batch_limit,status,payload_candidate_count,payload_deleted_count,
                        payload_blocked_count,payload_has_more,source_evidence_due_count,
                        source_evidence_deleted_count,source_evidence_blocked_count,
                        dependent_cleanup_counts,has_more,started_at,finished_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""")
                        .format(self._audit),
                        (run_id, window.policy_version, window.business_timezone,
                         window.business_date, window.cutoff_at, now, dry_run, limit,
                         status, len(digests), payload_deleted, blocked_payloads,
                         bool(payload_has_more), due_count, source_deleted, source_blocked,
                         Jsonb(dependent), has_more, now, now),
                    )
                    result = self._summary(
                        window, status=status, dry_run=dry_run, run_id=run_id,
                        payload_candidate_count=len(digests),
                        payload_deleted_count=payload_deleted,
                        payload_blocked_count=blocked_payloads,
                        payload_has_more=bool(payload_has_more),
                        source_evidence_due_count=due_count,
                        source_evidence_deleted_count=source_deleted,
                        source_evidence_blocked_count=source_blocked,
                        dependent_cleanup_counts=dependent, has_more=has_more,
                    )
            if not dry_run:
                _remember_sweep(cache_identity, now, has_more)
            return result
        except psycopg.Error:
            raise RuntimeError("PostgreSQL retention sweep unavailable") from None
