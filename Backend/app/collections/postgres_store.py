"""Incremental collection operations used by explicit PostgreSQL runtimes.

This store is available in opt-in Local and Vercel compositions; default runtime
cutover still waits for legacy-data reconciliation.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.collections.analysis_storage import analysis_state_digest
from app.collections.identity import fingerprint
from app.collections.postgres_reads import PostgresCollectionReader, _json_result
from app.collections.source_lineage import (
    build_source_evidence,
    remap_source_evidence_keyword_ids,
)
from app.storage.postgres_connection import PostgresConnectionError, postgres_connection


class CollectionWriteError(RuntimeError):
    """A collection mutation could not be safely completed."""


_AI_PROPOSAL_REASON_PREFIX = "TRENDSCOUT_AI_PROPOSAL_V1:"
_AI_TARGETED_RUN_PREFIX = "TRENDSCOUT_AI_TARGETED_V1:"


def _decode_ai_proposal_reason(value):
    if not isinstance(value, str) or not value.startswith(_AI_PROPOSAL_REASON_PREFIX):
        return {"reason": value if isinstance(value, str) else ""}
    try:
        payload = json.loads(value[len(_AI_PROPOSAL_REASON_PREFIX):])
    except (TypeError, ValueError):
        return {"reason": "AI generated Collection proposal"}
    return payload if isinstance(payload, dict) else {"reason": "AI generated Collection proposal"}


class PostgresAlgorithmAnalysisSession:
    """Database operations used during one atomic PostgreSQL analysis pass."""

    def __init__(self, db, table, data_mode_scope):
        self._db = db
        self._table = table
        self._data_mode_scope = data_mode_scope

    def eligible_keywords(self, *, source=None, observed_from=None, observed_to=None, ids=None):
        clauses = [sql.SQL("eligible=1")]
        parameters = []
        if self._data_mode_scope:
            clauses.append(sql.SQL("data_mode=%s"))
            parameters.append(self._data_mode_scope)
        if source:
            clauses.append(sql.SQL("source=%s"))
            parameters.append(source)
        if observed_from:
            clauses.append(sql.SQL("left(observed_at,10)>=%s"))
            parameters.append(observed_from)
        if observed_to:
            clauses.append(sql.SQL("left(observed_at,10)<=%s"))
            parameters.append(observed_to)
        if ids is not None:
            identifiers = tuple(dict.fromkeys(ids))
            if not identifiers:
                return []
            clauses.append(sql.SQL("id=ANY(%s)"))
            parameters.append(list(identifiers))
        rows = self._db.execute(sql.SQL("SELECT * FROM {} WHERE {} ORDER BY id")
                                .format(self._table("keywords"), sql.SQL(" AND ").join(clauses)),
                                parameters).fetchall()
        records = [dict(row) for row in rows]
        for record in records:
            evidence = record["evidence"]
            if not isinstance(evidence, str):
                record["evidence"] = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        return records

    def upsert_observations(self, observations):
        if not observations:
            return
        with self._db.cursor() as cursor:
            cursor.executemany(sql.SQL("""INSERT INTO {}(keyword_id,source,observation_date,
                country,access,project,metric,unit,status,value,rank,snapshot_status,input_digest)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(keyword_id,source,observation_date,metric,unit) DO UPDATE SET
                value=EXCLUDED.value,rank=EXCLUDED.rank,status=EXCLUDED.status,
                country=EXCLUDED.country,access=EXCLUDED.access,project=EXCLUDED.project,
                snapshot_status=EXCLUDED.snapshot_status,input_digest=EXCLUDED.input_digest
                WHERE algorithm_observations.value IS DISTINCT FROM EXCLUDED.value
                   OR algorithm_observations.rank IS DISTINCT FROM EXCLUDED.rank
                   OR algorithm_observations.status IS DISTINCT FROM EXCLUDED.status
                   OR algorithm_observations.country IS DISTINCT FROM EXCLUDED.country
                   OR algorithm_observations.access IS DISTINCT FROM EXCLUDED.access
                   OR algorithm_observations.project IS DISTINCT FROM EXCLUDED.project
                   OR algorithm_observations.snapshot_status IS DISTINCT FROM EXCLUDED.snapshot_status
                   OR algorithm_observations.input_digest IS DISTINCT FROM EXCLUDED.input_digest""")
                .format(self._table("algorithm_observations")), observations)

    def state_digest(self, records, metadata_by_key):
        scope = sql.SQL("AND k.data_mode=%s") if self._data_mode_scope else sql.SQL("")
        parameters = (self._data_mode_scope,) if self._data_mode_scope else ()
        observations = self._db.execute(sql.SQL("""SELECT o.* FROM {} o JOIN {} k ON k.id=o.keyword_id
            WHERE k.eligible=1 {} ORDER BY o.keyword_id,o.source,o.observation_date,o.metric,o.unit""")
            .format(self._table("algorithm_observations"), self._table("keywords"), scope),
            parameters).fetchall()
        results = self._db.execute(sql.SQL("""SELECT r.keyword_id,r.algorithm_id,r.version,r.status,
            r.reason_code,r.value,r.metric,r.unit,r.result_json,r.input_digest
            FROM {} r JOIN {} k ON k.id=r.keyword_id WHERE k.eligible=1 {}
            ORDER BY r.keyword_id,r.algorithm_id""").format(
                self._table("algorithm_results"), self._table("keywords"), scope), parameters).fetchall()
        return analysis_state_digest(records, metadata_by_key, observations, results)

    def setting(self, key):
        row = self._db.execute(sql.SQL("SELECT value FROM {} WHERE key=%s")
                               .format(self._table("settings")), (key,)).fetchone()
        return row["value"] if row else None

    def history(self, keyword_id, source, before_date, metric, unit, *, after_date=None,
                country=None, access=None, project=None):
        clauses = [sql.SQL("keyword_id=%s"), sql.SQL("source=%s"),
                   sql.SQL("observation_date<%s"), sql.SQL("metric=%s"), sql.SQL("unit=%s")]
        parameters = [keyword_id, source, before_date, metric, unit]
        if after_date:
            clauses.append(sql.SQL("observation_date>=%s"))
            parameters.append(after_date)
        for column, value in (("country", country), ("access", access), ("project", project)):
            if value is not None:
                clauses.append(sql.SQL("{}=%s").format(sql.Identifier(column)))
                parameters.append(value)
        return self._db.execute(sql.SQL("""SELECT observation_date,status,value,snapshot_status,input_digest
            FROM {} WHERE {} ORDER BY observation_date""")
            .format(self._table("algorithm_observations"), sql.SQL(" AND ").join(clauses)),
            parameters).fetchall()

    def histories(self, keyword_ids, source, before_date, metric, unit, *, after_date=None,
                  country=None, access=None, project=None):
        identifiers = list(dict.fromkeys(keyword_ids))
        if not identifiers:
            return []
        clauses = [sql.SQL("keyword_id=ANY(%s)"), sql.SQL("source=%s"),
                   sql.SQL("observation_date<%s"), sql.SQL("metric=%s"), sql.SQL("unit=%s")]
        parameters = [identifiers, source, before_date, metric, unit]
        if after_date:
            clauses.append(sql.SQL("observation_date>=%s"))
            parameters.append(after_date)
        for column, value in (("country", country), ("access", access), ("project", project)):
            if value is not None:
                clauses.append(sql.SQL("{}=%s").format(sql.Identifier(column)))
                parameters.append(value)
        return self._db.execute(sql.SQL("""SELECT keyword_id,observation_date,status,value,snapshot_status,
            input_digest FROM {} WHERE {} ORDER BY keyword_id,observation_date""")
            .format(self._table("algorithm_observations"), sql.SQL(" AND ").join(clauses)),
            parameters).fetchall()

    def result_digests(self, keyword_id):
        rows = self._db.execute(sql.SQL("SELECT algorithm_id,input_digest FROM {} WHERE keyword_id=%s")
                                .format(self._table("algorithm_results")), (keyword_id,)).fetchall()
        return {row["algorithm_id"]: row["input_digest"] for row in rows}

    def result_digests_for(self, keyword_ids):
        identifiers = list(dict.fromkeys(keyword_ids))
        if not identifiers:
            return {}
        rows = self._db.execute(sql.SQL("SELECT keyword_id,algorithm_id,input_digest FROM {} "
                                        "WHERE keyword_id=ANY(%s)")
                                .format(self._table("algorithm_results")), (identifiers,)).fetchall()
        result = {}
        for row in rows:
            result.setdefault(row["keyword_id"], {})[row["algorithm_id"]] = row["input_digest"]
        return result

    def result_payloads_for(self, keyword_ids, algorithm_id):
        identifiers = list(dict.fromkeys(keyword_ids))
        if not identifiers:
            return {}
        rows = self._db.execute(sql.SQL("SELECT keyword_id,result_json FROM {} "
                                        "WHERE keyword_id=ANY(%s) AND algorithm_id=%s")
                                .format(self._table("algorithm_results")),
                                (identifiers, algorithm_id)).fetchall()
        return {row["keyword_id"]: row["result_json"] for row in rows}

    def upsert_results(self, results):
        if not results:
            return
        with self._db.cursor() as cursor:
            cursor.executemany(sql.SQL("""INSERT INTO {}(keyword_id,algorithm_id,version,status,
                reason_code,value,metric,unit,result_json,input_digest,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(keyword_id,algorithm_id) DO UPDATE SET version=EXCLUDED.version,
                status=EXCLUDED.status,reason_code=EXCLUDED.reason_code,value=EXCLUDED.value,
                metric=EXCLUDED.metric,unit=EXCLUDED.unit,result_json=EXCLUDED.result_json,
                input_digest=EXCLUDED.input_digest,updated_at=EXCLUDED.updated_at""")
                .format(self._table("algorithm_results")), results)

    def set_setting(self, key, value):
        self._db.execute(sql.SQL("""INSERT INTO {}(key,value) VALUES(%s,%s)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""")
            .format(self._table("settings")), (key, value))

    def delete_setting(self, key):
        self._db.execute(sql.SQL("DELETE FROM {} WHERE key=%s").format(self._table("settings")), (key,))


class PostgresCollectionStore(PostgresCollectionReader):
    supports_source_evidence_lineage = True
    supports_incremental_fetch_persistence = True

    def _write(self, operation, *, serialize=True):
        try:
            with postgres_connection(self._environment) as db:
                db.row_factory = dict_row
                with db.transaction():
                    db.execute("""SELECT set_config('lock_timeout', '10s', true),
                        set_config('statement_timeout', '30s', true),
                        set_config('idle_in_transaction_session_timeout', '30s', true)""")
                    if serialize:
                        # Collection-wide reads and mutations use one lock until
                        # their narrower ownership boundaries are fully audited.
                        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                                   ("trendscout-collections:" + self._schema,))
                    return operation(db)
        except (psycopg.Error, PostgresConnectionError) as exc:
            state = getattr(exc, "sqlstate", None)
            category = state or (
                "connection" if isinstance(exc, PostgresConnectionError) else type(exc).__name__
            )
            raise CollectionWriteError(f"collection database mutation failed ({category})") from None

    def set_setting(self, key, value):
        return self._write(lambda db: db.execute(sql.SQL("""INSERT INTO {}(key,value) VALUES (%s,%s)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""").format(self._table("settings")),
            (key, value)))

    def entity_context_rows(self, keyword_ids):
        identifiers = list(dict.fromkeys(keyword_ids))
        if not identifiers:
            return []
        rows = self._read(lambda db: db.execute(
            sql.SQL("SELECT * FROM {} WHERE keyword_id=ANY(%s)").format(self._table("entity_context")),
            (identifiers,),
        ).fetchall())
        for row in rows:
            for field in ("occupations", "category_ids", "instance_types"):
                row[field] = json.dumps(row[field], ensure_ascii=False)
            captured_at = row.get("captured_at")
            if isinstance(captured_at, datetime):
                row["captured_at"] = captured_at.astimezone(timezone.utc).isoformat()
        return rows

    def entity_context_keyword_ids(self, keyword_ids, context_version):
        identifiers = list(dict.fromkeys(keyword_ids))
        if not identifiers:
            return set()
        rows = self._read(lambda db: db.execute(sql.SQL("""SELECT keyword_id FROM {}
            WHERE context_version=%s AND keyword_id=ANY(%s)""").format(self._table("entity_context")),
            (context_version, identifiers)).fetchall())
        return {row["keyword_id"] for row in rows}

    def upsert_entity_context(self, rows):
        if not rows:
            return
        lineage = build_source_evidence([], entity_context_rows=rows)
        columns = ("keyword_id", "title", "page_id", "entity_id", "description", "page_namespace",
                   "is_disambiguation", "redirected_from", "page_language", "is_main_page",
                   "is_main_page_known", "entity_type", "occupations", "category_ids", "instance_types",
                   "source_url", "method", "status", "context_version", "captured_at")
        values = []
        for row in rows:
            item = list(row)
            for index in (12, 13, 14):
                item[index] = Jsonb(json.loads(item[index]))
            timestamp = item[19]
            item[19] = (datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                         if isinstance(timestamp, str) else timestamp)
            values.append(tuple(item))

        def write(db):
            with db.cursor() as cursor:
                cursor.executemany(sql.SQL("""INSERT INTO {}({}) VALUES ({})
                    ON CONFLICT(keyword_id) DO UPDATE SET title=EXCLUDED.title,page_id=EXCLUDED.page_id,
                    entity_id=EXCLUDED.entity_id,description=EXCLUDED.description,
                    page_namespace=EXCLUDED.page_namespace,is_disambiguation=EXCLUDED.is_disambiguation,
                    redirected_from=EXCLUDED.redirected_from,page_language=EXCLUDED.page_language,
                    is_main_page=EXCLUDED.is_main_page,is_main_page_known=EXCLUDED.is_main_page_known,
                    entity_type=EXCLUDED.entity_type,occupations=EXCLUDED.occupations,
                    category_ids=EXCLUDED.category_ids,instance_types=EXCLUDED.instance_types,
                    source_url=EXCLUDED.source_url,method=EXCLUDED.method,status=EXCLUDED.status,
                    context_version=EXCLUDED.context_version,captured_at=EXCLUDED.captured_at""")
                    .format(self._table("entity_context"), sql.SQL(",").join(map(sql.Identifier, columns)),
                            sql.SQL(",").join(sql.Placeholder() for _ in columns)), values)
            self._persist_source_evidence(db, None, lineage["rows"])

        return self._write(write)

    def create_research(self, record):
        def operation(db):
            if record.get("keyword_id"):
                scope = sql.SQL("AND data_mode=%s") if self.data_mode_scope else sql.SQL("")
                parameters = ((record["keyword_id"], self.data_mode_scope) if self.data_mode_scope
                              else (record["keyword_id"],))
                keyword = db.execute(sql.SQL("SELECT 1 FROM {} WHERE id=%s ").format(
                    self._table("keywords")) + scope, parameters).fetchone()
                if keyword is None:
                    raise ValueError("RESEARCH_KEYWORD_NOT_FOUND")
            columns = ("id", "keyword_id", "subject", "hypothesis", "product_category", "niche",
                       "competitor_notes", "market_evidence", "ip_notes", "risk_notes", "findings",
                       "status", "created_at", "updated_at")
            db.execute(sql.SQL("INSERT INTO {}({}) VALUES ({})").format(
                self._table("research_records"),
                sql.SQL(",").join(map(sql.Identifier, columns)),
                sql.SQL(",").join(sql.Placeholder() for _ in columns)),
                tuple(record[key] for key in columns))
            return {key: record[key] for key in ("id", "status", "created_at", "updated_at")}
        return self._write(operation)

    def update_research(self, identifier, updates, updated_at):
        allowed = {"subject", "hypothesis", "product_category", "niche", "competitor_notes",
                   "market_evidence", "ip_notes", "risk_notes", "findings", "status"}
        values = {key: value for key, value in updates.items() if key in allowed}
        if not values:
            return None

        def operation(db):
            assignments = sql.SQL(",").join(
                sql.SQL("{}=%s").format(sql.Identifier(key)) for key in values
            )
            row = db.execute(sql.SQL("UPDATE {} SET {},updated_at=%s WHERE id=%s "
                "RETURNING id,status,updated_at").format(self._table("research_records"), assignments),
                (*values.values(), updated_at, identifier)).fetchone()
            return _json_result(row) if row else None
        return self._write(operation)

    def run_algorithm_analysis(self, operation):
        """Run algorithm reads and writes inside one serialized transaction."""
        return self._write(lambda db: operation(PostgresAlgorithmAnalysisSession(
            db, self._table, self.data_mode_scope)))

    def persist_cleaned_keywords(self, records, *, rejected=(), bing_updates=(),
                                 source_evidence_rows=None):
        """Persist a bounded Fetch ingestion batch with native set-based SQL.

        The caller supplies records already validated by the existing source
        cleaning rules. Identity conflicts across LIVE and SYNTHETIC scopes are
        disambiguated before any upsert, matching the SQLite service contract.
        """
        records = [dict(record) for record in records]
        rejected = tuple((str(identifier), str(reason)) for identifier, reason in rejected)
        bing_updates = tuple(dict(update) for update in bing_updates)
        required = {"id", "keyword", "normalized", "source", "project", "observed_at",
                    "data_mode", "evidence", "context"}
        if any(not required.issubset(record) for record in records):
            raise ValueError("cleaned keyword record is incomplete")
        if (self.data_mode_scope
                and any(record["data_mode"] != self.data_mode_scope for record in records)):
            raise ValueError("cleaned keyword record is outside the store's data mode scope")

        def operation(db):
            persisted_source_evidence_ids = ()
            identifiers = list(dict.fromkeys(record["id"] for record in records))
            existing_by_id = {}
            id_remap = {}
            if identifiers:
                rows = db.execute(sql.SQL("""SELECT id,data_mode,keyword,observed_at,evidence,context
                    FROM {} WHERE id=ANY(%s)""").format(self._table("keywords")),
                    (identifiers,)).fetchall()
                existing_by_id = {row["id"]: row for row in rows}
            if self.data_mode_scope and identifiers:
                for record in records:
                    existing = existing_by_id.get(record["id"])
                    if existing is not None and existing["data_mode"] != self.data_mode_scope:
                        original_id = record["id"]
                        record["id"] = hashlib.sha256(
                            f"{self.data_mode_scope}:{record['id']}".encode("utf-8")
                        ).hexdigest()
                        id_remap[original_id] = record["id"]
                if id_remap:
                    resolved_ids = list(dict.fromkeys(record["id"] for record in records))
                    collisions = db.execute(sql.SQL("""SELECT id,data_mode,keyword,observed_at,
                        evidence,context FROM {} WHERE id=ANY(%s)""").format(
                            self._table("keywords")), (resolved_ids,)).fetchall()
                    if any(row["data_mode"] != self.data_mode_scope for row in collisions):
                        raise CollectionWriteError("cross-mode keyword identity collision")
                    existing_by_id.update({row["id"]: row for row in collisions})

            if rejected:
                rejected_by_id = {}
                for identifier, reason in rejected:
                    rejected_by_id[identifier] = reason
                pairs = list(rejected_by_id.items())
                for offset in range(0, len(pairs), 500):
                    batch = pairs[offset:offset + 500]
                    values = sql.SQL(",").join(sql.SQL("(%s,%s)") for _ in batch)
                    params = tuple(value for pair in batch for value in pair)
                    db.execute(sql.SQL("""UPDATE {} AS k SET eligible=0,rejection_reason=v.reason
                        FROM (VALUES {}) AS v(id,reason) WHERE k.id=v.id {}""").format(
                            self._table("keywords"), values,
                            sql.SQL("AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")),
                        params + ((self.data_mode_scope,) if self.data_mode_scope else ()))

            # PostgreSQL rejects an INSERT ... ON CONFLICT statement that names
            # the same key twice. Keep the last source occurrence, as the
            # legacy ordered SQLite upserts did, and bound each statement.
            last_by_id = {}
            for record in records:
                last_by_id[record["id"]] = record
            keyword_stats = {
                "keyword_new_rows": 0,
                "keyword_content_changed_rows": 0,
                "keyword_observation_only_rows": 0,
                "keyword_unchanged_rows": 0,
                "keyword_new_observation_rows": 0,
            }
            upsert_records = []
            evidence_objects = {}
            for identifier, record in last_by_id.items():
                evidence = record["evidence"]
                if isinstance(evidence, str):
                    def reject_constant(_value):
                        raise ValueError("non-finite JSON number")
                    evidence = json.loads(evidence, parse_constant=reject_constant)
                if not isinstance(evidence, dict):
                    raise ValueError("keyword evidence must be a JSON object")
                json.dumps(evidence, ensure_ascii=False, allow_nan=False)
                evidence_objects[identifier] = evidence

                existing = existing_by_id.get(identifier)
                if existing is None:
                    keyword_stats["keyword_new_rows"] += 1
                    upsert_records.append(record)
                    continue
                if existing["observed_at"] != record["observed_at"]:
                    keyword_stats["keyword_new_observation_rows"] += 1
                content_changed = (
                    existing["keyword"] != record["keyword"]
                    or existing["evidence"] != evidence
                    or existing["context"] != record["context"]
                )
                if content_changed:
                    keyword_stats["keyword_content_changed_rows"] += 1
                    upsert_records.append(record)
                elif existing["observed_at"] != record["observed_at"]:
                    keyword_stats["keyword_observation_only_rows"] += 1
                    upsert_records.append(record)
                else:
                    keyword_stats["keyword_unchanged_rows"] += 1

            columns = ("id", "keyword", "normalized", "source", "project", "observed_at",
                       "data_mode", "evidence", "context")
            for offset in range(0, len(upsert_records), 500):
                batch = upsert_records[offset:offset + 500]
                values = sql.SQL(",").join(
                    sql.SQL("(" + ",".join(["%s"] * len(columns)) + ")") for _ in batch
                )
                params = []
                for record in batch:
                    params.extend(record[key] for key in columns[:7])
                    params.extend((Jsonb(evidence_objects[record["id"]]), record["context"]))
                db.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,observed_at,
                    data_mode,evidence,context) VALUES {} ON CONFLICT(id) DO UPDATE SET
                    keyword=EXCLUDED.keyword,observed_at=EXCLUDED.observed_at,evidence=EXCLUDED.evidence,
                    context=EXCLUDED.context,
                    eligible=COALESCE((SELECT CASE decision WHEN 'EXCLUDED' THEN 0 ELSE 1 END
                        FROM {} WHERE keyword_id=EXCLUDED.id),1),
                    rejection_reason=CASE WHEN (SELECT decision FROM {} WHERE keyword_id=EXCLUDED.id)
                        ='EXCLUDED' THEN 'USER_EXCLUDED' ELSE NULL END
                    WHERE {}.keyword IS DISTINCT FROM EXCLUDED.keyword
                       OR {}.observed_at IS DISTINCT FROM EXCLUDED.observed_at
                       OR {}.evidence IS DISTINCT FROM EXCLUDED.evidence
                       OR {}.context IS DISTINCT FROM EXCLUDED.context
                       OR {}.eligible IS DISTINCT FROM COALESCE((SELECT CASE decision
                            WHEN 'EXCLUDED' THEN 0 ELSE 1 END FROM {} WHERE keyword_id=EXCLUDED.id),1)
                       OR {}.rejection_reason IS DISTINCT FROM CASE WHEN (SELECT decision
                            FROM {} WHERE keyword_id=EXCLUDED.id)='EXCLUDED'
                            THEN 'USER_EXCLUDED' ELSE NULL END""").format(
                                self._table("keywords"), values, self._table("keyword_decisions"),
                                self._table("keyword_decisions"), self._table("keywords"),
                                self._table("keywords"), self._table("keywords"),
                                self._table("keywords"),
                                self._table("keywords"), self._table("keyword_decisions"),
                                self._table("keywords"), self._table("keyword_decisions")), params)

            skipped_bing = 0
            original_candidate_ids = list(dict.fromkeys(
                update.get("candidate_id") for update in bing_updates
                if isinstance(update.get("candidate_id"), str)
            ))
            if self.data_mode_scope and original_candidate_ids:
                mapped_candidates = {
                    identifier: hashlib.sha256(
                        f"{self.data_mode_scope}:{identifier}".encode("utf-8")
                    ).hexdigest()
                    for identifier in original_candidate_ids
                }
                lookup_ids = list(dict.fromkeys([
                    *original_candidate_ids, *mapped_candidates.values(),
                ]))
                rows = db.execute(sql.SQL("SELECT id,data_mode FROM {} WHERE id=ANY(%s)")
                                  .format(self._table("keywords")), (lookup_ids,)).fetchall()
                modes_by_id = {row["id"]: row["data_mode"] for row in rows}
                for identifier, mapped_id in mapped_candidates.items():
                    if (modes_by_id.get(mapped_id) == self.data_mode_scope
                            and modes_by_id.get(identifier) != self.data_mode_scope):
                        id_remap[identifier] = mapped_id
            for update in bing_updates:
                candidate_id = update.get("candidate_id")
                if isinstance(candidate_id, str):
                    update["candidate_id"] = id_remap.get(candidate_id, candidate_id)
            candidate_ids = list(dict.fromkeys(
                update.get("candidate_id") for update in bing_updates
                if isinstance(update.get("candidate_id"), str)
            ))
            candidate_evidence = {}
            updated_candidate_ids = set()
            if candidate_ids:
                rows = db.execute(sql.SQL("""SELECT id,observed_at,evidence FROM {}
                    WHERE id=ANY(%s) AND source='google_web_serp'
                      AND project LIKE 'google_trends:%%' AND data_mode='LIVE' AND eligible=1""")
                    .format(self._table("keywords")), (candidate_ids,)).fetchall()
                candidate_evidence = {
                    row["id"]: {"observed_at": row["observed_at"], "evidence": row["evidence"]}
                    for row in rows
                }
            for update in bing_updates:
                candidate = candidate_evidence.get(update.get("candidate_id"))
                if candidate is None:
                    skipped_bing += 1
                    continue
                try:
                    observed = datetime.fromisoformat(candidate["observed_at"].replace("Z", "+00:00"))
                    evidence = candidate["evidence"]
                except (TypeError, ValueError, AttributeError):
                    skipped_bing += 1
                    continue
                if (observed.tzinfo is None or observed.utcoffset() is None
                        or observed.astimezone(timezone.utc).isoformat() != update.get("observed_at")
                        or not isinstance(evidence, dict)
                        or evidence.get("canonical_keyword_identity") != update.get("identity")):
                    skipped_bing += 1
                    continue
                history = evidence.get("bing_verification_history", [])
                verification = update.get("verification")
                if (not isinstance(history, list) or len(history) > 20
                        or not isinstance(verification, dict)):
                    skipped_bing += 1
                    continue
                fetched_at = verification.get("fetched_at")
                query_fingerprint = verification.get("query_fingerprint")
                duplicate = any(isinstance(item, dict)
                                and item.get("fetched_at") == fetched_at
                                and item.get("query_fingerprint") == query_fingerprint
                                for item in history)
                if not duplicate:
                    history = [*history, verification][-20:]
                evidence["bing_verification_history"] = history
                evidence["bing_verification"] = verification
                candidate["evidence"] = evidence
                updated_candidate_ids.add(update["candidate_id"])

            updates = [(identifier, candidate["evidence"])
                       for identifier, candidate in candidate_evidence.items()
                       if identifier in updated_candidate_ids]
            for offset in range(0, len(updates), 500):
                batch = updates[offset:offset + 500]
                values = sql.SQL(",").join(sql.SQL("(%s,%s)") for _ in batch)
                params = []
                for identifier, evidence in batch:
                    json.dumps(evidence, ensure_ascii=False, allow_nan=False)
                    params.extend((identifier, Jsonb(evidence)))
                db.execute(sql.SQL("""UPDATE {} AS k SET evidence=v.evidence
                    FROM (VALUES {}) AS v(id,evidence) WHERE k.id=v.id
                      AND k.source='google_web_serp' AND k.data_mode='LIVE'""").format(
                          self._table("keywords"), values), params)
            source_evidence_stats = {
                "source_evidence_new_rows": 0,
                "source_evidence_updated_rows": 0,
                "source_evidence_existing_rows": 0,
                "evidence_keyword_links_inserted": 0,
                "dataset_evidence_links_inserted": 0,
            }
            if source_evidence_rows:
                remapped_evidence_rows = remap_source_evidence_keyword_ids(
                    source_evidence_rows, id_remap,
                )
                source_evidence_stats = self._persist_source_evidence(
                    db, None, remapped_evidence_rows,
                )
                persisted_source_evidence_ids = tuple(
                    row["id"] for row in remapped_evidence_rows
                    if isinstance(row, dict) and isinstance(row.get("id"), str)
                )
            return {
                "records": records,
                "skipped_bing_updates": skipped_bing,
                "id_remap": id_remap,
                "source_evidence_ids": persisted_source_evidence_ids,
                "persistence_stats": {**keyword_stats, **source_evidence_stats},
            }

        return self._write(operation)

    def persist_classifications(self, classification_records):
        result_columns = ("entity_type", "entity_id", "entity_description", "topic_category_ids",
                          "subcategory_ids", "classification_status", "confidence_level",
                          "commercial_relevance", "classification_method", "classification_version",
                          "taxonomy_version", "rule_evidence", "source_evidence", "review_status",
                          "classified_at", "classification_reason", "classification_input_digest")
        json_columns = {"topic_category_ids", "subcategory_ids", "rule_evidence", "source_evidence"}

        def operation(db):
            upsert = sql.SQL("""INSERT INTO {}(keyword_id,entity_type,entity_id,entity_description,
                topic_category_ids,subcategory_ids,classification_status,confidence_level,commercial_relevance,
                classification_method,classification_version,taxonomy_version,rule_evidence,source_evidence,
                review_status,classified_at,classification_reason,classification_input_digest)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(keyword_id) DO UPDATE SET entity_type=EXCLUDED.entity_type,
                entity_id=EXCLUDED.entity_id,entity_description=EXCLUDED.entity_description,
                topic_category_ids=EXCLUDED.topic_category_ids,subcategory_ids=EXCLUDED.subcategory_ids,
                classification_status=EXCLUDED.classification_status,confidence_level=EXCLUDED.confidence_level,
                commercial_relevance=EXCLUDED.commercial_relevance,classification_method=EXCLUDED.classification_method,
                classification_version=EXCLUDED.classification_version,taxonomy_version=EXCLUDED.taxonomy_version,
                rule_evidence=EXCLUDED.rule_evidence,source_evidence=EXCLUDED.source_evidence,
                review_status=EXCLUDED.review_status,classified_at=EXCLUDED.classified_at,
                classification_reason=EXCLUDED.classification_reason,
                classification_input_digest=EXCLUDED.classification_input_digest""").format(
                    self._table("classification_results"))
            for offset in range(0, len(classification_records), 500):
                batch = classification_records[offset:offset + 500]
                values = []
                for item in batch:
                    result = item["classification_result"]
                    values.append((item["id"], *(
                        Jsonb(result[key]) if key in json_columns
                        else result.get(key) if key == "classification_input_digest"
                        else result[key]
                        for key in result_columns
                    )))
                with db.cursor() as cursor:
                    cursor.executemany(upsert, values)

            identifiers = list(dict.fromkeys(item["id"] for item in classification_records))
            decisions = {}
            for offset in range(0, len(identifiers), 500):
                batch = identifiers[offset:offset + 500]
                rows = db.execute(sql.SQL("SELECT keyword_id,decision FROM {} WHERE keyword_id=ANY(%s)")
                                  .format(self._table("keyword_decisions")), (batch,)).fetchall()
                decisions.update({row["keyword_id"]: row["decision"] for row in rows})

            updates = []
            for item in classification_records:
                decision = decisions.get(item["id"])
                result = item["classification_result"]
                if decision == "EXCLUDED":
                    item["eligible"] = 0
                    updates.append((item["id"], 0, "USER_EXCLUDED"))
                elif result["classification_status"] == "INELIGIBLE":
                    item["eligible"] = 0
                    item["entity_category_ids"] = ()
                    updates.append((item["id"], 0, "CLASSIFICATION_INELIGIBLE"))
                elif item.get("rejection_reason") == "CLASSIFICATION_INELIGIBLE":
                    item["eligible"] = 1
                    item["entity_category_ids"] = tuple(result["topic_category_ids"])
                    updates.append((item["id"], 1, None))

            for offset in range(0, len(updates), 500):
                batch = updates[offset:offset + 500]
                values = sql.SQL(",").join(sql.SQL("(%s,%s,%s)") for _ in batch)
                params = [value for update in batch for value in update]
                db.execute(sql.SQL("UPDATE {} AS k SET eligible=v.eligible,rejection_reason=v.rejection_reason "
                    "FROM (VALUES {}) AS v(id,eligible,rejection_reason) WHERE k.id=v.id")
                    .format(self._table("keywords"), values), params)
            scope = sql.SQL("AND data_mode=%s") if self.data_mode_scope else sql.SQL("")
            parameters = (self.data_mode_scope,) if self.data_mode_scope else ()
            return db.execute(sql.SQL("SELECT count(*) AS total FROM {} WHERE eligible=1 {}")
                              .format(self._table("keywords"), scope), parameters).fetchone()["total"]

        return self._write(operation)

    def begin_fetch_processing(self, fetch_id, sources, *, cached_context_only=False,
                               resume_model_unavailable=False):
        from app.collections.source_digest import reusable_collection_input

        def operation(db):
            prior = db.execute(sql.SQL("SELECT * FROM {} WHERE fetch_id=%s FOR UPDATE")
                               .format(self._table("fetch_jobs")), (fetch_id,)).fetchone()
            if (resume_model_unavailable and prior
                    and prior["status"] == "PROCESSING"):
                result = dict(prior)
                if isinstance(result.get("updated_at"), datetime):
                    result["updated_at"] = result["updated_at"].astimezone(timezone.utc).isoformat()
                return {"action": "RETURN", "result": result}
            retry_missing_snapshot = False
            retry_existing_snapshot = False
            replay_available = replay_skipped = 0
            if prior and self.data_mode_scope:
                snapshot = db.execute(sql.SQL("""SELECT data_mode,processing_status,
                    materialization_status,
                    received_record_count,rejected_record_count FROM {} WHERE dataset_id=%s FOR UPDATE""")
                    .format(self._table("dataset_snapshots")), (fetch_id,)).fetchone()
                if snapshot and snapshot["data_mode"] != self.data_mode_scope:
                    return {"action": "RETURN", "result": {
                        "fetch_id": fetch_id, "status": "FAILED", "error_code": "FETCH_ID_MODE_CONFLICT",
                    }}
                if (snapshot and prior["status"] in {"COMPLETED", "MODEL_UNAVAILABLE"}
                        and snapshot["materialization_status"] == "COMPLETED"
                        and snapshot["processing_status"] in {"COMPLETED", "MODEL_UNAVAILABLE"}
                        and (not sources or resume_model_unavailable)):
                    retry_existing_snapshot = True
                    replay_available = snapshot["received_record_count"]
                    replay_skipped = snapshot["rejected_record_count"]
                if not snapshot:
                    replayable_sources = [source for source in sources
                        if source.get("data_mode") == self.data_mode_scope
                        and source.get("status") in {"SUCCESS", "PARTIAL", "FAILED", "UNAVAILABLE"}
                        and isinstance(source.get("records"), list)]
                    if (prior["status"] not in {"FAILED", "MODEL_UNAVAILABLE"}
                            or not replayable_sources):
                        return {"action": "RETURN", "result": {
                            "fetch_id": fetch_id, "status": "FAILED",
                            "error_code": "FETCH_ID_MODE_CONFLICT",
                        }}
                    retry_missing_snapshot = True
            if ((prior and prior["status"] == "COMPLETED" and not retry_existing_snapshot)
                    or (prior and prior["status"] == "MODEL_UNAVAILABLE"
                        and not (retry_missing_snapshot or retry_existing_snapshot))):
                result = dict(prior)
                if isinstance(result.get("updated_at"), datetime):
                    result["updated_at"] = result["updated_at"].astimezone(timezone.utc).isoformat()
                return {"action": "RETURN", "result": result}
            if not prior and cached_context_only and self.data_mode_scope == "LIVE":
                latest = db.execute(sql.SQL("""SELECT dataset_id,source_manifest,processing_status,
                    materialization_status,
                    received_record_count,rejected_record_count,eligible_keyword_count,
                    unassigned_keyword_count FROM {} WHERE data_mode='LIVE'
                    ORDER BY created_at DESC,dataset_id DESC LIMIT 1""")
                    .format(self._table("dataset_snapshots"))).fetchone()
                if latest is not None:
                    previous_manifest = latest["source_manifest"]
                    if isinstance(previous_manifest, str):
                        try:
                            previous_manifest = json.loads(previous_manifest)
                        except (TypeError, ValueError):
                            previous_manifest = None
                    if reusable_collection_input(
                            sources, previous_manifest,
                            processing_status=latest["processing_status"],
                            materialization_status=latest["materialization_status"]):
                        return {"action": "RETURN", "result": {
                            "fetch_id": fetch_id, "status": "REUSED", "dataset_reused": True,
                            "dataset_id": latest["dataset_id"],
                            "available": latest["received_record_count"],
                            "skipped": latest["rejected_record_count"],
                            "eligible": latest["eligible_keyword_count"],
                            "unassigned": latest["unassigned_keyword_count"],
                        }}
            stamp = datetime.now(timezone.utc)
            db.execute(sql.SQL("""INSERT INTO {}(fetch_id,status,stage,updated_at)
                VALUES (%s,'PROCESSING','CLEANING',%s) ON CONFLICT(fetch_id) DO UPDATE SET
                status='PROCESSING',available=0,processed=0,assigned=0,unassigned=0,
                created_collections=0,suggestions=0,skipped=0,error_code=NULL,updated_at=EXCLUDED.updated_at,
                stage='CLEANING',eligible=0,cached_embeddings=0,inferred_embeddings=0,
                membership_reconciliation_id=NULL""").format(self._table("fetch_jobs")), (fetch_id, stamp))
            return {"action": "PROCESS", "retry_missing_snapshot": retry_missing_snapshot,
                    "retry_existing_snapshot": retry_existing_snapshot,
                    "replay_available": replay_available, "replay_skipped": replay_skipped}

        return self._write(operation)

    def get_embedding_cache(self, keys):
        requested = {key: (model_version, collection_version, text_hash)
                     for key, model_version, collection_version, text_hash in keys}
        identifiers = list(requested)
        if not identifiers:
            return {}
        found = {}
        for offset in range(0, len(identifiers), 500):
            batch = identifiers[offset:offset + 500]
            def read(db):
                return db.execute(sql.SQL("SELECT fingerprint,model_version,collection_version,text_hash,vector,dimension "
                                          "FROM {} WHERE fingerprint=ANY(%s)")
                                  .format(self._table("cache")), (batch,)).fetchall()

            rows = self._read(read)
            for row in rows:
                if (row["model_version"], row["collection_version"], row["text_hash"]) == requested[row["fingerprint"]]:
                    found[row["fingerprint"]] = {
                        "vector": bytes(row["vector"]), "dimension": row["dimension"],
                    }
        return found

    def upsert_embedding_cache(self, entries, *, evidence_ids_by_fingerprint=None):
        """Persist derived vectors and optional source-evidence expiry lineage.

        PostgreSQL retention can invalidate a vector only when the producer
        supplies the evidence IDs used to build it. Unlinked legacy vectors
        are preserved until their owner can provide lineage.
        """
        entries = list(entries)
        lineage = evidence_ids_by_fingerprint or {}
        if (not isinstance(lineage, dict)
                or any(not isinstance(key, str) or not isinstance(values, (list, tuple, set))
                       or not values or len(values) > 100
                       or any(not isinstance(value, str) or not value for value in values)
                       for key, values in lineage.items())):
            raise ValueError("embedding-cache source evidence lineage is invalid")

        def operation(db):
            for offset in range(0, len(entries), 500):
                batch = entries[offset:offset + 500]
                if not batch:
                    continue
                values = sql.SQL(",").join(sql.SQL("(%s,%s,%s,%s,%s,%s)") for _ in batch)
                params = [value for entry in batch for value in entry]
                db.execute(sql.SQL("""INSERT INTO {}(fingerprint,model_version,collection_version,
                    text_hash,vector,dimension) VALUES {} ON CONFLICT(fingerprint) DO UPDATE SET
                    model_version=EXCLUDED.model_version,collection_version=EXCLUDED.collection_version,
                    text_hash=EXCLUDED.text_hash,vector=EXCLUDED.vector,dimension=EXCLUDED.dimension""")
                    .format(self._table("cache"), values), params)

            fingerprints = [entry[0] for entry in entries]
            if fingerprints:
                db.execute(sql.SQL("DELETE FROM {} WHERE fingerprint=ANY(%s)")
                           .format(self._table("cache_evidence")), (fingerprints,))
            requested_ids = sorted({evidence_id for fingerprint in fingerprints
                                    for evidence_id in lineage.get(fingerprint, ())})
            expiry_by_id = {}
            for offset in range(0, len(requested_ids), 500):
                rows = db.execute(sql.SQL("SELECT id,expires_at FROM {} WHERE id=ANY(%s)")
                                  .format(self._table("source_evidence")),
                                  (requested_ids[offset:offset + 500],)).fetchall()
                expiry_by_id.update({row["id"]: row["expires_at"] for row in rows})
            if set(expiry_by_id) != set(requested_ids):
                raise ValueError("embedding-cache lineage references unknown source evidence")
            now = datetime.now(timezone.utc)
            links = []
            for cache_fingerprint, evidence_ids in lineage.items():
                if cache_fingerprint not in fingerprints:
                    continue
                expires = [expiry_by_id[value] for value in evidence_ids]
                if any(value is None or value <= now for value in expires):
                    raise ValueError("embedding-cache source evidence is expired")
                links.extend((cache_fingerprint, evidence_id, min(expires))
                             for evidence_id in sorted(set(evidence_ids)))
            if links:
                with db.cursor() as cursor:
                    cursor.executemany(sql.SQL("""INSERT INTO {}(
                        fingerprint,evidence_id,expires_at) VALUES(%s,%s,%s)
                        ON CONFLICT(fingerprint,evidence_id) DO UPDATE
                        SET expires_at=EXCLUDED.expires_at""").format(
                            self._table("cache_evidence")), links)

        return self._write(operation)

    def refresh_latest_topic_counts(self):
        if not self.data_mode_scope:
            return None

        def operation(db):
            latest = db.execute(sql.SQL("SELECT dataset_id FROM {} WHERE data_mode=%s "
                "ORDER BY created_at DESC LIMIT 1 FOR UPDATE").format(self._table("dataset_snapshots")),
                (self.data_mode_scope,)).fetchone()
            if latest is None:
                return None
            dataset_id = latest["dataset_id"]
            topic_filter = sql.SQL("k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE'")
            eligible = db.execute(sql.SQL("SELECT count(*) AS total FROM {} k LEFT JOIN {} cr "
                "ON cr.keyword_id=k.id WHERE {} AND k.data_mode=%s").format(
                    self._table("keywords"), self._table("classification_results"), topic_filter),
                (self.data_mode_scope,)).fetchone()["total"]
            assigned = db.execute(sql.SQL("SELECT count(DISTINCT m.keyword_id) AS total FROM {} m "
                "JOIN {} k ON k.id=m.keyword_id LEFT JOIN {} cr ON cr.keyword_id=k.id "
                "WHERE {} AND k.data_mode=%s").format(self._table("memberships"),
                    self._table("keywords"), self._table("classification_results"), topic_filter),
                (self.data_mode_scope,)).fetchone()["total"]
            unassigned = db.execute(sql.SQL("SELECT count(*) AS total FROM {} k LEFT JOIN {} cr "
                "ON cr.keyword_id=k.id WHERE {} AND k.data_mode=%s AND NOT EXISTS "
                "(SELECT 1 FROM {} m WHERE m.keyword_id=k.id)").format(
                    self._table("keywords"), self._table("classification_results"), topic_filter,
                    self._table("memberships")), (self.data_mode_scope,)).fetchone()["total"]
            ai_assigned = db.execute(sql.SQL("SELECT count(DISTINCT m.keyword_id) AS total FROM {} m "
                "JOIN {} k ON k.id=m.keyword_id LEFT JOIN {} cr ON cr.keyword_id=k.id "
                "WHERE {} AND k.data_mode=%s AND m.origin='LOCAL_AI'").format(
                    self._table("memberships"), self._table("keywords"),
                    self._table("classification_results"), topic_filter),
                (self.data_mode_scope,)).fetchone()["total"]
            db.execute(sql.SQL("UPDATE {} SET eligible_keyword_count=%s,assigned_keyword_count=%s,"
                "unassigned_keyword_count=%s WHERE dataset_id=%s").format(self._table("dataset_snapshots")),
                (eligible, assigned, unassigned, dataset_id))
            db.execute(sql.SQL("UPDATE {} SET eligible=%s,assigned=%s,unassigned=%s WHERE fetch_id=%s")
                .format(self._table("fetch_jobs")), (eligible, ai_assigned, unassigned, dataset_id))
            return {"dataset_id": dataset_id, "eligible": eligible, "assigned": assigned,
                    "ai_assigned": ai_assigned, "unassigned": unassigned}

        return self._write(operation)

    def complete_fetch_dataset_snapshot(self, fetch_id, *,
                                        algorithm_processing_status="COMPLETED",
                                        ai_classification_status="COMPLETED"):
        keyword_filter = sql.SQL("AND data_mode=%s") if self.data_mode_scope else sql.SQL("")
        membership_filter = sql.SQL("AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
        keyword_parameters = (self.data_mode_scope,) if self.data_mode_scope else ()

        def operation(db):
            eligible = db.execute(sql.SQL("SELECT count(*) AS total FROM {} WHERE eligible=1 {}")
                .format(self._table("keywords"), keyword_filter), keyword_parameters).fetchone()["total"]
            assigned = db.execute(sql.SQL("SELECT count(DISTINCT m.keyword_id) AS total FROM {} m "
                "JOIN {} k ON k.id=m.keyword_id LEFT JOIN {} cr ON cr.keyword_id=k.id "
                "WHERE k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE' {}")
                .format(self._table("memberships"), self._table("keywords"),
                        self._table("classification_results"), membership_filter),
                keyword_parameters).fetchone()["total"]
            unassigned = db.execute(sql.SQL("SELECT count(*) AS total FROM {} k LEFT JOIN {} cr "
                "ON cr.keyword_id=k.id WHERE k.eligible=1 {} "
                "AND COALESCE(cr.classification_status,'')<>'INELIGIBLE' AND NOT EXISTS "
                "(SELECT 1 FROM {} m WHERE m.keyword_id=k.id)").format(
                    self._table("keywords"), self._table("classification_results"),
                    membership_filter, self._table("memberships")), keyword_parameters).fetchone()["total"]
            db.execute(sql.SQL("UPDATE {} SET processing_status='COMPLETED',"
                "algorithm_processing_status=%s,ai_classification_status=%s,"
                "eligible_keyword_count=%s,assigned_keyword_count=%s,unassigned_keyword_count=%s "
                "WHERE dataset_id=%s")
                .format(self._table("dataset_snapshots")),
                (algorithm_processing_status, ai_classification_status,
                 eligible, assigned, unassigned, fetch_id))
            return {"eligible": eligible, "assigned": assigned, "unassigned": unassigned}

        return self._write(operation)

    def update_fetch_job(self, fetch_id, values):
        allowed = {"status", "stage", "available", "eligible", "processed", "assigned", "unassigned",
                   "created_collections", "suggestions", "skipped", "cached_embeddings", "inferred_embeddings",
                   "membership_reconciliation_id"}
        changes = {key: value for key, value in values.items() if key in allowed}
        if not changes:
            changes = {}

        def operation(db):
            assignments = sql.SQL(",").join(
                sql.SQL("{}=%s").format(sql.Identifier(key)) for key in changes
            )
            if changes:
                assignments += sql.SQL(",")
            assignments += sql.SQL("updated_at=%s")
            params = (*changes.values(), datetime.now(timezone.utc), fetch_id)
            return db.execute(sql.SQL("UPDATE {} SET {} WHERE fetch_id=%s").format(
                self._table("fetch_jobs"), assignments), params).rowcount

        # Fetch-job rows are keyed by fetch_id; row-level UPDATE locking is
        # sufficient and avoids serializing unrelated collection operations.
        return self._write(operation, serialize=False)

    def mark_fetch_failed(self, fetch_id, error_code):
        def operation(db):
            db.execute(sql.SQL("""INSERT INTO {}(fetch_id,status,error_code,updated_at)
                VALUES (%s,'FAILED',%s,%s) ON CONFLICT(fetch_id) DO UPDATE SET
                status='FAILED',error_code=EXCLUDED.error_code,updated_at=EXCLUDED.updated_at""")
                .format(self._table("fetch_jobs")),
                (fetch_id, error_code, datetime.now(timezone.utc)))

        return self._write(operation)

    def complete_empty_fetch(self, fetch_id, values):
        allowed = {"status", "stage", "available", "eligible", "processed", "assigned", "unassigned",
                   "created_collections", "suggestions", "skipped", "cached_embeddings", "inferred_embeddings",
                   "membership_reconciliation_id"}
        changes = {key: value for key, value in values.items() if key in allowed}

        def operation(db):
            snapshot = db.execute(sql.SQL("SELECT eligible_keyword_count FROM {} WHERE dataset_id=%s FOR UPDATE")
                                  .format(self._table("dataset_snapshots")), (fetch_id,)).fetchone()
            db.execute(sql.SQL("UPDATE {} SET processing_status='COMPLETED',"
                "algorithm_processing_status='SKIPPED',ai_classification_status='NOT_REQUIRED' "
                "WHERE dataset_id=%s")
                       .format(self._table("dataset_snapshots")), (fetch_id,))
            changes["eligible"] = snapshot["eligible_keyword_count"] if snapshot else 0
            assignments = sql.SQL(",").join(
                sql.SQL("{}=%s").format(sql.Identifier(key)) for key in changes
            )
            if changes:
                assignments += sql.SQL(",")
            assignments += sql.SQL("updated_at=%s")
            db.execute(sql.SQL("UPDATE {} SET {} WHERE fetch_id=%s").format(
                self._table("fetch_jobs"), assignments),
                (*changes.values(), datetime.now(timezone.utc), fetch_id))
            return snapshot["eligible_keyword_count"] if snapshot else 0

        return self._write(operation)

    def mark_fetch_model_unavailable(self, fetch_id, *, available, skipped, eligible,
                                     cached_embeddings, inferred_embeddings, error_code,
                                     algorithm_processing_status="UNKNOWN"):
        def operation(db):
            db.execute(sql.SQL("UPDATE {} SET processing_status='MODEL_UNAVAILABLE',"
                "ai_classification_status='MODEL_UNAVAILABLE',algorithm_processing_status=%s "
                "WHERE dataset_id=%s")
                .format(self._table("dataset_snapshots")),
                (algorithm_processing_status, fetch_id))
            db.execute(sql.SQL("""INSERT INTO {}(fetch_id,status,available,processed,assigned,unassigned,
                created_collections,suggestions,skipped,error_code,updated_at,stage,eligible,
                cached_embeddings,inferred_embeddings,membership_reconciliation_id)
                VALUES (%s,'MODEL_UNAVAILABLE',%s,0,0,0,0,0,%s,%s,%s,
                'MODEL_UNAVAILABLE',%s,%s,%s,NULL) ON CONFLICT(fetch_id) DO UPDATE SET
                status=EXCLUDED.status,available=EXCLUDED.available,processed=EXCLUDED.processed,
                assigned=EXCLUDED.assigned,unassigned=EXCLUDED.unassigned,
                created_collections=EXCLUDED.created_collections,suggestions=EXCLUDED.suggestions,
                skipped=EXCLUDED.skipped,error_code=EXCLUDED.error_code,updated_at=EXCLUDED.updated_at,
                stage=EXCLUDED.stage,eligible=EXCLUDED.eligible,cached_embeddings=EXCLUDED.cached_embeddings,
                inferred_embeddings=EXCLUDED.inferred_embeddings,
                membership_reconciliation_id=EXCLUDED.membership_reconciliation_id""")
                .format(self._table("fetch_jobs")),
                (fetch_id, available, skipped, error_code, datetime.now(timezone.utc), eligible,
                 cached_embeddings, inferred_embeddings))

        return self._write(operation)

    def persist_dataset_snapshot(self, dataset_id, *, status, data_mode, source_manifest,
                                 provider_record_count, received_record_count,
                                 retained_keyword_count, rejected_record_count,
                                 observation_start, observation_end, fetched_at, quality_flags,
                                 processing_status, created_at,
                                 source_ingestion_status="UNKNOWN",
                                 materialization_status="COMPLETED",
                                 algorithm_processing_status="PENDING",
                                 ai_classification_status="PENDING",
                                 source_evidence_rows=None,
                                 source_evidence_ids_already_persisted=()):
        if (not isinstance(source_evidence_ids_already_persisted, (tuple, list, set, frozenset))
                or any(not isinstance(identifier, str) or not identifier
                       or len(identifier) > 256
                       for identifier in source_evidence_ids_already_persisted)):
            raise ValueError("persisted source evidence ids must be bounded strings")
        already_persisted = frozenset(source_evidence_ids_already_persisted)

        def operation(db):
            mode_scope = sql.SQL("AND data_mode=%s") if self.data_mode_scope else sql.SQL("")
            keyword_scope = sql.SQL("AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
            counts = db.execute(sql.SQL("""SELECT
                (SELECT count(*) FROM {} WHERE eligible=1 {}) AS eligible,
                (SELECT count(DISTINCT m.keyword_id) FROM {} m JOIN {} k ON k.id=m.keyword_id
                    WHERE TRUE {}) AS assigned,
                (SELECT count(*) FROM {} k LEFT JOIN {} cr ON cr.keyword_id=k.id
                    WHERE k.eligible=1 {} AND COALESCE(cr.classification_status,'')<>'INELIGIBLE'
                    AND NOT EXISTS (SELECT 1 FROM {} m WHERE m.keyword_id=k.id)) AS unassigned""").format(
                        self._table("keywords"), mode_scope, self._table("memberships"),
                        self._table("keywords"), keyword_scope, self._table("keywords"),
                        self._table("classification_results"), keyword_scope,
                        self._table("memberships")),
                ((self.data_mode_scope,) * 3) if self.data_mode_scope else ()).fetchone()
            db.execute(sql.SQL("""INSERT INTO {}(dataset_id,status,data_mode,source_manifest,
                provider_record_count,received_record_count,retained_keyword_count,rejected_record_count,
                observation_start,observation_end,fetched_at,quality_flags,processing_status,
                eligible_keyword_count,assigned_keyword_count,unassigned_keyword_count,created_at,
                source_ingestion_status,materialization_status,algorithm_processing_status,
                ai_classification_status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(dataset_id) DO UPDATE SET status=EXCLUDED.status,data_mode=EXCLUDED.data_mode,
                source_manifest=EXCLUDED.source_manifest,provider_record_count=EXCLUDED.provider_record_count,
                received_record_count=EXCLUDED.received_record_count,
                retained_keyword_count=EXCLUDED.retained_keyword_count,
                rejected_record_count=EXCLUDED.rejected_record_count,
                observation_start=EXCLUDED.observation_start,observation_end=EXCLUDED.observation_end,
                fetched_at=EXCLUDED.fetched_at,quality_flags=EXCLUDED.quality_flags,
                processing_status=EXCLUDED.processing_status,eligible_keyword_count=EXCLUDED.eligible_keyword_count,
                assigned_keyword_count=EXCLUDED.assigned_keyword_count,
                unassigned_keyword_count=EXCLUDED.unassigned_keyword_count,created_at=EXCLUDED.created_at,
                source_ingestion_status=EXCLUDED.source_ingestion_status,
                materialization_status=EXCLUDED.materialization_status,
                algorithm_processing_status=EXCLUDED.algorithm_processing_status,
                ai_classification_status=EXCLUDED.ai_classification_status""")
                .format(self._table("dataset_snapshots")),
                (dataset_id, status, data_mode, Jsonb(source_manifest), provider_record_count,
                 received_record_count, retained_keyword_count, rejected_record_count, observation_start,
                 observation_end, fetched_at, Jsonb(quality_flags), processing_status,
                 counts["eligible"], counts["assigned"], counts["unassigned"], created_at,
                 source_ingestion_status, materialization_status, algorithm_processing_status,
                 ai_classification_status))
            if source_evidence_rows:
                rows = [row for row in source_evidence_rows if isinstance(row, dict)]
                if already_persisted:
                    pending_rows = [row for row in rows if row.get("id") not in already_persisted]
                    if pending_rows:
                        self._persist_source_evidence(db, None, pending_rows)
                    self._link_dataset_evidence(db, dataset_id, rows)
                else:
                    self._persist_source_evidence(db, dataset_id, rows)
            return counts

        return self._write(operation)

    def _persist_source_evidence(self, db, dataset_id, evidence_rows):
        """Write source lineage and keyword links, optionally associating a snapshot."""
        by_id = {
            row["id"]: row for row in evidence_rows
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
        rows = list(by_id.values())
        if not rows:
            return {
                "source_evidence_new_rows": 0,
                "source_evidence_updated_rows": 0,
                "source_evidence_existing_rows": 0,
                "evidence_keyword_links_inserted": 0,
                "dataset_evidence_links_inserted": 0,
            }
        source_names = {}
        for row in rows:
            source_id = row["source_id"]
            policy = row.get("retention_policy")
            if (not isinstance(policy, dict)
                    or not isinstance(policy.get("version"), str)
                    or policy.get("review_status") not in {"reviewed", "partial"}
                    or (policy.get("provider_max_age_days") is not None
                        and (type(policy["provider_max_age_days"]) is not int
                             or policy["provider_max_age_days"] < 1))
                    or not isinstance(policy.get("scope"), str)):
                raise ValueError("source evidence has no valid retention policy")
            source_entry = (row["source_display_name"], policy)
            previous = source_names.get(source_id)
            if previous is not None and previous != source_entry:
                raise ValueError("source evidence has conflicting retention policy metadata")
            source_names[source_id] = source_entry
        stats = {
            "source_evidence_new_rows": 0,
            "source_evidence_updated_rows": 0,
            "source_evidence_existing_rows": 0,
            "evidence_keyword_links_inserted": 0,
            "dataset_evidence_links_inserted": 0,
        }
        with db.cursor() as cursor:
            cursor.executemany(sql.SQL("""INSERT INTO {} AS stored_source(
                    id,display_name,configuration)
                VALUES (%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET
                    display_name=EXCLUDED.display_name,
                    configuration=stored_source.configuration || EXCLUDED.configuration,
                    updated_at=clock_timestamp()
                WHERE stored_source.display_name IS DISTINCT FROM EXCLUDED.display_name
                   OR stored_source.configuration->'retention_policy'
                      IS DISTINCT FROM EXCLUDED.configuration->'retention_policy'""")
                .format(self._table("sources")), [
                    (source_id, display_name, Jsonb({"retention_policy": policy}))
                    for source_id, (display_name, policy) in sorted(source_names.items())
                ])

            identifiers = list(by_id)
            existing_rows = db.execute(sql.SQL("""SELECT id,source_id,content_digest,query_scope,
                geography,observed_at,collected_at,payload,expires_at FROM {}
                WHERE id=ANY(%s)""").format(self._table("source_evidence")),
                (identifiers,)).fetchall()
            existing_by_id = {row["id"]: row for row in existing_rows}
            rows_to_write = []
            for row in rows:
                existing = existing_by_id.get(row["id"])
                if existing is None:
                    stats["source_evidence_new_rows"] += 1
                    rows_to_write.append(row)
                    continue
                identity_matches = all((
                    existing["source_id"] == row["source_id"],
                    existing["content_digest"] == row["content_digest"],
                    existing["query_scope"] == row["query_scope"],
                    existing["geography"] == row.get("geography"),
                    existing["observed_at"] == row.get("observed_at"),
                    existing["collected_at"] == row["collected_at"],
                ))
                if not identity_matches:
                    raise CollectionWriteError("source evidence identity conflict")
                stats["source_evidence_existing_rows"] += 1
                expiration_tightened = (
                    existing["expires_at"] is None
                    or row["expires_at"] < existing["expires_at"]
                )
                lineage_missing = existing["payload"] is None and row.get("payload") is not None
                if expiration_tightened or lineage_missing:
                    stats["source_evidence_updated_rows"] += 1
                    rows_to_write.append(row)
            insert_evidence = sql.SQL("""INSERT INTO {} AS stored_evidence(
                id,source_id,external_id,content_digest,
                query_scope,geography,observed_at,collected_at,payload,expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET
                    expires_at=LEAST(stored_evidence.expires_at,EXCLUDED.expires_at),
                    payload=COALESCE(stored_evidence.payload,EXCLUDED.payload)""").format(
                        self._table("source_evidence"))
            for offset in range(0, len(rows_to_write), 500):
                batch = rows_to_write[offset:offset + 500]
                cursor.executemany(insert_evidence, [(
                    row["id"], row["source_id"], row.get("external_id"), row["content_digest"],
                    Jsonb(row["query_scope"]),
                    Jsonb(row["geography"]) if row.get("geography") is not None else None,
                    row.get("observed_at"), row["collected_at"],
                    Jsonb(row["payload"]) if row.get("payload") is not None else None,
                    row["expires_at"],
                ) for row in batch])

            requested_keyword_ids = sorted({keyword_id for row in rows
                                            for keyword_id in row.get("keyword_ids", ())})
            existing_keyword_ids = set()
            for offset in range(0, len(requested_keyword_ids), 500):
                batch = requested_keyword_ids[offset:offset + 500]
                if batch:
                    found = db.execute(sql.SQL("SELECT id FROM {} WHERE id=ANY(%s)")
                                       .format(self._table("keywords")), (batch,)).fetchall()
                    existing_keyword_ids.update(item["id"] for item in found)
            links = sorted({(row["id"], keyword_id) for row in rows
                            for keyword_id in row.get("keyword_ids", ())
                            if keyword_id in existing_keyword_ids})
            for offset in range(0, len(links), 500):
                cursor.executemany(sql.SQL("""INSERT INTO {}(evidence_id,keyword_id)
                    VALUES (%s,%s) ON CONFLICT DO NOTHING""").format(self._table("evidence_keywords")),
                    links[offset:offset + 500])
                stats["evidence_keyword_links_inserted"] += max(cursor.rowcount, 0)

        if dataset_id is not None:
            stats["dataset_evidence_links_inserted"] = self._link_dataset_evidence(
                db, dataset_id, rows,
            )
        return stats

    def _link_dataset_evidence(self, db, dataset_id, evidence_rows):
        rows = [row for row in evidence_rows
                if isinstance(row, dict) and isinstance(row.get("id"), str)]
        if not rows:
            return 0
        with db.cursor() as cursor:
            cursor.executemany(sql.SQL("""INSERT INTO {}(dataset_id,evidence_id)
                VALUES (%s,%s) ON CONFLICT DO NOTHING""").format(self._table("dataset_evidence")),
                [(dataset_id, row["id"]) for row in rows])
            return max(cursor.rowcount, 0)

    def create(self, data, *, created_by="USER", reason=""):
        identifier = str(uuid.uuid4())
        stamp = datetime.now(timezone.utc)
        def operation(db):
            names = db.execute(sql.SQL("SELECT name FROM {} WHERE status='ACTIVE'")
                               .format(self._table("collections"))).fetchall()
            if any(fingerprint(row["name"]) == fingerprint(data["name"]) for row in names):
                raise ValueError("COLLECTION_ALREADY_EXISTS")
            db.execute(sql.SQL("""INSERT INTO {}(id,name,description,category_id,parent_id,examples,
                exclusions,created_by,auto_match,reason,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""").format(self._table("collections")),
                (identifier, data["name"], data.get("description", ""), data.get("category_id"),
                 data.get("parent_id"), Jsonb(data.get("examples", [])), Jsonb(data.get("exclusions", [])),
                 created_by, int(data.get("auto_match", True)), reason, stamp, stamp))
            return identifier
        return self._write(operation)

    def ensure_category_collection(self, category, *, parent_id=None):
        identifier = "topic-" + fingerprint(category.id)[:32]
        description = f"Keywords and entities about {category.display_name.casefold()}."
        def operation(db):
            existing = db.execute(sql.SQL("SELECT id FROM {} WHERE category_id=%s AND status='ACTIVE'")
                                  .format(self._table("collections")), (category.id,)).fetchone()
            if existing:
                return existing["id"]
            suppressed = db.execute(sql.SQL("SELECT 1 FROM {} WHERE fingerprint=%s AND kind='DELETED_COLLECTION'")
                                    .format(self._table("suppression")),
                                    (fingerprint(category.display_name),)).fetchone()
            if suppressed:
                return None
            # SQLite NOCASE is ASCII-only, unlike locale-sensitive ILIKE.
            existing = db.execute(sql.SQL("""SELECT id FROM {} WHERE status='ACTIVE' AND
                translate(name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')=
                translate(%s,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz') ORDER BY created_at LIMIT 1""")
                .format(self._table("collections")), (category.display_name,)).fetchone()
            if existing:
                return existing["id"]
            stamp = datetime.now(timezone.utc)
            db.execute(sql.SQL("""INSERT INTO {}(id,name,description,category_id,parent_id,
                examples,exclusions,created_by,auto_match,reason,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,'[]'::jsonb,'[]'::jsonb,'AI',1,
                    'taxonomy-backed entity classification',%s,%s) ON CONFLICT DO NOTHING""")
                .format(self._table("collections")),
                (identifier, category.display_name, description, category.id, parent_id, stamp, stamp))
            row = db.execute(sql.SQL("SELECT id FROM {} WHERE category_id=%s AND status='ACTIVE'")
                             .format(self._table("collections")), (category.id,)).fetchone()
            return row["id"] if row else None
        return self._write(operation)

    def ensure_category_collections(self, categories):
        """Ensure a bounded group of taxonomy roots with one database transaction."""
        categories = tuple(categories)
        if (len(categories) > 1000
                or any(not isinstance(getattr(category, "id", None), str)
                       or not category.id
                       or not isinstance(getattr(category, "display_name", None), str)
                       or not category.display_name
                       for category in categories)):
            raise ValueError("bounded taxonomy categories with string identities required")
        if not categories:
            return {}

        by_id = {category.id: category for category in categories}
        category_ids = list(by_id)
        ascii_lower = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
        name_fingerprints = {
            category.id: fingerprint(category.display_name) for category in by_id.values()
        }
        folded_names = sorted({category.display_name.translate(ascii_lower)
                               for category in by_id.values()})
        suppressed_fingerprints = sorted(set(name_fingerprints.values()))

        def operation(db):
            result = {
                row["category_id"]: row["id"]
                for row in db.execute(sql.SQL("""SELECT id,category_id FROM {}
                    WHERE category_id=ANY(%s) AND status='ACTIVE'""").format(
                        self._table("collections")), (category_ids,)).fetchall()
            }
            missing = [category for category_id, category in by_id.items()
                       if category_id not in result]
            if not missing:
                return result

            suppressed = {
                row["fingerprint"]
                for row in db.execute(sql.SQL("SELECT fingerprint FROM {} WHERE fingerprint=ANY(%s) "
                                               "AND kind='DELETED_COLLECTION'").format(
                    self._table("suppression")), (suppressed_fingerprints,)).fetchall()
            }
            existing_names = {}
            for row in db.execute(sql.SQL("""SELECT id,name FROM {} WHERE status='ACTIVE'
                AND translate(name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')=ANY(%s)
                ORDER BY created_at,id""").format(self._table("collections")),
                    (folded_names,)).fetchall():
                existing_names.setdefault(row["name"].translate(ascii_lower), row["id"])

            stamp = datetime.now(timezone.utc)
            inserts = []
            for category in missing:
                if name_fingerprints[category.id] in suppressed:
                    continue
                folded_name = category.display_name.translate(ascii_lower)
                existing_id = existing_names.get(folded_name)
                if existing_id:
                    result[category.id] = existing_id
                    continue
                category_collection_id = "topic-" + fingerprint(category.id)[:32]
                inserts.append((
                    category_collection_id,
                    category.display_name,
                    f"Keywords and entities about {category.display_name.casefold()}.",
                    category.id,
                    stamp,
                    stamp,
                ))
                existing_names[folded_name] = category_collection_id

            if inserts:
                with db.cursor() as cursor:
                    cursor.executemany(sql.SQL("""INSERT INTO {}(id,name,description,category_id,
                        parent_id,examples,exclusions,created_by,auto_match,reason,created_at,updated_at)
                        VALUES (%s,%s,%s,%s,NULL,'[]'::jsonb,'[]'::jsonb,'AI',1,
                            'taxonomy-backed entity classification',%s,%s) ON CONFLICT DO NOTHING""")
                        .format(self._table("collections")), inserts)
                result.update({
                    row["category_id"]: row["id"]
                    for row in db.execute(sql.SQL("""SELECT id,category_id FROM {}
                        WHERE category_id=ANY(%s) AND status='ACTIVE'""").format(
                            self._table("collections")), (category_ids,)).fetchall()
                })
            return result

        return self._write(operation)

    def update(self, identifier, data):
        allowed = {"name", "description", "examples", "exclusions", "auto_match"}
        values = {key: value for key, value in data.items() if key in allowed}
        if not values:
            raise ValueError("empty update")
        def operation(db):
            row = db.execute(sql.SQL("SELECT version FROM {} WHERE id=%s AND status='ACTIVE' FOR UPDATE")
                             .format(self._table("collections")), (identifier,)).fetchone()
            if row is None:
                return False
            assignments = sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(key)) for key in values)
            params = [Jsonb(value) if key in {"examples", "exclusions"} else int(value) if key == "auto_match"
                      else value for key, value in values.items()]
            db.execute(sql.SQL("UPDATE {} SET {},version=version+1,updated_at=%s WHERE id=%s")
                       .format(self._table("collections"), assignments),
                       (*params, datetime.now(timezone.utc), identifier))
            db.execute(sql.SQL("DELETE FROM {} WHERE collection_version>0 AND collection_version<%s")
                       .format(self._table("cache")), (row["version"] + 1,))
            return True
        return self._write(operation)

    def delete(self, identifier):
        def operation(db):
            row = db.execute(sql.SQL("SELECT id FROM {} WHERE id=%s FOR UPDATE")
                             .format(self._table("collections")), (identifier,)).fetchone()
            if row is None:
                return False
            descendants = db.execute(sql.SQL("""WITH RECURSIVE tree(id,name) AS (
                SELECT id,name FROM {} WHERE id=%s UNION
                SELECT c.id,c.name FROM {} c JOIN tree t ON c.parent_id=t.id
            ) SELECT name FROM tree""").format(self._table("collections"), self._table("collections")),
                (identifier,)).fetchall()
            stamp = datetime.now(timezone.utc)
            with db.cursor() as cursor:
                cursor.executemany(sql.SQL("""INSERT INTO {}(fingerprint,kind,created_at,label)
                    VALUES (%s,'DELETED_COLLECTION',%s,%s) ON CONFLICT(fingerprint) DO UPDATE
                    SET kind=EXCLUDED.kind,created_at=EXCLUDED.created_at,label=EXCLUDED.label""")
                    .format(self._table("suppression")),
                    [(fingerprint(item["name"]), stamp, item["name"]) for item in descendants])
            db.execute(sql.SQL("DELETE FROM {} WHERE id=%s").format(self._table("collections")), (identifier,))
            return True
        return self._write(operation)

    def merge(self, source_id, target_id):
        if source_id == target_id:
            raise ValueError("COLLECTION_MERGE_SAME_TARGET")
        def operation(db):
            endpoints = db.execute(sql.SQL("""SELECT id,name,category_id FROM {}
                WHERE id=ANY(%s) AND status='ACTIVE'""").format(self._table("collections")),
                ([source_id, target_id],)).fetchall()
            by_id = {row["id"]: row for row in endpoints}
            if source_id not in by_id or target_id not in by_id:
                raise ValueError("COLLECTION_NOT_FOUND")
            tree = db.execute(sql.SQL("""WITH RECURSIVE tree(id,name,category_id) AS (
                SELECT id,name,category_id FROM {} WHERE id=%s UNION
                SELECT c.id,c.name,c.category_id FROM {} c JOIN tree p ON c.parent_id=p.id
            ) SELECT id,name,category_id FROM tree""").format(
                self._table("collections"), self._table("collections")), (source_id,)).fetchall()
            if any(item["id"] == target_id for item in tree):
                raise ValueError("COLLECTION_MERGE_TARGET_DESCENDANT")
            if by_id[target_id]["category_id"] or any(item["category_id"] for item in tree):
                raise ValueError("TAXONOMY_COLLECTION_MERGE_FORBIDDEN")
            names = {item["id"]: item["name"] for item in tree}
            source_members = db.execute(sql.SQL('SELECT * FROM {} WHERE collection_id=ANY(%s) '
                                                'ORDER BY collection_id COLLATE "C",keyword_id COLLATE "C"')
                .format(self._table("memberships")), (list(names),)).fetchall()
            target_members = {row["keyword_id"]: row for row in db.execute(sql.SQL(
                "SELECT keyword_id,pinned,origin FROM {} WHERE collection_id=%s")
                .format(self._table("memberships")), (target_id,)).fetchall()}
            feedback = {row["keyword_id"]: row["action"] for row in db.execute(sql.SQL(
                "SELECT keyword_id,action FROM {} WHERE collection_id=%s")
                .format(self._table("feedback")), (target_id,)).fetchall()}
            transferred = skipped = 0
            stamp = datetime.now(timezone.utc)
            for member in source_members:
                key = member["keyword_id"]
                existing = target_members.get(key)
                if (feedback.get(key) == "REMOVE" or existing and existing["pinned"]
                        or existing and member["origin"] == "LOCAL_AI"):
                    skipped += 1
                    continue
                origin = member["origin"]
                pinned = int(bool(member["pinned"]) or origin == "USER")
                reason = f"{member['reason']} (merged from {names[member['collection_id']]})".strip()
                db.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,origin,pinned,score,reason,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(collection_id,keyword_id) DO UPDATE
                    SET origin=EXCLUDED.origin,pinned=EXCLUDED.pinned,score=EXCLUDED.score,
                        reason=EXCLUDED.reason,created_at=EXCLUDED.created_at""").format(self._table("memberships")),
                    (target_id, key, origin, pinned, member["score"], reason, stamp))
                target_members[key] = {"keyword_id": key, "pinned": pinned, "origin": origin}
                if origin == "USER":
                    db.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,action,created_at)
                        VALUES (%s,%s,'ADD',%s) ON CONFLICT(collection_id,keyword_id) DO UPDATE
                        SET action=EXCLUDED.action,created_at=EXCLUDED.created_at""").format(self._table("feedback")),
                        (target_id, key, stamp))
                    feedback[key] = "ADD"
                transferred += 1
            with db.cursor() as cursor:
                cursor.executemany(sql.SQL("""INSERT INTO {}(fingerprint,kind,created_at,label)
                    VALUES (%s,'DELETED_COLLECTION',%s,%s) ON CONFLICT(fingerprint) DO UPDATE
                    SET kind=EXCLUDED.kind,created_at=EXCLUDED.created_at,label=EXCLUDED.label""")
                    .format(self._table("suppression")),
                    [(fingerprint(item["name"]), stamp, item["name"]) for item in tree])
            db.execute(sql.SQL("DELETE FROM {} WHERE id=%s").format(self._table("collections")), (source_id,))
            db.execute(sql.SQL("UPDATE {} SET version=version+1,updated_at=%s WHERE id=%s")
                       .format(self._table("collections")), (stamp, target_id))
            return {"merged": True, "transferred": transferred, "skipped": skipped, "target_id": target_id}
        return self._write(operation)

    def archive_unpinned_ai_memberships(self, run_id: str, reason: str):
        stamp = datetime.now(timezone.utc)

        def operation(db):
            db.execute(sql.SQL("""INSERT INTO {}(id,created_at,reason,status)
                VALUES (%s,%s,%s,'APPLYING')""").format(self._table("membership_reconciliation_runs")),
                (run_id, stamp, reason))
            scope = sql.SQL(" AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
            parameters = (run_id, self.data_mode_scope) if self.data_mode_scope else (run_id,)
            db.execute(sql.SQL("""INSERT INTO {}(run_id,collection_id,keyword_id,origin,pinned,score,reason,created_at)
                SELECT %s,m.collection_id,m.keyword_id,m.origin,m.pinned,m.score,m.reason,m.created_at
                FROM {} m JOIN {} k ON k.id=m.keyword_id
                WHERE m.origin='LOCAL_AI' AND m.pinned=0""").format(
                    self._table("membership_reconciliation_items"),
                    self._table("memberships"), self._table("keywords")) + scope,
                parameters)
            archived = db.execute("SELECT count(*) AS n FROM {} WHERE run_id=%s".format(
                self._table("membership_reconciliation_items").as_string()), (run_id,)).fetchone()["n"]
            db.execute(sql.SQL("""DELETE FROM {} m USING {} k
                WHERE m.keyword_id=k.id AND m.origin='LOCAL_AI' AND m.pinned=0""").format(
                    self._table("memberships"), self._table("keywords")) + scope,
                (self.data_mode_scope,) if self.data_mode_scope else ())
            db.execute(sql.SQL("UPDATE {} SET status='APPLIED' WHERE id=%s")
                       .format(self._table("membership_reconciliation_runs")), (run_id,))
            return archived
        return self._write(operation)

    def rollback_membership_reconciliation(self, run_id: str):
        def operation(db):
            run = db.execute(sql.SQL("SELECT status,created_at,reason FROM {} WHERE id=%s")
                             .format(self._table("membership_reconciliation_runs")), (run_id,)).fetchone()
            if not run or run["status"] != "APPLIED":
                return False
            later = db.execute(sql.SQL("""SELECT 1 FROM {} WHERE status='APPLIED' AND created_at>%s
                LIMIT 1""").format(self._table("membership_reconciliation_runs")),
                (run["created_at"],)).fetchone()
            if later:
                return False

            reason = run.get("reason", "")
            if isinstance(reason, str) and reason.startswith(_AI_TARGETED_RUN_PREFIX):
                try:
                    targeted = json.loads(reason[len(_AI_TARGETED_RUN_PREFIX):]).get("keyword_ids")
                except (TypeError, ValueError, AttributeError):
                    return False
                if (not isinstance(targeted, list) or not targeted
                        or any(not isinstance(value, str) or not value for value in targeted)):
                    return False
                live_scope = sql.SQL(" AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
                parameters = (targeted, self.data_mode_scope) if self.data_mode_scope else (targeted,)
                db.execute(sql.SQL("DELETE FROM {} m USING {} k WHERE m.keyword_id=k.id "
                    "AND m.origin='LOCAL_AI' AND m.pinned=0 AND m.keyword_id=ANY(%s)").format(
                        self._table("memberships"), self._table("keywords")) + live_scope, parameters)
                restore_parameters = (run_id, self.data_mode_scope) if self.data_mode_scope else (run_id,)
                db.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,origin,pinned,score,reason,created_at)
                    SELECT i.collection_id,i.keyword_id,i.origin,i.pinned,i.score,i.reason,i.created_at
                    FROM {} i JOIN {} c ON c.id=i.collection_id JOIN {} k ON k.id=i.keyword_id
                    WHERE i.run_id=%s""").format(self._table("memberships"),
                        self._table("membership_reconciliation_items"), self._table("collections"),
                        self._table("keywords"))
                    + (sql.SQL(" AND k.data_mode=%s") if self.data_mode_scope else sql.SQL(""))
                    + sql.SQL(" ON CONFLICT(collection_id,keyword_id) DO NOTHING"), restore_parameters)
                db.execute(sql.SQL("UPDATE {} SET status='ROLLED_BACK' WHERE id=%s")
                           .format(self._table("membership_reconciliation_runs")), (run_id,))
                return True

            created = db.execute(sql.SQL("""SELECT id FROM {} WHERE created_by='AI' AND reason LIKE %s""")
                                 .format(self._table("collections")), (f"%{run_id}%",)).fetchall()
            for item in created:
                adopted = db.execute(sql.SQL("""SELECT EXISTS(SELECT 1 FROM {} WHERE collection_id=%s
                    AND origin='USER') OR EXISTS(SELECT 1 FROM {} WHERE collection_id=%s) AS adopted""")
                    .format(self._table("memberships"), self._table("feedback")),
                    (item["id"], item["id"])).fetchone()["adopted"]
                if not adopted:
                    db.execute(sql.SQL("DELETE FROM {} WHERE id=%s").format(self._table("collections")),
                               (item["id"],))

            live_scope = sql.SQL(" AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
            parameters = (run["created_at"], run_id, self.data_mode_scope) if self.data_mode_scope else (
                run["created_at"], run_id)
            db.execute(sql.SQL("""DELETE FROM {} m USING {} k
                WHERE m.keyword_id=k.id AND m.origin='LOCAL_AI' AND m.pinned=0 AND
                (m.created_at >= %s OR EXISTS (SELECT 1 FROM {} i WHERE i.run_id=%s
                    AND i.collection_id=m.collection_id AND i.keyword_id=m.keyword_id))""").format(
                    self._table("memberships"), self._table("keywords"),
                    self._table("membership_reconciliation_items")) + live_scope, parameters)
            scope = sql.SQL(" AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
            restore_parameters = (run_id, self.data_mode_scope) if self.data_mode_scope else (run_id,)
            db.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,origin,pinned,score,reason,created_at)
                SELECT i.collection_id,i.keyword_id,i.origin,i.pinned,i.score,i.reason,i.created_at
                FROM {} i JOIN {} c ON c.id=i.collection_id JOIN {} k ON k.id=i.keyword_id
                WHERE i.run_id=%s""").format(self._table("memberships"),
                    self._table("membership_reconciliation_items"), self._table("collections"),
                    self._table("keywords")) + scope + sql.SQL(" ON CONFLICT(collection_id,keyword_id) DO NOTHING"),
                restore_parameters)
            db.execute(sql.SQL("UPDATE {} SET status='ROLLED_BACK' WHERE id=%s")
                       .format(self._table("membership_reconciliation_runs")), (run_id,))
            return True
        return self._write(operation)

    def add_local_ai_memberships(self, collection_id, keyword_ids, reason):
        keyword_ids = list(keyword_ids)
        if not keyword_ids:
            return

        def operation(db):
            stamp = datetime.now(timezone.utc)
            values = sql.SQL(",").join(sql.SQL("(%s,%s,'LOCAL_AI',0,NULL,%s,%s)")
                                         for _ in keyword_ids)
            params = [value for keyword_id in keyword_ids
                      for value in (collection_id, keyword_id, reason, stamp)]
            db.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,origin,pinned,score,reason,created_at)
                VALUES {} ON CONFLICT(collection_id,keyword_id) DO NOTHING""")
                .format(self._table("memberships"), values), params)

        return self._write(operation)

    def add_taxonomy_memberships_if_allowed(self, assignments):
        assignments = list(assignments)

        def operation(db):
            for collection_id, keyword_id, reason in assignments:
                collection = db.execute(sql.SQL("SELECT auto_match,status FROM {} WHERE id=%s")
                                        .format(self._table("collections")), (collection_id,)).fetchone()
                if not collection or collection["status"] != "ACTIVE" or not collection["auto_match"]:
                    continue
                removed = db.execute(sql.SQL("SELECT 1 FROM {} WHERE collection_id=%s AND keyword_id=%s "
                    "AND action='REMOVE'").format(self._table("feedback")),
                    (collection_id, keyword_id)).fetchone()
                if removed:
                    continue
                db.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,origin,pinned,score,reason,created_at)
                    VALUES (%s,%s,'LOCAL_AI',0,NULL,%s,%s) ON CONFLICT(collection_id,keyword_id) DO NOTHING""")
                    .format(self._table("memberships")),
                    (collection_id, keyword_id, reason, datetime.now(timezone.utc)))

        return self._write(operation)

    def replace_local_ai_memberships(self, assignments, *, source_ids=None):
        assignments = list(assignments)
        sources = None if source_ids is None else list(source_ids)

        def operation(db):
            conditions = [sql.SQL("origin='LOCAL_AI'"), sql.SQL("pinned=0")]
            parameters = []
            if self.data_mode_scope:
                conditions.append(sql.SQL("keyword_id IN (SELECT id FROM {} WHERE data_mode=%s)")
                                  .format(self._table("keywords")))
                parameters.append(self.data_mode_scope)
            if sources is not None:
                conditions.append(sql.SQL("keyword_id=ANY(%s)"))
                parameters.append(sources)
            db.execute(sql.SQL("DELETE FROM {} WHERE ").format(self._table("memberships"))
                       + sql.SQL(" AND ").join(conditions), parameters)

            inserted = 0
            for offset in range(0, len(assignments), 500):
                batch = assignments[offset:offset + 500]
                values = sql.SQL(",").join(
                    sql.SQL("(%s,%s,%s::double precision,%s,%s::timestamptz)") for _ in batch
                )
                params = [value for collection_id, keyword_id, score, reason in batch
                          for value in (collection_id, keyword_id, score, reason, datetime.now(timezone.utc))]
                rows = db.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,origin,pinned,score,reason,created_at)
                    SELECT v.collection_id,v.keyword_id,'LOCAL_AI',0,v.score,v.reason,v.created_at
                    FROM (VALUES {}) AS v(collection_id,keyword_id,score,reason,created_at)
                    JOIN {} c ON c.id=v.collection_id AND c.status='ACTIVE' AND c.auto_match=1
                    WHERE NOT EXISTS(SELECT 1 FROM {} f WHERE f.collection_id=v.collection_id
                        AND f.keyword_id=v.keyword_id AND f.action='REMOVE')
                    AND NOT EXISTS(SELECT 1 FROM {} m WHERE m.collection_id=v.collection_id
                        AND m.keyword_id=v.keyword_id AND m.pinned=1)
                    ON CONFLICT(collection_id,keyword_id) DO NOTHING RETURNING keyword_id""")
                    .format(self._table("memberships"), values, self._table("collections"),
                            self._table("feedback"), self._table("memberships")), params).fetchall()
                inserted += len(rows)
            return inserted

        return self._write(operation)

    def persist_ai_classification(self, run_id, keyword_ids, assignments, proposals, *, job_id,
                                  expected_collection_versions=None, expected_keyword_texts=None,
                                  expected_collection_auto_match=None, expected_keyword_states=None,
                                  allowed_sources=None):
        """Atomically reconcile only the submitted keywords and create pending proposals."""
        identifiers = list(dict.fromkeys(keyword_ids))
        assignments = list(assignments)
        proposals = list(proposals)
        expected_collection_versions = dict(expected_collection_versions or {})
        expected_keyword_texts = dict(expected_keyword_texts or {})
        expected_collection_auto_match = dict(expected_collection_auto_match or {})
        expected_keyword_states = dict(expected_keyword_states or {})
        if (not isinstance(run_id, str) or not run_id or len(run_id) > 128
                or not isinstance(job_id, str) or not job_id or len(job_id) > 128
                or not identifiers or len(identifiers) > 200
                or any(not isinstance(value, str) or not value or len(value) > 64 for value in identifiers)):
            raise ValueError("AI_INPUT_CHANGED")
        if (any(not isinstance(row, (tuple, list)) or len(row) != 4
                or row[1] not in identifiers or not isinstance(row[0], str) or not row[0].strip()
                or type(row[2]) not in (int, float) or not math.isfinite(row[2])
                or not 0 <= row[2] <= 1 or not isinstance(row[3], str) for row in assignments)
                or len({row[1] for row in assignments}) != len(assignments)):
            raise ValueError("AI_INPUT_CHANGED")
        stamp = datetime.now(timezone.utc)
        reason_payload = {"job_id": job_id, "keyword_ids": identifiers,
                          "reason": "OpenRouter AI classification re-run"}
        run_reason = _AI_TARGETED_RUN_PREFIX + json.dumps(
            reason_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

        def operation(db):
            existing_run = db.execute(sql.SQL("SELECT status FROM {} WHERE id=%s FOR UPDATE")
                                      .format(self._table("membership_reconciliation_runs")),
                                      (run_id,)).fetchone()
            proposal_ids = [proposal.get("id") for proposal in proposals
                            if isinstance(proposal, dict) and isinstance(proposal.get("id"), str)]
            if existing_run:
                if existing_run["status"] != "APPLIED":
                    raise ValueError("AI_INPUT_CHANGED")
                present = db.execute(sql.SQL("SELECT id FROM {} WHERE id=ANY(%s)")
                                     .format(self._table("proposals")), (proposal_ids,)).fetchall() if proposal_ids else []
                scope = sql.SQL(" AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
                parameters = (identifiers, self.data_mode_scope) if self.data_mode_scope else (identifiers,)
                current_assignments = db.execute(sql.SQL("""SELECT m.collection_id,m.keyword_id,m.score,m.reason
                    FROM {} m JOIN {} k ON k.id=m.keyword_id WHERE m.origin='LOCAL_AI'
                    AND m.keyword_id=ANY(%s)""").format(self._table("memberships"),
                        self._table("keywords")) + scope, parameters).fetchall()
                return {"assigned": len(current_assignments),
                        "assignments": [dict(row) for row in current_assignments],
                        "proposal_ids": [row["id"] for row in present],
                        "membership_reconciliation_id": run_id}

            # Lock the keyword and candidate Collection versions used by the prompt.
            keywords = db.execute(sql.SQL("SELECT id,normalized,eligible,data_mode FROM {} "
                "WHERE id=ANY(%s) FOR UPDATE").format(self._table("keywords")), (identifiers,)).fetchall()
            if len(keywords) != len(identifiers):
                raise ValueError("AI_KEYWORD_NOT_FOUND")
            if self.data_mode_scope and any(row["data_mode"] != self.data_mode_scope for row in keywords):
                raise ValueError("AI_KEYWORD_NOT_FOUND")
            if any(not row["eligible"] or row["normalized"] != expected_keyword_texts.get(row["id"], row["normalized"])
                   for row in keywords):
                raise ValueError("AI_INPUT_CHANGED")
            if expected_keyword_states:
                sources = sorted(set(allowed_sources or ()))
                states = db.execute(sql.SQL("""SELECT k.id,k.normalized,k.source,k.eligible,
                    COALESCE(kd.decision,'') AS decision,
                    COALESCE(cr.classification_status,'') AS classification_status,
                    COALESCE((SELECT jsonb_agg(DISTINCT ek.evidence_id ORDER BY ek.evidence_id)
                        FROM {} ek JOIN {} se ON se.id=ek.evidence_id
                        WHERE ek.keyword_id=k.id AND se.source_id=ANY(%s)), '[]'::jsonb) AS evidence_ids,
                    EXISTS(SELECT 1 FROM {} m WHERE m.keyword_id=k.id AND m.origin='USER') AS user_assigned,
                    EXISTS(SELECT 1 FROM {} m WHERE m.keyword_id=k.id AND m.pinned=1) AS user_pinned,
                    COALESCE((SELECT jsonb_agg(DISTINCT f.collection_id ORDER BY f.collection_id)
                        FROM {} f WHERE f.keyword_id=k.id AND f.action='REMOVE'), '[]'::jsonb)
                        AS removed_collection_ids,
                    COALESCE(kd.updated_at::text,'') AS override_updated_at
                    FROM {} k LEFT JOIN {} kd ON kd.keyword_id=k.id
                    LEFT JOIN {} cr ON cr.keyword_id=k.id
                    WHERE k.id=ANY(%s)""").format(
                        self._table("evidence_keywords"), self._table("source_evidence"),
                        self._table("memberships"), self._table("memberships"),
                        self._table("feedback"), self._table("keywords"),
                        self._table("keyword_decisions"), self._table("classification_results")),
                    (sources, identifiers)).fetchall()
                actual_states = {row["id"]: row for row in states}
                if set(actual_states) != set(identifiers):
                    raise ValueError("AI_INPUT_CHANGED")
                for identifier, expected in expected_keyword_states.items():
                    actual = actual_states.get(identifier)
                    if not isinstance(expected, dict) or actual is None or any(
                            actual.get(field) != expected.get(field) for field in (
                                "normalized", "source", "eligible", "decision", "classification_status",
                                "evidence_ids", "user_assigned", "user_pinned", "removed_collection_ids",
                                "override_updated_at")):
                        raise ValueError("AI_INPUT_CHANGED")
            if expected_collection_versions:
                collection_ids = list(expected_collection_versions)
                versions = db.execute(sql.SQL("SELECT id,version,status,auto_match FROM {} "
                    "WHERE id=ANY(%s) FOR SHARE").format(self._table("collections")),
                    (collection_ids,)).fetchall()
                actual = {row["id"]: row for row in versions}
                if (set(actual) != set(expected_collection_versions)
                        or any(actual[identifier]["version"] != expected_collection_versions[identifier]
                               or actual[identifier]["status"] != "ACTIVE"
                               or (identifier in expected_collection_auto_match
                                   and bool(actual[identifier]["auto_match"])
                                   != expected_collection_auto_match[identifier])
                               for identifier in expected_collection_versions)):
                    raise ValueError("AI_INPUT_CHANGED")
            scope = sql.SQL(" AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
            scope_parameters = (self.data_mode_scope,) if self.data_mode_scope else ()
            current_rows = db.execute(sql.SQL("""SELECT m.collection_id,m.keyword_id,m.score,m.reason
                FROM {} m JOIN {} k ON k.id=m.keyword_id
                WHERE m.origin='LOCAL_AI' AND m.pinned=0 AND m.keyword_id=ANY(%s)""").format(
                    self._table("memberships"), self._table("keywords")) + scope,
                (identifiers, *scope_parameters)).fetchall()
            current_assignments = {(row["collection_id"], row["keyword_id"]):
                                   (float(row["score"]) if row["score"] is not None else None,
                                    row["reason"]) for row in current_rows}
            desired_assignments = {(collection_id, keyword_id): (float(score), str(reason)[:500])
                                   for collection_id, keyword_id, score, reason in assignments}
            reconcile = current_assignments != desired_assignments
            if reconcile:
                db.execute(sql.SQL("""INSERT INTO {}(id,created_at,reason,status)
                    VALUES (%s,%s,%s,'APPLYING')""").format(self._table("membership_reconciliation_runs")),
                    (run_id, stamp, run_reason))
                db.execute(sql.SQL("""INSERT INTO {}(run_id,collection_id,keyword_id,origin,pinned,score,reason,created_at)
                    SELECT %s,m.collection_id,m.keyword_id,m.origin,m.pinned,m.score,m.reason,m.created_at
                    FROM {} m JOIN {} k ON k.id=m.keyword_id
                    WHERE m.origin='LOCAL_AI' AND m.pinned=0 AND m.keyword_id=ANY(%s)""").format(
                        self._table("membership_reconciliation_items"), self._table("memberships"),
                        self._table("keywords")) + scope,
                    (run_id, identifiers, *scope_parameters))
                db.execute(sql.SQL("DELETE FROM {} m USING {} k WHERE m.keyword_id=k.id "
                    "AND m.origin='LOCAL_AI' AND m.pinned=0 AND m.keyword_id=ANY(%s)").format(
                        self._table("memberships"), self._table("keywords")) + scope,
                    (identifiers, *scope_parameters))

                for offset in range(0, len(assignments), 500):
                    batch = assignments[offset:offset + 500]
                    if not batch:
                        continue
                    values = sql.SQL(",").join(sql.SQL("(%s,%s,%s::double precision,%s,%s::timestamptz)")
                                               for _ in batch)
                    params = [value for collection_id, keyword_id, score, reason in batch
                              for value in (collection_id, keyword_id, score, str(reason)[:500], stamp)]
                    db.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,origin,pinned,score,reason,created_at)
                        SELECT v.collection_id,v.keyword_id,'LOCAL_AI',0,v.score,v.reason,v.created_at
                        FROM (VALUES {}) AS v(collection_id,keyword_id,score,reason,created_at)
                        JOIN {} c ON c.id=v.collection_id AND c.status='ACTIVE' AND c.auto_match=1
                        JOIN {} k ON k.id=v.keyword_id AND k.eligible=1
                        WHERE NOT EXISTS(SELECT 1 FROM {} f WHERE f.collection_id=v.collection_id
                            AND f.keyword_id=v.keyword_id AND f.action='REMOVE')
                        AND NOT EXISTS(SELECT 1 FROM {} manual WHERE manual.keyword_id=v.keyword_id
                            AND manual.origin='USER')
                        AND NOT EXISTS(SELECT 1 FROM {} kd WHERE kd.keyword_id=v.keyword_id
                            AND kd.decision='EXCLUDED')
                        AND NOT EXISTS(SELECT 1 FROM {} pinned WHERE pinned.keyword_id=v.keyword_id
                            AND pinned.pinned=1)
                        ON CONFLICT(collection_id,keyword_id) DO NOTHING""").format(
                            self._table("memberships"), values, self._table("collections"),
                            self._table("keywords"), self._table("feedback"), self._table("memberships"),
                            self._table("keyword_decisions"), self._table("memberships")),
                            params)
                db.execute(sql.SQL("UPDATE {} SET status='APPLIED' WHERE id=%s")
                           .format(self._table("membership_reconciliation_runs")), (run_id,))

            persisted_assignments = db.execute(sql.SQL("""SELECT m.collection_id,m.keyword_id,m.score,m.reason
                FROM {} m JOIN {} k ON k.id=m.keyword_id
                WHERE m.origin='LOCAL_AI' AND m.keyword_id=ANY(%s)""").format(
                    self._table("memberships"), self._table("keywords")) + scope,
                (identifiers, *scope_parameters)).fetchall()

            created_proposals = []
            for proposal in proposals:
                if not isinstance(proposal, dict):
                    continue
                proposal_id = proposal.get("id")
                name = proposal.get("name")
                description = proposal.get("description")
                support_ids = proposal.get("support_ids")
                if (not isinstance(proposal_id, str) or not proposal_id or len(proposal_id) > 128
                        or not isinstance(name, str) or not 3 <= len(name.strip()) <= 80
                        or not isinstance(description, str) or len(description) > 1200
                        or not isinstance(support_ids, list) or len(support_ids) < 2
                        or any(not isinstance(value, str) for value in support_ids)
                        or len(support_ids) != len(set(support_ids))
                        or any(value not in identifiers for value in support_ids)):
                    continue
                eligible_support = db.execute(sql.SQL("""SELECT k.id FROM {} k
                    WHERE k.id=ANY(%s) AND k.eligible=1
                    AND NOT EXISTS(SELECT 1 FROM {} kd WHERE kd.keyword_id=k.id AND kd.decision='EXCLUDED')
                    AND NOT EXISTS(SELECT 1 FROM {} m WHERE m.keyword_id=k.id
                        AND (m.origin='USER' OR m.pinned=1))""").format(
                        self._table("keywords"), self._table("keyword_decisions"),
                        self._table("memberships")), (support_ids,)).fetchall()
                if len(eligible_support) != len(support_ids):
                    continue
                name_fingerprint = fingerprint(name)
                suppressed = db.execute(sql.SQL("SELECT 1 FROM {} WHERE fingerprint=%s")
                                        .format(self._table("suppression")),
                                        (name_fingerprint,)).fetchone()
                if suppressed:
                    continue
                labels = db.execute(sql.SQL("SELECT name FROM {} WHERE status='ACTIVE' "
                    "UNION ALL SELECT name FROM {} WHERE status='PENDING'")
                    .format(self._table("collections"), self._table("proposals"))).fetchall()
                if any(fingerprint(row["name"]) == name_fingerprint for row in labels):
                    continue
                metadata = {
                    "reason": str(proposal.get("reason", ""))[:500],
                    "score": proposal.get("score"),
                    "uncertainty": str(proposal.get("uncertainty", ""))[:500],
                    "evidence_ids": proposal.get("evidence_ids", []),
                    "source_ids": proposal.get("source_ids", []),
                    "model_id": str(proposal.get("model_id", ""))[:200],
                    "job_id": job_id,
                }
                stored_reason = _AI_PROPOSAL_REASON_PREFIX + json.dumps(
                    metadata, ensure_ascii=False, sort_keys=True, allow_nan=False,
                    separators=(",", ":"))
                db.execute(sql.SQL("""INSERT INTO {}(id,name,description,support_ids,reason,status,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,'PENDING',%s,%s) ON CONFLICT(id) DO NOTHING""").format(
                        self._table("proposals")),
                    (proposal_id, name.strip(), description, Jsonb(support_ids), stored_reason, stamp, stamp))
                found = db.execute(sql.SQL("SELECT id FROM {} WHERE id=%s AND status='PENDING'")
                                   .format(self._table("proposals")), (proposal_id,)).fetchone()
                if found:
                    created_proposals.append(proposal_id)
            return {"assigned": len(persisted_assignments),
                    "assignments": [dict(row) for row in persisted_assignments],
                    "proposal_ids": created_proposals,
                    "membership_reconciliation_id": run_id if reconcile else None}

        return self._write(operation)

    def feedback_action(self, collection_id, record, action, *, pinned=True):
        if self.data_mode_scope and record.get("data_mode") != self.data_mode_scope:
            raise ValueError("KEYWORD_DATA_MODE_MISMATCH")
        keyword_id = record["id"]

        def operation(db):
            existing = db.execute(sql.SQL("SELECT data_mode FROM {} WHERE id=%s")
                                  .format(self._table("keywords")), (keyword_id,)).fetchone()
            if self.data_mode_scope and existing and existing["data_mode"] != self.data_mode_scope:
                raise ValueError("KEYWORD_DATA_MODE_MISMATCH")
            evidence = record.get("evidence", {})
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence)
                except (TypeError, ValueError):
                    evidence = {}
            db.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,observed_at,
                data_mode,evidence,context) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET evidence=EXCLUDED.evidence,observed_at=EXCLUDED.observed_at
                WHERE EXCLUDED.keyword<>''""").format(self._table("keywords")),
                (keyword_id, record["keyword"], record["normalized"], record["source"],
                 record["project"], record["observed_at"], record["data_mode"], Jsonb(evidence),
                 record["context"]))
            stamp = datetime.now(timezone.utc)
            db.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,action,created_at)
                VALUES (%s,%s,%s,%s) ON CONFLICT(collection_id,keyword_id) DO UPDATE
                SET action=EXCLUDED.action,created_at=EXCLUDED.created_at""").format(self._table("feedback")),
                (collection_id, keyword_id, action, stamp))
            if action == "REMOVE":
                db.execute(sql.SQL("DELETE FROM {} WHERE collection_id=%s AND keyword_id=%s")
                           .format(self._table("memberships")), (collection_id, keyword_id))
            elif action == "ADD":
                db.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,origin,pinned,score,reason,created_at)
                    VALUES (%s,%s,'USER',%s,NULL,'Manually added',%s)
                    ON CONFLICT(collection_id,keyword_id) DO UPDATE SET origin='USER',pinned=EXCLUDED.pinned,
                        score=NULL,reason=EXCLUDED.reason,created_at=EXCLUDED.created_at""").format(
                            self._table("memberships")), (collection_id, keyword_id, int(pinned), stamp))
            db.execute(sql.SQL("UPDATE {} SET version=version+1,updated_at=%s WHERE id=%s")
                       .format(self._table("collections")), (stamp, collection_id))
        self._write(operation)

    def bulk_membership_action(self, keyword_ids, action, *, collection_id=None, source_collection_id=None):
        ids = list(dict.fromkeys(keyword_ids))
        stamp = datetime.now(timezone.utc)
        operation = self._bulk_action_operation(
            ids, action, collection_id, source_collection_id, stamp=stamp,
        )
        return self._write(operation)

    def _bulk_action_operation(self, keyword_ids, action, collection_id, source_collection_id,
                               *, stamp, touch=True, validate_collections=True):
        ids = list(dict.fromkeys(keyword_ids))

        def operation(db):
            def touch_collections():
                if touch and collection_id:
                    db.execute(sql.SQL("UPDATE {} SET version=version+1,updated_at=%s WHERE id=%s")
                               .format(self._table("collections")), (stamp, collection_id))
                if touch and source_collection_id and source_collection_id != collection_id:
                    db.execute(sql.SQL("UPDATE {} SET version=version+1,updated_at=%s WHERE id=%s")
                               .format(self._table("collections")), (stamp, source_collection_id))

            if validate_collections:
                for identifier in (collection_id, source_collection_id):
                    if identifier:
                        found = db.execute(sql.SQL("SELECT 1 FROM {} WHERE id=%s AND status='ACTIVE'")
                                           .format(self._table("collections")), (identifier,)).fetchone()
                        if not found:
                            raise ValueError("COLLECTION_NOT_FOUND")
            if not ids:
                touch_collections()
                return {"updated": 0, "skipped": 0, "failed": 0}

            keyword_scope = sql.SQL(" AND data_mode=%s") if self.data_mode_scope else sql.SQL("")
            mode_parameters = (self.data_mode_scope,) if self.data_mode_scope else ()
            if action in {"EXCLUDE", "RESTORE"}:
                valid_ids = [row["id"] for row in db.execute(sql.SQL("SELECT id FROM {} WHERE id=ANY(%s)")
                    .format(self._table("keywords")) + keyword_scope,
                    (ids, *mode_parameters)).fetchall()]
                if valid_ids:
                    decision, reason = (("EXCLUDED", "User excluded") if action == "EXCLUDE"
                                        else ("RESTORED", "User restored"))
                    db.execute(sql.SQL("""INSERT INTO {}(keyword_id,decision,reason,updated_at)
                        SELECT id,%s,%s,%s FROM unnest(%s::text[]) AS input(id)
                        ON CONFLICT(keyword_id) DO UPDATE SET decision=EXCLUDED.decision,
                            reason=EXCLUDED.reason,updated_at=EXCLUDED.updated_at""").format(
                                self._table("keyword_decisions")), (decision, reason, stamp, valid_ids))
                    if action == "EXCLUDE":
                        db.execute(sql.SQL("UPDATE {} SET eligible=0,rejection_reason='USER_EXCLUDED' "
                                           "WHERE id=ANY(%s)").format(self._table("keywords")), (valid_ids,))
                        db.execute(sql.SQL("DELETE FROM {} WHERE keyword_id=ANY(%s) AND origin='LOCAL_AI'")
                                   .format(self._table("memberships")), (valid_ids,))
                    else:
                        db.execute(sql.SQL("""UPDATE {} k SET
                            eligible=CASE WHEN COALESCE((SELECT classification_status FROM {} cr
                                WHERE cr.keyword_id=k.id),'')='INELIGIBLE' THEN 0 ELSE 1 END,
                            rejection_reason=CASE WHEN COALESCE((SELECT classification_status FROM {} cr
                                WHERE cr.keyword_id=k.id),'')='INELIGIBLE'
                                THEN 'CLASSIFICATION_INELIGIBLE' ELSE NULL END WHERE k.id=ANY(%s)""").format(
                                    self._table("keywords"), self._table("classification_results"),
                                    self._table("classification_results")), (valid_ids,))
                touch_collections()
                return {"updated": len(valid_ids), "skipped": len(ids) - len(valid_ids), "failed": 0}

            if action in {"REMOVE", "REJECT"}:
                member_scope = sql.SQL(" AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
                valid_ids = [row["keyword_id"] for row in db.execute(sql.SQL("""SELECT m.keyword_id
                    FROM {} m JOIN {} k ON k.id=m.keyword_id
                    WHERE m.collection_id=%s AND m.keyword_id=ANY(%s)""").format(
                        self._table("memberships"), self._table("keywords")) + member_scope,
                    (collection_id, ids, *mode_parameters)).fetchall()]
                if action == "REJECT" and valid_ids:
                    db.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,action,created_at)
                        SELECT %s,id,'REMOVE',%s FROM unnest(%s::text[]) AS input(id)
                        ON CONFLICT(collection_id,keyword_id) DO UPDATE SET action='REMOVE',
                            created_at=EXCLUDED.created_at""").format(self._table("feedback")),
                        (collection_id, stamp, valid_ids))
                if valid_ids:
                    db.execute(sql.SQL("DELETE FROM {} WHERE collection_id=%s AND keyword_id=ANY(%s)")
                               .format(self._table("memberships")), (collection_id, valid_ids))
                touch_collections()
                return {"updated": len(valid_ids), "skipped": len(ids) - len(valid_ids), "failed": 0}

            eligible_sql = sql.SQL("""SELECT k.id FROM {} k WHERE k.id=ANY(%s) AND k.eligible=1
                AND COALESCE((SELECT classification_status FROM {} cr WHERE cr.keyword_id=k.id),'')<>'INELIGIBLE'""")
            eligible_ids = [row["id"] for row in db.execute(eligible_sql.format(
                self._table("keywords"), self._table("classification_results")) + keyword_scope,
                (ids, *mode_parameters)).fetchall()]
            if action not in {"ADD", "COPY", "MOVE", "APPROVE"}:
                if eligible_ids:
                    raise ValueError("INVALID_BULK_ACTION")
                touch_collections()
                return {"updated": 0, "skipped": len(ids), "failed": 0}
            if eligible_ids:
                existing_count = db.execute(sql.SQL("SELECT count(*) AS n FROM {} "
                    "WHERE collection_id=%s AND keyword_id=ANY(%s)").format(self._table("memberships")),
                    (collection_id, eligible_ids)).fetchone()["n"]
                db.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,action,created_at)
                    SELECT %s,id,'ADD',%s FROM unnest(%s::text[]) AS input(id)
                    ON CONFLICT(collection_id,keyword_id) DO UPDATE SET action='ADD',
                        created_at=EXCLUDED.created_at""").format(self._table("feedback")),
                    (collection_id, stamp, eligible_ids))
                db.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,origin,pinned,score,reason,created_at)
                    SELECT %s,id,'USER',1,NULL,'Manually approved',%s FROM unnest(%s::text[]) AS input(id)
                    ON CONFLICT(collection_id,keyword_id) DO UPDATE SET origin='USER',pinned=1,score=NULL,
                        reason=EXCLUDED.reason,created_at=EXCLUDED.created_at""").format(
                            self._table("memberships")), (collection_id, stamp, eligible_ids))
                if action == "MOVE" and source_collection_id and source_collection_id != collection_id:
                    db.execute(sql.SQL("DELETE FROM {} WHERE collection_id=%s AND keyword_id=ANY(%s)")
                               .format(self._table("memberships")), (source_collection_id, eligible_ids))
                    db.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,action,created_at)
                        SELECT %s,id,'REMOVE',%s FROM unnest(%s::text[]) AS input(id)
                        ON CONFLICT(collection_id,keyword_id) DO UPDATE SET action='REMOVE',
                            created_at=EXCLUDED.created_at""").format(self._table("feedback")),
                        (source_collection_id, stamp, eligible_ids))
            else:
                existing_count = 0
            touch_collections()
            return {"updated": len(eligible_ids) if action == "APPROVE" else len(eligible_ids) - existing_count,
                    "skipped": len(ids) - len(eligible_ids), "failed": 0}
        return operation

    def bulk_membership_filtered(self, *, query="", state="all", collection_ids=(), source="",
                                 classification="all", evidence="all", date_from="", date_to="",
                                 action, collection_id=None, source_collection_id=None,
                                 batch_size=500):
        if type(batch_size) is not int or not 1 <= batch_size <= 500:
            raise ValueError("INVALID_BULK_BATCH_SIZE")
        ids_query, parameters = self.keyword_ids_query(
            query=query, state=state, collection_ids=collection_ids, source=source, sort="recent",
            classification=classification, evidence=evidence, date_from=date_from, date_to=date_to)
        stamp = datetime.now(timezone.utc)

        def operation(db):
            for identifier in (collection_id, source_collection_id):
                if identifier:
                    found = db.execute(sql.SQL("SELECT 1 FROM {} WHERE id=%s AND status='ACTIVE'")
                                       .format(self._table("collections")), (identifier,)).fetchone()
                    if not found:
                        raise ValueError("COLLECTION_NOT_FOUND")
            updated = skipped = seen = 0
            with db.cursor(name="trendscout_filtered_membership_ids") as cursor:
                cursor.itersize = batch_size
                cursor.execute(ids_query, parameters)
                while True:
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        break
                    ids = [row["id"] for row in rows]
                    seen += len(ids)
                    result = self._bulk_action_operation(ids, action, collection_id,
                        source_collection_id, stamp=stamp, touch=False,
                        validate_collections=False)(db)
                    updated += result["updated"]
                    skipped += result["skipped"]
            if not seen:
                raise ValueError("KEYWORDS_REQUIRED")
            if collection_id:
                db.execute(sql.SQL("UPDATE {} SET version=version+1,updated_at=%s WHERE id=%s")
                           .format(self._table("collections")), (stamp, collection_id))
            if source_collection_id and source_collection_id != collection_id:
                db.execute(sql.SQL("UPDATE {} SET version=version+1,updated_at=%s WHERE id=%s")
                           .format(self._table("collections")), (stamp, source_collection_id))
            return {"updated": updated, "skipped": skipped, "failed": 0}
        return self._write(operation)

    def create_proposal(self, proposal_id, name, description, support_ids, reason, created_at, updated_at):
        def operation(db):
            db.execute(sql.SQL("""INSERT INTO {}(id,name,description,support_ids,reason,status,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,'PENDING',%s,%s)""").format(self._table("proposals")),
                (proposal_id, name, description, Jsonb(support_ids), reason, created_at, updated_at))

        return self._write(operation)

    def suggestions(self):
        def operation(db):
            scope = (sql.SQL("AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(p.support_ids) s(value) "
                             "JOIN {} k ON k.id=s.value WHERE k.data_mode=%s)").format(self._table("keywords"))
                     if self.data_mode_scope else sql.SQL(""))
            parameters = (self.data_mode_scope,) if self.data_mode_scope else ()
            rows = db.execute(sql.SQL("SELECT p.* FROM {} p WHERE p.status='PENDING' ").format(
                self._table("proposals")) + scope + sql.SQL(" ORDER BY p.created_at DESC"), parameters).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                metadata = _decode_ai_proposal_reason(item.get("reason"))
                item["reason"] = metadata.get("reason", "")
                for field in ("score", "uncertainty", "evidence_ids", "source_ids", "model_id"):
                    if field in metadata:
                        item["model_" + field if field == "score" else field] = metadata[field]
                result.append(item)
            return result
        return self._read(operation)

    def patch_proposal(self, proposal_id, *, name=None, description=None, support_ids=None,
                       allowed_sources=None):
        if not isinstance(proposal_id, str) or not proposal_id or len(proposal_id) > 128:
            return False
        if name is not None and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 80):
            raise ValueError("INVALID_PROPOSAL_NAME")
        if description is not None and (not isinstance(description, str) or len(description) > 1200):
            raise ValueError("INVALID_PROPOSAL_DESCRIPTION")
        if support_ids is not None and (not isinstance(support_ids, list) or len(support_ids) < 2
                or len(support_ids) > 200
                or any(not isinstance(value, str) or not value or len(value) > 64 for value in support_ids)
                or len(support_ids) != len(set(support_ids))):
            raise ValueError("INVALID_PROPOSAL_KEYWORDS")
        stamp = datetime.now(timezone.utc)

        def operation(db):
            row = db.execute(sql.SQL("SELECT * FROM {} WHERE id=%s AND status='PENDING' FOR UPDATE")
                             .format(self._table("proposals")), (proposal_id,)).fetchone()
            if row is None:
                return False
            assignments, parameters = [], []
            if name is not None:
                assignments.append("name=%s")
                parameters.append(name.strip())
            if description is not None:
                assignments.append("description=%s")
                parameters.append(description.strip())
            if support_ids is not None:
                sources = sorted(set(allowed_sources or ()))
                if not sources:
                    raise ValueError("AI_POLICY_DENIED")
                mode_filter = sql.SQL(" AND data_mode=%s") if self.data_mode_scope else sql.SQL("")
                mode_params = (self.data_mode_scope,) if self.data_mode_scope else ()
                found = db.execute(sql.SQL("""SELECT k.id FROM {} k WHERE k.id=ANY(%s)
                    AND k.eligible=1 AND k.source=ANY(%s)
                    AND NOT EXISTS(SELECT 1 FROM {} kd WHERE kd.keyword_id=k.id AND kd.decision='EXCLUDED')
                    AND NOT EXISTS(SELECT 1 FROM {} m WHERE m.keyword_id=k.id AND m.origin='USER')
                    AND EXISTS(SELECT 1 FROM {} ek JOIN {} se ON se.id=ek.evidence_id
                        WHERE ek.keyword_id=k.id AND se.source_id=ANY(%s))""").format(
                            self._table("keywords"), self._table("keyword_decisions"),
                            self._table("memberships"), self._table("evidence_keywords"),
                            self._table("source_evidence")) + mode_filter,
                    (support_ids, sources, sources, *mode_params)).fetchall()
                if len(found) != len(support_ids):
                    raise ValueError("AI_KEYWORD_NOT_FOUND")
                if isinstance(row.get("reason"), str) and row["reason"].startswith(_AI_PROPOSAL_REASON_PREFIX):
                    metadata = _decode_ai_proposal_reason(row["reason"])
                    evidence = db.execute(sql.SQL("""SELECT DISTINCT se.id,se.source_id FROM {} ek
                        JOIN {} se ON se.id=ek.evidence_id WHERE ek.keyword_id=ANY(%s)
                        AND se.source_id=ANY(%s) ORDER BY se.id""").format(
                            self._table("evidence_keywords"), self._table("source_evidence")),
                        (support_ids, sources)).fetchall()
                    metadata["evidence_ids"] = [item["id"] for item in evidence]
                    metadata["source_ids"] = sorted({item["source_id"] for item in evidence})
                    assignments.append("reason=%s")
                    parameters.append(_AI_PROPOSAL_REASON_PREFIX + json.dumps(
                        metadata, ensure_ascii=False, sort_keys=True, allow_nan=False,
                        separators=(",", ":")))
                assignments.append("support_ids=%s")
                parameters.append(Jsonb(support_ids))
            if name is not None:
                name_fingerprint = fingerprint(name.strip())
                suppressed = db.execute(sql.SQL("SELECT 1 FROM {} WHERE fingerprint=%s")
                    .format(self._table("suppression")), (name_fingerprint,)).fetchone()
                duplicate = db.execute(sql.SQL("SELECT 1 FROM {} WHERE status='ACTIVE' AND id<>%s "
                    "AND lower(name)=lower(%s) UNION ALL SELECT 1 FROM {} WHERE status='PENDING' "
                    "AND id<>%s AND lower(name)=lower(%s) LIMIT 1").format(
                        self._table("collections"), self._table("proposals")),
                    (proposal_id, name.strip(), proposal_id, name.strip())).fetchone()
                if suppressed or duplicate:
                    raise ValueError("COLLECTION_NAME_CONFLICT")
            if not assignments:
                raise ValueError("EMPTY_UPDATE")
            assignments.append("updated_at=%s")
            parameters.extend((stamp, proposal_id))
            db.execute(sql.SQL("UPDATE {} SET " + ",".join(assignments) + " WHERE id=%s")
                       .format(self._table("proposals")), parameters)
            return True
        return self._write(operation)

    def resolve_proposal(self, proposal_id, accept, *, name=None, description=None,
                         allowed_sources=None):
        if name is not None and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 80):
            raise ValueError("INVALID_PROPOSAL_NAME")
        if description is not None and (not isinstance(description, str) or len(description) > 1200):
            raise ValueError("INVALID_PROPOSAL_DESCRIPTION")
        name = name.strip() if name is not None else None
        description = description.strip() if description is not None else None

        def operation(db):
            row = db.execute(sql.SQL("SELECT * FROM {} WHERE id=%s AND status='PENDING' FOR UPDATE")
                             .format(self._table("proposals")), (proposal_id,)).fetchone()
            if row is None:
                return None
            support_ids = row["support_ids"]
            if isinstance(support_ids, str):
                support_ids = json.loads(support_ids)
            if (not isinstance(support_ids, list) or not all(isinstance(item, str) for item in support_ids)
                    or len(support_ids) != len(set(support_ids))):
                return None
            ai_proposal = (isinstance(row.get("reason"), str)
                           and row["reason"].startswith(_AI_PROPOSAL_REASON_PREFIX))
            if accept and ai_proposal and len(support_ids) < 2:
                return None
            ai_metadata = _decode_ai_proposal_reason(row.get("reason")) if ai_proposal else {}
            if accept and ai_proposal:
                sources = ai_metadata.get("source_ids")
                evidence_ids = ai_metadata.get("evidence_ids")
                allowed = set(allowed_sources or ())
                if (not allowed or not isinstance(sources, list) or not sources
                        or any(source not in allowed for source in sources)
                        or not isinstance(evidence_ids, list) or not evidence_ids
                        or any(not isinstance(value, str) for value in evidence_ids)):
                    return None
            if support_ids:
                mode_filter = sql.SQL(" AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
                mode_params = (self.data_mode_scope,) if self.data_mode_scope else ()
                supported = db.execute(sql.SQL("""SELECT count(*) AS n FROM {} k
                    WHERE k.id=ANY(%s) AND k.eligible=1
                    AND NOT EXISTS(SELECT 1 FROM {} kd WHERE kd.keyword_id=k.id AND kd.decision='EXCLUDED')""").format(
                        self._table("keywords"), self._table("keyword_decisions")) + mode_filter,
                    (support_ids, *mode_params)).fetchone()["n"]
                if supported != len(support_ids):
                    return None
            if accept and ai_proposal:
                allowed = set(allowed_sources or ())
                supported = db.execute(sql.SQL("SELECT id FROM {} WHERE id=ANY(%s) "
                    "AND eligible=1 AND source=ANY(%s)").format(self._table("keywords")),
                    (support_ids, sorted(allowed))).fetchall()
                if not allowed or len(supported) != len(support_ids):
                    return None
                references = db.execute(sql.SQL("""SELECT DISTINCT se.id FROM {} ek
                    JOIN {} se ON se.id=ek.evidence_id WHERE ek.keyword_id=ANY(%s)
                    AND se.id=ANY(%s) AND se.source_id=ANY(%s) ORDER BY se.id""").format(
                        self._table("evidence_keywords"), self._table("source_evidence")),
                    (support_ids, ai_metadata["evidence_ids"], sorted(allowed))).fetchall()
                if {item["id"] for item in references} != set(ai_metadata["evidence_ids"]):
                    return None
            stamp = datetime.now(timezone.utc)
            status = "ACCEPTED" if accept else "REJECTED"
            collection_name = name if name is not None else row["name"]
            if accept and (ai_proposal or name is not None):
                name_fingerprint = fingerprint(collection_name)
                suppressed = db.execute(sql.SQL("SELECT 1 FROM {} WHERE fingerprint=%s")
                    .format(self._table("suppression")), (name_fingerprint,)).fetchone()
                labels = db.execute(sql.SQL("SELECT name FROM {} WHERE status='ACTIVE' "
                    "UNION ALL SELECT name FROM {} WHERE status='PENDING' AND id<>%s").format(
                        self._table("collections"), self._table("proposals")),
                    (proposal_id,)).fetchall()
                if suppressed or any(fingerprint(item["name"]) == name_fingerprint for item in labels):
                    return None
            db.execute(sql.SQL("UPDATE {} SET status=%s,updated_at=%s WHERE id=%s")
                       .format(self._table("proposals")), (status, stamp, proposal_id))
            if not accept:
                db.execute(sql.SQL("""INSERT INTO {}(fingerprint,kind,created_at,label)
                    VALUES (%s,'REJECTED_PROPOSAL',%s,%s) ON CONFLICT(fingerprint) DO UPDATE
                    SET kind=EXCLUDED.kind,created_at=EXCLUDED.created_at,label=EXCLUDED.label""")
                    .format(self._table("suppression")), (fingerprint(row["name"]), stamp, row["name"]))
                return ""
            identifier = str(uuid.uuid4())
            collection_name = name if name is not None else row["name"]
            collection_description = description if description is not None else row["description"]
            reason = _decode_ai_proposal_reason(row["reason"]).get("reason", "")
            db.execute(sql.SQL("""INSERT INTO {}(id,name,description,created_by,reason,created_at,updated_at)
                VALUES (%s,%s,%s,'AI',%s,%s,%s)""").format(self._table("collections")),
                (identifier, collection_name, collection_description, reason, stamp, stamp))
            if support_ids:
                with db.cursor() as cursor:
                    cursor.executemany(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,origin,pinned,
                        score,reason,created_at) VALUES (%s,%s,'LOCAL_AI',0,NULL,%s,%s)
                        ON CONFLICT(collection_id,keyword_id) DO NOTHING""").format(self._table("memberships")),
                        [(identifier, key, "Representative keyword in accepted AI suggestion", stamp)
                         for key in support_ids])
            return identifier
        return self._write(operation)

    def keywords_for(self, collection_id):
        rows = self._read(lambda db: db.execute(sql.SQL("""WITH RECURSIVE topic_tree(id) AS (
                SELECT id FROM {} WHERE id=%s
                UNION ALL SELECT c.id FROM {} c JOIN topic_tree p ON c.parent_id=p.id
            ) SELECT k.*,m.origin,m.pinned,m.score,m.reason,e.entity_id,e.entity_type,
                e.description AS entity_description,e.occupations AS entity_occupations,
                e.category_ids AS entity_category_ids,e.source_url AS entity_source_url,
                e.method AS entity_classification_method,cr.classification_status
            FROM {} m JOIN {} k ON k.id=m.keyword_id
            LEFT JOIN {} e ON e.keyword_id=k.id LEFT JOIN {} cr ON cr.keyword_id=k.id
            WHERE m.collection_id IN (SELECT id FROM topic_tree)""").format(
                self._table("collections"), self._table("collections"), self._table("memberships"),
                self._table("keywords"), self._table("entity_context"),
                self._table("classification_results")) +
            (sql.SQL(" AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")) +
            sql.SQL(" ORDER BY translate(k.keyword,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz') COLLATE \"C\",k.keyword COLLATE \"C\",k.id COLLATE \"C\""),
            (collection_id, self.data_mode_scope) if self.data_mode_scope else (collection_id,)).fetchall())
        for item in rows:
            if item.get("classification_status") == "INELIGIBLE":
                item["eligible"] = 0
                item["topic_eligible"] = False
            else:
                item["topic_eligible"] = bool(item.get("eligible"))
        return rows
