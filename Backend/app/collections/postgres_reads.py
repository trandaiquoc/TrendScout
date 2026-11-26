"""PostgreSQL collection summary reads; not yet composed into runtime."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.storage.postgres_connection import PostgresConnectionError, postgres_connection
from app.storage.postgres_migrations import _SCHEMA_NAME


class CollectionReadError(RuntimeError):
    """Collection data could not be read without changing its meaning."""


def _json_result(row):
    return {key: value.astimezone(timezone.utc).isoformat() if isinstance(value, datetime) else value
            for key, value in row.items()}


def _sqlite_json_integer(field):
    # SQLite CAST(... AS INTEGER) accepts a numeric prefix, truncates real
    # values and clamps overflow. Keys are selected only from this allowlist.
    if field not in {"views_ceil", "provider_rank"}:
        raise ValueError("unsupported evidence sort field")
    value = f"k.evidence->'{field}'"
    text = f"k.evidence->>'{field}'"
    numeric = (f"CASE WHEN jsonb_typeof({value})='number' THEN trunc(({text})::numeric) "
               f"WHEN jsonb_typeof({value})='string' THEN "
               f"COALESCE(substring({text} from '^[[:space:]]*[+-]?[0-9]+')::numeric,0) "
               f"WHEN jsonb_typeof({value})='boolean' THEN CASE WHEN {text}='true' THEN 1 ELSE 0 END "
               "ELSE 0 END")
    return (f"CASE WHEN {value} IS NULL OR jsonb_typeof({value})='null' THEN NULL ELSE "
            f"greatest(-9223372036854775808::numeric,least(9223372036854775807::numeric,({numeric})))::bigint END")


class PostgresCollectionReader:
    def __init__(self, environment, *, schema="trendscout", data_mode_scope=None):
        if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
            raise ValueError("invalid PostgreSQL schema name")
        if data_mode_scope not in (None, "LIVE"):
            raise ValueError("unsupported Collections data mode scope")
        self._environment, self._schema = dict(environment), schema
        self.data_mode_scope = data_mode_scope

    def _table(self, name):
        return sql.SQL("{}.{}").format(sql.Identifier(self._schema), sql.Identifier(name))

    def _read(self, operation):
        try:
            with postgres_connection(self._environment) as db:
                db.row_factory = dict_row
                with db.transaction():
                    db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    db.execute("SELECT set_config('statement_timeout', '30s', true)")
                    return operation(db)
        except psycopg.Error as exc:
            state = getattr(exc, "sqlstate", None)
            raise CollectionReadError(f"collection database read failed ({state or 'database'})") from None
        except PostgresConnectionError:
            raise CollectionReadError("collection database read failed (connection)") from None

    def keyword_evidence_rows(self, *, source_id, project_prefix=None, data_mode=None,
                              eligible_only=False, limit=5000):
        """Read bounded source candidates without exposing a database connection."""
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id is required")
        if project_prefix is not None and (not isinstance(project_prefix, str) or not project_prefix):
            raise ValueError("project_prefix must be non-empty when provided")
        if data_mode is not None and (not isinstance(data_mode, str) or not data_mode.strip()):
            raise ValueError("data_mode must be non-empty when provided")
        if self.data_mode_scope and data_mode not in (None, self.data_mode_scope):
            raise ValueError("data_mode conflicts with store scope")
        if type(eligible_only) is not bool:
            raise TypeError("eligible_only must be a boolean")
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError("limit must be between 1 and 5000")

        conditions = [sql.SQL("source=%s")]
        parameters = [source_id]
        effective_mode = self.data_mode_scope or data_mode
        if effective_mode:
            conditions.append(sql.SQL("data_mode=%s"))
            parameters.append(effective_mode)
        if project_prefix is not None:
            conditions.append(sql.SQL("substring(project from 1 for char_length(%s))=%s"))
            parameters.extend((project_prefix, project_prefix))
        if eligible_only:
            conditions.append(sql.SQL("eligible=1"))
        parameters.append(limit)
        where = sql.SQL(" AND ").join(conditions)

        def read(db):
            rows = db.execute(
                sql.SQL("SELECT id,keyword,normalized,source,project,observed_at,data_mode,evidence,eligible "
                        "FROM {} WHERE {} ORDER BY observed_at DESC,id LIMIT %s")
                .format(self._table("keywords"), where), parameters,
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["evidence"] = json.dumps(item["evidence"], ensure_ascii=False, sort_keys=True,
                                              separators=(",", ":"))
                result.append(item)
            return result

        return self._read(read)

    def latest_dataset(self):
        scoped = self.data_mode_scope is not None
        params = (self.data_mode_scope,) if scoped else ()
        keyword_filter = sql.SQL("WHERE k.data_mode=%s") if scoped else sql.SQL("")
        dataset_filter = sql.SQL("WHERE data_mode=%s") if scoped else sql.SQL("")

        def read(db):
            snapshot = db.execute(sql.SQL("SELECT * FROM {} {} ORDER BY created_at DESC LIMIT 1")
                                  .format(self._table("dataset_snapshots"), dataset_filter), params).fetchone()
            counts = db.execute(sql.SQL("""SELECT count(*) AS total,
                count(*) FILTER (WHERE k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE') AS eligible,
                count(*) FILTER (WHERE (k.eligible=0 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE')
                    OR EXISTS (SELECT 1 FROM {} kd WHERE kd.keyword_id=k.id AND kd.decision='EXCLUDED')) AS excluded,
                count(*) FILTER (WHERE k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE'
                    AND EXISTS (SELECT 1 FROM {} m WHERE m.keyword_id=k.id)) AS assigned,
                count(*) FILTER (WHERE k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE'
                    AND NOT EXISTS (SELECT 1 FROM {} m WHERE m.keyword_id=k.id)) AS unassigned
                FROM {} k LEFT JOIN {} cr ON cr.keyword_id=k.id {}""").format(
                    self._table("keyword_decisions"), self._table("memberships"), self._table("memberships"),
                    self._table("keywords"), self._table("classification_results"), keyword_filter), params).fetchone()
            if snapshot is None:
                legacy = None if scoped else db.execute(sql.SQL("""SELECT * FROM {} WHERE status IN
                    ('COMPLETED','MODEL_UNAVAILABLE') ORDER BY updated_at DESC LIMIT 1""")
                    .format(self._table("fetch_jobs"))).fetchone()
                if legacy is None and not counts["total"]:
                    return None
                coverage = db.execute(sql.SQL("""SELECT array_agg(DISTINCT k.source ORDER BY k.source) AS sources,
                    array_agg(DISTINCT k.data_mode ORDER BY k.data_mode) AS modes,
                    max(k.evidence->>'fetched_at') AS fetched_at,
                    min(left(k.observed_at,10)) AS observation_start,max(left(k.observed_at,10)) AS observation_end
                    FROM {} k {}""").format(self._table("keywords"), keyword_filter), params).fetchone()
                modes = coverage["modes"] or []
                return {"dataset_id": legacy["fetch_id"] if legacy else "legacy-local-ledger",
                        "status": "LEGACY_CACHED", "processing_status": legacy["status"] if legacy else "UNKNOWN",
                        "data_mode": modes[0] if len(modes) == 1 else "MIXED" if modes else "UNKNOWN",
                        "sources": coverage["sources"] or [],
                        "provider_record_count": legacy["available"] if legacy else None,
                        "received_record_count": None, "retained_keyword_count": counts["total"],
                        "eligible_keyword_count": counts["eligible"], "excluded_keyword_count": counts["excluded"],
                        "assigned_keyword_count": counts["assigned"], "unassigned_keyword_count": counts["unassigned"],
                        "observation_start": coverage["observation_start"], "observation_end": coverage["observation_end"],
                        "fetched_at": coverage["fetched_at"], "quality_flags": ["LEGACY_COVERAGE_UNKNOWN"],
                        "coverage_status": "UNKNOWN"}
            item = _json_result(snapshot)
            manifest, flags = item["source_manifest"], item["quality_flags"]
            if (not isinstance(manifest, list) or not all(isinstance(source, dict) and "source_id" in source
                                                          for source in manifest)
                    or not isinstance(flags, list)):
                raise CollectionReadError("stored dataset manifest is invalid")
            item["sources"] = [source["source_id"] for source in manifest]
            item["snapshot_retained_keyword_count"] = item["retained_keyword_count"]
            item["retained_keyword_count"] = counts["total"]
            item["ledger_coverage_status"] = ("MIXED_OR_UNKNOWN"
                if item["snapshot_retained_keyword_count"] < counts["total"] else "SNAPSHOT_ONLY")
            if item["ledger_coverage_status"] == "MIXED_OR_UNKNOWN":
                item["quality_flags"] = sorted(set(flags + ["PRIOR_LEDGER_COVERAGE_UNKNOWN"]))
            for source_key, target_key in (("eligible", "eligible_keyword_count"),
                                           ("excluded", "excluded_keyword_count"),
                                           ("assigned", "assigned_keyword_count"),
                                           ("unassigned", "unassigned_keyword_count")):
                item[target_key] = counts[source_key]
            item["coverage_status"] = "PARTIAL" if item["status"] == "PARTIAL" else "RECORDED"
            return item
        return self._read(read)

    def latest_dataset_reuse_summary(self):
        """Read the compact manifest needed to identify unchanged Fetch input."""
        mode_filter = sql.SQL("WHERE data_mode=%s") if self.data_mode_scope else sql.SQL("")
        params = (self.data_mode_scope,) if self.data_mode_scope else ()

        def read(db):
            row = db.execute(sql.SQL("""SELECT dataset_id,processing_status,source_manifest
                FROM {} {} ORDER BY created_at DESC LIMIT 1""").format(
                    self._table("dataset_snapshots"), mode_filter), params).fetchone()
            if row is None:
                return None
            summary = _json_result(row)
            manifest = summary.get("source_manifest")
            if (not isinstance(manifest, list)
                    or not all(isinstance(source, dict)
                               and isinstance(source.get("source_id"), str)
                               for source in manifest)):
                raise CollectionReadError("stored dataset manifest is invalid")
            return summary

        return self._read(read)

    def unassigned_keyword_count(self):
        mode_filter = sql.SQL("AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
        params = (self.data_mode_scope,) if self.data_mode_scope else ()

        def read(db):
            return db.execute(sql.SQL("""SELECT count(*) AS total FROM {} k
                WHERE k.eligible=1 {} AND COALESCE((SELECT classification_status FROM {} cr
                    WHERE cr.keyword_id=k.id),'')<>'INELIGIBLE'
                AND NOT EXISTS(SELECT 1 FROM {} m WHERE m.keyword_id=k.id)""").format(
                    self._table("keywords"), mode_filter, self._table("classification_results"),
                    self._table("memberships")), params).fetchone()["total"]

        return self._read(read)

    def count_ai_assigned_keywords(self):
        mode_filter = sql.SQL("AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
        parameters = (self.data_mode_scope,) if self.data_mode_scope else ()

        def read(db):
            return db.execute(sql.SQL("SELECT count(DISTINCT m.keyword_id) AS total FROM {} m "
                "JOIN {} k ON k.id=m.keyword_id WHERE m.origin='LOCAL_AI' {}")
                .format(self._table("memberships"), self._table("keywords"), mode_filter),
                parameters).fetchone()["total"]

        return self._read(read)

    def collection_name_candidates(self, name_fingerprint):
        def read(db):
            suppressed = db.execute(sql.SQL("SELECT 1 FROM {} WHERE fingerprint=%s")
                                    .format(self._table("suppression")),
                                    (name_fingerprint,)).fetchone() is not None
            pending_scope = (sql.SQL("AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(p.support_ids) s(value) "
                                     "JOIN {} k ON k.id=s.value WHERE k.data_mode=%s)").format(
                                         self._table("keywords")) if self.data_mode_scope else sql.SQL(""))
            parameters = (self.data_mode_scope,) if self.data_mode_scope else ()
            labels = [row["name"] for row in db.execute(sql.SQL(
                "SELECT p.name FROM {} p WHERE p.status='PENDING' ").format(self._table("proposals"))
                + pending_scope, parameters).fetchall()]
            labels.extend(row["label"] for row in db.execute(sql.SQL(
                "SELECT label FROM {} WHERE label<>''").format(self._table("suppression"))).fetchall())
            active = db.execute(sql.SQL("SELECT name,category_id FROM {} WHERE status='ACTIVE'")
                                .format(self._table("collections"))).fetchall()
            from app.collections.identity import fingerprint
            labels.extend(row["name"] for row in active
                          if not row["category_id"] or fingerprint(row["name"]) == name_fingerprint)
            return {"suppressed": suppressed, "labels": labels}

        return self._read(read)

    def load_cached_keywords_for_reclassification(self, identifiers):
        identifiers = list(dict.fromkeys(identifiers))
        if not identifiers:
            return []
        mode_filter = sql.SQL("AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
        parameters = (identifiers, self.data_mode_scope) if self.data_mode_scope else (identifiers,)

        def read(db):
            rows = db.execute(sql.SQL("""SELECT k.*,e.entity_id,e.entity_type,e.description AS entity_description,
                e.occupations AS entity_occupations,e.category_ids AS entity_category_ids,
                e.instance_types AS entity_instance_types,e.source_url AS entity_source_url,
                e.method AS entity_context_method,e.status AS entity_context_status,
                e.page_namespace,e.is_disambiguation,e.is_main_page,e.is_main_page_known,
                e.redirected_from,e.page_language FROM {} k LEFT JOIN {} e ON e.keyword_id=k.id
                WHERE k.id=ANY(%s) {}""").format(self._table("keywords"), self._table("entity_context"),
                                                      mode_filter), parameters).fetchall()
            found = {row["id"] for row in rows}
            if found != set(identifiers):
                raise ValueError("CACHED_KEYWORD_NOT_FOUND")
            records = []
            for row in rows:
                item = dict(row)
                for name in ("evidence", "entity_occupations", "entity_category_ids", "entity_instance_types"):
                    value = item.get(name)
                    if isinstance(value, (dict, list)):
                        item[name] = json.dumps(value, ensure_ascii=False, sort_keys=True)
                records.append(item)
            return records

        return self._read(read)

    def ai_classification_context(self, identifiers, *, allowed_sources):
        """Load only normalized keyword text, override state, approved evidence IDs, and Collections."""
        identifiers = list(dict.fromkeys(identifiers))
        if not identifiers or len(identifiers) > 200 or any(
                not isinstance(value, str) or not value or len(value) > 64 for value in identifiers):
            raise ValueError("AI_KEYWORD_NOT_FOUND")
        sources = sorted(set(allowed_sources))
        if any(not isinstance(value, str) or not value or len(value) > 100 for value in sources):
            raise ValueError("AI_POLICY_DENIED")
        mode_filter = sql.SQL("AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
        parameters = [identifiers, sources]
        if self.data_mode_scope:
            parameters.append(self.data_mode_scope)

        def read(db):
            rows = db.execute(sql.SQL("""SELECT k.id,k.keyword,k.normalized,k.source,k.observed_at,
                k.data_mode,k.eligible,COALESCE(kd.decision,'') AS decision,
                COALESCE(cr.classification_status,'') AS classification_status,
                COALESCE((SELECT jsonb_agg(DISTINCT ek.evidence_id ORDER BY ek.evidence_id)
                    FROM {} ek JOIN {} se ON se.id=ek.evidence_id
                    WHERE ek.keyword_id=k.id AND se.source_id=ANY(%s)), '[]'::jsonb) AS evidence_ids,
                COALESCE((SELECT jsonb_object_agg(se.id,se.expires_at::text)
                    FROM {} ek JOIN {} se ON se.id=ek.evidence_id
                    WHERE ek.keyword_id=k.id AND se.source_id=ANY(%s)), '{{}}'::jsonb) AS evidence_expiry,
                EXISTS(SELECT 1 FROM {} m WHERE m.keyword_id=k.id AND m.origin='USER') AS user_assigned,
                EXISTS(SELECT 1 FROM {} m WHERE m.keyword_id=k.id AND m.pinned=1) AS user_pinned,
                COALESCE((SELECT jsonb_agg(DISTINCT f.collection_id ORDER BY f.collection_id)
                    FROM {} f WHERE f.keyword_id=k.id AND f.action='REMOVE'), '[]'::jsonb) AS removed_collection_ids,
                COALESCE(kd.updated_at::text,'') AS override_updated_at
                FROM {} k LEFT JOIN {} kd ON kd.keyword_id=k.id
                LEFT JOIN {} cr ON cr.keyword_id=k.id WHERE k.id=ANY(%s) {}""").format(
                    self._table("evidence_keywords"), self._table("source_evidence"),
                    self._table("evidence_keywords"), self._table("source_evidence"),
                    self._table("memberships"), self._table("memberships"), self._table("feedback"),
                    self._table("keywords"),
                    self._table("keyword_decisions"), self._table("classification_results"),
                    mode_filter), [sources, sources, identifiers, *parameters[2:]] if self.data_mode_scope
                    else [sources, sources, identifiers]).fetchall()
            found = {row["id"] for row in rows}
            if found != set(identifiers):
                raise ValueError("AI_KEYWORD_NOT_FOUND")
            collections = db.execute(sql.SQL("""SELECT id,name,description,version,auto_match
                FROM {} WHERE status='ACTIVE' AND category_id IS NULL
                ORDER BY id COLLATE "C"""").format(self._table("collections"))).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                if not isinstance(item.get("evidence_ids"), list):
                    raise CollectionReadError("stored AI evidence references are invalid")
                result.append(item)
            return {"keywords": result, "collections": [dict(item) for item in collections]}

        return self._read(read)

    def ai_recommendation_context(self, identifiers, *, allowed_sources):
        """Load bounded business context without source payloads or credentials."""
        identifiers = list(dict.fromkeys(identifiers))
        if not identifiers or len(identifiers) > 20 or any(
                not isinstance(value, str) or not value or len(value) > 64 for value in identifiers):
            raise ValueError("AI_KEYWORD_NOT_FOUND")
        sources = sorted(set(allowed_sources))
        if not sources:
            return {"keywords": [], "collections": []}
        if any(not isinstance(value, str) or not value or len(value) > 100 for value in sources):
            raise ValueError("AI_POLICY_DENIED")
        mode_filter = sql.SQL("AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
        params = (sources, sources, sources, identifiers, self.data_mode_scope) if self.data_mode_scope else (
            sources, sources, sources, identifiers)

        def read(db):
            rows = db.execute(sql.SQL("""SELECT k.id,k.keyword,k.normalized,k.source,k.project,
                k.observed_at,k.data_mode,k.eligible,COALESCE(kd.decision,'') AS decision,
                COALESCE(cr.classification_status,'') AS classification_status,
                COALESCE((SELECT jsonb_agg(DISTINCT ek.evidence_id ORDER BY ek.evidence_id)
                    FROM {} ek JOIN {} se ON se.id=ek.evidence_id
                    WHERE ek.keyword_id=k.id AND se.source_id=ANY(%s)), '[]'::jsonb) AS evidence_ids,
                COALESCE((SELECT jsonb_object_agg(se.id,se.expires_at::text)
                    FROM {} ek JOIN {} se ON se.id=ek.evidence_id
                    WHERE ek.keyword_id=k.id AND se.source_id=ANY(%s)), '{{}}'::jsonb) AS evidence_expiry,
                COALESCE((SELECT jsonb_agg(jsonb_build_object('evidence_id',e.id,
                    'source_id',e.source_id,'geography',e.geography,'observed_at',e.observed_at,
                    'collected_at',e.collected_at) ORDER BY e.collected_at DESC,e.id)
                    FROM (SELECT se.id,se.source_id,se.geography,se.observed_at,se.collected_at
                        FROM {} ek JOIN {} se ON se.id=ek.evidence_id
                        WHERE ek.keyword_id=k.id AND se.source_id=ANY(%s)
                        ORDER BY se.collected_at DESC,se.id LIMIT 20) e), '[]'::jsonb) AS evidence_context,
                COALESCE((SELECT jsonb_agg(jsonb_build_object('algorithm_id',ar.algorithm_id,
                    'version',ar.version,'status',ar.status,'reason_code',ar.reason_code,
                    'value',ar.value,'metric',ar.metric,'unit',ar.unit) ORDER BY ar.algorithm_id)
                    FROM {} ar WHERE ar.keyword_id=k.id), '[]'::jsonb) AS algorithm_results,
                COALESCE((SELECT jsonb_agg(jsonb_build_object('id',c.id,'name',c.name)
                    ORDER BY c.name COLLATE "C") FROM {} m JOIN {} c ON c.id=m.collection_id
                    WHERE m.keyword_id=k.id AND c.status='ACTIVE'), '[]'::jsonb) AS collections
                FROM {} k LEFT JOIN {} kd ON kd.keyword_id=k.id
                LEFT JOIN {} cr ON cr.keyword_id=k.id WHERE k.id=ANY(%s) {}""").format(
                    self._table("evidence_keywords"), self._table("source_evidence"),
                    self._table("evidence_keywords"), self._table("source_evidence"),
                    self._table("evidence_keywords"), self._table("source_evidence"),
                    self._table("algorithm_results"), self._table("memberships"),
                    self._table("collections"), self._table("keywords"),
                    self._table("keyword_decisions"), self._table("classification_results"),
                    mode_filter), params).fetchall()
            found = {row["id"] for row in rows}
            if found != set(identifiers):
                raise ValueError("AI_KEYWORD_NOT_FOUND")
            return {"keywords": [dict(row) for row in rows], "collections": []}

        return self._read(read)

    def load_keywords_for_fetch(self, source_ids=None):
        mode_filter = sql.SQL("AND data_mode=%s") if self.data_mode_scope else sql.SQL("")
        mode_parameters = (self.data_mode_scope,) if self.data_mode_scope else ()
        source_filter = sql.SQL("AND id=ANY(%s)") if source_ids is not None else sql.SQL("")
        source_parameters = (list(source_ids),) if source_ids is not None else ()
        parameters = (*mode_parameters, *source_parameters)

        def read(db):
            records = db.execute(sql.SQL("SELECT * FROM {} WHERE eligible=1 {} {} "
                "ORDER BY observed_at DESC,translate(keyword,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                "'abcdefghijklmnopqrstuvwxyz')").format(self._table("keywords"), mode_filter, source_filter),
                parameters).fetchall()
            eligible = db.execute(sql.SQL("SELECT count(*) AS total FROM {} WHERE eligible=1 {}")
                                  .format(self._table("keywords"), mode_filter), mode_parameters).fetchone()["total"]
            excluded = db.execute(sql.SQL("SELECT * FROM {} WHERE eligible=0 {} {} "
                "ORDER BY observed_at DESC,translate(keyword,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                "'abcdefghijklmnopqrstuvwxyz')").format(self._table("keywords"), mode_filter, source_filter),
                parameters).fetchall()

            def as_legacy_record(row):
                item = dict(row)
                if not isinstance(item.get("evidence"), dict):
                    raise CollectionReadError("stored keyword evidence is not an object")
                item["evidence"] = json.dumps(item["evidence"], ensure_ascii=False, sort_keys=True)
                return item

            return {"records": [as_legacy_record(row) for row in records],
                    "excluded_records": [as_legacy_record(row) for row in excluded],
                    "eligible": eligible}

        return self._read(read)

    def load_keywords_for_collection_processing(self, *, include_invalid_google=False):
        conditions, parameters = [], []
        if include_invalid_google:
            conditions.append(sql.SQL("(eligible=1 OR (source='google_web_serp' AND "
                                      "rejection_reason='INVALID_GOOGLE_TRENDS_RECORD'))"))
        if self.data_mode_scope:
            conditions.append(sql.SQL("data_mode=%s"))
            parameters.append(self.data_mode_scope)
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions) if conditions else sql.SQL("")

        def read(db):
            rows = db.execute(sql.SQL("SELECT * FROM {}" ).format(self._table("keywords")) + where
                              + sql.SQL(" ORDER BY observed_at DESC"), parameters).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                evidence = item.get("evidence")
                if not isinstance(evidence, str):
                    item["evidence"] = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
                result.append(item)
            return result
        return self._read(read)

    def user_member_examples(self, collection_ids):
        identifiers = list(dict.fromkeys(collection_ids))
        result = {identifier: [] for identifier in identifiers}
        if not identifiers:
            return result
        mode_filter = sql.SQL("AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
        parameters = (identifiers, self.data_mode_scope) if self.data_mode_scope else (identifiers,)

        def read(db):
            rows = db.execute(sql.SQL("""SELECT collection_id,keyword FROM (
                SELECT m.collection_id,k.keyword,row_number() OVER (PARTITION BY m.collection_id
                    ORDER BY m.created_at DESC) AS position FROM {} m JOIN {} k ON k.id=m.keyword_id
                WHERE m.origin='USER' AND m.collection_id=ANY(%s) {}) ranked WHERE position<=100
                ORDER BY collection_id,position""").format(
                    self._table("memberships"), self._table("keywords"), mode_filter), parameters).fetchall()
            for row in rows:
                result[row["collection_id"]].append(row["keyword"])

        self._read(read)
        return result

    def membership_guardrails(self, keyword_ids, category_collection_ids):
        identifiers = list(dict.fromkeys(keyword_ids))
        category_ids = list(dict.fromkeys(category_collection_ids))
        removed_pairs, removed_keyword_ids, pinned_pairs = set(), set(), set()
        active_category_ids = set()
        mode_filter = sql.SQL("AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")

        def read(db):
            for offset in range(0, len(identifiers), 500):
                batch = identifiers[offset:offset + 500]
                parameters = (batch, self.data_mode_scope) if self.data_mode_scope else (batch,)
                removed = db.execute(sql.SQL("SELECT f.collection_id,f.keyword_id FROM {} f "
                    "JOIN {} k ON k.id=f.keyword_id WHERE f.action='REMOVE' "
                    "AND f.keyword_id=ANY(%s) {}").format(self._table("feedback"),
                        self._table("keywords"), mode_filter), parameters).fetchall()
                removed_pairs.update((row["collection_id"], row["keyword_id"]) for row in removed)
                removed_keyword_ids.update(row["keyword_id"] for row in removed)
                pinned = db.execute(sql.SQL("SELECT m.collection_id,m.keyword_id FROM {} m "
                    "JOIN {} k ON k.id=m.keyword_id WHERE m.pinned=1 AND m.keyword_id=ANY(%s) {}")
                    .format(self._table("memberships"), self._table("keywords"), mode_filter),
                    parameters).fetchall()
                pinned_pairs.update((row["collection_id"], row["keyword_id"]) for row in pinned)
            for offset in range(0, len(category_ids), 500):
                batch = category_ids[offset:offset + 500]
                rows = db.execute(sql.SQL("SELECT id FROM {} WHERE status='ACTIVE' AND auto_match=1 "
                    "AND id=ANY(%s)").format(self._table("collections")), (batch,)).fetchall()
                active_category_ids.update(row["id"] for row in rows)

        self._read(read)
        return {"removed_pairs": removed_pairs, "removed_keyword_ids": removed_keyword_ids,
                "pinned_pairs": pinned_pairs, "active_category_ids": active_category_ids}

    def list_collections(self):
        scoped = self.data_mode_scope is not None
        visible_filter = sql.SQL("""AND (c.created_by<>'AI' OR c.category_id IS NOT NULL OR EXISTS (
            SELECT 1 FROM {} lm JOIN {} lk ON lk.id=lm.keyword_id
            WHERE lm.collection_id=c.id AND lk.data_mode=%s))""").format(
                self._table("memberships"), self._table("keywords")) if scoped else sql.SQL("")
        member_filter = sql.SQL("AND k.data_mode=%s") if scoped else sql.SQL("")
        params = (self.data_mode_scope, self.data_mode_scope, self.data_mode_scope) if scoped else ()

        def read(db):
            query = sql.SQL("""WITH RECURSIVE visible AS (
                SELECT c.* FROM {} c WHERE c.status='ACTIVE' {}
            ), tree(ancestor, descendant) AS (
                SELECT id,id FROM visible UNION
                SELECT tree.ancestor,c.id FROM tree JOIN visible c ON c.parent_id=tree.descendant
            ), direct AS (
                SELECT m.collection_id,count(DISTINCT k.id) AS n FROM {} m JOIN {} k ON k.id=m.keyword_id {}
                GROUP BY m.collection_id
            ), subtree AS (
                SELECT tree.ancestor,count(DISTINCT k.id) AS n FROM tree
                JOIN {} m ON m.collection_id=tree.descendant JOIN {} k ON k.id=m.keyword_id {}
                GROUP BY tree.ancestor
            ) SELECT c.*,COALESCE(direct.n,0) AS direct_membership_count,
                COALESCE(subtree.n,0) AS subtree_unique_keyword_count
              FROM visible c LEFT JOIN direct ON direct.collection_id=c.id
              LEFT JOIN subtree ON subtree.ancestor=c.id ORDER BY c.updated_at DESC""").format(
                  self._table("collections"), visible_filter,
                  self._table("memberships"), self._table("keywords"), member_filter,
                  self._table("memberships"), self._table("keywords"), member_filter)
            items = []
            for row in db.execute(query, params).fetchall():
                item = _json_result(row)
                if not isinstance(item["examples"], list) or not isinstance(item["exclusions"], list):
                    raise CollectionReadError("stored collection metadata is invalid")
                item["auto_match"] = bool(item["auto_match"])
                item["membership_count"] = item["subtree_unique_keyword_count"]
                items.append(item)
            return items
        return self._read(read)

    def get_setting(self, key, default=None):
        row = self._read(lambda db: db.execute(sql.SQL("SELECT value FROM {} WHERE key=%s")
            .format(self._table("settings")), (key,)).fetchone())
        return row["value"] if row else default

    def algorithm_results_overview(self, *, limit=100):
        scope = sql.SQL("AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
        parameters = (self.data_mode_scope,) if self.data_mode_scope else ()

        def read(db):
            counts = db.execute(sql.SQL("""SELECT r.algorithm_id,r.version,
                count(*) FILTER (WHERE r.status='COMPLETED') AS completed,
                count(*) FILTER (WHERE r.status='SKIPPED') AS skipped,
                count(*) FILTER (WHERE r.status='FAILED') AS failed,count(*) AS total,
                max(r.updated_at) AS updated_at FROM {} r JOIN {} k ON k.id=r.keyword_id
                LEFT JOIN {} cr ON cr.keyword_id=k.id
                WHERE COALESCE(cr.classification_status,'')<>'INELIGIBLE' {}
                GROUP BY r.algorithm_id,r.version ORDER BY r.algorithm_id""").format(
                    self._table("algorithm_results"), self._table("keywords"),
                    self._table("classification_results"), scope), parameters).fetchall()
            rows = db.execute(sql.SQL("""SELECT r.keyword_id,k.keyword,r.algorithm_id,r.version,
                r.status,r.reason_code,r.value,r.metric,r.unit,r.result_json,r.updated_at
                FROM {} r JOIN {} k ON k.id=r.keyword_id
                LEFT JOIN {} cr ON cr.keyword_id=k.id
                WHERE k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE' {}
                ORDER BY r.updated_at DESC,
                translate(k.keyword,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz') COLLATE "C",
                k.keyword COLLATE "C",r.algorithm_id LIMIT %s""").format(
                    self._table("algorithm_results"), self._table("keywords"),
                    self._table("classification_results"), scope), (*parameters, limit)).fetchall()
            return {"summary": [_json_result(row) for row in counts],
                    "items": [_json_result(row) for row in rows], "limit": limit}
        return self._read(read)

    def runtime_storage_metrics(self, model_version):
        scope = sql.SQL("AND data_mode=%s") if self.data_mode_scope else sql.SQL("")
        mode_parameters = (self.data_mode_scope,) if self.data_mode_scope else ()

        def read(db):
            cache = db.execute(sql.SQL("""SELECT count(*) AS vectors,coalesce(sum(dimension),0) AS dimensions,
                string_agg(DISTINCT dimension::text, ',') AS vector_dimensions FROM {} WHERE model_version=%s""")
                .format(self._table("cache")), (model_version,)).fetchone()
            keyword_count = db.execute(sql.SQL("SELECT count(*) AS total FROM {} WHERE TRUE {}")
                .format(self._table("keywords"), scope), mode_parameters).fetchone()["total"]
            database_size = db.execute("SELECT pg_database_size(current_database()) AS bytes").fetchone()["bytes"]
            return {"engine": "POSTGRESQL", "integrity": None, "keyword_records": keyword_count,
                    "cache_vectors": cache["vectors"], "cached_dimensions_total": cache["dimensions"],
                    "vector_dimensions": cache["vector_dimensions"], "database_size_bytes": database_size}
        return self._read(read)

    def recent_dataset_snapshots(self, *, limit=100):
        scope = sql.SQL("WHERE data_mode=%s") if self.data_mode_scope else sql.SQL("")
        parameters = (self.data_mode_scope, limit) if self.data_mode_scope else (limit,)

        def read(db):
            rows = db.execute(sql.SQL("""SELECT status,data_mode,source_manifest,fetched_at,
                observation_start,observation_end FROM {} {} ORDER BY created_at DESC LIMIT %s""")
                .format(self._table("dataset_snapshots"), scope), parameters).fetchall()
            result = []
            for row in rows:
                item = _json_result(row)
                if not isinstance(item["source_manifest"], str):
                    item["source_manifest"] = json.dumps(item["source_manifest"], ensure_ascii=False)
                result.append(item)
            return result
        return self._read(read)

    def analytics_overview_inputs(self):
        scope = sql.SQL("AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
        keyword_where = sql.SQL("WHERE k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
        dataset_where = sql.SQL("WHERE data_mode=%s") if self.data_mode_scope else sql.SQL("")
        successful_scope = sql.SQL("AND data_mode=%s") if self.data_mode_scope else sql.SQL("")
        parameters = (self.data_mode_scope,) if self.data_mode_scope else ()

        def legacy_row(row, json_fields=()):
            if row is None:
                return None
            item = _json_result(row)
            for field in json_fields:
                value = item.get(field)
                if not isinstance(value, str):
                    item[field] = json.dumps(value, ensure_ascii=False)
            return item

        def read(db):
            totals = db.execute(sql.SQL("""SELECT count(*) AS keywords,
                sum(CASE WHEN k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE'
                    THEN 1 ELSE 0 END) AS eligible,
                sum(CASE WHEN (k.eligible=0 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE')
                    OR EXISTS(SELECT 1 FROM {} kd WHERE kd.keyword_id=k.id AND kd.decision='EXCLUDED')
                    THEN 1 ELSE 0 END) AS excluded FROM {} k
                LEFT JOIN {} cr ON cr.keyword_id=k.id {}""").format(
                    self._table("keyword_decisions"), self._table("keywords"),
                    self._table("classification_results"), keyword_where), parameters).fetchone()
            classifications = db.execute(sql.SQL("""SELECT cr.classification_status,count(*) AS count
                FROM {} cr JOIN {} k ON k.id=cr.keyword_id WHERE
                (k.eligible=1 OR cr.classification_status='INELIGIBLE') AND NOT EXISTS(
                    SELECT 1 FROM {} kd WHERE kd.keyword_id=k.id AND kd.decision='EXCLUDED') {}
                GROUP BY cr.classification_status""").format(
                    self._table("classification_results"), self._table("keywords"),
                    self._table("keyword_decisions"), scope), parameters).fetchall()
            source_rows = db.execute(sql.SQL("""SELECT source,count(DISTINCT id) AS keywords,
                min(left(observed_at,10)) AS first_seen,max(left(observed_at,10)) AS last_seen
                FROM {} k WHERE TRUE {} GROUP BY source ORDER BY keywords DESC,source""")
                .format(self._table("keywords"), scope), parameters).fetchall()
            algorithm_rows = db.execute(sql.SQL("""SELECT r.algorithm_id,r.version,r.status,count(*) AS count
                FROM {} r JOIN {} k ON k.id=r.keyword_id LEFT JOIN {} cr ON cr.keyword_id=k.id
                WHERE COALESCE(cr.classification_status,'')<>'INELIGIBLE' {}
                GROUP BY r.algorithm_id,r.version,r.status ORDER BY r.algorithm_id,r.status""").format(
                    self._table("algorithm_results"), self._table("keywords"),
                    self._table("classification_results"), scope), parameters).fetchall()
            observations = db.execute(sql.SQL("""SELECT o.source,o.observation_date,
                count(DISTINCT o.keyword_id) AS keywords,
                count(*) FILTER (WHERE o.status='OBSERVED') AS observed,
                count(*) FILTER (WHERE o.status<>'OBSERVED') AS unavailable
                FROM {} o JOIN {} k ON k.id=o.keyword_id WHERE TRUE {}
                GROUP BY o.source,o.observation_date ORDER BY o.observation_date,o.source""").format(
                    self._table("algorithm_observations"), self._table("keywords"), scope),
                parameters).fetchall()
            top = db.execute(sql.SQL("""SELECT k.id,k.keyword,k.source,k.project,k.observed_at,
                r.value,r.metric,r.unit,r.updated_at,r.result_json FROM {} r JOIN {} k ON k.id=r.keyword_id
                WHERE r.algorithm_id='observed-volume-rank' AND r.status='COMPLETED' AND r.value IS NOT NULL
                AND k.eligible=1 AND COALESCE((SELECT classification_status FROM {} cr
                    WHERE cr.keyword_id=k.id),'')<>'INELIGIBLE' {}
                ORDER BY r.updated_at DESC,
                translate(k.keyword,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz') COLLATE "C",
                k.keyword COLLATE "C" LIMIT 1000""").format(
                    self._table("algorithm_results"), self._table("keywords"),
                    self._table("classification_results"), scope), parameters).fetchall()
            latest = db.execute(sql.SQL("""SELECT dataset_id,status,data_mode,source_manifest,
                provider_record_count,received_record_count,retained_keyword_count,rejected_record_count,
                observation_start,observation_end,fetched_at,created_at,quality_flags,processing_status,
                eligible_keyword_count,assigned_keyword_count,unassigned_keyword_count FROM {} {}
                ORDER BY created_at DESC LIMIT 1""").format(self._table("dataset_snapshots"), dataset_where),
                parameters).fetchone()
            history = db.execute(sql.SQL("""SELECT dataset_id,status,data_mode,source_manifest,
                provider_record_count,received_record_count,retained_keyword_count,rejected_record_count,
                observation_start,observation_end,fetched_at,quality_flags,processing_status FROM {} {}
                ORDER BY created_at DESC LIMIT 30""").format(
                    self._table("dataset_snapshots"), dataset_where), parameters).fetchall()
            successful = db.execute(sql.SQL("""SELECT dataset_id,status,data_mode,source_manifest,fetched_at,
                created_at,observation_start,observation_end,quality_flags FROM {}
                WHERE status IN ('SUCCESS','PARTIAL') {} ORDER BY created_at DESC LIMIT 1""").format(
                    self._table("dataset_snapshots"), successful_scope), parameters).fetchone()
            membership_count = db.execute(sql.SQL("""SELECT count(DISTINCT m.keyword_id) FROM {} m
                JOIN {} k ON k.id=m.keyword_id LEFT JOIN {} cr ON cr.keyword_id=k.id
                WHERE COALESCE(cr.classification_status,'')<>'INELIGIBLE' {}""").format(
                    self._table("memberships"), self._table("keywords"),
                    self._table("classification_results"), scope), parameters).fetchone()["count"]
            unassigned_topic_count = db.execute(sql.SQL("""SELECT count(*) FROM {} k
                WHERE k.eligible=1 AND COALESCE((SELECT classification_status FROM {} cr
                    WHERE cr.keyword_id=k.id),'')<>'INELIGIBLE' AND NOT EXISTS(
                    SELECT 1 FROM {} m WHERE m.keyword_id=k.id) {}""").format(
                    self._table("keywords"), self._table("classification_results"),
                    self._table("memberships"), scope), parameters).fetchone()["count"]
            return {
                "totals": dict(totals),
                "classifications": [dict(row) for row in classifications],
                "source_rows": [dict(row) for row in source_rows],
                "algorithm_rows": [dict(row) for row in algorithm_rows],
                "observations": [dict(row) for row in observations],
                "top": [_json_result(row) for row in top],
                "latest": legacy_row(latest, ("source_manifest", "quality_flags")),
                "history": [legacy_row(row, ("source_manifest", "quality_flags")) for row in history],
                "successful": legacy_row(successful, ("source_manifest", "quality_flags")),
                "membership_count": membership_count,
                "unassigned_topic_count": unassigned_topic_count,
            }
        return self._read(read)

    def local_run_history_rows(self, *, limit=None, cursor=None, status=None, mode=None, fetch_id=None):
        conditions, parameters = [], []
        if cursor:
            conditions.append(sql.SQL("(COALESCE(j.updated_at,d.fetched_at)<%s OR "
                "(COALESCE(j.updated_at,d.fetched_at)=%s AND j.fetch_id<%s))"))
            parameters.extend((cursor[0], cursor[0], cursor[1]))
        if status:
            conditions.append(sql.SQL("""(CASE WHEN d.status='SUCCESS' THEN 'COMPLETED'
                WHEN d.status IN ('COMPLETED','PARTIAL','FAILED','ABORTED')
                THEN d.status ELSE j.status END)=%s"""))
            parameters.append(status)
        if mode:
            conditions.append(sql.SQL("d.data_mode=%s"))
            parameters.append(mode)
        if fetch_id:
            conditions.append(sql.SQL("j.fetch_id=%s"))
            parameters.append(fetch_id)
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions) if conditions else sql.SQL("")
        limit_clause = sql.SQL(" LIMIT %s") if limit is not None else sql.SQL("")
        if limit is not None:
            parameters.append(limit)

        def read(db):
            rows = db.execute(sql.SQL("""SELECT j.*,d.dataset_id,d.status AS dataset_status,d.data_mode,
                d.source_manifest,d.provider_record_count,d.received_record_count,d.rejected_record_count,
                d.observation_start,d.observation_end,d.fetched_at,d.quality_flags,d.processing_status,
                COALESCE(j.updated_at,d.fetched_at) AS history_sort_at
                FROM {} j LEFT JOIN {} d ON d.dataset_id=j.fetch_id""").format(
                    self._table("fetch_jobs"), self._table("dataset_snapshots")) + where
                + sql.SQL(" ORDER BY history_sort_at DESC,j.fetch_id DESC") + limit_clause,
                parameters).fetchall()
            result = []
            for row in rows:
                item = _json_result(row)
                for field in ("source_manifest", "quality_flags"):
                    value = item.get(field)
                    if value is not None and not isinstance(value, str):
                        item[field] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                result.append(item)
            return result
        return self._read(read)

    def fetch_collection_processing_result(self, fetch_id):
        row = self._read(lambda db: db.execute(sql.SQL("""SELECT j.status,j.error_code
            FROM {} j JOIN {} d ON d.dataset_id=j.fetch_id
            WHERE j.fetch_id=%s AND d.data_mode='LIVE'
            AND j.status IN ('COMPLETED','MODEL_UNAVAILABLE')
            AND d.processing_status IN ('COMPLETED','MODEL_UNAVAILABLE')""").format(
                self._table("fetch_jobs"), self._table("dataset_snapshots")), (fetch_id,)).fetchone())
        return dict(row) if row is not None else None

    def list_research(self, *, q="", status=None, limit=50, offset=0):
        query = " ".join(q.casefold().split())
        conditions, parameters = [], []
        if self.data_mode_scope:
            conditions.append(sql.SQL("(r.keyword_id IS NULL OR k.data_mode=%s)"))
            parameters.append(self.data_mode_scope)
        if status:
            conditions.append(sql.SQL("r.status=%s"))
            parameters.append(status)
        if query:
            conditions.append(sql.SQL("""translate((r.subject || ' ' || r.hypothesis || ' ' ||
                r.product_category || ' ' || r.niche),
                'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz') LIKE %s"""))
            parameters.append("%" + query + "%")
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions) if conditions else sql.SQL("")

        def read(db):
            total = db.execute(sql.SQL("SELECT count(*) FROM {} r LEFT JOIN {} k ON k.id=r.keyword_id")
                .format(self._table("research_records"), self._table("keywords")) + where,
                parameters).fetchone()["count"]
            rows = db.execute(sql.SQL("""SELECT r.*,k.keyword,k.source,k.project,k.observed_at,k.data_mode
                FROM {} r LEFT JOIN {} k ON k.id=r.keyword_id""").format(
                    self._table("research_records"), self._table("keywords")) + where
                + sql.SQL(" ORDER BY r.updated_at DESC,r.id COLLATE \"C\" LIMIT %s OFFSET %s"),
                (*parameters, limit, offset)).fetchall()
            return {"items": [_json_result(row) for row in rows], "total": total, "limit": limit,
                    "offset": offset,
                    "next_offset": offset + limit if offset + limit < total else None}
        return self._read(read)

    def collection_exists(self, identifier, *, active=True):
        condition = " AND status='ACTIVE'" if active else ""
        row = self._read(lambda db: db.execute(sql.SQL("SELECT 1 FROM {} WHERE id=%s" + condition)
            .format(self._table("collections")), (identifier,)).fetchone())
        return row is not None

    def has_membership(self, collection_id, keyword_id):
        scope = sql.SQL(" AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
        params = (collection_id, keyword_id, self.data_mode_scope) if self.data_mode_scope else (
            collection_id, keyword_id)
        row = self._read(lambda db: db.execute(sql.SQL("""SELECT 1 FROM {} m JOIN {} k
            ON k.id=m.keyword_id WHERE m.collection_id=%s AND m.keyword_id=%s""").format(
                self._table("memberships"), self._table("keywords")) + scope, params).fetchone())
        return row is not None

    def collection_keyword_record(self, identifier):
        scope = sql.SQL(" AND data_mode=%s") if self.data_mode_scope else sql.SQL("")
        params = (identifier, self.data_mode_scope) if self.data_mode_scope else (identifier,)
        return self._read(lambda db: db.execute(sql.SQL("SELECT * FROM {} WHERE id=%s")
            .format(self._table("keywords")) + scope, params).fetchone())

    def classification_for(self, identifier):
        scope = sql.SQL(" AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
        params = (identifier, self.data_mode_scope) if self.data_mode_scope else (identifier,)
        return self._read(lambda db: db.execute(sql.SQL("""SELECT cr.* FROM {} cr JOIN {} k
            ON k.id=cr.keyword_id WHERE cr.keyword_id=%s""").format(
                self._table("classification_results"), self._table("keywords")) + scope,
            params).fetchone())

    def keyword_detail_metadata(self, identifier):
        scope = sql.SQL(" AND k.data_mode=%s") if self.data_mode_scope else sql.SQL("")
        params = (identifier, self.data_mode_scope) if self.data_mode_scope else (identifier,)

        def read(db):
            keyword = db.execute(sql.SQL("SELECT 1 FROM {} k WHERE k.id=%s").format(
                self._table("keywords")) + scope, params).fetchone()
            if not keyword:
                return None
            decision = db.execute(sql.SQL("SELECT decision,reason,updated_at FROM {} WHERE keyword_id=%s")
                .format(self._table("keyword_decisions")), (identifier,)).fetchone()
            feedback = db.execute(sql.SQL("""SELECT f.collection_id,c.name,f.action,f.created_at
                FROM {} f LEFT JOIN {} c ON c.id=f.collection_id
                WHERE f.keyword_id=%s ORDER BY f.created_at DESC""").format(
                    self._table("feedback"), self._table("collections")), (identifier,)).fetchall()
            return {"decision": decision, "feedback": feedback}
        return self._read(read)

    def collection_job(self, fetch_id):
        params = ((self.data_mode_scope, fetch_id) if self.data_mode_scope else (fetch_id,))
        dataset_join = (sql.SQL("JOIN {} d ON d.dataset_id=f.fetch_id AND d.data_mode=%s ").format(
            self._table("dataset_snapshots")) if self.data_mode_scope else sql.SQL(""))
        def read(db):
            row = db.execute(sql.SQL("SELECT f.* FROM {} f ").format(
                self._table("fetch_jobs")) + dataset_join + sql.SQL("WHERE f.fetch_id=%s"), params).fetchone()
            created = db.execute(sql.SQL("""SELECT name FROM {} WHERE created_by='AI'
                AND reason LIKE %s ORDER BY created_at""").format(self._table("collections")),
                (f"%{fetch_id}%",)).fetchall()
            return row, created
        return self._read(read)

    def workbench_summary(self):
        scoped = self.data_mode_scope is not None
        mode_filter = " AND k.data_mode=%s" if scoped else ""
        mode_params = (self.data_mode_scope,) if scoped else ()
        keywords = self._table("keywords").as_string()
        memberships = self._table("memberships").as_string()
        decisions = self._table("keyword_decisions").as_string()
        classifications = self._table("classification_results").as_string()
        collections = self._table("collections").as_string()
        topic = ("COALESCE((SELECT classification_status FROM " + classifications
                 + " cr WHERE cr.keyword_id=k.id),'')<>'INELIGIBLE'")

        def read(db):
            totals = db.execute("""SELECT count(*) AS stored,
                count(*) FILTER (WHERE k.eligible=1 AND """ + topic + ") AS eligible, "
                "count(*) FILTER (WHERE (k.eligible=0 AND " + topic + ") OR EXISTS(SELECT 1 FROM "
                + decisions + " kd WHERE kd.keyword_id=k.id AND kd.decision='EXCLUDED')) AS excluded, "
                "count(*) FILTER (WHERE k.eligible=1 AND " + topic + " AND NOT EXISTS(SELECT 1 FROM "
                + memberships + " m WHERE m.keyword_id=k.id)) AS unclassified, "
                "count(*) FILTER (WHERE k.eligible=1 AND " + topic + " AND EXISTS(SELECT 1 FROM "
                + memberships + " m WHERE m.keyword_id=k.id)) AS assigned FROM " + keywords
                + " k WHERE TRUE" + mode_filter, mode_params).fetchone()
            statuses = {row["classification_status"]: row["total"] for row in db.execute(
                "SELECT cr.classification_status,count(DISTINCT cr.keyword_id) AS total FROM "
                + classifications + " cr JOIN " + keywords + " k ON k.id=cr.keyword_id "
                "WHERE (k.eligible=1 OR cr.classification_status='INELIGIBLE') AND NOT EXISTS "
                "(SELECT 1 FROM " + decisions + " kd WHERE kd.keyword_id=k.id AND kd.decision='EXCLUDED')"
                + mode_filter + " GROUP BY cr.classification_status", mode_params).fetchall()}
            assignment = {row["origin"]: row["total"] for row in db.execute(
                "SELECT m.origin,count(DISTINCT m.keyword_id) AS total FROM " + memberships
                + " m JOIN " + keywords + " k ON k.id=m.keyword_id WHERE (m.origin='USER' OR "
                + topic + ")" + mode_filter + " GROUP BY m.origin", mode_params).fetchall()}
            mixed = db.execute("SELECT count(*) AS n FROM " + keywords + " k WHERE " + topic
                + " AND k.eligible=1 AND EXISTS(SELECT 1 FROM " + memberships
                + " m WHERE m.keyword_id=k.id AND m.origin='LOCAL_AI') AND EXISTS(SELECT 1 FROM "
                + memberships + " m WHERE m.keyword_id=k.id AND m.origin='USER')" + mode_filter,
                mode_params).fetchone()["n"]
            if scoped:
                ai_created = db.execute("SELECT count(DISTINCT c.id) AS n FROM " + collections
                    + " c LEFT JOIN " + memberships + " m ON m.collection_id=c.id LEFT JOIN "
                    + keywords + " k ON k.id=m.keyword_id AND k.data_mode=%s WHERE c.status='ACTIVE' "
                    "AND c.created_by='AI' AND c.category_id IS NULL AND k.id IS NOT NULL",
                    (self.data_mode_scope,)).fetchone()["n"]
                sources = [row["source"] for row in db.execute("SELECT DISTINCT source COLLATE \"C\" AS source FROM "
                    + keywords + " WHERE data_mode=%s ORDER BY source",
                    (self.data_mode_scope,)).fetchall()]
                latest_job = db.execute("""SELECT f.fetch_id,f.status,f.stage,f.available,f.eligible,f.processed,
                    f.assigned,f.unassigned,f.created_collections,f.suggestions,f.skipped,f.error_code,f.updated_at
                    FROM {} f JOIN {} d ON d.dataset_id=f.fetch_id WHERE d.data_mode=%s
                    ORDER BY f.updated_at DESC LIMIT 1""".format(
                        self._table("fetch_jobs").as_string(),
                        self._table("dataset_snapshots").as_string()),
                    (self.data_mode_scope,)).fetchone()
            else:
                ai_created = db.execute("SELECT count(*) AS n FROM " + collections
                    + " WHERE status='ACTIVE' AND created_by='AI' AND category_id IS NULL").fetchone()["n"]
                sources = [row["source"] for row in db.execute("SELECT DISTINCT source COLLATE \"C\" AS source FROM "
                    + keywords + " ORDER BY source").fetchall()]
                latest_job = db.execute("""SELECT fetch_id,status,stage,available,eligible,processed,assigned,
                    unassigned,created_collections,suggestions,skipped,error_code,updated_at FROM {}
                    ORDER BY updated_at DESC LIMIT 1""".format(self._table("fetch_jobs").as_string())).fetchone()
            return {"stored_keywords": totals["stored"] or 0,
                "eligible_keywords": totals["eligible"] or 0, "excluded_keywords": totals["excluded"] or 0,
                "unclassified_keywords": totals["unclassified"] or 0, "assigned_keywords": totals["assigned"] or 0,
                "classified_keywords": statuses.get("CLASSIFIED", 0),
                "ambiguous_keywords": statuses.get("AMBIGUOUS", 0),
                "unknown_keywords": statuses.get("UNKNOWN", 0),
                "ineligible_topic_keywords": statuses.get("INELIGIBLE", 0),
                "ai_assigned_keywords": assignment.get("LOCAL_AI", 0),
                "manually_managed_keywords": assignment.get("USER", 0),
                "mixed_origin_keywords": mixed, "ai_created_collections": ai_created,
                "sources": sources, "latest_job": _json_result(latest_job) if latest_job else None}
        return self._read(read)

    def _keyword_search_plan(self, *, query="", state="all", collection_ids=(), source="",
                             sort="recent", classification="all", evidence="all",
                             date_from="", date_to="", keyword_id=None):
        normalized = " ".join(query.casefold().split())
        clauses, parameters = [], []
        if self.data_mode_scope:
            clauses.append("k.data_mode=%s")
            parameters.append(self.data_mode_scope)
        if normalized:
            escaped = normalized.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            # SQLite LIKE folds ASCII case only; PostgreSQL LIKE is otherwise
            # case-sensitive. Keep the existing behavior for stored legacy text.
            clauses.append("translate(k.normalized,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz') "
                           "LIKE %s ESCAPE '!'")
            parameters.append(f"%{escaped}%")
        if source:
            clauses.append("k.source=%s")
            parameters.append(source)
        if keyword_id:
            clauses.append("k.id=%s")
            parameters.append(keyword_id)
        memberships = self._table("memberships").as_string()
        decisions = self._table("keyword_decisions").as_string()
        algorithms = self._table("algorithm_results").as_string()
        collections = self._table("collections").as_string()
        topic = "COALESCE(cr.classification_status,'')<>'INELIGIBLE'"
        filters = {
            "excluded": ("k.eligible=0 AND (" + topic + " OR EXISTS(SELECT 1 FROM " + decisions
                         + " kd WHERE kd.keyword_id=k.id AND kd.decision='EXCLUDED'))"),
            "non_topic": "cr.classification_status='INELIGIBLE'",
            "unassigned": ("k.eligible=1 AND " + topic + " AND NOT EXISTS(SELECT 1 FROM "
                           + memberships + " m WHERE m.keyword_id=k.id)"),
            "ai": ("k.eligible=1 AND " + topic + " AND EXISTS(SELECT 1 FROM " + memberships
                   + " m WHERE m.keyword_id=k.id AND m.origin='LOCAL_AI')"),
            "manual": ("EXISTS(SELECT 1 FROM " + memberships + " m WHERE m.keyword_id=k.id AND m.origin='USER')"),
            "mixed": (topic + " AND EXISTS(SELECT 1 FROM " + memberships
                      + " m WHERE m.keyword_id=k.id AND m.origin='LOCAL_AI') AND EXISTS(SELECT 1 FROM "
                      + memberships + " m WHERE m.keyword_id=k.id AND m.origin='USER')"),
            "needs_review": ("k.eligible=1 AND " + topic + " AND (NOT EXISTS(SELECT 1 FROM " + memberships
                             + " m WHERE m.keyword_id=k.id) OR EXISTS(SELECT 1 FROM " + memberships
                             + " m WHERE m.keyword_id=k.id AND m.origin='LOCAL_AI'))"),
        }
        if state != "all":
            if state not in filters:
                raise ValueError("INVALID_KEYWORD_STATE")
            clauses.append(filters[state])
        if classification != "all":
            if classification not in {"CLASSIFIED", "AMBIGUOUS", "UNKNOWN", "INELIGIBLE"}:
                raise ValueError("INVALID_CLASSIFICATION_FILTER")
            clauses.append("cr.classification_status=%s")
            parameters.append(classification)
        metric = "(k.evidence->>'views_ceil' IS NOT NULL OR k.evidence->>'approximate_views' IS NOT NULL)"
        algorithm = "EXISTS(SELECT 1 FROM " + algorithms + " ar WHERE ar.keyword_id=k.id AND ar.status='COMPLETED')"
        if evidence == "metric":
            clauses.append(metric)
        elif evidence == "algorithm":
            clauses.append(algorithm)
        elif evidence == "missing":
            clauses.append("NOT " + metric + " AND NOT " + algorithm)
        elif evidence != "all":
            raise ValueError("INVALID_EVIDENCE_FILTER")
        if date_from:
            clauses.append("left(k.observed_at,10)>=%s")
            parameters.append(date_from)
        if date_to:
            clauses.append("left(k.observed_at,10)<=%s")
            parameters.append(date_to)
        ids = tuple(dict.fromkeys(collection_ids))
        if ids:
            clauses.append("EXISTS(SELECT 1 FROM " + memberships + " m WHERE m.keyword_id=k.id AND m.collection_id IN ("
                "WITH RECURSIVE topic_tree(id) AS (SELECT id FROM " + collections
                + " WHERE id=ANY(%s) UNION SELECT child.id FROM " + collections
                + " child JOIN topic_tree parent ON child.parent_id=parent.id) SELECT id FROM topic_tree))")
            parameters.append(list(ids))
        where = " AND ".join(clauses) or "TRUE"
        ordering = {
            "keyword": 'k.normalized COLLATE "C" ASC,k.id COLLATE "C" ASC',
            "views": _sqlite_json_integer("views_ceil") + ' DESC NULLS LAST,k.id COLLATE "C" ASC',
            "rank": _sqlite_json_integer("provider_rank") + ' ASC NULLS FIRST,k.id COLLATE "C" ASC',
            "recent": 'k.observed_at COLLATE "C" DESC,k.id COLLATE "C" ASC',
        }.get(sort)
        if ordering is None:
            raise ValueError("INVALID_KEYWORD_SORT")
        base = (" FROM " + self._table("keywords").as_string() + " k LEFT JOIN "
                + self._table("classification_results").as_string()
                + " cr ON cr.keyword_id=k.id WHERE " + where)
        return base, where, parameters, ordering

    def keyword_ids_query(self, **filters):
        """Return the deterministic ID query and bound parameters for a keyword filter."""
        base, _, parameters, ordering = self._keyword_search_plan(**filters)
        return "SELECT k.id" + base + " ORDER BY " + ordering, parameters

    def search_keywords(self, *, query="", state="all", collection_ids=(), source="",
                        sort="recent", limit=50, offset=0, ids_only=False,
                        classification="all", evidence="all", date_from="", date_to="",
                        keyword_id=None, include_algorithm_results=True):
        base, where, parameters, ordering = self._keyword_search_plan(
            query=query, state=state, collection_ids=collection_ids, source=source, sort=sort,
            classification=classification, evidence=evidence, date_from=date_from,
            date_to=date_to, keyword_id=keyword_id)
        memberships = self._table("memberships").as_string()
        algorithms = self._table("algorithm_results").as_string()
        collections = self._table("collections").as_string()

        def read(db):
            total = db.execute("SELECT count(*) AS total" + base, parameters).fetchone()["total"]
            if ids_only:
                rows = db.execute("SELECT k.id" + base + " ORDER BY " + ordering, parameters).fetchall()
                return {"ids": [row["id"] for row in rows], "total": total}
            pagination = " LIMIT %s OFFSET %s" if limit is not None else ""
            page_params = [*parameters, limit, offset] if limit is not None else list(parameters)
            query_text = """SELECT k.*,e.entity_id,e.entity_type,e.description AS entity_description,
                e.occupations AS entity_occupations,e.category_ids AS entity_category_ids,
                e.instance_types AS entity_instance_types,cr.entity_type AS classified_entity_type,
                cr.entity_id AS classified_entity_id,cr.entity_description AS classified_entity_description,
                cr.topic_category_ids AS classified_topic_category_ids,cr.subcategory_ids AS classified_subcategory_ids,
                cr.classification_status,cr.confidence_level,cr.commercial_relevance,cr.classification_method,
                cr.classification_version,cr.taxonomy_version,cr.rule_evidence AS classification_rule_evidence,
                cr.source_evidence AS classification_source_evidence,cr.review_status,cr.classified_at,
                cr.classification_reason,e.source_url AS entity_source_url,e.method AS entity_classification_method,
                e.status AS entity_classification_status,e.captured_at AS entity_context_captured_at,
                COALESCE((SELECT jsonb_agg(jsonb_build_object('id',c.id,'name',c.name,'origin',m.origin,
                    'pinned',m.pinned,'score',m.score,'reason',m.reason,'category_id',c.category_id) ORDER BY c.id)
                    FROM """ + memberships + " m JOIN " + collections + """ c ON c.id=m.collection_id
                    WHERE m.keyword_id=k.id),'[]'::jsonb) AS memberships_json FROM """
            query_text += (self._table("keywords").as_string() + " k LEFT JOIN "
                           + self._table("entity_context").as_string() + " e ON e.keyword_id=k.id LEFT JOIN "
                           + self._table("classification_results").as_string()
                           + " cr ON cr.keyword_id=k.id WHERE " + where + " ORDER BY " + ordering + pagination)
            rows = db.execute(query_text, page_params).fetchall()
            by_keyword = {}
            if include_algorithm_results and rows:
                result_rows = db.execute("SELECT keyword_id,algorithm_id,version,status,reason_code,value,metric,unit,"
                    "result_json FROM " + algorithms + " WHERE keyword_id=ANY(%s) ORDER BY keyword_id,algorithm_id",
                    ([row["id"] for row in rows],)).fetchall()
                for result in result_rows:
                    item = dict(result)
                    result_id = item.pop("keyword_id")
                    by_keyword.setdefault(result_id, []).append(item)
            from app.taxonomy.registry import resolve

            items = []
            for row in rows:
                item = _json_result(row)
                item["memberships"] = item.pop("memberships_json")
                for field in ("entity_occupations", "entity_category_ids", "entity_instance_types",
                              "classified_topic_category_ids", "classified_subcategory_ids",
                              "classification_rule_evidence"):
                    if item[field] is None:
                        item[field] = []
                    if not isinstance(item[field], list):
                        raise CollectionReadError("stored keyword metadata is invalid")
                if item["classification_source_evidence"] is None:
                    item["classification_source_evidence"] = {}
                if not isinstance(item["classification_source_evidence"], dict):
                    raise CollectionReadError("stored classification evidence is invalid")
                item["classified_topic_categories"] = [
                    {"id": category_id, "name": resolve(category_id).display_name,
                     "scope": resolve(category_id).scope}
                    for category_id in item["classified_topic_category_ids"]
                ]
                if item.get("classification_status") == "INELIGIBLE":
                    item["eligible"] = 0
                    item["topic_eligible"] = False
                    if not item.get("rejection_reason"):
                        item["rejection_reason"] = "CLASSIFICATION_INELIGIBLE"
                else:
                    item["topic_eligible"] = bool(item.get("eligible"))
                if include_algorithm_results:
                    item["algorithm_results"] = by_keyword.get(item["id"], [])
                items.append(item)
            return {"items": items, "total": total, "limit": limit, "offset": offset,
                    "next_offset": offset + limit if limit is not None and offset + limit < total else None}
        return self._read(read)
