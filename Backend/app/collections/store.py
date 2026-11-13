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
 error_code TEXT, updated_at TEXT NOT NULL
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
