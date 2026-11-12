"""Exercise Firestore transaction boundary using fakes, without cloud/emulator."""

import unittest
from unittest.mock import patch

from google.api_core.exceptions import DeadlineExceeded

from app.storage.firestore_atomic_state import CommitOutcomeUnknown, FirestoreAtomicStateStore


class FakeSnapshot:
    def __init__(self, value):
        self.value, self.exists = value, value is not None

    def to_dict(self):
        return self.value


class FakeTransaction:
    def __init__(self, client):
        self.client = client

    def get(self, reference):
        return iter([FakeSnapshot(self.client.data.get(reference))])

    def set(self, reference, data):
        self.client.pending = (reference, data)


class FakeCollection:
    def document(self, key):
        return key


class FakeClient:
    def __init__(self):
        self.data, self.pending = {}, None

    def collection(self, name):
        if name != "_ts_d5_coordination":
            raise AssertionError("unexpected collection")
        return FakeCollection()

    def transaction(self, max_attempts):
        return FakeTransaction(self)


def fake_transactional(callback):
    def run(tx):
        result = callback(tx)
        if tx.client.pending:
            key, value = tx.client.pending
            tx.client.data[key] = value
        return result
    return run


class FirestoreAtomicStateTests(unittest.TestCase):
    def test_injected_fake_round_trip_and_reject_non_json(self):
        client = FakeClient()
        store = FirestoreAtomicStateStore(client)
        with patch("app.storage.firestore_atomic_state.firestore.transactional", fake_transactional):
            self.assertEqual(store.transact("public", lambda state: ({"count": 1}, "ok")), "ok")
            self.assertEqual(store.transact("public", lambda state: (state, state["count"])), 1)
            with self.assertRaises(ValueError):
                store.transact("public", lambda state: ({"value": float("nan")}, None))
        self.assertEqual(client.data["public"], {"count": 1})

    def test_ambiguous_commit_is_never_reported_successful(self):
        client = FakeClient()
        store = FirestoreAtomicStateStore(client)
        def ambiguous(callback):
            def run(tx):
                callback(tx)
                raise DeadlineExceeded("unknown commit")
            return run
        with patch("app.storage.firestore_atomic_state.firestore.transactional", ambiguous):
            with self.assertRaises(CommitOutcomeUnknown):
                store.transact("public", lambda state: ({"reserved": 1}, "NEW"))

    def test_rejects_invalid_identifier_and_oversize(self):
        store = FirestoreAtomicStateStore(FakeClient())
        for key in ("..", "__reserved__", "slash/name"):
            with self.assertRaises(ValueError):
                store.transact(key, lambda state: (state, None))
        with patch("app.storage.firestore_atomic_state.firestore.transactional", fake_transactional):
            with self.assertRaises(ValueError):
                store.transact("public", lambda state: ({"data": "x" * 700_001}, None))


if __name__ == "__main__":
    unittest.main()
