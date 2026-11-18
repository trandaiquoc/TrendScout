"""Offline contracts for the explicit injected Firestore LIVE composition."""

import tempfile
import unittest
from pathlib import Path

from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import AdmissionPolicy, PoolLimit, PublicAdmission
from app.jobs.shared_cache import SharedCache
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.orchestration.firestore_shared_live_composition import (
    PRODUCTION_FIRESTORE_PROJECT_ID,
    create_firestore_shared_live_composition,
)
from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from app.storage.firestore_atomic_state import FirestoreAtomicStateStore
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore


class FakeClient:
    def __init__(self, project=PRODUCTION_FIRESTORE_PROJECT_ID):
        self.project = project
        self._database = "(default)"
        self.calls = []

    def collection(self, name):
        self.calls.append(("collection", name))
        return object()

    def transaction(self, max_attempts):
        self.calls.append(("transaction", max_attempts))
        return object()


def _policy():
    return AdmissionPolicy(
        enabled_sources=frozenset({WIKIMEDIA_SOURCE_ID}),
        pools={"wikimedia": PoolLimit(10, 20, 10, 20, 1)},
        plan_fingerprint="a" * 64,
        enabled=True,
    )


def _handler_factory(cache):
    async def handler(_context):
        return {
            "status": "SUCCESS",
            "data_mode": "LIVE",
            "record_count": 0,
            "records": [],
        }

    handler.__trendscout_data_mode__ = "LIVE"
    return {WIKIMEDIA_SOURCE_ID: handler}


def _composition(client, **overrides):
    arguments = {
        "policy": _policy(),
        "state_key": "firestore-runtime-unit-test",
        "handlers_factory": _handler_factory,
        "resolve_grant": lambda _source_id, _now: None,
        "resolve_processing_grants": lambda _now: (),
    }
    arguments.update(overrides)
    return create_firestore_shared_live_composition(client, **arguments)


class FirestoreRuntimeCompositionTests(unittest.TestCase):
    def test_all_shared_runtime_components_use_the_injected_client(self):
        client = FakeClient()
        received_caches = []

        def handlers_factory(cache):
            received_caches.append(cache)
            return _handler_factory(cache)

        composition = _composition(client, handlers_factory=handlers_factory)

        self.assertEqual(composition.project_id, PRODUCTION_FIRESTORE_PROJECT_ID)
        self.assertIs(composition.state_store._client, client)
        self.assertIs(composition.snapshot_blobs._client, client)
        self.assertIs(composition.admission.store, composition.state_store)
        self.assertIs(composition.shared_cache.store, composition.state_store)
        self.assertIs(composition.shared_cache.payload_blobs, composition.snapshot_blobs)
        self.assertIs(composition.worker.admission, composition.admission)
        self.assertIs(composition.worker.journal.snapshot_blobs, composition.snapshot_blobs)
        self.assertIs(composition.public_fetch.admission, composition.admission)
        self.assertEqual(received_caches, [composition.shared_cache])
        self.assertTrue(composition.public_fetch.firestore_shared_live)
        self.assertEqual(composition.public_fetch.data_mode, "LIVE")
        self.assertEqual(client.calls, [])

    def test_shared_live_rejects_local_sqlite_state_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            state_store = LocalSQLiteAtomicStateStore(
                Path(directory) / "state.sqlite3",
                environment_profile="test",
            )
            admission = PublicAdmission(
                state_store,
                _policy(),
                state_key="firestore-runtime-unit-test",
            )
            cache = SharedCache(state_store)
            worker = DistributedFetchWorker(
                admission,
                handlers=_handler_factory(cache),
                resolve_grant=lambda _source_id, _now: None,
            )

            with self.assertRaisesRegex(ValueError, "Firestore shared LIVE requires"):
                DistributedPublicFetch(
                    admission,
                    worker,
                    resolve_grant=lambda _source_id, _now: None,
                    firestore_shared_live=True,
                    resolve_processing_grants=lambda _now: (),
                )

    def test_unselected_firestore_project_is_rejected_before_client_use(self):
        client = FakeClient(project="unexpected-project")

        with self.assertRaisesRegex(ValueError, "must target trendscout-192e4"):
            _composition(client)

        self.assertEqual(client.calls, [])

    def test_named_firestore_database_is_rejected_before_client_use(self):
        client = FakeClient()
        client._database = "other"

        with self.assertRaisesRegex(ValueError, r"existing \(default\) database"):
            _composition(client)

        self.assertEqual(client.calls, [])

    def test_firestore_live_facade_rejects_missing_shared_blob_store(self):
        client = FakeClient()
        admission = PublicAdmission(
            FirestoreAtomicStateStore(client),
            _policy(),
            state_key="firestore-runtime-unit-test",
        )
        worker = DistributedFetchWorker(
            admission,
            handlers=_handler_factory(SharedCache(admission.store)),
            resolve_grant=lambda _source_id, _now: None,
        )

        with self.assertRaisesRegex(ValueError, "Firestore shared LIVE requires"):
            DistributedPublicFetch(
                admission,
                worker,
                resolve_grant=lambda _source_id, _now: None,
                firestore_shared_live=True,
                resolve_processing_grants=lambda _now: (),
            )


if __name__ == "__main__":
    unittest.main()
