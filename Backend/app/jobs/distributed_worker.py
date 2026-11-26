"""Fenced worker loop for source-scoped jobs stored by ``PublicAdmission``.

The worker is used by the guarded local LIVE Fetch only when an explicit source
composition is selected. Production deployment and production Fetch remain
separate opt-in decisions; offline tests use local or injected Firestore stores.
"""

import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Awaitable, Callable, Mapping

from app.collections.source_digest import (
    COLLECTION_INPUT_DIGEST_VERSION,
    legacy_source_input_digest_candidates,
    source_input_digest,
)
from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.jobs.source_checkpoints import CheckpointGrant, SourceCheckpointJournal
from app.sources.catalog import WIKIMEDIA_SOURCE_ID

_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SAFE_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SAFE_EXCEPTION_CLASS = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SAFE_SOURCE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CACHE_DELIVERY_STATES = frozenset({"CACHED", "STALE_CACHE"})
_logger = logging.getLogger(__name__)
_MAX_SOURCE_RESULT_BYTES = 4_900_000
_TERMINAL_STATUSES = frozenset({"SUCCESS", "PARTIAL", "FAILED", "UNAVAILABLE"})
_SOURCE_FAILURE_PHASES = frozenset({
    "SOURCE_ADMISSION", "SOURCE_EXECUTION", "RESULT_VALIDATION",
    "RESULT_SERIALIZATION",
})
_SOURCE_FAILURE_REASONS = frozenset({
    "ADMISSION_DENIED", "HANDLER_EXCEPTION", "INVALID_SOURCE_RESULT",
    "RESULT_NOT_SERIALIZABLE",
})
_PERSISTENCE_FAILURE_REASONS = frozenset({
    "CALLBACK_ERROR", "CALLBACK_TIMEOUT", "DEADLINE_EXPIRED",
})
_TRANSPORT_FAILURE_PHASES = frozenset({
    "REQUEST_DISPATCH", "RESPONSE_HEADERS", "BODY_STREAM", "JSON_DECODE", "SCHEMA_VALIDATION",
})
_TRANSPORT_EXCEPTION_CLASSES = frozenset({
    "ConnectError", "ConnectTimeout", "ReadError", "ReadTimeout", "RemoteProtocolError",
    "DecodingError", "WriteError", "WriteTimeout", "PoolTimeout", "CloseError",
    "RequestError", "TimeoutException", "HttpDeadlineExceeded", "TimeoutError",
    "JSONDecodeError", "ValueError", "TypeError", "RecursionError",
})


class _SourceResultSerializationError(ValueError):
    def __init__(self, exception_class):
        super().__init__("source result is not serializable")
        self.exception_class = exception_class


@dataclass(frozen=True)
class SourceWorkContext:
    job_id: str
    owner: str
    fence: int
    source_id: str
    plan_fingerprint: str
    job_expires_at: datetime
    resume_snapshot: dict | None
    checkpoint: Callable[[dict], int]
    heartbeat: Callable[[], str]
    request_metadata: dict = field(default_factory=dict)
    completed_sources: dict = field(default_factory=dict)


SourceHandler = Callable[[SourceWorkContext], dict | Awaitable[dict]]
GrantResolver = Callable[[str, datetime], CheckpointGrant]


class DistributedFetchWorker:
    """Claim one durable job, checkpoint each source under its lease, then finish.

    Source handlers are server-owned and must use the guarded gateway/attempt
    boundary. They may persist bounded progress through ``context.checkpoint``.
    A resumed handler receives the last committed snapshot and must not repeat a
    provider request whose reservation receipt already exists.
    """

    def __init__(self, admission: PublicAdmission, *,
                 handlers: Mapping[str, SourceHandler],
                 cache_only_handlers: Mapping[str, SourceHandler] | None = None,
                 resolve_grant: GrantResolver,
                 utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 snapshot_blobs=None,
                 source_order: tuple[str, ...] | None = None):
        if not isinstance(admission, PublicAdmission):
            raise TypeError("PublicAdmission required")
        order = tuple(sorted(handlers)) if source_order is None else source_order
        if (set(handlers) != set(admission.policy.enabled_sources)
                or any(not callable(handler) for handler in handlers.values())
                or not isinstance(order, tuple) or len(order) != len(handlers)
                or set(order) != set(handlers)
                or not callable(resolve_grant) or not callable(utcnow)):
            raise ValueError("complete server-owned handler and grant registries required")
        self.admission = admission
        self.handlers = dict(handlers)
        if cache_only_handlers is not None and (
                not isinstance(cache_only_handlers, Mapping)
                or set(cache_only_handlers) != set(self.handlers)
                or any(not callable(handler) for handler in cache_only_handlers.values())):
            raise ValueError("complete server-owned cache-only handler registry required")
        self.cache_only_handlers = (dict(cache_only_handlers)
                                    if cache_only_handlers is not None else None)
        self.source_order = order
        self.source_modes = {
            source_id: getattr(handler, "__trendscout_data_mode__", None)
            for source_id, handler in self.handlers.items()
        }
        if any(mode is not None and mode not in {"SYNTHETIC", "LIVE"}
               for mode in self.source_modes.values()):
            raise ValueError("invalid server-owned source mode declaration")
        if self.cache_only_handlers is not None:
            cache_modes = {
                source_id: getattr(handler, "__trendscout_data_mode__", None)
                for source_id, handler in self.cache_only_handlers.items()
            }
            if cache_modes != self.source_modes:
                raise ValueError("cache-only handlers must preserve source data-mode declarations")
        self.resolve_grant = resolve_grant
        self.utcnow = utcnow
        self.journal = SourceCheckpointJournal(admission, snapshot_blobs=snapshot_blobs)

    async def run_once(self, owner: str, *, job_id: str | None = None,
                       max_sources: int | None = None,
                       continue_cached: bool = False,
                       previous_source_manifest: list[dict] | None = None) -> dict | None:
        if (job_id is not None and (not isinstance(job_id, str) or not job_id)
                or type(continue_cached) is not bool
                or previous_source_manifest is not None
                and not isinstance(previous_source_manifest, list)
                or continue_cached and max_sources is None
                or max_sources is not None and (
                    type(max_sources) is not int or not 1 <= max_sources <= len(self.handlers)
                )
                or max_sources is not None and job_id is None):
            raise ValueError("a valid job and bounded source slice are required")
        now = self.utcnow()
        claim = (self.admission.claim_job(job_id, owner, now=now) if job_id is not None
                 else self.admission.claim_next(owner, now=now))
        if claim is None:
            return None
        job_id, fence = claim["job_id"], claim["fence"]
        job_expires_at = datetime.fromisoformat(claim["expires_at"])
        outcomes = {}
        deferred_until = None
        processed_sources = 0
        pending_checkpoints = []
        previous_by_source = {}
        if isinstance(previous_source_manifest, list):
            for item in previous_source_manifest:
                source_id = item.get("source_id") if isinstance(item, dict) else None
                if isinstance(source_id, str) and source_id not in previous_by_source:
                    previous_by_source[source_id] = item

        def flush_pending_checkpoints():
            if not pending_checkpoints:
                return
            save_started = perf_counter()
            revisions = self.journal.save_many(
                job_id, owner, fence, now=self.utcnow(),
                checkpoints=[{
                    "source_id": item["source_id"],
                    "grant": item["grant"],
                    "expected_revision": item["expected_revision"],
                    "snapshot": item["snapshot"],
                } for item in pending_checkpoints],
            )
            save_finished = perf_counter()
            batch_save_ms = int((save_finished - save_started) * 1000)
            batch_size = len(pending_checkpoints)
            for item in pending_checkpoints:
                result = item["result"]
                delivery = result.get("delivery_state")
                if delivery not in ("FRESH", "CACHED", "STALE_CACHE", "NOT_ELIGIBLE"):
                    delivery = "UNKNOWN"
                count = result.get("provider_request_count")
                if type(count) is not int or count < 0:
                    coverage = result.get("coverage")
                    count = (coverage.get("provider_request_count")
                             if isinstance(coverage, dict) else None)
                if type(count) is not int or count < 0:
                    count = "UNKNOWN"
                _logger.info(
                    "Fetch source timing source=%s status=%s delivery=%s provider_requests=%s "
                    "checkpoint_load_ms=%d handler_ms=%d checkpoint_save_ms=%d "
                    "checkpoint_batch_size=%d checkpoint_revision=%d",
                    item["source_id"] if _SAFE_SOURCE.fullmatch(item["source_id"])
                    else "unknown",
                    result["status"], delivery, count,
                    item["checkpoint_load_ms"], item["handler_ms"], batch_save_ms,
                    batch_size, revisions[item["source_id"]],
                )
            pending_checkpoints.clear()

        request_metadata = claim.get("request_metadata", {})
        selected_sources = ([WIKIMEDIA_SOURCE_ID]
            if request_metadata.get("kind") == "WIKIMEDIA_HISTORY"
            else request_metadata.get("sources", sorted(self.handlers)))
        if (not isinstance(selected_sources, list) or not selected_sources
                or len(selected_sources) != len(set(selected_sources))
                or not set(selected_sources) <= set(self.handlers)):
            raise AdmissionDenied("REQUEST_SCOPE_INVALID")
        selected = set(selected_sources)
        for source_id in self.source_order:
            if source_id not in selected:
                continue
            if max_sources is not None and processed_sources >= max_sources:
                flush_pending_checkpoints()
                self.admission.yield_for_continuation(
                    job_id, owner, fence, now=self.utcnow(),
                )
                return {"job_id": job_id, "state": "QUEUED", "sources": outcomes}
            now = self.utcnow()
            try:
                grant = self.resolve_grant(source_id, now)
            except Exception:
                raise AdmissionDenied("CHECKPOINT_POLICY_UNAVAILABLE") from None
            if not isinstance(grant, CheckpointGrant) or grant.source_id != source_id:
                raise AdmissionDenied("CHECKPOINT_POLICY_UNAVAILABLE")
            declared_mode = self.source_modes[source_id]
            if declared_mode is not None and declared_mode != grant.data_mode:
                raise AdmissionDenied("SOURCE_MODE_POLICY_MISMATCH")
            checkpoint_started = perf_counter()
            saved = self.journal.load(job_id, owner, fence, source_id=source_id,
                                      grant=grant, now=now,
                                      claimed_checkpoints=claim.get("source_checkpoints"))
            checkpoint_finished = perf_counter()
            if saved and saved["snapshot"].get("phase") == "COMPLETE":
                outcomes[source_id] = saved["snapshot"]["result"]
                continue
            revision = saved["revision"] if saved else 0
            resume = saved["snapshot"] if saved else None
            revision_box = [revision]

            def checkpoint(progress):
                if not isinstance(progress, dict):
                    raise ValueError("source checkpoint progress must be an object")
                # A terminal cache receipt is immediately followed by the
                # worker's durable COMPLETE checkpoint. Writing it first as
                # RUNNING doubles Firestore transactions without protecting a
                # provider attempt: no provider work occurred and the worker
                # will persist the returned result before finishing the job.
                if self._is_cache_only_checkpoint(progress, grant.data_mode):
                    return revision_box[0]
                snapshot = {
                    "schema": 1,
                    "source_id": source_id,
                    "data_mode": grant.data_mode,
                    "plan_fingerprint": self.admission.policy.plan_fingerprint,
                    "phase": "RUNNING",
                    "progress": progress,
                }
                next_revision = self.journal.save(
                    job_id, owner, fence, source_id=source_id, grant=grant,
                    now=self.utcnow(), expected_revision=revision_box[0], snapshot=snapshot,
                )
                revision_box[0] = next_revision
                return next_revision

            def heartbeat():
                return self.admission.heartbeat(job_id, owner, fence, now=self.utcnow())

            context = SourceWorkContext(job_id, owner, fence, source_id,
                                        self.admission.policy.plan_fingerprint, job_expires_at, resume,
                                        checkpoint, heartbeat,
                                        request_metadata=claim.get("request_metadata", {}),
                                        completed_sources=dict(outcomes))
            try:
                handler_started = perf_counter()
                request_metadata = context.request_metadata
                if (isinstance(request_metadata, dict)
                        and request_metadata.get("cache_only") is True):
                    if self.cache_only_handlers is None:
                        raise AdmissionDenied("CACHE_ONLY_UNAVAILABLE")
                    handler = self.cache_only_handlers[source_id]
                else:
                    handler = self.handlers[source_id]
                result = handler(context)
                if inspect.isawaitable(result):
                    result = await result
            except AdmissionDenied as exc:
                # A source-level admission/persistence failure must not strand
                # the whole durable Fetch before later independent sources run.
                # Lease loss is different: this worker no longer owns the job
                # and must not write a terminal result under a stale fence.
                if exc.code == "LEASE_LOST":
                    raise
                result = self._source_failure_result(
                    source_id, grant.data_mode, exc,
                    phase="SOURCE_ADMISSION", reason="ADMISSION_DENIED",
                )
            except Exception as exc:
                result = self._source_failure_result(
                    source_id, grant.data_mode, exc,
                    phase="SOURCE_EXECUTION", reason="HANDLER_EXCEPTION",
                )
            else:
                try:
                    result = self._validate_result(
                        result, expected_mode=grant.data_mode,
                        expected_source_id=source_id,
                    )
                except Exception as exc:
                    serialization_error = isinstance(exc, _SourceResultSerializationError)
                    result = self._source_failure_result(
                        source_id, grant.data_mode, exc,
                        phase=("RESULT_SERIALIZATION" if serialization_error
                               else "RESULT_VALIDATION"),
                        reason=("RESULT_NOT_SERIALIZABLE" if serialization_error
                                else "INVALID_SOURCE_RESULT"),
                    )

            if result["status"] == "DEFERRED":
                retry_at = result["retry_at"]
                if (not isinstance(retry_at, datetime) or retry_at.tzinfo is None
                        or retry_at.utcoffset() is None):
                    raise ValueError("deferred result requires timezone-aware retry_at")
                if retry_at.astimezone(timezone.utc) >= job_expires_at.astimezone(timezone.utc):
                    result = {"status": "PARTIAL", "data_mode": grant.data_mode,
                              "records": [], "record_count": 0,
                              "error_code": "RETRY_OUTSIDE_DEADLINE"}
                else:
                    deferred_until = retry_at if deferred_until is None else max(deferred_until, retry_at)
                    outcomes[source_id] = {"status": "DEFERRED"}
                    break

            snapshot = {
                "schema": 1,
                "source_id": source_id,
                "data_mode": grant.data_mode,
                "plan_fingerprint": self.admission.policy.plan_fingerprint,
                "phase": "COMPLETE",
                "result": result,
                "source_input_digest": source_input_digest(source_id, result),
                "source_input_digest_version": COLLECTION_INPUT_DIGEST_VERSION,
            }
            previous_item = previous_by_source.get(source_id)
            if (isinstance(previous_item, dict)
                    and previous_item.get("source_input_digest_version") is None):
                legacy_digests = legacy_source_input_digest_candidates(
                    source_id, result, previous_item,
                )
                if legacy_digests:
                    snapshot["legacy_source_input_digest"] = legacy_digests[0]
                    snapshot["legacy_source_input_digests"] = legacy_digests
            handler_finished = perf_counter()
            delivery = result.get("delivery_state")
            if delivery not in ("FRESH", "CACHED", "STALE_CACHE", "NOT_ELIGIBLE"):
                delivery = "UNKNOWN"
            count = result.get("provider_request_count")
            if type(count) is not int or count < 0:
                coverage = result.get("coverage")
                count = (coverage.get("provider_request_count")
                         if isinstance(coverage, dict) else None)
            if type(count) is not int or count < 0:
                count = "UNKNOWN"
            slice_provider_count = result.get("provider_request_count")
            coverage = result.get("coverage")
            if type(slice_provider_count) is not int or slice_provider_count < 0:
                slice_provider_count = (coverage.get("provider_request_count")
                                        if isinstance(coverage, dict) else None)
            confirmed_zero_requests = (
                type(slice_provider_count) is int and slice_provider_count == 0
                and result.get("delivery_state") in {"CACHED", "STALE_CACHE"}
                and self._zero_request_coverage(coverage)
            )
            checkpoint_load_ms = int((checkpoint_finished - checkpoint_started) * 1000)
            handler_ms = int((handler_finished - handler_started) * 1000)
            if continue_cached and confirmed_zero_requests:
                pending_checkpoints.append({
                    "source_id": source_id,
                    "grant": grant,
                    "expected_revision": revision_box[0],
                    "snapshot": snapshot,
                    "result": result,
                    "checkpoint_load_ms": checkpoint_load_ms,
                    "handler_ms": handler_ms,
                })
            else:
                # Preserve each provider-backed receipt immediately. Only
                # confirmed zero-request cache results are safe to batch.
                flush_pending_checkpoints()
                save_started = perf_counter()
                revision = self.journal.save(
                    job_id, owner, fence, source_id=source_id, grant=grant,
                    now=self.utcnow(), expected_revision=revision_box[0], snapshot=snapshot,
                )
                checkpoint_saved = perf_counter()
                _logger.info(
                    "Fetch source timing source=%s status=%s delivery=%s provider_requests=%s "
                    "checkpoint_load_ms=%d handler_ms=%d checkpoint_save_ms=%d",
                    source_id if _SAFE_SOURCE.fullmatch(source_id) else "unknown",
                    result["status"], delivery, count, checkpoint_load_ms, handler_ms,
                    int((checkpoint_saved - save_started) * 1000),
                )
            outcomes[source_id] = result
            # A confirmed cache hit performs no provider request. Continue to
            # the next source in this invocation while retaining the live-source
            # slice bound and all normal checkpoints and quota checks.
            if not (continue_cached and confirmed_zero_requests):
                processed_sources += 1

        if deferred_until is not None:
            flush_pending_checkpoints()
            self.admission.defer(job_id, owner, fence, now=self.utcnow(),
                                 not_before=deferred_until)
            return {"job_id": job_id, "state": "DEFERRED", "sources": outcomes}

        flush_pending_checkpoints()
        partial = any(value["status"] != "SUCCESS" for value in outcomes.values())
        finished = self.admission.finish(
            job_id, owner, fence, now=self.utcnow(), partial=partial,
            require_source_results=True,
        )
        return {"job_id": job_id, "state": "PARTIAL" if partial else "COMPLETED",
                "sources": outcomes,
                "created_at": claim.get("created_at"),
                "finished_at": finished.get("finished_at") if isinstance(finished, dict) else None}

    @staticmethod
    def _source_failure_result(source_id, data_mode, error, *, phase, reason):
        code = getattr(error, "code", None)
        if hasattr(code, "value"):
            code = code.value
        if not isinstance(code, str) or not _SAFE_CODE.fullmatch(code):
            code = ("RESULT_SERIALIZATION_FAILED"
                    if phase == "RESULT_SERIALIZATION" else "SOURCE_EXECUTION_FAILED")
        exception_class = getattr(error, "exception_class", None) or type(error).__name__
        if not _SAFE_EXCEPTION_CLASS.fullmatch(exception_class):
            exception_class = "UnknownError"
        diagnostic = {
            "source_id": source_id,
            "failure_phase": phase,
            "failure_reason": reason,
            "exception_class": exception_class,
            "error_code": code,
        }
        return {
            "status": "FAILED", "data_mode": data_mode,
            "records": [], "record_count": 0, "error_code": code,
            "coverage": {"worker_failure_diagnostic": diagnostic},
        }

    @staticmethod
    def _zero_request_coverage(coverage):
        """Validate numeric counters and the optional boolean verification flag."""
        if coverage is None:
            return True
        if not isinstance(coverage, dict):
            return False
        for coverage_field in ("provider_request_count", "refresh_provider_request_count"):
            if (coverage_field in coverage
                    and (type(coverage[coverage_field]) is not int
                         or coverage[coverage_field] != 0)):
                return False
        verification = coverage.get("serp_verification_attempted")
        return (
            verification is None
            or type(verification) is int and verification == 0
            or type(verification) is bool and verification is False
        )

    @staticmethod
    def _is_cache_only_checkpoint(progress, expected_mode):
        if not isinstance(progress, dict) or len(progress) != 1:
            return False
        receipt = next(iter(progress.values()))
        if not isinstance(receipt, dict) or set(receipt) != {"result"}:
            return False
        result = receipt.get("result")
        if (not isinstance(result, dict)
                or result.get("status") not in {"SUCCESS", "PARTIAL"}
                or result.get("data_mode") != expected_mode
                or result.get("delivery_state") not in _CACHE_DELIVERY_STATES
                or type(result.get("provider_request_count")) is not int
                or result["provider_request_count"] != 0
                or not isinstance(result.get("records"), list)
                or type(result.get("record_count")) is not int
                or result["record_count"] != len(result["records"])):
            return False
        coverage = result.get("coverage", {})
        if not isinstance(coverage, dict):
            return False
        return DistributedFetchWorker._zero_request_coverage(coverage)

    @staticmethod
    def _validate_result(value, *, expected_mode, expected_source_id=None):
        if not isinstance(value, dict) or value.get("status") not in _TERMINAL_STATUSES | {"DEFERRED"}:
            raise ValueError("source handler returned an invalid status")
        if value["status"] == "DEFERRED":
            if set(value) != {"status", "retry_at"}:
                raise ValueError("invalid deferred source result")
            return value
        allowed = {"status", "data_mode", "delivery_state", "records", "record_count",
                   "error_code", "retry_after_seconds", "coverage", "quality_flags",
                   "transport_diagnostic", "provider_request_count"}
        if set(value) - allowed:
            raise ValueError("source result contains an unsupported field")
        value = dict(value)
        if value["status"] in {"FAILED", "UNAVAILABLE"}:
            # Delivery freshness describes returned observations. A failed or
            # unavailable source has no fresh delivery, even if an adapter's
            # generic default filled that field in.
            if not (value["status"] == "UNAVAILABLE"
                    and value.get("delivery_state") == "NOT_ELIGIBLE"):
                value["delivery_state"] = None
        elif value.get("delivery_state") is None:
            value["delivery_state"] = "FRESH"
        if value.get("data_mode") != expected_mode:
            raise ValueError("source result provenance mismatch")
        if (value.get("delivery_state") not in {
                    "FRESH", "CACHED", "STALE_CACHE", "NOT_ELIGIBLE", None,
                }
                or value["status"] not in {"FAILED", "UNAVAILABLE"}
                and value.get("delivery_state") is None):
            raise ValueError("invalid result delivery state")
        if "provider_request_count" in value and (
                type(value["provider_request_count"]) is not int
                or value["provider_request_count"] < 0):
            raise ValueError("invalid provider request count")
        records = value.get("records")
        if not isinstance(records, list) or len(records) > 5000 or any(
            not isinstance(record, dict) for record in records
        ):
            raise ValueError("source records must be a bounded normalized list")
        if type(value.get("record_count")) is not int or value["record_count"] != len(records):
            raise ValueError("invalid source record count")
        if "error_code" in value and (not isinstance(value["error_code"], str)
                                       or not _SAFE_CODE.fullmatch(value["error_code"])):
            raise ValueError("invalid source error code")
        if "retry_after_seconds" in value and (
            type(value["retry_after_seconds"]) is not int
            or not 1 <= value["retry_after_seconds"] <= 31_536_000
        ):
            raise ValueError("invalid retry interval")
        if "quality_flags" in value and (
            not isinstance(value["quality_flags"], list)
            or any(not isinstance(flag, str) or len(flag) > 64 for flag in value["quality_flags"])
        ):
            raise ValueError("invalid quality flags")
        if "coverage" in value and not isinstance(value["coverage"], dict):
            raise ValueError("coverage must be a bounded object")
        coverage = value.get("coverage", {})
        worker_diagnostic = coverage.get("worker_failure_diagnostic")
        if worker_diagnostic is not None:
            expected_fields = {
                "source_id", "failure_phase", "failure_reason", "exception_class", "error_code",
            }
            if (not isinstance(worker_diagnostic, dict)
                    or set(worker_diagnostic) != expected_fields
                    or worker_diagnostic.get("source_id") != expected_source_id
                    or worker_diagnostic.get("failure_phase") not in _SOURCE_FAILURE_PHASES
                    or worker_diagnostic.get("failure_reason") not in _SOURCE_FAILURE_REASONS
                    or not isinstance(worker_diagnostic.get("exception_class"), str)
                    or not _SAFE_EXCEPTION_CLASS.fullmatch(worker_diagnostic["exception_class"])
                    or worker_diagnostic.get("error_code") != value.get("error_code")
                    or not isinstance(worker_diagnostic.get("error_code"), str)
                    or not _SAFE_CODE.fullmatch(worker_diagnostic["error_code"])):
                raise ValueError("invalid worker failure diagnostic")
        persistence_diagnostic = coverage.get("persistence_diagnostic")
        if persistence_diagnostic is not None:
            expected_fields = {
                "source_id", "failure_phase", "failure_reason", "exception_class",
                "attempt_id", "error_code",
            }
            if (not isinstance(persistence_diagnostic, dict)
                    or set(persistence_diagnostic) != expected_fields
                    or persistence_diagnostic.get("source_id") != expected_source_id
                    or persistence_diagnostic.get("failure_phase") != "RESULT_PERSISTENCE"
                    or persistence_diagnostic.get("failure_reason") is not None
                    and persistence_diagnostic.get("failure_reason") not in _PERSISTENCE_FAILURE_REASONS
                    or persistence_diagnostic.get("exception_class") is not None
                    and (not isinstance(persistence_diagnostic.get("exception_class"), str)
                         or not _SAFE_EXCEPTION_CLASS.fullmatch(
                             persistence_diagnostic["exception_class"]))
                    or not isinstance(persistence_diagnostic.get("attempt_id"), str)
                    or not _SAFE_ATTEMPT_ID.fullmatch(persistence_diagnostic["attempt_id"])
                    or persistence_diagnostic.get("error_code") != value.get("error_code")
                    or not isinstance(persistence_diagnostic.get("error_code"), str)
                    or not _SAFE_CODE.fullmatch(persistence_diagnostic["error_code"])):
                raise ValueError("invalid persistence diagnostic")
        if "transport_diagnostic" in value:
            diagnostic = value["transport_diagnostic"]
            expected_diagnostic_fields = {
                "source_id", "failure_phase", "http_status", "exception_class",
                "bytes_received", "response_size_limit", "attempt_id", "error_code",
            }
            if not isinstance(diagnostic, dict) or set(diagnostic) != expected_diagnostic_fields:
                raise ValueError("invalid transport diagnostic shape")
            if (diagnostic["source_id"] != WIKIMEDIA_SOURCE_ID
                    or not isinstance(diagnostic["failure_phase"], str)
                    or diagnostic["failure_phase"] not in _TRANSPORT_FAILURE_PHASES
                    or (diagnostic["http_status"] is not None
                        and (type(diagnostic["http_status"]) is not int
                             or not 100 <= diagnostic["http_status"] <= 599))
                    or (diagnostic["exception_class"] is not None
                        and (not isinstance(diagnostic["exception_class"], str)
                             or diagnostic["exception_class"] not in _TRANSPORT_EXCEPTION_CLASSES))
                    or type(diagnostic["bytes_received"]) is not int
                    or not 0 <= diagnostic["bytes_received"] <= 1_000_000_000
                    or type(diagnostic["response_size_limit"]) is not int
                    or not 1 <= diagnostic["response_size_limit"] <= 10_000_000
                    or not isinstance(diagnostic["attempt_id"], str)
                    or not _SAFE_ATTEMPT_ID.fullmatch(diagnostic["attempt_id"])
                    or not isinstance(diagnostic["error_code"], str)
                    or not _SAFE_CODE.fullmatch(diagnostic["error_code"])
                    or diagnostic["error_code"] != value.get("error_code")):
                raise ValueError("invalid transport diagnostic values")
        try:
            serialized = json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise _SourceResultSerializationError(type(exc).__name__) from None
        if len(serialized) > _MAX_SOURCE_RESULT_BYTES:
            raise _SourceResultSerializationError("ValueError")
        return value
