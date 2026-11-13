"""Conservative, explainable local semantic matching and fetch coverage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import struct
import uuid
from collections import Counter

from app.collections import embeddings
from app.collections.store import (
    CollectionStore,
    fingerprint,
    keyword_identity,
    keyword_record,
    now,
)

ASSIGN_THRESHOLD = 0.46
ASSIGN_MARGIN = 0.06
PROPOSAL_THRESHOLD = 0.82
EXCLUSION_THRESHOLD = 0.46
MIN_PROPOSAL_SUPPORT = 3
MAX_AUTO_CREATE_PER_FETCH = 2


def cosine(left, right):
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(value * value for value in left))
    norm_right = math.sqrt(sum(value * value for value in right))
    return dot / (norm_left * norm_right) if norm_left and norm_right else 0.0


class CollectionService:
    def __init__(self, store: CollectionStore):
        self.store = store

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
        with self.store.connect() as db:
            prior = db.execute("SELECT * FROM fetch_jobs WHERE fetch_id=?", (fetch_id,)).fetchone()
            if prior and prior["status"] in {"COMPLETED", "MODEL_UNAVAILABLE"}:
                return dict(prior)
            db.execute("INSERT OR REPLACE INTO fetch_jobs(fetch_id,status,updated_at) VALUES(?,?,?)",
                       (fetch_id, "PROCESSING", now()))
        records, skipped, rejected = [], 0, []
        available = sum(len(source.get("records", [])) for source in sources if isinstance(source.get("records"), list))
        for source in sources:
            source_id, mode = source.get("source_id", ""), source.get("data_mode", "")
            for raw in source.get("records", []):
                record = keyword_record(raw, source_id, mode)
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
                evidence=excluded.evidence,eligible=1,rejection_reason=NULL""",
                [tuple(record[key] for key in ("id", "keyword", "normalized", "source", "project",
                    "observed_at", "data_mode", "evidence", "context")) for record in records])
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
            collection_texts = [self._collection_text(item) for item in collections]
            collection_vectors = self._embed_cached(
                collection_texts, [item["version"] for item in collections]
            ) if collection_texts else []
            exclusion_inputs = [example for item in collections for example in item["exclusions"]]
            exclusion_vectors = self._embed_cached(exclusion_inputs, [0] * len(exclusion_inputs)) if exclusion_inputs else []
        except Exception:
            with self.store.connect() as db:
                db.execute("""INSERT OR REPLACE INTO fetch_jobs(fetch_id,status,available,processed,skipped,
                    error_code,updated_at) VALUES(?,?,?,?,?,?,?)""",
                    (fetch_id, "MODEL_UNAVAILABLE", available, 0, skipped,
                     "LOCAL_MODEL_UNAVAILABLE", now()))
            return {"fetch_id": fetch_id, "status": "MODEL_UNAVAILABLE", "available": available,
                    "processed": 0, "assigned": 0, "unassigned": len(records), "skipped": skipped}

        assigned = 0
        unassigned_rows = []
        with self.store.connect() as db:
            for record, vector, topic_vector in zip(records, vectors, topic_vectors):
                scores = []
                exclusion_cursor = 0
                for item, collection_vector in zip(collections, collection_vectors):
                    exclusion_start = exclusion_cursor
                    exclusion_cursor += len(item["exclusions"])
                    if not item["auto_match"]:
                        continue
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
                    score = cosine(vector, collection_vector)
                    scores.append((score, item))
                scores.sort(key=lambda value: value[0], reverse=True)
                if scores and scores[0][0] >= ASSIGN_THRESHOLD and (
                    len(scores) == 1 or scores[0][0] - scores[1][0] >= ASSIGN_MARGIN
                ):
                    score, item = scores[0]
                    reason = f"LOCAL_AI semantic cosine={score:.3f}; threshold={ASSIGN_THRESHOLD:.2f}; source context included"
                    db.execute("""INSERT OR IGNORE INTO memberships VALUES(?,?, 'LOCAL_AI',0,?,?,?)""",
                               (item["id"], record["id"], score, reason, now()))
                    if db.execute("SELECT changes()").fetchone()[0]:
                        assigned += 1
                else:
                    unassigned_rows.append((record, topic_vector))
        created, suggestions = self._propose(fetch_id, unassigned_rows, collections)
        outcome = {"fetch_id": fetch_id, "status": "COMPLETED", "available": available,
                   "processed": len(records), "assigned": assigned,
                   "unassigned": max(0, len(records) - assigned), "created_collections": created,
                   "suggestions": suggestions, "skipped": skipped}
        with self.store.connect() as db:
            db.execute("""INSERT OR REPLACE INTO fetch_jobs(fetch_id,status,available,processed,assigned,
                unassigned,created_collections,suggestions,skipped,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (fetch_id, "COMPLETED", outcome["available"], outcome["processed"], assigned,
                 outcome["unassigned"], created, suggestions, skipped, now()))
        return outcome

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
                else:
                    pending.append(text)
                    keys.append((index, key, text_hash, version))
        if pending:
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
        return f"Topic: {record['keyword']}. Wikimedia evidence context: {record['context']}."

    @staticmethod
    def _topic_text(record):
        return f"Wikimedia topic: {record['keyword']}."

    @staticmethod
    def _collection_text(item):
        examples = "; ".join((item["examples"] + item.get("member_examples", []))[:120])
        return f"Collection topic: {item['name']}. Context: {item['description']}. Examples: {examples}."

    def _propose(self, fetch_id, unassigned, collections):
        if len(unassigned) < MIN_PROPOSAL_SUPPORT:
            return 0, 0
        with self.store.connect() as db:
            setting = db.execute("SELECT value FROM settings WHERE key='creation_mode'").fetchone()
        mode = setting["value"] if setting else "SUGGEST"
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
            if len(group) >= MIN_PROPOSAL_SUPPORT:
                groups.append(group)
        created = suggestions = 0
        for group in groups[:MAX_AUTO_CREATE_PER_FETCH]:
            titles = [row[0]["keyword"] for row in group]
            name = self._representative_name(titles)
            key = fingerprint(name)
            if any(fingerprint(item["name"]) == key for item in collections):
                continue
            if self._near_duplicate_name(name, collections):
                continue
            support = [row[0]["id"] for row in group]
            description = "Related Wikimedia pageview topics grouped by local semantic similarity: " + "; ".join(titles[:5])
            reason = f"{len(group)} unassigned keyword records formed a local semantic group in fetch {fetch_id}."
            if mode == "AUTO_CREATE":
                identifier = self.store.create({"name": name, "description": description,
                    "examples": titles[:5]}, created_by="AI", reason=reason)
                with self.store.connect() as db:
                    for keyword_id in support:
                        db.execute("INSERT OR IGNORE INTO memberships VALUES(?,?, 'LOCAL_AI',0,NULL,?,?)",
                                   (identifier, keyword_id, "Representative keyword in AI-created group", now()))
                created += 1
            else:
                stamp, proposal_id = now(), str(uuid.uuid4())
                with self.store.connect() as db:
                    db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,'PENDING',?,?)",
                               (proposal_id, name, description, json.dumps(support), reason, stamp, stamp))
                suggestions += 1
        return created, suggestions

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
        # Bounded deterministic phrase selection from observed titles only.
        words = Counter()
        for title in titles:
            parts = [part for part in title.replace("_", " ").split() if len(part) > 2]
            for index in range(max(1, len(parts) - 1)):
                phrase = " ".join(parts[index:index + 2])
                if phrase:
                    words[phrase] += 1
        return (words.most_common(1)[0][0] if words else "Related Wikimedia topics")[:80]
