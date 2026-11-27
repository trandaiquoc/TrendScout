"""SQLite persistence boundary for algorithm analysis state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict


def analysis_state_digest(records, metadata_by_key, observation_rows, result_rows):
    """Fingerprint the persisted inputs observed by the algorithm digest pass."""
    digest = hashlib.sha256(b"trendscout-algorithm-state-v1\n")

    def add(value):
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("ascii")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)

    try:
        add({key: asdict(value) for key, value in metadata_by_key.items()})
        for record in records:
            add(record)
        for row in observation_rows:
            add(tuple(row.values()) if hasattr(row, "values") else tuple(row))
        for row in result_rows:
            add(tuple(row.values()) if hasattr(row, "values") else tuple(row))
        return digest.hexdigest()
    except (TypeError, ValueError, OverflowError, UnicodeError):
        # The ordinary per-record path remains authoritative for malformed or
        # unusual database values that cannot be represented in this cache key.
        return None


class SQLiteAlgorithmAnalysisSession:
    """Database operations used during one atomic SQLite analysis pass."""

    def __init__(self, db, data_mode_scope):
        self._db = db
        self._data_mode_scope = data_mode_scope

    def eligible_keywords(self, *, source=None, observed_from=None, observed_to=None, ids=None):
        clauses = ["eligible=1"]
        parameters = []
        if self._data_mode_scope:
            clauses.append("data_mode=?")
            parameters.append(self._data_mode_scope)
        if source:
            clauses.append("source=?")
            parameters.append(source)
        if observed_from:
            clauses.append("substr(observed_at,1,10)>=?")
            parameters.append(observed_from)
        if observed_to:
            clauses.append("substr(observed_at,1,10)<=?")
            parameters.append(observed_to)
        if ids is not None:
            identifiers = tuple(dict.fromkeys(ids))
            if not identifiers:
                return []
            clauses.append("id IN (" + ",".join("?" for _ in identifiers) + ")")
            parameters.extend(identifiers)
        rows = self._db.execute("SELECT * FROM keywords WHERE " + " AND ".join(clauses)
                                + " ORDER BY id", parameters).fetchall()
        return [dict(row) for row in rows]

    def upsert_observations(self, observations):
        self._db.executemany("""INSERT INTO algorithm_observations(keyword_id,source,observation_date,
            country,access,project,metric,unit,status,value,rank,snapshot_status,input_digest)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(keyword_id,source,observation_date,metric,unit)
            DO UPDATE SET value=excluded.value,rank=excluded.rank,status=excluded.status,
            country=excluded.country,access=excluded.access,project=excluded.project,
            snapshot_status=excluded.snapshot_status,input_digest=excluded.input_digest
            WHERE algorithm_observations.value IS NOT excluded.value
               OR algorithm_observations.rank IS NOT excluded.rank
               OR algorithm_observations.status IS NOT excluded.status
               OR algorithm_observations.country IS NOT excluded.country
               OR algorithm_observations.access IS NOT excluded.access
               OR algorithm_observations.project IS NOT excluded.project
               OR algorithm_observations.snapshot_status IS NOT excluded.snapshot_status
               OR algorithm_observations.input_digest IS NOT excluded.input_digest""", observations)

    def state_digest(self, records, metadata_by_key):
        scope = " AND k.data_mode=?" if self._data_mode_scope else ""
        parameters = (self._data_mode_scope,) if self._data_mode_scope else ()
        observations = self._db.execute(
            "SELECT o.* FROM algorithm_observations o JOIN keywords k ON k.id=o.keyword_id "
            "WHERE k.eligible=1" + scope +
            " ORDER BY o.keyword_id,o.source,o.observation_date,o.metric,o.unit", parameters
        )
        results = self._db.execute(
            "SELECT r.keyword_id,r.algorithm_id,r.version,r.status,r.reason_code,r.value,"
            "r.metric,r.unit,r.result_json,r.input_digest FROM algorithm_results r "
            "JOIN keywords k ON k.id=r.keyword_id WHERE k.eligible=1" + scope +
            " ORDER BY r.keyword_id,r.algorithm_id", parameters
        )
        return analysis_state_digest(records, metadata_by_key, observations, results)

    def setting(self, key):
        row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def history(self, keyword_id, source, before_date, metric, unit, *, after_date=None,
                country=None, access=None, project=None):
        clauses = ["keyword_id=?", "source=?", "observation_date<?", "metric=?", "unit=?"]
        parameters = [keyword_id, source, before_date, metric, unit]
        if after_date:
            clauses.append("observation_date>=?")
            parameters.append(after_date)
        for column, value in (("country", country), ("access", access), ("project", project)):
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        return [dict(row) for row in self._db.execute("""SELECT observation_date,status,value,snapshot_status,input_digest
            FROM algorithm_observations WHERE """ + " AND ".join(clauses) + " ORDER BY observation_date",
            parameters).fetchall()]

    def histories(self, keyword_ids, source, before_date, metric, unit, *, after_date=None,
                  country=None, access=None, project=None):
        identifiers = tuple(dict.fromkeys(keyword_ids))
        if not identifiers:
            return []
        clauses = ["keyword_id IN (" + ",".join("?" for _ in identifiers) + ")",
                   "source=?", "observation_date<?", "metric=?", "unit=?"]
        parameters = [*identifiers, source, before_date, metric, unit]
        if after_date:
            clauses.append("observation_date>=?")
            parameters.append(after_date)
        for column, value in (("country", country), ("access", access), ("project", project)):
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        return [dict(row) for row in self._db.execute("""SELECT keyword_id,observation_date,status,value,
            snapshot_status,input_digest FROM algorithm_observations WHERE """ + " AND ".join(clauses)
            + " ORDER BY keyword_id,observation_date", parameters).fetchall()]

    def result_digests(self, keyword_id):
        return {row["algorithm_id"]: row["input_digest"] for row in self._db.execute(
            "SELECT algorithm_id,input_digest FROM algorithm_results WHERE keyword_id=?",
            (keyword_id,)).fetchall()}

    def result_digests_for(self, keyword_ids):
        identifiers = tuple(dict.fromkeys(keyword_ids))
        if not identifiers:
            return {}
        placeholders = ",".join("?" for _ in identifiers)
        rows = self._db.execute(
            "SELECT keyword_id,algorithm_id,input_digest FROM algorithm_results WHERE keyword_id IN ("
            + placeholders + ")", identifiers,
        ).fetchall()
        result = {}
        for row in rows:
            result.setdefault(row["keyword_id"], {})[row["algorithm_id"]] = row["input_digest"]
        return result

    def result_payloads_for(self, keyword_ids, algorithm_id):
        identifiers = tuple(dict.fromkeys(keyword_ids))
        if not identifiers:
            return {}
        placeholders = ",".join("?" for _ in identifiers)
        rows = self._db.execute(
            "SELECT keyword_id,result_json FROM algorithm_results WHERE keyword_id IN ("
            + placeholders + ") AND algorithm_id=?",
            (*identifiers, algorithm_id),
        ).fetchall()
        return {row["keyword_id"]: row["result_json"] for row in rows}

    def upsert_results(self, results):
        self._db.executemany("""INSERT INTO algorithm_results(keyword_id,algorithm_id,version,status,
            reason_code,value,metric,unit,result_json,input_digest,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(keyword_id,algorithm_id) DO UPDATE SET version=excluded.version,
            status=excluded.status,reason_code=excluded.reason_code,value=excluded.value,
            metric=excluded.metric,unit=excluded.unit,result_json=excluded.result_json,
            input_digest=excluded.input_digest,updated_at=excluded.updated_at""", results)

    def set_setting(self, key, value):
        self._db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))

    def delete_setting(self, key):
        self._db.execute("DELETE FROM settings WHERE key=?", (key,))
