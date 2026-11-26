"""Dedicated local SQLite state for user-defined Collections, isolated from D6."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.collections.analysis_storage import SQLiteAlgorithmAnalysisSession
from app.collections.identity import fingerprint as fingerprint
from app.storage.local_file_security import secure_private_directory

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS collections (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 category_id TEXT UNIQUE, parent_id TEXT REFERENCES collections(id) ON DELETE CASCADE,
 examples TEXT NOT NULL DEFAULT '[]', exclusions TEXT NOT NULL DEFAULT '[]',
 created_by TEXT NOT NULL CHECK(created_by IN ('USER','AI')),
 auto_match INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'ACTIVE',
 version INTEGER NOT NULL DEFAULT 1, reason TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS keywords (
 id TEXT PRIMARY KEY, keyword TEXT NOT NULL, normalized TEXT NOT NULL,
 source TEXT NOT NULL, project TEXT NOT NULL DEFAULT '', observed_at TEXT NOT NULL,
 data_mode TEXT NOT NULL, evidence TEXT NOT NULL, context TEXT NOT NULL DEFAULT '',
 eligible INTEGER NOT NULL DEFAULT 1, rejection_reason TEXT
);
CREATE TABLE IF NOT EXISTS memberships (
 collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
 keyword_id TEXT NOT NULL REFERENCES keywords(id) ON DELETE RESTRICT,
 origin TEXT NOT NULL CHECK(origin IN ('USER','LOCAL_AI')),
 pinned INTEGER NOT NULL DEFAULT 0, score REAL, reason TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, PRIMARY KEY(collection_id, keyword_id)
);
-- Keyword-centric filters and unassigned counts cannot efficiently use the
-- collection-first primary key. Also creates the index for existing snapshots.
CREATE INDEX IF NOT EXISTS memberships_keyword_id ON memberships(keyword_id);
CREATE TABLE IF NOT EXISTS feedback (
 collection_id TEXT NOT NULL, keyword_id TEXT NOT NULL, action TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(collection_id, keyword_id)
);
CREATE TABLE IF NOT EXISTS cache (
 fingerprint TEXT PRIMARY KEY, model_version TEXT NOT NULL, collection_version INTEGER NOT NULL,
 text_hash TEXT NOT NULL, vector BLOB NOT NULL, dimension INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
 support_ids TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppression (
 fingerprint TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TEXT NOT NULL, label TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fetch_jobs (
 fetch_id TEXT PRIMARY KEY, status TEXT NOT NULL, available INTEGER NOT NULL DEFAULT 0,
 processed INTEGER NOT NULL DEFAULT 0, assigned INTEGER NOT NULL DEFAULT 0,
 unassigned INTEGER NOT NULL DEFAULT 0, created_collections INTEGER NOT NULL DEFAULT 0,
 suggestions INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
 error_code TEXT, updated_at TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'QUEUED',
 eligible INTEGER NOT NULL DEFAULT 0, cached_embeddings INTEGER NOT NULL DEFAULT 0,
 inferred_embeddings INTEGER NOT NULL DEFAULT 0,
 membership_reconciliation_id TEXT
);
CREATE TABLE IF NOT EXISTS algorithm_observations (
 keyword_id TEXT NOT NULL, source TEXT NOT NULL, observation_date TEXT NOT NULL,
 country TEXT NOT NULL, access TEXT NOT NULL, project TEXT NOT NULL, metric TEXT NOT NULL,
 unit TEXT NOT NULL, status TEXT NOT NULL, value INTEGER, rank INTEGER,
 snapshot_status TEXT NOT NULL, input_digest TEXT NOT NULL,
 PRIMARY KEY(keyword_id, source, observation_date, metric, unit)
);
CREATE TABLE IF NOT EXISTS algorithm_results (
 keyword_id TEXT NOT NULL, algorithm_id TEXT NOT NULL, version TEXT NOT NULL,
 status TEXT NOT NULL, reason_code TEXT, value REAL, metric TEXT NOT NULL, unit TEXT NOT NULL,
 result_json TEXT NOT NULL, input_digest TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(keyword_id, algorithm_id)
);
CREATE TABLE IF NOT EXISTS dataset_snapshots (
 dataset_id TEXT PRIMARY KEY, status TEXT NOT NULL, data_mode TEXT NOT NULL,
 source_manifest TEXT NOT NULL, provider_record_count INTEGER NOT NULL,
 received_record_count INTEGER NOT NULL, retained_keyword_count INTEGER NOT NULL,
 rejected_record_count INTEGER NOT NULL, observation_start TEXT, observation_end TEXT,
 fetched_at TEXT, quality_flags TEXT NOT NULL, processing_status TEXT NOT NULL,
 eligible_keyword_count INTEGER NOT NULL DEFAULT 0,
 assigned_keyword_count INTEGER NOT NULL DEFAULT 0,
 unassigned_keyword_count INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS keyword_decisions (
 keyword_id TEXT PRIMARY KEY REFERENCES keywords(id) ON DELETE CASCADE,
 decision TEXT NOT NULL CHECK(decision IN ('EXCLUDED','RESTORED')),
 reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_context (
 keyword_id TEXT PRIMARY KEY REFERENCES keywords(id) ON DELETE CASCADE,
 title TEXT NOT NULL, page_id INTEGER, entity_id TEXT, description TEXT NOT NULL DEFAULT '',
 page_namespace INTEGER, is_disambiguation INTEGER NOT NULL DEFAULT 0,
 is_main_page INTEGER NOT NULL DEFAULT 0,
 is_main_page_known INTEGER NOT NULL DEFAULT 0,
 redirected_from TEXT NOT NULL DEFAULT '', page_language TEXT NOT NULL DEFAULT '',
 entity_type TEXT NOT NULL, occupations TEXT NOT NULL DEFAULT '[]',
 category_ids TEXT NOT NULL DEFAULT '[]', instance_types TEXT NOT NULL DEFAULT '[]', source_url TEXT NOT NULL DEFAULT '',
 method TEXT NOT NULL, status TEXT NOT NULL, context_version TEXT NOT NULL,
 captured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS classification_results (
 keyword_id TEXT PRIMARY KEY REFERENCES keywords(id) ON DELETE CASCADE,
 entity_type TEXT NOT NULL, entity_id TEXT, entity_description TEXT NOT NULL DEFAULT '',
 topic_category_ids TEXT NOT NULL DEFAULT '[]', subcategory_ids TEXT NOT NULL DEFAULT '[]',
 classification_status TEXT NOT NULL CHECK(classification_status IN ('CLASSIFIED','AMBIGUOUS','UNKNOWN','INELIGIBLE')),
 confidence_level TEXT NOT NULL, commercial_relevance TEXT NOT NULL DEFAULT 'NOT_ASSESSED', classification_method TEXT NOT NULL,
 classification_version TEXT NOT NULL, taxonomy_version TEXT NOT NULL,
 rule_evidence TEXT NOT NULL DEFAULT '[]', source_evidence TEXT NOT NULL DEFAULT '{}',
 review_status TEXT NOT NULL, classified_at TEXT NOT NULL,
 classification_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS membership_reconciliation_runs (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS membership_reconciliation_items (
 run_id TEXT NOT NULL, collection_id TEXT NOT NULL, keyword_id TEXT NOT NULL,
 origin TEXT NOT NULL, pinned INTEGER NOT NULL, score REAL, reason TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(run_id,collection_id,keyword_id)
);
CREATE TABLE IF NOT EXISTS research_records (
 id TEXT PRIMARY KEY, keyword_id TEXT REFERENCES keywords(id) ON DELETE SET NULL,
 subject TEXT NOT NULL, hypothesis TEXT NOT NULL, product_category TEXT NOT NULL DEFAULT '',
 niche TEXT NOT NULL DEFAULT '', competitor_notes TEXT NOT NULL DEFAULT '',
 market_evidence TEXT NOT NULL DEFAULT '', ip_notes TEXT NOT NULL DEFAULT '',
 risk_notes TEXT NOT NULL DEFAULT '', findings TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL CHECK(status IN ('DRAFT','RESEARCHING','NEEDS_REVIEW','VALIDATED','ARCHIVED')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


class ClosingConnection(sqlite3.Connection):
    """Close SQLite handles when a with-block exits (including on Windows)."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CollectionStore:
    def __init__(self, path: Path, *, data_mode_scope: str | None = None):
        if data_mode_scope not in {None, "LIVE"}:
            raise ValueError("unsupported Collections data mode scope")
        path = Path(path)
        secure_private_directory(path.parent)
        self.path = path
        self.data_mode_scope = data_mode_scope
        with self.connect() as db:
            db.executescript(SCHEMA)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(suppression)")}
            if "label" not in columns:
                db.execute("ALTER TABLE suppression ADD COLUMN label TEXT NOT NULL DEFAULT ''")
            keyword_columns = {row["name"] for row in db.execute("PRAGMA table_info(keywords)")}
            if "eligible" not in keyword_columns:
                db.execute("ALTER TABLE keywords ADD COLUMN eligible INTEGER NOT NULL DEFAULT 1")
            classification_columns = {row[1] for row in db.execute("PRAGMA table_info(classification_results)")}
            if "classification_reason" not in classification_columns:
                db.execute("ALTER TABLE classification_results ADD COLUMN classification_reason TEXT NOT NULL DEFAULT ''")
            if "rejection_reason" not in keyword_columns:
                db.execute("ALTER TABLE keywords ADD COLUMN rejection_reason TEXT")
            job_columns = {row["name"] for row in db.execute("PRAGMA table_info(fetch_jobs)")}
            for name, declaration in (("stage", "TEXT NOT NULL DEFAULT 'QUEUED'"),
                                      ("eligible", "INTEGER NOT NULL DEFAULT 0"),
                                      ("cached_embeddings", "INTEGER NOT NULL DEFAULT 0"),
                                      ("inferred_embeddings", "INTEGER NOT NULL DEFAULT 0"),
                                      ("membership_reconciliation_id", "TEXT")):
                if name not in job_columns:
                    db.execute(f"ALTER TABLE fetch_jobs ADD COLUMN {name} {declaration}")
            collection_columns = {row["name"] for row in db.execute("PRAGMA table_info(collections)")}
            if "category_id" not in collection_columns:
                db.execute("ALTER TABLE collections ADD COLUMN category_id TEXT")
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS collections_category_id ON collections(category_id)")
            if "parent_id" not in collection_columns:
                db.execute("ALTER TABLE collections ADD COLUMN parent_id TEXT REFERENCES collections(id) ON DELETE CASCADE")
            context_columns = {row["name"] for row in db.execute("PRAGMA table_info(entity_context)")}
            if "instance_types" not in context_columns:
                db.execute("ALTER TABLE entity_context ADD COLUMN instance_types TEXT NOT NULL DEFAULT '[]'")
            for name, declaration in (("page_namespace", "INTEGER"),
                                      ("is_disambiguation", "INTEGER NOT NULL DEFAULT 0"),
                                      ("is_main_page", "INTEGER NOT NULL DEFAULT 0"),
                                      ("is_main_page_known", "INTEGER NOT NULL DEFAULT 0"),
                                      ("redirected_from", "TEXT NOT NULL DEFAULT ''"),
                                      ("page_language", "TEXT NOT NULL DEFAULT ''")):
                if name not in context_columns:
                    db.execute(f"ALTER TABLE entity_context ADD COLUMN {name} {declaration}")
            classification_columns = {row["name"] for row in db.execute("PRAGMA table_info(classification_results)")}
            if classification_columns and "commercial_relevance" not in classification_columns:
                db.execute("ALTER TABLE classification_results ADD COLUMN commercial_relevance TEXT NOT NULL DEFAULT 'NOT_ASSESSED'")
        if self.path.is_symlink():
            raise ValueError("collection database must not be a symlink")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            raise ValueError("collection database permissions could not be restricted") from None

    @classmethod
    def local(cls, state_dir: Path | None = None):
        configured_directory = os.environ.get("TRENDSCOUT_COLLECTIONS_DIR") if state_dir is None else None
        if configured_directory:
            directory = Path(configured_directory)
        elif state_dir is None and os.name == "nt":
            app_data = os.environ.get("LOCALAPPDATA")
            if not app_data:
                raise RuntimeError("LOCALAPPDATA is required for Windows Collections storage")
            directory = Path(app_data) / "TrendScout" / "collections-v1"
        else:
            directory = state_dir or Path.home() / ".local/state/trendscout/collections-v1"
        return cls(Path(directory) / "collections.sqlite", data_mode_scope="LIVE")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def run_algorithm_analysis(self, operation):
        """Run algorithm reads and writes inside one serialized transaction."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return operation(SQLiteAlgorithmAnalysisSession(db, self.data_mode_scope))

    def entity_context_rows(self, keyword_ids):
        identifiers = list(dict.fromkeys(keyword_ids))
        rows = []
        with self.connect() as db:
            for offset in range(0, len(identifiers), 500):
                batch = identifiers[offset:offset + 500]
                if not batch:
                    continue
                marks = ",".join("?" for _ in batch)
                rows.extend(dict(row) for row in db.execute(
                    f"SELECT * FROM entity_context WHERE keyword_id IN ({marks})", batch
                ).fetchall())
        return rows

    def entity_context_keyword_ids(self, keyword_ids, context_version):
        identifiers = list(dict.fromkeys(keyword_ids))
        found = set()
        with self.connect() as db:
            for offset in range(0, len(identifiers), 500):
                batch = identifiers[offset:offset + 500]
                if not batch:
                    continue
                marks = ",".join("?" for _ in batch)
                found.update(row[0] for row in db.execute(
                    f"SELECT keyword_id FROM entity_context WHERE context_version=? "
                    f"AND keyword_id IN ({marks})", (context_version, *batch),
                ).fetchall())
        return found

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

        conditions = ["source=?"]
        parameters = [source_id]
        effective_mode = self.data_mode_scope or data_mode
        if effective_mode:
            conditions.append("data_mode=?")
            parameters.append(effective_mode)
        if project_prefix is not None:
            conditions.append("substr(project,1,length(?))=?")
            parameters.extend((project_prefix, project_prefix))
        if eligible_only:
            conditions.append("eligible=1")
        parameters.append(limit)

        with self.connect() as db:
            rows = db.execute(
                "SELECT id,keyword,normalized,source,project,observed_at,data_mode,evidence,eligible "
                "FROM keywords WHERE " + " AND ".join(conditions) +
                " ORDER BY observed_at DESC,id LIMIT ?",
                parameters,
            ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            try:
                row["evidence"] = json.dumps(
                    json.loads(row["evidence"]), ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError):
                # Preserve malformed legacy evidence so consumers can report
                # or skip it without turning a read into a write or failure.
                pass
        return result

    def upsert_entity_context(self, rows):
        if not rows:
            return
        with self.connect() as db:
            db.executemany("""INSERT INTO entity_context(keyword_id,title,page_id,entity_id,description,
                page_namespace,is_disambiguation,redirected_from,page_language,is_main_page,is_main_page_known,
                entity_type,occupations,category_ids,instance_types,source_url,method,status,context_version,captured_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(keyword_id) DO UPDATE SET
                title=excluded.title,page_id=excluded.page_id,entity_id=excluded.entity_id,
                description=excluded.description,page_namespace=excluded.page_namespace,
                is_disambiguation=excluded.is_disambiguation,redirected_from=excluded.redirected_from,
                page_language=excluded.page_language,is_main_page=excluded.is_main_page,
                is_main_page_known=excluded.is_main_page_known,entity_type=excluded.entity_type,
                occupations=excluded.occupations,category_ids=excluded.category_ids,instance_types=excluded.instance_types,
                source_url=excluded.source_url,method=excluded.method,status=excluded.status,
                context_version=excluded.context_version,captured_at=excluded.captured_at""", rows)

    def runtime_storage_metrics(self, model_version):
        mode = self.data_mode_scope
        with self.connect() as db:
            cache = db.execute("""SELECT count(*) AS vectors,coalesce(sum(dimension),0) AS dimensions,
                group_concat(DISTINCT dimension) AS vector_dimensions FROM cache WHERE model_version=?""",
                (model_version,)).fetchone()
            integrity = db.execute("PRAGMA quick_check(1)").fetchone()[0]
            keyword_count = db.execute("SELECT count(*) FROM keywords "
                + ("WHERE data_mode=?" if mode else ""), (mode,) if mode else ()).fetchone()[0]
        return {"engine": "SQLITE", "integrity": integrity, "keyword_records": keyword_count,
                "cache_vectors": cache["vectors"] or 0,
                "cached_dimensions_total": cache["dimensions"] or 0,
                "vector_dimensions": cache["vector_dimensions"]}

    def recent_dataset_snapshots(self, *, limit=100):
        mode = self.data_mode_scope
        with self.connect() as db:
            rows = db.execute("""SELECT status,data_mode,source_manifest,fetched_at,
                observation_start,observation_end FROM dataset_snapshots """
                + ("WHERE data_mode=? " if mode else "")
                + "ORDER BY created_at DESC LIMIT ?",
                (mode, limit) if mode else (limit,)).fetchall()
        return [dict(row) for row in rows]

    def latest_dataset(self):
        """Return durable local dataset metadata, with an honest legacy fallback."""
        mode = self.data_mode_scope
        mode_parameters = (mode,) if mode else ()
        with self.connect() as db:
            row = db.execute("SELECT * FROM dataset_snapshots "
                + ("WHERE data_mode=? " if mode else "")
                + "ORDER BY created_at DESC LIMIT 1", mode_parameters).fetchone()
            keyword_filter = " WHERE data_mode=?" if mode else ""
            total, eligible, excluded = db.execute("""SELECT count(*),
                sum(k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE'),
                sum((k.eligible=0 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE') OR
                    EXISTS(SELECT 1 FROM keyword_decisions kd WHERE kd.keyword_id=k.id AND kd.decision='EXCLUDED'))
                FROM keywords k LEFT JOIN classification_results cr ON cr.keyword_id=k.id"""
                + (" WHERE k.data_mode=?" if mode else ""),
                mode_parameters).fetchone()
            assigned = db.execute("""SELECT count(DISTINCT m.keyword_id) FROM memberships m
                JOIN keywords k ON k.id=m.keyword_id LEFT JOIN classification_results cr ON cr.keyword_id=k.id
                WHERE k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE' """
                + ("AND k.data_mode=?" if mode else ""), mode_parameters).fetchone()[0]
            unassigned = db.execute("""SELECT count(*) FROM keywords k WHERE k.eligible=1 """
                + ("AND k.data_mode=? " if mode else "") + """
                AND COALESCE((SELECT classification_status FROM classification_results cr WHERE cr.keyword_id=k.id),'')<>'INELIGIBLE'
                AND NOT EXISTS
                (SELECT 1 FROM memberships m WHERE m.keyword_id=k.id)""",
                mode_parameters).fetchone()[0]
            if row is None:
                legacy = None if mode else db.execute("""SELECT * FROM fetch_jobs WHERE status IN ('COMPLETED','MODEL_UNAVAILABLE')
                    ORDER BY updated_at DESC LIMIT 1""").fetchone()
                if legacy is None and not total:
                    return None
                sources = [item[0] for item in db.execute("SELECT DISTINCT source FROM keywords" + keyword_filter
                    + " ORDER BY source", mode_parameters)]
                modes = [item[0] for item in db.execute("SELECT DISTINCT data_mode FROM keywords" + keyword_filter
                    + " ORDER BY data_mode", mode_parameters)]
                fetched = None
                dates = []
                for evidence, observed in db.execute("SELECT evidence,observed_at FROM keywords" + keyword_filter,
                                                      mode_parameters):
                    try:
                        metadata = json.loads(evidence)
                        fetched = max(fetched or '', metadata.get('fetched_at') or '') or None
                    except (TypeError, ValueError):
                        pass
                    dates.append(str(observed)[:10])
                return {"dataset_id": legacy["fetch_id"] if legacy else "legacy-local-ledger",
                    "status": "LEGACY_CACHED", "processing_status": legacy["status"] if legacy else "UNKNOWN",
                    "data_mode": modes[0] if len(modes) == 1 else "MIXED" if modes else "UNKNOWN",
                    "sources": sources, "provider_record_count": legacy["available"] if legacy else None,
                    "received_record_count": None, "retained_keyword_count": total,
                    "eligible_keyword_count": eligible or 0, "excluded_keyword_count": excluded or 0,
                    "assigned_keyword_count": assigned, "unassigned_keyword_count": unassigned,
                    "observation_start": min(dates) if dates else None,
                    "observation_end": max(dates) if dates else None, "fetched_at": fetched,
                    "quality_flags": ["LEGACY_COVERAGE_UNKNOWN"], "coverage_status": "UNKNOWN"}
            item = dict(row)
            item["source_manifest"] = json.loads(item["source_manifest"])
            item["quality_flags"] = json.loads(item["quality_flags"])
            item["sources"] = [source["source_id"] for source in item["source_manifest"]]
            item["snapshot_retained_keyword_count"] = item["retained_keyword_count"]
            item["retained_keyword_count"] = total
            item["ledger_coverage_status"] = (
                "MIXED_OR_UNKNOWN" if item["snapshot_retained_keyword_count"] < total else "SNAPSHOT_ONLY"
            )
            if item["ledger_coverage_status"] == "MIXED_OR_UNKNOWN":
                item["quality_flags"] = sorted(set(item["quality_flags"] + ["PRIOR_LEDGER_COVERAGE_UNKNOWN"]))
            item["eligible_keyword_count"] = eligible or 0
            item["excluded_keyword_count"] = excluded or 0
            item["assigned_keyword_count"] = assigned
            item["unassigned_keyword_count"] = unassigned
            item["coverage_status"] = "PARTIAL" if item["status"] == "PARTIAL" else "RECORDED"
            return item

    def algorithm_results_overview(self, *, limit=100):
        mode = self.data_mode_scope
        keyword_filter = " AND k.data_mode=?" if mode else ""
        parameters = (mode,) if mode else ()
        with self.connect() as db:
            counts = db.execute('''SELECT r.algorithm_id,r.version,
                sum(status='COMPLETED') AS completed,sum(status='SKIPPED') AS skipped,
                sum(status='FAILED') AS failed,count(*) AS total,max(updated_at) AS updated_at
                FROM algorithm_results r JOIN keywords k ON k.id=r.keyword_id
                LEFT JOIN classification_results cr ON cr.keyword_id=k.id
                WHERE COALESCE(cr.classification_status,'')<>'INELIGIBLE' '''
                + keyword_filter + ' GROUP BY r.algorithm_id,r.version ORDER BY r.algorithm_id',
                parameters).fetchall()
            rows = db.execute('''SELECT r.keyword_id,k.keyword,r.algorithm_id,r.version,r.status,r.reason_code,
                r.value,r.metric,r.unit,r.result_json,r.updated_at FROM algorithm_results r
                JOIN keywords k ON k.id=r.keyword_id LEFT JOIN classification_results cr ON cr.keyword_id=k.id
                WHERE k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE' '''
                + keyword_filter + ' ORDER BY r.updated_at DESC,k.keyword COLLATE NOCASE,r.algorithm_id LIMIT ?',
                (*parameters, limit)).fetchall()
        return {"summary": [dict(row) for row in counts], "items": [dict(row) for row in rows],
                "limit": limit}

    def analytics_overview_inputs(self):
        mode = self.data_mode_scope
        parameters = (mode,) if mode else ()
        keyword_filter = " AND k.data_mode=?" if mode else ""
        dataset_filter = " WHERE data_mode=?" if mode else ""
        with self.connect() as db:
            totals = db.execute("""SELECT count(*) AS keywords,
                sum(CASE WHEN k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE'
                    THEN 1 ELSE 0 END) AS eligible,
                sum(CASE WHEN (k.eligible=0 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE')
                    OR EXISTS(SELECT 1 FROM keyword_decisions kd WHERE kd.keyword_id=k.id
                        AND kd.decision='EXCLUDED') THEN 1 ELSE 0 END) AS excluded
                FROM keywords k LEFT JOIN classification_results cr ON cr.keyword_id=k.id"""
                + (" WHERE k.data_mode=?" if mode else ""), parameters).fetchone()
            classifications = db.execute("""SELECT cr.classification_status,count(*) AS count
                FROM classification_results cr JOIN keywords k ON k.id=cr.keyword_id
                WHERE (k.eligible=1 OR cr.classification_status='INELIGIBLE')
                AND NOT EXISTS(SELECT 1 FROM keyword_decisions kd WHERE kd.keyword_id=k.id
                    AND kd.decision='EXCLUDED')""" + keyword_filter
                + " GROUP BY cr.classification_status", parameters).fetchall()
            source_rows = db.execute("""SELECT source,count(DISTINCT id) AS keywords,
                min(substr(observed_at,1,10)) AS first_seen,max(substr(observed_at,1,10)) AS last_seen
                FROM keywords k WHERE 1=1""" + keyword_filter
                + " GROUP BY source ORDER BY keywords DESC,source", parameters).fetchall()
            algorithm_rows = db.execute("""SELECT r.algorithm_id,r.version,r.status,count(*) AS count
                FROM algorithm_results r JOIN keywords k ON k.id=r.keyword_id
                LEFT JOIN classification_results cr ON cr.keyword_id=k.id
                WHERE COALESCE(cr.classification_status,'')<>'INELIGIBLE'""" + keyword_filter
                + " GROUP BY r.algorithm_id,r.version,r.status ORDER BY r.algorithm_id,r.status",
                parameters).fetchall()
            observations = db.execute("""SELECT o.source,o.observation_date,
                count(DISTINCT o.keyword_id) AS keywords,sum(o.status='OBSERVED') AS observed,
                sum(o.status<>'OBSERVED') AS unavailable FROM algorithm_observations o
                JOIN keywords k ON k.id=o.keyword_id WHERE 1=1""" + keyword_filter
                + " GROUP BY o.source,o.observation_date ORDER BY o.observation_date,o.source",
                parameters).fetchall()
            top = db.execute("""SELECT k.id,k.keyword,k.source,k.project,k.observed_at,r.value,r.metric,
                r.unit,r.updated_at,r.result_json FROM algorithm_results r JOIN keywords k ON k.id=r.keyword_id
                WHERE r.algorithm_id='observed-volume-rank' AND r.status='COMPLETED' AND r.value IS NOT NULL
                AND k.eligible=1 AND COALESCE((SELECT classification_status FROM classification_results cr
                    WHERE cr.keyword_id=k.id),'')<>'INELIGIBLE'""" + keyword_filter
                + " ORDER BY r.updated_at DESC,k.keyword COLLATE NOCASE LIMIT 1000", parameters).fetchall()
            latest = db.execute("""SELECT dataset_id,status,data_mode,source_manifest,provider_record_count,
                received_record_count,retained_keyword_count,rejected_record_count,observation_start,
                observation_end,fetched_at,created_at,quality_flags,processing_status,eligible_keyword_count,
                assigned_keyword_count,unassigned_keyword_count FROM dataset_snapshots"""
                + dataset_filter + " ORDER BY created_at DESC LIMIT 1", parameters).fetchone()
            history = db.execute("""SELECT dataset_id,status,data_mode,source_manifest,provider_record_count,
                received_record_count,retained_keyword_count,rejected_record_count,observation_start,
                observation_end,fetched_at,quality_flags,processing_status FROM dataset_snapshots"""
                + dataset_filter + " ORDER BY created_at DESC LIMIT 30", parameters).fetchall()
            successful = db.execute("""SELECT dataset_id,status,data_mode,source_manifest,fetched_at,created_at,
                observation_start,observation_end,quality_flags FROM dataset_snapshots
                WHERE status IN ('SUCCESS','PARTIAL') """ + ("AND data_mode=? " if mode else "")
                + "ORDER BY created_at DESC LIMIT 1", parameters).fetchone()
            membership_count = db.execute("""SELECT count(DISTINCT m.keyword_id) FROM memberships m
                JOIN keywords k ON k.id=m.keyword_id LEFT JOIN classification_results cr ON cr.keyword_id=k.id
                WHERE COALESCE(cr.classification_status,'')<>'INELIGIBLE'""" + keyword_filter,
                parameters).fetchone()[0]
            unassigned_topic_count = db.execute("""SELECT count(*) FROM keywords k
                WHERE k.eligible=1 AND COALESCE((SELECT classification_status FROM classification_results cr
                    WHERE cr.keyword_id=k.id),'')<>'INELIGIBLE' AND NOT EXISTS(
                    SELECT 1 FROM memberships m WHERE m.keyword_id=k.id)""" + keyword_filter,
                parameters).fetchone()[0]
        return {
            "totals": dict(totals),
            "classifications": [dict(row) for row in classifications],
            "source_rows": [dict(row) for row in source_rows],
            "algorithm_rows": [dict(row) for row in algorithm_rows],
            "observations": [dict(row) for row in observations],
            "top": [dict(row) for row in top],
            "latest": dict(latest) if latest else None,
            "history": [dict(row) for row in history],
            "successful": dict(successful) if successful else None,
            "membership_count": membership_count,
            "unassigned_topic_count": unassigned_topic_count,
        }

    def local_run_history_rows(self, *, limit=None, cursor=None, status=None, mode=None, fetch_id=None):
        clauses, parameters = [], []
        if cursor:
            clauses.append("(COALESCE(j.updated_at,d.fetched_at)<? OR "
                           "(COALESCE(j.updated_at,d.fetched_at)=? AND j.fetch_id<?))")
            parameters.extend((cursor[0], cursor[0], cursor[1]))
        if status:
            clauses.append("(CASE WHEN d.status='SUCCESS' THEN 'COMPLETED' "
                "WHEN d.status IN ('COMPLETED','PARTIAL','FAILED','ABORTED') "
                "THEN d.status ELSE j.status END)=?")
            parameters.append(status)
        if mode:
            clauses.append("d.data_mode=?")
            parameters.append(mode)
        if fetch_id:
            clauses.append("j.fetch_id=?")
            parameters.append(fetch_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = """SELECT j.*,d.dataset_id,d.status AS dataset_status,d.data_mode,d.source_manifest,
            d.provider_record_count,d.received_record_count,d.rejected_record_count,d.observation_start,
            d.observation_end,d.fetched_at,d.quality_flags,d.processing_status,
            COALESCE(j.updated_at,d.fetched_at) AS history_sort_at
            FROM fetch_jobs j LEFT JOIN dataset_snapshots d ON d.dataset_id=j.fetch_id""" + where
        query += " ORDER BY history_sort_at DESC,j.fetch_id DESC"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self.connect() as db:
            rows = db.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def fetch_collection_processing_result(self, fetch_id):
        with self.connect() as db:
            row = db.execute("""SELECT j.status,j.error_code FROM fetch_jobs j
                JOIN dataset_snapshots d ON d.dataset_id=j.fetch_id
                WHERE j.fetch_id=? AND d.data_mode='LIVE'
                AND j.status IN ('COMPLETED','MODEL_UNAVAILABLE')
                AND d.processing_status IN ('COMPLETED','MODEL_UNAVAILABLE')""",
                (fetch_id,)).fetchone()
        return dict(row) if row is not None else None

    def search_keywords(self, *, query="", state="all", collection_ids=(), source="",
                        sort="recent", limit=50, offset=0, ids_only=False,
                        classification="all", evidence="all", date_from="", date_to="",
                        keyword_id=None, include_algorithm_results=True):
        normalized = " ".join(query.casefold().split())
        clauses, parameters = [], []
        if self.data_mode_scope:
            clauses.append("k.data_mode=?")
            parameters.append(self.data_mode_scope)
        if normalized:
            escaped = normalized.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            clauses.append("k.normalized LIKE ? ESCAPE '!'")
            parameters.append(f"%{escaped}%")
        if source:
            clauses.append("k.source=?")
            parameters.append(source)
        if keyword_id:
            clauses.append("k.id=?")
            parameters.append(keyword_id)
        if state == "excluded":
            clauses.append("k.eligible=0 AND (COALESCE(cr.classification_status,'')<>'INELIGIBLE' OR "
                "EXISTS(SELECT 1 FROM keyword_decisions kd WHERE kd.keyword_id=k.id AND kd.decision='EXCLUDED'))")
        elif state == "non_topic":
            clauses.append("cr.classification_status='INELIGIBLE'")
        elif state == "unassigned":
            clauses.append("k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE' AND NOT EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id)")
        elif state == "ai":
            clauses.append("k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE' AND EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id AND m.origin='LOCAL_AI')")
        elif state == "manual":
            clauses.append("EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id AND m.origin='USER')")
        elif state == "mixed":
            clauses.append("COALESCE(cr.classification_status,'')<>'INELIGIBLE' AND EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id AND m.origin='LOCAL_AI') AND EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id AND m.origin='USER')")
        elif state == "needs_review":
            clauses.append("k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE' AND (NOT EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id) OR EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id AND m.origin='LOCAL_AI'))")
        elif state != "all":
            raise ValueError("INVALID_KEYWORD_STATE")
        if classification != "all":
            if classification not in {"CLASSIFIED", "AMBIGUOUS", "UNKNOWN", "INELIGIBLE"}:
                raise ValueError("INVALID_CLASSIFICATION_FILTER")
            clauses.append("cr.classification_status=?")
            parameters.append(classification)
        if evidence == "metric":
            clauses.append("(json_extract(k.evidence,'$.views_ceil') IS NOT NULL OR json_extract(k.evidence,'$.approximate_views') IS NOT NULL)")
        elif evidence == "algorithm":
            clauses.append("EXISTS(SELECT 1 FROM algorithm_results ar WHERE ar.keyword_id=k.id AND ar.status='COMPLETED')")
        elif evidence == "missing":
            clauses.append("json_extract(k.evidence,'$.views_ceil') IS NULL AND json_extract(k.evidence,'$.approximate_views') IS NULL AND NOT EXISTS(SELECT 1 FROM algorithm_results ar WHERE ar.keyword_id=k.id AND ar.status='COMPLETED')")
        elif evidence != "all":
            raise ValueError("INVALID_EVIDENCE_FILTER")
        if date_from:
            clauses.append("substr(k.observed_at,1,10)>=?")
            parameters.append(date_from)
        if date_to:
            clauses.append("substr(k.observed_at,1,10)<=?")
            parameters.append(date_to)
        ids = tuple(dict.fromkeys(collection_ids))
        if ids:
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"""EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id AND m.collection_id IN (
                WITH RECURSIVE topic_tree(id) AS (
                    SELECT id FROM collections WHERE id IN ({placeholders})
                    UNION ALL SELECT child.id FROM collections child JOIN topic_tree parent ON child.parent_id=parent.id
                ) SELECT id FROM topic_tree))""")
            parameters.extend(ids)
        where = " AND ".join(clauses) or "1=1"
        ordering = {
            "keyword": "k.normalized ASC,k.id ASC",
            "views": "CAST(json_extract(k.evidence,'$.views_ceil') AS INTEGER) DESC,k.id ASC",
            "rank": "CAST(json_extract(k.evidence,'$.provider_rank') AS INTEGER) ASC,k.id ASC",
            "recent": "k.observed_at DESC,k.id ASC",
        }.get(sort)
        if ordering is None:
            raise ValueError("INVALID_KEYWORD_SORT")
        with self.connect() as db:
            total = db.execute(f"SELECT count(*) FROM keywords k LEFT JOIN classification_results cr ON cr.keyword_id=k.id WHERE {where}", parameters).fetchone()[0]
            if ids_only:
                rows = db.execute(f"SELECT k.id FROM keywords k LEFT JOIN classification_results cr ON cr.keyword_id=k.id WHERE {where} ORDER BY {ordering}",
                                  parameters).fetchall()
                return {"ids": [row["id"] for row in rows], "total": total}
            pagination = " LIMIT ? OFFSET ?" if limit is not None else ""
            page_parameters = (*parameters, limit, offset) if limit is not None else tuple(parameters)
            rows = db.execute(f"""SELECT k.*,e.entity_id,e.entity_type,e.description AS entity_description,
                e.occupations AS entity_occupations,e.category_ids AS entity_category_ids,
                e.instance_types AS entity_instance_types,
                cr.entity_type AS classified_entity_type,cr.entity_id AS classified_entity_id,
                cr.entity_description AS classified_entity_description,
                cr.topic_category_ids AS classified_topic_category_ids,
                cr.subcategory_ids AS classified_subcategory_ids,cr.classification_status,
                cr.confidence_level,cr.commercial_relevance,cr.classification_method,cr.classification_version,cr.taxonomy_version,
                cr.rule_evidence AS classification_rule_evidence,cr.source_evidence AS classification_source_evidence,
                cr.review_status,cr.classified_at,cr.classification_reason,
                e.source_url AS entity_source_url,e.method AS entity_classification_method,
                e.status AS entity_classification_status,e.captured_at AS entity_context_captured_at,
                (SELECT json_group_array(json_object('id',c.id,'name',c.name,'origin',m.origin,
                    'pinned',m.pinned,'score',m.score,'reason',m.reason,'category_id',c.category_id))
                 FROM memberships m JOIN collections c ON c.id=m.collection_id WHERE m.keyword_id=k.id) memberships_json
                FROM keywords k LEFT JOIN entity_context e ON e.keyword_id=k.id
                LEFT JOIN classification_results cr ON cr.keyword_id=k.id
                WHERE {where} ORDER BY {ordering}{pagination}""",
                page_parameters).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                try:
                    item["evidence"] = json.loads(item["evidence"])
                    item["memberships"] = json.loads(item.pop("memberships_json") or "[]")
                    item["entity_occupations"] = json.loads(item["entity_occupations"] or "[]")
                    item["entity_category_ids"] = json.loads(item["entity_category_ids"] or "[]")
                    item["entity_instance_types"] = json.loads(item["entity_instance_types"] or "[]")
                    item["classified_topic_category_ids"] = json.loads(item["classified_topic_category_ids"] or "[]")
                    item["classified_subcategory_ids"] = json.loads(item["classified_subcategory_ids"] or "[]")
                    item["classification_rule_evidence"] = json.loads(item["classification_rule_evidence"] or "[]")
                    item["classification_source_evidence"] = json.loads(item["classification_source_evidence"] or "{}")
                    from app.taxonomy.registry import resolve
                    item["classified_topic_categories"] = [
                        {"id": category_id, "name": resolve(category_id).display_name,
                         "scope": resolve(category_id).scope}
                        for category_id in item["classified_topic_category_ids"]
                    ]
                except (TypeError, ValueError):
                    item["memberships"] = []
                    item["entity_occupations"] = []
                    item["entity_category_ids"] = []
                    item["entity_instance_types"] = []
                    item["classified_topic_category_ids"] = []
                    item["classified_subcategory_ids"] = []
                    item["classification_rule_evidence"] = []
                    item["classification_source_evidence"] = {}
                if item.get("classification_status") == "INELIGIBLE":
                    item["eligible"] = 0
                    item["topic_eligible"] = False
                    if not item.get("rejection_reason"):
                        item["rejection_reason"] = "CLASSIFICATION_INELIGIBLE"
                else:
                    item["topic_eligible"] = bool(item.get("eligible"))
                if include_algorithm_results:
                    item["algorithm_results"] = [dict(result) for result in db.execute(
                        "SELECT algorithm_id,version,status,reason_code,value,metric,unit,result_json FROM algorithm_results WHERE keyword_id=? ORDER BY algorithm_id",
                        (item["id"],))]
                items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "next_offset": offset + limit if limit is not None and offset + limit < total else None}

    def workbench_summary(self):
        """Return unique-keyword status counts and the latest persisted AI job."""
        scoped = self.data_mode_scope == "LIVE"
        keyword_scope = " AND k.data_mode=?" if scoped else ""
        mode_parameters = ("LIVE",) if scoped else ()
        with self.connect() as db:
            topic_status = "COALESCE((SELECT classification_status FROM classification_results cr WHERE cr.keyword_id=k.id),'')<>'INELIGIBLE'"
            totals = db.execute("""SELECT count(*) AS stored,
                sum(k.eligible=1 AND """ + topic_status + ") AS eligible, "
                "sum((k.eligible=0 AND " + topic_status + ") OR EXISTS(SELECT 1 FROM keyword_decisions kd "
                "WHERE kd.keyword_id=k.id AND kd.decision='EXCLUDED')) AS excluded, "
                "sum(k.eligible=1 AND " + topic_status + " AND NOT EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id)) AS unclassified, "
                "sum(k.eligible=1 AND " + topic_status + " AND EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id)) AS assigned "
                "FROM keywords k WHERE 1=1" + keyword_scope, mode_parameters).fetchone()
            statuses = {row["classification_status"]: row["total"] for row in db.execute("""SELECT cr.classification_status,
                count(DISTINCT cr.keyword_id) AS total FROM classification_results cr
                JOIN keywords k ON k.id=cr.keyword_id WHERE (k.eligible=1 OR cr.classification_status='INELIGIBLE')
                AND NOT EXISTS(SELECT 1 FROM keyword_decisions kd WHERE kd.keyword_id=k.id AND kd.decision='EXCLUDED')"""
                + keyword_scope + " GROUP BY cr.classification_status",
                mode_parameters)}
            assignment = {row["origin"]: row["total"] for row in db.execute("""SELECT m.origin,
                count(DISTINCT m.keyword_id) AS total FROM memberships m JOIN keywords k ON k.id=m.keyword_id
                WHERE (m.origin='USER' OR """ + topic_status + ")" + keyword_scope + " GROUP BY m.origin", mode_parameters)}
            mixed_sql = ("SELECT count(*) FROM keywords k WHERE " + topic_status + " AND k.eligible=1 "
                "AND EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id AND m.origin='LOCAL_AI') "
                "AND EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id AND m.origin='USER')"
                + keyword_scope)
            mixed = db.execute(mixed_sql, mode_parameters).fetchone()[0]
            if scoped:
                ai_created = db.execute("""SELECT count(DISTINCT c.id) FROM collections c
                    LEFT JOIN memberships m ON m.collection_id=c.id
                    LEFT JOIN keywords k ON k.id=m.keyword_id AND k.data_mode='LIVE'
                    WHERE c.status='ACTIVE' AND c.created_by='AI' AND c.category_id IS NULL
                    AND k.id IS NOT NULL""").fetchone()[0]
                sources = [row[0] for row in db.execute(
                    "SELECT DISTINCT source FROM keywords WHERE data_mode='LIVE' ORDER BY source")]
                latest_job = db.execute("""SELECT f.fetch_id,f.status,f.stage,f.available,f.eligible,f.processed,
                    f.assigned,f.unassigned,f.created_collections,f.suggestions,f.skipped,f.error_code,f.updated_at
                    FROM fetch_jobs f JOIN dataset_snapshots d ON d.dataset_id=f.fetch_id
                    WHERE d.data_mode='LIVE' ORDER BY f.updated_at DESC LIMIT 1""").fetchone()
            else:
                ai_created = db.execute("SELECT count(*) FROM collections WHERE status='ACTIVE' AND created_by='AI' AND category_id IS NULL").fetchone()[0]
                sources = [row[0] for row in db.execute("SELECT DISTINCT source FROM keywords ORDER BY source")]
                latest_job = db.execute("SELECT fetch_id,status,stage,available,eligible,processed,assigned,unassigned,created_collections,suggestions,skipped,error_code,updated_at FROM fetch_jobs ORDER BY updated_at DESC LIMIT 1").fetchone()
            return {
                "stored_keywords": totals["stored"] or 0,
                "eligible_keywords": totals["eligible"] or 0,
                "excluded_keywords": totals["excluded"] or 0,
                "unclassified_keywords": totals["unclassified"] or 0,
                "assigned_keywords": totals["assigned"] or 0,
                "classified_keywords": statuses.get("CLASSIFIED", 0),
                "ambiguous_keywords": statuses.get("AMBIGUOUS", 0),
                "unknown_keywords": statuses.get("UNKNOWN", 0),
                "ineligible_topic_keywords": statuses.get("INELIGIBLE", 0),
                "ai_assigned_keywords": assignment.get("LOCAL_AI", 0),
                "manually_managed_keywords": assignment.get("USER", 0),
                "mixed_origin_keywords": mixed,
                "ai_created_collections": ai_created,
                "sources": sources,
                "latest_job": dict(latest_job) if latest_job else None,
            }

    def list_collections(self):
        with self.connect() as db:
            member_scope = " AND k.data_mode=?" if self.data_mode_scope else ""
            mode_parameters = (self.data_mode_scope,) if self.data_mode_scope else ()
            rows = db.execute("""SELECT c.*, count(k.id) AS membership_count
                FROM collections c LEFT JOIN memberships m ON m.collection_id=c.id
                LEFT JOIN keywords k ON k.id=m.keyword_id""" + member_scope + """
                WHERE c.status='ACTIVE'"""
                + (" AND (c.created_by<>'AI' OR c.category_id IS NOT NULL OR EXISTS ("
                   "SELECT 1 FROM memberships lm JOIN keywords lk ON lk.id=lm.keyword_id "
                   "WHERE lm.collection_id=c.id AND lk.data_mode=?))" if self.data_mode_scope else "")
                + " GROUP BY c.id ORDER BY c.updated_at DESC",
                (*mode_parameters, self.data_mode_scope) if self.data_mode_scope else ()).fetchall()
            items = [self._collection(row) for row in rows]
            by_id = {item["id"]: item for item in items}
            for item in items:
                item["direct_membership_count"] = item["membership_count"]
                item["subtree_unique_keyword_count"] = 0
            membership_ids = {item["id"]: set() for item in items}
            for row in db.execute("SELECT m.collection_id,m.keyword_id FROM memberships m "
                                  "JOIN keywords k ON k.id=m.keyword_id "
                                  + ("WHERE k.data_mode=?" if self.data_mode_scope else ""), mode_parameters):
                if row["collection_id"] in membership_ids:
                    membership_ids[row["collection_id"]].add(row["keyword_id"])
            for item in sorted(items, key=lambda row: self._collection_depth(row, by_id), reverse=True):
                item["subtree_unique_keyword_count"] = len(membership_ids[item["id"]])
                if item["parent_id"] in membership_ids:
                    membership_ids[item["parent_id"]].update(membership_ids[item["id"]])
            for item in items:
                item["membership_count"] = item["subtree_unique_keyword_count"]
            return items

    @staticmethod
    def _collection_depth(item, by_id):
        depth, parent_id = 0, item.get("parent_id")
        seen = {item["id"]}
        while parent_id in by_id and parent_id not in seen:
            seen.add(parent_id)
            depth += 1
            parent_id = by_id[parent_id].get("parent_id")
        return depth

    @staticmethod
    def _collection(row):
        item = dict(row)
        for key in ("examples", "exclusions"):
            item[key] = json.loads(item[key])
        item["auto_match"] = bool(item["auto_match"])
        return item

    def create(self, data, *, created_by="USER", reason=""):
        identifier, stamp = str(uuid.uuid4()), now()
        with self.connect() as db:
            name_key = fingerprint(data["name"])
            if any(fingerprint(row[0]) == name_key for row in db.execute(
                    "SELECT name FROM collections WHERE status='ACTIVE'")):
                raise ValueError("COLLECTION_ALREADY_EXISTS")
            db.execute("""INSERT INTO collections(id,name,description,category_id,parent_id,examples,exclusions,created_by,
                auto_match,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, data["name"], data.get("description", ""), data.get("category_id"), data.get("parent_id"),
                 json.dumps(data.get("examples", []), ensure_ascii=False),
                 json.dumps(data.get("exclusions", []), ensure_ascii=False), created_by,
                 int(data.get("auto_match", True)), reason, stamp, stamp))
        return identifier

    def ensure_category_collection(self, category, *, parent_id=None):
        """Create one stable system topic node per taxonomy ID, unless the owner suppressed it."""
        identifier = "topic-" + fingerprint(category.id)[:32]
        description = f"Keywords and entities about {category.display_name.casefold()}."
        with self.connect() as db:
            row = db.execute("SELECT id FROM collections WHERE category_id=? AND status='ACTIVE'",
                             (category.id,)).fetchone()
            if row:
                return row["id"]
            if db.execute("SELECT 1 FROM suppression WHERE fingerprint=? AND kind='DELETED_COLLECTION'",
                          (fingerprint(category.display_name),)).fetchone():
                return None
            # Reuse a same-named user Collection so taxonomy filters do not fork its identity.
            existing = db.execute("""SELECT id FROM collections WHERE status='ACTIVE'
                AND name=? COLLATE NOCASE ORDER BY created_at LIMIT 1""", (category.display_name,)).fetchone()
            if existing:
                return existing["id"]
            stamp = now()
            db.execute("""INSERT OR IGNORE INTO collections(id,name,description,category_id,parent_id,
                examples,exclusions,created_by,auto_match,reason,created_at,updated_at)
                VALUES(?,?,?,?,?,'[]','[]','AI',1,'taxonomy-backed entity classification',?,?)""",
                (identifier, category.display_name, description, category.id, parent_id, stamp, stamp))
            row = db.execute("SELECT id FROM collections WHERE category_id=? AND status='ACTIVE'",
                             (category.id,)).fetchone()
            return row["id"] if row else None

    def update(self, identifier, data):
        allowed = {"name", "description", "examples", "exclusions", "auto_match"}
        values = {key: value for key, value in data.items() if key in allowed}
        if not values:
            raise ValueError("empty update")
        with self.connect() as db:
            row = db.execute("SELECT version FROM collections WHERE id=? AND status='ACTIVE'", (identifier,)).fetchone()
            if row is None:
                return False
            assignments, params = [], []
            for key, value in values.items():
                if key in {"examples", "exclusions"}:
                    value = json.dumps(value, ensure_ascii=False)
                if key == "auto_match":
                    value = int(value)
                assignments.append(f"{key}=?")
                params.append(value)
            params.extend([now(), identifier])
            db.execute(f"UPDATE collections SET {','.join(assignments)},version=version+1,updated_at=? WHERE id=?", params)
            # Version zero entries are keyword vectors shared across Collections; retain them
            # so a metadata edit does not force unchanged keywords through the model again.
            db.execute("DELETE FROM cache WHERE collection_version > 0 AND collection_version < ?",
                       (row["version"] + 1,))
            return True

    def delete(self, identifier):
        with self.connect() as db:
            row = db.execute("SELECT id,name FROM collections WHERE id=?", (identifier,)).fetchone()
            if row is None:
                return False
            descendants = db.execute("""WITH RECURSIVE tree(id,name) AS (
                SELECT id,name FROM collections WHERE id=? UNION ALL
                SELECT c.id,c.name FROM collections c JOIN tree t ON c.parent_id=t.id
            ) SELECT name FROM tree""", (identifier,)).fetchall()
            stamp = now()
            db.executemany("INSERT OR REPLACE INTO suppression(fingerprint,kind,created_at,label) VALUES(?,?,?,?)",
                [(fingerprint(item["name"]), "DELETED_COLLECTION", stamp, item["name"]) for item in descendants])
            db.execute("DELETE FROM collections WHERE id=?", (identifier,))
            return True

    def merge(self, source_id, target_id):
        """Merge a user/AI Collection into another, pin moved links, and suppress its old name."""
        if source_id == target_id:
            raise ValueError("COLLECTION_MERGE_SAME_TARGET")
        with self.connect() as db:
            rows = db.execute("""WITH RECURSIVE source_tree(id,name,category_id) AS (
                SELECT id,name,category_id FROM collections WHERE id=? AND status='ACTIVE'
                UNION ALL SELECT c.id,c.name,c.category_id FROM collections c
                JOIN source_tree parent ON c.parent_id=parent.id WHERE c.status='ACTIVE'
            ) SELECT id,name,category_id FROM source_tree
            UNION SELECT id,name,category_id FROM collections WHERE id=? AND status='ACTIVE'""",
                (source_id, target_id)).fetchall()
            by_id = {row["id"]: row for row in rows}
            if source_id not in by_id or target_id not in by_id:
                raise ValueError("COLLECTION_NOT_FOUND")
            target = by_id[target_id]
            source_tree = db.execute("""WITH RECURSIVE tree(id,name,category_id) AS (
                SELECT id,name,category_id FROM collections WHERE id=? UNION ALL
                SELECT c.id,c.name,c.category_id FROM collections c JOIN tree p ON c.parent_id=p.id
            ) SELECT id,name,category_id FROM tree""", (source_id,)).fetchall()
            if any(item["id"] == target_id for item in source_tree):
                raise ValueError("COLLECTION_MERGE_TARGET_DESCENDANT")
            if target["category_id"] or any(item["category_id"] for item in source_tree):
                raise ValueError("TAXONOMY_COLLECTION_MERGE_FORBIDDEN")
            source_ids = [item["id"] for item in source_tree]
            placeholders = ",".join("?" for _ in source_ids)
            memberships = db.execute(f"SELECT * FROM memberships WHERE collection_id IN ({placeholders})", source_ids).fetchall()
            transferred = skipped = 0
            stamp = now()
            for membership in memberships:
                decision = db.execute("SELECT action FROM feedback WHERE collection_id=? AND keyword_id=?",
                                      (target_id, membership["keyword_id"])).fetchone()
                if decision and decision["action"] == "REMOVE":
                    skipped += 1
                    continue
                existing = db.execute("SELECT pinned FROM memberships WHERE collection_id=? AND keyword_id=?",
                                      (target_id, membership["keyword_id"])).fetchone()
                if existing and existing["pinned"]:
                    skipped += 1
                    continue
                if existing and membership["origin"] == "LOCAL_AI":
                    skipped += 1
                    continue
                origin = membership["origin"]
                pinned = int(bool(membership["pinned"]) or origin == "USER")
                reason = f"{membership['reason']} (merged from {by_id[membership['collection_id']]['name']})".strip()
                db.execute("""INSERT OR REPLACE INTO memberships
                    (collection_id,keyword_id,origin,pinned,score,reason,created_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (target_id, membership["keyword_id"], origin, pinned, membership["score"], reason, stamp))
                if origin == "USER":
                    db.execute("INSERT OR REPLACE INTO feedback VALUES(?,?, 'ADD',?)",
                               (target_id, membership["keyword_id"], stamp))
                transferred += 1
            db.executemany("INSERT OR REPLACE INTO suppression(fingerprint,kind,created_at,label) VALUES(?,?,?,?)",
                [(fingerprint(item["name"]), "DELETED_COLLECTION", stamp, item["name"]) for item in source_tree])
            db.execute("DELETE FROM collections WHERE id=?", (source_id,))
            db.execute("UPDATE collections SET version=version+1,updated_at=? WHERE id=?", (stamp, target_id))
            return {"merged": True, "transferred": transferred, "skipped": skipped, "target_id": target_id}

    def archive_unpinned_ai_memberships(self, run_id: str, reason: str):
        """Archive automatic memberships before reconciliation so they can be restored exactly."""
        stamp = now()
        with self.connect() as db:
            db.execute("INSERT INTO membership_reconciliation_runs VALUES(?,?,?,'APPLYING')",
                       (run_id, stamp, reason))
            scope = " AND k.data_mode=?" if self.data_mode_scope else ""
            mode_parameters = (self.data_mode_scope,) if self.data_mode_scope else ()
            db.execute("""INSERT INTO membership_reconciliation_items
                SELECT ?,collection_id,keyword_id,origin,pinned,score,reason,created_at
                FROM memberships m JOIN keywords k ON k.id=m.keyword_id
                WHERE origin='LOCAL_AI' AND pinned=0""" + scope,
                (run_id, *mode_parameters))
            archived = db.execute("SELECT changes()").fetchone()[0]
            db.execute("DELETE FROM memberships WHERE origin='LOCAL_AI' AND pinned=0 "
                + ("AND keyword_id IN (SELECT id FROM keywords WHERE data_mode=?)" if self.data_mode_scope else ""),
                mode_parameters)
            db.execute("UPDATE membership_reconciliation_runs SET status='APPLIED' WHERE id=?", (run_id,))
            return archived

    def rollback_membership_reconciliation(self, run_id: str):
        with self.connect() as db:
            run = db.execute("SELECT status,created_at FROM membership_reconciliation_runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["status"] != "APPLIED":
                return False
            later = db.execute("SELECT 1 FROM membership_reconciliation_runs WHERE status='APPLIED' AND created_at>? LIMIT 1",
                               (run["created_at"],)).fetchone()
            if later:
                return False
            # Remove automatic Collections created during this run if the user has not
            # adopted them with a manual membership. Avoid creating a deletion suppression
            # because this operation is a migration rollback, not a user delete action.
            created = db.execute("SELECT id FROM collections WHERE created_by='AI' AND reason LIKE ?",
                                 (f"%{run_id}%",)).fetchall()
            for item in created:
                user_membership = db.execute("SELECT 1 FROM memberships WHERE collection_id=? AND origin='USER' LIMIT 1",
                                             (item["id"],)).fetchone()
                feedback = db.execute("SELECT 1 FROM feedback WHERE collection_id=? LIMIT 1", (item["id"],)).fetchone()
                if not user_membership and not feedback:
                    db.execute("DELETE FROM collections WHERE id=?", (item["id"],))
            live_scope = (" AND keyword_id IN (SELECT id FROM keywords WHERE data_mode=?)"
                          if self.data_mode_scope else "")
            scoped_parameters = (self.data_mode_scope,) if self.data_mode_scope else ()
            db.execute("""DELETE FROM memberships
                WHERE origin='LOCAL_AI' AND pinned=0 AND (
                    created_at>=? OR EXISTS (
                        SELECT 1 FROM membership_reconciliation_items i
                        WHERE i.run_id=? AND i.collection_id=memberships.collection_id
                          AND i.keyword_id=memberships.keyword_id
                    )
                )""" + live_scope, (run["created_at"], run_id, *scoped_parameters))
            db.execute("""INSERT OR IGNORE INTO memberships(collection_id,keyword_id,origin,pinned,score,reason,created_at)
                SELECT i.collection_id,i.keyword_id,i.origin,i.pinned,i.score,i.reason,i.created_at
                FROM membership_reconciliation_items i
                JOIN collections c ON c.id=i.collection_id
                JOIN keywords k ON k.id=i.keyword_id WHERE i.run_id=?"""
                + (" AND k.data_mode=?" if self.data_mode_scope else ""),
                (run_id, *scoped_parameters))
            db.execute("UPDATE membership_reconciliation_runs SET status='ROLLED_BACK' WHERE id=?", (run_id,))
            return True

    def keywords_for(self, collection_id):
        scope = " AND k.data_mode=?" if self.data_mode_scope else ""
        parameters = (collection_id, self.data_mode_scope) if self.data_mode_scope else (collection_id,)
        with self.connect() as db:
            rows = db.execute("""SELECT k.*,m.origin,m.pinned,m.score,m.reason,
                e.entity_id,e.entity_type,e.description AS entity_description,
                e.occupations AS entity_occupations,e.category_ids AS entity_category_ids,
                e.source_url AS entity_source_url,e.method AS entity_classification_method,
                cr.classification_status
                FROM memberships m JOIN keywords k ON k.id=m.keyword_id
                LEFT JOIN entity_context e ON e.keyword_id=k.id
                LEFT JOIN classification_results cr ON cr.keyword_id=k.id
                WHERE m.collection_id IN (
                    WITH RECURSIVE topic_tree(id) AS (
                        SELECT id FROM collections WHERE id=?
                        UNION ALL SELECT child.id FROM collections child JOIN topic_tree parent ON child.parent_id=parent.id
                    ) SELECT id FROM topic_tree
                )""" + scope + " ORDER BY k.keyword COLLATE NOCASE", parameters).fetchall()
            items = [dict(row) for row in rows]
            for item in items:
                if item.get("classification_status") == "INELIGIBLE":
                    item["eligible"] = 0
                    item["topic_eligible"] = False
                else:
                    item["topic_eligible"] = bool(item.get("eligible"))
            return items

    def feedback_action(self, collection_id, record, action, *, pinned=True):
        if self.data_mode_scope and record.get("data_mode") != self.data_mode_scope:
            raise ValueError("KEYWORD_DATA_MODE_MISMATCH")
        with self.connect() as db:
            if self.data_mode_scope:
                existing = db.execute("SELECT data_mode FROM keywords WHERE id=?", (record["id"],)).fetchone()
                if existing and existing["data_mode"] != self.data_mode_scope:
                    raise ValueError("KEYWORD_DATA_MODE_MISMATCH")
            db.execute("""INSERT INTO keywords(id,keyword,normalized,source,project,observed_at,data_mode,evidence,context)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET evidence=excluded.evidence,
                observed_at=excluded.observed_at WHERE excluded.keyword<>''""", tuple(record[k] for k in (
                    "id", "keyword", "normalized", "source", "project", "observed_at", "data_mode", "evidence", "context")))
            db.execute("INSERT OR REPLACE INTO feedback VALUES(?,?,?,?)",
                       (collection_id, record["id"], action, now()))
            if action == "REMOVE":
                db.execute("DELETE FROM memberships WHERE collection_id=? AND keyword_id=?", (collection_id, record["id"]))
            elif action == "ADD":
                db.execute("""INSERT OR REPLACE INTO memberships VALUES(?,?, 'USER', ?,NULL,'Manually added',?)""",
                           (collection_id, record["id"], int(pinned), now()))
            db.execute("UPDATE collections SET version=version+1,updated_at=? WHERE id=?", (now(), collection_id))

    def persist_cleaned_keywords(self, records, *, rejected=(), bing_updates=()):
        """Persist source-cleaned keyword records and verified candidate updates."""
        records = [dict(record) for record in records]
        if (self.data_mode_scope
                and any(record.get("data_mode") != self.data_mode_scope for record in records)):
            raise ValueError("cleaned keyword record is outside the store's data mode scope")
        skipped_bing = 0
        from app.sources.catalog import GOOGLE_SOURCE_ID

        with self.connect() as db:
            if self.data_mode_scope and records:
                identifiers = list(dict.fromkeys(record["id"] for record in records))
                existing_modes = {}
                for offset in range(0, len(identifiers), 500):
                    batch = identifiers[offset:offset + 500]
                    placeholders = ",".join("?" for _ in batch)
                    rows = db.execute(
                        f"SELECT id,data_mode FROM keywords WHERE id IN ({placeholders})", batch
                    ).fetchall()
                    existing_modes.update({row["id"]: row["data_mode"] for row in rows})
                for record in records:
                    if (record["id"] in existing_modes
                            and existing_modes[record["id"]] != self.data_mode_scope):
                        record["id"] = hashlib.sha256(
                            f"{self.data_mode_scope}:{record['id']}".encode("utf-8")
                        ).hexdigest()

            if rejected:
                statement = "UPDATE keywords SET eligible=0,rejection_reason=? WHERE id=? "
                if self.data_mode_scope:
                    statement += "AND data_mode=?"
                rows = [(reason, identifier, self.data_mode_scope) if self.data_mode_scope
                        else (reason, identifier) for identifier, reason in rejected]
                db.executemany(statement, rows)

            db.executemany("""INSERT INTO keywords(id,keyword,normalized,source,project,observed_at,
                data_mode,evidence,context,eligible,rejection_reason) VALUES(?,?,?,?,?,?,?,?,?,1,NULL)
                ON CONFLICT(id) DO UPDATE SET keyword=excluded.keyword,observed_at=excluded.observed_at,
                evidence=excluded.evidence,
                eligible=COALESCE((SELECT CASE decision WHEN 'EXCLUDED' THEN 0 ELSE 1 END
                    FROM keyword_decisions WHERE keyword_id=excluded.id),1),
                rejection_reason=CASE WHEN (SELECT decision FROM keyword_decisions
                    WHERE keyword_id=excluded.id)='EXCLUDED' THEN 'USER_EXCLUDED' ELSE NULL END
                WHERE keywords.keyword IS NOT excluded.keyword
                   OR keywords.observed_at IS NOT excluded.observed_at
                   OR keywords.evidence IS NOT excluded.evidence
                   OR keywords.eligible IS NOT COALESCE((SELECT CASE decision
                        WHEN 'EXCLUDED' THEN 0 ELSE 1 END FROM keyword_decisions
                        WHERE keyword_id=excluded.id),1)
                   OR keywords.rejection_reason IS NOT CASE WHEN (SELECT decision
                        FROM keyword_decisions WHERE keyword_id=excluded.id)='EXCLUDED'
                        THEN 'USER_EXCLUDED' ELSE NULL END""",
                [tuple(record[key] for key in ("id", "keyword", "normalized", "source", "project",
                    "observed_at", "data_mode", "evidence", "context")) for record in records])

            for update in bing_updates:
                existing = db.execute(
                    "SELECT observed_at,evidence FROM keywords WHERE id=? AND source=? "
                    "AND project LIKE 'google_trends:%' AND data_mode='LIVE' AND eligible=1",
                    (update["candidate_id"], GOOGLE_SOURCE_ID),
                ).fetchone()
                if existing is None:
                    skipped_bing += 1
                    continue
                try:
                    observed = datetime.fromisoformat(
                        existing["observed_at"].replace("Z", "+00:00")
                    )
                    evidence = json.loads(existing["evidence"])
                except (TypeError, ValueError, AttributeError):
                    skipped_bing += 1
                    continue
                if (observed.tzinfo is None or observed.utcoffset() is None
                        or observed.astimezone(timezone.utc).isoformat() != update["observed_at"]
                        or not isinstance(evidence, dict)
                        or evidence.get("canonical_keyword_identity") != update["identity"]):
                    skipped_bing += 1
                    continue
                history = evidence.get("bing_verification_history", [])
                if not isinstance(history, list) or len(history) > 20:
                    skipped_bing += 1
                    continue
                verification = update["verification"]
                fetched_at = verification["fetched_at"]
                query_fingerprint = verification.get("query_fingerprint")
                duplicate = any(
                    isinstance(item, dict)
                    and item.get("fetched_at") == fetched_at
                    and item.get("query_fingerprint") == query_fingerprint
                    for item in history
                )
                if not duplicate:
                    history = [*history, verification][-20:]
                evidence["bing_verification_history"] = history
                evidence["bing_verification"] = verification
                db.execute("UPDATE keywords SET evidence=? WHERE id=? AND source=? AND data_mode='LIVE'",
                           (json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                                       allow_nan=False, separators=(",", ":")),
                            update["candidate_id"], GOOGLE_SOURCE_ID))

        return {"records": records, "skipped_bing_updates": skipped_bing}

    def begin_fetch_processing(self, fetch_id, sources, *, cached_context_only=False,
                               resume_model_unavailable=False):
        from app.collections.source_digest import reusable_collection_input

        retry_missing_snapshot = False
        retry_existing_snapshot = False
        replay_available = replay_skipped = 0
        with self.connect() as db:
            if resume_model_unavailable:
                db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM fetch_jobs WHERE fetch_id=?", (fetch_id,)).fetchone()
            if (resume_model_unavailable and prior
                    and prior["status"] == "PROCESSING"):
                return {"action": "RETURN", "result": dict(prior)}
            if prior and self.data_mode_scope:
                snapshot = db.execute(
                    "SELECT data_mode,processing_status,received_record_count,rejected_record_count "
                    "FROM dataset_snapshots WHERE dataset_id=?", (fetch_id,),
                ).fetchone()
                if snapshot and snapshot["data_mode"] != self.data_mode_scope:
                    return {"action": "RETURN", "result": {
                        "fetch_id": fetch_id, "status": "FAILED", "error_code": "FETCH_ID_MODE_CONFLICT",
                    }}
                if (snapshot and prior["status"] in {"COMPLETED", "MODEL_UNAVAILABLE"}
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
                return {"action": "RETURN", "result": dict(prior)}
            if not prior and cached_context_only and self.data_mode_scope == "LIVE":
                latest = db.execute("""SELECT dataset_id,source_manifest,processing_status,
                    received_record_count,rejected_record_count,eligible_keyword_count,
                    unassigned_keyword_count FROM dataset_snapshots WHERE data_mode='LIVE'
                    ORDER BY created_at DESC,dataset_id DESC LIMIT 1""").fetchone()
                if latest is not None:
                    try:
                        previous_manifest = json.loads(latest["source_manifest"])
                    except (TypeError, ValueError):
                        previous_manifest = None
                    if reusable_collection_input(
                            sources, previous_manifest,
                            processing_status=latest["processing_status"]):
                        return {"action": "RETURN", "result": {
                            "fetch_id": fetch_id, "status": "REUSED", "dataset_reused": True,
                            "dataset_id": latest["dataset_id"],
                            "available": latest["received_record_count"],
                            "skipped": latest["rejected_record_count"],
                            "eligible": latest["eligible_keyword_count"],
                            "unassigned": latest["unassigned_keyword_count"],
                        }}
            db.execute("INSERT OR REPLACE INTO fetch_jobs(fetch_id,status,stage,updated_at) "
                       "VALUES(?,'PROCESSING','CLEANING',?)", (fetch_id, now()))
        return {"action": "PROCESS", "retry_missing_snapshot": retry_missing_snapshot,
                "retry_existing_snapshot": retry_existing_snapshot,
                "replay_available": replay_available, "replay_skipped": replay_skipped}

    def load_keywords_for_fetch(self, source_ids=None):
        with self.connect() as db:
            mode_filter = " AND data_mode=?" if self.data_mode_scope else ""
            mode_parameters = (self.data_mode_scope,) if self.data_mode_scope else ()
            source_filter, source_parameters = "", ()
            if source_ids is not None:
                identifiers = tuple(source_ids)
                if identifiers:
                    source_filter = " AND id IN (" + ",".join("?" for _ in identifiers) + ")"
                    source_parameters = identifiers
                else:
                    source_filter = " AND 1=0"
            records = [dict(row) for row in db.execute(
                "SELECT * FROM keywords WHERE eligible=1" + mode_filter + source_filter
                + " ORDER BY observed_at DESC,keyword COLLATE NOCASE",
                (*mode_parameters, *source_parameters)).fetchall()]
            eligible = db.execute("SELECT count(*) FROM keywords WHERE eligible=1" + mode_filter,
                                  mode_parameters).fetchone()[0]
            excluded = [dict(row) for row in db.execute(
                "SELECT * FROM keywords WHERE eligible=0" + mode_filter + source_filter
                + " ORDER BY observed_at DESC,keyword COLLATE NOCASE",
                (*mode_parameters, *source_parameters)).fetchall()]
        return {"records": records, "excluded_records": excluded, "eligible": eligible}

    def load_keywords_for_collection_processing(self, *, include_invalid_google=False):
        query = "SELECT * FROM keywords"
        clauses, parameters = [], []
        if include_invalid_google:
            clauses.append("(eligible=1 OR (source='google_web_serp' "
                            "AND rejection_reason='INVALID_GOOGLE_TRENDS_RECORD'))")
        if self.data_mode_scope:
            clauses.append("data_mode=?")
            parameters.append(self.data_mode_scope)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self.connect() as db:
            rows = db.execute(query + " ORDER BY observed_at DESC", parameters).fetchall()
        return [dict(row) for row in rows]

    def persist_classifications(self, classification_records, *, delete_unpinned_ai_for_ineligible=False):
        result_columns = ("entity_type", "entity_id", "entity_description", "topic_category_ids",
                          "subcategory_ids", "classification_status", "confidence_level",
                          "commercial_relevance", "classification_method", "classification_version",
                          "taxonomy_version", "rule_evidence", "source_evidence", "review_status",
                          "classified_at", "classification_reason")
        with self.connect() as db:
            db.executemany("""INSERT INTO classification_results(keyword_id,entity_type,entity_id,
                entity_description,topic_category_ids,subcategory_ids,classification_status,confidence_level,
                commercial_relevance,classification_method,classification_version,taxonomy_version,rule_evidence,source_evidence,
                review_status,classified_at,classification_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(keyword_id) DO UPDATE SET entity_type=excluded.entity_type,entity_id=excluded.entity_id,
                entity_description=excluded.entity_description,topic_category_ids=excluded.topic_category_ids,
                subcategory_ids=excluded.subcategory_ids,classification_status=excluded.classification_status,
                confidence_level=excluded.confidence_level,commercial_relevance=excluded.commercial_relevance,
                classification_method=excluded.classification_method,
                classification_version=excluded.classification_version,taxonomy_version=excluded.taxonomy_version,
                rule_evidence=excluded.rule_evidence,source_evidence=excluded.source_evidence,
                review_status=excluded.review_status,classified_at=excluded.classified_at,
                classification_reason=excluded.classification_reason""",
                [(item["id"], *(json.dumps(item["classification_result"][key], ensure_ascii=False)
                    if key in {"topic_category_ids", "subcategory_ids", "rule_evidence", "source_evidence"}
                    else item["classification_result"][key] for key in result_columns))
                 for item in classification_records])
            decisions = {row["keyword_id"]: row["decision"] for row in
                         db.execute("SELECT keyword_id,decision FROM keyword_decisions")}
            for item in classification_records:
                decision = decisions.get(item["id"])
                result = item["classification_result"]
                if decision == "EXCLUDED":
                    item["eligible"] = 0
                    db.execute("UPDATE keywords SET eligible=0,rejection_reason='USER_EXCLUDED' WHERE id=?",
                               (item["id"],))
                elif result["classification_status"] == "INELIGIBLE":
                    item["eligible"] = 0
                    item["entity_category_ids"] = ()
                    db.execute("UPDATE keywords SET eligible=0,rejection_reason='CLASSIFICATION_INELIGIBLE' "
                               "WHERE id=?", (item["id"],))
                    if delete_unpinned_ai_for_ineligible:
                        db.execute("DELETE FROM memberships WHERE keyword_id=? AND origin='LOCAL_AI' AND pinned=0",
                                   (item["id"],))
                elif item.get("rejection_reason") == "CLASSIFICATION_INELIGIBLE":
                    item["eligible"] = 1
                    item["entity_category_ids"] = tuple(result["topic_category_ids"])
                    db.execute("UPDATE keywords SET eligible=1,rejection_reason=NULL WHERE id=?",
                               (item["id"],))
            eligible = db.execute("SELECT count(*) FROM keywords WHERE eligible=1 "
                + ("AND data_mode=?" if self.data_mode_scope else ""),
                (self.data_mode_scope,) if self.data_mode_scope else ()).fetchone()[0]
        return eligible

    def load_cached_keywords_for_reclassification(self, identifiers):
        identifiers = tuple(dict.fromkeys(identifiers))
        placeholders = ",".join("?" for _ in identifiers)
        with self.connect() as db:
            rows = db.execute(f"""SELECT k.*,e.entity_id,e.entity_type,e.description AS entity_description,
                e.occupations AS entity_occupations,e.category_ids AS entity_category_ids,
                e.instance_types AS entity_instance_types,e.source_url AS entity_source_url,
                e.method AS entity_context_method,e.status AS entity_context_status,
                e.page_namespace,e.is_disambiguation,e.is_main_page,e.is_main_page_known,
                e.redirected_from,e.page_language FROM keywords k
                LEFT JOIN entity_context e ON e.keyword_id=k.id WHERE k.id IN ({placeholders})"""
                + (" AND k.data_mode=?" if self.data_mode_scope else ""),
                (*identifiers, self.data_mode_scope) if self.data_mode_scope else identifiers).fetchall()
        found = {row["id"] for row in rows}
        if found != set(identifiers):
            raise ValueError("CACHED_KEYWORD_NOT_FOUND")
        return [dict(row) for row in rows]

    def get_embedding_cache(self, keys):
        requested = {key: (model_version, collection_version, text_hash)
                     for key, model_version, collection_version, text_hash in keys}
        found = {}
        identifiers = list(requested)
        with self.connect() as db:
            for offset in range(0, len(identifiers), 500):
                batch = identifiers[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = db.execute(
                    f"SELECT fingerprint,model_version,collection_version,text_hash,vector,dimension "
                    f"FROM cache WHERE fingerprint IN ({placeholders})", batch,
                ).fetchall()
                for row in rows:
                    if (row["model_version"], row["collection_version"], row["text_hash"]) == requested[row["fingerprint"]]:
                        found[row["fingerprint"]] = {
                            "vector": row["vector"], "dimension": row["dimension"],
                        }
        return found

    def upsert_embedding_cache(self, entries):
        entries = list(entries)
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?,?)", entries)

    def unassigned_keyword_count(self):
        with self.connect() as db:
            return db.execute("""SELECT count(*) FROM keywords k WHERE k.eligible=1
                AND COALESCE((SELECT classification_status FROM classification_results cr
                    WHERE cr.keyword_id=k.id),'')<>'INELIGIBLE'
                AND NOT EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id)""").fetchone()[0]

    def get_setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))

    def collection_exists(self, identifier, *, active=True):
        condition = " AND status='ACTIVE'" if active else ""
        with self.connect() as db:
            return db.execute("SELECT 1 FROM collections WHERE id=?" + condition,
                              (identifier,)).fetchone() is not None

    def has_membership(self, collection_id, keyword_id):
        scope = " AND k.data_mode=?" if self.data_mode_scope else ""
        parameters = ((collection_id, keyword_id, self.data_mode_scope) if self.data_mode_scope
                      else (collection_id, keyword_id))
        with self.connect() as db:
            return db.execute("""SELECT 1 FROM memberships m JOIN keywords k ON k.id=m.keyword_id
                WHERE m.collection_id=? AND m.keyword_id=?""" + scope, parameters).fetchone() is not None

    def collection_keyword_record(self, identifier):
        scope = "AND data_mode=?" if self.data_mode_scope else ""
        parameters = ((identifier, self.data_mode_scope) if self.data_mode_scope else (identifier,))
        with self.connect() as db:
            row = db.execute("SELECT * FROM keywords WHERE id=? " + scope, parameters).fetchone()
        return row

    def classification_for(self, identifier):
        scope = "AND k.data_mode=?" if self.data_mode_scope else ""
        parameters = ((identifier, self.data_mode_scope) if self.data_mode_scope else (identifier,))
        with self.connect() as db:
            return db.execute("""SELECT cr.* FROM classification_results cr JOIN keywords k
                ON k.id=cr.keyword_id WHERE cr.keyword_id=? """ + scope, parameters).fetchone()

    def keyword_detail_metadata(self, identifier):
        scope = "AND data_mode=?" if self.data_mode_scope else ""
        parameters = ((identifier, self.data_mode_scope) if self.data_mode_scope else (identifier,))
        with self.connect() as db:
            keyword = db.execute("SELECT 1 FROM keywords WHERE id=? " + scope, parameters).fetchone()
            if keyword is None:
                return None
            decision = db.execute("SELECT decision,reason,updated_at FROM keyword_decisions WHERE keyword_id=?",
                                  (identifier,)).fetchone()
            feedback = db.execute("""SELECT f.collection_id,c.name,f.action,f.created_at
                FROM feedback f LEFT JOIN collections c ON c.id=f.collection_id
                WHERE f.keyword_id=? ORDER BY f.created_at DESC""", (identifier,)).fetchall()
        return {"decision": decision, "feedback": feedback}

    def collection_job(self, fetch_id):
        with self.connect() as db:
            if self.data_mode_scope:
                row = db.execute("""SELECT f.* FROM fetch_jobs f JOIN dataset_snapshots d
                    ON d.dataset_id=f.fetch_id AND d.data_mode=? WHERE f.fetch_id=?""",
                    (self.data_mode_scope, fetch_id)).fetchone()
            else:
                row = db.execute("SELECT * FROM fetch_jobs WHERE fetch_id=?", (fetch_id,)).fetchone()
            created = db.execute("SELECT name FROM collections WHERE created_by='AI' AND reason LIKE ? "
                                 "ORDER BY created_at", (f"%{fetch_id}%",)).fetchall()
        return row, created

    def add_local_ai_memberships(self, collection_id, keyword_ids, reason):
        with self.connect() as db:
            db.executemany("""INSERT OR IGNORE INTO memberships
                (collection_id,keyword_id,origin,pinned,score,reason,created_at)
                VALUES(?,?,'LOCAL_AI',0,NULL,?,?)""",
                [(collection_id, keyword_id, reason, now()) for keyword_id in keyword_ids])

    def add_taxonomy_memberships_if_allowed(self, assignments):
        with self.connect() as db:
            for collection_id, keyword_id, reason in assignments:
                collection = db.execute("SELECT auto_match,status FROM collections WHERE id=?",
                                        (collection_id,)).fetchone()
                if not collection or collection["status"] != "ACTIVE" or not collection["auto_match"]:
                    continue
                removed = db.execute("SELECT 1 FROM feedback WHERE collection_id=? AND keyword_id=? "
                                     "AND action='REMOVE'", (collection_id, keyword_id)).fetchone()
                if removed:
                    continue
                db.execute("""INSERT OR IGNORE INTO memberships
                    (collection_id,keyword_id,origin,pinned,score,reason,created_at)
                    VALUES(?,?,'LOCAL_AI',0,NULL,?,?)""",
                    (collection_id, keyword_id, reason, now()))

    def membership_guardrails(self, keyword_ids, category_collection_ids):
        keyword_ids = list(dict.fromkeys(keyword_ids))
        category_collection_ids = list(dict.fromkeys(category_collection_ids))
        removed_pairs, removed_keyword_ids, pinned_pairs = set(), set(), set()
        active_category_ids = set()
        with self.connect() as db:
            for offset in range(0, len(keyword_ids), 500):
                batch = keyword_ids[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                scope = " AND k.data_mode=?" if self.data_mode_scope else ""
                parameters = (*batch, self.data_mode_scope) if self.data_mode_scope else tuple(batch)
                removed = db.execute("SELECT f.collection_id,f.keyword_id FROM feedback f "
                    "JOIN keywords k ON k.id=f.keyword_id WHERE f.action='REMOVE' "
                    f"AND f.keyword_id IN ({placeholders})" + scope, parameters).fetchall()
                removed_pairs.update((row["collection_id"], row["keyword_id"]) for row in removed)
                removed_keyword_ids.update(row["keyword_id"] for row in removed)
                pinned = db.execute("SELECT m.collection_id,m.keyword_id FROM memberships m "
                    "JOIN keywords k ON k.id=m.keyword_id WHERE m.pinned=1 "
                    f"AND m.keyword_id IN ({placeholders})" + scope, parameters).fetchall()
                pinned_pairs.update((row["collection_id"], row["keyword_id"]) for row in pinned)
            for offset in range(0, len(category_collection_ids), 500):
                batch = category_collection_ids[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                active_category_ids.update(row["id"] for row in db.execute(
                    "SELECT id FROM collections WHERE status='ACTIVE' AND auto_match=1 "
                    f"AND id IN ({placeholders})", batch))
        return {"removed_pairs": removed_pairs, "removed_keyword_ids": removed_keyword_ids,
                "pinned_pairs": pinned_pairs, "active_category_ids": active_category_ids}

    def replace_local_ai_memberships(self, assignments, *, source_ids=None):
        inserted = 0
        with self.connect() as db:
            conditions = ["origin='LOCAL_AI'", "pinned=0"]
            parameters = []
            if self.data_mode_scope:
                conditions.append("keyword_id IN (SELECT id FROM keywords WHERE data_mode=?)")
                parameters.append(self.data_mode_scope)
            if source_ids is not None:
                source_ids = tuple(source_ids)
                if source_ids:
                    conditions.append("keyword_id IN (" + ",".join("?" for _ in source_ids) + ")")
                    parameters.extend(source_ids)
                else:
                    conditions.append("0")
            db.execute("DELETE FROM memberships WHERE " + " AND ".join(conditions), parameters)
            for collection_id, keyword_id, score, reason in assignments:
                cursor = db.execute("""INSERT OR IGNORE INTO memberships
                    (collection_id,keyword_id,origin,pinned,score,reason,created_at)
                    SELECT ?,?, 'LOCAL_AI',0,?,?,? WHERE EXISTS(SELECT 1 FROM collections
                        WHERE id=? AND status='ACTIVE' AND auto_match=1)
                        AND NOT EXISTS(SELECT 1 FROM feedback WHERE collection_id=? AND keyword_id=?
                            AND action='REMOVE')
                        AND NOT EXISTS(SELECT 1 FROM memberships WHERE collection_id=? AND keyword_id=? AND pinned=1)""",
                    (collection_id, keyword_id, score, reason, now(), collection_id,
                     collection_id, keyword_id, collection_id, keyword_id))
                inserted += max(cursor.rowcount, 0)
        return inserted

    def collection_name_candidates(self, name_fingerprint):
        with self.connect() as db:
            suppressed = db.execute("SELECT 1 FROM suppression WHERE fingerprint=?",
                                    (name_fingerprint,)).fetchone() is not None
            pending = db.execute("SELECT p.name FROM proposals p WHERE p.status='PENDING' "
                + ("AND EXISTS (SELECT 1 FROM json_each(p.support_ids) s JOIN keywords k ON k.id=s.value "
                   "WHERE k.data_mode=?)" if self.data_mode_scope else ""),
                (self.data_mode_scope,) if self.data_mode_scope else ()).fetchall()
            labels = [row["name"] for row in pending]
            labels.extend(row["label"] for row in db.execute(
                "SELECT label FROM suppression WHERE label<>''"))
            active = [dict(row) for row in db.execute(
                "SELECT name,category_id FROM collections WHERE status='ACTIVE'")]
            labels.extend(item["name"] for item in active
                          if not item["category_id"] or fingerprint(item["name"]) == name_fingerprint)
            return {"suppressed": suppressed, "labels": labels}

    def user_member_examples(self, collection_ids):
        result = {identifier: [] for identifier in collection_ids}
        if not result:
            return result
        with self.connect() as db:
            for identifier in result:
                rows = db.execute("""SELECT k.keyword FROM memberships m
                    JOIN keywords k ON k.id=m.keyword_id WHERE m.collection_id=? AND m.origin='USER' """
                    + ("AND k.data_mode=? " if self.data_mode_scope else "")
                    + "ORDER BY m.created_at DESC LIMIT 100",
                    (identifier, self.data_mode_scope) if self.data_mode_scope else (identifier,)).fetchall()
                result[identifier] = [row["keyword"] for row in rows]
        return result

    def refresh_latest_topic_counts(self):
        if not self.data_mode_scope:
            return None
        with self.connect() as db:
            latest = db.execute("SELECT dataset_id FROM dataset_snapshots WHERE data_mode=? "
                "ORDER BY created_at DESC LIMIT 1", (self.data_mode_scope,)).fetchone()
            if latest is None:
                return None
            dataset_id = latest["dataset_id"]
            topic_filter = "k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE'"
            eligible = db.execute("""SELECT count(*) FROM keywords k
                LEFT JOIN classification_results cr ON cr.keyword_id=k.id WHERE """ + topic_filter
                + " AND k.data_mode=?", (self.data_mode_scope,)).fetchone()[0]
            assigned = db.execute("""SELECT count(DISTINCT m.keyword_id) FROM memberships m
                JOIN keywords k ON k.id=m.keyword_id LEFT JOIN classification_results cr ON cr.keyword_id=k.id
                WHERE """ + topic_filter + " AND k.data_mode=?",
                (self.data_mode_scope,)).fetchone()[0]
            unassigned = db.execute("""SELECT count(*) FROM keywords k
                LEFT JOIN classification_results cr ON cr.keyword_id=k.id WHERE """ + topic_filter
                + " AND k.data_mode=? AND NOT EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id)",
                (self.data_mode_scope,)).fetchone()[0]
            ai_assigned = db.execute("""SELECT count(DISTINCT m.keyword_id) FROM memberships m
                JOIN keywords k ON k.id=m.keyword_id LEFT JOIN classification_results cr ON cr.keyword_id=k.id
                WHERE """ + topic_filter + " AND k.data_mode=? AND m.origin='LOCAL_AI'",
                (self.data_mode_scope,)).fetchone()[0]
            db.execute("""UPDATE dataset_snapshots SET eligible_keyword_count=?,assigned_keyword_count=?,
                unassigned_keyword_count=? WHERE dataset_id=?""", (eligible, assigned, unassigned, dataset_id))
            db.execute("UPDATE fetch_jobs SET eligible=?,assigned=?,unassigned=? WHERE fetch_id=?",
                       (eligible, ai_assigned, unassigned, dataset_id))
        return {"dataset_id": dataset_id, "eligible": eligible, "assigned": assigned,
                "ai_assigned": ai_assigned, "unassigned": unassigned}

    def count_ai_assigned_keywords(self):
        query = """SELECT count(DISTINCT m.keyword_id) FROM memberships m
            JOIN keywords k ON k.id=m.keyword_id WHERE m.origin='LOCAL_AI'"""
        parameters = ()
        if self.data_mode_scope:
            query += " AND k.data_mode=?"
            parameters = (self.data_mode_scope,)
        with self.connect() as db:
            return db.execute(query, parameters).fetchone()[0]

    def complete_fetch_dataset_snapshot(self, fetch_id):
        scope = " AND k.data_mode=?" if self.data_mode_scope else ""
        keyword_parameters = (self.data_mode_scope,) if self.data_mode_scope else ()
        membership_parameters = (self.data_mode_scope,) if self.data_mode_scope else ()
        with self.connect() as db:
            eligible = db.execute("SELECT count(*) FROM keywords WHERE eligible=1 "
                + ("AND data_mode=?" if self.data_mode_scope else ""), keyword_parameters).fetchone()[0]
            assigned = db.execute("""SELECT count(DISTINCT m.keyword_id) FROM memberships m
                JOIN keywords k ON k.id=m.keyword_id LEFT JOIN classification_results cr ON cr.keyword_id=k.id
                WHERE k.eligible=1 AND COALESCE(cr.classification_status,'')<>'INELIGIBLE'"""
                + scope, membership_parameters).fetchone()[0]
            unassigned = db.execute("""SELECT count(*) FROM keywords k WHERE k.eligible=1"""
                + (" AND k.data_mode=?" if self.data_mode_scope else "") + """
                AND COALESCE((SELECT classification_status FROM classification_results cr
                    WHERE cr.keyword_id=k.id),'')<>'INELIGIBLE'
                AND NOT EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id)""",
                keyword_parameters).fetchone()[0]
            db.execute("""UPDATE dataset_snapshots SET processing_status='COMPLETED',
                eligible_keyword_count=?,assigned_keyword_count=?,unassigned_keyword_count=?
                WHERE dataset_id=?""", (eligible, assigned, unassigned, fetch_id))
        return {"eligible": eligible, "assigned": assigned, "unassigned": unassigned}

    def update_fetch_job(self, fetch_id, values):
        allowed = {"status", "stage", "available", "eligible", "processed", "assigned", "unassigned",
                   "created_collections", "suggestions", "skipped", "cached_embeddings", "inferred_embeddings",
                   "membership_reconciliation_id"}
        changes = {key: value for key, value in values.items() if key in allowed}
        assignments = [f"{key}=?" for key in changes]
        parameters = list(changes.values())
        assignments.append("updated_at=?")
        parameters.extend([now(), fetch_id])
        with self.connect() as db:
            db.execute(f"UPDATE fetch_jobs SET {','.join(assignments)} WHERE fetch_id=?", parameters)

    def mark_fetch_failed(self, fetch_id, error_code):
        stamp = now()
        with self.connect() as db:
            db.execute("""INSERT INTO fetch_jobs(fetch_id,status,error_code,updated_at)
                VALUES(?,'FAILED',?,?) ON CONFLICT(fetch_id) DO UPDATE SET
                status='FAILED',error_code=excluded.error_code,updated_at=excluded.updated_at""",
                (fetch_id, error_code, stamp))

    def complete_empty_fetch(self, fetch_id, values):
        allowed = {"status", "stage", "available", "eligible", "processed", "assigned", "unassigned",
                   "created_collections", "suggestions", "skipped", "cached_embeddings", "inferred_embeddings",
                   "membership_reconciliation_id"}
        changes = {key: value for key, value in values.items() if key in allowed}
        with self.connect() as db:
            snapshot = db.execute(
                "SELECT eligible_keyword_count FROM dataset_snapshots WHERE dataset_id=?", (fetch_id,)
            ).fetchone()
            changes["eligible"] = snapshot["eligible_keyword_count"] if snapshot else 0
            assignments = [f"{key}=?" for key in changes]
            parameters = list(changes.values())
            assignments.append("updated_at=?")
            parameters.append(now())
            parameters.append(fetch_id)
            db.execute("UPDATE dataset_snapshots SET processing_status='COMPLETED' WHERE dataset_id=?",
                       (fetch_id,))
            db.execute(f"UPDATE fetch_jobs SET {','.join(assignments)} WHERE fetch_id=?", parameters)
        return snapshot["eligible_keyword_count"] if snapshot else 0

    def mark_fetch_model_unavailable(self, fetch_id, *, available, skipped, eligible,
                                     cached_embeddings, inferred_embeddings, error_code):
        with self.connect() as db:
            db.execute("UPDATE dataset_snapshots SET processing_status='MODEL_UNAVAILABLE' WHERE dataset_id=?",
                       (fetch_id,))
            db.execute("""INSERT OR REPLACE INTO fetch_jobs(fetch_id,status,available,processed,skipped,
                error_code,updated_at,stage,eligible,cached_embeddings,inferred_embeddings)
                VALUES(?,?,?,?,?,?,?,'MODEL_UNAVAILABLE',?,?,?)""",
                (fetch_id, "MODEL_UNAVAILABLE", available, 0, skipped, error_code,
                 now(), eligible, cached_embeddings, inferred_embeddings))

    def persist_dataset_snapshot(self, dataset_id, *, status, data_mode, source_manifest,
                                 provider_record_count, received_record_count,
                                 retained_keyword_count, rejected_record_count,
                                 observation_start, observation_end, fetched_at, quality_flags,
                                 processing_status, created_at):
        with self.connect() as db:
            mode_parameters = (self.data_mode_scope,) if self.data_mode_scope else ()
            eligible = db.execute("SELECT count(*) FROM keywords WHERE eligible=1 "
                + ("AND data_mode=?" if self.data_mode_scope else ""), mode_parameters).fetchone()[0]
            assigned = db.execute("SELECT count(DISTINCT m.keyword_id) FROM memberships m "
                "JOIN keywords k ON k.id=m.keyword_id "
                + ("WHERE k.data_mode=?" if self.data_mode_scope else ""), mode_parameters).fetchone()[0]
            unassigned = db.execute("""SELECT count(*) FROM keywords k WHERE k.eligible=1"""
                + (" AND k.data_mode=?" if self.data_mode_scope else "") + """
                AND COALESCE((SELECT classification_status FROM classification_results cr
                    WHERE cr.keyword_id=k.id),'')<>'INELIGIBLE'
                AND NOT EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id)""",
                mode_parameters).fetchone()[0]
            db.execute("""INSERT OR REPLACE INTO dataset_snapshots(dataset_id,status,data_mode,source_manifest,
                provider_record_count,received_record_count,retained_keyword_count,rejected_record_count,
                observation_start,observation_end,fetched_at,quality_flags,processing_status,
                eligible_keyword_count,assigned_keyword_count,unassigned_keyword_count,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (dataset_id, status, data_mode, json.dumps(source_manifest, ensure_ascii=False, sort_keys=True),
                 provider_record_count, received_record_count, retained_keyword_count, rejected_record_count,
                 observation_start, observation_end, fetched_at, json.dumps(quality_flags), processing_status,
                 eligible, assigned, unassigned, created_at))

    def bulk_membership_action(self, keyword_ids, action, *, collection_id=None, source_collection_id=None):
        """Apply a bounded user action atomically; return explicit updated/skipped counts."""
        ids = list(dict.fromkeys(keyword_ids))
        updated = skipped = 0
        stamp = now()
        with self.connect() as db:
            collections = {row["id"] for row in db.execute(
                "SELECT id FROM collections WHERE status='ACTIVE'")}
            if collection_id and collection_id not in collections:
                raise ValueError("COLLECTION_NOT_FOUND")
            if source_collection_id and source_collection_id not in collections:
                raise ValueError("COLLECTION_NOT_FOUND")
            for keyword_id in ids:
                if action in {"EXCLUDE", "RESTORE"}:
                    keyword = db.execute("SELECT 1 FROM keywords WHERE id=? "
                        + ("AND data_mode=?" if self.data_mode_scope else ""),
                        (keyword_id, self.data_mode_scope) if self.data_mode_scope else (keyword_id,)).fetchone()
                    if not keyword:
                        skipped += 1
                        continue
                    if action == "EXCLUDE":
                        db.execute("INSERT OR REPLACE INTO keyword_decisions VALUES(?,'EXCLUDED','User excluded',?)",
                                   (keyword_id, stamp))
                        db.execute("UPDATE keywords SET eligible=0,rejection_reason='USER_EXCLUDED' WHERE id=? "
                            + ("AND data_mode=?" if self.data_mode_scope else ""),
                            (keyword_id, self.data_mode_scope) if self.data_mode_scope else (keyword_id,))
                        db.execute("DELETE FROM memberships WHERE keyword_id=? AND origin='LOCAL_AI' "
                            + ("AND keyword_id IN (SELECT id FROM keywords WHERE data_mode=?)" if self.data_mode_scope else ""),
                            (keyword_id, self.data_mode_scope) if self.data_mode_scope else (keyword_id,))
                    else:
                        db.execute("INSERT OR REPLACE INTO keyword_decisions VALUES(?,'RESTORED','User restored',?)",
                                   (keyword_id, stamp))
                        db.execute("""UPDATE keywords SET eligible=CASE WHEN COALESCE((SELECT classification_status
                            FROM classification_results WHERE keyword_id=keywords.id),'')='INELIGIBLE' THEN 0 ELSE 1 END,
                            rejection_reason=CASE WHEN COALESCE((SELECT classification_status
                            FROM classification_results WHERE keyword_id=keywords.id),'')='INELIGIBLE'
                            THEN 'CLASSIFICATION_INELIGIBLE' ELSE NULL END WHERE id=? """
                            + ("AND data_mode=?" if self.data_mode_scope else ""),
                            (keyword_id, self.data_mode_scope) if self.data_mode_scope else (keyword_id,))
                    updated += 1
                    continue
                if action in {"REMOVE", "REJECT"}:
                    keyword = db.execute("""SELECT 1 FROM memberships m JOIN keywords k ON k.id=m.keyword_id
                        WHERE m.collection_id=? AND m.keyword_id=? """
                        + ("AND k.data_mode=?" if self.data_mode_scope else ""),
                        (collection_id, keyword_id, self.data_mode_scope) if self.data_mode_scope
                        else (collection_id, keyword_id)).fetchone()
                else:
                    keyword = db.execute("""SELECT 1 FROM keywords k WHERE k.id=? AND k.eligible=1
                        AND COALESCE((SELECT classification_status FROM classification_results cr
                            WHERE cr.keyword_id=k.id),'')<>'INELIGIBLE' """
                        + ("AND data_mode=?" if self.data_mode_scope else ""),
                        (keyword_id, self.data_mode_scope) if self.data_mode_scope else (keyword_id,)).fetchone()
                if not keyword:
                    skipped += 1
                    continue
                if action in {"ADD", "COPY", "MOVE", "APPROVE"}:
                    cursor = db.execute("SELECT 1 FROM memberships WHERE collection_id=? AND keyword_id=?",
                                        (collection_id, keyword_id)).fetchone()
                    db.execute("INSERT OR REPLACE INTO feedback VALUES(?,?, 'ADD',?)",
                               (collection_id, keyword_id, stamp))
                    db.execute("""INSERT OR REPLACE INTO memberships VALUES(?,?, 'USER',1,NULL,
                        'Manually approved',?)""", (collection_id, keyword_id, stamp))
                    if action == "MOVE" and source_collection_id and source_collection_id != collection_id:
                        db.execute("DELETE FROM memberships WHERE collection_id=? AND keyword_id=?",
                                   (source_collection_id, keyword_id))
                        db.execute("INSERT OR REPLACE INTO feedback VALUES(?,?, 'REMOVE',?)",
                                   (source_collection_id, keyword_id, stamp))
                    if not cursor:
                        updated += 1
                    else:
                        # APPROVE changes the provenance/pin even for an existing AI membership.
                        updated += int(action == "APPROVE")
                elif action in {"REMOVE", "REJECT"}:
                    cursor = db.execute("SELECT 1 FROM memberships WHERE collection_id=? AND keyword_id=?",
                                        (collection_id, keyword_id)).fetchone()
                    if action == "REJECT":
                        db.execute("INSERT OR REPLACE INTO feedback VALUES(?,?, 'REMOVE',?)",
                                   (collection_id, keyword_id, stamp))
                    db.execute("DELETE FROM memberships WHERE collection_id=? AND keyword_id=?",
                               (collection_id, keyword_id))
                    if cursor:
                        updated += 1
                    else:
                        skipped += 1
                else:
                    raise ValueError("INVALID_BULK_ACTION")
            if collection_id:
                db.execute("UPDATE collections SET version=version+1,updated_at=? WHERE id=?",
                           (stamp, collection_id))
            if source_collection_id and source_collection_id != collection_id:
                db.execute("UPDATE collections SET version=version+1,updated_at=? WHERE id=?",
                           (stamp, source_collection_id))
        return {"updated": updated, "skipped": skipped, "failed": 0}

    def create_proposal(self, proposal_id, name, description, support_ids, reason, created_at, updated_at):
        with self.connect() as db:
            db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,'PENDING',?,?)",
                (proposal_id, name, description, json.dumps(support_ids), reason, created_at, updated_at))

    def list_research(self, *, q="", status=None, limit=50, offset=0):
        query = " ".join(q.casefold().split())
        where, parameters = [], []
        if self.data_mode_scope:
            where.append("(r.keyword_id IS NULL OR k.data_mode=?)")
            parameters.append(self.data_mode_scope)
        if status:
            where.append("r.status=?")
            parameters.append(status)
        if query:
            where.append('lower(r.subject || " " || r.hypothesis || " " || r.product_category || " " || r.niche) LIKE ?')
            parameters.append("%" + query + "%")
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.connect() as db:
            total = db.execute(
                "SELECT count(*) FROM research_records r LEFT JOIN keywords k ON k.id=r.keyword_id" + clause,
                parameters,
            ).fetchone()[0]
            rows = db.execute("""SELECT r.*,k.keyword,k.source,k.project,k.observed_at,k.data_mode
                FROM research_records r LEFT JOIN keywords k ON k.id=r.keyword_id""" + clause
                + " ORDER BY r.updated_at DESC,r.id LIMIT ? OFFSET ?",
                (*parameters, limit, offset)).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "limit": limit,
                "offset": offset, "next_offset": offset + limit if offset + limit < total else None}

    def create_research(self, record):
        with self.connect() as db:
            if record.get("keyword_id"):
                keyword = db.execute("SELECT 1 FROM keywords WHERE id=? "
                    + ("AND data_mode=?" if self.data_mode_scope else ""),
                    (record["keyword_id"], self.data_mode_scope) if self.data_mode_scope
                    else (record["keyword_id"],)).fetchone()
                if keyword is None:
                    raise ValueError("RESEARCH_KEYWORD_NOT_FOUND")
            db.execute("""INSERT INTO research_records(id,keyword_id,subject,hypothesis,product_category,niche,
                competitor_notes,market_evidence,ip_notes,risk_notes,findings,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(record[key] for key in (
                    "id", "keyword_id", "subject", "hypothesis", "product_category", "niche",
                    "competitor_notes", "market_evidence", "ip_notes", "risk_notes", "findings",
                    "status", "created_at", "updated_at")))
        return {key: record[key] for key in ("id", "status", "created_at", "updated_at")}

    def update_research(self, identifier, updates, updated_at):
        allowed = {"subject", "hypothesis", "product_category", "niche", "competitor_notes",
                   "market_evidence", "ip_notes", "risk_notes", "findings", "status"}
        values = {key: value for key, value in updates.items() if key in allowed}
        if not values:
            return None
        with self.connect() as db:
            exists = db.execute("SELECT 1 FROM research_records WHERE id=?", (identifier,)).fetchone()
            if exists is None:
                return None
            assignments = [key + "=?" for key in values] + ["updated_at=?"]
            db.execute("UPDATE research_records SET " + ",".join(assignments) + " WHERE id=?",
                       (*values.values(), updated_at, identifier))
            row = db.execute("SELECT id,status,updated_at FROM research_records WHERE id=?",
                             (identifier,)).fetchone()
        return dict(row)

    def suggestions(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM proposals p WHERE p.status='PENDING' "
                + ("AND EXISTS (SELECT 1 FROM json_each(p.support_ids) s JOIN keywords k ON k.id=s.value WHERE k.data_mode=?) "
                   if self.data_mode_scope else "")
                + "ORDER BY p.created_at DESC",
                (self.data_mode_scope,) if self.data_mode_scope else ())]

    def resolve_proposal(self, proposal_id, accept, *, name=None, description=None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM proposals WHERE id=? AND status='PENDING'", (proposal_id,)).fetchone()
            if row is None:
                return None
            support_ids = json.loads(row["support_ids"])
            if self.data_mode_scope and support_ids:
                placeholders = ",".join("?" for _ in support_ids)
                supported = db.execute(f"SELECT count(*) FROM keywords WHERE id IN ({placeholders}) AND data_mode=?",
                    (*support_ids, self.data_mode_scope)).fetchone()[0]
                if supported != len(set(support_ids)):
                    return None
            status = "ACCEPTED" if accept else "REJECTED"
            db.execute("UPDATE proposals SET status=?,updated_at=? WHERE id=?", (status, now(), proposal_id))
            if not accept:
                db.execute("INSERT OR REPLACE INTO suppression(fingerprint,kind,created_at,label) VALUES(?,?,?,?)",
                           (fingerprint(row["name"]), "REJECTED_PROPOSAL", now(), row["name"]))
            else:
                identifier, stamp = str(uuid.uuid4()), now()
                collection_name = name if name is not None else row["name"]
                collection_description = description if description is not None else row["description"]
                db.execute("""INSERT INTO collections(id,name,description,created_by,reason,created_at,updated_at)
                    VALUES(?,?,?,'AI',?,?,?)""",
                    (identifier, collection_name, collection_description, row["reason"], stamp, stamp))
                for keyword_id in support_ids:
                    db.execute("INSERT OR IGNORE INTO memberships VALUES(?,?, 'LOCAL_AI',0,NULL,?,?)",
                               (identifier, keyword_id, "Representative keyword in accepted AI suggestion", now()))
                return identifier
            return ""


def bing_verification_update(record: dict, data_mode: str) -> dict | None:
    """Validate Bing SERP evidence for attachment to its saved Google candidate."""
    from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, source_capability
    from app.sources.google_trends.trending_now import normalize_keyword
    from app.sources.serpapi.normalization import canonical_url

    if (not isinstance(record, dict)
            or record.get("record_type") != "BING_SAVED_CANDIDATE_VERIFICATION"
            or data_mode != "LIVE"
            or record.get("candidate_source_id") != GOOGLE_SOURCE_ID):
        return None
    candidate_id = record.get("candidate_keyword_id")
    identity = record.get("candidate_identity")
    keyword = record.get("candidate_keyword")
    if (not isinstance(candidate_id, str) or len(candidate_id) != 64
            or any(char not in "0123456789abcdef" for char in candidate_id)
            or not isinstance(identity, str) or len(identity) != 64
            or any(char not in "0123456789abcdef" for char in identity)):
        return None
    try:
        title, normalized = normalize_keyword(keyword)
        observed = datetime.fromisoformat(
            record.get("candidate_observed_at", "").replace("Z", "+00:00")
        )
    except (TypeError, ValueError, AttributeError):
        return None
    if (hashlib.sha256(normalized.encode("utf-8")).hexdigest() != identity
            or observed.tzinfo is None or observed.utcoffset() is None):
        return None
    verification = record.get("bing_verification")
    if (not isinstance(verification, dict)
            or verification.get("status") != "VERIFIED"
            or verification.get("provider_id") != source_capability(BING_SOURCE_ID).provider_id
            or verification.get("verification_mode") != "BING_SERP"
            or verification.get("query") != title
            or verification.get("market") != "US"
            or verification.get("language") != "en-US"):
        return None
    fetched = verification.get("fetched_at")
    provider_created = verification.get("provider_created_at")
    try:
        fetched_dt = datetime.fromisoformat(fetched.replace("Z", "+00:00"))
        created_dt = (datetime.fromisoformat(provider_created.replace("Z", "+00:00"))
                      if provider_created is not None else None)
    except (TypeError, ValueError, AttributeError):
        return None
    if (fetched_dt.tzinfo is None or fetched_dt.utcoffset() is None
            or created_dt is not None and (created_dt.tzinfo is None or created_dt.utcoffset() is None)
            or verification.get("delivery_state") not in {"FRESH", "CACHED"}):
        return None
    results = verification.get("results")
    if (not isinstance(results, list) or len(results) > 100
            or verification.get("result_count") != len(results)
            or type(verification.get("provider_result_count")) is not int
            or verification["provider_result_count"] < len(results)):
        return None
    clean_results = []
    for item in results:
        if not isinstance(item, dict):
            return None
        try:
            url = canonical_url(item.get("canonical_url"))
        except ValueError:
            return None
        rank = item.get("provider_rank")
        result_fetched = item.get("fetched_at")
        if (url != item.get("canonical_url") or type(rank) is not int or rank < 1
                or not isinstance(result_fetched, str)):
            return None
        clean_results.append(dict(item))
    return {"candidate_id": candidate_id, "identity": identity,
            "keyword": title, "observed_at": observed.astimezone(timezone.utc).isoformat(),
            "verification": {**verification, "results": clean_results}}


def saved_source_record(record: dict) -> dict:
    """Reconstruct source evidence for a cache-only collection reclassification."""
    from app.sources.catalog import GOOGLE_SOURCE_ID

    if not isinstance(record, dict):
        raise ValueError("saved keyword row is required")
    try:
        evidence = json.loads(record.get("evidence", "{}"))
    except (TypeError, ValueError):
        evidence = {}
    if not isinstance(evidence, dict):
        evidence = {}
    if record.get("source") == GOOGLE_SOURCE_ID:
        return {**evidence, "canonical_keyword": record.get("keyword")}
    return {
        "article_display": record.get("keyword"),
        "article_original": record.get("keyword"),
        "project": record.get("project"),
        "observation_date": record.get("observed_at"),
        "fetched_at": evidence.get("fetched_at"),
        "provider_rank": evidence.get("provider_rank"),
        "views_ceil": evidence.get("views_ceil"),
        "country_scope": evidence.get("country_scope"),
        "access": evidence.get("access"),
    }


def keyword_record(record: dict, source: str, data_mode: str, *, snapshot_status="SUCCESS") -> dict | None:
    if source == "google_web_serp" and record.get("data_source") == "GOOGLE_TRENDS":
        from app.sources.google_trends.trending_now import normalize_keyword

        try:
            title, identity = normalize_keyword(record.get("canonical_keyword"))
            fetched = record.get("fetched_at")
            fetched_dt = datetime.fromisoformat(fetched.replace("Z", "+00:00"))
            if fetched_dt.tzinfo is None or fetched_dt.utcoffset() is None:
                return None
            identity_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            if record.get("canonical_keyword_identity") != identity_digest:
                return None
            provider = record.get("provider_id")
            if provider not in {
                "serpapi_google_trends_trending_now", "google_trends_rss",
            }:
                return None
            if data_mode not in {"LIVE", "SYNTHETIC"}:
                return None
            clusters = record.get("trend_clusters")
            if not isinstance(clusters, list) or not clusters:
                return None
            if any(not isinstance(cluster, dict) for cluster in clusters):
                return None
            evidence_fields = (
                "data_source", "discovery_mode", "provider_id", "transport_id",
                "canonical_keyword_identity", "region", "language", "time_window_hours",
                "provider_data_at", "provider_created_at", "fetched_at", "source_url",
                "delivery_state", "trend_clusters", "serp_verification", "quality_flags",
                "bing_verification", "bing_verification_history",
            )
            evidence = {key: record[key] for key in evidence_fields if key in record}
            encoded_evidence = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                                          allow_nan=False, separators=(",", ":"))
            # RSS and Trending Now are discovery paths within one Google source.
            # A provider switch must update the same keyword row, not inflate counts.
            project = "google_trends:US:en"
            stable = keyword_identity({**record, "project": project}, source)
            if stable is None:
                return None
            started = [cluster.get("started_at") for cluster in clusters
                       if isinstance(cluster.get("started_at"), str)]
            observed = min(started) if started else fetched_dt.astimezone(timezone.utc).isoformat()
            source_url = record.get("source_url")
            if (not isinstance(source_url, str) or len(source_url) > 2048
                    or not source_url.startswith("https://trends.google.com/")):
                return None
            return {
                "id": stable, "keyword": title,
                "normalized": identity,
                "source": source, "project": project, "observed_at": observed,
                "data_mode": data_mode,
                "evidence": json.dumps({**json.loads(encoded_evidence),
                    "snapshot_status": snapshot_status,
                    "metric_definition": "Provider-reported approximate traffic bucket; not an exact search count",
                    "increase_percentage_definition": "Provider-reported increase; no application baseline"},
                    ensure_ascii=False, sort_keys=True, allow_nan=False),
                "context": f"Google Trends discovery, US scope; {record.get('discovery_mode', 'UNKNOWN')} "
                           "with no compatible historical baseline",
            }
        except (AttributeError, TypeError, ValueError, UnicodeError, OverflowError):
            return None

    from app.sources.wikimedia.cleaning_rules import (
        COLLECTION_FILTER_VERSION,
        RULE_VERSION,
        collection_technical_reason,
    )

    title = record.get("article_display") or record.get("canonical_keyword") or record.get("article_original")
    if not isinstance(title, str) or not title.strip() or len(title) > 256:
        return None
    project = record.get("project", "")
    observed = record.get("observation_date", "")
    if not isinstance(project, str) or not isinstance(observed, str):
        return None
    original = title.strip()
    if collection_technical_reason(project, original):
        return None
    if (not isinstance(record.get("fetched_at"), str)
            or type(record.get("views_ceil")) is not int or record["views_ceil"] < 0
            or type(record.get("provider_rank")) is not int or record["provider_rank"] < 1):
        return None
    stable = keyword_identity(record, source)
    return {"id": stable, "keyword": original, "normalized": " ".join(original.casefold().split()),
        "source": source, "project": project, "observed_at": observed, "data_mode": data_mode,
        "evidence": json.dumps({**{key: record.get(key) for key in (
            "article_original", "article_display", "provider_rank", "views_ceil", "country_scope",
            "access", "observation_date", "fetched_at", "source_url", "quality_flags")},
            "snapshot_status": snapshot_status,
            "normalization_provenance": f"{RULE_VERSION}; {COLLECTION_FILTER_VERSION}"}, ensure_ascii=False, sort_keys=True),
        "context": f"Wikimedia Pageviews, project {project}; country scope {record.get('country_scope', 'unknown')}, access {record.get('access', 'unknown')}"}


def keyword_identity(record: dict, source: str) -> str | None:
    title = record.get("article_display") or record.get("canonical_keyword") or record.get("article_original")
    project = record.get("project", "")
    if not isinstance(title, str) or not isinstance(project, str):
        return None
    if source == "google_web_serp" and record.get("data_source") == "GOOGLE_TRENDS":
        try:
            from app.sources.google_trends.trending_now import normalize_keyword
            _, normalized = normalize_keyword(title)
        except (TypeError, ValueError):
            return None
    else:
        normalized = " ".join(title.strip().casefold().split())
    if not normalized:
        return None
    return hashlib.sha256(f"{source}:{project}:{normalized}".encode()).hexdigest()
