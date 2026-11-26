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
from datetime import date, datetime, timezone
from threading import local

from app.collections import embeddings
from app.collections.source_digest import (
    COLLECTION_INPUT_DIGEST_VERSION,
    source_input_digest,
)
from app.collections.source_lineage import (
    build_source_evidence,
    remap_source_evidence_keyword_ids,
)
from app.collections.store import (
    CollectionStore,
    bing_verification_update,
    fingerprint,
    keyword_identity,
    keyword_record,
    now,
)
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.taxonomy.registry import resolve as resolve_category

# Calibrated against the real local-model Smart Home positive (0.538 with its
# explicit semantic anchor) and unrelated saved topics. The margin and explicit
# negative examples remain additional gates when several Collections fit.
ASSIGN_THRESHOLD = 0.52
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
INITIAL_TOPIC_ROOTS = ("fashion", "home_living", "technology", "entertainment", "sports", "gaming")

_COHORT_MARKER = {"__trendscout_cohort_payload__": True}
_COHORT_MARKER_JSON = b'{"__trendscout_cohort_payload__":true}'


def _serialized_cohort(cohort):
    """Pre-sort the exact JSON rows used by existing algorithm input digests."""
    rows = [(keyword_id, (point.scope.project, point.scope.article, point.value, point.rank))
            for keyword_id, point in cohort]
    rows.sort(key=lambda row: row[1])
    return [(keyword_id, json.dumps(values, separators=(",", ":")).encode())
            for keyword_id, values in rows]


def _cohort_json_index(rows):
    """Index the already sorted JSON so single-row exclusions need no Python scan."""
    positions = {}
    offset = 0
    for identity, value in rows:
        if identity in positions:
            # An identity appearing more than once requires removing every row.
            return None
        positions[identity] = (offset, offset + len(value))
        offset += len(value) + 1
    return b",".join(value for _, value in rows), positions


def _cohort_json_without(rows, keyword_id, index=None):
    if index is None:
        return b"[" + b",".join(value for identity, value in rows if identity != keyword_id) + b"]"
    joined, positions = index
    position = positions.get(keyword_id)
    if position is None:
        return b"[" + joined + b"]"
    start, end = position
    if end < len(joined):
        end += 1
    elif start:
        start -= 1
    return b"[" + joined[:start] + joined[end:] + b"]"


def _algorithm_digest_prefix(metadata):
    return (b'{"algorithm":' + json.dumps(metadata, sort_keys=True,
            separators=(",", ":")).encode() + b',"input":')


def _algorithm_digest_input(input_data, *, popularity, cohort_bytes):
    value = dict(input_data)
    if not popularity:
        value.pop("cohort")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if popularity and cohort_bytes is not None:
        encoded = encoded.replace(b'"cohort":' + _COHORT_MARKER_JSON,
                                  b'"cohort":' + cohort_bytes, 1)
    return encoded


def cosine(left, right):
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(value * value for value in left))
    norm_right = math.sqrt(sum(value * value for value in right))
    return dot / (norm_left * norm_right) if norm_left and norm_right else 0.0


class CollectionService:
    def __init__(self, store: CollectionStore, entity_context_resolver=None, *,
                 fetch_model_inference_enabled=True):
        if type(fetch_model_inference_enabled) is not bool:
            raise TypeError("fetch model inference setting must be boolean")
        self.store = store
        self.entity_context_resolver = entity_context_resolver
        self.fetch_model_inference_enabled = fetch_model_inference_enabled
        self._thread_state = local()

    def _model_unavailable_result(self, fetch_id, *, available, skipped,
                                  policy_excluded, eligible, unassigned,
                                  error_code):
        self.store.mark_fetch_model_unavailable(
            fetch_id, available=available, skipped=skipped + policy_excluded,
            eligible=eligible, error_code=error_code,
            cached_embeddings=self._thread_state.embedding_counts["cached_embeddings"],
            inferred_embeddings=self._thread_state.embedding_counts["inferred_embeddings"],
        )
        return {"fetch_id": fetch_id, "status": "MODEL_UNAVAILABLE", "available": available,
                "processed": 0, "assigned": 0, "unassigned": unassigned,
                "skipped": skipped + policy_excluded, "error_code": error_code}

    async def process_fetch(self, fetch_id: str, sources: list[dict], *,
                            cached_context_only: bool = False,
                            resume_model_unavailable: bool = False):
        try:
            return await asyncio.to_thread(
                self._process_fetch, fetch_id, sources, cached_context_only,
                resume_model_unavailable,
            )
        except Exception:
            try:
                self.store.mark_fetch_failed(fetch_id, "COLLECTION_PROCESSING_FAILED")
            except Exception:
                pass
            return {"fetch_id": fetch_id, "status": "FAILED", "error_code": "COLLECTION_PROCESSING_FAILED"}

    def _process_fetch(self, fetch_id, sources, cached_context_only=False,
                       resume_model_unavailable=False):
        self._thread_state.embedding_counts = {"cached_embeddings": 0, "inferred_embeddings": 0}
        if self.store.data_mode_scope:
            sources = [source for source in sources
                       if source.get("data_mode") == self.store.data_mode_scope]
        google_only_live_fetch = (
            self.store.data_mode_scope == "LIVE"
            and bool(sources)
            and all(source.get("source_id") == GOOGLE_SOURCE_ID for source in sources)
        )
        begin_options = {"cached_context_only": cached_context_only}
        if resume_model_unavailable:
            begin_options["resume_model_unavailable"] = True
        begin = self.store.begin_fetch_processing(fetch_id, sources, **begin_options)
        if begin["action"] == "RETURN":
            return begin["result"]
        retry_existing_snapshot = begin["retry_existing_snapshot"]
        replay_available = begin["replay_available"]
        replay_skipped = begin["replay_skipped"]
        records, rejected, bing_updates = [], [], []
        source_batches, bing_sources = [], []
        incremental_persistence = bool(getattr(
            self.store, "supports_incremental_fetch_persistence", False,
        ))
        entity_context_rows = []
        policy_excluded = 0
        skipped = replay_skipped if retry_existing_snapshot and not sources else 0
        available = (sum(len(source.get("records", [])) for source in sources
                          if isinstance(source.get("records"), list))
                     if sources else replay_available if retry_existing_snapshot else 0)
        self._update_job(fetch_id, stage="EXTRACTING_KEYWORDS", available=available, skipped=skipped)
        for source in sources:
            source_id, mode = source.get("source_id", ""), source.get("data_mode", "")
            if source_id == YOUTUBE_SOURCE_ID:
                raw_records = source.get("records", [])
                policy_excluded += len(raw_records) if isinstance(raw_records, list) else 0
                continue
            snapshot_status = source.get("status", "SUCCESS")
            if snapshot_status not in {"SUCCESS", "PARTIAL"}:
                continue
            source_records = []
            for raw in source.get("records", []):
                if source_id == BING_SOURCE_ID:
                    update = bing_verification_update(raw, mode)
                    if update is None:
                        skipped += 1
                    else:
                        bing_updates.append(update)
                    continue
                record = keyword_record(raw, source_id, mode, snapshot_status=snapshot_status)
                if record is None:
                    skipped += 1
                    identifier = keyword_identity(raw, source_id)
                    if identifier:
                        if source_id == "google_web_serp":
                            reason = "INVALID_GOOGLE_TRENDS_RECORD"
                        else:
                            from app.sources.wikimedia.cleaning_rules import (
                                collection_technical_reason,
                            )
                            title = raw.get("article_display") or raw.get("canonical_keyword") or raw.get("article_original")
                            reason = collection_technical_reason(raw.get("project", ""), title) or "INVALID_WIKIMEDIA_RECORD"
                        rejected.append((identifier, reason))
                else:
                    source_records.append(record)
            if source_id == BING_SOURCE_ID:
                bing_sources.append(source)
                continue
            records.extend(source_records)
            if incremental_persistence:
                source_batches.append((source, source_records))

        keyword_id_map = {}
        if incremental_persistence:
            # Preserve the legacy precedence: rejection updates happen before any
            # valid keyword upsert, while each source's records and lineage commit
            # together in a bounded, idempotent transaction.
            if rejected:
                rejection_result = self.store.persist_cleaned_keywords([], rejected=rejected)
                keyword_id_map.update(rejection_result.get("id_remap", {}))
                skipped += rejection_result["skipped_bing_updates"]
            persisted_records = []
            supports_lineage = getattr(self.store, "supports_source_evidence_lineage", False)
            for source, source_records in source_batches:
                evidence_rows = []
                if supports_lineage:
                    evidence_rows = build_source_evidence([source])["rows"]
                persisted = self.store.persist_cleaned_keywords(
                    source_records, source_evidence_rows=evidence_rows,
                )
                persisted_records.extend(persisted["records"])
                keyword_id_map.update(persisted.get("id_remap", {}))
                skipped += persisted["skipped_bing_updates"]
            records = persisted_records

            bing_evidence_rows = []
            if supports_lineage and bing_sources:
                bing_evidence_rows = build_source_evidence(
                    bing_sources, bing_updates=bing_updates,
                )["rows"]
            if bing_updates or bing_evidence_rows:
                bing_result = self.store.persist_cleaned_keywords(
                    [], bing_updates=bing_updates,
                    source_evidence_rows=bing_evidence_rows,
                )
                keyword_id_map.update(bing_result.get("id_remap", {}))
                skipped += bing_result["skipped_bing_updates"]
        else:
            # The legacy SQLite path keeps its existing single-write behavior until
            # the reconciled migration enables PostgreSQL as the default runtime.
            persisted = self.store.persist_cleaned_keywords(
                records, rejected=rejected, bing_updates=bing_updates,
            )
            records = persisted["records"]
            keyword_id_map.update(persisted.get("id_remap", {}))
            skipped += persisted["skipped_bing_updates"]
        source_keyword_ids = tuple(record["id"] for record in records)
        # Entity context is enriched only on a successful provider update. Organize/replay
        # operations consume persisted context and never trigger a new provider request.
        if sources and self.entity_context_resolver is not None and not cached_context_only:
            try:
                contexts = self.entity_context_resolver.enrich(records)
                if isinstance(contexts, dict):
                    entity_context_rows = [context.as_row() for context in contexts.values()
                                           if callable(getattr(context, "as_row", None))]
            except Exception:
                # Context enrichment is optional evidence; provider Fetch and analysis continue.
                pass
        self._update_job(fetch_id, stage="ANALYZING_SEMANTIC_MEANING",
                         skipped=skipped + policy_excluded)
        if sources and not fetch_id.startswith("organize-"):
            self._persist_dataset_snapshot(
                fetch_id, sources, records, available, skipped, policy_excluded,
                bing_updates=bing_updates, entity_context_rows=entity_context_rows,
                keyword_id_map=keyword_id_map,
            )
        if sources and not records and not fetch_id.startswith("organize-"):
            # A failed or empty source response is still an auditable snapshot,
            # but it must not replay analysis and rewrite timestamps or results
            # for the existing keyword ledger.
            self.store.complete_empty_fetch(fetch_id, {
                "stage": "COMPLETED", "status": "COMPLETED", "available": available,
                "processed": 0, "assigned": 0, "unassigned": 0, "created_collections": 0,
                "suggestions": 0, "skipped": skipped + policy_excluded,
                "cached_embeddings": 0, "inferred_embeddings": 0,
            })
            return {
                "fetch_id": fetch_id, "status": "COMPLETED", "available": available,
                "processed": 0, "assigned": 0, "unassigned": 0,
                "created_collections": 0, "suggestions": 0,
                "skipped": skipped + policy_excluded,
            }
        analysis = self._analyze_persisted_keywords(records)
        # The source response can be capped or paginated. Classification operates on every
        # eligible persisted keyword still awaiting a decision, not only this response or UI page.
        loaded_keywords = self.store.load_keywords_for_fetch(
            source_keyword_ids if google_only_live_fetch or resume_model_unavailable else None,
        )
        records = loaded_keywords["records"]
        eligible = loaded_keywords["eligible"]
        if self.entity_context_resolver is not None:
            contexts = self.entity_context_resolver.cached(record["id"] for record in records)
            for record in records:
                context = contexts.get(record["id"])
                if context is not None:
                    record["entity_type"] = context.entity_type
                    record["entity_description"] = context.description
                    record["entity_id"] = context.entity_id
                    record["entity_category_ids"] = context.category_ids
                    record["entity_occupations"] = context.occupations
                    record["entity_instance_types"] = context.instance_types
                    record["entity_source_url"] = context.source_url
                    record["entity_context_method"] = context.method
                    record["entity_context_status"] = context.status
                    record["entity_context_captured_at"] = context.captured_at
                    record["page_namespace"] = context.page_namespace
                    record["page_is_disambiguation"] = context.is_disambiguation
                    record["page_is_main_page"] = context.is_main_page
                    record["page_is_main_page_known"] = context.is_main_page_known
                    record["redirected_from"] = context.redirected_from
                    record["page_language"] = context.page_language
        excluded_records = loaded_keywords["excluded_records"]
        classification_records = [*records, *excluded_records]
        if self.entity_context_resolver is not None and excluded_records:
            excluded_contexts = self.entity_context_resolver.cached(row["id"] for row in excluded_records)
            for record in excluded_records:
                context = excluded_contexts.get(record["id"])
                if context is not None:
                    record.update(entity_type=context.entity_type, entity_description=context.description,
                        entity_id=context.entity_id, entity_category_ids=context.category_ids,
                        entity_occupations=context.occupations, entity_instance_types=context.instance_types,
                        entity_source_url=context.source_url, entity_context_method=context.method,
                        entity_context_status=context.status, page_namespace=context.page_namespace,
                        page_is_disambiguation=context.is_disambiguation,
                        page_is_main_page=context.is_main_page,
                        page_is_main_page_known=context.is_main_page_known,
                        redirected_from=context.redirected_from, page_language=context.page_language)
        from app.taxonomy.classifier import classify_contextual
        classified_at = now()
        for record in classification_records:
            context = {
                "entity_type": record.get("entity_type", "unknown"),
                "entity_id": record.get("entity_id"),
                "description": record.get("entity_description", ""),
                "occupations": record.get("entity_occupations", ()),
                "instance_types": record.get("entity_instance_types", ()),
                "category_ids": record.get("entity_category_ids", ()),
                "source_url": record.get("entity_source_url", ""),
                "method": record.get("entity_context_method", ""),
                "status": record.get("entity_context_status", ""),
                "page_namespace": record.get("page_namespace"),
                "is_disambiguation": record.get("page_is_disambiguation", False),
                "is_main_page": record.get("page_is_main_page"),
                "is_main_page_known": record.get("page_is_main_page_known", False),
                "redirected_from": record.get("redirected_from", ""),
                "page_language": record.get("page_language", ""),
            }
            result = classify_contextual(record, context)
            result["classified_at"] = classified_at
            record["classification_result"] = result
            if record.get("eligible", 1):
                record["entity_category_ids"] = tuple(result["topic_category_ids"])
        eligible = self.store.persist_classifications(classification_records)
        category_collection_ids = {}
        for category_id in INITIAL_TOPIC_ROOTS:
            self.store.ensure_category_collection(resolve_category(category_id))
        for category_id in sorted({category for record in records
                                   for category in record.get("entity_category_ids", ())}):
            category = resolve_category(category_id)
            def ensure_taxonomy_path(item):
                if item.parent_id:
                    parent = resolve_category(item.parent_id)
                    parent_collection_id = ensure_taxonomy_path(parent)
                    if parent_collection_id is None:
                        return None
                    return self.store.ensure_category_collection(item, parent_id=parent_collection_id)
                return self.store.ensure_category_collection(item)
            category_collection_ids[category_id] = ensure_taxonomy_path(category)
        for record in records:
            record["category_collection_ids"] = tuple(
                category_collection_ids[category_id]
                for category_id in record.get("entity_category_ids", ())
                if category_collection_ids.get(category_id)
            )
        if not self.fetch_model_inference_enabled:
            return self._model_unavailable_result(
                fetch_id, available=available, skipped=skipped,
                policy_excluded=policy_excluded, eligible=eligible,
                unassigned=len(records), error_code="MODEL_INFERENCE_DEFERRED",
            )

        # Do not claim inference succeeded if package/model is missing. Provider Fetch remains complete.
        try:
            collections = self.store.list_collections()
            examples = self.store.user_member_examples([item["id"] for item in collections])
            for item in collections:
                item["member_examples"] = examples.get(item["id"], [])
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
            return self._model_unavailable_result(
                fetch_id, available=available, skipped=skipped,
                policy_excluded=policy_excluded, eligible=eligible,
                unassigned=len(records), error_code="LOCAL_MODEL_UNAVAILABLE",
            )

        unassigned_rows = []
        reconciliation_id = None if google_only_live_fetch else f"reconcile-{uuid.uuid4()}"
        self._update_job(fetch_id, stage="MATCHING_EXISTING_COLLECTIONS", eligible=eligible,
            processed=len(records), cached_embeddings=self._thread_state.embedding_counts["cached_embeddings"],
            inferred_embeddings=self._thread_state.embedding_counts["inferred_embeddings"])
        self._update_job(fetch_id, stage="ASSIGNING_KEYWORDS")
        if reconciliation_id is not None:
            self.store.archive_unpinned_ai_memberships(
                reconciliation_id, f"Contextual full-ledger replay for {fetch_id}"
            )
            self._update_job(fetch_id, membership_reconciliation_id=reconciliation_id)
        guardrails = self.store.membership_guardrails(
            [record["id"] for record in records],
            [category_id for record in records for category_id in record.get("category_collection_ids", ())],
        )
        membership_assignments = []
        for record, vector, topic_vector in zip(records, vectors, topic_vectors):
            if record.get("classification_result", {}).get("classification_status") == "INELIGIBLE":
                continue
            scores = []
            fits_existing_collection = False
            exclusion_cursor = 0
            prototype_cursor = 0
            for item, prototype_texts in zip(collections, collection_prototypes):
                exclusion_start = exclusion_cursor
                exclusion_cursor += len(item["exclusions"])
                local_prototypes = prototype_vectors[prototype_cursor:prototype_cursor + len(prototype_texts)]
                prototype_cursor += len(prototype_texts)
                if item.get("category_id"):
                    continue
                pair = (item["id"], record["id"])
                if pair in guardrails["removed_pairs"] or pair in guardrails["pinned_pairs"]:
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
            for category_collection_id in record.get("category_collection_ids", ()):
                pair = (category_collection_id, record["id"])
                if (category_collection_id not in guardrails["active_category_ids"]
                        or pair in guardrails["removed_pairs"] or pair in guardrails["pinned_pairs"]):
                    continue
                reason = (f"Verified entity context: {record.get('entity_type', 'unknown')}; "
                          f"taxonomy category {record.get('entity_category_ids', ())}")
                membership_assignments.append((category_collection_id, record["id"], None, reason))
                fits_existing_collection = True
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
                    membership_assignments.append((item["id"], record["id"], score, reason))
            else:
                rejected = record["id"] in guardrails["removed_keyword_ids"]
                unresolved_person = record.get("entity_type") == "person" and not record.get("entity_category_ids")
                if not rejected and not fits_existing_collection and not unresolved_person:
                    unassigned_rows.append((record, topic_vector))
        assigned = self.store.replace_local_ai_memberships(
            membership_assignments,
            source_ids=source_keyword_ids if google_only_live_fetch else None,
        )
        self._update_job(fetch_id, stage="DISCOVERING_NEW_TOPICS", assigned=assigned)
        created, suggestions = self._propose(
            reconciliation_id or fetch_id, unassigned_rows, collections
        )
        assigned = self.store.count_ai_assigned_keywords()
        outcome = {"fetch_id": fetch_id, "status": "COMPLETED", "available": available,
                   "processed": len(records), "assigned": assigned,
                   "unassigned": self._unassigned_count(), "created_collections": created,
                   "suggestions": suggestions, "skipped": skipped + policy_excluded,
                   "eligible": eligible,
                   "cached_embeddings": self._thread_state.embedding_counts["cached_embeddings"],
                   "inferred_embeddings": self._thread_state.embedding_counts["inferred_embeddings"],
                   "membership_reconciliation_id": reconciliation_id,
                   "algorithms": analysis}
        self._update_job(fetch_id, stage="SAVING_RESULTS", assigned=assigned, created_collections=created,
            unassigned=outcome["unassigned"])
        self._update_job(fetch_id, stage="COMPLETED", status="COMPLETED", available=available,
            eligible=eligible, processed=outcome["processed"], assigned=assigned,
            unassigned=outcome["unassigned"], created_collections=created,
            suggestions=suggestions, skipped=skipped + policy_excluded,
            cached_embeddings=self._thread_state.embedding_counts["cached_embeddings"],
            inferred_embeddings=self._thread_state.embedding_counts["inferred_embeddings"],
            membership_reconciliation_id=reconciliation_id)
        self.store.complete_fetch_dataset_snapshot(fetch_id)
        return outcome

    def _update_job(self, fetch_id, **values):
        allowed = {"status", "stage", "available", "eligible", "processed", "assigned", "unassigned",
                   "created_collections", "suggestions", "skipped", "cached_embeddings", "inferred_embeddings",
                   "membership_reconciliation_id"}
        changes = {key: value for key, value in values.items() if key in allowed}
        self.store.update_fetch_job(fetch_id, changes)

    def _persist_dataset_snapshot(self, fetch_id, sources, records, available, skipped,
                                  policy_excluded=0, *, bing_updates=(), entity_context_rows=(),
                                  keyword_id_map=None):
        manifests, flags, modes = [], [], set()
        provider_count = 0
        for source_index, source in enumerate(sources):
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
            manifest = {key: source.get(key) for key in (
                "source_id", "status", "data_mode", "delivery_state", "coverage",
                "quality_flags", "error_code", "retry_after_seconds",
            ) if source.get(key) is not None}
            manifest["source_input_digest"] = source_input_digest(
                manifest.get("source_id") if isinstance(manifest.get("source_id"), str)
                else f"UNKNOWN_{source_index}", source
            )
            manifest["source_input_digest_version"] = COLLECTION_INPUT_DIGEST_VERSION
            # Keep source-level counts so a later targeted deletion can redact one
            # provider from mixed Fetch history without guessing aggregate totals.
            manifest["record_count"] = declared
            manifest["received_record_count"] = len(raw_records)
            manifests.append(manifest)
        if skipped:
            flags.append("CLEANING_REJECTIONS")
        if policy_excluded:
            flags.append("YOUTUBE_KEYWORD_EXTRACTION_DISABLED")
        source_evidence_rows = None
        if getattr(self.store, "supports_source_evidence_lineage", False):
            lineage = build_source_evidence(
                sources, bing_updates=bing_updates, entity_context_rows=entity_context_rows,
            )
            source_evidence_rows = remap_source_evidence_keyword_ids(
                lineage["rows"], keyword_id_map,
            )
            if lineage["untracked_record_count"]:
                flags.append("SOURCE_EVIDENCE_LINEAGE_INCOMPLETE")
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
        for source in sources:
            if not isinstance(source, dict) or source.get("status") not in {"SUCCESS", "PARTIAL"}:
                continue
            coverage = source.get("coverage")
            if not isinstance(coverage, dict):
                continue
            source_fetched_at = coverage.get("fetched_at")
            if not isinstance(source_fetched_at, str):
                source_fetched_at = coverage.get("cache_fetched_at")
            if not isinstance(source_fetched_at, str) or len(source_fetched_at) > 64:
                continue
            try:
                timestamp = datetime.fromisoformat(source_fetched_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if timestamp.tzinfo is not None and timestamp.utcoffset() is not None:
                fetched.append(timestamp.astimezone(timezone.utc).isoformat())
        snapshot = {
            "status": ("FAILED" if sources and all(
                source.get("status") not in {"SUCCESS", "PARTIAL"} for source in sources
            ) else "PARTIAL" if flags else "COMPLETED"),
            "data_mode": next(iter(modes)) if len(modes) == 1 else "MIXED" if modes else "UNKNOWN",
            "source_manifest": manifests,
            "provider_record_count": provider_count,
            "received_record_count": available,
            "retained_keyword_count": len(records),
            "rejected_record_count": skipped,
            "observation_start": min(dates) if dates else None,
            "observation_end": max(dates) if dates else None,
            "fetched_at": max(fetched) if fetched else None,
            "quality_flags": sorted(set(flags)),
            "processing_status": "PROCESSING",
            "created_at": now(),
        }
        if source_evidence_rows is not None:
            snapshot["source_evidence_rows"] = source_evidence_rows
        self.store.persist_dataset_snapshot(fetch_id, **snapshot)

    def _analyze_persisted_keywords(self, changed_records):
        """Persist observation facts and execute the registry across every eligible keyword."""
        from app.algorithms.contracts import AlgorithmInput, Observation, Scope
        from app.composition import algorithm_engine

        def analyze(session):
            records = session.eligible_keywords()
            observations = []
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
                observations.append((record["id"], record["source"], observed.isoformat(),
                    evidence.get("country_scope") or "US", evidence.get("access") or "all-access",
                    record["project"], metric, unit, "OBSERVED", value, rank, snapshot_status, digest))
            session.upsert_observations(observations)
            records = session.eligible_keywords()
            engine = algorithm_engine()
            keys = engine.registry.keys()
            metadata_by_key = {key: engine.registry.get(key).metadata for key in keys}
            cache_key = "algorithm_analysis_state_v1:" + (self.store.data_mode_scope or "ALL")
            state_digest = session.state_digest(records, metadata_by_key)
            previous_state = session.setting(cache_key)
            if state_digest is not None and previous_state == state_digest:
                return {"eligible_keywords": len(records), "executed_keywords": 0,
                        "unchanged_keywords": len(records), "recomputed_algorithms": 0,
                        "registered_algorithms": len(keys)}
            metadata_prefix_by_key = {
                key: _algorithm_digest_prefix(asdict(value))
                for key, value in metadata_by_key.items()
            }
            snapshot_cohorts = defaultdict(list)
            for peer in records:
                try:
                    peer_evidence = json.loads(peer["evidence"])
                    peer_date = peer_evidence.get("observation_date") or peer["observed_at"][:10]
                    peer_key = (peer["source"], peer_evidence.get("country_scope") or "US",
                        peer_evidence.get("access") or "all-access", peer_date, "views_ceil",
                        "approx_pageviews_upper_bound", peer["project"])
                    peer_value = peer_evidence.get("views_ceil")
                    if type(peer_value) is int and peer_value >= 0:
                        peer_scope = Scope(peer["source"], peer_key[1], peer_key[2], peer["project"],
                            peer_evidence.get("article_original") or peer["keyword"], peer_key[4], peer_key[5])
                        snapshot_cohorts[peer_key].append((peer["id"], Observation(
                            date.fromisoformat(peer_date), peer_scope, "OBSERVED", peer_value,
                        peer_evidence.get("snapshot_status", "SUCCESS"), peer_evidence.get("provider_rank"))))
                except (TypeError, ValueError):
                    continue
            serialized_cohorts = {}
            cohort_indexes = {}
            for cohort_key, cohort in snapshot_cohorts.items():
                try:
                    serialized_cohorts[cohort_key] = _serialized_cohort(cohort)
                    cohort_indexes[cohort_key] = _cohort_json_index(serialized_cohorts[cohort_key])
                except TypeError:
                    # Preserve the old per-record sort behavior for unusual
                    # mixed values that cannot be globally ordered.
                    pass
            executed = unchanged = recomputed_algorithms = 0
            cacheable = True
            for record in records:
                try:
                    evidence = json.loads(record["evidence"])
                    analysis_date = date.fromisoformat(evidence.get("observation_date") or record["observed_at"][:10])
                    metric, unit = "views_ceil", "approx_pageviews_upper_bound"
                    scope = Scope(record["source"], evidence.get("country_scope") or "US",
                        evidence.get("access") or "all-access", record["project"],
                        evidence.get("article_original") or record["keyword"], metric, unit)
                    history_rows = session.history(
                        record["id"], record["source"], analysis_date.isoformat(), metric, unit
                    )
                    history = tuple(Observation(date.fromisoformat(point["observation_date"]), scope,
                        point["status"], point["value"], point["snapshot_status"]) for point in history_rows)
                    current = evidence.get("views_ceil")
                    cohort_key = (record["source"], scope.country, scope.access,
                        analysis_date.isoformat(), metric, unit, record["project"])
                    peers = snapshot_cohorts.get(cohort_key, ())
                    serialized = serialized_cohorts.get(cohort_key)
                    if serialized is None:
                        cohort = tuple(point for keyword_id, point in peers if keyword_id != record["id"])
                        cohort_input = sorted((point.scope.project, point.scope.article, point.value, point.rank)
                                              for point in cohort)
                        cohort_bytes = None
                    else:
                        cohort = None
                        cohort_input = _COHORT_MARKER
                        cohort_bytes = _cohort_json_without(
                            serialized, record["id"], cohort_indexes[cohort_key]
                        )
                    input_data = {"scope": asdict(scope),
                        "date": analysis_date.isoformat(), "current": current,
                        "current_snapshot_status": evidence.get("snapshot_status", "SUCCESS"),
                        "history": [(str(point.date), point.status, point.value, point.snapshot_status) for point in history],
                        "cohort": cohort_input}
                    existing = session.result_digests(record["id"])
                    digest_by_key = {}
                    missing_keys = []
                    encoded_inputs = {}
                    for key in keys:
                        metadata = metadata_by_key[key]
                        popularity = metadata.family == "popularity"
                        if popularity not in encoded_inputs:
                            encoded_inputs[popularity] = _algorithm_digest_input(
                                input_data, popularity=popularity, cohort_bytes=cohort_bytes
                            )
                        encoded = metadata_prefix_by_key[key] + encoded_inputs[popularity] + b"}"
                        digest = hashlib.sha256(encoded).hexdigest()
                        digest_by_key[key] = digest
                        if existing.get(metadata.algorithm_id) != digest:
                            missing_keys.append(key)
                    if not missing_keys:
                        unchanged += 1
                        continue
                    if cohort is None:
                        cohort = tuple(point for keyword_id, point in peers if keyword_id != record["id"])
                    item = AlgorithmInput(record["id"], scope, analysis_date, current, history,
                        window_days=7, minimum_samples=3, evidence_url=evidence.get("source_url"),
                        current_snapshot_status=evidence.get("snapshot_status", "SUCCESS"), cohort=cohort)
                    produced_keys = []
                    persisted_results = []
                    for result in engine.run(item, missing_keys):
                        payload = json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
                        result_key = f"{result.algorithm_id}@{result.algorithm_version}"
                        result_digest = digest_by_key[result_key]
                        produced_keys.append(result_key)
                        persisted_results.append((record["id"], result.algorithm_id,
                            result.algorithm_version, result.status, result.reason_code, result.value,
                            result.metric_name, result.metric_unit, payload, result_digest, now()))
                    session.upsert_results(persisted_results)
                    executed += 1
                    recomputed_algorithms += len(missing_keys)
                    if len(produced_keys) != len(missing_keys) or set(produced_keys) != set(missing_keys):
                        cacheable = False
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    cacheable = False
                    continue
            if cacheable:
                # Results may have been inserted above, so fingerprint the final
                # state before this store transaction is committed.
                final_digest = session.state_digest(records, metadata_by_key)
                if final_digest is not None:
                    session.set_setting(cache_key, final_digest)
            elif previous_state is not None:
                session.delete_setting(cache_key)
            return {"eligible_keywords": len(records), "executed_keywords": executed,
                    "unchanged_keywords": unchanged, "recomputed_algorithms": recomputed_algorithms,
                    "registered_algorithms": len(keys)}

        return self.store.run_algorithm_analysis(analyze)

    def _embed_cached(self, texts, versions):
        if (not isinstance(texts, list) or not isinstance(versions, list)
                or len(texts) != len(versions)
                or any(not isinstance(text, str) or not text.strip() for text in texts)):
            raise ValueError("embedding text and version lists must be aligned and non-empty")
        # Entity descriptions and Collection examples can exceed the local
        # model's input budget. Bound model context while keeping full source
        # evidence unchanged in storage and classification results.
        texts = [text[:embeddings.MAX_INPUT_CHARS] for text in texts]
        results, pending, keys = [None] * len(texts), [], []
        requested = []
        for index, (text, version) in enumerate(zip(texts, versions)):
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            key = hashlib.sha256(f"{embeddings.MODEL_VERSION}:{version}:{text_hash}".encode()).hexdigest()
            requested.append((index, key, text_hash, version, text))
        cached = self.store.get_embedding_cache([
            (key, embeddings.MODEL_VERSION, version, text_hash)
            for _, key, text_hash, version, _ in requested
        ])
        for index, key, text_hash, version, text in requested:
            row = cached.get(key)
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
            cache_entries = []
            for (index, key, text_hash, version), vector in zip(keys, vectors):
                results[index] = vector
                cache_entries.append((key, embeddings.MODEL_VERSION, version, text_hash,
                                      struct.pack(f"<{len(vector)}f", *vector), len(vector)))
            self.store.upsert_embedding_cache(cache_entries)
        return results

    @staticmethod
    def _candidate_text(record):
        # Provider/project metadata describes provenance, not the keyword's subject.
        # Keep it in evidence and avoid diluting topic semantics with repetitive context.
        description = record.get("entity_description", "")
        categories = ", ".join(record.get("entity_category_ids", ()))
        entity_type = record.get("entity_type", "unknown")
        context = f" Verified context: {description}." if description else ""
        role = f" Verified topic categories: {categories}." if categories else ""
        return f"Topic: {record['keyword']}. Entity type: {entity_type}.{context}{role}"

    @staticmethod
    def _topic_text(record):
        description = record.get("entity_description", "")
        categories = ", ".join(record.get("entity_category_ids", ()))
        context = f" Description: {description}." if description else ""
        topic = f" Categories: {categories}." if categories else ""
        return f"Wikimedia topic: {record['keyword']}.{context}{topic}"

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
        return self.store.unassigned_keyword_count()

    def _refresh_latest_topic_counts(self):
        """Refresh the latest saved run's topic-only counts after cached reclassification."""
        return self.store.refresh_latest_topic_counts()

    async def reclassify_unassigned(self, fetch_id: str | None = None):
        """Run cached local matching after Collection creation/edits without a provider fetch."""
        identifier = fetch_id or f"reclassify-{uuid.uuid4()}"
        return await asyncio.to_thread(self._process_fetch, identifier, [])

    def reclassify_cached_keywords(self, keyword_ids):
        """Reclassify cached records and add missing taxonomy links without replacing user state."""
        identifiers = tuple(dict.fromkeys(keyword_ids))
        if not identifiers:
            return {"reclassified": 0, "items": []}
        rows = self.store.load_cached_keywords_for_reclassification(identifiers)
        source_records = {row["id"]: dict(row) for row in rows}
        updated = []
        from app.taxonomy.classifier import classify_contextual
        for raw in rows:
            record = dict(raw)
            try:
                evidence = json.loads(record.get("evidence") or "{}")
            except (TypeError, ValueError):
                evidence = {}
            record.update(evidence)
            def json_values(name):
                try:
                    return json.loads(record.get(name) or "[]")
                except (TypeError, ValueError):
                    return []
            context = {
                "entity_id": record.get("entity_id"), "entity_type": record.get("entity_type") or "unknown",
                "description": record.get("entity_description") or "",
                "occupations": json_values("entity_occupations"),
                "category_ids": json_values("entity_category_ids"),
                "instance_types": json_values("entity_instance_types"),
                "source_url": record.get("entity_source_url") or "",
                "method": record.get("entity_context_method") or "",
                "status": record.get("entity_context_status") or "",
                "page_namespace": record.get("page_namespace"),
                "is_disambiguation": bool(record.get("is_disambiguation")),
                "is_main_page": bool(record.get("is_main_page")) if record.get("is_main_page_known") else None,
                "is_main_page_known": bool(record.get("is_main_page_known")),
                "redirected_from": record.get("redirected_from") or "",
                "page_language": record.get("page_language") or "",
            }
            result = classify_contextual(record, context)
            result["classified_at"] = now()
            updated.append((record["id"], result))
            source_records[record["id"]]["classification_result"] = result
        self.store.persist_classifications(
            [source_records[identifier] for identifier, _ in updated],
            delete_unpinned_ai_for_ineligible=True,
        )
        taxonomy_assignments = []
        for keyword_id, result in updated:
            source_record = source_records[keyword_id]
            if not source_record["eligible"] or result["classification_status"] != "CLASSIFIED":
                continue
            for category_id in result["topic_category_ids"]:
                def ensure_taxonomy_path(item):
                    if item.parent_id:
                        parent = resolve_category(item.parent_id)
                        parent_id = ensure_taxonomy_path(parent)
                        if parent_id is None:
                            return None
                        return self.store.ensure_category_collection(item, parent_id=parent_id)
                    return self.store.ensure_category_collection(item)
                collection_id = ensure_taxonomy_path(resolve_category(category_id))
                if collection_id is None:
                    continue
                taxonomy_assignments.append((
                    collection_id, keyword_id,
                    f"Verified cached entity classification: {category_id}",
                ))
        self.store.add_taxonomy_memberships_if_allowed(taxonomy_assignments)
        self._refresh_latest_topic_counts()
        return {"reclassified": len(updated), "items": [
            {"keyword_id": identifier, "classification_status": result["classification_status"],
             "classification_reason": result["classification_reason"],
             "topic_category_ids": result["topic_category_ids"],
             "eligible": bool(source_records[identifier]["eligible"])}
            for identifier, result in updated
        ]}

    def _propose(self, fetch_id, unassigned, collections):
        if len(unassigned) < MIN_PROPOSAL_SUPPORT:
            return 0, 0
        mode = self.store.get_setting("creation_mode", "AUTO_CREATE")
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
            if len(name.split()) < 2:
                # A single shared word such as "Harry" does not identify a
                # cohesive topic when the source titles differ.
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
                self.store.add_local_ai_memberships(identifier, support, assignment_reason)
                created += 1
            elif mode == "SUGGEST_ONLY":
                stamp, proposal_id = now(), str(uuid.uuid4())
                self.store.create_proposal(proposal_id, name, description, support, reason, stamp, stamp)
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
        candidates = self.store.collection_name_candidates(fingerprint(name))
        if candidates["suppressed"]:
            return True
        labels = candidates["labels"]
        labels.extend(item["name"] for item in collections
                      if not item.get("category_id") or fingerprint(item["name"]) == fingerprint(name))
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
