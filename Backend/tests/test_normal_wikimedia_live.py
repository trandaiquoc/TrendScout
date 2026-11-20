"""Normal LIVE composition contracts verified only with Wikimedia MockTransport."""

import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.collections.store import CollectionStore
from app.discovery.contracts import ProcessingGrant
from app.discovery.source_results import discover_fetch_results
from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import PublicAdmission
from app.jobs.shared_cache import CacheGrant, SharedCache
from app.jobs.source_checkpoints import CheckpointGrant
from app.main import create_normal_wikimedia_live_app
from app.normal_wikimedia_live_policy import POLICY_VERSION, STATE_KEY, normal_wikimedia_live_policy
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.orchestration.normal_wikimedia_live_composition import _PersistentOneSecondPacer
from app.scheduling.persistent import PersistentScheduleStore
from app.settings import Settings
from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from app.sources.guarded_collector import CollectionLimits
from app.sources.live_gateway import SourceApproval
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.wikimedia.live_gateway import (
    WikimediaGatewayError,
    WikimediaRequest,
    WikimediaTopPageGateway,
)
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.storage.local_sqlite_snapshot_blobs import LocalSQLiteSnapshotBlobs
from app.workers.d5_source_handlers import D5SourceHandlers
from app.workers.guarded_attempt import GuardedAttemptRunner

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
USER_AGENT = "TrendScoutTest/1.0 (https://github.com/trandaiquoc/TrendScout)"


class NoEntityContext:
    def __init__(self, _store, **_kwargs):
        pass

    def enrich(self, _records):
        return {}

    def cached(self, _keyword_ids):
        return {}


def payload(day, count=1):
    return {"items": [{"country": "US", "access": "all-access",
        "year": day.strftime("%Y"), "month": day.strftime("%m"), "day": day.strftime("%d"),
        "articles": [{"article": "Coffee" if count == 1 else f"Live topic {index:04d}",
                      "project": "en.wikipedia",
                      "views_ceil": 100 if count == 1 else 1000 - index,
                      "rank": index + 1} for index in range(count)]}]}


class LiveMockGateway(WikimediaTopPageGateway):
    @property
    def data_mode(self):
        return "LIVE"

    async def fetch_day(self, **kwargs):
        callback = kwargs.get("on_page_receipt")
        if callback is not None:
            kwargs["on_page_receipt"] = lambda page: callback(replace(page, data_mode="LIVE"))
        return replace(await super().fetch_day(**kwargs), data_mode="LIVE")


class NormalWikimediaLiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        root.chmod(0o700)
        self.store = LocalSQLiteAtomicStateStore.for_local_live(root / "state.sqlite")
        self.policy = normal_wikimedia_live_policy()
        self.admission = PublicAdmission(self.store, self.policy, state_key=STATE_KEY)
        self.now = NOW
        self.runner = GuardedAttemptRunner(self.admission, clock=lambda: self.now)
        self.approvals = type("Approvals", (), {"current": lambda _self, source: SourceApproval(
            source, DEFAULT_QUERY_PLAN.fingerprint, NOW + timedelta(hours=1), POLICY_VERSION,
            approval_id=POLICY_VERSION, source_enabled=True, global_enabled=True,
            usage_approved=True, retention_approved=True, quota_approved=True,
            cost_approved=True, security_approved=True, egress_controls_verified=True,
            telemetry_controls_verified=True, scope="LOCAL_NORMAL", local_controls_verified=True,
        ) if source == WIKIMEDIA_SOURCE_ID else None})()
        self.requests = []
        self.response_count = 1
        self.first_response_404 = True
        self.day = (NOW - timedelta(days=2)).date()

        def handle(request):
            self.requests.append(request)
            if self.first_response_404 and len(self.requests) == 1:
                return httpx.Response(404, request=request)
            date_parts = request.url.path.rstrip("/").split("/")[-3:]
            response_day = datetime.strptime("/".join(date_parts), "%Y/%m/%d").date()
            return httpx.Response(200, json=payload(response_day, self.response_count), request=request)

        self.gateway = LiveMockGateway.offline_fixture(
            self.approvals, self.runner, user_agent=USER_AGENT,
            transport=httpx.MockTransport(handle), clock=lambda: self.now,
            before_request=AsyncMock(),
        )
        self.cache = SharedCache(self.store)
        self.cache_grant = lambda source, now: CacheGrant(
            source, POLICY_VERSION, now + timedelta(days=7), 86400, 604800, 604800, True
        ) if source == WIKIMEDIA_SOURCE_ID else None
        self.checkpoint_grant = lambda source, now: CheckpointGrant(
            source, "LIVE", POLICY_VERSION, now + timedelta(minutes=15), 600, True
        ) if source == WIKIMEDIA_SOURCE_ID else None
        self.handler = D5SourceHandlers(
            query_collectors={"google_web_serp": None, "bing_web_serp": None,
                              "youtube_video_search": None},
            wikimedia=self.gateway, plan=DEFAULT_QUERY_PLAN,
            limits=CollectionLimits(max_queries=1, max_pages_per_query=1, max_requests=3,
                max_retries_per_page=0, deadline_seconds=60, max_results=10),
            observation_date=lambda: self.day, shared_cache=self.cache,
            resolve_cache_grant=self.cache_grant, max_wikimedia_date_attempts=3,
            include_wikimedia_transport_metrics=True,
            unavailable_mode="LIVE", utcnow=lambda: self.now,
        ).build()[WIKIMEDIA_SOURCE_ID]
        self.worker = DistributedFetchWorker(self.admission,
            handlers={WIKIMEDIA_SOURCE_ID: self.handler},
            resolve_grant=self.checkpoint_grant, utcnow=lambda: self.now,
            snapshot_blobs=LocalSQLiteSnapshotBlobs(self.store))

    async def asyncTearDown(self):
        await self.gateway.aclose()
        self.directory.cleanup()

    async def fetch(self, key):
        accepted = self.admission.admit(key, now=self.now)
        await self.worker.run_once("test-worker")
        return self.admission.read_job(accepted["job_id"], now=self.now)

    async def test_date_fallback_then_successful_live_cache_preserves_provenance(self):
        first = await self.fetch("normal-wikimedia-fetch-first")
        result = first["source_checkpoints"][WIKIMEDIA_SOURCE_ID]["snapshot"]["result"]
        self.assertEqual(result["data_mode"], "LIVE")
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["coverage"]["http_status"], 200)
        self.assertGreater(result["coverage"]["response_bytes"], 0)
        self.assertEqual(result["coverage"]["rejected"], 0)
        self.assertEqual(result["coverage"]["observation_date"],
                         (self.day - timedelta(days=1)).isoformat())
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.gateway.before_request.await_count, 2)

        self.now += timedelta(seconds=2)
        second = await self.fetch("normal-wikimedia-fetch-cached")
        cached = second["source_checkpoints"][WIKIMEDIA_SOURCE_ID]["snapshot"]["result"]
        self.assertEqual(cached["delivery_state"], "CACHED")
        self.assertEqual(cached["data_mode"], "LIVE")
        self.assertEqual(cached["records"][0]["source"], WIKIMEDIA_SOURCE_ID)
        self.assertEqual(len(self.requests), 2)

    async def test_complete_provider_list_survives_checkpoint_cache_and_discovery(self):
        self.response_count = 1000
        finished = await self.fetch("normal-wikimedia-full-top-list")
        grant = self.checkpoint_grant(WIKIMEDIA_SOURCE_ID, self.now)
        result = self.worker.journal.completed_result(
            finished, source_id=WIKIMEDIA_SOURCE_ID, grant=grant, now=self.now)

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["record_count"], 1000)
        self.assertEqual(len(result["records"]), 1000)
        self.assertEqual(result["coverage"]["provider_returned_records"], 1000)
        self.assertEqual(result["coverage"]["accepted_records"], 1000)
        self.assertEqual(result["coverage"]["rejected_records"], 0)
        self.assertEqual(result["coverage"]["explicitly_omitted_records"], 0)

        processing_grant = ProcessingGrant(WIKIMEDIA_SOURCE_ID, True, False,
            self.now + timedelta(minutes=15), POLICY_VERSION)
        discovered = discover_fetch_results([{"source_id": WIKIMEDIA_SOURCE_ID, **result}],
            now=self.now, processing_grants=(processing_grant,), empty_mode="LIVE")
        self.assertGreater(discovered["candidate_count"], 0)
        self.assertEqual(discovered["rejected_record_count"], 0)
        self.assertEqual(discovered["source_coverage"][0]["provider_returned_records"], 1000)

        restarted_journal = type(self.worker.journal)(self.admission,
            snapshot_blobs=LocalSQLiteSnapshotBlobs(self.store))
        restored = restarted_journal.completed_result(
            finished, source_id=WIKIMEDIA_SOURCE_ID, grant=grant, now=self.now)
        self.assertEqual(len(restored["records"]), 1000)

        self.now += timedelta(seconds=2)
        cached_job = await self.fetch("normal-wikimedia-full-top-list-cache")
        cached = restarted_journal.completed_result(
            cached_job, source_id=WIKIMEDIA_SOURCE_ID,
            grant=self.checkpoint_grant(WIKIMEDIA_SOURCE_ID, self.now), now=self.now)
        self.assertEqual(cached["delivery_state"], "CACHED")
        self.assertEqual(len(cached["records"]), 1000)
        self.assertEqual(len(self.requests), 2)

    async def test_public_fetch_restores_large_terminal_result_from_worker_blob_store(self):
        self.response_count = 1000
        job = await self.fetch("normal-wikimedia-public-large-result")

        def processing_grants(now):
            return (ProcessingGrant(WIKIMEDIA_SOURCE_ID, True, False,
                                    now + timedelta(minutes=15), POLICY_VERSION),)

        facade = DistributedPublicFetch(
            self.admission, self.worker, resolve_grant=self.checkpoint_grant,
            utcnow=lambda: self.now, normal_wikimedia_live=True,
            resolve_processing_grants=processing_grants,
        )
        status = facade.status(job["job_id"], include_records=True)

        self.assertEqual(facade.journal, self.worker.journal)
        self.assertEqual(status["data_mode"], "LIVE")
        self.assertEqual(status["sources"][0]["status"], "SUCCESS")
        self.assertEqual(status["sources"][0]["record_count"], 1000)
        self.assertEqual(len(status["sources"][0]["records"]), 1000)
        self.assertGreater(status["discovery"]["candidate_count"], 0)

    async def test_completed_fetch_result_survives_worker_deadline_until_receipt_expiry(self):
        job = await self.fetch("normal-wikimedia-result-outlives-worker")

        def processing_grants(now):
            return (ProcessingGrant(WIKIMEDIA_SOURCE_ID, True, False,
                                    now + timedelta(minutes=15), POLICY_VERSION),)

        facade = DistributedPublicFetch(
            self.admission, self.worker, resolve_grant=self.checkpoint_grant,
            utcnow=lambda: self.now, normal_wikimedia_live=True,
            resolve_processing_grants=processing_grants,
        )
        self.now += timedelta(seconds=self.policy.max_job_seconds + 1)
        status = facade.status(job["job_id"])

        self.assertTrue(status["terminal"])
        self.assertEqual(status["data_mode"], "LIVE")
        self.assertEqual(status["sources"][0]["status"], "SUCCESS")
        self.assertGreater(status["sources"][0]["record_count"], 0)

    async def test_explicit_two_day_history_preserves_complete_lists_across_restart(self):
        self.response_count = 1000
        self.first_response_404 = False
        accepted = self.admission.admit(
            "normal-wikimedia-history-two-days",
            now=self.now,
            request_metadata={"kind": "WIKIMEDIA_HISTORY", "history_days": 2},
        )
        finished = await self.worker.run_once("history-worker")
        self.assertEqual(finished["job_id"], accepted["job_id"])
        job = self.admission.read_job(accepted["job_id"], now=self.now)
        grant = self.checkpoint_grant(WIKIMEDIA_SOURCE_ID, self.now)
        result = self.worker.journal.completed_result(
            job, source_id=WIKIMEDIA_SOURCE_ID, grant=grant, now=self.now)

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["data_mode"], "LIVE")
        self.assertEqual(result["record_count"], 2000)
        self.assertEqual(len(result["records"]), 2000)
        self.assertEqual(result["coverage"]["provider_returned_records"], 2000)
        self.assertEqual(result["coverage"]["accepted_records"], 2000)
        self.assertEqual(result["coverage"]["rejected_records"], 0)
        self.assertEqual(result["coverage"]["explicitly_omitted_records"], 0)
        observed_days = sorted({row["observation_date"] for row in result["records"]})
        self.assertEqual(observed_days, [
            (self.day - timedelta(days=1)).isoformat(), self.day.isoformat(),
        ])
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all("wikimedia.org" in request.url.host for request in self.requests))

        restarted = type(self.worker.journal)(self.admission,
            snapshot_blobs=LocalSQLiteSnapshotBlobs(self.store))
        restored = restarted.completed_result(job, source_id=WIKIMEDIA_SOURCE_ID,
            grant=grant, now=self.now)
        self.assertEqual(len(restored["records"]), 2000)

        self.now += timedelta(seconds=2)
        cached_job = self.admission.admit(
            "normal-wikimedia-history-two-days-cached", now=self.now,
            request_metadata={"kind": "WIKIMEDIA_HISTORY", "history_days": 2})
        await self.worker.run_once("history-worker")
        cached_job = self.admission.read_job(cached_job["job_id"], now=self.now)
        cached = restarted.completed_result(cached_job, source_id=WIKIMEDIA_SOURCE_ID,
            grant=self.checkpoint_grant(WIKIMEDIA_SOURCE_ID, self.now), now=self.now)
        self.assertEqual(cached["delivery_state"], "CACHED")
        self.assertEqual(cached["record_count"], 2000)
        self.assertEqual(len(self.requests), 2)

    async def test_history_persists_each_observation_before_semantic_model_matching(self):
        from app.collections.service import CollectionService

        self.first_response_404 = False
        accepted = self.admission.admit(
            "normal-wikimedia-history-observation-persistence", now=self.now,
            request_metadata={"kind": "WIKIMEDIA_HISTORY", "history_days": 2})
        await self.worker.run_once("history-worker")
        job = self.admission.read_job(accepted["job_id"], now=self.now)
        result = self.worker.journal.completed_result(job, source_id=WIKIMEDIA_SOURCE_ID,
            grant=self.checkpoint_grant(WIKIMEDIA_SOURCE_ID, self.now), now=self.now)
        local_store = CollectionStore(Path(self.directory.name) / "history-collections.sqlite")
        with patch.object(CollectionService, "_embed_cached",
                          side_effect=RuntimeError("offline model intentionally unavailable")):
            await CollectionService(local_store, NoEntityContext(local_store)).process_fetch(
                accepted["job_id"], [{"source_id": WIKIMEDIA_SOURCE_ID, **result}])
        with local_store.connect() as db:
            rows = db.execute("SELECT observation_date,value,snapshot_status FROM algorithm_observations "
                "ORDER BY observation_date").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual([row[0] for row in rows], [
                (self.day - timedelta(days=1)).isoformat(), self.day.isoformat(),
            ])
            self.assertTrue(all(row[1] == 100 for row in rows))
            self.assertTrue(all(row[2] == "SUCCESS" for row in rows))
            snapshot = db.execute("SELECT data_mode,observation_start,observation_end "
                "FROM dataset_snapshots WHERE dataset_id=?", (accepted["job_id"],)).fetchone()
            self.assertEqual(tuple(snapshot), ("LIVE", (self.day - timedelta(days=1)).isoformat(),
                                                self.day.isoformat()))

    async def test_live_result_replays_after_model_failure_without_snapshot(self):
        from app.collections.service import CollectionService

        accepted = self.admission.admit("normal-wikimedia-live-replay-after-model-failure",
                                        now=self.now)
        await self.worker.run_once("replay-worker")
        job = self.admission.read_job(accepted["job_id"], now=self.now)
        result = self.worker.journal.completed_result(job, source_id=WIKIMEDIA_SOURCE_ID,
            grant=self.checkpoint_grant(WIKIMEDIA_SOURCE_ID, self.now), now=self.now)
        local_store = CollectionStore(Path(self.directory.name) / "retry-collections.sqlite",
                                      data_mode_scope="LIVE")
        with local_store.connect() as db:
            db.execute("INSERT INTO fetch_jobs(fetch_id,status,error_code,updated_at,stage) "
                       "VALUES(?,'MODEL_UNAVAILABLE','LOCAL_MODEL_UNAVAILABLE',?,'MODEL_UNAVAILABLE')",
                       (accepted["job_id"], self.now.isoformat()))
        with patch.object(CollectionService, "_embed_cached",
                          side_effect=RuntimeError("offline model intentionally unavailable")):
            replayed = await CollectionService(local_store, NoEntityContext(local_store)).process_fetch(
                accepted["job_id"], [{"source_id": WIKIMEDIA_SOURCE_ID, **result}])

        self.assertEqual(replayed["status"], "MODEL_UNAVAILABLE")
        with local_store.connect() as db:
            snapshot = db.execute("SELECT data_mode,provider_record_count,received_record_count "
                "FROM dataset_snapshots WHERE dataset_id=?", (accepted["job_id"],)).fetchone()
            self.assertEqual(tuple(snapshot), ("LIVE", 1, 1))
            self.assertEqual(db.execute("SELECT count(*) FROM keywords WHERE data_mode='LIVE'").fetchone()[0], 1)
        with patch.object(CollectionService, "_embed_cached",
                          side_effect=lambda texts, _versions: [[1.0, 0.0, 0.0] for _ in texts]):
            retried = await CollectionService(local_store, NoEntityContext(local_store)).reclassify_unassigned(
                accepted["job_id"])
        self.assertEqual(retried["status"], "COMPLETED")
        with local_store.connect() as db:
            self.assertEqual(db.execute("SELECT processing_status FROM dataset_snapshots WHERE dataset_id=?",
                (accepted["job_id"],)).fetchone()[0], "COMPLETED")
            fetch_job = db.execute("SELECT available,skipped FROM fetch_jobs WHERE fetch_id=?",
                (accepted["job_id"],)).fetchone()
            self.assertEqual(tuple(fetch_job), (1, 0))

    async def test_unavailable_live_receipts_are_snapshotted_without_running_model(self):
        from app.collections.service import CollectionService

        fetch_id = "f" * 32
        local_store = CollectionStore(Path(self.directory.name) / "unavailable-collections.sqlite",
                                      data_mode_scope="LIVE")
        with local_store.connect() as db:
            db.execute("INSERT INTO fetch_jobs(fetch_id,status,error_code,updated_at,stage) "
                       "VALUES(?,'MODEL_UNAVAILABLE','LOCAL_MODEL_UNAVAILABLE',?,'MODEL_UNAVAILABLE')",
                       (fetch_id, self.now.isoformat()))

        result = await CollectionService(local_store, NoEntityContext(local_store)).process_fetch(
            fetch_id, [{
                "source_id": WIKIMEDIA_SOURCE_ID,
                "status": "UNAVAILABLE",
                "data_mode": "LIVE",
                "record_count": 0,
                "records": [],
                "error_code": "RESULT_UNAVAILABLE",
            }], cached_context_only=True,
        )

        self.assertEqual(result["status"], "COMPLETED")
        snapshot = local_store.latest_dataset()
        self.assertEqual(snapshot["dataset_id"], fetch_id)
        self.assertEqual(snapshot["data_mode"], "LIVE")
        self.assertEqual(snapshot["status"], "FAILED")
        self.assertEqual(snapshot["received_record_count"], 0)
        with local_store.connect() as db:
            manifest = db.execute("SELECT source_manifest,processing_status FROM dataset_snapshots "
                                  "WHERE dataset_id=?", (fetch_id,)).fetchone()
        self.assertEqual(manifest[1], "COMPLETED")
        self.assertEqual(json.loads(manifest[0])[0]["error_code"], "RESULT_UNAVAILABLE")

    def test_policy_has_repeating_user_budget_without_lifetime_ceiling(self):
        self.assertEqual(self.policy.enabled_sources, frozenset({WIKIMEDIA_SOURCE_ID}))
        self.assertEqual(self.policy.pools["wikimedia"].max_inflight, 1)
        self.assertEqual(self.policy.cooldown_seconds, 1)
        self.assertEqual(self.policy.max_attempts, 3)
        self.assertEqual(STATE_KEY, "normal-wikimedia-live-v1")

    async def test_retry_after_is_recorded_and_blocks_next_user_fetch(self):
        pacer = _PersistentOneSecondPacer(self.store, clock=lambda: self.now)
        await pacer.defer(17)
        self.assertEqual(pacer.retry_after_seconds(), 17)
        self.now += timedelta(seconds=18)
        self.assertEqual(pacer.retry_after_seconds(), 0)

        throttle = AsyncMock()
        gateway = WikimediaTopPageGateway.offline_fixture(
            self.approvals, self.runner, user_agent=USER_AGENT,
            transport=httpx.MockTransport(lambda request: httpx.Response(
                429, headers={"Retry-After": "17"}, request=request)),
            clock=lambda: self.now, on_rate_limited=throttle,
        )
        self.addAsyncCleanup(gateway.aclose)
        accepted = self.admission.admit("normal-wikimedia-rate-test", now=self.now)
        claim = self.admission.claim_next("test-rate-worker", now=self.now)
        with self.assertRaises(WikimediaGatewayError) as caught:
            await gateway.fetch_day(
                request=WikimediaRequest(self.day, DEFAULT_QUERY_PLAN.fingerprint),
                job_id=accepted["job_id"], owner="test-rate-worker", fence=claim["fence"],
                attempt_id="normal-wikimedia-rate-attempt", timeout_seconds=10,
            )
        self.assertEqual(caught.exception.code, "RATE_LIMITED")
        self.assertEqual(throttle.await_args.args[0], 17)

    async def test_local_live_http_flow_returns_real_shaped_discovery_evidence(self):
        def processing_grants(now):
            return (ProcessingGrant(WIKIMEDIA_SOURCE_ID, True, False,
                                    now + timedelta(minutes=15), POLICY_VERSION),)

        facade = DistributedPublicFetch(
            self.admission, self.worker, resolve_grant=self.checkpoint_grant,
            utcnow=lambda: self.now, normal_wikimedia_live=True,
            resolve_processing_grants=processing_grants,
        )

        @asynccontextmanager
        async def composition():
            yield facade

        app = create_normal_wikimedia_live_app(
            enabled=True, composition_factory=composition,
            settings=Settings.from_environment({
                "TREND_SCOUT_ENVIRONMENT": "local", "TREND_SCOUT_RUN_MODE": "live",
                "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false",
            }),
        )
        inner = app.app
        isolated_collections = CollectionStore(self.directory.name + "/collections.sqlite")
        with (patch("app.collections.store.CollectionStore.local", return_value=isolated_collections),
              patch("app.main.WikimediaEntityContextResolver", NoEntityContext)):
            async with inner.router.lifespan_context(inner):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
                    base_url="http://127.0.0.1",
                ) as client:
                    capabilities = await client.get("/api/v1/capabilities")
                    self.assertEqual(capabilities.status_code, 200)
                    wikimedia = next(item for item in capabilities.json()["sources"]
                                     if item["source_id"] == WIKIMEDIA_SOURCE_ID)
                    self.assertEqual(wikimedia["supported_data_modes"], ["LIVE"])
                    response = await client.post("/api/v1/fetches", json={}, headers={
                        "Idempotency-Key": "normal-wikimedia-http-test-0001",
                    })
                    self.assertEqual(response.status_code, 202, response.text)
                    status = await client.get(response.json()["status_url"])
                    self.assertEqual(status.status_code, 200, status.text)
                    body = status.json()
                    self.assertEqual(body["data_mode"], "LIVE")
                    self.assertEqual(len(body["sources"]), 1)
                    evidence = body["discovery"]["candidates"][0]["evidence"][0]
                    self.assertEqual(evidence["article"], "Coffee")
                    self.assertEqual(evidence["project"], "en.wikipedia")
                    self.assertEqual(evidence["provider_rank"], 1)
                    self.assertEqual(evidence["approximate_views"], 100)
                    self.assertEqual(evidence["data_mode"], "LIVE")
                    self.assertEqual(evidence["source_url"].split("/")[:3], ["https:", "", "en.wikipedia.org"])
                    self.now += timedelta(seconds=2)
                    history = await client.post("/api/v1/fetches/history", json={"days": 2}, headers={
                        "Idempotency-Key": "normal-wikimedia-history-http-test-0001",
                    })
                    self.assertEqual(history.status_code, 202, history.text)
                    self.assertEqual(history.json()["request_kind"], "WIKIMEDIA_HISTORY")
                    self.assertEqual(history.json()["history_days"], 2)
                    history_status = await client.get(history.json()["status_url"])
                    self.assertEqual(history_status.status_code, 200, history_status.text)
                    history_source = history_status.json()["sources"][0]
                    self.assertEqual(history_source["data_mode"], "LIVE")
                    self.assertEqual(history_source["coverage"]["history_days_requested"], 2)

    async def test_local_live_scheduler_uses_configured_operator_token_and_stays_disabled(self):
        def processing_grants(now):
            return (ProcessingGrant(WIKIMEDIA_SOURCE_ID, True, False,
                                    now + timedelta(minutes=15), POLICY_VERSION),)

        facade = DistributedPublicFetch(
            self.admission, self.worker, resolve_grant=self.checkpoint_grant,
            utcnow=lambda: self.now, normal_wikimedia_live=True,
            resolve_processing_grants=processing_grants,
        )

        @asynccontextmanager
        async def composition():
            yield facade

        token = "synthetic-local-live-operator-token"
        app = create_normal_wikimedia_live_app(
            enabled=True, composition_factory=composition,
            settings=Settings.from_environment({
                "TREND_SCOUT_ENVIRONMENT": "local", "TREND_SCOUT_RUN_MODE": "live",
                "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false",
                "TREND_SCOUT_OPERATOR_TOKEN": token,
            }),
        )
        inner = app.app
        isolated_collections = CollectionStore(self.directory.name + "/scheduler-collections.sqlite")
        scheduler = PersistentScheduleStore(Path(self.directory.name) / "schedules.sqlite")
        with (patch("app.collections.store.CollectionStore.local", return_value=isolated_collections),
              patch("app.main.WikimediaEntityContextResolver", NoEntityContext)):
            async with inner.router.lifespan_context(inner):
                inner.state.scheduler_store = scheduler
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
                    base_url="http://127.0.0.1",
                ) as client:
                    headers = {"Origin": "http://127.0.0.1", "Authorization": f"Bearer {token}"}
                    created = await client.post("/api/v1/scheduler", headers=headers, json={
                        "name": "Synthetic schedule", "frequency": "DAILY",
                        "timezone": "Asia/Bangkok", "hour": 9, "minute": 10,
                    })
                    self.assertEqual(created.status_code, 201, created.text)
                    schedule_id = created.json()["item"]["id"]
                    edited = await client.patch(f"/api/v1/scheduler/{schedule_id}",
                        headers=headers, json={"name": "Edited synthetic schedule",
                            "frequency": "WEEKLY", "timezone": "America/Los_Angeles",
                            "hour": 14, "minute": 20})
                    self.assertEqual(edited.status_code, 200, edited.text)
                    self.assertEqual(edited.json()["item"]["frequency"], "WEEKLY")
                    enabled = await client.patch(f"/api/v1/scheduler/{schedule_id}",
                        headers=headers, json={"enabled": True})
                    self.assertEqual(enabled.status_code, 409, enabled.text)
                    listing = (await client.get("/api/v1/scheduler")).json()
                    self.assertFalse(listing["enabled"])
                    self.assertFalse(listing["worker_running"])
                    self.assertEqual(len(listing["items"]), 1)
                    self.assertEqual(listing["items"][0]["name"], "Edited synthetic schedule")
                    self.assertFalse(listing["items"][0]["enabled"])


if __name__ == "__main__":
    unittest.main()
