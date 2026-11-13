"""Normal LIVE composition contracts verified only with Wikimedia MockTransport."""

import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.discovery.contracts import ProcessingGrant
from app.collections.store import CollectionStore
from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import PublicAdmission
from app.jobs.shared_cache import CacheGrant, SharedCache
from app.jobs.source_checkpoints import CheckpointGrant
from app.main import create_normal_wikimedia_live_app
from app.normal_wikimedia_live_policy import POLICY_VERSION, STATE_KEY, normal_wikimedia_live_policy
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.orchestration.normal_wikimedia_live_composition import _PersistentOneSecondPacer
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
from app.workers.d5_source_handlers import D5SourceHandlers
from app.workers.guarded_attempt import GuardedAttemptRunner

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
USER_AGENT = "TrendScoutTest/1.0 (https://github.com/trandaiquoc/TrendScout)"


def payload(day):
    return {"items": [{"country": "US", "access": "all-access",
        "year": day.strftime("%Y"), "month": day.strftime("%m"), "day": day.strftime("%d"),
        "articles": [{"article": "Coffee", "project": "en.wikipedia",
                      "views_ceil": 100, "rank": 1}]}]}


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
        self.day = (NOW - timedelta(days=2)).date()

        def handle(request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return httpx.Response(404, request=request)
            date_parts = request.url.path.rstrip("/").split("/")[-3:]
            response_day = datetime.strptime("/".join(date_parts), "%Y/%m/%d").date()
            return httpx.Response(200, json=payload(response_day), request=request)

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
            resolve_grant=self.checkpoint_grant, utcnow=lambda: self.now)

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
        with patch("app.collections.store.CollectionStore.local", return_value=isolated_collections):
            async with inner.router.lifespan_context(inner):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
                    base_url="http://127.0.0.1",
                ) as client:
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


if __name__ == "__main__":
    unittest.main()
