"""Conservative, explainable local semantic matching and fetch coverage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import struct
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date
from threading import local

from app.collections import embeddings
from app.collections.store import (
    CollectionStore,
    fingerprint,
    keyword_identity,
    keyword_record,
    now,
)

ASSIGN_THRESHOLD = 0.68
ASSIGN_MARGIN = 0.08
PROPOSAL_THRESHOLD = 0.82
EXCLUSION_THRESHOLD = 0.68
MIN_PROPOSAL_SUPPORT = 2
PAIR_PROPOSAL_THRESHOLD = 0.92

# Small, explicit domain anchors let a named Collection work before the user has
# supplied examples. They are semantic model inputs, never assignment decisions.
COLLECTION_SEMANTIC_ANCHORS = {
    "smart home": "Relevant Smart Home topics include robot vacuum cleaner, smart thermostat, connected appliances, smart speakers, and home automation.",
}


def cosine(left, right):
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(value * value for value in left))
    norm_right = math.sqrt(sum(value * value for value in right))
    return dot / (norm_left * norm_right) if norm_left and norm_right else 0.0


class CollectionService:
    def __init__(self, store: CollectionStore):
        self.store = store
        self._thread_state = local()

    async def process_fetch(self, fetch_id: str, sources: list[dict]):
        try:
            return await asyncio.to_thread(self._process_fetch, fetch_id, sources)
        except Exception:
            try:
                with self.store.connect() as db:
                    db.execute("""INSERT OR REPLACE INTO fetch_jobs(fetch_id,status,error_code,updated_at)
                        VALUES(?,'FAILED','COLLECTION_PROCESSING_FAILED',?)""", (fetch_id, now()))
            except Exception:
                pass
            return {"fetch_id": fetch_id, "status": "FAILED", "error_code": "COLLECTION_PROCESSING_FAILED"}

    def _process_fetch(self, fetch_id, sources):
        self._thread_state.embedding_counts = {"cached_embeddings": 0, "inferred_embeddings": 0}
        with self.store.connect() as db:
            prior = db.execute("SELECT * FROM fetch_jobs WHERE fetch_id=?", (fetch_id,)).fetchone()
            if prior and prior["status"] in {"COMPLETED", "MODEL_UNAVAILABLE"}:
                return dict(prior)
            db.execute("""INSERT OR REPLACE INTO fetch_jobs(fetch_id,status,stage,updated_at)
                VALUES(?,'PROCESSING','CLEANING',?)""", (fetch_id, now()))
        records, skipped, rejected = [], 0, []
        available = sum(len(source.get("records", [])) for source in sources if isinstance(source.get("records"), list))
        self._update_job(fetch_id, stage="EXTRACTING_KEYWORDS", available=available, skipped=skipped)
        for source in sources:
            source_id, mode = source.get("source_id", ""), source.get("data_mode", "")
            snapshot_status = source.get("status", "SUCCESS")
            if snapshot_status not in {"SUCCESS", "PARTIAL"}:
                continue
            for raw in source.get("records", []):
                record = keyword_record(raw, source_id, mode, snapshot_status=snapshot_status)
                if record is None:
                    skipped += 1
                    identifier = keyword_identity(raw, source_id)
                    if identifier:
                        from app.sources.wikimedia.cleaning_rules import collection_technical_reason
                        title = raw.get("article_display") or raw.get("canonical_keyword") or raw.get("article_original")
                        reason = collection_technical_reason(raw.get("project", ""), title) or "INVALID_WIKIMEDIA_RECORD"
                        rejected.append((identifier, reason))
                else:
                    records.append(record)
        if rejected:
            with self.store.connect() as db:
                db.executemany("UPDATE keywords SET eligible=0,rejection_reason=? WHERE id=?",
                               [(reason, identifier) for identifier, reason in rejected])
        # Keep cleaned source evidence available for manual work or an inference retry.
        with self.store.connect() as db:
            db.executemany("""INSERT INTO keywords(id,keyword,normalized,source,project,observed_at,
                data_mode,evidence,context,eligible,rejection_reason) VALUES(?,?,?,?,?,?,?,?,?,1,NULL)
                ON CONFLICT(id) DO UPDATE SET keyword=excluded.keyword,observed_at=excluded.observed_at,
                evidence=excluded.evidence,
                eligible=COALESCE((SELECT CASE decision WHEN 'EXCLUDED' THEN 0 ELSE 1 END
                    FROM keyword_decisions WHERE keyword_id=excluded.id),1),
                rejection_reason=CASE WHEN (SELECT decision FROM keyword_decisions
                    WHERE keyword_id=excluded.id)='EXCLUDED' THEN 'USER_EXCLUDED' ELSE NULL END""",
                [tuple(record[key] for key in ("id", "keyword", "normalized", "source", "project",
                    "observed_at", "data_mode", "evidence", "context")) for record in records])
        self._update_job(fetch_id, stage="ANALYZING_SEMANTIC_MEANING", skipped=skipped)
        if sources and not fetch_id.startswith("organize-"):
            self._persist_dataset_snapshot(fetch_id, sources, records, available, skipped)
        analysis = self._analyze_persisted_keywords(records)
        # The source response can be capped or paginated. Classification operates on every
        # eligible persisted keyword still awaiting a decision, not only this response or UI page.
        with self.store.connect() as db:
            persisted = db.execute("""SELECT k.* FROM keywords k WHERE k.eligible=1
                ORDER BY k.observed_at DESC,k.keyword COLLATE NOCASE""").fetchall()
            records = [dict(row) for row in persisted]
            eligible = db.execute("SELECT count(*) FROM keywords WHERE eligible=1").fetchone()[0]
        # Do not claim inference succeeded if package/model is missing. Provider Fetch remains complete.
        try:
            collections = self.store.list_collections()
            with self.store.connect() as db:
                for item in collections:
                    item["member_examples"] = [row["keyword"] for row in db.execute(
                        "SELECT k.keyword FROM memberships m JOIN keywords k ON k.id=m.keyword_id "
                        "WHERE m.collection_id=? AND m.origin='USER' ORDER BY m.created_at DESC LIMIT 100",
                        (item["id"],))]
            vectors = self._embed_cached([self._candidate_text(row) for row in records], [0] * len(records))
            topic_vectors = self._embed_cached([self._topic_text(row) for row in records], [0] * len(records))
            collection_prototypes = [self._collection_prototypes(item) for item in collections]
            flattened_prototypes = [text for prototypes in collection_prototypes for text in prototypes]
            prototype_versions = [item["version"] for item, prototypes in zip(collections, collection_prototypes)
                                  for _ in prototypes]
            prototype_vectors = self._embed_cached(flattened_prototypes, prototype_versions) if flattened_prototypes else []
            exclusion_inputs = [example for item in collections for example in item["exclusions"]]
            exclusion_vectors = self._embed_cached(exclusion_inputs, [0] * len(exclusion_inputs)) if exclusion_inputs else []
        except Exception:
            with self.store.connect() as db:
                db.execute("UPDATE dataset_snapshots SET processing_status='MODEL_UNAVAILABLE' WHERE dataset_id=?",
                           (fetch_id,))
                db.execute("""INSERT OR REPLACE INTO fetch_jobs(fetch_id,status,available,processed,skipped,
                    error_code,updated_at,stage,eligible,cached_embeddings,inferred_embeddings)
                    VALUES(?,?,?,?,?,?,?,'MODEL_UNAVAILABLE',?,?,?)""",
                    (fetch_id, "MODEL_UNAVAILABLE", available, 0, skipped,
                     "LOCAL_MODEL_UNAVAILABLE", now(), eligible,
                     self._thread_state.embedding_counts["cached_embeddings"],
                     self._thread_state.embedding_counts["inferred_embeddings"]))
            return {"fetch_id": fetch_id, "status": "MODEL_UNAVAILABLE", "available": available,
                    "processed": 0, "assigned": 0, "unassigned": len(records), "skipped": skipped}

        assigned = 0
        unassigned_rows = []
        self._update_job(fetch_id, stage="MATCHING_EXISTING_COLLECTIONS", eligible=eligible,
            processed=len(records), cached_embeddings=self._thread_state.embedding_counts["cached_embeddings"],
            inferred_embeddings=self._thread_state.embedding_counts["inferred_embeddings"])
        self._update_job(fetch_id, stage="ASSIGNING_KEYWORDS")
        with self.store.connect() as db:
            # Recompute automatic memberships from the complete eligible ledger after
            # inference succeeds. Pinned USER memberships and explicit REMOVE feedback stay intact.
            db.execute("DELETE FROM memberships WHERE origin='LOCAL_AI' AND pinned=0")
            for record, vector, topic_vector in zip(records, vectors, topic_vectors):
                scores = []
                fits_existing_collection = False
                exclusion_cursor = 0
                prototype_cursor = 0
                for item, prototype_texts in zip(collections, collection_prototypes):
                    exclusion_start = exclusion_cursor
                    exclusion_cursor += len(item["exclusions"])
                    local_prototypes = prototype_vectors[prototype_cursor:prototype_cursor + len(prototype_texts)]
                    prototype_cursor += len(prototype_texts)
                    feedback = db.execute("SELECT action FROM feedback WHERE collection_id=? AND keyword_id=?",
                                          (item["id"], record["id"])).fetchone()
                    membership = db.execute("SELECT pinned FROM memberships WHERE collection_id=? AND keyword_id=?",
                                            (item["id"], record["id"])).fetchone()
                    if feedback and feedback["action"] == "REMOVE":
                        continue
                    if membership and membership["pinned"]:
                        continue
                    excluded = item["exclusions"]
                    exclusion_match = any(
                        cosine(vector, exclusion_vectors[exclusion_start + offset]) >= EXCLUSION_THRESHOLD
                        for offset in range(len(excluded))
                    )
                    if exclusion_match:
                        continue
                    score = max((cosine(vector, prototype) for prototype in local_prototypes), default=0.0)
                    if score >= ASSIGN_THRESHOLD:
                        fits_existing_collection = True
                    if not item["auto_match"]:
                        continue
                    scores.append((score, item))
                strong = [(score, item) for score, item in scores if score >= ASSIGN_THRESHOLD]
                # A single strong match is unambiguous. Multiple strong matches are retained
                # when each collection independently matches; they are valid many-to-many links.
                # Close matches just above threshold remain review-only unless backed by an
                # explicit positive example or approved keyword prototype.
                scores.sort(key=lambda value: value[0], reverse=True)
                ambiguous = (not strong or (len(strong) > 1
                    and strong[0][0] - strong[1][0] < ASSIGN_MARGIN))
                if not ambiguous:
                    for score, item in strong:
                        reason = (f"LOCAL_AI semantic cosine={score:.3f}; threshold={ASSIGN_THRESHOLD:.2f}; "
                                  "matched collection name, context, example, or approved keyword")
                        db.execute("""INSERT OR IGNORE INTO memberships VALUES(?,?, 'LOCAL_AI',0,?,?,?)""",
                                   (item["id"], record["id"], score, reason, now()))
                        if db.execute("SELECT changes()").fetchone()[0]:
                            assigned += 1
                else:
                    rejected = db.execute("SELECT 1 FROM feedback WHERE keyword_id=? AND action='REMOVE' LIMIT 1",
                                          (record["id"],)).fetchone()
                    if not rejected and not fits_existing_collection:
                        unassigned_rows.append((record, topic_vector))
        self._update_job(fetch_id, stage="DISCOVERING_NEW_TOPICS", assigned=assigned)
        created, suggestions = self._propose(fetch_id, unassigned_rows, collections)
        with self.store.connect() as db:
            assigned = db.execute("SELECT count(DISTINCT keyword_id) FROM memberships WHERE origin='LOCAL_AI'").fetchone()[0]
        outcome = {"fetch_id": fetch_id, "status": "COMPLETED", "available": available,
                   "processed": len(records), "assigned": assigned,
                   "unassigned": self._unassigned_count(), "created_collections": created,
                   "suggestions": suggestions, "skipped": skipped,
                   "eligible": eligible,
                   "cached_embeddings": self._thread_state.embedding_counts["cached_embeddings"],
                   "inferred_embeddings": self._thread_state.embedding_counts["inferred_embeddings"],
                   "algorithms": analysis}
        self._update_job(fetch_id, stage="SAVING_RESULTS", assigned=assigned, created_collections=created,
            unassigned=outcome["unassigned"])
        self._update_job(fetch_id, stage="COMPLETED", status="COMPLETED", available=available,
            eligible=eligible, processed=outcome["processed"], assigned=assigned,
            unassigned=outcome["unassigned"], created_collections=created,
            suggestions=suggestions, skipped=skipped,
            cached_embeddings=self._thread_state.embedding_counts["cached_embeddings"],
            inferred_embeddings=self._thread_state.embedding_counts["inferred_embeddings"])
        with self.store.connect() as db:
            db.execute("""UPDATE dataset_snapshots SET processing_status='COMPLETED',
                eligible_keyword_count=(SELECT count(*) FROM keywords WHERE eligible=1),
                assigned_keyword_count=(SELECT count(DISTINCT keyword_id) FROM memberships),
                unassigned_keyword_count=(SELECT count(*) FROM keywords k WHERE eligible=1 AND NOT EXISTS
                    (SELECT 1 FROM memberships m WHERE m.keyword_id=k.id)) WHERE dataset_id=?""", (fetch_id,))
        return outcome

    def _update_job(self, fetch_id, **values):
        allowed = {"status", "stage", "available", "eligible", "processed", "assigned", "unassigned",
                   "created_collections", "suggestions", "skipped", "cached_embeddings", "inferred_embeddings"}
        changes = {key: value for key, value in values.items() if key in allowed}
        assignments = [f"{key}=?" for key in changes]
        parameters = list(changes.values())
        assignments.append("updated_at=?")
        parameters.extend([now(), fetch_id])
        with self.store.connect() as db:
            db.execute(f"UPDATE fetch_jobs SET {','.join(assignments)} WHERE fetch_id=?", parameters)

    def _persist_dataset_snapshot(self, fetch_id, sources, records, available, skipped):
        manifests, flags, modes = [], [], set()
        provider_count = 0
        for source in sources:
            raw_records = source.get("records", []) if isinstance(source.get("records"), list) else []
            status = source.get("status", "SUCCESS")
            declared = source.get("record_count")
            if type(declared) is not int or declared < len(raw_records):
                declared = len(raw_records)
            provider_count += declared
            if source.get("data_mode"):
                modes.add(source["data_mode"])
            if status != "SUCCESS":
                flags.append(f"{source.get('source_id', 'UNKNOWN')}_{status}")
            if declared > len(raw_records):
                flags.append(f"{source.get('source_id', 'UNKNOWN')}_RECORDS_OMITTED")
            source_flags = source.get("quality_flags")
            if isinstance(source_flags, list):
                flags.extend(str(value) for value in source_flags)
            elif source_flags:
                flags.append(str(source_flags))
            manifests.append({key: source.get(key) for key in (
                "source_id", "status", "data_mode", "record_count", "coverage", "quality_flags",
                "error_code", "retry_after_seconds") if source.get(key) is not None})
        if skipped:
            flags.append("CLEANING_REJECTIONS")
        dates, fetched = [], []
        for record in records:
            try:
                evidence = json.loads(record["evidence"])
                if evidence.get("observation_date"):
                    dates.append(evidence["observation_date"][:10])
                if evidence.get("fetched_at"):
                    fetched.append(evidence["fetched_at"])
            except (TypeError, ValueError):
                continue
        with self.store.connect() as db:
            eligible = db.execute("SELECT count(*) FROM keywords WHERE eligible=1").fetchone()[0]
            assigned = db.execute("SELECT count(DISTINCT keyword_id) FROM memberships").fetchone()[0]
            unassigned = db.execute("""SELECT count(*) FROM keywords k WHERE eligible=1 AND NOT EXISTS
                (SELECT 1 FROM memberships m WHERE m.keyword_id=k.id)""").fetchone()[0]
            db.execute("""INSERT OR REPLACE INTO dataset_snapshots(dataset_id,status,data_mode,source_manifest,
                provider_record_count,received_record_count,retained_keyword_count,rejected_record_count,
                observation_start,observation_end,fetched_at,quality_flags,processing_status,
                eligible_keyword_count,assigned_keyword_count,unassigned_keyword_count,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fetch_id, "FAILED" if sources and all(
                    source.get("status") not in {"SUCCESS", "PARTIAL"} for source in sources
                ) else "PARTIAL" if flags else "COMPLETED",
                 next(iter(modes)) if len(modes) == 1 else "MIXED" if modes else "UNKNOWN",
                 json.dumps(manifests, ensure_ascii=False, sort_keys=True), provider_count, available,
                 len(records), skipped, min(dates) if dates else None, max(dates) if dates else None,
                 max(fetched) if fetched else None, json.dumps(sorted(set(flags))), "PROCESSING",
                 eligible, assigned, unassigned, now()))

    def _analyze_persisted_keywords(self, changed_records):
        """Persist observation facts and execute the registry across every eligible keyword."""
        from app.algorithms.contracts import AlgorithmInput, Observation, Scope
        from app.composition import algorithm_engine

        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM keywords WHERE eligible=1 ORDER BY id").fetchall()
            records = [dict(row) for row in rows]
            for record in changed_records:
                evidence = json.loads(record["evidence"])
                try:
                    observed = date.fromisoformat(evidence.get("observation_date") or record["observed_at"][:10])
                except (TypeError, ValueError):
                    continue
                metric, unit = "views_ceil", "approx_pageviews_upper_bound"
                snapshot_status = evidence.get("snapshot_status", "SUCCESS")
                value, rank = evidence.get("views_ceil"), evidence.get("provider_rank")
                if type(value) is not int or value < 0:
                    continue
                digest = hashlib.sha256(json.dumps([record["id"], observed.isoformat(), value, rank,
                    snapshot_status, record["source"], record["project"]], separators=(",", ":")).encode()).hexdigest()
                db.execute("""INSERT INTO algorithm_observations(keyword_id,source,observation_date,
                    country,access,project,metric,unit,status,value,rank,snapshot_status,input_digest)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(keyword_id,source,observation_date,metric,unit)
                    DO UPDATE SET value=excluded.value,rank=excluded.rank,status=excluded.status,
                    snapshot_status=excluded.snapshot_status,input_digest=excluded.input_digest""",
                    (record["id"], record["source"], observed.isoformat(), evidence.get("country_scope") or "US",
                     evidence.get("access") or "all-access", record["project"], metric, unit,
                     "OBSERVED", value, rank, snapshot_status, digest))
            records = [dict(row) for row in db.execute("SELECT * FROM keywords WHERE eligible=1 ORDER BY id")]
            engine = algorithm_engine()
            keys = engine.registry.keys()
            snapshot_cohorts = defaultdict(list)
            for peer in records:
                try:
                    peer_evidence = json.loads(peer["evidence"])
                    peer_date = peer_evidence.get("observation_date") or peer["observed_at"][:10]
                    peer_key = (peer["source"], peer_evidence.get("country_scope") or "US",
                        peer_evidence.get("access") or "all-access", peer_date, "views_ceil",
                        "approx_pageviews_upper_bound")
                    peer_value = peer_evidence.get("views_ceil")
                    if type(peer_value) is int and peer_value >= 0:
                        peer_scope = Scope(peer["source"], peer_key[1], peer_key[2], peer["project"],
                            peer_evidence.get("article_original") or peer["keyword"], peer_key[4], peer_key[5])
                        snapshot_cohorts[peer_key].append((peer["id"], Observation(
                            date.fromisoformat(peer_date), peer_scope, "OBSERVED", peer_value,
                        peer_evidence.get("snapshot_status", "SUCCESS"), peer_evidence.get("provider_rank"))))
                except (TypeError, ValueError):
                    continue
            executed = unchanged = recomputed_algorithms = 0
            for record in records:
                try:
                    evidence = json.loads(record["evidence"])
                    analysis_date = date.fromisoformat(evidence.get("observation_date") or record["observed_at"][:10])
                    metric, unit = "views_ceil", "approx_pageviews_upper_bound"
                    scope = Scope(record["source"], evidence.get("country_scope") or "US",
                        evidence.get("access") or "all-access", record["project"],
                        evidence.get("article_original") or record["keyword"], metric, unit)
                    history_rows = db.execute("""SELECT * FROM algorithm_observations WHERE keyword_id=?
                        AND source=? AND observation_date<? AND metric=? AND unit=? ORDER BY observation_date""",
                        (record["id"], record["source"], analysis_date.isoformat(), metric, unit)).fetchall()
                    history = tuple(Observation(date.fromisoformat(point["observation_date"]), scope,
                        point["status"], point["value"], point["snapshot_status"]) for point in history_rows)
                    current = evidence.get("views_ceil")
                    cohort_key = (record["source"], scope.country, scope.access,
                        analysis_date.isoformat(), metric, unit)
                    cohort = tuple(point for keyword_id, point in snapshot_cohorts.get(cohort_key, ())
                                   if keyword_id != record["id"])
                    item = AlgorithmInput(record["id"], scope, analysis_date, current, history,
                        window_days=7, minimum_samples=3, evidence_url=evidence.get("source_url"),
                        current_snapshot_status=evidence.get("snapshot_status", "SUCCESS"), cohort=cohort)
                    input_data = {"scope": asdict(scope),
                        "date": analysis_date.isoformat(), "current": current,
                        "current_snapshot_status": evidence.get("snapshot_status", "SUCCESS"),
                        "history": [(str(point.date), point.status, point.value, point.snapshot_status) for point in history],
                        "cohort": sorted((point.scope.project, point.scope.article, point.value, point.rank)
                                         for point in cohort)}
                    existing = {row["algorithm_id"]: row["input_digest"] for row in db.execute(
                        "SELECT algorithm_id,input_digest FROM algorithm_results WHERE keyword_id=?",
                        (record["id"],))}
                    digest_by_key = {}
                    missing_keys = []
                    for key in keys:
                        metadata = engine.registry.get(key).metadata
                        digest_input = {"input": dict(input_data), "algorithm": asdict(metadata)}
                        if metadata.family != "popularity":
                            digest_input["input"].pop("cohort")
                        digest = hashlib.sha256(json.dumps(digest_input, sort_keys=True,
                            separators=(",", ":")).encode()).hexdigest()
                        digest_by_key[key] = digest
                        if existing.get(metadata.algorithm_id) != digest:
                            missing_keys.append(key)
                    if not missing_keys:
                        unchanged += 1
                        continue
                    for result in engine.run(item, missing_keys):
                        payload = json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
                        result_digest = digest_by_key[f"{result.algorithm_id}@{result.algorithm_version}"]
                        db.execute("""INSERT INTO algorithm_results(keyword_id,algorithm_id,version,status,
                            reason_code,value,metric,unit,result_json,input_digest,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(keyword_id,algorithm_id) DO UPDATE SET version=excluded.version,
                            status=excluded.status,reason_code=excluded.reason_code,value=excluded.value,
                            metric=excluded.metric,unit=excluded.unit,result_json=excluded.result_json,
                            input_digest=excluded.input_digest,updated_at=excluded.updated_at""",
                            (record["id"], result.algorithm_id, result.algorithm_version, result.status,
                             result.reason_code, result.value, result.metric_name, result.metric_unit,
                             payload, result_digest, now()))
                    executed += 1
                    recomputed_algorithms += len(missing_keys)
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    continue
        return {"eligible_keywords": len(records), "executed_keywords": executed,
                "unchanged_keywords": unchanged, "recomputed_algorithms": recomputed_algorithms,
                "registered_algorithms": len(keys)}

    def _embed_cached(self, texts, versions):
        results, pending, keys = [None] * len(texts), [], []
        with self.store.connect() as db:
            for index, (text, version) in enumerate(zip(texts, versions)):
                text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                key = hashlib.sha256(f"{embeddings.MODEL_VERSION}:{version}:{text_hash}".encode()).hexdigest()
                row = db.execute("SELECT vector,dimension FROM cache WHERE fingerprint=? AND model_version=? AND collection_version=? AND text_hash=?",
                    (key, embeddings.MODEL_VERSION, version, text_hash)).fetchone()
                if row:
                    results[index] = list(struct.unpack(f"<{row['dimension']}f", row["vector"]))
                    self._thread_state.embedding_counts["cached_embeddings"] += 1
                else:
                    pending.append(text)
                    keys.append((index, key, text_hash, version))
        if pending:
            self._thread_state.embedding_counts["inferred_embeddings"] += len(pending)
            vectors = []
            for offset in range(0, len(pending), 256):
                vectors.extend(embeddings.embed(pending[offset:offset + 256]))
            with self.store.connect() as db:
                for (index, key, text_hash, version), vector in zip(keys, vectors):
                    results[index] = vector
                    db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?,?)",
                        (key, embeddings.MODEL_VERSION, version, text_hash,
                         struct.pack(f"<{len(vector)}f", *vector), len(vector)))
        return results

    @staticmethod
    def _candidate_text(record):
        # Provider/project metadata describes provenance, not the keyword's subject.
        # Keep it in evidence and avoid diluting topic semantics with repetitive context.
        return f"Topic: {record['keyword']}."

    @staticmethod
    def _topic_text(record):
        return f"Wikimedia topic: {record['keyword']}."

    @staticmethod
    def _collection_text(item):
        examples = "; ".join((item["examples"] + item.get("member_examples", []))[:120])
        return f"Collection topic: {item['name']}. Context: {item['description']}. Examples: {examples}."

    @staticmethod
    def _collection_prototypes(item):
        """Keep names, descriptions, examples, and prior approvals individually searchable.

        Embedding one long concatenated description can dilute an exact positive example.
        Each prototype still goes through the real local model and is version-cached.
        """
        prototypes = [f"Collection topic: {item['name']}. Context: {item['description']}."]
        anchor = COLLECTION_SEMANTIC_ANCHORS.get(" ".join(item["name"].casefold().split()))
        if anchor:
            prototypes.append(anchor)
        prototypes.extend(f"Relevant keyword example: {example}." for example in item["examples"])
        prototypes.extend(f"Previously approved keyword: {example}." for example in item.get("member_examples", []))
        return list(dict.fromkeys(text for text in prototypes if text.strip()))

    def _unassigned_count(self):
        with self.store.connect() as db:
            return db.execute("""SELECT count(*) FROM keywords k
                WHERE k.eligible=1 AND NOT EXISTS (
                    SELECT 1 FROM memberships m WHERE m.keyword_id=k.id
                )""").fetchone()[0]

    async def reclassify_unassigned(self, fetch_id: str | None = None):
        """Run cached local matching after Collection creation/edits without a provider fetch."""
        identifier = fetch_id or f"reclassify-{uuid.uuid4()}"
        return await asyncio.to_thread(self._process_fetch, identifier, [])

    def _propose(self, fetch_id, unassigned, collections):
        if len(unassigned) < MIN_PROPOSAL_SUPPORT:
            return 0, 0
        with self.store.connect() as db:
            setting = db.execute("SELECT value FROM settings WHERE key='creation_mode'").fetchone()
        mode = setting["value"] if setting else "AUTO_CREATE"
        if mode == "OFF":
            return 0, 0
        # Require a cohesive group: every pair must clear the reviewed cosine cutoff.
        groups, used = [], set()
        for index, (record, vector) in enumerate(unassigned[:256]):
            if index in used:
                continue
            group = [(record, vector)]
            for other_index in range(index + 1, min(len(unassigned), 256)):
                candidate_vector = unassigned[other_index][1]
                if other_index not in used and all(
                    cosine(member_vector, candidate_vector) >= PROPOSAL_THRESHOLD
                    for _, member_vector in group
                ):
                    used.add(other_index)
                    group.append(unassigned[other_index])
            if len(group) >= MIN_PROPOSAL_SUPPORT and (len(group) > 2 or all(
                cosine(group[0][1], member[1]) >= PAIR_PROPOSAL_THRESHOLD for member in group[1:]
            )):
                groups.append(group)
        created = suggestions = 0
        for group in groups:
            titles = [row[0]["keyword"] for row in group]
            if self._contains_platform_noise(titles):
                continue
            name = self._representative_name(titles)
            if len(name.strip()) < 3:
                continue
            key = fingerprint(name)
            if any(fingerprint(item["name"]) == key for item in collections):
                continue
            if self._near_duplicate_name(name, collections):
                continue
            support = [row[0]["id"] for row in group]
            description = self._group_description(name, titles)
            reason = f"{len(group)} unassigned keyword records formed a local semantic group in fetch {fetch_id}."
            if mode == "AUTO_CREATE":
                identifier = self.store.create({"name": name, "description": description,
                    "examples": titles[:5]}, created_by="AI", reason=reason)
                assignment_reason = f"Grouped with {len(group)} locally matched topics: " + "; ".join(titles[:3])
                with self.store.connect() as db:
                    for keyword_id in support:
                        db.execute("INSERT OR IGNORE INTO memberships VALUES(?,?, 'LOCAL_AI',0,NULL,?,?)",
                                   (identifier, keyword_id, assignment_reason, now()))
                created += 1
            elif mode == "SUGGEST_ONLY":
                stamp, proposal_id = now(), str(uuid.uuid4())
                with self.store.connect() as db:
                    db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,'PENDING',?,?)",
                               (proposal_id, name, description, json.dumps(support), reason, stamp, stamp))
                suggestions += 1
        return created, suggestions

    @staticmethod
    def _group_description(name, titles):
        examples = ", ".join(titles[:4])
        return f"{name} topics, including {examples}. Identified from saved keyword evidence."

    @staticmethod
    def _contains_platform_noise(titles):
        platform_terms = ("mediawiki", "wikimedia api", "wikipedia", "wiktionary", "wikisource",
                          "wikibooks", "wikifunctions")
        return any(any(term in title.casefold() for term in platform_terms) for title in titles)

    def _near_duplicate_name(self, name, collections):
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM suppression WHERE fingerprint=?", (fingerprint(name),)).fetchone():
                return True
            labels = [row["name"] for row in db.execute("SELECT name FROM proposals WHERE status='PENDING'")]
            labels.extend(row["label"] for row in db.execute("SELECT label FROM suppression WHERE label<>''"))
            labels.extend(row["name"] for row in db.execute("SELECT name FROM collections WHERE status='ACTIVE'"))
        labels.extend(item["name"] for item in collections)
        labels = list(dict.fromkeys(labels))[:500]
        if any(self._name_overlap(name, existing) >= 0.70 for existing in labels):
            return True
        if not labels:
            return False
        vectors = self._embed_cached(
            [f"Collection topic: {value}." for value in [name, *labels]], [0] * (len(labels) + 1)
        )
        return any(cosine(vectors[0], vector) >= 0.84 for vector in vectors[1:])

    @staticmethod
    def _name_overlap(left, right):
        left_tokens = set(" ".join(left.casefold().split()).replace("-", " ").split())
        right_tokens = set(" ".join(right.casefold().split()).replace("-", " ").split())
        union = left_tokens | right_tokens
        return len(left_tokens & right_tokens) / len(union) if union else 0.0

    @staticmethod
    def _representative_name(titles):
        # Use a title phrase shared by the actual group; remove parenthetical metadata
        # such as language or format before finding the common prefix.
        cleaned = [" ".join(title.split("(", 1)[0].replace("_", " ").split()).strip(" -:") for title in titles]
        tokens = [title.split() for title in cleaned]
        prefix = []
        for group in zip(*tokens):
            if len({word.casefold() for word in group}) != 1:
                break
            prefix.append(group[0])
        name = " ".join(prefix).strip(" -:")
        if name.casefold().endswith(" minus"):
            name = name.rsplit(" ", 1)[0]
        if name and all("film" in title.casefold() or "movie" in title.casefold() for title in titles):
            name = f"{name} films"
        if len(name) < 3:
            # Fall back to a repeated observed bigram, never a fabricated generic label.
            phrases = Counter()
            for title in cleaned:
                parts = [part for part in title.split() if len(part) > 2]
                for index in range(max(1, len(parts) - 1)):
                    phrase = " ".join(parts[index:index + 2])
                    if phrase:
                        phrases[phrase] += 1
            shared = [(phrase, count) for phrase, count in phrases.items() if count >= len(titles)]
            name = max(shared, key=lambda item: (len(item[0].split()), len(item[0])))[0] if shared else ""
        return name[:80]
