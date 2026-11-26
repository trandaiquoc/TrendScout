"""Collection persistence, deterministic taxonomy, and AI workflow delegation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timezone

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
    keyword_identity,
    keyword_record,
    now,
)
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.taxonomy.registry import resolve as resolve_category

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


class CollectionService:
    def __init__(self, store: CollectionStore, entity_context_resolver=None, *,
                 fetch_model_inference_enabled=False, ai_engine=None):
        if type(fetch_model_inference_enabled) is not bool:
            raise TypeError("fetch model inference setting must be boolean")
        self.store = store
        self.entity_context_resolver = entity_context_resolver
        self.fetch_model_inference_enabled = fetch_model_inference_enabled
        self.ai_engine = ai_engine

    def _model_unavailable_result(self, fetch_id, *, available, skipped,
                                  policy_excluded, eligible, unassigned,
                                  error_code, algorithms=None):
        self.store.mark_fetch_model_unavailable(
            fetch_id, available=available, skipped=skipped + policy_excluded,
            eligible=eligible, error_code=error_code,
            cached_embeddings=0,
            inferred_embeddings=0,
        )
        return {"fetch_id": fetch_id, "status": "MODEL_UNAVAILABLE", "available": available,
                "processed": 0, "assigned": 0, "unassigned": unassigned,
                "eligible": eligible, "created_collections": 0, "suggestions": 0,
                "skipped": skipped + policy_excluded, "error_code": error_code,
                "algorithms": algorithms}

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
        analysis = self._analyze_persisted_keywords(
            records, sources=sources, cache_only=cached_context_only,
        )
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
        taxonomy_assignments = []
        for record in records:
            if record.get("classification_result", {}).get("classification_status") == "INELIGIBLE":
                continue
            for category_collection_id in record.get("category_collection_ids", ()):
                taxonomy_assignments.append((
                    category_collection_id, record["id"],
                    f"Verified entity context: {record.get('entity_type', 'unknown')}; "
                    f"taxonomy category {record.get('entity_category_ids', ())}",
                ))
        if taxonomy_assignments:
            self.store.add_taxonomy_memberships_if_allowed(taxonomy_assignments)

        if self.ai_engine is None:
            return self._model_unavailable_result(
                fetch_id, available=available, skipped=skipped,
                policy_excluded=policy_excluded, eligible=eligible,
                unassigned=len(records), error_code="AI_DISABLED", algorithms=analysis,
            )
        self._update_job(fetch_id, stage="MATCHING_EXISTING_COLLECTIONS", eligible=eligible,
                         processed=len(records))
        combined = {"assigned": [], "unassigned_keyword_ids": [], "proposal_ids": [],
                    "job_ids": [], "reconciliation_ids": []}
        candidate_ids = [record["id"] for record in records
                         if record.get("classification_result", {}).get("classification_status") != "INELIGIBLE"]
        for offset in range(0, len(candidate_ids), 200):
            batch_ids = candidate_ids[offset:offset + 200]
            stable_job_id = "ai_" + uuid.uuid5(
                uuid.NAMESPACE_URL, f"trendscout-fetch:{fetch_id}:{offset}").hex
            try:
                result = self.ai_engine.classify_keyword_ids(
                    self.store, batch_ids, job_id=stable_job_id, automatic=True,
                )
            except Exception:
                result = {"job_id": stable_job_id, "status": "DEFERRED",
                          "error_code": "AI_STORAGE_UNAVAILABLE"}
            combined["job_ids"].append(result.get("job_id", stable_job_id))
            if result.get("status") != "COMPLETED":
                unavailable = self._model_unavailable_result(
                    fetch_id, available=available, skipped=skipped,
                    policy_excluded=policy_excluded, eligible=eligible,
                    unassigned=len(records), error_code=result.get("error_code") or "AI_MODEL_UNAVAILABLE",
                    algorithms=analysis,
                )
                unavailable["ai_job_ids"] = combined["job_ids"]
                return unavailable
            combined["assigned"].extend(result.get("assigned", []))
            combined["unassigned_keyword_ids"].extend(result.get("unassigned_keyword_ids", []))
            combined["proposal_ids"].extend(result.get("proposal_ids", []))
            if result.get("membership_reconciliation_id"):
                combined["reconciliation_ids"].append(result["membership_reconciliation_id"])

        assigned = self.store.count_ai_assigned_keywords()
        outcome = {"fetch_id": fetch_id, "status": "COMPLETED", "available": available,
                   "processed": len(records), "assigned": assigned,
                   "unassigned": self._unassigned_count(), "created_collections": 0,
                   "suggestions": len(combined["proposal_ids"]), "skipped": skipped + policy_excluded,
                   "eligible": eligible, "ai_job_ids": combined["job_ids"],
                   "proposal_ids": combined["proposal_ids"],
                   "membership_reconciliation_ids": combined["reconciliation_ids"],
                   "algorithms": analysis}
        self._update_job(fetch_id, stage="SAVING_RESULTS", assigned=assigned,
                         created_collections=0, unassigned=outcome["unassigned"])
        self._update_job(fetch_id, stage="COMPLETED", status="COMPLETED", available=available,
            eligible=eligible, processed=outcome["processed"], assigned=assigned,
            unassigned=outcome["unassigned"], created_collections=0,
            suggestions=outcome["suggestions"], skipped=skipped + policy_excluded)
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

    def _analyze_persisted_keywords(self, changed_records, *, sources=(), cache_only=False):
        """Persist bounded Wikimedia observations and analyze only compatible inputs."""
        from datetime import timedelta

        from app.algorithms.catalog import persisted_keys_for_source
        from app.algorithms.contracts import (
            RESULT_CONTRACT_VERSION,
            AlgorithmInput,
            Observation,
            Scope,
        )
        from app.composition import algorithm_engine
        from app.sources.catalog import WIKIMEDIA_SOURCE_ID

        def collected_instant(value):
            if not isinstance(value, str) or len(value) > 64:
                return None
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
            return parsed.astimezone(timezone.utc).isoformat()

        engine = algorithm_engine()
        registered_count = len(engine.registry.keys())
        if cache_only:
            return {"eligible_keywords": 0, "executed_keywords": 0, "unchanged_keywords": 0,
                    "recomputed_algorithms": 0, "registered_algorithms": registered_count,
                    "persisted_algorithms": 0, "analysis_status": "CACHE_ONLY"}
        for source in sources:
            if source.get("source_id") != WIKIMEDIA_SOURCE_ID:
                continue
            coverage = source.get("coverage")
            coverage = coverage if isinstance(coverage, dict) else {}
            if (coverage.get("cache_only") is True
                    or source.get("delivery_state") in {"CACHE_REPLAY", "CACHE_ONLY"}
                    or (coverage.get("cache_fetched_at")
                        and coverage.get("provider_request_count") == 0)):
                return {"eligible_keywords": 0, "executed_keywords": 0, "unchanged_keywords": 0,
                        "recomputed_algorithms": 0, "registered_algorithms": registered_count,
                        "persisted_algorithms": 0, "analysis_status": "CACHE_ONLY"}
        source_records = [record for record in changed_records
                          if record.get("source") == WIKIMEDIA_SOURCE_ID]
        if not source_records:
            return {"eligible_keywords": 0, "executed_keywords": 0, "unchanged_keywords": 0,
                    "recomputed_algorithms": 0, "registered_algorithms": registered_count,
                    "persisted_algorithms": 0, "analysis_status": "NO_COMPATIBLE_SOURCE_INPUT"}

        metric, unit = "views_ceil", "approx_pageviews_upper_bound"
        source_collection_time = None
        for source in sources:
            if source.get("source_id") != WIKIMEDIA_SOURCE_ID:
                continue
            coverage = source.get("coverage")
            coverage = coverage if isinstance(coverage, dict) else {}
            source_collection_time = collected_instant(
                coverage.get("fetched_at") or source.get("fetched_at")
            )
            if source_collection_time:
                break
        prepared_by_id = {}
        for record in source_records:
            try:
                evidence = json.loads(record.get("evidence") or "{}")
                if not isinstance(evidence, dict):
                    continue
                date_value = evidence.get("observation_date") or record.get("observed_at", "")[:10]
                analysis_date = date.fromisoformat(date_value[:10])
                value, rank = evidence.get("views_ceil"), evidence.get("provider_rank")
                snapshot_status = evidence.get("snapshot_status", "SUCCESS")
                if (type(value) is not int or value < 0 or type(rank) is not int or rank < 1
                        or snapshot_status not in {"SUCCESS", "PARTIAL"}):
                    continue
                prepared_by_id[record["id"]] = {
                    "id": record["id"], "date": analysis_date,
                    "country": evidence.get("country_scope") or "US",
                    "access": evidence.get("access") or "all-access",
                    "project": record.get("project") or "",
                    "article": evidence.get("article_original") or record.get("keyword") or "",
                    "value": value, "rank": rank, "snapshot_status": snapshot_status,
                    "evidence_url": evidence.get("source_url"),
                    "collected_at": collected_instant(evidence.get("fetched_at")) or source_collection_time,
                }
            except (TypeError, ValueError, KeyError, IndexError):
                continue
        if not prepared_by_id:
            return {"eligible_keywords": 0, "executed_keywords": 0, "unchanged_keywords": 0,
                    "recomputed_algorithms": 0, "registered_algorithms": registered_count,
                    "persisted_algorithms": 0, "analysis_status": "NO_VALID_OBSERVATIONS"}

        keys = persisted_keys_for_source(engine.registry, WIKIMEDIA_SOURCE_ID)
        metadata_by_key = {key: engine.registry.get(key).metadata for key in keys}
        metadata_prefix_by_key = {
            key: _algorithm_digest_prefix({
                "metadata": asdict(value),
                "result_contract_version": RESULT_CONTRACT_VERSION,
            })
            for key, value in metadata_by_key.items()
        }

        def analyze(session):
            eligible_current = {}
            identifiers = list(prepared_by_id)
            for offset in range(0, len(identifiers), 500):
                rows = session.eligible_keywords(
                    source=WIKIMEDIA_SOURCE_ID, ids=identifiers[offset:offset + 500],
                )
                eligible_current.update({row["id"]: row for row in rows})
            prepared = {key: value for key, value in prepared_by_id.items()
                        if key in eligible_current}
            if not prepared:
                return {"eligible_keywords": 0, "executed_keywords": 0, "unchanged_keywords": 0,
                        "recomputed_algorithms": 0, "registered_algorithms": registered_count,
                        "persisted_algorithms": len(keys), "analysis_status": "NO_ELIGIBLE_OBSERVATIONS"}

            snapshot_groups = defaultdict(list)
            for value in prepared.values():
                group = (value["date"], value["country"], value["access"], value["project"])
                snapshot_groups[group].append(value)

            observations = []
            for value in prepared.values():
                digest = hashlib.sha256(json.dumps([
                    value["id"], value["date"].isoformat(), value["value"], value["rank"],
                    value["snapshot_status"], WIKIMEDIA_SOURCE_ID, value["project"],
                    value["collected_at"],
                ], separators=(",", ":")).encode()).hexdigest()
                value["observation_digest"] = digest
                observations.append((
                    value["id"], WIKIMEDIA_SOURCE_ID, value["date"].isoformat(),
                    value["country"], value["access"], value["project"], metric, unit,
                    "OBSERVED", value["value"], value["rank"], value["snapshot_status"], digest,
                ))

            # A confirmed complete top-list snapshot records absence as censored
            # NOT_IN_TOP. A partial snapshot records uncertainty, never a zero.
            missing_current = {}
            for group, current_values in snapshot_groups.items():
                analysis_date, country, access, project = group
                status = "SUCCESS" if all(
                    item["snapshot_status"] == "SUCCESS" for item in current_values
                ) else "PARTIAL"
                current_ids = {item["id"] for item in current_values}
                prior_rows = session.eligible_keywords(
                    source=WIKIMEDIA_SOURCE_ID,
                    observed_from=(analysis_date - timedelta(days=6)).isoformat(),
                    observed_to=(analysis_date - timedelta(days=1)).isoformat(),
                )
                for prior in prior_rows:
                    if prior["id"] in current_ids:
                        continue
                    try:
                        prior_evidence = json.loads(prior.get("evidence") or "{}")
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(prior_evidence, dict):
                        continue
                    if ((prior.get("project") or "") != project
                            or (prior_evidence.get("country_scope") or "US") != country
                            or (prior_evidence.get("access") or "all-access") != access):
                        continue
                    missing_status = "NOT_IN_TOP" if status == "SUCCESS" else "PARTIAL_SNAPSHOT"
                    digest = hashlib.sha256(json.dumps([
                        prior["id"], analysis_date.isoformat(), missing_status, status,
                        WIKIMEDIA_SOURCE_ID, project, country, access, source_collection_time,
                    ], separators=(",", ":")).encode()).hexdigest()
                    observations.append((
                        prior["id"], WIKIMEDIA_SOURCE_ID, analysis_date.isoformat(),
                        country, access, project, metric, unit, missing_status, None, None,
                        status, digest,
                    ))
                    missing_current[prior["id"]] = {
                        "date": analysis_date, "country": country, "access": access,
                        "project": project,
                        "article": prior_evidence.get("article_original") or prior.get("keyword") or "",
                        "value": None, "rank": None, "snapshot_status": status,
                        "collected_at": source_collection_time,
                        "observation_digest": digest,
                        "evidence_url": prior_evidence.get("source_url"),
                        "observation_status": missing_status,
                    }
            observation_digest_by_id_date = {
                (value["id"], value["date"].isoformat()): value["observation_digest"]
                for value in prepared.values()
            }
            observation_digest_by_id_date.update({
                (keyword_id, value["date"].isoformat()): value["observation_digest"]
                for keyword_id, value in missing_current.items()
            })
            session.upsert_observations(observations)

            analysis_rows = dict(eligible_current)
            missing_ids = list(missing_current)
            for offset in range(0, len(missing_ids), 500):
                rows = session.eligible_keywords(
                    source=WIKIMEDIA_SOURCE_ID, ids=missing_ids[offset:offset + 500],
                )
                analysis_rows.update({row["id"]: row for row in rows})

            snapshot_cohorts = defaultdict(list)
            for keyword_id, value in prepared.items():
                scope = Scope(WIKIMEDIA_SOURCE_ID, value["country"], value["access"],
                              value["project"], value["article"], metric, unit)
                point = Observation(value["date"], scope, "OBSERVED", value["value"],
                                    value["snapshot_status"], value["rank"])
                group = (value["date"], value["country"], value["access"],
                         value["project"], metric, unit)
                snapshot_cohorts[group].append((keyword_id, point))
            serialized_cohorts = {}
            cohort_indexes = {}
            for group, cohort in snapshot_cohorts.items():
                serialized = _serialized_cohort(cohort)
                serialized_cohorts[group] = serialized
                cohort_indexes[group] = _cohort_json_index(serialized)

            history_rows_by_keyword = defaultdict(list)
            history_groups = defaultdict(list)
            for keyword_id, value in missing_current.items():
                history_groups[(value["date"], value["country"], value["access"],
                                value["project"])].append(keyword_id)
            for keyword_id, value in prepared.items():
                history_groups[(value["date"], value["country"], value["access"],
                                value["project"])].append(keyword_id)
            for (history_date, country, access, project), keyword_ids in history_groups.items():
                for offset in range(0, len(keyword_ids), 500):
                    rows = session.histories(
                        keyword_ids[offset:offset + 500], WIKIMEDIA_SOURCE_ID,
                        history_date.isoformat(), metric, unit,
                        after_date=(history_date - timedelta(days=6)).isoformat(),
                        country=country, access=access, project=project,
                    )
                    for point in rows:
                        history_rows_by_keyword[point["keyword_id"]].append(point)

            existing_results_by_keyword = {}
            analysis_ids = list(analysis_rows)
            for offset in range(0, len(analysis_ids), 500):
                existing_results_by_keyword.update(
                    session.result_digests_for(analysis_ids[offset:offset + 500])
                )

            executed = unchanged = recomputed_algorithms = 0
            cacheable = True
            for record in analysis_rows.values():
                keyword_id = record["id"]
                value = prepared.get(keyword_id) or missing_current.get(keyword_id)
                if value is None:
                    continue
                analysis_date = value["date"]
                scope = Scope(WIKIMEDIA_SOURCE_ID, value["country"], value["access"],
                              value["project"], value["article"], metric, unit)
                history_rows = history_rows_by_keyword.get(keyword_id, [])
                history = tuple(Observation(
                    date.fromisoformat(str(point["observation_date"])[:10]), scope,
                    point["status"], point["value"], point["snapshot_status"],
                ) for point in history_rows)
                current = value["value"]
                cohort_key = (analysis_date, scope.country, scope.access, scope.project, metric, unit)
                peers = snapshot_cohorts.get(cohort_key, ())
                cohort_index = cohort_indexes.get(cohort_key)
                if cohort_key in serialized_cohorts:
                    cohort = None
                    cohort_input = _COHORT_MARKER
                    cohort_bytes = _cohort_json_without(
                        serialized_cohorts[cohort_key], keyword_id, cohort_index
                    )
                else:
                    cohort = ()
                    cohort_input = []
                    cohort_bytes = None
                input_data = {
                    "scope": asdict(scope), "date": analysis_date.isoformat(), "current": current,
                    "collected_at": value.get("collected_at"),
                    "current_snapshot_status": value["snapshot_status"],
                    "current_observation_status": value.get("observation_status", "OBSERVED"),
                    "history": [(str(point.date), point.status, point.value, point.snapshot_status)
                                for point in history],
                    "cohort": cohort_input,
                }
                existing = existing_results_by_keyword.get(keyword_id, {})
                digest_by_key, missing_keys, encoded_inputs = {}, [], {}
                for key in keys:
                    metadata = metadata_by_key[key]
                    popularity = metadata.family == "popularity"
                    if popularity not in encoded_inputs:
                        encoded_inputs[popularity] = _algorithm_digest_input(
                            input_data, popularity=popularity, cohort_bytes=cohort_bytes,
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
                    cohort = tuple(point for peer_id, point in peers if peer_id != keyword_id)
                item = AlgorithmInput(
                    keyword_id, scope, analysis_date, current, history,
                    window_days=6, minimum_samples=3, evidence_url=value["evidence_url"],
                    current_snapshot_status=value["snapshot_status"], cohort=cohort,
                )
                produced_keys, persisted_results = [], []
                for result in engine.run(item, missing_keys):
                    result_key = f"{result.algorithm_id}@{result.algorithm_version}"
                    result_digest = digest_by_key[result_key]
                    evidence_references = [{
                        "source_id": WIKIMEDIA_SOURCE_ID,
                        "keyword_id": keyword_id,
                        "observation_date": point["observation_date"],
                        "metric": metric,
                        "unit": unit,
                        "input_digest": point.get("input_digest"),
                    } for point in history_rows]
                    current_observation_digest = observation_digest_by_id_date.get(
                        (keyword_id, analysis_date.isoformat())
                    )
                    evidence_references.append({
                        "source_id": WIKIMEDIA_SOURCE_ID,
                        "keyword_id": keyword_id,
                        "observation_date": analysis_date.isoformat(),
                        "metric": metric,
                        "unit": unit,
                        "input_digest": current_observation_digest,
                    })
                    result_payload = asdict(result)
                    result_payload.update({
                        "source_id": WIKIMEDIA_SOURCE_ID,
                        "observation_date": analysis_date.isoformat(),
                        "collected_at": value.get("collected_at"),
                        "input_scope": asdict(scope),
                        "input_metric": metric,
                        "input_unit": unit,
                        "result_contract_version": RESULT_CONTRACT_VERSION,
                        "input_digest": result_digest,
                        "evidence_references": evidence_references,
                        "cohort_input_digest": (
                            hashlib.sha256(cohort_bytes).hexdigest()
                            if result.algorithm_id in {"observed-volume-rank", "observed-percentile-rank"}
                            and cohort_bytes is not None else None
                        ),
                    })
                    payload = json.dumps(result_payload, ensure_ascii=False, sort_keys=True,
                                         allow_nan=False)
                    produced_keys.append(result_key)
                    persisted_results.append((
                        keyword_id, result.algorithm_id, result.algorithm_version, result.status,
                        result.reason_code, result.value, result.metric_name, result.metric_unit,
                        payload, result_digest, now(),
                    ))
                session.upsert_results(persisted_results)
                executed += 1
                recomputed_algorithms += len(missing_keys)
                if len(produced_keys) != len(missing_keys) or set(produced_keys) != set(missing_keys):
                    cacheable = False
            return {
                "eligible_keywords": len(analysis_rows), "executed_keywords": executed,
                "unchanged_keywords": unchanged, "recomputed_algorithms": recomputed_algorithms,
                "registered_algorithms": registered_count, "persisted_algorithms": len(keys),
                "analysis_status": "COMPLETED" if cacheable else "PARTIAL",
                "source_id": WIKIMEDIA_SOURCE_ID, "window_days": 6,
                "retention_calendar_days": 7,
            }

        return self.store.run_algorithm_analysis(analyze)

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
