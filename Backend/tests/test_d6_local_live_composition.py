"""Composition checks only: no live provider calls or real credentials."""

import os
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from app import local_ledger
from test_d5_live_gateway import Approvals
from test_d5_public_admission import NOW
from test_d5_wikimedia_gateway import USER_AGENT

from app.jobs.source_checkpoints import CheckpointGrant
from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.local_smoke_policy import local_smoke_policy
from app.orchestration.local_live_composition import local_live_composition, _bounded_checkpoint_resolver
from app.sources.live_gateway import GatewayError
from app.sources.catalog import GOOGLE_SOURCE_ID
from app.storage.local_smoke_store import LocalSmokeStore
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore


class FixtureApprovals:
    def __init__(self, denied=None):
        self.denied = denied

    def current(self, source):
        if source == self.denied:
            return None
        return replace(Approvals(source).value, scope="LOCAL_SMOKE", local_controls_verified=True)


class NoCredentials:
    def resolve(self, _reference):
        raise AssertionError("composition must not read credentials")


def grant(source, now):
    return CheckpointGrant(source, "LIVE", "fixture-policy", now + timedelta(minutes=1), 30, True)


class LocalLiveCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_cap_does_not_extend_or_grant_permission(self):
        source = GOOGLE_SOURCE_ID
        long_grant = replace(grant(source, NOW), max_retention_seconds=3600)
        resolve = _bounded_checkpoint_resolver(lambda _source, _now: long_grant)
        self.assertEqual(resolve(source, NOW).max_retention_seconds, 120)
        self.assertEqual(resolve(source, NOW).expires_at, long_grant.expires_at)
        self.assertIsNone(resolve("another-source", NOW))
        resolve = _bounded_checkpoint_resolver(lambda _source, _now: replace(long_grant, allowed=False))
        self.assertIsNone(resolve(source, NOW))
        resolve = _bounded_checkpoint_resolver(lambda _source, _now: grant(source, NOW))
        self.assertEqual(resolve(source, NOW).max_retention_seconds, 30)

    async def test_composition_sends_no_requests_and_releases_its_lock(self):
        async def forbidden_request(*_args, **_kwargs):
            raise AssertionError("provider request is forbidden in this test")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(local_ledger, "DIRECTORY", Path(directory) / "smoke"), \
             patch.object(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_request):
            args = dict(credentials=NoCredentials(),
                approvals=FixtureApprovals(), user_agent=USER_AGENT, resolve_checkpoint_grant=grant,
                resolve_processing_grants=lambda _now: (), utcnow=lambda: NOW)
            async with local_live_composition(**args) as facade:
                self.assertEqual(facade.data_mode, "LIVE")
                self.assertEqual(facade.admission.policy.max_starts_per_window, 1)
                self.assertFalse(any(facade.resolve_processing_grants(NOW)))
                job = facade.submit("shared-ledger-idempotency-001")["job_id"]
                cli_store = LocalSmokeStore(LocalSQLiteAtomicStateStore.for_local_live(
                    local_ledger.DIRECTORY / "ledger.sqlite"))
                cli = PublicAdmission(cli_store, replace(local_smoke_policy(), enabled=True),
                                      state_key=local_ledger.STATE_KEY)
                self.assertEqual(cli.admit("shared-ledger-idempotency-001", now=NOW)["job_id"], job)
                fence = facade.admission.claim(job, "fixture", now=NOW)["fence"]
                facade.admission.reserve_attempt(job, "fixture", fence, source_id=GOOGLE_SOURCE_ID,
                    attempt_id="fixture-first-attempt", now=NOW)
                facade.admission.settle_attempt(job, "fixture", fence,
                    attempt_id="fixture-first-attempt", now=NOW)
                with self.assertRaises(AdmissionDenied):
                    cli.reserve_attempt(job, "fixture", fence, source_id=GOOGLE_SOURCE_ID,
                        attempt_id="fixture-second-attempt", now=NOW)
            async with local_live_composition(**args):
                pass

    async def test_missing_approval_and_vercel_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(local_ledger, "DIRECTORY", Path(directory) / "smoke"):
            args = dict(credentials=NoCredentials(),
                approvals=FixtureApprovals(denied="bing_web_serp"), user_agent=USER_AGENT,
                resolve_checkpoint_grant=grant, resolve_processing_grants=lambda _now: (), utcnow=lambda: NOW)
            with self.assertRaises(GatewayError):
                async with local_live_composition(**args):
                    self.fail("missing source approval must reject composition")
            with patch.dict(os.environ, {"VERCEL": "1"}), self.assertRaises(ValueError):
                async with local_live_composition(**args):
                    self.fail("Vercel must remain disabled")


if __name__ == "__main__":
    unittest.main()
