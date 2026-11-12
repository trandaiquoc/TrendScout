"""Offline worker integration over the atomic admission and checkpoint ports."""

import asyncio
import hashlib
import multiprocessing
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from test_d5_live_gateway import Approvals, Credentials
from test_d5_public_admission import NOW, SqliteTestStore, coordinator, policy
from test_d5_wikimedia_gateway import USER_AGENT
from test_d5_wikimedia_gateway import response as wikimedia_response

from app.jobs.distributed_worker import DistributedFetchWorker, SourceWorkContext
from app.jobs.public_admission import AdmissionDenied, PoolLimit
from app.jobs.shared_cache import CacheGrant, SharedCache
from app.jobs.source_checkpoints import CheckpointGrant, SourceCheckpointJournal
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)
from app.sources.guarded_collector import CollectionLimits, GuardedQueryCollector
from app.sources.live_gateway import LivePageGateway
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.wikimedia.live_gateway import WikimediaTopPageGateway
from app.workers.d5_source_handlers import D5SourceHandlers, _bounded_records
from app.workers.guarded_attempt import GuardedAttemptRunner

SOURCES = (WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID)


def run_worker_process(args):
    path, index = args
    config = policy(
        enabled_sources=frozenset(SOURCES),
        pools={"wikimedia": PoolLimit(2, 5, 2, 5, 1),
               "serpapi": PoolLimit(2, 5, 2, 5, 1),
               "youtube": PoolLimit(1, 3, 1, 3, 1)},
        lease_seconds=10, cooldown_seconds=0, plan_fingerprint="a" * 64,
    )
    admission = coordinator(path, config)

    def handler(context):
        return {"status": "SUCCESS", "data_mode": "SYNTHETIC",
                "records": [{"source_id": context.source_id}], "record_count": 1}

    handlers = {}
    for source in SOURCES:
        def source_handler(context, expected=source):
            if context.source_id != expected:
                raise AssertionError("handler source mismatch")
            return handler(context)
        source_handler.__trendscout_data_mode__ = "SYNTHETIC"
        handlers[source] = source_handler

    def grant(source_id, now):
        return CheckpointGrant(source_id, "SYNTHETIC", "worker-process-policy",
                               now + timedelta(minutes=10), 300, True)

    worker = DistributedFetchWorker(
        admission, handlers=handlers, resolve_grant=grant, utcnow=lambda: NOW
    )
    return asyncio.run(worker.run_once(f"offline-worker-{index}"))


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
        self.clock = MutableClock()
        self.config = policy(
            enabled_sources=frozenset(SOURCES),
            pools={"wikimedia": PoolLimit(2, 5, 2, 5, 1),
                   "serpapi": PoolLimit(2, 5, 2, 5, 1),
                   "youtube": PoolLimit(1, 3, 1, 3, 1)},
            lease_seconds=10, cooldown_seconds=0, plan_fingerprint="a" * 64,
        )
        self.admission = coordinator(self.path, self.config)
        self.gateways = []

    async def asyncTearDown(self):
        for gateway in getattr(self, "gateways", []):
            await gateway.aclose()
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

    def test_normalized_record_budget_reports_omitted_rows(self):
        selected, omitted = _bounded_records([
            {"title": "kept"}, {"snippet": "x" * 110_000}, {"title": "kept after cap"},
        ])
        self.assertEqual([row["title"] for row in selected], ["kept", "kept after cap"])
        self.assertEqual(omitted, 1)

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

    async def test_retry_beyond_job_expiry_becomes_partial_instead_of_stranding_lease(self):
        self.admit()
        retry_at = NOW + timedelta(seconds=self.config.max_job_seconds + 1)

        async def deferred(context):
            if context.source_id == BING_SOURCE_ID:
                return {"status": "DEFERRED", "retry_at": retry_at}
            return self.source_result()

        worker = self.worker({source: deferred for source in SOURCES})
        result = await worker.run_once("worker-one")
        self.assertEqual(result["state"], "PARTIAL")
        self.assertEqual(result["sources"][BING_SOURCE_ID]["error_code"],
                         "RETRY_OUTSIDE_DEADLINE")

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

    async def test_independent_worker_processes_execute_one_durable_job_once(self):
        self.admit()
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=8, mp_context=context) as processes:
            results = list(processes.map(
                run_worker_process, [(self.path, index) for index in range(8)]
            ))
        completed = [result for result in results if result is not None]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["state"], "COMPLETED")
        self.assertEqual(set(completed[0]["sources"]), set(SOURCES))

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

    async def test_wikimedia_page_receipt_recovers_after_crash_before_settlement(self):
        class SimulatedProcessCrash(BaseException):
            pass

        config = policy(
            enabled_sources=frozenset(SOURCES),
            pools={"wikimedia": PoolLimit(2, 5, 2, 5, 1),
                   "serpapi": PoolLimit(2, 5, 2, 5, 1),
                   "youtube": PoolLimit(1, 3, 1, 3, 1)},
            lease_seconds=10, cooldown_seconds=0,
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
        )
        admission = coordinator(self.path, config)
        self.admission = admission
        plan = DEFAULT_QUERY_PLAN
        job_id = admission.admit("wikimedia-receipt-crash-idempotency", now=self.clock())["job_id"]
        first_claim = admission.claim(job_id, "worker-a", now=self.clock())
        runner = GuardedAttemptRunner(admission, clock=self.clock)
        request_count = [0]

        def wikimedia_handler(_request):
            request_count[0] += 1
            return httpx.Response(200, json=wikimedia_response(NOW.date()))

        wikimedia = WikimediaTopPageGateway.offline_fixture(
            Approvals(WIKIMEDIA_SOURCE_ID), runner, user_agent=USER_AGENT,
            transport=httpx.MockTransport(wikimedia_handler), clock=self.clock,
        )
        self.gateways.append(wikimedia)
        collectors = {}
        for source in (GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID):
            gateway = LivePageGateway.offline_fixture(
                source, Credentials(), Approvals(source), runner,
                transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
                clock=self.clock,
            )
            self.gateways.append(gateway)
            collectors[source] = GuardedQueryCollector(gateway, utcnow=self.clock)
        handlers = D5SourceHandlers(
            query_collectors=collectors, wikimedia=wikimedia, plan=plan,
            limits=CollectionLimits(max_queries=1, max_pages_per_query=1, max_requests=1),
            observation_date=lambda: NOW.date(), utcnow=self.clock,
        ).build()
        handler = handlers[WIKIMEDIA_SOURCE_ID]
        grant = self.grant(WIKIMEDIA_SOURCE_ID, self.clock())
        journal = SourceCheckpointJournal(admission)
        revision = [0]

        def make_context(claim, owner, resume_snapshot, *, crash=False):
            def checkpoint(progress):
                revision[0] = journal.save(
                    job_id, owner, claim["fence"], source_id=WIKIMEDIA_SOURCE_ID,
                    grant=grant, now=self.clock(), expected_revision=revision[0],
                    snapshot={"schema": 1, "source_id": WIKIMEDIA_SOURCE_ID,
                              "data_mode": "SYNTHETIC", "plan_fingerprint": plan.fingerprint,
                              "phase": "RUNNING", "progress": progress},
                )
                if crash and "wikimedia_receipt" in progress:
                    raise SimulatedProcessCrash()
                return revision[0]

            return SourceWorkContext(
                job_id, owner, claim["fence"], WIKIMEDIA_SOURCE_ID, plan.fingerprint,
                datetime.fromisoformat(claim["expires_at"]), resume_snapshot,
                checkpoint, lambda: "OK",
            )

        with self.assertRaises(SimulatedProcessCrash):
            await handler(make_context(first_claim, "worker-a", None, crash=True))
        self.assertEqual(request_count[0], 1)

        self.clock.value += timedelta(seconds=11)
        second_claim = admission.claim_next("worker-b", now=self.clock())
        self.assertIsNotNone(second_claim)
        saved = journal.load(job_id, "worker-b", second_claim["fence"],
                             source_id=WIKIMEDIA_SOURCE_ID, grant=grant, now=self.clock())
        result = await handler(make_context(second_claim, "worker-b", saved["snapshot"]))

        self.assertEqual(request_count[0], 1)
        self.assertEqual(result["data_mode"], "SYNTHETIC")
        self.assertEqual(result["records"][0]["canonical_keyword"], "Coffee")
        self.assertFalse(admission.settle_attempt(
            job_id, "worker-b", second_claim["fence"],
            attempt_id=hashlib.sha256(
                f"{job_id}:{WIKIMEDIA_SOURCE_ID}:{NOW.date().isoformat()}:page-1".encode()
            ).hexdigest(), now=self.clock(),
        ))

    async def test_all_four_guarded_fixture_adapters_run_through_worker(self):
        self.gateways = []
        query_config = policy(
            enabled_sources=frozenset(SOURCES),
            pools=self.config.pools,
            lease_seconds=10, cooldown_seconds=0,
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
        )
        admission = coordinator(self.path, query_config)
        admission.admit("four-source-worker-fixture-key", now=self.clock())
        runner = GuardedAttemptRunner(admission, clock=self.clock)
        collectors = {}
        request_counts = {source: 0 for source in SOURCES}

        def search_handler(source):
            def handle(request):
                request_counts[source] += 1
                return httpx.Response(200, json={
                    "search_metadata": {"status": "Success"},
                    "organic_results": [{"position": 1, "link": "https://example.org/fixture",
                                         "title": "synthetic fixture result"}],
                })
            return handle

        def youtube_handler(_request):
            request_counts[YOUTUBE_SOURCE_ID] += 1
            return httpx.Response(200, json={
                "kind": "youtube#searchListResponse",
                "items": [{"kind": "youtube#searchResult",
                           "id": {"kind": "youtube#video", "videoId": "AbCdEf12345"},
                           "snippet": {"title": "synthetic video", "publishedAt": "2026-11-11T00:00:00Z"}}],
            })

        for source in (GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID):
            handler = youtube_handler if source == YOUTUBE_SOURCE_ID else search_handler(source)
            gateway = LivePageGateway.offline_fixture(
                source, Credentials(), Approvals(source), runner,
                transport=httpx.MockTransport(handler), clock=self.clock,
            )
            self.gateways.append(gateway)
            collectors[source] = GuardedQueryCollector(gateway, utcnow=self.clock)

        wikimedia = WikimediaTopPageGateway.offline_fixture(
            Approvals(WIKIMEDIA_SOURCE_ID), runner, user_agent=USER_AGENT,
            transport=httpx.MockTransport(lambda _request: self._counted_wikimedia_response(
                request_counts)), clock=self.clock,
        )
        self.gateways.append(wikimedia)
        cache = SharedCache(SqliteTestStore(self.path))

        def cache_grant(source_id, now):
            return CacheGrant(source_id, "fixture-cache-policy-v1", now + timedelta(hours=1),
                              max_fresh_seconds=600, max_stale_seconds=1200,
                              max_retention_seconds=1800, allowed=True)

        registry = D5SourceHandlers(
            query_collectors=collectors, wikimedia=wikimedia, plan=DEFAULT_QUERY_PLAN,
            limits=CollectionLimits(max_queries=1, max_pages_per_query=1, max_requests=1),
            observation_date=lambda: NOW.date(), shared_cache=cache,
            resolve_cache_grant=cache_grant, utcnow=self.clock,
        ).build()
        worker = DistributedFetchWorker(admission, handlers=registry,
                                        resolve_grant=self.grant, utcnow=self.clock)
        result = await worker.run_once("fixture-worker")

        self.assertEqual(set(result["sources"]), set(SOURCES))
        for source_result in result["sources"].values():
            self.assertEqual(source_result["data_mode"], "SYNTHETIC")
            self.assertGreater(source_result["record_count"], 0)
        self.assertNotIn("raw_record", repr(result))
        first_request_counts = dict(request_counts)

        admission.admit("four-source-worker-cached-key", now=self.clock())
        cached = await worker.run_once("cached-fixture-worker")
        self.assertEqual(cached["state"], "PARTIAL", repr(cached["sources"]))
        self.assertEqual(request_counts, first_request_counts)
        self.assertTrue(all(item.get("delivery_state") == "CACHED"
                            for item in cached["sources"].values()))

    @staticmethod
    def _counted_wikimedia_response(request_counts):
        request_counts[WIKIMEDIA_SOURCE_ID] += 1
        return httpx.Response(200, json=wikimedia_response(NOW.date()))

    async def test_public_fetch_facade_reads_retained_worker_results(self):
        results = {source: self.source_result(1) for source in SOURCES}
        handlers = {}
        for source, result in results.items():
            def handler(_context, value=result):
                return value
            handler.__trendscout_data_mode__ = "SYNTHETIC"
            handlers[source] = handler
        admission = coordinator(self.path, self.config)
        worker = DistributedFetchWorker(admission, handlers=handlers,
                                        resolve_grant=self.grant, utcnow=self.clock)
        public = DistributedPublicFetch(admission, worker,
                                        resolve_grant=self.grant, utcnow=self.clock)
        accepted = public.submit("public-facade-idempotency-key")
        self.assertEqual(accepted["status"], "QUEUED")
        completed = await public.run_once("public-worker")
        self.assertEqual(completed["state"], "COMPLETED")
        status = public.status(accepted["job_id"])
        self.assertEqual(status["status"], "COMPLETED")
        self.assertEqual(status["data_mode"], "SYNTHETIC")
        self.assertEqual({row["source_id"] for row in status["sources"]}, set(SOURCES))
        self.assertTrue(all(row["delivery_state"] == "FRESH" for row in status["sources"]))
        self.assertTrue(all(row["records"] for row in status["sources"]))

    async def test_public_poll_budget_is_shared_and_atomic(self):
        config = policy(
            enabled_sources=frozenset(SOURCES), pools=self.config.pools,
            lease_seconds=10, cooldown_seconds=0, max_status_polls=1,
            plan_fingerprint="a" * 64,
        )
        admission = coordinator(self.path, config)
        handlers = {}
        for source in SOURCES:
            def handler(_context):
                return self.source_result()
            handler.__trendscout_data_mode__ = "SYNTHETIC"
            handlers[source] = handler
        worker = DistributedFetchWorker(admission, handlers=handlers,
                                        resolve_grant=self.grant, utcnow=self.clock)
        public = DistributedPublicFetch(admission, worker,
                                        resolve_grant=self.grant, utcnow=self.clock)
        accepted = public.submit("public-poll-limit-idempotency-key")
        self.assertIsNotNone(public.status(accepted["job_id"]))
        with self.assertRaisesRegex(AdmissionDenied, "STATUS_POLL_LIMIT"):
            public.status(accepted["job_id"])

    async def test_public_fetch_facade_rejects_undeclared_or_live_handlers(self):
        admission = coordinator(self.path, self.config)
        for mode in (None, "LIVE"):
            handlers = {}
            for source in SOURCES:
                def handler(_context):
                    return self.source_result()
                if mode is not None:
                    handler.__trendscout_data_mode__ = mode
                handlers[source] = handler
            worker = DistributedFetchWorker(admission, handlers=handlers,
                                            resolve_grant=self.grant, utcnow=self.clock)
            with self.assertRaisesRegex(ValueError, "explicitly synthetic"):
                DistributedPublicFetch(admission, worker,
                                       resolve_grant=self.grant, utcnow=self.clock)


if __name__ == "__main__":
    unittest.main()
