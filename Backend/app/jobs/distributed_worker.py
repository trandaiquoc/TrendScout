"""Fenced worker loop for source-scoped jobs stored by ``PublicAdmission``.

The worker is deliberately not started by the web application. A deployment must
compose it with an approved durable ``AtomicStateStore`` and a bounded source
handler registry. It is not yet wired to HTTP Fetch or a production job runner.
Offline tests use a multi-process SQLite harness only.
"""

import inspect
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Mapping

from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.jobs.source_checkpoints import CheckpointGrant, SourceCheckpointJournal

_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_TERMINAL_STATUSES = frozenset({"SUCCESS", "PARTIAL", "FAILED", "UNAVAILABLE"})


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
                 resolve_grant: GrantResolver,
                 utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if not isinstance(admission, PublicAdmission):
            raise TypeError("PublicAdmission required")
        if (set(handlers) != set(admission.policy.enabled_sources)
                or any(not callable(handler) for handler in handlers.values())
                or not callable(resolve_grant) or not callable(utcnow)):
            raise ValueError("complete server-owned handler and grant registries required")
        self.admission = admission
        self.handlers = dict(handlers)
        self.source_modes = {
            source_id: getattr(handler, "__trendscout_data_mode__", None)
            for source_id, handler in self.handlers.items()
        }
        if any(mode is not None and mode not in {"SYNTHETIC", "LIVE"}
               for mode in self.source_modes.values()):
            raise ValueError("invalid server-owned source mode declaration")
        self.resolve_grant = resolve_grant
        self.utcnow = utcnow
        self.journal = SourceCheckpointJournal(admission)

    async def run_once(self, owner: str) -> dict | None:
        now = self.utcnow()
        claim = self.admission.claim_next(owner, now=now)
        if claim is None:
            return None
        job_id, fence = claim["job_id"], claim["fence"]
        job_expires_at = datetime.fromisoformat(claim["expires_at"])
        outcomes = {}
        deferred_until = None
        for source_id in sorted(self.handlers):
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
            saved = self.journal.load(job_id, owner, fence, source_id=source_id,
                                      grant=grant, now=now)
            if saved and saved["snapshot"].get("phase") == "COMPLETE":
                outcomes[source_id] = saved["snapshot"]["result"]
                continue
            revision = saved["revision"] if saved else 0
            resume = saved["snapshot"] if saved else None
            revision_box = [revision]

            def checkpoint(progress):
                if not isinstance(progress, dict):
                    raise ValueError("source checkpoint progress must be an object")
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
                                        checkpoint, heartbeat)
            try:
                result = self.handlers[source_id](context)
                if inspect.isawaitable(result):
                    result = await result
                result = self._validate_result(result, expected_mode=grant.data_mode)
            except AdmissionDenied:
                raise
            except Exception as exc:
                code = getattr(exc, "code", "SOURCE_EXECUTION_FAILED")
                if not isinstance(code, str) or not _SAFE_CODE.fullmatch(code):
                    code = "SOURCE_EXECUTION_FAILED"
                result = {"status": "FAILED", "data_mode": grant.data_mode,
                          "records": [], "record_count": 0, "error_code": code}

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
            }
            self.journal.save(job_id, owner, fence, source_id=source_id, grant=grant,
                              now=self.utcnow(), expected_revision=revision_box[0],
                              snapshot=snapshot)
            outcomes[source_id] = result

        if deferred_until is not None:
            self.admission.defer(job_id, owner, fence, now=self.utcnow(),
                                 not_before=deferred_until)
            return {"job_id": job_id, "state": "DEFERRED", "sources": outcomes}

        partial = any(value["status"] != "SUCCESS" for value in outcomes.values())
        self.admission.finish(job_id, owner, fence, now=self.utcnow(), partial=partial)
        return {"job_id": job_id, "state": "PARTIAL" if partial else "COMPLETED",
                "sources": outcomes}

    @staticmethod
    def _validate_result(value, *, expected_mode):
        if not isinstance(value, dict) or value.get("status") not in _TERMINAL_STATUSES | {"DEFERRED"}:
            raise ValueError("source handler returned an invalid status")
        if value["status"] == "DEFERRED":
            if set(value) != {"status", "retry_at"}:
                raise ValueError("invalid deferred source result")
            return value
        allowed = {"status", "data_mode", "records", "record_count", "error_code",
                   "coverage", "quality_flags"}
        if set(value) - allowed:
            raise ValueError("source result contains an unsupported field")
        if value.get("data_mode") != expected_mode:
            raise ValueError("source result provenance mismatch")
        records = value.get("records")
        if not isinstance(records, list) or len(records) > 1000 or any(
            not isinstance(record, dict) for record in records
        ):
            raise ValueError("source records must be a bounded normalized list")
        if type(value.get("record_count")) is not int or value["record_count"] != len(records):
            raise ValueError("invalid source record count")
        if "error_code" in value and (not isinstance(value["error_code"], str)
                                       or not _SAFE_CODE.fullmatch(value["error_code"])):
            raise ValueError("invalid source error code")
        if "quality_flags" in value and (
            not isinstance(value["quality_flags"], list)
            or any(not isinstance(flag, str) or len(flag) > 64 for flag in value["quality_flags"])
        ):
            raise ValueError("invalid quality flags")
        if "coverage" in value and not isinstance(value["coverage"], dict):
            raise ValueError("coverage must be a bounded object")
        return value
