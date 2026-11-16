"""Public Fetch facade over durable admission and retained source results.

The facade has no provider configuration surface. Its admission and result reads
are usable only when explicitly injected by backend composition.
"""

import os
from dataclasses import replace
from datetime import datetime, timezone

from app.discovery.source_results import discover_fetch_results
from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.jobs.source_checkpoints import CheckpointGrant
from app.sources.catalog import WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID


class DistributedPublicFetch:
    def __init__(self, admission: PublicAdmission, worker: DistributedFetchWorker, *,
                 resolve_grant, utcnow=lambda: datetime.now(timezone.utc),
                 local_live=False, normal_wikimedia_live=False, normal_google_live=False,
                 resolve_processing_grants=None, provider_retry_after_seconds=None):
        if (not isinstance(admission, PublicAdmission)
                or not isinstance(worker, DistributedFetchWorker)
                or worker.admission.policy_digest != admission.policy_digest
                or not callable(resolve_grant) or not callable(utcnow)
                or type(local_live) is not bool or type(normal_wikimedia_live) is not bool
                or type(normal_google_live) is not bool
                or sum((local_live, normal_wikimedia_live, normal_google_live)) > 1
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
            if (os.environ.get("VERCEL") or declared_modes != {"LIVE"}
                    or worker.admission is not admission
                    or type(admission.store) is not LocalSQLiteAtomicStateStore
                    or admission.store.data_mode != "LIVE"
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
        else:
            if declared_modes != {"SYNTHETIC"} or resolve_processing_grants is not None:
                raise ValueError("public demo Fetch requires explicitly synthetic source handlers")
            self.data_mode = "SYNTHETIC"
        self.resolve_processing_grants = resolve_processing_grants
        self.provider_retry_after_seconds = provider_retry_after_seconds

    def submit(self, idempotency_key: str, *, history_days: int | None = None,
               source_ids: list[str] | None = None) -> dict:
        if history_days is not None:
            if (source_ids is not None or not self.normal_wikimedia_live
                    or type(history_days) is not int or not 1 <= history_days <= 2):
                raise AdmissionDenied("HISTORICAL_FETCH_UNAVAILABLE")
            request_metadata = {"kind": "WIKIMEDIA_HISTORY", "history_days": history_days}
        elif source_ids is not None:
            request_metadata = {"sources": source_ids}
        else:
            request_metadata = None
        if self.provider_retry_after_seconds is not None:
            try:
                retry_after = self.provider_retry_after_seconds()
            except Exception:
                raise AdmissionDenied("ADMISSION_STATE_UNAVAILABLE") from None
            if type(retry_after) is not int or retry_after < 0:
                raise AdmissionDenied("ADMISSION_STATE_UNAVAILABLE")
            if retry_after:
                raise AdmissionDenied("PROVIDER_RATE_LIMITED")
        accepted = self.admission.admit(idempotency_key, now=self.utcnow(),
                                        request_metadata=request_metadata)
        return {"job_id": accepted["job_id"], "result": accepted["result"],
                "status": accepted["status"], "created_at": accepted.get("created_at")}

    def status(self, job_id: str, *, include_records: bool = False) -> dict | None:
        now = self.utcnow()
        job = self.admission.read_job(job_id, now=now)
        if job is None:
            return None
        source_results = []
        modes = set()
        terminal = job["state"] in {"COMPLETED", "PARTIAL"}
        if terminal:
            request_metadata = job.get("request_metadata", {})
            selected_sources = ([WIKIMEDIA_SOURCE_ID]
                if request_metadata.get("kind") == "WIKIMEDIA_HISTORY"
                else request_metadata.get("sources", sorted(self.admission.policy.enabled_sources)))
            for source_id in sorted(selected_sources):
                try:
                    grant = self.resolve_grant(source_id, now)
                except Exception:
                    grant = None
                if not isinstance(grant, CheckpointGrant) or grant.source_id != source_id:
                    source_results.append({"source_id": source_id, "status": "UNAVAILABLE",
                                          "error_code": "RETENTION_POLICY_UNAVAILABLE"})
                    continue
                try:
                    result = self.journal.completed_result(
                        job, source_id=source_id, grant=grant, now=now
                    )
                except AdmissionDenied:
                    result = None
                if result is None:
                    source_results.append({"source_id": source_id, "status": "UNAVAILABLE",
                                          "error_code": "RESULT_UNAVAILABLE"})
                    continue
                if result["status"] != "UNAVAILABLE":
                    modes.add(result["data_mode"])
                delivery_state = result.get("delivery_state")
                if result["status"] in {"FAILED", "UNAVAILABLE"}:
                    delivery_state = None
                elif delivery_state not in {"FRESH", "CACHED", "STALE_CACHE"}:
                    delivery_state = "FRESH"
                source_results.append({
                    "source_id": source_id,
                    "status": result["status"],
                    **({"data_mode": result["data_mode"]} if result["status"] != "UNAVAILABLE" else {}),
                    "delivery_state": delivery_state,
                    "record_count": result["record_count"],
                    "records": result["records"],
                    "coverage": result.get("coverage"),
                    "error_code": result.get("error_code"),
                    "retry_after_seconds": result.get("retry_after_seconds"),
                    "quality_flags": result.get("quality_flags", []),
                    **({"transport_diagnostic": result["transport_diagnostic"]}
                       if "transport_diagnostic" in result else {}),
                })
        discovery = None
        if terminal:
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
        visible_sources = source_results if include_records or self.data_mode != "LIVE" else [
            (item if item.get("source_id") == YOUTUBE_SOURCE_ID else
             {key: value for key, value in item.items() if key != "records"})
            for item in source_results
        ]
        return {
            "fetch_id": job["job_id"],
            "status": job["state"],
            "stage": job["state"],
            "request_kind": job.get("request_metadata", {}).get("kind", "LATEST"),
            "selected_sources": ([WIKIMEDIA_SOURCE_ID]
                if job.get("request_metadata", {}).get("kind") == "WIKIMEDIA_HISTORY"
                else job.get("request_metadata", {}).get(
                    "sources", sorted(self.admission.policy.enabled_sources))),
            **({"history_days": job["request_metadata"]["history_days"]}
               if job.get("request_metadata", {}).get("kind") == "WIKIMEDIA_HISTORY" else {}),
            "data_mode": next(iter(modes)) if len(modes) == 1 else "MIXED" if modes else None,
            "sources": visible_sources if terminal else [
                {"source_id": source_id, "status": job["state"]}
                for source_id in sorted(
                    [WIKIMEDIA_SOURCE_ID]
                    if job.get("request_metadata", {}).get("kind") == "WIKIMEDIA_HISTORY"
                    else job.get("request_metadata", {}).get(
                        "sources", self.admission.policy.enabled_sources))
            ],
            "discovery": discovery,
            "created_at": job["created_at"],
            "updated_at": job.get("updated_at") or job.get("available_at"),
            "finished_at": job.get("finished_at"),
            "terminal": terminal or job["state"] in {"FAILED", "EXPIRED"},
        }

    async def run_once(self, owner: str):
        return await self.worker.run_once(owner)
