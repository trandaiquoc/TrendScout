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
from app.storage.local_volatile_checkpoints import LocalVolatileCheckpoints
from local_security_helpers import private_test_directory


class LiveFacadeContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_contract_requires_local_boundary_and_hides_raw_records(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            private = private_test_directory(directory)
            sqlite = LocalSQLiteAtomicStateStore.for_local_live(private / "state.sqlite")
            volatile = LocalVolatileCheckpoints(sqlite)
            store = LocalSmokeStore(volatile)
            admission = PublicAdmission(store, replace(local_smoke_policy(), enabled=True), state_key="demo")
            def grant(source, at):
                return CheckpointGrant(source, "LIVE", "test-contract-only", at + timedelta(minutes=5), 120, True)
            def handler(_context):
                return {"status": "SUCCESS", "data_mode": "LIVE", "record_count": 1,
                        "records": [{"raw": "fixture-private-record"}]}
            handler.__trendscout_data_mode__ = "LIVE"
            worker = DistributedFetchWorker(admission, handlers={source: handler for source in
                admission.policy.enabled_sources}, resolve_grant=grant, utcnow=lambda: now)
            raw_admission = PublicAdmission(LocalSmokeStore(sqlite),
                replace(local_smoke_policy(), enabled=True), state_key="raw-demo")
            raw_worker = DistributedFetchWorker(raw_admission, handlers={source: handler for source in
                raw_admission.policy.enabled_sources}, resolve_grant=grant, utcnow=lambda: now)
            with self.assertRaises(ValueError):
                DistributedPublicFetch(raw_admission, raw_worker, resolve_grant=grant, utcnow=lambda: now,
                    local_live=True, resolve_processing_grants=lambda _at: ())
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
            for path in private.iterdir():
                if path.is_file() and not path.name.endswith(".process-lock"):
                    self.assertNotIn(b"fixture-private-record", path.read_bytes())
            # Restart/expiry may remove all volatile results while the quota job remains.
            # A permission grant alone is not evidence of a live source result.
            volatile._values.clear()
            missing = service.status(job)
            self.assertIsNone(missing["data_mode"])
            self.assertTrue(all(row["status"] == "UNAVAILABLE" and "data_mode" not in row
                                for row in missing["sources"]))
            volatile.close()


if __name__ == "__main__":
    unittest.main()
