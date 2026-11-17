"""Firestore Emulator integration for shared cache, quota, locks, and recovery."""

import asyncio
import http.client
import json
import subprocess
import sys
import textwrap
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from google.api_core.exceptions import ServiceUnavailable
from google.cloud.firestore_v1.transaction import Transaction

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "Backend"
sys.path.insert(0, str(BACKEND / "tests"))

from app.jobs.public_admission import AdmissionDenied, AdmissionPolicy, PoolLimit, PublicAdmission  # noqa: E402
from app.jobs.shared_cache import CacheGrant, CacheScope  # noqa: E402
from app.orchestration.firestore_shared_live_composition import (  # noqa: E402
    create_firestore_shared_live_composition,
)
from app.sources.catalog import (  # noqa: E402
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
    source_capability,
)
from app.sources.guarded_collector import CollectionLimits  # noqa: E402
from app.sources.query_plan import DEFAULT_QUERY_PLAN  # noqa: E402
from app.storage.firestore_atomic_state import (  # noqa: E402
    CommitOutcomeUnknown,
    FirestoreAtomicStateStore,
)
from app.storage.firestore_emulator import (  # noqa: E402
    SYNTHETIC_PROJECT_ID,
    create_guarded_emulator_client,
)
from app.storage.firestore_shared_cache import create_firestore_shared_cache  # noqa: E402
from app.storage.firestore_snapshot_blobs import FirestoreSnapshotBlobs  # noqa: E402
from app.workers.d5_source_handlers import D5SourceHandlers  # noqa: E402


def admission_policy(*, request_limit=8, max_inflight=8):
    return AdmissionPolicy(
        enabled_sources=frozenset({WIKIMEDIA_SOURCE_ID}),
        pools={
            "wikimedia": PoolLimit(
                request_limit, request_limit, request_limit, request_limit, 1,
                max_inflight=max_inflight, attempt_timeout_seconds=60,
            ),
        },
        plan_fingerprint="a" * 64,
        max_starts_per_window=16,
        cooldown_seconds=0,
        lease_seconds=60,
        max_attempts=3,
        max_job_seconds=300,
        receipt_seconds=3600,
        max_records=128,
        enabled=True,
    )


class FirestoreSharedRuntimeEmulatorTests(unittest.TestCase):
    """Each test uses only the guarded synthetic project and unique documents."""

    def setUp(self):
        self.clients = []
        self.state_keys = []
        self.blob_digests = set()
        self.suffix = uuid.uuid4().hex
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.client = self.new_client()

    def tearDown(self):
        if self.clients:
            client = self.clients[0]
            for state_key in self.state_keys:
                client.collection("_ts_d5_coordination").document(state_key).delete()
            for digest in self.blob_digests:
                manifest = client.collection("_ts_d5_cache_blobs").document(digest)
                for chunk in manifest.collection("chunks").stream():
                    chunk.reference.delete()
                manifest.delete()
        for client in self.clients:
            client.close()

    def new_client(self):
        client = create_guarded_emulator_client()
        self.clients.append(client)
        return client

    def close_client(self, client):
        client.close()
        self.clients.remove(client)

    def state_key(self, label):
        value = f"emulator-{label}-{self.suffix}"
        self.state_keys.append(value)
        return value

    def test_runtime_composition_binds_quota_cache_and_worker_to_one_client(self):
        policy = admission_policy()
        state_key = self.state_key("runtime")

        def handlers_factory(_cache):
            async def handler(_context):
                return {
                    "status": "SUCCESS",
                    "data_mode": "LIVE",
                    "record_count": 0,
                    "records": [],
                }

            handler.__trendscout_data_mode__ = "LIVE"
            return {WIKIMEDIA_SOURCE_ID: handler}

        runtime = create_firestore_shared_live_composition(
            self.client,
            policy=policy,
            state_key=state_key,
            handlers_factory=handlers_factory,
            resolve_grant=lambda _source_id, _now: None,
            resolve_processing_grants=lambda _now: (),
            utcnow=lambda: self.now,
        )

        self.assertEqual(runtime.project_id, SYNTHETIC_PROJECT_ID)
        self.assertIs(runtime.state_store._client, self.client)
        self.assertIs(runtime.snapshot_blobs._client, self.client)
        self.assertIs(runtime.admission.store, runtime.state_store)
        self.assertIs(runtime.shared_cache.store, runtime.state_store)
        self.assertIs(runtime.shared_cache.payload_blobs, runtime.snapshot_blobs)
        self.assertIs(runtime.worker.admission, runtime.admission)
        self.assertIs(runtime.worker.journal.snapshot_blobs, runtime.snapshot_blobs)
        self.assertIs(runtime.public_fetch.admission, runtime.admission)
        self.assertTrue(runtime.public_fetch.firestore_shared_live)
        self.assertEqual(runtime.public_fetch.data_mode, "LIVE")

    def test_competing_atomic_updates_serialize_across_independent_clients(self):
        count = 8
        key = self.state_key("counter")
        clients = [self.client, self.new_client()]
        stores = [FirestoreAtomicStateStore(client, max_attempts=10) for client in clients]
        barrier = threading.Barrier(count)

        def increment(index):
            store = stores[index % len(stores)]
            barrier.wait(timeout=10)
            return store.transact(
                key,
                lambda state: ({"count": state.get("count", 0) + 1},
                               state.get("count", 0) + 1),
            )

        with ThreadPoolExecutor(max_workers=count) as pool:
            results = list(pool.map(increment, range(count)))

        self.assertEqual(sorted(results), list(range(1, count + 1)))
        stored = self.client.collection("_ts_d5_coordination").document(key).get().to_dict()
        self.assertEqual(stored, {"count": count})

    def test_shared_quota_race_allows_one_reservation_and_no_provider_call(self):
        count = 8
        key = self.state_key("quota")
        policy = admission_policy(request_limit=1, max_inflight=count)
        clients = [self.client, self.new_client()]
        admissions = [
            PublicAdmission(FirestoreAtomicStateStore(client, max_attempts=10),
                            policy, state_key=key)
            for client in clients
        ]
        jobs = []
        for index in range(count):
            admission = admissions[0]
            job = admission.admit(
                f"emulator-quota-job-{self.suffix}-{index:02d}", now=self.now
            )
            claim = admission.claim(job["job_id"], f"worker-{index}", now=self.now)
            jobs.append((job["job_id"], claim["fence"]))

        barrier = threading.Barrier(count)

        def reserve(index):
            admission = admissions[index % len(admissions)]
            job_id, fence = jobs[index]
            barrier.wait(timeout=10)
            try:
                return admission.reserve_attempt(
                    job_id,
                    f"worker-{index}",
                    fence,
                    source_id=WIKIMEDIA_SOURCE_ID,
                    attempt_id=f"emulator-quota-attempt-{self.suffix}-{index:02d}",
                    now=self.now,
                )
            except AdmissionDenied as error:
                return error.code

        with ThreadPoolExecutor(max_workers=count) as pool:
            outcomes = list(pool.map(reserve, range(count)))

        self.assertEqual(outcomes.count("NEW"), 1)
        self.assertEqual(outcomes.count("QUOTA_EXHAUSTED"), count - 1)
        quota = admissions[1].quota_snapshot("wikimedia", now=self.now)
        self.assertEqual(quota["daily"]["used"], 1)
        self.assertEqual(quota["monthly"]["used"], 1)
        self.assertEqual(policy.enabled_sources, frozenset({WIKIMEDIA_SOURCE_ID}))

    def test_lost_commit_ack_replays_receipt_without_double_reserving(self):
        key = self.state_key("recovery")
        policy = admission_policy(request_limit=4, max_inflight=4)
        first = PublicAdmission(
            FirestoreAtomicStateStore(self.client, max_attempts=1), policy, state_key=key
        )
        job = first.admit(f"emulator-recovery-job-{self.suffix}", now=self.now)
        claim = first.claim(job["job_id"], "recovery-worker", now=self.now)
        attempt_id = f"emulator-recovery-attempt-{self.suffix}"

        original_commit = Transaction._commit
        commit_ack_lost = {"value": False}

        def commit_then_drop_ack(transaction):
            result = original_commit(transaction)
            if not commit_ack_lost["value"]:
                commit_ack_lost["value"] = True
                raise ServiceUnavailable("synthetic lost commit acknowledgement")
            return result

        with patch.object(Transaction, "_commit", commit_then_drop_ack):
            with self.assertRaises(CommitOutcomeUnknown):
                first.reserve_attempt(
                    job["job_id"],
                    "recovery-worker",
                    claim["fence"],
                    source_id=WIKIMEDIA_SOURCE_ID,
                    attempt_id=attempt_id,
                    now=self.now,
                )

        second_client = self.new_client()
        second = PublicAdmission(
            FirestoreAtomicStateStore(second_client, max_attempts=1),
            policy,
            state_key=key,
        )
        self.assertEqual(
            second.reserve_attempt(
                job["job_id"],
                "recovery-worker",
                claim["fence"],
                source_id=WIKIMEDIA_SOURCE_ID,
                attempt_id=attempt_id,
                now=self.now,
            ),
            "REPLAY",
        )
        quota = second.quota_snapshot("wikimedia", now=self.now)
        self.assertEqual(quota["daily"]["used"], 1)
        self.assertTrue(commit_ack_lost["value"])

    def test_synthetic_cache_hit_survives_client_and_process_reopen_without_provider_request(self):
        capability = source_capability(WIKIMEDIA_SOURCE_ID)
        scope = CacheScope(
            source_id=WIKIMEDIA_SOURCE_ID,
            provider_id=capability.provider_id,
            query=DEFAULT_QUERY_PLAN.fingerprint,
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            market=DEFAULT_QUERY_PLAN.market,
            language=DEFAULT_QUERY_PLAN.language or "all",
            window=DEFAULT_QUERY_PLAN.time_filter_rule,
            parser_version=f"source-v{capability.capability_version}",
            policy_version="emulator-cache-v1",
        )
        grant = CacheGrant(
            WIKIMEDIA_SOURCE_ID,
            "emulator-cache-v1",
            self.now + timedelta(days=1),
            max_fresh_seconds=60,
            max_stale_seconds=120,
            max_retention_seconds=300,
            allowed=True,
        )
        state_key = scope.key
        self.state_keys.append(state_key)
        payload = {
            "status": "SUCCESS",
            "data_mode": "SYNTHETIC",
            "record_count": 1,
            "records": [{"title": "synthetic-emulator-fixture", "text": "x" * 350_000}],
        }
        writer = create_firestore_shared_cache(self.client, max_inline_bytes=256)
        writer.put(
            scope,
            grant=grant,
            origin="SYNTHETIC",
            payload=payload,
            fetched_at=self.now,
            now=self.now,
        )
        self.close_client(self.client)

        child_check = textwrap.dedent(
            """
            import asyncio
            import json
            import sys
            from datetime import datetime
            from app.jobs.shared_cache import CacheGrant
            from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID
            from app.sources.guarded_collector import CollectionLimits
            from app.sources.query_plan import DEFAULT_QUERY_PLAN
            from app.storage.firestore_emulator import create_guarded_emulator_client
            from app.storage.firestore_shared_cache import create_firestore_shared_cache
            from app.workers.d5_source_handlers import D5SourceHandlers

            values = json.loads(sys.argv[1])
            grant_data = values["grant"]
            grant_data["expires_at"] = datetime.fromisoformat(grant_data["expires_at"])
            grant = CacheGrant(**grant_data)
            now = datetime.fromisoformat(values["now"])
            client = create_guarded_emulator_client()
            provider_calls = []

            async def provider():
                provider_calls.append("called")
                return {
                    "status": "UNAVAILABLE",
                    "data_mode": "SYNTHETIC",
                    "records": [],
                    "record_count": 0,
                }

            try:
                cache = create_firestore_shared_cache(client, max_inline_bytes=256)
                handlers = D5SourceHandlers(
                    query_collectors={
                        GOOGLE_SOURCE_ID: None,
                        BING_SOURCE_ID: None,
                        YOUTUBE_SOURCE_ID: None,
                    },
                    wikimedia=None,
                    plan=DEFAULT_QUERY_PLAN,
                    limits=CollectionLimits(
                        max_queries=1,
                        max_pages_per_query=1,
                        max_requests=1,
                    ),
                    shared_cache=cache,
                    resolve_cache_grant=lambda _source_id, _now: grant,
                    unavailable_mode="LIVE",
                    utcnow=lambda: now,
                )
                result = asyncio.run(
                    handlers._with_cache(WIKIMEDIA_SOURCE_ID, None, provider)
                )
                assert result["delivery_state"] == "CACHED", result
                assert result["provider_request_count"] == 0, result
                assert provider_calls == [], provider_calls
                assert len(result["records"]) == 1, result
                assert len(result["records"][0]["text"]) == 350_000, result
                print(json.dumps({
                    "delivery_state": result["delivery_state"],
                    "provider_request_count": result["provider_request_count"],
                    "provider_calls": len(provider_calls),
                }))
            finally:
                client.close()
            """
        )
        child_arguments = {
            "grant": {
                "source_id": grant.source_id,
                "policy_version": grant.policy_version,
                "expires_at": grant.expires_at.isoformat(),
                "max_fresh_seconds": grant.max_fresh_seconds,
                "max_stale_seconds": grant.max_stale_seconds,
                "max_retention_seconds": grant.max_retention_seconds,
                "allowed": grant.allowed,
            },
            "now": (self.now + timedelta(seconds=5)).isoformat(),
        }
        child = subprocess.run(
            [sys.executable, "-c", child_check, json.dumps(child_arguments)],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(
            json.loads(child.stdout),
            {
                "delivery_state": "CACHED",
                "provider_request_count": 0,
                "provider_calls": 0,
            },
        )

        reader_client = self.new_client()
        reader = create_firestore_shared_cache(reader_client, max_inline_bytes=256)
        provider_calls = []

        async def provider():
            provider_calls.append("called")
            return {"status": "UNAVAILABLE", "data_mode": "SYNTHETIC", "records": [],
                    "record_count": 0}

        source_handlers = D5SourceHandlers(
            query_collectors={GOOGLE_SOURCE_ID: None, BING_SOURCE_ID: None,
                              YOUTUBE_SOURCE_ID: None},
            wikimedia=None,
            plan=DEFAULT_QUERY_PLAN,
            limits=CollectionLimits(max_queries=1, max_pages_per_query=1, max_requests=1),
            shared_cache=reader,
            resolve_cache_grant=lambda _source_id, _now: grant,
            unavailable_mode="LIVE",
            utcnow=lambda: self.now + timedelta(seconds=5),
        )
        result = asyncio.run(source_handlers._with_cache(WIKIMEDIA_SOURCE_ID, None, provider))

        self.assertEqual(result["delivery_state"], "CACHED")
        self.assertEqual(result["provider_request_count"], 0)
        self.assertEqual(provider_calls, [])
        self.assertEqual(result["records"], payload["records"])
        cached_state = reader_client.collection("_ts_d5_coordination").document(state_key).get()
        self.blob_digests.add(cached_state.to_dict()["payload_ref"]["digest"])

        self.close_client(reader_client)
        reopened_client = self.new_client()
        reopened = create_firestore_shared_cache(reopened_client, max_inline_bytes=256)
        after_reopen = reopened.get(scope, grant=grant, now=self.now + timedelta(seconds=6))
        self.assertEqual(after_reopen["status"], "CACHED")
        self.assertEqual(after_reopen["payload"], payload)

    def test_expired_blob_is_purged_with_its_chunks(self):
        blobs = FirestoreSnapshotBlobs(self.client)
        records = [{"fixture": f"purge-{self.suffix}"}]
        reference = blobs.put(
            records,
            now=self.now,
            retain_until=self.now + timedelta(seconds=10),
        )
        self.blob_digests.add(reference["digest"])

        with self.assertRaisesRegex(ValueError, "expired"):
            blobs.get(reference, now=self.now + timedelta(seconds=11))
        self.assertEqual(blobs.purge_expired(now=self.now + timedelta(seconds=11)), 1)
        with self.assertRaises(ValueError):
            blobs.get(reference, now=self.now + timedelta(seconds=11))

    def test_emulator_rules_reject_unauthenticated_application_read_and_write(self):
        document_id = f"emulator-client-denied-{self.suffix}"
        path = (
            f"/v1/projects/{SYNTHETIC_PROJECT_ID}/databases/(default)/documents/"
            f"_ts_d5_coordination/{document_id}"
        )
        host, port = self.client.host.rsplit(":", 1)
        connection = http.client.HTTPConnection(host, int(port), timeout=3)
        try:
            connection.request("GET", path, headers={"Accept": "application/json"})
            read_response = connection.getresponse()
            read_response.read()
            connection.request(
                "PATCH",
                path,
                body=json.dumps({"fields": {"probe": {"stringValue": "blocked"}}}),
                headers={"Content-Type": "application/json"},
            )
            write_response = connection.getresponse()
            write_response.read()
        finally:
            connection.close()

        self.assertIn(read_response.status, {401, 403})
        self.assertIn(write_response.status, {401, 403})
        self.assertFalse(
            self.client.collection("_ts_d5_coordination").document(document_id).get().exists
        )


if __name__ == "__main__":
    unittest.main()
