"""Four fixture gateways through bounded local admission and volatile checkpoints."""

import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
from test_d5_live_gateway import Approvals, Credentials
from test_d5_public_admission import NOW
from test_d5_wikimedia_gateway import USER_AGENT, response as wikimedia_response

from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import PublicAdmission
from app.jobs.source_checkpoints import CheckpointGrant
from app.local_smoke_policy import local_smoke_policy
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.sources.guarded_collector import CollectionLimits, GuardedQueryCollector
from app.sources.live_gateway import LivePageGateway
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.wikimedia.live_gateway import WikimediaTopPageGateway
from app.storage.local_smoke_store import LocalSmokeStore
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.storage.local_volatile_checkpoints import LocalVolatileCheckpoints
from app.workers.d5_source_handlers import D5SourceHandlers
from app.workers.guarded_attempt import GuardedAttemptRunner


class OfflineWorkerCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_sources_share_bounded_ledger_without_persisting_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite"
            backing = LocalSQLiteAtomicStateStore.for_local_live(path)
            volatile = LocalVolatileCheckpoints(backing, clock=lambda: NOW)
            self.addCleanup(volatile.close)
            admission = PublicAdmission(LocalSmokeStore(volatile),
                replace(local_smoke_policy(), enabled=True), state_key="fixture")
            runner = GuardedAttemptRunner(admission, clock=lambda: NOW)
            counts = {source: 0 for source in admission.policy.enabled_sources}
            gateways = []

            def respond(source):
                def fixture(_request):
                    counts[source] += 1
                    if source == WIKIMEDIA_SOURCE_ID:
                        return httpx.Response(200, json=wikimedia_response(NOW.date()))
                    if source == YOUTUBE_SOURCE_ID:
                        return httpx.Response(200, json={"kind": "youtube#searchListResponse", "items": []})
                    return httpx.Response(200, json={"search_metadata": {"status": "Success"},
                        "organic_results": [{"position": 1, "link": "https://example.org/fixture",
                                             "title": "fixture-only-private-marker"}]})
                return fixture

            collectors = {}
            for source in (GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID):
                gateway = LivePageGateway.offline_fixture(source, Credentials(), Approvals(source), runner,
                    transport=httpx.MockTransport(respond(source)), clock=lambda: NOW)
                gateways.append(gateway)
                collectors[source] = GuardedQueryCollector(gateway, utcnow=lambda: NOW)
            wiki = WikimediaTopPageGateway.offline_fixture(Approvals(WIKIMEDIA_SOURCE_ID), runner,
                user_agent=USER_AGENT, transport=httpx.MockTransport(respond(WIKIMEDIA_SOURCE_ID)),
                clock=lambda: NOW)
            gateways.append(wiki)
            try:
                handlers = D5SourceHandlers(query_collectors=collectors, wikimedia=wiki,
                    plan=DEFAULT_QUERY_PLAN, limits=CollectionLimits(max_results=10),
                    observation_date=lambda: NOW.date(), utcnow=lambda: NOW).build()
                def grant(source, now):
                    return CheckpointGrant(source, "SYNTHETIC", "fixture-v1",
                        now + timedelta(minutes=5), 120, True)
                worker = DistributedFetchWorker(admission, handlers=handlers,
                    resolve_grant=grant, utcnow=lambda: NOW)
                service = DistributedPublicFetch(admission, worker, resolve_grant=grant, utcnow=lambda: NOW)
                key = "fixture-idempotency-key-001"
                job = service.submit(key)["job_id"]
                self.assertEqual(service.submit(key)["job_id"], job)
                await service.run_once("fixture-worker")
                result = service.status(job)
                self.assertTrue(result["terminal"])
                self.assertEqual(result["data_mode"], "SYNTHETIC")
                self.assertEqual(set(counts.values()), {1})
                self.assertIn("fixture-only-private-marker", repr(result["sources"]))
                self.assertEqual(len(result["sources"]), 4)
                for file in Path(directory).iterdir():
                    if file.is_file():
                        self.assertNotIn(b"fixture-only-private-marker", file.read_bytes())
            finally:
                for gateway in gateways:
                    await gateway.aclose()


if __name__ == "__main__":
    unittest.main()
