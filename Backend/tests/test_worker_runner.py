"""The queue loop drains work, honors shutdown and exposes fatal worker errors."""

import asyncio
import unittest

from app.jobs.worker_runner import DistributedWorkerRunner


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_drains_jobs_then_returns_after_shutdown(self):
        stop = asyncio.Event()

        class Worker:
            calls = 0
            owners = []

            async def run_once(self, owner):
                self.calls += 1
                self.owners.append(owner)
                if self.calls <= 2:
                    return {"state": "COMPLETED"}
                stop.set()
                return None

        worker = Worker()
        runner = DistributedWorkerRunner(
            worker, owner="demo-worker-1", idle_poll_seconds=0.05
        )
        count = await runner.run(stop)

        self.assertEqual(count, 2)
        self.assertEqual(worker.calls, 3)
        self.assertEqual(set(worker.owners), {"demo-worker-1"})

    async def test_idle_loop_wakes_immediately_for_shutdown(self):
        stop = asyncio.Event()

        class Worker:
            calls = 0

            async def run_once(self, _owner):
                self.calls += 1
                stop.set()
                return None

        worker = Worker()
        count = await DistributedWorkerRunner(
            worker, owner="worker-a", idle_poll_seconds=60
        ).run(stop)

        self.assertEqual(count, 0)
        self.assertEqual(worker.calls, 1)

    async def test_invalid_outcome_and_worker_failure_are_not_hidden(self):
        class InvalidWorker:
            async def run_once(self, _owner):
                return "not-an-object"

        class FailingWorker:
            async def run_once(self, _owner):
                raise RuntimeError("storage unavailable")

        with self.assertRaisesRegex(TypeError, "outcome"):
            await DistributedWorkerRunner(
                InvalidWorker(), owner="worker-a"
            ).run(asyncio.Event())
        with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
            await DistributedWorkerRunner(
                FailingWorker(), owner="worker-a"
            ).run(asyncio.Event())

    def test_owner_and_poll_interval_are_bounded(self):
        class Worker:
            async def run_once(self, _owner):
                return None

        for owner in ("", "bad owner", "x" * 129):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                DistributedWorkerRunner(Worker(), owner=owner)
        for interval in (True, 0, 60.1):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                DistributedWorkerRunner(Worker(), owner="worker-a",
                                        idle_poll_seconds=interval)


if __name__ == "__main__":
    unittest.main()
