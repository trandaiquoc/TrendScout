"""Simulated LIVE contract test; no provider calls or live evidence."""

import tempfile
import unittest
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import PublicAdmission
from app.jobs.source_checkpoints import CheckpointGrant
from app.local_smoke_policy import local_smoke_policy
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.storage.local_smoke_store import LocalSmokeStore
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore


class LiveFacadeContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_contract_requires_local_boundary_and_hides_raw_records(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            store = LocalSmokeStore(LocalSQLiteAtomicStateStore.for_local_live(Path(directory) / "state.sqlite"))
            admission = PublicAdmission(store, replace(local_smoke_policy(), enabled=True), state_key="demo")
            def grant(source, at):
                return CheckpointGrant(source, "LIVE", "test-contract-only", at + timedelta(minutes=5), 120, True)
            def handler(_context):
                return {"status": "SUCCESS", "data_mode": "LIVE", "record_count": 1,
                        "records": [{"raw": "fixture-private-record"}]}
            handler.__trendscout_data_mode__ = "LIVE"
            worker = DistributedFetchWorker(admission, handlers={source: handler for source in
                admission.policy.enabled_sources}, resolve_grant=grant, utcnow=lambda: now)
            with self.assertRaises(ValueError):
                DistributedPublicFetch(admission, worker, resolve_grant=grant, utcnow=lambda: now)
            with patch.dict(os.environ, {"VERCEL": "1"}), self.assertRaises(ValueError):
                DistributedPublicFetch(admission, worker, resolve_grant=grant, utcnow=lambda: now,
                    local_live=True, resolve_processing_grants=lambda _at: ())
            service = DistributedPublicFetch(admission, worker, resolve_grant=grant, utcnow=lambda: now,
                local_live=True, resolve_processing_grants=lambda _at: ())
            job = service.submit("demo-contract-idempotency-key")["job_id"]
            await service.run_once("test-worker")
            result = service.status(job)
            self.assertEqual(result["data_mode"], "LIVE")
            self.assertEqual(result["discovery"]["data_mode"], "LIVE")
            self.assertEqual(result["discovery"]["candidate_count"], 0)
            self.assertNotIn("fixture-private-record", repr(result))
            self.assertTrue(all("records" not in source for source in result["sources"]))


if __name__ == "__main__":
    unittest.main()
