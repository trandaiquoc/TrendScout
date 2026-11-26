"""Public Fetch facade over durable admission and retained source results.

The facade has no provider configuration surface. Its admission and result reads
are usable only when explicitly injected by backend composition.
"""

import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.collections.source_digest import (
    PRIVATE_SOURCE_DIGEST_FIELDS,
    reusable_collection_input,
    reusable_collection_manifest,
)
from app.discovery.source_results import discover_fetch_results
from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import (
    MANUAL_SOURCE_REFRESH_MARKER,
    AdmissionDenied,
    PublicAdmission,
)
from app.jobs.source_checkpoints import CheckpointGrant
from app.sources.catalog import WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID


def _terminal_missing_source_result(job_state, source_id, terminal_reason=None):
    failure = {
        "FAILED": ("RECOVERY_ATTEMPTS_EXHAUSTED", "RECOVERY_ATTEMPTS_EXHAUSTED"),
        "EXPIRED": ("JOB_DEADLINE_EXPIRED", "FETCH_DEADLINE_EXCEEDED"),
        "ABORTED": ("JOB_ABORTED", "FETCH_ABORTED"),
    }.get(job_state, ("TERMINAL_RESULT_MISSING", "SOURCE_RESULT_MISSING"))
    failure = {
        "JOB_DEADLINE_EXPIRED": ("JOB_DEADLINE_EXPIRED", "FETCH_DEADLINE_EXCEEDED"),
        "RECOVERY_ATTEMPTS_EXHAUSTED": (
            "RECOVERY_ATTEMPTS_EXHAUSTED", "RECOVERY_ATTEMPTS_EXHAUSTED"
        ),
    }.get(terminal_reason, failure)
    reason, error_code = failure
    diagnostic = {
        "source_id": source_id,
        "failure_phase": "WORKER_RECOVERY",
        "failure_reason": reason,
        "exception_class": None,
        "error_code": error_code,
    }
    return {
        "source_id": source_id,
        "status": ("FAILED" if job_state in {"FAILED", "EXPIRED", "ABORTED", "PARTIAL"}
                   else "UNAVAILABLE"),
        "delivery_state": None,
        "record_count": 0,
        "records": [],
        "coverage": {"worker_failure_diagnostic": diagnostic},
        "provider_request_count": None,
        "error_code": error_code,
        "quality_flags": [],
    }


def _public_youtube_record(record):
    """Project a validated video into the fields the Fetch UI displays."""
    if not isinstance(record, dict):
        return None
    video_id = record.get("provider_video_id")
    if (record.get("source_id") != YOUTUBE_SOURCE_ID
            or record.get("provider_id") != "youtube_data_api_v3"
            or record.get("data_mode") != "LIVE"
            or not isinstance(video_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)):
        return None

    video_url = record.get("canonical_video_url")
    if video_url != f"https://www.youtube.com/watch?v={video_id}":
        return None
    parsed_url = urlsplit(video_url)
    if (parsed_url.scheme != "https" or parsed_url.netloc != "www.youtube.com"
            or parsed_url.path != "/watch" or parsed_url.fragment):
        return None

    title = record.get("title")
    description = record.get("description")
    channel_title = record.get("channel_title")
    published_at = record.get("published_at")
    result_rank = record.get("result_rank")
    if (title is not None and (not isinstance(title, str) or len(title) > 500)
            or description is not None
            and (not isinstance(description, str) or len(description) > 5000)
            or channel_title is not None
            and (not isinstance(channel_title, str) or len(channel_title) > 500)
            or published_at is not None
            and (not isinstance(published_at, str) or len(published_at) > 64)
            or type(result_rank) is not int or result_rank < 1):
        return None

    return {
        "provider_video_id": video_id,
        "canonical_video_url": video_url,
        "title": title,
        "description": description,
        "channel_title": channel_title,
        "published_at": published_at,
        "result_rank": result_rank,
    }


class DistributedPublicFetch:
    def __init__(self, admission: PublicAdmission, worker: DistributedFetchWorker, *,
                 resolve_grant, utcnow=lambda: datetime.now(timezone.utc),
                 local_live=False, normal_wikimedia_live=False, normal_google_live=False,
                 firestore_shared_live=False, vercel_postgres_live=False,
                 resolve_processing_grants=None,
                 provider_retry_after_seconds=None,
                 owner_fresh_acceptance_once: bool = False):
        if (not isinstance(admission, PublicAdmission)
                or not isinstance(worker, DistributedFetchWorker)
                or worker.admission.policy_digest != admission.policy_digest
                or not callable(resolve_grant) or not callable(utcnow)
                or type(local_live) is not bool or type(normal_wikimedia_live) is not bool
                or type(normal_google_live) is not bool
                or type(firestore_shared_live) is not bool
                or type(vercel_postgres_live) is not bool
                or type(owner_fresh_acceptance_once) is not bool
                or owner_fresh_acceptance_once and not firestore_shared_live
                or sum((local_live, normal_wikimedia_live, normal_google_live,
                        firestore_shared_live)) > 1
                or vercel_postgres_live and (not normal_google_live or firestore_shared_live)
                or (provider_retry_after_seconds is not None
                    and not callable(provider_retry_after_seconds))):
            raise ValueError("matching admission, worker and grant resolver required")
        self.admission = admission
        self.worker = worker
        self.resolve_grant = resolve_grant
        self.utcnow = utcnow
        self.local_live = local_live
        self.normal_wikimedia_live = normal_wikimedia_live
        self.normal_google_live = normal_google_live
        self.firestore_shared_live = firestore_shared_live
        self.vercel_postgres_live = vercel_postgres_live
        self.postgres_live = False
        # Results larger than the inline checkpoint budget are stored by the
        # worker's journal in its durable snapshot blob store. Reads must use
        # that same journal so terminal status can restore the full result.
        self.journal = worker.journal
        declared_modes = set(worker.source_modes.values())
        if normal_wikimedia_live:
            from app.normal_wikimedia_live_policy import normal_wikimedia_live_policy
            from app.sources.catalog import WIKIMEDIA_SOURCE_ID
            from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
            if (os.environ.get("VERCEL") or declared_modes != {"LIVE"}
                    or worker.admission is not admission
                    or type(admission.store) is not LocalSQLiteAtomicStateStore
                    or admission.store.data_mode != "LIVE"
                    or admission.policy != normal_wikimedia_live_policy()
                    or admission.policy.enabled_sources != frozenset({WIKIMEDIA_SOURCE_ID})
                    or not callable(resolve_processing_grants)):
                raise ValueError("normal Wikimedia LIVE requires its separate local policy and state")
            self.data_mode = "LIVE"
        elif normal_google_live:
            from app.normal_wikimedia_live_policy import normal_google_live_policy
            from app.sources.catalog import (
                BING_SOURCE_ID,
                GOOGLE_SOURCE_ID,
                WIKIMEDIA_SOURCE_ID,
                YOUTUBE_SOURCE_ID,
            )
            from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
            from app.storage.postgres_atomic_state import PostgresAtomicStateStore
            pools = admission.policy.pools
            bing_enabled = BING_SOURCE_ID in admission.policy.enabled_sources
            google_pool = (pools.get("serpapi_google") if bing_enabled
                           else pools.get("serpapi"))
            bing_pool = pools.get("serpapi_bing") if bing_enabled else None
            try:
                expected = normal_google_live_policy(
                    serpapi_daily_budget=google_pool.daily_requests,
                    serpapi_monthly_budget=google_pool.monthly_requests,
                    rss_daily_budget=pools["google_rss"].daily_requests,
                    rss_monthly_budget=pools["google_rss"].monthly_requests,
                    fetch_starts_per_hour=admission.policy.max_starts_per_window,
                    bing_enabled=bing_enabled,
                    bing_daily_budget=(bing_pool.daily_requests if bing_pool else 3),
                    youtube_enabled=True,
                )
            except (KeyError, TypeError, ValueError):
                expected = None
            expected_sources = {WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID}
            if bing_enabled:
                expected_sources.add(BING_SOURCE_ID)
            local_state = type(admission.store) is LocalSQLiteAtomicStateStore
            postgres_state = type(admission.store) is PostgresAtomicStateStore
            self.postgres_live = postgres_state
            vercel_postgres_state = (
                self.vercel_postgres_live and os.environ.get("VERCEL") == "1"
                and postgres_state
            )
            if ((os.environ.get("VERCEL") and not vercel_postgres_state)
                    or declared_modes != {"LIVE"}
                    or worker.admission is not admission
                    or not (local_state or postgres_state)
                    or local_state and admission.store.data_mode != "LIVE"
                    or self.vercel_postgres_live and not vercel_postgres_state
                    or admission.policy != expected
                    or admission.policy.enabled_sources != frozenset(expected_sources)
                    or not callable(resolve_processing_grants)):
                raise ValueError("normal Google LIVE requires its separate local policy and state")
            self.data_mode = "LIVE"
        elif local_live:
            from app.local_smoke_policy import local_smoke_policy
            from app.storage.local_smoke_store import LocalSmokeStore
            from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
            from app.storage.local_volatile_checkpoints import LocalVolatileCheckpoints
            if (os.environ.get("VERCEL") or declared_modes != {"LIVE"}
                    or worker.admission is not admission
                    or type(admission.store) is not LocalSmokeStore
                    or type(admission.store.store) is not LocalVolatileCheckpoints
                    or type(admission.store.store.store) is not LocalSQLiteAtomicStateStore
                    or admission.store.store.store.data_mode != "LIVE"
                    or admission.policy != replace(local_smoke_policy(), enabled=True)
                    or not callable(resolve_processing_grants)):
                raise ValueError("local live demo requires bounded local policy and processing grants")
            self.data_mode = "LIVE"
        elif firestore_shared_live:
            from app.storage.firestore_atomic_state import FirestoreAtomicStateStore
            from app.storage.firestore_snapshot_blobs import FirestoreSnapshotBlobs

            snapshot_blobs = worker.journal.snapshot_blobs
            if (declared_modes != {"LIVE"}
                    or worker.admission is not admission
                    or type(admission.store) is not FirestoreAtomicStateStore
                    or type(snapshot_blobs) is not FirestoreSnapshotBlobs
                    or admission.store._client is not snapshot_blobs._client
                    or getattr(admission.store._client, "_database", None) != "(default)"
                    or not callable(resolve_processing_grants)):
                raise ValueError(
                    "Firestore shared LIVE requires matching Firestore state, blobs, and processing grants"
                )
            self.data_mode = "LIVE"
        else:
            if declared_modes != {"SYNTHETIC"} or resolve_processing_grants is not None:
                raise ValueError("public demo Fetch requires explicitly synthetic source handlers")
            self.data_mode = "SYNTHETIC"
        self.resolve_processing_grants = resolve_processing_grants
        self.provider_retry_after_seconds = provider_retry_after_seconds
        self.owner_fresh_acceptance_once = owner_fresh_acceptance_once
        self.latest_dataset_summary_available = False
        self.latest_dataset_summary = None

    def _latest_reuse_summary(self):
        """Read the current compact PostgreSQL manifest or use a request-scoped Firestore summary."""
        if getattr(self, "postgres_live", False):
            store = getattr(self, "collection_store", None)
            read_summary = getattr(store, "latest_dataset_reuse_summary", None)
            if not callable(read_summary):
                return None
            summary = read_summary()
            self.latest_dataset_summary_available = isinstance(summary, dict)
            self.latest_dataset_summary = summary if isinstance(summary, dict) else None
            return self.latest_dataset_summary
        summary = getattr(self, "latest_dataset_summary", None)
        if (getattr(self, "latest_dataset_summary_available", False)
                and isinstance(summary, dict)):
            return summary
        return None

    def submit(self, idempotency_key: str, *, history_days: int | None = None,
               source_ids: list[str] | None = None,
               manual_source_refresh: bool = False,
               cache_only: bool = False) -> dict:
        if type(manual_source_refresh) is not bool or type(cache_only) is not bool:
            raise ValueError("Fetch mode flags must be boolean")
        if manual_source_refresh and (history_days is not None or source_ids is None):
            raise AdmissionDenied("MANUAL_REFRESH_NOT_ALLOWED")
        if cache_only and (history_days is not None or source_ids is None
                           or manual_source_refresh or self.data_mode != "LIVE"):
            raise AdmissionDenied("CACHE_ONLY_UNAVAILABLE")
        if history_days is not None:
            if (source_ids is not None or not self.normal_wikimedia_live
                    or type(history_days) is not int or not 1 <= history_days <= 2):
                raise AdmissionDenied("HISTORICAL_FETCH_UNAVAILABLE")
            request_metadata = {"kind": "WIKIMEDIA_HISTORY", "history_days": history_days}
        elif source_ids is not None:
            request_metadata = {"sources": source_ids}
            if cache_only:
                request_metadata["cache_only"] = True
            if manual_source_refresh:
                if (not (self.firestore_shared_live or self.vercel_postgres_live)
                        or self.data_mode != "LIVE"):
                    raise AdmissionDenied("MANUAL_REFRESH_NOT_ALLOWED")
                request_metadata["manual_source_refresh"] = MANUAL_SOURCE_REFRESH_MARKER
        else:
            request_metadata = None
        if self.provider_retry_after_seconds is not None:
            try:
                existing = self.admission.lookup_idempotency(
                    idempotency_key, now=self.utcnow(), request_metadata=request_metadata
                )
            except AdmissionDenied:
                raise
            except Exception:
                raise AdmissionDenied("ADMISSION_STATE_UNAVAILABLE") from None
        else:
            existing = None
        if self.provider_retry_after_seconds is not None and existing is None and not cache_only:
            try:
                retry_after = self.provider_retry_after_seconds()
            except Exception:
                raise AdmissionDenied("ADMISSION_STATE_UNAVAILABLE") from None
            if type(retry_after) is not int or retry_after < 0:
                raise AdmissionDenied("ADMISSION_STATE_UNAVAILABLE")
            if retry_after:
                raise AdmissionDenied("PROVIDER_RATE_LIMITED")
        accepted = self.admission.admit(idempotency_key, now=self.utcnow(),
                                        request_metadata=request_metadata,
                                        owner_fresh_acceptance_once=(
                                            self.owner_fresh_acceptance_once
                                            and history_days is None
                                            and source_ids is not None
                                            and not cache_only
                                            and set(source_ids) == set(self.admission.policy.enabled_sources)
                                        ))
        return {"job_id": accepted["job_id"], "result": accepted["result"],
                "status": accepted["status"], "created_at": accepted.get("created_at"),
                "fetch_mode": "CACHE_ONLY" if cache_only else "LIVE",
                "owner_fresh_acceptance": accepted.get("owner_fresh_acceptance", False)}

    def _reused_terminal_status(self, job, reuse_check):
        metadata = job.get("request_metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        selected_sources = sorted(item["source_id"]
                                  for item in reuse_check["source_manifest"])
        source_results = []
        modes = set()
        for item in reuse_check["source_manifest"]:
            data_mode = item.get("data_mode")
            if item.get("status") != "UNAVAILABLE" and data_mode in {"LIVE", "SYNTHETIC"}:
                modes.add(data_mode)
            source_results.append({
                "source_id": item["source_id"],
                "status": item.get("status"),
                **({"data_mode": data_mode} if data_mode in {"LIVE", "SYNTHETIC"} else {}),
                "delivery_state": item.get("delivery_state"),
                "record_count": item.get("record_count"),
                "coverage": item.get("coverage", {}),
                "provider_request_count": item.get("provider_request_count"),
                "error_code": item.get("error_code"),
                "retry_after_seconds": item.get("retry_after_seconds"),
                "quality_flags": item.get("quality_flags", []),
                **({"transport_diagnostic": item["transport_diagnostic"]}
                   if "transport_diagnostic" in item else {}),
            })
        request_kind = metadata.get("kind", "LATEST")
        return {
            "fetch_id": job["job_id"],
            "status": job["state"],
            "stage": job["state"],
            **({"lease_until": job["lease_until"]}
               if job.get("state") == "RUNNING"
               and isinstance(job.get("lease_until"), str) else {}),
            "request_kind": request_kind,
            "fetch_mode": "CACHE_ONLY" if metadata.get("cache_only") is True else "LIVE",
            "selected_sources": selected_sources,
            **({"history_days": metadata["history_days"]}
               if request_kind == "WIKIMEDIA_HISTORY" else {}),
            "data_mode": (next(iter(modes)) if len(modes) == 1
                          else "MIXED" if modes else None),
            "sources": source_results,
            "progress": {"completed_sources": len(selected_sources),
                         "total_sources": len(selected_sources), "percent": 100},
            "discovery": None,
            "created_at": job["created_at"],
            "updated_at": job.get("updated_at") or job.get("available_at"),
            "finished_at": job.get("finished_at"),
            **({"terminal_reason": job["terminal_reason"]}
               if isinstance(job.get("terminal_reason"), str) else {}),
            "terminal": True,
            "recovery_available": job.get("recovery_available") is True,
            "collection_input_unchanged": True,
        }

    def status(self, job_id: str, *, include_records: bool = False,
               include_discovery: bool = True) -> dict | None:
        if type(include_records) is not bool or type(include_discovery) is not bool:
            raise ValueError("status projection flags must be boolean")
        now = self.utcnow()
        job = self.admission.read_job(job_id, now=now)
        if job is None:
            return None
        source_results = []
        modes = set()
        terminal = job["state"] in {"COMPLETED", "PARTIAL", "FAILED", "EXPIRED", "ABORTED"}
        summary = self._latest_reuse_summary() if terminal else None
        if terminal and not include_records:
            reuse_check = self.collection_reuse_for_fetch(
                job_id, job=job, _summary=summary, _summary_loaded=True,
            )
            if (isinstance(reuse_check, dict)
                    and reuse_check.get("collection_input_unchanged") is True):
                return self._reused_terminal_status(job, reuse_check)
        if terminal:
            request_metadata = job.get("request_metadata", {})
            selected_sources = ([WIKIMEDIA_SOURCE_ID]
                if request_metadata.get("kind") == "WIKIMEDIA_HISTORY"
                else request_metadata.get("sources", sorted(self.admission.policy.enabled_sources)))
            grants = {}
            for source_id in sorted(selected_sources):
                checkpoint = job.get("source_checkpoints", {}).get(source_id)
                snapshot = checkpoint.get("snapshot") if isinstance(checkpoint, dict) else None
                if not isinstance(snapshot, dict) or snapshot.get("phase") != "COMPLETE":
                    continue
                try:
                    grant = self.resolve_grant(source_id, now)
                except Exception:
                    grant = None
                if isinstance(grant, CheckpointGrant) and grant.source_id == source_id:
                    grants[source_id] = grant

            def read_completed(source_id):
                try:
                    return self.journal.completed_result(
                        job, source_id=source_id, grant=grants[source_id], now=now
                    )
                except AdmissionDenied:
                    return None

            source_ids = list(grants)
            if (self.firestore_shared_live or self.postgres_live) and len(source_ids) > 1:
                # Independent immutable checkpoint reads share no transaction.
                # Keep source ordering deterministic while overlapping I/O.
                with ThreadPoolExecutor(max_workers=min(4, len(source_ids))) as executor:
                    resolved = dict(zip(source_ids, executor.map(read_completed, source_ids), strict=True))
            else:
                resolved = {source_id: read_completed(source_id) for source_id in source_ids}
            for source_id in sorted(selected_sources):
                checkpoint = job.get("source_checkpoints", {}).get(source_id)
                checkpoint_snapshot = (checkpoint.get("snapshot")
                                       if isinstance(checkpoint, dict) else None)
                checkpoint_complete = (
                    isinstance(checkpoint_snapshot, dict)
                    and checkpoint_snapshot.get("phase") == "COMPLETE"
                )
                if not checkpoint_complete:
                    missing = _terminal_missing_source_result(
                        job["state"], source_id, job.get("terminal_reason"),
                    )
                    source_results.append(missing)
                    continue
                if source_id not in grants:
                    source_results.append({"source_id": source_id, "status": "UNAVAILABLE",
                                          "error_code": "RETENTION_POLICY_UNAVAILABLE"})
                    continue
                result = resolved[source_id]
                if result is None:
                    source_results.append({"source_id": source_id, "status": "UNAVAILABLE",
                                          "record_count": 0, "records": [],
                                          "error_code": "RESULT_UNAVAILABLE",
                                          "coverage": {"checkpoint_status": "RESULT_UNAVAILABLE"}})
                    continue
                if result["status"] != "UNAVAILABLE":
                    modes.add(result["data_mode"])
                delivery_state = result.get("delivery_state")
                if delivery_state not in {"FRESH", "CACHED", "STALE_CACHE", "NOT_ELIGIBLE"}:
                    delivery_state = "FRESH"
                    if result["status"] in {"FAILED", "UNAVAILABLE"}:
                        delivery_state = None
                coverage = result.get("coverage")
                coverage = coverage if isinstance(coverage, dict) else {}
                request_count = result.get("provider_request_count")
                if type(request_count) is not int or request_count < 0:
                    request_count = coverage.get("provider_request_count")
                if type(request_count) is not int or request_count < 0:
                    request_count = None
                source_results.append({
                    "source_id": source_id,
                    "status": result["status"],
                    **({"data_mode": result["data_mode"]}
                       if result.get("data_mode") in {"LIVE", "SYNTHETIC"} else {}),
                    "delivery_state": delivery_state,
                    "record_count": result["record_count"],
                    "records": result["records"],
                    "coverage": coverage,
                    "provider_request_count": request_count,
                    "error_code": result.get("error_code"),
                    "retry_after_seconds": result.get("retry_after_seconds"),
                    "quality_flags": result.get("quality_flags", []),
                    **({"transport_diagnostic": result["transport_diagnostic"]}
                       if "transport_diagnostic" in result else {}),
                })
        selected_sources = ([WIKIMEDIA_SOURCE_ID]
            if job.get("request_metadata", {}).get("kind") == "WIKIMEDIA_HISTORY"
            else job.get("request_metadata", {}).get(
                "sources", sorted(self.admission.policy.enabled_sources)))
        progress_sources = []
        completed_sources = 0
        if not terminal:
            checkpoints = job.get("source_checkpoints", {})
            checkpoints = checkpoints if isinstance(checkpoints, dict) else {}
            for source_id in sorted(selected_sources):
                entry = checkpoints.get(source_id)
                snapshot = entry.get("snapshot") if isinstance(entry, dict) else None
                result = snapshot.get("result") if isinstance(snapshot, dict) else None
                complete = (isinstance(snapshot, dict)
                            and snapshot.get("phase") == "COMPLETE"
                            and isinstance(result, dict))
                if complete:
                    completed_sources += 1
                    progress_sources.append({
                        "source_id": source_id,
                        "status": result.get("status", "UNKNOWN"),
                        "record_count": result.get("record_count"),
                        "delivery_state": result.get("delivery_state"),
                        "error_code": result.get("error_code"),
                        "coverage": result.get("coverage", {}),
                    })
                else:
                    progress_sources.append({"source_id": source_id, "status": job["state"]})
        collection_input_unchanged = bool(
            terminal and (self.firestore_shared_live or self.postgres_live)
            and isinstance(summary, dict)
            and reusable_collection_input(
                source_results,
                summary.get("source_manifest"),
                processing_status=summary.get("processing_status"),
            )
        )
        discovery = None
        if terminal and include_discovery and not collection_input_unchanged:
            try:
                grants = self.resolve_processing_grants(now) if callable(self.resolve_processing_grants) else ()
                discovery = discover_fetch_results(source_results, now=now, processing_grants=grants,
                                                   empty_mode=self.data_mode)
            except Exception as exc:
                discovery = {
                    "status": "UNAVAILABLE",
                    "data_mode": self.data_mode,
                    "error_code": type(exc).__name__,
                    "candidate_count": 0,
                    "shown_count": 0,
                    "rejected_record_count": 0,
                    "candidates": [],
                    "limitations": ["DISCOVERY_PROCESSING_FAILED"],
                }
        if include_records or self.data_mode != "LIVE":
            visible_sources = source_results
        else:
            visible_sources = []
            for item in source_results:
                if item.get("source_id") == YOUTUBE_SOURCE_ID:
                    public_records = [
                        projected for record in item.get("records", [])
                        if (projected := _public_youtube_record(record)) is not None
                    ] if isinstance(item.get("records"), list) else []
                    visible = {key: value for key, value in item.items() if key != "records"}
                    if public_records:
                        visible["records"] = public_records
                    visible_sources.append(visible)
                else:
                    visible_sources.append({
                        key: value for key, value in item.items() if key != "records"
                    })
        retry_at = None
        if job.get("state") == "QUEUED" and job.get("recovery_available") is not True:
            try:
                available_at = datetime.fromisoformat(job["available_at"])
                created_at = datetime.fromisoformat(job["created_at"])
                if (available_at.tzinfo is not None and created_at.tzinfo is not None
                        and available_at > created_at):
                    retry_at = available_at.isoformat()
            except (KeyError, TypeError, ValueError):
                retry_at = None
        return {
            "fetch_id": job["job_id"],
            "status": job["state"],
            "stage": job["state"],
            **({"lease_until": job["lease_until"]}
               if job.get("state") == "RUNNING"
               and isinstance(job.get("lease_until"), str) else {}),
            "request_kind": job.get("request_metadata", {}).get("kind", "LATEST"),
            "fetch_mode": ("CACHE_ONLY"
                           if job.get("request_metadata", {}).get("cache_only") is True
                           else "LIVE"),
            "selected_sources": selected_sources,
            **({"history_days": job["request_metadata"]["history_days"]}
               if job.get("request_metadata", {}).get("kind") == "WIKIMEDIA_HISTORY" else {}),
            "data_mode": next(iter(modes)) if len(modes) == 1 else "MIXED" if modes else None,
            "sources": visible_sources if terminal else progress_sources,
            "progress": {
                "completed_sources": (len(selected_sources) if terminal else completed_sources),
                "total_sources": len(selected_sources),
                "percent": (100 if terminal else int(completed_sources * 100 / len(selected_sources))),
            },
            "discovery": discovery,
            "created_at": job["created_at"],
            "updated_at": job.get("updated_at") or job.get("available_at"),
            "finished_at": job.get("finished_at"),
            **({"terminal_reason": job["terminal_reason"]}
               if isinstance(job.get("terminal_reason"), str) else {}),
            "terminal": terminal or job["state"] in {"FAILED", "EXPIRED"},
            "recovery_available": job.get("recovery_available") is True,
            "collection_input_unchanged": collection_input_unchanged,
            **({"retry_at": retry_at} if retry_at is not None else {}),
        }

    def terminal_status_from_worker_result(self, worker_result: dict, *, created_at=None) -> dict | None:
        """Build the terminal public response from results already in this worker.

        A terminal continuation has just validated and checkpointed every
        source. Re-reading the shared job and loading its immutable blobs here
        would repeat the same work before the browser can display the result.
        """
        if (not isinstance(worker_result, dict)
                or worker_result.get("state") not in {"COMPLETED", "PARTIAL"}
                or not isinstance(worker_result.get("sources"), dict)):
            return None
        outcomes = worker_result["sources"]
        selected_sources = sorted(outcomes)
        if (not selected_sources or any(not isinstance(source_id, str)
                                        or not isinstance(outcomes[source_id], dict)
                                        for source_id in selected_sources)):
            return None
        source_results = []
        modes = set()
        for source_id in selected_sources:
            result = outcomes[source_id]
            result_mode = result.get("data_mode")
            if result_mode in {"LIVE", "SYNTHETIC"}:
                modes.add(result_mode)
            coverage = result.get("coverage")
            coverage = coverage if isinstance(coverage, dict) else {}
            request_count = result.get("provider_request_count")
            if type(request_count) is not int or request_count < 0:
                request_count = coverage.get("provider_request_count")
            if type(request_count) is not int or request_count < 0:
                request_count = None
            source_results.append({
                "source_id": source_id,
                "status": result.get("status"),
                **({"data_mode": result_mode} if result_mode in {"LIVE", "SYNTHETIC"} else {}),
                "delivery_state": result.get("delivery_state"),
                "record_count": result.get("record_count"),
                "records": result.get("records", []),
                "coverage": coverage,
                "provider_request_count": request_count,
                "error_code": result.get("error_code"),
                "retry_after_seconds": result.get("retry_after_seconds"),
                "quality_flags": result.get("quality_flags", []),
                **({"transport_diagnostic": result["transport_diagnostic"]}
                   if "transport_diagnostic" in result else {}),
            })
        summary = self._latest_reuse_summary()
        collection_input_unchanged = bool(
            (self.firestore_shared_live or self.postgres_live)
            and isinstance(summary, dict)
            and reusable_collection_input(
                source_results, summary.get("source_manifest"),
                processing_status=summary.get("processing_status"),
            )
        )
        discovery = None
        if not collection_input_unchanged:
            now = self.utcnow()
            try:
                grants = (self.resolve_processing_grants(now)
                          if callable(self.resolve_processing_grants) else ())
                discovery = discover_fetch_results(
                    source_results, now=now, processing_grants=grants,
                    empty_mode=self.data_mode,
                )
            except Exception as exc:
                discovery = {
                    "status": "UNAVAILABLE", "data_mode": self.data_mode,
                    "error_code": type(exc).__name__, "candidate_count": 0,
                    "shown_count": 0, "rejected_record_count": 0, "candidates": [],
                    "limitations": ["DISCOVERY_PROCESSING_FAILED"],
                }
        if self.data_mode != "LIVE":
            visible_sources = source_results
        else:
            visible_sources = []
            for item in source_results:
                if item.get("source_id") == YOUTUBE_SOURCE_ID:
                    projected = [
                        safe for record in item.get("records", [])
                        if (safe := _public_youtube_record(record)) is not None
                    ]
                    visible = {key: value for key, value in item.items() if key != "records"}
                    if projected:
                        visible["records"] = projected
                    visible_sources.append(visible)
                else:
                    visible_sources.append({
                        key: value for key, value in item.items() if key != "records"
                    })
        finished_at = worker_result.get("finished_at")
        result = {
            "fetch_id": worker_result.get("job_id"),
            "status": worker_result["state"],
            "stage": worker_result["state"],
            "request_kind": "LATEST",
            "selected_sources": selected_sources,
            "data_mode": (next(iter(modes)) if len(modes) == 1
                          else "MIXED" if modes else None),
            "sources": visible_sources,
            "progress": {"completed_sources": len(selected_sources),
                         "total_sources": len(selected_sources), "percent": 100},
            "discovery": discovery,
            "created_at": created_at or worker_result.get("created_at"),
            "updated_at": finished_at,
            "finished_at": finished_at,
            "terminal": True,
            "recovery_available": False,
        }
        result["collection_input_unchanged"] = collection_input_unchanged
        return result

    def collection_reuse_for_fetch(self, job_id: str, *, job=None,
                                   _summary=None, _summary_loaded=False) -> dict | None:
        """Compare compact terminal receipts with the latest dataset manifest.

        The checkpoint shell contains counts, provenance, and the source input
        digest even when its records are stored in immutable blobs. This check
        avoids downloading those blobs or restoring the SQLite snapshot when a
        Fetch is an exact cache-only replay of the current dataset.
        """
        if not (self.firestore_shared_live or self.postgres_live):
            return None
        summary = _summary if _summary_loaded else self._latest_reuse_summary()
        if not isinstance(summary, dict):
            return None
        if job is None:
            read_receipt = getattr(self.admission, "read_job_unmetered", None)
            if not callable(read_receipt):
                return None
            job = read_receipt(job_id)
        if job is None or job.get("state") not in {
            "COMPLETED", "PARTIAL", "FAILED", "EXPIRED", "ABORTED",
        }:
            return None
        metadata = job.get("request_metadata", {})
        if not isinstance(metadata, dict):
            return None
        selected = ([WIKIMEDIA_SOURCE_ID]
                    if metadata.get("kind") == "WIKIMEDIA_HISTORY"
                    else metadata.get("sources", sorted(self.admission.policy.enabled_sources)))
        checkpoints = job.get("source_checkpoints", {})
        if (not isinstance(selected, list) or not selected
                or not isinstance(checkpoints, dict)):
            return None
        manifest = []
        for source_id in sorted(selected):
            entry = checkpoints.get(source_id)
            snapshot = entry.get("snapshot") if isinstance(entry, dict) else None
            source = snapshot.get("result") if isinstance(snapshot, dict) else None
            if (not isinstance(snapshot, dict) or snapshot.get("phase") != "COMPLETE"
                    or not isinstance(source, dict)):
                return None
            record_count = source.get("record_count")
            if type(record_count) is not int or record_count < 0:
                return None
            storage = entry.get("snapshot_storage", "INLINE")
            if storage == "EXTERNAL_RECORDS":
                reference = entry.get("snapshot_ref")
                if (not isinstance(reference, dict)
                        or reference.get("path") != ["result", "records"]
                        or reference.get("item_count") != record_count
                        or not isinstance(reference.get("digest"), str)
                        or re.fullmatch(r"[a-f0-9]{64}", reference["digest"]) is None
                        or type(reference.get("raw_bytes")) is not int
                        or reference["raw_bytes"] < 0):
                    return None
            elif storage == "INLINE":
                records = source.get("records")
                if not isinstance(records, list) or len(records) != record_count:
                    return None
            else:
                return None
            coverage = source.get("coverage")
            coverage = coverage if isinstance(coverage, dict) else {}
            request_count = source.get("provider_request_count")
            if type(request_count) is not int or request_count < 0:
                request_count = coverage.get("provider_request_count")
            if type(request_count) is not int or request_count < 0:
                request_count = None
            item = {
                "source_id": source_id,
                "status": source.get("status"),
                "data_mode": source.get("data_mode"),
                "delivery_state": source.get("delivery_state"),
                "record_count": record_count,
                "provider_request_count": request_count,
                "coverage": coverage,
                "quality_flags": source.get("quality_flags", []),
                "error_code": source.get("error_code"),
                "retry_after_seconds": source.get("retry_after_seconds"),
            }
            if "transport_diagnostic" in source:
                item["transport_diagnostic"] = source["transport_diagnostic"]
            if isinstance(snapshot.get("source_input_digest"), str):
                item["source_input_digest"] = snapshot["source_input_digest"]
            if type(snapshot.get("source_input_digest_version")) is int:
                item["source_input_digest_version"] = snapshot[
                    "source_input_digest_version"
                ]
            if isinstance(snapshot.get("legacy_source_input_digest"), str):
                item["legacy_source_input_digest"] = snapshot[
                    "legacy_source_input_digest"
                ]
            if isinstance(snapshot.get("legacy_source_input_digests"), list):
                item["legacy_source_input_digests"] = snapshot[
                    "legacy_source_input_digests"
                ]
            manifest.append(item)
        unchanged = reusable_collection_manifest(
            manifest, summary.get("source_manifest"),
            processing_status=summary.get("processing_status"),
        )
        return {
            "fetch_id": job_id,
            "status": job["state"],
            "terminal": True,
            "collection_input_unchanged": unchanged,
            "source_manifest": manifest,
            **({"dataset_id": summary.get("dataset_id")}
               if unchanged and isinstance(summary.get("dataset_id"), str) else {}),
        }

    def history(self, *, limit: int = 20) -> list[dict]:
        """Return shared job summaries without charging the per-job poll budget."""
        jobs = self.admission.list_jobs(limit=limit, now=self.utcnow())
        summary = self._latest_reuse_summary()
        items = []
        for job in jobs:
            metadata = job.get("request_metadata", {})
            selected = ([WIKIMEDIA_SOURCE_ID]
                if metadata.get("kind") == "WIKIMEDIA_HISTORY"
                else metadata.get("sources", sorted(self.admission.policy.enabled_sources)))
            if not isinstance(selected, list) or any(source not in self.admission.policy.enabled_sources
                                                      for source in selected):
                raise AdmissionDenied("JOB_STATE_INVALID")
            checkpoints = job.get("source_checkpoints", {})
            if not isinstance(checkpoints, dict):
                raise AdmissionDenied("JOB_STATE_INVALID")
            manifest = []
            received = 0
            for source_id in sorted(selected):
                entry = checkpoints.get(source_id)
                snapshot = entry.get("snapshot") if isinstance(entry, dict) else None
                result = snapshot.get("result") if isinstance(snapshot, dict) else None
                complete = (isinstance(snapshot, dict)
                            and snapshot.get("phase") == "COMPLETE"
                            and isinstance(result, dict))
                status = result.get("status") if complete else job.get("state")
                error_code = result.get("error_code") if complete else None
                coverage = result.get("coverage") if complete else {}
                if not complete and job.get("state") in {
                    "COMPLETED", "PARTIAL", "FAILED", "EXPIRED", "ABORTED",
                }:
                    missing = _terminal_missing_source_result(
                        job["state"], source_id, self.data_mode,
                        job.get("terminal_reason"),
                    )
                    status = missing["status"]
                    error_code = missing["error_code"]
                    coverage = missing["coverage"]
                if status not in {"QUEUED", "RUNNING", "DEFERRED", "SUCCESS", "PARTIAL",
                                  "FAILED", "UNAVAILABLE", "COMPLETED", "EXPIRED", "ABORTED"}:
                    status = "UNKNOWN"
                record_count = result.get("record_count") if complete else None
                if type(record_count) is int and record_count >= 0:
                    received += record_count
                quality_flags = result.get("quality_flags", []) if complete else []
                if not isinstance(quality_flags, list):
                    quality_flags = []
                provider_request_count = (
                    result.get("provider_request_count") if complete else None
                )
                if (type(provider_request_count) is not int
                        or provider_request_count < 0):
                    provider_request_count = (
                        coverage.get("provider_request_count")
                        if isinstance(coverage, dict) else None
                    )
                if (type(provider_request_count) is not int
                        or provider_request_count < 0):
                    provider_request_count = None
                manifest.append({
                    "source_id": source_id,
                    "status": status,
                    "data_mode": result.get("data_mode") if complete else self.data_mode,
                    "delivery_state": result.get("delivery_state") if complete else None,
                    "record_count": record_count if type(record_count) is int and record_count >= 0 else None,
                    "quality_flags": quality_flags,
                    "provider_request_count": provider_request_count,
                    **({"source_input_digest": snapshot["source_input_digest"]}
                       if complete and isinstance(snapshot.get("source_input_digest"), str)
                       else {}),
                    **({"source_input_digest_version": snapshot["source_input_digest_version"]}
                       if complete and type(snapshot.get("source_input_digest_version")) is int
                       else {}),
                    **({"legacy_source_input_digest": snapshot["legacy_source_input_digest"]}
                       if complete and isinstance(snapshot.get("legacy_source_input_digest"), str)
                       else {}),
                    **({"legacy_source_input_digests": snapshot["legacy_source_input_digests"]}
                       if complete and isinstance(snapshot.get("legacy_source_input_digests"), list)
                       else {}),
                    "error_code": error_code,
                    "coverage": coverage if isinstance(coverage, dict) else {},
                })
            collection_input_unchanged = bool(
                (self.firestore_shared_live or self.postgres_live)
                and isinstance(summary, dict)
                and reusable_collection_manifest(
                    manifest,
                    summary.get("source_manifest"),
                    processing_status=summary.get("processing_status"),
                )
            )
            for source in manifest:
                for field in PRIVATE_SOURCE_DIGEST_FIELDS:
                    source.pop(field, None)
            items.append({
                "fetch_id": job["job_id"],
                "run_type": "FETCH",
                "status": job.get("state", "UNKNOWN"),
                "outcome_status": job.get("state", "UNKNOWN"),
                "mode": "LIVE" if self.data_mode == "LIVE" else self.data_mode,
                "data_mode": "LIVE" if self.data_mode == "LIVE" else self.data_mode,
                "sources": list(selected),
                "request_kind": metadata.get("kind", "LATEST"),
                "fetch_mode": "CACHE_ONLY" if metadata.get("cache_only") is True else "LIVE",
                **({"history_days": metadata["history_days"]}
                   if metadata.get("kind") == "WIKIMEDIA_HISTORY" else {}),
                "source_manifest": manifest,
                "received_record_count": received,
                "retained_keyword_count": None,
                "rejected_record_count": None,
                "run_at": job.get("finished_at") or job.get("updated_at") or job.get("created_at"),
                "created_at": job.get("created_at"),
                "updated_at": job.get("updated_at") or job.get("created_at"),
                "fetched_at": job.get("finished_at"),
                "recovery_available": job.get("recovery_available") is True,
                "error_code": None,
                "duration_seconds": None,
                "progress": {
                    "completed_sources": sum(1 for source in manifest if source["status"] in {
                        "SUCCESS", "PARTIAL", "FAILED", "UNAVAILABLE",
                    }),
                    "total_sources": len(manifest),
                },
                "collection_input_unchanged": collection_input_unchanged,
            })
        return items

    async def run_once(self, owner: str):
        return await self.worker.run_once(owner)

    async def run_slice(self, owner: str, fetch_id: str, *, max_sources: int = 1):
        """Continue one requested shared Fetch through a bounded source slice."""
        previous_manifest = None
        summary = await asyncio.to_thread(self._latest_reuse_summary)
        if ((self.firestore_shared_live or self.postgres_live)
                and isinstance(summary, dict)):
            candidate = summary.get("source_manifest")
            if isinstance(candidate, list):
                previous_manifest = candidate
        return await self.worker.run_once(
            owner, job_id=fetch_id, max_sources=max_sources,
            continue_cached=(self.firestore_shared_live or self.postgres_live),
            previous_source_manifest=previous_manifest,
        )
