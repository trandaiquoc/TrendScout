"""No real network: prove quota reservation precedes creation of each request."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_d5_public_admission import NOW, coordinator

from app.sources.catalog import GOOGLE_SOURCE_ID
from app.storage.firestore_atomic_state import CommitOutcomeUnknown
from app.workers.guarded_attempt import GuardedAttemptRunner


class GuardedAttemptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = str(Path(self.directory.name) / "ledger.sqlite")
        self.control = coordinator(path)
        self.job = self.control.admit("one-request-idempotency", now=NOW)["job_id"]
        self.fence = self.control.claim(self.job, "worker", now=NOW)["fence"]
        self.runner = GuardedAttemptRunner(self.control, clock=lambda: NOW)
        self.calls = 0

    async def asyncTearDown(self):
        self.directory.cleanup()

    def args(self):
        return {"job_id": self.job, "owner": "worker", "fence": self.fence,
                "source_id": GOOGLE_SOURCE_ID, "attempt_id": "one-physical-attempt",
                "timeout_seconds": 1, "request": self.request}

    async def request(self):
        self.calls += 1
        return {"synthetic": True}

    async def test_success_and_duplicate_do_not_issue_duplicate_request(self):
        outcome = await self.runner.run(**self.args())
        self.assertEqual(outcome.status, "COMPLETED")
        self.assertEqual(outcome.value, {"synthetic": True})
        self.assertNotIn("synthetic", repr(outcome))
        self.assertEqual((await self.runner.run(**self.args())).status, "ALREADY_RESERVED")
        self.assertEqual(self.calls, 1)

    async def test_unknown_commit_never_creates_request(self):
        underlying = self.control.store
        class AmbiguousStore:
            fail = True
            def transact(self, key, operation):
                result = underlying.transact(key, operation)
                if self.fail:
                    self.fail = False
                    raise CommitOutcomeUnknown("synthetic ambiguous commit")
                return result
        self.control.store = AmbiguousStore()
        with self.assertRaises(CommitOutcomeUnknown):
            await self.runner.run(**self.args())
        self.assertEqual(self.calls, 0)
        self.assertEqual((await self.runner.run(**self.args())).status, "ALREADY_RESERVED")
        self.assertEqual(self.calls, 0)

    async def test_revocation_before_request_is_fail_closed(self):
        original = self.control.check_attempt
        def disable_before_check(*args, **kwargs):
            self.control.disable()
            return original(*args, **kwargs)
        with patch.object(self.control, "check_attempt", disable_before_check):
            outcome = await self.runner.run(**self.args())
        self.assertEqual(outcome.status, "ADMISSION_REVOKED")
        self.assertEqual(self.calls, 0)

    async def test_timeout_and_provider_error_do_not_leak_or_release_early(self):
        async def slow():
            await asyncio.sleep(1)
        outcome = await self.runner.run(**{**self.args(), "request": slow, "timeout_seconds": 0.01})
        self.assertEqual(outcome.status, "UPSTREAM_TIMEOUT")
        self.assertTrue(outcome.settlement_pending)

        async def fail():
            raise RuntimeError("sensitive query and fake credential")
        outcome = await self.runner.run(**{**self.args(), "request": fail,
                                           "attempt_id": "another-physical-attempt"})
        self.assertEqual(outcome.status, "UPSTREAM_ERROR")
        self.assertNotIn("sensitive", repr(outcome))
        self.assertTrue(outcome.settlement_pending)
