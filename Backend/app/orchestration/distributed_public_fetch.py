"""Public Fetch facade over durable admission and retained source results.

The facade has no provider configuration surface. Its admission and result reads
are usable only when explicitly injected by backend composition.
"""

from datetime import datetime, timezone

from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.jobs.source_checkpoints import CheckpointGrant, SourceCheckpointJournal


class DistributedPublicFetch:
    def __init__(self, admission: PublicAdmission, worker: DistributedFetchWorker, *,
                 resolve_grant, utcnow=lambda: datetime.now(timezone.utc)):
        if (not isinstance(admission, PublicAdmission)
                or not isinstance(worker, DistributedFetchWorker)
                or worker.admission.policy_digest != admission.policy_digest
                or not callable(resolve_grant) or not callable(utcnow)):
            raise ValueError("matching admission, worker and grant resolver required")
        self.admission = admission
        self.worker = worker
        self.resolve_grant = resolve_grant
        self.utcnow = utcnow
        self.journal = SourceCheckpointJournal(admission)
        declared_modes = set(worker.source_modes.values())
        if declared_modes != {"SYNTHETIC"}:
            raise ValueError("public demo Fetch requires explicitly synthetic source handlers")
        self.data_mode = "SYNTHETIC"

    def submit(self, idempotency_key: str) -> dict:
        accepted = self.admission.admit(idempotency_key, now=self.utcnow())
        return {"job_id": accepted["job_id"], "result": accepted["result"],
                "status": "QUEUED", "created_at": accepted.get("created_at")}

    def status(self, job_id: str) -> dict | None:
        now = self.utcnow()
        job = self.admission.read_job(job_id, now=now)
        if job is None:
            return None
        source_results = []
        modes = set()
        terminal = job["state"] in {"COMPLETED", "PARTIAL"}
        if terminal:
            for source_id in sorted(self.admission.policy.enabled_sources):
                try:
                    grant = self.resolve_grant(source_id, now)
                except Exception:
                    grant = None
                if not isinstance(grant, CheckpointGrant) or grant.source_id != source_id:
                    source_results.append({"source_id": source_id, "status": "UNAVAILABLE",
                                          "error_code": "RETENTION_POLICY_UNAVAILABLE"})
                    continue
                modes.add(grant.data_mode)
                try:
                    result = self.journal.completed_result(
                        job, source_id=source_id, grant=grant, now=now
                    )
                except AdmissionDenied:
                    result = None
                if result is None:
                    source_results.append({"source_id": source_id, "status": "UNAVAILABLE",
                                          "data_mode": grant.data_mode,
                                          "error_code": "RESULT_UNAVAILABLE"})
                    continue
                source_results.append({
                    "source_id": source_id,
                    "status": result["status"],
                    "data_mode": result["data_mode"],
                    "delivery_state": result.get("delivery_state", "FRESH"),
                    "record_count": result["record_count"],
                    "records": result["records"],
                    "coverage": result.get("coverage"),
                    "error_code": result.get("error_code"),
                    "quality_flags": result.get("quality_flags", []),
                })
        return {
            "fetch_id": job["job_id"],
            "status": job["state"],
            "stage": job["state"],
            "data_mode": next(iter(modes)) if len(modes) == 1 else "MIXED" if modes else None,
            "sources": source_results if terminal else [
                {"source_id": source_id, "status": job["state"]}
                for source_id in sorted(self.admission.policy.enabled_sources)
            ],
            "created_at": job["created_at"],
            "updated_at": job.get("updated_at") or job.get("available_at"),
            "finished_at": job.get("finished_at"),
            "terminal": terminal or job["state"] in {"FAILED", "EXPIRED"},
        }

    async def run_once(self, owner: str):
        return await self.worker.run_once(owner)
