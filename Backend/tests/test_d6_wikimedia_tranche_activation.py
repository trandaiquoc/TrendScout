"""Offline-only tranche integration through the public API and Wikimedia worker."""

import copy
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from d6_wikimedia_synthetic_support import fixture_payload
from test_d6_authorization_tranches import make_legacy

from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import AdmissionDenied, AdmissionPolicy, PoolLimit, PublicAdmission
from app.jobs.source_checkpoints import CheckpointGrant
from app.local_ledger import STATE_KEY
from app.main import create_app
from app.orchestration.d6_wikimedia_activation import (
    FutureWikimediaLiveActivation,
    SameLedgerRuntimeAdmissionStore,
    TrancheAwareGuardedAttemptRunner,
    require_offline_activation,
)
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.settings import Settings
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)
from app.sources.guarded_collector import CollectionLimits
from app.sources.live_gateway import SourceApproval
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.wikimedia.live_gateway import WikimediaTopPageGateway
from app.storage.d6_authorization_tranches import (
    AuthorizationTranche,
    SameLedgerTranches,
    read_only_ledger_compatibility_preflight,
)
from app.storage.fake import FakeStorage
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.workers.d5_source_handlers import D5SourceHandlers

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
NOW_TEXT = NOW.isoformat()
HOST = "d6-2-6-fixture-host"
AUTH_ID = "d6-2-6-offline-fixture"
APPROVAL_ID = "D6.2.6-OFFLINE-FIXTURE-ONLY"
OBSERVATION_DATE = date(2026, 11, 10)
SOURCE_IDS = frozenset({WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID})


class ApprovalFixture:
    enabled = True

    def current(self, source_id):
        if source_id != WIKIMEDIA_SOURCE_ID or not self.enabled:
            return None
        return SourceApproval(
            source_id=source_id,
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            expires_at=NOW + timedelta(minutes=5),
            policy_version="d6-2-6-fixture-v1",
            approval_id=APPROVAL_ID,
            source_enabled=True, global_enabled=True, usage_approved=True,
            retention_approved=True, quota_approved=True, cost_approved=True,
            security_approved=True, distributed_controls_verified=True,
            egress_controls_verified=True, telemetry_controls_verified=True,
            scope="PRODUCTION",
        )


class BrokenBody(httpx.AsyncByteStream):
    def __init__(self, error):
        self.error = error

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise self.error

    async def aclose(self):
        return None


class OfflineTrancheActivationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="d6-2-6-offline-")
        self.root = Path(self.directory.name)
        self.scenario = "success"
        self.calls = 0
        self.app = self.build_app()
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        await self.app.state.d6_2_6_gateway.aclose()
        self.directory.cleanup()

    async def reset_fixture(self, scenario):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.root = self.root / scenario
        self.root.mkdir()
        self.calls = 0
        self.app = self.build_app(scenario=scenario)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1"
        )

    def build_app(self, *, scenario="success", auth_status="ACTIVE", auth_expiry=None,
                  auth_country="US", transport=None, existing=False):
        self.scenario = scenario
        store = LocalSQLiteAtomicStateStore(
            self.root / "ledger.sqlite", environment_profile="test", data_mode="SYNTHETIC"
        )
        tranches = SameLedgerTranches(store, designated_host_id=HOST, host_id=HOST)
        if not existing:
            store.transact(STATE_KEY, lambda _current: (make_legacy(), None))
            tranches.migrate_legacy(migrated_at_utc=NOW_TEXT)
            policy = self.policy()
            digest_admission = PublicAdmission(
                SameLedgerRuntimeAdmissionStore(store), policy, state_key=STATE_KEY
            )
            auth = AuthorizationTranche(
                authorization_id=AUTH_ID, source_id=WIKIMEDIA_SOURCE_ID,
                approval_id=APPROVAL_ID, approver="Synthetic fixture only",
                valid_from_utc=(NOW - timedelta(hours=1)).isoformat(),
                expires_at_utc=(auth_expiry or NOW + timedelta(hours=1)).isoformat(),
                policy_version="d6-2-6-fixture-v1", policy_digest=digest_admission.policy_digest,
                query_plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
                endpoint_scope="top-per-country", country_scope=auth_country,
                access_scope="all-access", observation_date=OBSERVATION_DATE.isoformat(),
                maximum_requests=2, maximum_attempts=2, maximum_job_starts=2,
                maximum_cost_units=2, maximum_monetary_fee="0", currency="USD",
                processing_permission="FIXTURE_ONLY", derived_metrics_permission="DENIED",
                retention_policy="FIXTURE_ONLY", raw_body_policy="DO_NOT_STORE",
                security_controls="MOCK_TRANSPORT_ONLY", rollback_owner="Fixture maintainer",
                authorization_status=auth_status,
            ).validate()
            store.transact(STATE_KEY, lambda state: (
                {**state, "authorizations": [auth]}, None
            ))
        policy = self.policy()
        runtime_store = SameLedgerRuntimeAdmissionStore(store)
        admission = PublicAdmission(runtime_store, policy, state_key=STATE_KEY)
        if transport is None:
            transport = httpx.MockTransport(self.respond)
        self.assertTrue(require_offline_activation(transport, tranches))
        approvals = ApprovalFixture()
        runner = TrancheAwareGuardedAttemptRunner(
            admission, tranches, authorization_id=AUTH_ID, approvals=approvals,
            endpoint_scope="top-per-country", country_scope="US", access_scope="all-access",
            observation_date=OBSERVATION_DATE.isoformat(), clock=lambda: NOW,
        )
        gateway = WikimediaTopPageGateway.offline_fixture(
            approvals, runner,
            user_agent="TrendScout offline acceptance/1.0 (https://example.invalid/contact)",
            transport=transport, clock=lambda: NOW,
        )
        handlers = D5SourceHandlers(
            query_collectors={GOOGLE_SOURCE_ID: None, BING_SOURCE_ID: None, YOUTUBE_SOURCE_ID: None},
            wikimedia=gateway, plan=DEFAULT_QUERY_PLAN,
            limits=CollectionLimits(max_requests=1, max_results=50),
            observation_date=lambda: OBSERVATION_DATE, unavailable_mode="SYNTHETIC",
            utcnow=lambda: NOW,
        ).build()

        def grant(source_id, now):
            return CheckpointGrant(source_id, "SYNTHETIC", "d6-2-6-checkpoint-fixture",
                                   now + timedelta(minutes=5), 300, True)

        worker = DistributedFetchWorker(admission, handlers=handlers,
                                        resolve_grant=grant, utcnow=lambda: NOW)
        facade = DistributedPublicFetch(admission, worker, resolve_grant=grant,
                                        utcnow=lambda: NOW)
        settings = Settings.from_environment({
            "TREND_SCOUT_ENVIRONMENT": "test", "TREND_SCOUT_RUN_MODE": "synthetic",
            "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "true", "TREND_SCOUT_RUN_COOLDOWN_SECONDS": "0",
            "TREND_SCOUT_LOCK_PATH": str(self.root / "fixture.lock"),
        })
        app = create_app(settings=settings, storage=FakeStorage(), distributed_public_fetch=facade)
        app.state.d6_2_6_store = store
        app.state.d6_2_6_tranches = tranches
        app.state.d6_2_6_gateway = gateway
        app.state.d6_2_6_mock = transport
        app.state.d6_2_6_approvals = approvals
        return app

    @staticmethod
    def policy():
        return AdmissionPolicy(
            enabled_sources=SOURCE_IDS,
            pools={
                "wikimedia": PoolLimit(2, 2, 2, 2, 1, max_inflight=1, attempt_timeout_seconds=15),
                "serpapi": PoolLimit(1, 1, 1, 1, 1, max_inflight=1),
                "youtube": PoolLimit(1, 1, 1, 1, 1, max_inflight=1),
            },
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            max_starts_per_window=2, window_seconds=3600, cooldown_seconds=0,
            lease_seconds=30, max_attempts=1, max_job_seconds=120, receipt_seconds=600,
            max_records=200, max_status_polls=100, enabled=True,
        )

    async def respond(self, request):
        self.calls += 1
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.host, "wikimedia.org")
        if self.scenario == "read_error":
            return httpx.Response(200, stream=BrokenBody(httpx.ReadError("fixture read error")))
        if self.scenario == "read_timeout":
            return httpx.Response(200, stream=BrokenBody(httpx.ReadTimeout("fixture timeout")))
        if self.scenario == "malformed":
            return httpx.Response(200, content=b"{")
        payload = fixture_payload()
        if self.scenario == "invalid_scope_date":
            payload["items"][0]["country"] = "CA"
            payload["items"][0]["day"] = "09"
        return httpx.Response(200, json=payload)

    async def submit_and_status(self, key):
        accepted = await self.client.post(
            "/api/v1/fetches", json={}, headers={"Origin": "http://127.0.0.1", "Idempotency-Key": key}
        )
        if accepted.status_code != 202:
            return accepted, None
        fetch_id = accepted.json()["fetch_id"]
        status = await self.client.get(
            f"/api/v1/fetches/{fetch_id}", headers={"Origin": "http://127.0.0.1"}
        )
        return accepted, status

    def state(self):
        return self.app.state.d6_2_6_store.transact(STATE_KEY, lambda state: (state, copy.deepcopy(state)))

    async def test_case_a_api_worker_parser_checkpoint_discovery_replay_and_budget(self):
        accepted, status = await self.submit_and_status("d6-2-6-fetch-first-0001")
        self.assertEqual(accepted.status_code, 202)
        body = status.json()
        self.assertTrue(body["terminal"])
        wiki = next(item for item in body["sources"] if item["source_id"] == WIKIMEDIA_SOURCE_ID)
        self.assertEqual(wiki["data_mode"], "SYNTHETIC")
        self.assertGreater(wiki["record_count"], 0)
        candidate = body["discovery"]["candidates"][0]
        evidence = candidate["evidence"][0]
        self.assertEqual(evidence["source_id"], WIKIMEDIA_SOURCE_ID)
        self.assertEqual(evidence["data_mode"], "SYNTHETIC")
        self.assertIn("observation_date", wiki["records"][0])
        self.assertEqual(self.calls, 1)
        replay, _ = await self.submit_and_status("d6-2-6-fetch-first-0001")
        self.assertEqual(replay.headers["idempotent-replay"], "true")
        self.assertEqual(self.calls, 1)
        next_action, next_status = await self.submit_and_status("d6-2-6-fetch-second-0002")
        self.assertEqual(next_action.status_code, 202)
        self.assertTrue(next_status.json()["terminal"])
        self.assertEqual(self.calls, 2)
        third, _ = await self.submit_and_status("d6-2-6-fetch-third-0003")
        self.assertEqual(third.status_code, 429)
        self.assertEqual(self.calls, 2)
        state = self.state()
        self.assertEqual(state["legacy_tranche"]["consumed_requests"], 1)
        self.assertEqual(len(state["reservations"]), 2)
        self.assertEqual(len(state["settlements"]), 2)
        self.assertEqual(1 + len(state["reservations"]), 3)
        self.assertEqual(len(state["legacy_state"]["jobs"]), 1)

    async def test_cases_b_c_d_e_failures_consume_request_without_retry(self):
        expected = {
            "read_error": "UPSTREAM_ERROR", "read_timeout": "UPSTREAM_TIMEOUT",
            "malformed": "MALFORMED_RESPONSE", "invalid_scope_date": "MALFORMED_RESPONSE",
        }
        for scenario, error_code in expected.items():
            with self.subTest(scenario=scenario):
                await self.reset_fixture(scenario)
                accepted, status = await self.submit_and_status(f"d6-2-6-failure-{scenario}")
                self.assertEqual(accepted.status_code, 202)
                self.assertTrue(status.json()["terminal"])
                wiki = next(item for item in status.json()["sources"]
                            if item["source_id"] == WIKIMEDIA_SOURCE_ID)
                self.assertEqual(wiki.get("error_code"), error_code)
                self.assertEqual(self.calls, 1, "no automatic retry or second request")
                self.assertEqual(len(self.state()["reservations"]), 1)

    async def test_case_k_expired_or_scope_mismatch_authorization_fails_before_dispatch(self):
        state = self.state()
        auth = state["authorizations"][0]
        auth["expires_at_utc"] = (NOW - timedelta(seconds=1)).isoformat()
        self.app.state.d6_2_6_store.transact(
            STATE_KEY, lambda envelope: ({**envelope, "authorizations": [auth]}, None)
        )
        accepted, status = await self.submit_and_status("d6-2-6-expired-auth-0001")
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.state()["reservations"], [])
        self.assertTrue(status.json()["terminal"])

    async def test_case_k_missing_authorization_fails_before_dispatch(self):
        self.app.state.d6_2_6_store.transact(
            STATE_KEY, lambda envelope: ({**envelope, "authorizations": []}, None)
        )
        accepted, status = await self.submit_and_status("d6-2-6-missing-auth-0001")
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.state()["reservations"], [])
        self.assertTrue(status.json()["terminal"])

    async def test_case_k_wrong_authorized_country_fails_before_dispatch(self):
        state = self.state()
        auth = state["authorizations"][0]
        auth["country_scope"] = "CA"
        self.app.state.d6_2_6_store.transact(
            STATE_KEY, lambda envelope: ({**envelope, "authorizations": [auth]}, None)
        )
        accepted, status = await self.submit_and_status("d6-2-6-wrong-scope-0001")
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.state()["reservations"], [])
        self.assertTrue(status.json()["terminal"])

    async def test_case_k_source_approval_policy_version_must_match_tranche(self):
        state = self.state()
        auth = state["authorizations"][0]
        auth["policy_version"] = "different-fixture-policy"
        self.app.state.d6_2_6_store.transact(
            STATE_KEY, lambda envelope: ({**envelope, "authorizations": [auth]}, None)
        )
        accepted, status = await self.submit_and_status("d6-2-6-wrong-policy-0001")
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.state()["reservations"], [])
        self.assertTrue(status.json()["terminal"])

    async def test_case_k_runtime_source_approval_revocation_stops_before_dispatch(self):
        self.app.state.d6_2_6_approvals.enabled = False
        accepted, status = await self.submit_and_status("d6-2-6-source-revoked-0001")
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.state()["reservations"], [])
        self.assertTrue(status.json()["terminal"])

    async def test_cases_f_g_j_duplicate_reservation_restart_and_recovery_are_conservative(self):
        accepted, status = await self.submit_and_status("d6-2-6-restart-0001")
        self.assertEqual(status.status_code, 200)
        original_id = accepted.json()["fetch_id"]
        self.assertEqual(self.calls, 1)
        state_before = self.state()
        store_path = self.app.state.d6_2_6_store.path
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.app = self.build_app(existing=True)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="http://127.0.0.1")
        self.assertEqual(self.app.state.d6_2_6_store.path, store_path)
        replay, status = await self.submit_and_status("d6-2-6-restart-0001")
        self.assertEqual(replay.json()["fetch_id"], original_id)
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.state()["reservations"]), len(state_before["reservations"]))

    async def test_cases_f_g_duplicate_attempt_and_crash_after_tranche_reservation_send_nothing(self):
        accepted, status = await self.submit_and_status("d6-2-6-attempt-duplicate-0001")
        self.assertTrue(status.json()["terminal"])
        self.assertEqual(self.calls, 1)
        reservation = self.state()["reservations"][0]
        runner = self.app.state.d6_2_6_gateway.runner
        from app.workers.guarded_attempt import AttemptOutcome

        async def should_not_send():
            self.fail("duplicate tranche attempt reached provider dispatch")

        replay = await runner.run(
            job_id=reservation["job_id"], owner="replay-worker", fence=1,
            source_id=WIKIMEDIA_SOURCE_ID, attempt_id=reservation["attempt_id"],
            timeout_seconds=10, request=should_not_send,
        )
        self.assertIsInstance(replay, AttemptOutcome)
        self.assertEqual(replay.status, "ALREADY_RESERVED")
        self.assertEqual(self.calls, 1)

        # Simulate process death after the first atomic write and before the
        # PublicAdmission handoff. Reopening the same ledger cannot dispatch it.
        auth = self.state()["authorizations"][0]
        crash_attempt = "f" * 64
        crash_job = "e" * 32
        result = self.app.state.d6_2_6_tranches.reserve_attempt(
            authorization_id=AUTH_ID, approval_id=APPROVAL_ID,
            source_id=WIKIMEDIA_SOURCE_ID, policy_version=auth["policy_version"],
            policy_digest=auth["policy_digest"],
            query_plan_fingerprint=auth["query_plan_fingerprint"],
            endpoint_scope=auth["endpoint_scope"], country_scope=auth["country_scope"],
            access_scope=auth["access_scope"], observation_date=auth["observation_date"],
            job_id=crash_job, attempt_id=crash_attempt,
            idempotency_key=f"d6266:{AUTH_ID}:{crash_attempt}",
            cost_units=1, monetary_fee="0", currency="USD", now_utc=NOW_TEXT,
        )
        self.assertEqual(result, "NEW")
        duplicate = await self.app.state.d6_2_6_gateway.runner.run(
            job_id=crash_job, owner="recovery-worker", fence=1,
            source_id=WIKIMEDIA_SOURCE_ID, attempt_id=crash_attempt,
            timeout_seconds=10, request=should_not_send,
        )
        self.assertEqual(duplicate.status, "ALREADY_RESERVED")
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.state()["reservations"]), 2)

    async def test_cases_m_disabled_providers_and_default_live_gate(self):
        self.assertEqual(FutureWikimediaLiveActivation().status, "BLOCKED")
        with self.assertRaises(AdmissionDenied):
            FutureWikimediaLiveActivation().require_live_dispatch()
        accepted, status = await self.submit_and_status("d6-2-6-providers-0001")
        self.assertEqual(accepted.status_code, 202)
        sources = {row["source_id"]: row for row in status.json()["sources"]}
        for source_id in (GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID):
            self.assertEqual(sources[source_id]["status"], "UNAVAILABLE")
        self.assertEqual(self.calls, 1)


class ReadOnlyCompatibilityTests(unittest.TestCase):
    def test_case_n_preflight_v1_and_v2_is_read_only_and_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="d6-2-6-preflight-") as directory:
            path = Path(directory) / "ledger.sqlite"
            store = LocalSQLiteAtomicStateStore(path, environment_profile="test")
            store.transact(STATE_KEY, lambda _old: (make_legacy(), None))
            before = path.read_bytes()
            result = read_only_ledger_compatibility_preflight(
                path, designated_host_id=HOST, host_id=HOST
            )
            self.assertEqual(result["status"], "READ_ONLY_COMPATIBLE")
            self.assertEqual(result["schema"], 1)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(read_only_ledger_compatibility_preflight(
                path, designated_host_id=HOST, host_id="another-host"
            )["reason"], "CANONICAL_EXECUTION_HOST_MISMATCH")
            self.assertEqual(read_only_ledger_compatibility_preflight(
                Path(directory) / "missing.sqlite", designated_host_id=HOST, host_id=HOST
            )["reason"], "LEDGER_UNAVAILABLE")

            v2_path = Path(directory) / "v2-ledger.sqlite"
            v2_store = LocalSQLiteAtomicStateStore(v2_path, environment_profile="test")
            tranches = SameLedgerTranches(v2_store, designated_host_id=HOST, host_id=HOST)
            v2_store.transact(STATE_KEY, lambda _old: (make_legacy(), None))
            tranches.migrate_legacy(migrated_at_utc=NOW_TEXT)
            v2_before = v2_path.read_bytes()
            v2_result = read_only_ledger_compatibility_preflight(
                v2_path, designated_host_id=HOST, host_id=HOST
            )
            self.assertEqual(v2_result["status"], "READ_ONLY_COMPATIBLE")
            self.assertEqual(v2_result["schema"], 2)
            self.assertEqual(v2_path.read_bytes(), v2_before)


if __name__ == "__main__":
    unittest.main()
