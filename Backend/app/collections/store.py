"""Dedicated local SQLite state for user-defined Collections, isolated from D6."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.storage.local_file_security import secure_private_directory

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS collections (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
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
 inferred_embeddings INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS membership_reconciliation_runs (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS membership_reconciliation_items (
 run_id TEXT NOT NULL, collection_id TEXT NOT NULL, keyword_id TEXT NOT NULL,
 origin TEXT NOT NULL, pinned INTEGER NOT NULL, score REAL, reason TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(run_id,collection_id,keyword_id)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value: str) -> str:
    return hashlib.sha256(" ".join(value.casefold().split()).encode()).hexdigest()


class CollectionStore:
    def __init__(self, path: Path):
        path = Path(path)
        secure_private_directory(path.parent)
        self.path = path
        with self.connect() as db:
            db.executescript(SCHEMA)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(suppression)")}
            if "label" not in columns:
                db.execute("ALTER TABLE suppression ADD COLUMN label TEXT NOT NULL DEFAULT ''")
            keyword_columns = {row["name"] for row in db.execute("PRAGMA table_info(keywords)")}
            if "eligible" not in keyword_columns:
                db.execute("ALTER TABLE keywords ADD COLUMN eligible INTEGER NOT NULL DEFAULT 1")
            if "rejection_reason" not in keyword_columns:
                db.execute("ALTER TABLE keywords ADD COLUMN rejection_reason TEXT")
            job_columns = {row["name"] for row in db.execute("PRAGMA table_info(fetch_jobs)")}
            for name, declaration in (("stage", "TEXT NOT NULL DEFAULT 'QUEUED'"),
                                      ("eligible", "INTEGER NOT NULL DEFAULT 0"),
                                      ("cached_embeddings", "INTEGER NOT NULL DEFAULT 0"),
                                      ("inferred_embeddings", "INTEGER NOT NULL DEFAULT 0")):
                if name not in job_columns:
                    db.execute(f"ALTER TABLE fetch_jobs ADD COLUMN {name} {declaration}")
        if self.path.is_symlink():
            raise ValueError("collection database must not be a symlink")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            raise ValueError("collection database permissions could not be restricted") from None

    @classmethod
    def local(cls, state_dir: Path | None = None):
        directory = state_dir or Path.home() / ".local/state/trendscout/collections-v1"
        return cls(Path(directory) / "collections.sqlite")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def latest_dataset(self):
        """Return durable local dataset metadata, with an honest legacy fallback."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM dataset_snapshots ORDER BY created_at DESC LIMIT 1").fetchone()
            total, eligible, excluded = db.execute("""SELECT count(*),
                sum(eligible=1),sum(eligible=0) FROM keywords""").fetchone()
            assigned = db.execute("SELECT count(DISTINCT keyword_id) FROM memberships").fetchone()[0]
            unassigned = db.execute("""SELECT count(*) FROM keywords k WHERE eligible=1 AND NOT EXISTS
                (SELECT 1 FROM memberships m WHERE m.keyword_id=k.id)""").fetchone()[0]
            if row is None:
                legacy = db.execute("""SELECT * FROM fetch_jobs WHERE status IN ('COMPLETED','MODEL_UNAVAILABLE')
                    ORDER BY updated_at DESC LIMIT 1""").fetchone()
                if legacy is None and not total:
                    return None
                sources = [item[0] for item in db.execute("SELECT DISTINCT source FROM keywords ORDER BY source")]
                modes = [item[0] for item in db.execute("SELECT DISTINCT data_mode FROM keywords ORDER BY data_mode")]
                fetched = None
                dates = []
                for evidence, observed in db.execute("SELECT evidence,observed_at FROM keywords"):
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

    def search_keywords(self, *, query="", state="all", collection_ids=(), source="",
                        sort="recent", limit=50, offset=0, ids_only=False):
        normalized = " ".join(query.casefold().split())
        clauses, parameters = [], []
        if normalized:
            escaped = normalized.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            clauses.append("k.normalized LIKE ? ESCAPE '!'")
            parameters.append(f"%{escaped}%")
        if source:
            clauses.append("k.source=?")
            parameters.append(source)
        if state == "excluded":
            clauses.append("k.eligible=0")
        elif state == "unassigned":
            clauses.append("k.eligible=1 AND NOT EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id)")
        elif state == "ai":
            clauses.append("k.eligible=1 AND EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id AND m.origin='LOCAL_AI')")
        elif state == "manual":
            clauses.append("k.eligible=1 AND EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id AND m.origin='USER')")
        elif state == "needs_review":
            clauses.append("k.eligible=1 AND (NOT EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id) OR EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id AND m.origin='LOCAL_AI'))")
        elif state != "all":
            raise ValueError("INVALID_KEYWORD_STATE")
        ids = tuple(dict.fromkeys(collection_ids))
        if ids:
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"EXISTS(SELECT 1 FROM memberships m WHERE m.keyword_id=k.id AND m.collection_id IN ({placeholders}))")
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
            total = db.execute(f"SELECT count(*) FROM keywords k WHERE {where}", parameters).fetchone()[0]
            if ids_only:
                rows = db.execute(f"SELECT k.id FROM keywords k WHERE {where} ORDER BY {ordering}",
                                  parameters).fetchall()
                return {"ids": [row["id"] for row in rows], "total": total}
            pagination = " LIMIT ? OFFSET ?" if limit is not None else ""
            page_parameters = (*parameters, limit, offset) if limit is not None else tuple(parameters)
            rows = db.execute(f"""SELECT k.*,
                (SELECT json_group_array(json_object('id',c.id,'name',c.name,'origin',m.origin))
                 FROM memberships m JOIN collections c ON c.id=m.collection_id WHERE m.keyword_id=k.id) memberships_json
                FROM keywords k WHERE {where} ORDER BY {ordering}{pagination}""",
                page_parameters).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                try:
                    item["evidence"] = json.loads(item["evidence"])
                    item["memberships"] = json.loads(item.pop("memberships_json") or "[]")
                except (TypeError, ValueError):
                    item["memberships"] = []
                item["algorithm_results"] = [dict(result) for result in db.execute(
                    "SELECT algorithm_id,version,status,reason_code,value,metric,unit,result_json FROM algorithm_results WHERE keyword_id=? ORDER BY algorithm_id",
                    (item["id"],))]
                items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "next_offset": offset + limit if limit is not None and offset + limit < total else None}

    def list_collections(self):
        with self.connect() as db:
            rows = db.execute("""SELECT c.*, count(m.keyword_id) AS membership_count
                FROM collections c LEFT JOIN memberships m ON m.collection_id=c.id
                WHERE c.status='ACTIVE' GROUP BY c.id ORDER BY c.updated_at DESC""").fetchall()
            return [self._collection(row) for row in rows]

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
            db.execute("""INSERT INTO collections(id,name,description,examples,exclusions,created_by,
                auto_match,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (identifier, data["name"], data.get("description", ""),
                 json.dumps(data.get("examples", []), ensure_ascii=False),
                 json.dumps(data.get("exclusions", []), ensure_ascii=False), created_by,
                 int(data.get("auto_match", True)), reason, stamp, stamp))
        return identifier

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
            row = db.execute("SELECT name FROM collections WHERE id=?", (identifier,)).fetchone()
            if row is None:
                return False
            db.execute("INSERT OR REPLACE INTO suppression(fingerprint,kind,created_at,label) VALUES(?,?,?,?)",
                       (fingerprint(row["name"]), "DELETED_COLLECTION", now(), row["name"]))
            db.execute("DELETE FROM collections WHERE id=?", (identifier,))
            return True

    def archive_unpinned_ai_memberships(self, run_id: str, reason: str):
        """Archive automatic memberships before reconciliation so they can be restored exactly."""
        stamp = now()
        with self.connect() as db:
            db.execute("INSERT INTO membership_reconciliation_runs VALUES(?,?,?,'APPLYING')",
                       (run_id, stamp, reason))
            db.execute("""INSERT INTO membership_reconciliation_items
                SELECT ?,collection_id,keyword_id,origin,pinned,score,reason,created_at
                FROM memberships WHERE origin='LOCAL_AI' AND pinned=0""", (run_id,))
            archived = db.execute("SELECT changes()").fetchone()[0]
            db.execute("DELETE FROM memberships WHERE origin='LOCAL_AI' AND pinned=0")
            db.execute("UPDATE membership_reconciliation_runs SET status='APPLIED' WHERE id=?", (run_id,))
            return archived

    def rollback_membership_reconciliation(self, run_id: str):
        with self.connect() as db:
            run = db.execute("SELECT status FROM membership_reconciliation_runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["status"] != "APPLIED":
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
            db.execute("""INSERT OR IGNORE INTO memberships(collection_id,keyword_id,origin,pinned,score,reason,created_at)
                SELECT i.collection_id,i.keyword_id,i.origin,i.pinned,i.score,i.reason,i.created_at
                FROM membership_reconciliation_items i
                JOIN collections c ON c.id=i.collection_id
                JOIN keywords k ON k.id=i.keyword_id WHERE i.run_id=?""", (run_id,))
            db.execute("UPDATE membership_reconciliation_runs SET status='ROLLED_BACK' WHERE id=?", (run_id,))
            return True

    def keywords_for(self, collection_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT k.*,m.origin,m.pinned,m.score,m.reason
                FROM memberships m JOIN keywords k ON k.id=m.keyword_id
                WHERE m.collection_id=? ORDER BY k.keyword COLLATE NOCASE""", (collection_id,))]

    def feedback_action(self, collection_id, record, action, *, pinned=True):
        with self.connect() as db:
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
                    keyword = db.execute("SELECT 1 FROM keywords WHERE id=?", (keyword_id,)).fetchone()
                    if not keyword:
                        skipped += 1
                        continue
                    if action == "EXCLUDE":
                        db.execute("INSERT OR REPLACE INTO keyword_decisions VALUES(?,'EXCLUDED','User excluded',?)",
                                   (keyword_id, stamp))
                        db.execute("UPDATE keywords SET eligible=0,rejection_reason='USER_EXCLUDED' WHERE id=?",
                                   (keyword_id,))
                        db.execute("DELETE FROM memberships WHERE keyword_id=? AND origin='LOCAL_AI'", (keyword_id,))
                    else:
                        db.execute("INSERT OR REPLACE INTO keyword_decisions VALUES(?,'RESTORED','User restored',?)",
                                   (keyword_id, stamp))
                        db.execute("UPDATE keywords SET eligible=1,rejection_reason=NULL WHERE id=?", (keyword_id,))
                    updated += 1
                    continue
                keyword = db.execute("SELECT 1 FROM keywords WHERE id=? AND eligible=1", (keyword_id,)).fetchone()
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

    def suggestions(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM proposals WHERE status='PENDING' ORDER BY created_at DESC")]

    def resolve_proposal(self, proposal_id, accept):
        with self.connect() as db:
            row = db.execute("SELECT * FROM proposals WHERE id=? AND status='PENDING'", (proposal_id,)).fetchone()
            if row is None:
                return None
            status = "ACCEPTED" if accept else "REJECTED"
            db.execute("UPDATE proposals SET status=?,updated_at=? WHERE id=?", (status, now(), proposal_id))
            if not accept:
                db.execute("INSERT OR REPLACE INTO suppression(fingerprint,kind,created_at,label) VALUES(?,?,?,?)",
                           (fingerprint(row["name"]), "REJECTED_PROPOSAL", now(), row["name"]))
            else:
                identifier, stamp = str(uuid.uuid4()), now()
                db.execute("""INSERT INTO collections(id,name,description,created_by,reason,created_at,updated_at)
                    VALUES(?,?,?,'AI',?,?,?)""",
                    (identifier, row["name"], row["description"], row["reason"], stamp, stamp))
                support_ids = json.loads(row["support_ids"])
                for keyword_id in support_ids:
                    db.execute("INSERT OR IGNORE INTO memberships VALUES(?,?, 'LOCAL_AI',0,NULL,?,?)",
                               (identifier, keyword_id, "Representative keyword in accepted AI suggestion", now()))
                return identifier
            return ""


def keyword_record(record: dict, source: str, data_mode: str, *, snapshot_status="SUCCESS") -> dict | None:
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
    normalized = " ".join(title.strip().casefold().split())
    if not normalized:
        return None
    return hashlib.sha256(f"{source}:{project}:{normalized}".encode()).hexdigest()
