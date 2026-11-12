"""Offline worker integration over the atomic admission and checkpoint ports."""

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from test_d5_public_admission import NOW, coordinator, policy

from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import PoolLimit
from app.jobs.source_checkpoints import CheckpointGrant
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)

SOURCES = (WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID)


class SimulatedProcessCrash(BaseException):
    pass


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


class DistributedWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "worker.sqlite")
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.clock = MutableClock()
        self.config = policy(
            enabled_sources=frozenset(SOURCES),
            pools={"wikimedia": PoolLimit(2, 5, 2, 5, 1),
                   "serpapi": PoolLimit(2, 5, 2, 5, 1),
                   "youtube": PoolLimit(1, 3, 1, 3, 1)},
            lease_seconds=10, cooldown_seconds=0, plan_fingerprint="a" * 64,
        )
        self.admission = coordinator(self.path, self.config)

    async def asyncTearDown(self):
        self.directory.cleanup()

    def grant(self, source_id, now):
        return CheckpointGrant(source_id, "SYNTHETIC", "fixture-policy-v1",
                               now + timedelta(minutes=10), 300, True)

    def worker(self, handlers):
        return DistributedFetchWorker(
            coordinator(self.path, self.config), handlers=handlers,
            resolve_grant=self.grant, utcnow=self.clock,
        )

    def admit(self):
        return self.admission.admit("distributed-worker-idempotency-key", now=self.clock())["job_id"]

    @staticmethod
    def source_result(count=0, *, status="SUCCESS"):
        return {"status": status, "data_mode": "SYNTHETIC",
                "records": [{"evidence_id": f"fixture-{index}"} for index in range(count)],
                "record_count": count}

    async def test_sources_run_under_fenced_queue_and_finish_with_source_status(self):
        self.admit()
        seen = []

        async def handler(context):
            seen.append((context.source_id, context.fence))
            context.checkpoint({"cursor": 1})
            return self.source_result(2)

        worker = self.worker({source: handler for source in SOURCES})
        outcome = await worker.run_once("worker-one")
        self.assertEqual(outcome["state"], "COMPLETED")
        self.assertEqual({source for source, _ in seen}, set(SOURCES))
        self.assertEqual(len({fence for _, fence in seen}), 1)
        self.assertIsNone(await worker.run_once("worker-two"))

    async def test_reclaim_resumes_progress_and_skips_completed_source(self):
        job_id = self.admit()
        calls = {source: 0 for source in SOURCES}

        async def complete(context):
            calls[context.source_id] += 1
            return self.source_result(1)

        async def crash_during_google(context):
            calls[GOOGLE_SOURCE_ID] += 1
            context.checkpoint({"cursor": 5, "accepted": 2})
            raise SimulatedProcessCrash()

        first = self.worker({WIKIMEDIA_SOURCE_ID: complete,
                             BING_SOURCE_ID: complete,
                             GOOGLE_SOURCE_ID: crash_during_google,
                             YOUTUBE_SOURCE_ID: complete})
        with self.assertRaises(SimulatedProcessCrash):
            await first.run_once("worker-old")

        self.clock.value += timedelta(seconds=11)
        resumed_snapshot = []

        async def resume_google(context):
            calls[GOOGLE_SOURCE_ID] += 1
            resumed_snapshot.append(context.resume_snapshot["progress"])
            return self.source_result(3)

        second = self.worker({WIKIMEDIA_SOURCE_ID: complete,
                              GOOGLE_SOURCE_ID: resume_google,
                              BING_SOURCE_ID: complete,
                              YOUTUBE_SOURCE_ID: complete})
        result = await second.run_once("worker-new")
        self.assertEqual(result["state"], "COMPLETED")
        self.assertEqual(calls[GOOGLE_SOURCE_ID], 2)
        self.assertEqual(calls[BING_SOURCE_ID], 1)
        self.assertEqual(calls[YOUTUBE_SOURCE_ID], 1)
        self.assertEqual(resumed_snapshot, [{"cursor": 5, "accepted": 2}])
        self.assertGreater(result["sources"][GOOGLE_SOURCE_ID]["record_count"], 0)
        self.assertEqual(job_id, result["job_id"])

    async def test_deferred_source_releases_lease_without_shortening_retry(self):
        self.admit()
        calls = []
        retry_at = NOW + timedelta(seconds=40)

        async def deferred(context):
            calls.append(context.source_id)
            if context.source_id == GOOGLE_SOURCE_ID and calls.count(GOOGLE_SOURCE_ID) == 1:
                return {"status": "DEFERRED", "retry_at": retry_at}
            return self.source_result()

        worker = self.worker({source: deferred for source in SOURCES})
        result = await worker.run_once("worker-one")
        self.assertEqual(result["state"], "DEFERRED")
        self.assertEqual(calls, [BING_SOURCE_ID, GOOGLE_SOURCE_ID])
        self.clock.value = retry_at - timedelta(seconds=1)
        self.assertIsNone(await worker.run_once("worker-two"))
        self.clock.value = retry_at
        result = await worker.run_once("worker-two")
        self.assertEqual(result["state"], "COMPLETED")

    async def test_concurrent_worker_delivery_cannot_claim_same_live_lease(self):
        self.admit()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking(context):
            if context.source_id == GOOGLE_SOURCE_ID:
                entered.set()
                await release.wait()
            return self.source_result()

        worker = self.worker({source: blocking for source in SOURCES})
        first_task = asyncio.create_task(worker.run_once("worker-one"))
        await entered.wait()
        self.assertIsNone(await worker.run_once("worker-two"))
        release.set()
        self.assertEqual((await first_task)["state"], "COMPLETED")

    async def test_invalid_or_secret_bearing_results_fail_as_source_errors(self):
        self.admit()

        async def leaks(context):
            return {"status": "SUCCESS", "api_key": "do-not-store"}

        worker = self.worker({source: leaks for source in SOURCES})
        result = await worker.run_once("worker-one")
        self.assertEqual(result["state"], "PARTIAL")
        expected = {"status": "FAILED", "data_mode": "SYNTHETIC", "records": [],
                    "record_count": 0, "error_code": "SOURCE_EXECUTION_FAILED"}
        self.assertTrue(all(item == expected
                            for item in result["sources"].values()))


if __name__ == "__main__":
    unittest.main()
