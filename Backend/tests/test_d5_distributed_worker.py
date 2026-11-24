"""Offline worker integration over the atomic admission and checkpoint ports."""

import asyncio
import hashlib
import multiprocessing
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier

import httpx
from test_d5_live_gateway import Approvals, Credentials
from test_d5_public_admission import NOW, SqliteTestStore, coordinator, policy
from test_d5_wikimedia_gateway import USER_AGENT
from test_d5_wikimedia_gateway import response as wikimedia_response

from app.jobs.distributed_worker import DistributedFetchWorker, SourceWorkContext
from app.jobs.public_admission import (
    MANUAL_SOURCE_REFRESH_MARKER,
    OWNER_FRESH_ACCEPTANCE_MARKER,
    AdmissionDenied,
    PoolLimit,
)
from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.jobs.source_checkpoints import CheckpointGrant, SourceCheckpointJournal
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
    source_capability,
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

    async def test_shared_terminal_status_reads_independent_sources_concurrently(self):
        job_id = self.admit()
        def handler(context):
            return self.source_result(1)
        handler.__trendscout_data_mode__ = "SYNTHETIC"
        worker = self.worker({source: handler for source in SOURCES})
        await worker.run_once("status-worker")
        service = DistributedPublicFetch(
            self.admission, worker, resolve_grant=self.grant, utcnow=self.clock,
        )
        # Exercise the shared read scheduling with synthetic source receipts.
        service.firestore_shared_live = True
        barrier = Barrier(len(SOURCES), timeout=5)
        original = service.journal.completed_result

        def concurrent_read(*args, **kwargs):
            barrier.wait()
            return original(*args, **kwargs)

        service.journal.completed_result = concurrent_read
        result = service.status(job_id, include_records=True)
        self.assertEqual([source["source_id"] for source in result["sources"]], sorted(SOURCES))
        self.assertTrue(all(source["record_count"] == 1 for source in result["sources"]))

    async def test_configured_source_order_is_used_for_fetch_all(self):
        self.admit()
        seen = []

        def handler(context):
            seen.append(context.source_id)
            return self.source_result(1)

        order = (GOOGLE_SOURCE_ID, BING_SOURCE_ID,
                 WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID)
        worker = DistributedFetchWorker(
            coordinator(self.path, self.config),
            handlers={source: handler for source in SOURCES},
            resolve_grant=self.grant,
            utcnow=self.clock,
            source_order=order,
        )

        outcome = await worker.run_once("worker-ordered")

        self.assertEqual(outcome["state"], "COMPLETED")
        self.assertEqual(seen, list(order))

    async def test_bounded_source_slices_resume_one_job_without_repeating_sources(self):
        job_id = self.admit()
        order = (GOOGLE_SOURCE_ID, BING_SOURCE_ID,
                 WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID)
        seen = []

        def handler(context):
            seen.append(context.source_id)
            return self.source_result(1)

        worker = DistributedFetchWorker(
            coordinator(self.path, self.config),
            handlers={source: handler for source in SOURCES},
            resolve_grant=self.grant,
            utcnow=self.clock,
            source_order=order,
        )
        states = []
        for index, source_id in enumerate(order):
            outcome = await worker.run_once(
                f"slice-worker-{index}", job_id=job_id, max_sources=1,
            )
            states.append(outcome["state"])
            self.assertEqual(seen, list(order[:index + 1]))
            self.assertEqual(outcome["job_id"], job_id)
            self.assertEqual(
                outcome["state"], "COMPLETED" if index == len(order) - 1 else "QUEUED",
            )

        status = self.admission.read_job(job_id, now=self.clock())
        self.assertEqual(states, ["QUEUED", "QUEUED", "QUEUED", "COMPLETED"])
        self.assertEqual(status["attempts"], 1)

    async def test_slice_continues_only_confirmed_zero_request_cached_sources(self):
        job_id = self.admit()
        order = (GOOGLE_SOURCE_ID, BING_SOURCE_ID,
                 WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID)
        seen = []

        def handler(context):
            seen.append(context.source_id)
            cached = context.source_id != WIKIMEDIA_SOURCE_ID
            return {**self.source_result(1),
                    "delivery_state": "CACHED" if cached else "FRESH",
                    "provider_request_count": 0 if cached else 1}

        worker = DistributedFetchWorker(
            coordinator(self.path, self.config),
            handlers={source: handler for source in SOURCES},
            resolve_grant=self.grant, utcnow=self.clock, source_order=order,
        )
        first = await worker.run_once("cache-slice-1", job_id=job_id,
                                      max_sources=1, continue_cached=True)
        self.assertEqual(first["state"], "QUEUED")
        self.assertEqual(seen, list(order[:3]))
        second = await worker.run_once("cache-slice-2", job_id=job_id,
                                       max_sources=1, continue_cached=True)
        self.assertEqual(second["state"], "COMPLETED")
        self.assertEqual(seen, list(order))

    async def test_slice_completes_all_cached_sources_in_one_invocation(self):
        job_id = self.admit()
        seen = []

        def handler(context):
            seen.append(context.source_id)
            return {**self.source_result(1), "delivery_state": "CACHED",
                    "provider_request_count": 0}

        worker = self.worker({source: handler for source in SOURCES})
        outcome = await worker.run_once("cached-slice", job_id=job_id,
                                        max_sources=1, continue_cached=True)
        self.assertEqual(outcome["state"], "COMPLETED")
        self.assertEqual(set(seen), set(SOURCES))
        self.assertTrue(all(value["provider_request_count"] == 0
                            for value in outcome["sources"].values()))

    async def test_source_admission_failure_is_saved_and_later_sources_finish(self):
        job_id = self.admit()
        order = (GOOGLE_SOURCE_ID, BING_SOURCE_ID,
                 WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID)
        seen = []

        def handler(context):
            seen.append(context.source_id)
            if context.source_id == WIKIMEDIA_SOURCE_ID:
                raise AdmissionDenied("RESULT_PERSISTENCE_FAILED")
            return self.source_result(1)
        handler.__trendscout_data_mode__ = "SYNTHETIC"

        worker = DistributedFetchWorker(
            coordinator(self.path, self.config),
            handlers={source: handler for source in SOURCES},
            resolve_grant=self.grant,
            utcnow=self.clock,
            source_order=order,
        )
        service = DistributedPublicFetch(
            self.admission, worker, resolve_grant=self.grant, utcnow=self.clock,
        )

        outcome = await worker.run_once("worker-source-failure")
        status = service.status(job_id)

        self.assertEqual(seen, list(order))
        self.assertEqual(outcome["state"], "PARTIAL")
        self.assertEqual(outcome["sources"][WIKIMEDIA_SOURCE_ID]["status"], "FAILED")
        self.assertEqual(
            outcome["sources"][WIKIMEDIA_SOURCE_ID]["error_code"],
            "RESULT_PERSISTENCE_FAILED",
        )
        self.assertTrue(status["terminal"])
        self.assertEqual(status["progress"]["completed_sources"], 4)
        self.assertEqual(
            next(source for source in status["sources"]
                 if source["source_id"] == YOUTUBE_SOURCE_ID)["status"],
            "SUCCESS",
        )

    async def test_sanitized_transport_diagnostic_survives_result_validation_and_status(self):
        job_id = self.admit()
        diagnostic = {
            "source_id": WIKIMEDIA_SOURCE_ID,
            "failure_phase": "BODY_STREAM",
            "http_status": 200,
            "exception_class": "ReadError",
            "bytes_received": 12,
            "response_size_limit": 1_000_000,
            "attempt_id": "a" * 64,
            "error_code": "UPSTREAM_ERROR",
        }

        def source_handler(context):
            if context.source_id == WIKIMEDIA_SOURCE_ID:
                return {"status": "FAILED", "data_mode": "SYNTHETIC", "records": [],
                        "record_count": 0, "error_code": "UPSTREAM_ERROR",
                        "transport_diagnostic": diagnostic}
            return self.source_result()

        source_handler.__trendscout_data_mode__ = "SYNTHETIC"
        handlers = {source: source_handler for source in SOURCES}
        worker = self.worker(handlers)
        service = DistributedPublicFetch(
            self.admission, worker, resolve_grant=self.grant, utcnow=self.clock,
        )
        await worker.run_once("diagnostic-worker")
        result = service.status(job_id)
        wiki = next(source for source in result["sources"]
                    if source["source_id"] == WIKIMEDIA_SOURCE_ID)
        self.assertEqual(wiki["transport_diagnostic"], diagnostic)
        self.assertNotIn("body", wiki["transport_diagnostic"])
        self.assertIsNone(wiki["delivery_state"])

        unsafe = dict(diagnostic, raw_body="private-marker")
        with self.assertRaisesRegex(ValueError, "diagnostic shape"):
            DistributedFetchWorker._validate_result({
                "status": "FAILED", "data_mode": "SYNTHETIC", "records": [],
                "record_count": 0, "error_code": "UPSTREAM_ERROR",
                "transport_diagnostic": unsafe,
            }, expected_mode="SYNTHETIC")

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

    async def test_fetch1_recovery_preserves_receipt_and_source_diagnostics(self):
        provider_calls = {source: 0 for source in SOURCES}
        persistence_diagnostic = {
            "source_id": GOOGLE_SOURCE_ID,
            "failure_phase": "RESULT_PERSISTENCE",
            "failure_reason": "CALLBACK_ERROR",
            "exception_class": "OSError",
            "attempt_id": "a" * 64,
            "error_code": "RESULT_PERSISTENCE_FAILED",
        }

        def wikimedia(context):
            saved = (context.resume_snapshot or {}).get("progress", {}).get("receipt")
            if isinstance(saved, dict):
                return saved["result"]
            provider_calls[WIKIMEDIA_SOURCE_ID] += 1
            result = {
                "status": "SUCCESS", "data_mode": "SYNTHETIC",
                "delivery_state": "FRESH", "records": [{"evidence_id": "wiki"}],
                "record_count": 1, "provider_request_count": 1,
                "coverage": {"provider_request_count": 1},
            }
            context.checkpoint({"receipt": {"result": result}})
            raise SimulatedProcessCrash()

        def google(_context):
            provider_calls[GOOGLE_SOURCE_ID] += 1
            return {
                "status": "FAILED", "data_mode": "SYNTHETIC", "records": [],
                "record_count": 0, "error_code": "RESULT_PERSISTENCE_FAILED",
                "coverage": {"persistence_diagnostic": persistence_diagnostic},
            }

        def bing(_context):
            provider_calls[BING_SOURCE_ID] += 1
            return {
                "status": "UNAVAILABLE", "data_mode": "SYNTHETIC", "records": [],
                "record_count": 0, "error_code": "NO_SAVED_CANDIDATE",
            }

        def youtube(_context):
            provider_calls[YOUTUBE_SOURCE_ID] += 1
            raise RuntimeError("private credential-like text must not be retained")

        handlers = {
            WIKIMEDIA_SOURCE_ID: wikimedia,
            GOOGLE_SOURCE_ID: google,
            BING_SOURCE_ID: bing,
            YOUTUBE_SOURCE_ID: youtube,
        }
        for handler in handlers.values():
            handler.__trendscout_data_mode__ = "SYNTHETIC"
        worker = DistributedFetchWorker(
            coordinator(self.path, self.config), handlers=handlers,
            resolve_grant=self.grant, utcnow=self.clock, source_order=SOURCES,
        )
        retry_gate = [0]
        service = DistributedPublicFetch(
            self.admission, worker, resolve_grant=self.grant, utcnow=self.clock,
            provider_retry_after_seconds=lambda: retry_gate[0],
        )
        key = "fetch1-recovery-google-youtube-0001"
        accepted = service.submit(key, source_ids=list(SOURCES))

        with self.assertRaises(SimulatedProcessCrash):
            await worker.run_once("fetch1-worker-first")
        self.assertEqual(provider_calls[WIKIMEDIA_SOURCE_ID], 1)

        self.clock.value += timedelta(seconds=self.config.lease_seconds + 1)
        interrupted = service.status(accepted["job_id"])
        self.assertEqual(interrupted["status"], "QUEUED")
        self.assertTrue(interrupted["recovery_available"])
        retry_gate[0] = 30
        replay = service.submit(key, source_ids=list(SOURCES))
        self.assertEqual(replay["result"], "REPLAY")
        self.assertEqual(replay["job_id"], accepted["job_id"])

        outcome = await worker.run_once("fetch1-worker-recovery")
        status = service.status(accepted["job_id"])
        by_source = {item["source_id"]: item for item in status["sources"]}
        history = service.history(limit=1)[0]
        history_by_source = {item["source_id"]: item for item in history["source_manifest"]}

        self.assertEqual(outcome["state"], "PARTIAL")
        self.assertTrue(status["terminal"])
        self.assertEqual(set(by_source), set(SOURCES))
        self.assertEqual(by_source[WIKIMEDIA_SOURCE_ID]["status"], "SUCCESS")
        self.assertEqual(by_source[WIKIMEDIA_SOURCE_ID]["provider_request_count"], 1)
        self.assertEqual(by_source[GOOGLE_SOURCE_ID]["status"], "FAILED")
        self.assertEqual(by_source[GOOGLE_SOURCE_ID]["coverage"]["persistence_diagnostic"],
                         persistence_diagnostic)
        self.assertEqual(by_source[BING_SOURCE_ID]["status"], "UNAVAILABLE")
        self.assertEqual(by_source[YOUTUBE_SOURCE_ID]["status"], "FAILED")
        self.assertEqual(by_source[YOUTUBE_SOURCE_ID]["coverage"]["worker_failure_diagnostic"], {
            "source_id": YOUTUBE_SOURCE_ID,
            "failure_phase": "SOURCE_EXECUTION",
            "failure_reason": "HANDLER_EXCEPTION",
            "exception_class": "RuntimeError",
            "error_code": "SOURCE_EXECUTION_FAILED",
        })
        self.assertNotIn("credential-like", repr(status))
        self.assertEqual(provider_calls, {
            WIKIMEDIA_SOURCE_ID: 1, GOOGLE_SOURCE_ID: 1,
            BING_SOURCE_ID: 1, YOUTUBE_SOURCE_ID: 1,
        })
        self.assertEqual(history_by_source[GOOGLE_SOURCE_ID]["coverage"]["persistence_diagnostic"],
                         persistence_diagnostic)
        self.assertEqual(history_by_source[YOUTUBE_SOURCE_ID]["coverage"]["worker_failure_diagnostic"],
                         by_source[YOUTUBE_SOURCE_ID]["coverage"]["worker_failure_diagnostic"])

    async def test_terminal_failed_recovery_keeps_completed_wikimedia_result(self):
        provider_calls = {source: 0 for source in SOURCES}

        def wikimedia(_context):
            provider_calls[WIKIMEDIA_SOURCE_ID] += 1
            return {
                "status": "SUCCESS", "data_mode": "SYNTHETIC",
                "delivery_state": "FRESH", "records": [{"evidence_id": "wiki"}],
                "record_count": 1, "provider_request_count": 1,
            }

        def crash_google(_context):
            provider_calls[GOOGLE_SOURCE_ID] += 1
            raise SimulatedProcessCrash()

        def untouched(context):
            provider_calls[context.source_id] += 1
            return self.source_result(1)

        for handler in (wikimedia, crash_google, untouched):
            handler.__trendscout_data_mode__ = "SYNTHETIC"

        worker = DistributedFetchWorker(
            coordinator(self.path, self.config),
            handlers={WIKIMEDIA_SOURCE_ID: wikimedia, GOOGLE_SOURCE_ID: crash_google,
                      BING_SOURCE_ID: untouched, YOUTUBE_SOURCE_ID: untouched},
            resolve_grant=self.grant, utcnow=self.clock, source_order=SOURCES,
        )
        service = DistributedPublicFetch(
            self.admission, worker, resolve_grant=self.grant, utcnow=self.clock,
        )
        key = "fetch1-terminal-recovery-wikimedia-0001"
        accepted = service.submit(key, source_ids=list(SOURCES))

        for attempt in range(self.config.max_attempts):
            with self.assertRaises(SimulatedProcessCrash):
                await worker.run_once(f"fetch1-crash-{attempt}")
            self.clock.value += timedelta(seconds=self.config.lease_seconds + 1)
            if attempt + 1 < self.config.max_attempts:
                interrupted = service.status(accepted["job_id"])
                self.assertTrue(interrupted["recovery_available"])
                replay = service.submit(key, source_ids=list(SOURCES))
                self.assertEqual(replay["result"], "REPLAY")

        status = service.status(accepted["job_id"])
        by_source = {item["source_id"]: item for item in status["sources"]}
        self.assertEqual(status["status"], "PARTIAL")
        self.assertEqual(set(by_source), set(SOURCES))
        self.assertEqual(by_source[WIKIMEDIA_SOURCE_ID]["status"], "SUCCESS")
        self.assertEqual(by_source[WIKIMEDIA_SOURCE_ID]["provider_request_count"], 1)
        self.assertTrue(all(by_source[source]["status"] == "FAILED"
                            for source in (GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID)))
        self.assertEqual(provider_calls[WIKIMEDIA_SOURCE_ID], 1)
        self.assertEqual(provider_calls[GOOGLE_SOURCE_ID], self.config.max_attempts)

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
        for source_id, item in result["sources"].items():
            self.assertEqual(item, {
                "status": "FAILED", "data_mode": "SYNTHETIC", "records": [],
                "record_count": 0, "error_code": "SOURCE_EXECUTION_FAILED",
                "coverage": {"worker_failure_diagnostic": {
                    "source_id": source_id,
                    "failure_phase": "RESULT_VALIDATION",
                    "failure_reason": "INVALID_SOURCE_RESULT",
                    "exception_class": "ValueError",
                    "error_code": "SOURCE_EXECUTION_FAILED",
                }},
            })

    async def test_non_finite_google_result_gets_safe_serialization_diagnostic(self):
        def handler(context):
            if context.source_id == GOOGLE_SOURCE_ID:
                return {
                    "status": "SUCCESS", "data_mode": "SYNTHETIC",
                    "records": [{"value": float("nan")}], "record_count": 1,
                }
            return self.source_result(1)

        handlers = {source: handler for source in SOURCES}
        for source_handler in handlers.values():
            source_handler.__trendscout_data_mode__ = "SYNTHETIC"
        worker = DistributedFetchWorker(
            coordinator(self.path, self.config), handlers=handlers,
            resolve_grant=self.grant, utcnow=self.clock,
        )
        service = DistributedPublicFetch(
            self.admission, worker, resolve_grant=self.grant, utcnow=self.clock,
        )
        accepted = service.submit(
            "google-non-finite-result-serialization-0001", source_ids=list(SOURCES),
        )
        outcome = await worker.run_once("google-serialization-worker")
        status = service.status(accepted["job_id"])
        google = next(item for item in status["sources"]
                      if item["source_id"] == GOOGLE_SOURCE_ID)

        self.assertEqual(outcome["state"], "PARTIAL")
        self.assertEqual(google["status"], "FAILED")
        self.assertEqual(google["error_code"], "RESULT_SERIALIZATION_FAILED")
        self.assertEqual(google["coverage"]["worker_failure_diagnostic"], {
            "source_id": GOOGLE_SOURCE_ID,
            "failure_phase": "RESULT_SERIALIZATION",
            "failure_reason": "RESULT_NOT_SERIALIZABLE",
            "exception_class": "ValueError",
            "error_code": "RESULT_SERIALIZATION_FAILED",
        })

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
        cache = SharedCache(SqliteTestStore(self.path))
        cache_policy = "d6-wikimedia-recovery-cache-v1"

        def cache_grant(source_id, now):
            return CacheGrant(
                source_id, cache_policy, now + timedelta(days=1),
                600, 1200, 1800, allowed=True,
            )

        job_id = admission.admit(
            "wikimedia-receipt-crash-idempotency", now=self.clock(),
            request_metadata={"sources": sorted(SOURCES)},
            owner_fresh_acceptance_once=True,
        )["job_id"]
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
            observation_date=lambda: NOW.date(), shared_cache=cache,
            resolve_cache_grant=cache_grant, utcnow=self.clock,
        ).build()
        handler = handlers[WIKIMEDIA_SOURCE_ID]
        grant = self.grant(WIKIMEDIA_SOURCE_ID, self.clock())
        capability = source_capability(WIKIMEDIA_SOURCE_ID)
        cache_scope = CacheScope(
            source_id=WIKIMEDIA_SOURCE_ID,
            provider_id=capability.provider_id,
            query=f"{plan.fingerprint}:{NOW.date().isoformat()}",
            plan_fingerprint=plan.fingerprint,
            market=plan.market,
            language=plan.language or "all",
            window=plan.time_filter_rule,
            parser_version=f"source-v{capability.capability_version}",
            policy_version=cache_policy,
        )
        journal = SourceCheckpointJournal(admission)
        revision = [0]

        def make_context(claim, owner, resume_snapshot, *, current_job_id=job_id,
                         crash=False):
            def checkpoint(progress):
                revision[0] = journal.save(
                    current_job_id, owner, claim["fence"], source_id=WIKIMEDIA_SOURCE_ID,
                    grant=grant, now=self.clock(), expected_revision=revision[0],
                    snapshot={"schema": 1, "source_id": WIKIMEDIA_SOURCE_ID,
                              "data_mode": "SYNTHETIC", "plan_fingerprint": plan.fingerprint,
                              "phase": "RUNNING", "progress": progress},
                )
                if crash and "wikimedia_receipt" in progress:
                    raise SimulatedProcessCrash()
                return revision[0]

            return SourceWorkContext(
                current_job_id, owner, claim["fence"], WIKIMEDIA_SOURCE_ID, plan.fingerprint,
                datetime.fromisoformat(claim["expires_at"]), resume_snapshot,
                checkpoint, lambda: "OK",
                request_metadata=claim.get("request_metadata", {}),
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
        self.assertEqual(result["delivery_state"], "FRESH")
        self.assertTrue(result["coverage"]["owner_fresh_acceptance"])
        self.assertEqual(result["provider_request_count"], 1)
        self.assertFalse(admission.settle_attempt(
            job_id, "worker-b", second_claim["fence"],
            attempt_id=hashlib.sha256(
                f"{job_id}:{WIKIMEDIA_SOURCE_ID}:{NOW.date().isoformat()}:page-1".encode()
            ).hexdigest(), now=self.clock(),
        ))
        admission.finish(job_id, "worker-b", second_claim["fence"], now=self.clock())

        second_job_id = admission.admit(
            "wikimedia-receipt-cache-idempotency", now=self.clock(),
            request_metadata={"sources": sorted(SOURCES)},
            owner_fresh_acceptance_once=True,
        )["job_id"]
        second_job_claim = admission.claim(second_job_id, "worker-c", now=self.clock())
        second_result = await handler(make_context(
            second_job_claim, "worker-c", None, current_job_id=second_job_id,
        ))
        cache_entry = cache.get(
            cache_scope, grant=cache_grant(WIKIMEDIA_SOURCE_ID, self.clock()),
            now=self.clock(),
        )

        self.assertEqual(second_result["delivery_state"], "CACHED")
        self.assertEqual(second_result["provider_request_count"], 0)
        self.assertEqual(second_result["coverage"]["provider_request_count"], 0)
        self.assertEqual(second_result["coverage"]["cache_fetched_at"],
                         result["records"][0]["fetched_at"])
        self.assertEqual(cache_entry["status"], "CACHED")
        self.assertEqual(request_count[0], 1)

    async def test_youtube_source_selection_supports_requested_combinations(self):
        combinations = (
            (YOUTUBE_SOURCE_ID,),
            (YOUTUBE_SOURCE_ID, BING_SOURCE_ID),
            (YOUTUBE_SOURCE_ID, GOOGLE_SOURCE_ID),
            SOURCES,
        )
        handled = []
        handlers = {}
        for source in SOURCES:
            def collect(context, expected=source):
                self.assertEqual(context.source_id, expected)
                handled.append(expected)
                records = ([{
                    "provider_video_id": "AbCdEf12345",
                    "canonical_video_url": "https://www.youtube.com/watch?v=AbCdEf12345",
                    "title": "Offline fixture video",
                }] if expected == YOUTUBE_SOURCE_ID else [{"source_id": expected}])
                return {"status": "SUCCESS", "data_mode": "SYNTHETIC",
                        "records": records, "record_count": len(records)}

            collect.__trendscout_data_mode__ = "SYNTHETIC"
            handlers[source] = collect

        worker = self.worker(handlers)
        for index, selected in enumerate(combinations):
            if index:
                self.clock.value += timedelta(hours=1)
            handled.clear()
            self.admission.admit(
                f"youtube-source-combinations-{index:02d}",
                now=self.clock(),
                request_metadata={"sources": list(selected)},
            )
            result = await worker.run_once("source-combination-worker")
            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(set(result["sources"]), set(selected))
            self.assertEqual(set(handled), set(selected))
            if selected == (YOUTUBE_SOURCE_ID,):
                self.assertEqual(result["sources"][YOUTUBE_SOURCE_ID]["records"][0]
                                 ["provider_video_id"], "AbCdEf12345")

    async def test_youtube_fetch_result_is_readable_after_sqlite_reopen(self):
        video = {
            "provider_video_id": "AbCdEf12345",
            "canonical_video_url": "https://www.youtube.com/watch?v=AbCdEf12345",
            "title": "Offline evidence fixture",
        }

        def youtube_handler(context):
            self.assertEqual(context.source_id, YOUTUBE_SOURCE_ID)
            return {"status": "SUCCESS", "data_mode": "SYNTHETIC",
                    "records": [video], "record_count": 1,
                    "coverage": {"provider_method": "search.list"}}

        def unused_source_handler(_context):
            return {"status": "SUCCESS", "data_mode": "SYNTHETIC",
                    "records": [], "record_count": 0}

        youtube_handler.__trendscout_data_mode__ = "SYNTHETIC"
        unused_source_handler.__trendscout_data_mode__ = "SYNTHETIC"
        handlers = {source: (youtube_handler if source == YOUTUBE_SOURCE_ID
                             else unused_source_handler) for source in SOURCES}
        self.admission.admit(
            "youtube-sqlite-reopen-evidence-0001",
            now=self.clock(),
            request_metadata={"sources": [YOUTUBE_SOURCE_ID]},
        )
        worker = self.worker(handlers)
        outcome = await worker.run_once("youtube-sqlite-writer")
        self.assertEqual(outcome["state"], "COMPLETED")

        reopened_admission = coordinator(self.path, self.config)
        reopened_worker = DistributedFetchWorker(
            reopened_admission,
            handlers=handlers,
            resolve_grant=self.grant,
            utcnow=self.clock,
        )
        reopened_fetch = DistributedPublicFetch(
            reopened_admission,
            reopened_worker,
            resolve_grant=self.grant,
            utcnow=self.clock,
        )
        saved = reopened_fetch.status(outcome["job_id"], include_records=True)

        self.assertEqual(saved["status"], "COMPLETED")
        self.assertEqual(saved["sources"][0]["source_id"], YOUTUBE_SOURCE_ID)
        self.assertEqual(saved["sources"][0]["records"][0]["provider_video_id"],
                         "AbCdEf12345")
        self.assertEqual(saved["sources"][0]["records"][0]["title"],
                         "Offline evidence fixture")

    async def test_all_four_guarded_fixture_adapters_run_through_worker(self):
        self.gateways = []
        query_config = policy(
            enabled_sources=frozenset(SOURCES),
            pools={**self.config.pools, "youtube": PoolLimit(2, 5, 2, 5, 1)},
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
                    "search_metadata": {"status": "Cached" if source == GOOGLE_SOURCE_ID else "Success"},
                    "organic_results": [{"position": 1, "link": "https://example.org/fixture",
                                         "title": "synthetic fixture result"}],
                })
            return handle

        youtube_calls = []

        def youtube_handler(request):
            request_counts[YOUTUBE_SOURCE_ID] += 1
            youtube_calls.append(request)
            if len(youtube_calls) > 1:
                return httpx.Response(500, json={"error": {"errors": [{"reason": "backendError"}]}})
            return httpx.Response(200, json={
                "kind": "youtube#searchListResponse",
                "items": [{"kind": "youtube#searchResult",
                           "id": {"kind": "youtube#video", "videoId": "AbCdEf12345"},
                           "snippet": {"title": "synthetic video",
                                       "publishedAt": "2026-11-11T00:00:00Z"}}],
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
            resolve_cache_grant=cache_grant,
            resolve_provider_batch=lambda source_id, _now: (
                {"allowed": True} if source_id == YOUTUBE_SOURCE_ID else None
            ),
            utcnow=self.clock,
        ).build()
        worker = DistributedFetchWorker(admission, handlers=registry,
                                        resolve_grant=self.grant, utcnow=self.clock)
        result = await worker.run_once("fixture-worker")

        self.assertEqual(set(result["sources"]), set(SOURCES))
        for source_result in result["sources"].values():
            self.assertEqual(source_result["data_mode"], "SYNTHETIC")
            self.assertGreater(source_result["record_count"], 0)
        self.assertEqual(result["sources"][GOOGLE_SOURCE_ID]["delivery_state"], "CACHED")
        self.assertEqual(result["sources"][BING_SOURCE_ID]["delivery_state"], "FRESH")
        self.assertEqual(result["sources"][YOUTUBE_SOURCE_ID]["delivery_state"], "FRESH")
        self.assertNotIn("raw_record", repr(result))
        first_request_counts = dict(request_counts)

        cached_id = admission.admit("four-source-worker-cached-key", now=self.clock())["job_id"]
        cached = await worker.run_once("cached-fixture-worker", job_id=cached_id,
                                       max_sources=1, continue_cached=True)
        self.assertEqual(cached["state"], "PARTIAL", repr(cached["sources"]))
        self.assertEqual(request_counts, first_request_counts)
        self.assertTrue(all(item.get("delivery_state") == "CACHED"
                            for item in cached["sources"].values()),
                        repr({source: {"delivery_state": item.get("delivery_state"),
                                       "status": item.get("status"),
                                       "error_code": item.get("error_code")}
                              for source, item in cached["sources"].items()}))
        self.assertEqual(len(youtube_calls), 1)
        self.assertIsNotNone(cached["sources"][YOUTUBE_SOURCE_ID]
                             ["coverage"]["cache_delivery_at"])
        self.assertEqual(cached["sources"][YOUTUBE_SOURCE_ID]
                         ["coverage"]["provider_request_count"], 0)
        self.assertEqual(cached["sources"][YOUTUBE_SOURCE_ID]
                         ["coverage"]["cached_provider_request_count"], 1)

        self.clock.value += timedelta(seconds=601)
        admission.admit("four-source-worker-stale-fallback-key", now=self.clock())
        stale = await worker.run_once("stale-cache-worker")
        self.assertEqual(stale["state"], "PARTIAL", repr(stale["sources"]))
        youtube = stale["sources"][YOUTUBE_SOURCE_ID]
        self.assertEqual(youtube["status"], "PARTIAL")
        self.assertEqual(youtube["delivery_state"], "STALE_CACHE")
        self.assertEqual(youtube["records"][0]["provider_video_id"], "AbCdEf12345")
        self.assertEqual(youtube["coverage"]["source_fetched_at"],
                         result["sources"][YOUTUBE_SOURCE_ID]["coverage"]["source_fetched_at"])
        self.assertEqual(youtube["coverage"]["refresh_attempt_status"], "FAILED")
        self.assertEqual(youtube["coverage"]["provider_request_count"], 1)
        self.assertEqual(youtube["provider_request_count"], 1)
        self.assertEqual(len(youtube_calls), 2)

    async def test_manual_fresh_test_bypasses_cache_once_then_returns_to_cache_first(self):
        cache = SharedCache(SqliteTestStore(self.path))

        def cache_grant(source_id, now):
            return CacheGrant(
                source_id, "manual-fresh-test-policy", now + timedelta(days=1),
                max_fresh_seconds=600, max_stale_seconds=1200,
                max_retention_seconds=1800, allowed=True,
            )

        def registry(*, force_fresh=False):
            return D5SourceHandlers(
                query_collectors={
                    GOOGLE_SOURCE_ID: None,
                    BING_SOURCE_ID: None,
                    YOUTUBE_SOURCE_ID: None,
                },
                wikimedia=None, plan=DEFAULT_QUERY_PLAN,
                limits=CollectionLimits(max_queries=1, max_pages_per_query=1,
                                        max_requests=1),
                shared_cache=cache, resolve_cache_grant=cache_grant,
                unavailable_mode="LIVE", youtube_query_mode="OWNER_MANUAL_LIVE",
                force_youtube_refresh_once=force_fresh, utcnow=self.clock,
            )

        first_calls = []

        async def first_collect():
            first_calls.append(self.clock())
            return {
                "status": "SUCCESS", "data_mode": "SYNTHETIC",
                "records": [{"video_id": "video-original"}], "record_count": 1,
                "coverage": {"source_fetched_at": NOW.isoformat()},
            }

        normal = registry()
        first = await normal._with_cache(YOUTUBE_SOURCE_ID, None, first_collect)
        self.assertEqual(first["delivery_state"], "FRESH")
        self.assertEqual(len(first_calls), 1)

        self.clock.value += timedelta(minutes=1)
        forced_calls = []

        async def forced_collect():
            forced_calls.append(self.clock())
            return {
                "status": "SUCCESS", "data_mode": "SYNTHETIC",
                "records": [{"video_id": "video-forced-refresh"}], "record_count": 1,
                "coverage": {"source_fetched_at": NOW.isoformat()},
            }

        owner_manual = registry(force_fresh=True)
        forced = await owner_manual._with_cache(
            YOUTUBE_SOURCE_ID, None, forced_collect
        )
        self.assertEqual(forced["delivery_state"], "FRESH")
        self.assertTrue(forced["coverage"]["owner_manual_fresh_test"])
        self.assertEqual(len(forced_calls), 1)

        self.clock.value += timedelta(minutes=1)
        cached = await owner_manual._with_cache(
            YOUTUBE_SOURCE_ID, None, forced_collect
        )
        self.assertEqual(cached["delivery_state"], "CACHED")
        self.assertEqual(cached["records"][0]["video_id"], "video-forced-refresh")
        self.assertEqual(len(forced_calls), 1)

    async def test_expired_checkpoint_can_replay_wikimedia_and_youtube_cache_only(self):
        cache = SharedCache(SqliteTestStore(self.path))

        def cache_grant(source_id, now):
            return CacheGrant(
                source_id, "cache-only-recovery-v1", now + timedelta(days=7),
                24 * 3600 if source_id == WIKIMEDIA_SOURCE_ID else 8 * 3600,
                7 * 24 * 3600, 7 * 24 * 3600, allowed=True,
            )

        def cache_scope(source_id):
            capability = source_capability(source_id)
            query_scope = DEFAULT_QUERY_PLAN.fingerprint
            if source_id == WIKIMEDIA_SOURCE_ID:
                query_scope += f":{NOW.date().isoformat()}"
            return CacheScope(
                source_id=source_id,
                provider_id=capability.provider_id,
                query=query_scope,
                plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
                market=DEFAULT_QUERY_PLAN.market,
                language=DEFAULT_QUERY_PLAN.language or "all",
                window=DEFAULT_QUERY_PLAN.time_filter_rule,
                parser_version=f"source-v{capability.capability_version}",
                policy_version="cache-only-recovery-v1",
            )

        for source_id in (WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID):
            payload = {
                "status": "SUCCESS" if source_id == WIKIMEDIA_SOURCE_ID else "PARTIAL",
                "data_mode": "LIVE",
                "delivery_state": "FRESH",
                "records": [{"source_id": source_id, "keyword": "cached evidence"}],
                "record_count": 1,
                "provider_request_count": 1,
                "coverage": {"provider_request_count": 1,
                             "owner_fresh_acceptance": True},
            }
            cache.put(cache_scope(source_id), grant=cache_grant(source_id, NOW),
                      origin="LIVE", payload=payload, fetched_at=NOW, now=NOW)

        handlers = D5SourceHandlers(
            query_collectors={GOOGLE_SOURCE_ID: None, BING_SOURCE_ID: None,
                              YOUTUBE_SOURCE_ID: None},
            wikimedia=None,
            plan=DEFAULT_QUERY_PLAN,
            limits=CollectionLimits(max_queries=1, max_pages_per_query=1,
                                    max_requests=1),
            observation_date=lambda: NOW.date(),
            shared_cache=cache,
            resolve_cache_grant=cache_grant,
            unavailable_mode="LIVE",
            utcnow=self.clock,
        )
        self.clock.value = NOW + timedelta(hours=10)

        wikimedia = handlers.read_cached_source_result(WIKIMEDIA_SOURCE_ID)
        youtube = handlers.read_cached_source_result(YOUTUBE_SOURCE_ID)

        self.assertEqual(wikimedia["status"], "SUCCESS")
        self.assertEqual(wikimedia["delivery_state"], "CACHED")
        self.assertEqual(wikimedia["records"][0]["source_id"], WIKIMEDIA_SOURCE_ID)
        self.assertEqual(wikimedia["provider_request_count"], 0)
        self.assertEqual(wikimedia["coverage"]["cached_provider_request_count"], 1)
        self.assertNotIn("owner_fresh_acceptance", wikimedia["coverage"])
        self.assertEqual(youtube["status"], "PARTIAL")
        self.assertEqual(youtube["delivery_state"], "STALE_CACHE")
        self.assertEqual(youtube["provider_request_count"], 0)
        self.assertEqual(youtube["coverage"]["cached_provider_request_count"], 1)

    async def test_d6_acceptance_refreshes_youtube_once_without_bypassing_provider_gate(self):
        cache = SharedCache(SqliteTestStore(self.path))
        current = [NOW]

        def cache_grant(source_id, now):
            return CacheGrant(
                source_id, "D6_YOUTUBE_ACCEPTANCE_CACHE_V1", now + timedelta(days=1),
                3600, 7 * 24 * 3600, 7 * 24 * 3600, allowed=True,
            )

        capability = source_capability(YOUTUBE_SOURCE_ID)
        old_fetched_at = current[0] - timedelta(minutes=1)
        scope = CacheScope(
            source_id=YOUTUBE_SOURCE_ID,
            provider_id=capability.provider_id,
            query=DEFAULT_QUERY_PLAN.fingerprint,
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            market=DEFAULT_QUERY_PLAN.market,
            language=DEFAULT_QUERY_PLAN.language or "all",
            window=DEFAULT_QUERY_PLAN.time_filter_rule,
            parser_version=f"source-v{capability.capability_version}",
            policy_version="D6_YOUTUBE_ACCEPTANCE_CACHE_V1",
        )
        cache.put(
            scope, grant=cache_grant(YOUTUBE_SOURCE_ID, current[0]), origin="LIVE",
            payload={"status": "SUCCESS", "data_mode": "LIVE", "delivery_state": "FRESH",
                     "records": [{"provider_video_id": "old-video"}], "record_count": 1,
                     "provider_request_count": 1,
                     "coverage": {"provider_request_count": 1,
                                  "source_fetched_at": old_fetched_at.isoformat()}},
            fetched_at=old_fetched_at, now=current[0],
        )
        batch_decisions = []
        batch_blocked = [False]

        def resolve_batch(source_id, now, *, owner_fresh_acceptance=False,
                          acceptance_job_id=None):
            batch_decisions.append((source_id, owner_fresh_acceptance, acceptance_job_id))
            if batch_blocked[0]:
                return {"allowed": False, "error_code": "NOT_YET_ELIGIBLE"}
            return {"allowed": True, "quota_remaining": 5}

        registry = D5SourceHandlers(
            query_collectors={GOOGLE_SOURCE_ID: None, BING_SOURCE_ID: None,
                              YOUTUBE_SOURCE_ID: None},
            wikimedia=None, plan=DEFAULT_QUERY_PLAN,
            limits=CollectionLimits(max_queries=1, max_pages_per_query=1, max_requests=1),
            unavailable_mode="LIVE", shared_cache=cache,
            resolve_cache_grant=cache_grant,
            resolve_provider_batch=resolve_batch,
            utcnow=lambda: current[0],
        )
        calls = []

        async def collect():
            calls.append(current[0])
            return {
                "status": "SUCCESS", "data_mode": "LIVE", "delivery_state": "FRESH",
                "records": [{"provider_video_id": "fresh-video"}], "record_count": 1,
                "provider_request_count": 1,
                "coverage": {"provider_request_count": 1,
                             "source_fetched_at": current[0].isoformat()},
            }

        all_sources = sorted(SOURCES)

        def context(job_id, metadata):
            return SourceWorkContext(
                job_id, "test-worker", 1, YOUTUBE_SOURCE_ID,
                DEFAULT_QUERY_PLAN.fingerprint, current[0] + timedelta(minutes=5),
                None, lambda _value: 1, lambda: "ACTIVE", request_metadata=metadata,
            )

        first = await registry._with_cache(YOUTUBE_SOURCE_ID, context("1" * 32, {
            "sources": all_sources,
            "owner_fresh_acceptance": OWNER_FRESH_ACCEPTANCE_MARKER,
        }), collect)
        self.assertEqual(first["delivery_state"], "FRESH")
        self.assertEqual(first["records"][0]["provider_video_id"], "fresh-video")
        self.assertTrue(first["coverage"]["owner_fresh_acceptance"])
        self.assertEqual(first["provider_request_count"], 1)
        self.assertEqual(batch_decisions, [(YOUTUBE_SOURCE_ID, True, "1" * 32)])

        current[0] += timedelta(minutes=1)
        second = await registry._with_cache(
            YOUTUBE_SOURCE_ID, context("2" * 32, {
                "sources": all_sources,
                "manual_source_refresh": MANUAL_SOURCE_REFRESH_MARKER,
            }), collect,
        )
        self.assertEqual(second["delivery_state"], "CACHED")
        self.assertEqual(second["records"][0]["provider_video_id"], "fresh-video")
        self.assertEqual(second["provider_request_count"], 0)
        self.assertEqual(second["coverage"]["provider_request_count"], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(batch_decisions), 1)

        batch_blocked[0] = True
        current[0] += timedelta(minutes=1)
        third = await registry._with_cache(YOUTUBE_SOURCE_ID, context("3" * 32, {
            "sources": all_sources,
            "owner_fresh_acceptance": OWNER_FRESH_ACCEPTANCE_MARKER,
        }), collect)
        self.assertEqual(third["delivery_state"], "CACHED")
        self.assertEqual(third["records"][0]["provider_video_id"], "fresh-video")
        self.assertEqual(third["provider_request_count"], 0)
        self.assertEqual(third["coverage"]["refresh_eligible"], False)
        self.assertEqual(third["coverage"]["refresh_error_code"], "NOT_YET_ELIGIBLE")
        self.assertNotIn("owner_fresh_acceptance", third["coverage"])
        self.assertEqual(len(calls), 1)

    def youtube_cache_only_handler(self, cache):
        capability = source_capability(YOUTUBE_SOURCE_ID)
        scope = CacheScope(
            source_id=YOUTUBE_SOURCE_ID,
            provider_id=capability.provider_id,
            query=DEFAULT_QUERY_PLAN.fingerprint,
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            market=DEFAULT_QUERY_PLAN.market,
            language=DEFAULT_QUERY_PLAN.language or "all",
            window=DEFAULT_QUERY_PLAN.time_filter_rule,
            parser_version=f"source-v{capability.capability_version}",
            policy_version="fixture-cache-policy-v1",
        )

        def cache_grant(source_id, now):
            return CacheGrant(source_id, "fixture-cache-policy-v1", now + timedelta(days=1),
                              max_fresh_seconds=10, max_stale_seconds=60,
                              max_retention_seconds=120, allowed=True)

        handlers = D5SourceHandlers(
            query_collectors={GOOGLE_SOURCE_ID: None, BING_SOURCE_ID: None,
                              YOUTUBE_SOURCE_ID: None},
            wikimedia=None, plan=DEFAULT_QUERY_PLAN,
            limits=CollectionLimits(max_queries=1, max_pages_per_query=1, max_requests=1),
            shared_cache=cache, resolve_cache_grant=cache_grant,
            resolve_provider_batch=lambda _source, _now: {
                "allowed": False, "error_code": "NOT_YET_ELIGIBLE",
                "next_eligible_at": "2026-11-17T08:00:00+00:00",
            },
            unavailable_mode="LIVE", utcnow=self.clock,
        ).build()
        return handlers[YOUTUBE_SOURCE_ID], scope, cache_grant

    async def test_youtube_fresh_cache_is_returned_before_schedule_with_zero_requests(self):
        cache = SharedCache(SqliteTestStore(self.path))
        handler, scope, cache_grant = self.youtube_cache_only_handler(cache)
        fetched_at = self.clock() - timedelta(seconds=5)
        cache.put(scope, grant=cache_grant(YOUTUBE_SOURCE_ID, fetched_at), origin="LIVE",
                  payload={"status": "SUCCESS", "data_mode": "LIVE", "records": [],
                           "record_count": 0, "coverage": {
                               "provider_method": "search.list",
                               "provider_request_count": 1,
                               "source_fetched_at": fetched_at.isoformat(),
                           }}, fetched_at=fetched_at, now=fetched_at)

        result = await handler(None)

        self.assertEqual((result["status"], result["data_mode"], result["delivery_state"]),
                         ("SUCCESS", "LIVE", "CACHED"))
        self.assertEqual(result["provider_request_count"], 0)
        self.assertEqual(result["coverage"]["provider_request_count"], 0)
        self.assertEqual(result["coverage"]["cached_provider_request_count"], 1)
        self.assertEqual(result["coverage"]["source_fetched_at"], fetched_at.isoformat())
        self.assertIsNotNone(result["coverage"]["cache_delivery_at"])

    async def test_youtube_stale_cache_is_delivered_outside_schedule(self):
        cache = SharedCache(SqliteTestStore(self.path))
        handler, scope, cache_grant = self.youtube_cache_only_handler(cache)
        fetched_at = self.clock() - timedelta(seconds=30)
        cache.put(scope, grant=cache_grant(YOUTUBE_SOURCE_ID, fetched_at), origin="LIVE",
                  payload={"status": "SUCCESS", "data_mode": "LIVE", "records": [],
                           "record_count": 0, "coverage": {
                               "provider_method": "search.list",
                               "source_fetched_at": fetched_at.isoformat(),
                           }}, fetched_at=fetched_at, now=fetched_at)

        result = await handler(None)

        self.assertEqual((result["status"], result["data_mode"], result["delivery_state"]),
                         ("SUCCESS", "LIVE", "STALE_CACHE"))
        self.assertEqual(result["provider_request_count"], 0)
        self.assertEqual(result["coverage"]["cache_freshness"], "STALE_CACHE")
        self.assertEqual(result["coverage"]["next_eligible_at"],
                         "2026-11-17T08:00:00+00:00")
        self.assertEqual(result["coverage"]["source_fetched_at"], fetched_at.isoformat())

    async def test_youtube_missing_expired_and_invalid_cache_are_distinguished(self):
        cache = SharedCache(SqliteTestStore(self.path))
        handler, scope, cache_grant = self.youtube_cache_only_handler(cache)
        missing = await handler(None)
        self.assertEqual((missing["status"], missing["error_code"]),
                         ("UNAVAILABLE", "NO_CACHE_AVAILABLE"))
        self.assertEqual(missing["coverage"]["provider_request_count"], 0)
        self.assertEqual(missing["coverage"]["next_eligible_at"],
                         "2026-11-17T08:00:00+00:00")

        expired_at = self.clock() - timedelta(minutes=5)
        expiring_grant = CacheGrant(
            YOUTUBE_SOURCE_ID, "fixture-cache-policy-v1", self.clock() + timedelta(days=1),
            max_fresh_seconds=10, max_stale_seconds=20,
            max_retention_seconds=30, allowed=True,
        )
        cache.put(scope, grant=expiring_grant, origin="LIVE",
                  payload={"status": "SUCCESS", "data_mode": "LIVE", "records": [],
                           "record_count": 0}, fetched_at=expired_at, now=expired_at)
        expired = await handler(None)
        self.assertEqual(expired["error_code"], "CACHE_EXPIRED")
        self.assertEqual(expired["coverage"]["cache_reason"], "RETENTION_EXPIRED")

        cache.store.transact(scope.key, lambda record: ({**record, "schema": 999}, None))
        invalid = await handler(None)
        self.assertEqual(invalid["error_code"], "CACHE_INVALID")
        self.assertEqual(invalid["coverage"]["cache_reason"], "INVALID_CACHE_RECORD")

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

    async def test_public_status_keeps_unavailable_source_mode_and_diagnostics(self):
        handlers = {}
        for source in SOURCES:
            if source == YOUTUBE_SOURCE_ID:
                def handler(_context):
                    return {"status": "UNAVAILABLE", "data_mode": "SYNTHETIC",
                            "records": [], "record_count": 0,
                            "delivery_state": "NOT_ELIGIBLE",
                            "provider_request_count": 0,
                            "error_code": "NO_CACHE_AVAILABLE",
                            "coverage": {"provider_request_count": 0,
                                         "next_eligible_at": "2026-11-17T08:00:00+00:00"}}
            else:
                def handler(_context):
                    return self.source_result()
            handler.__trendscout_data_mode__ = "SYNTHETIC"
            handlers[source] = handler
        worker = self.worker(handlers)
        public = DistributedPublicFetch(self.admission, worker,
                                        resolve_grant=self.grant, utcnow=self.clock)
        accepted = public.submit(
            "unavailable-source-diagnostic-key",
            source_ids=[YOUTUBE_SOURCE_ID],
        )
        await public.run_once("unavailable-source-worker")

        result = public.status(accepted["job_id"], include_records=True)
        youtube = result["sources"][0]
        self.assertEqual(result["selected_sources"], [YOUTUBE_SOURCE_ID])
        self.assertEqual(youtube["status"], "UNAVAILABLE")
        self.assertEqual(youtube["data_mode"], "SYNTHETIC")
        self.assertEqual(youtube["delivery_state"], "NOT_ELIGIBLE")
        self.assertEqual(youtube["provider_request_count"], 0)
        self.assertEqual(youtube["error_code"], "NO_CACHE_AVAILABLE")
        self.assertEqual(youtube["coverage"]["next_eligible_at"],
                         "2026-11-17T08:00:00+00:00")

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

    async def test_shared_history_preserves_recovery_request_scope(self):
        handlers = {}
        for source in SOURCES:
            def handler(_context):
                return self.source_result()
            handler.__trendscout_data_mode__ = "SYNTHETIC"
            handlers[source] = handler
        worker = self.worker(handlers)
        public = DistributedPublicFetch(self.admission, worker,
                                        resolve_grant=self.grant, utcnow=self.clock)
        latest = public.submit("history-latest-recovery-scope", source_ids=[GOOGLE_SOURCE_ID])
        historical = self.admission.admit(
            "history-wikimedia-recovery-scope", now=self.clock(),
            request_metadata={"kind": "WIKIMEDIA_HISTORY", "history_days": 2},
        )

        rows = {item["fetch_id"]: item for item in public.history(limit=20)}

        self.assertEqual(rows[latest["job_id"]]["request_kind"], "LATEST")
        self.assertEqual(rows[latest["job_id"]]["sources"], [GOOGLE_SOURCE_ID])
        self.assertEqual(rows[historical["job_id"]]["request_kind"], "WIKIMEDIA_HISTORY")
        self.assertEqual(rows[historical["job_id"]]["history_days"], 2)

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
