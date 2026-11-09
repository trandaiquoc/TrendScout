import unittest
from unittest.mock import MagicMock

from google.api_core.exceptions import AlreadyExists, NotFound, PermissionDenied

from app.storage.base import RecordAlreadyExists, RecordNotFound, StorageError
from app.storage.fake import FakeStorage
from app.storage.firestore import FirestoreStorage


class FakeStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = FakeStorage()

    async def test_run_create_read_update_and_absent(self):
        self.assertIsNone(await self.store.get_run("missing"))
        await self.store.create_run("r1", {"status": "PENDING", "count": 0, "score": None})
        await self.store.update_run("r1", {"status": "RUNNING", "baseline": None})
        result = await self.store.get_run("r1")
        self.assertEqual(result, {"status": "RUNNING", "count": 0, "score": None, "baseline": None})
        self.assertNotIn("not_reported", result)
        result["status"] = "changed"
        self.assertEqual((await self.store.get_run("r1"))["status"], "RUNNING")

    async def test_record_types_and_values(self):
        for kind in ("snapshots", "observations", "keyword_results", "baseline_buckets"):
            await self.store.put_record("r1", kind, "a", {"zero": 0, "null": None, "flags": []})
            self.assertEqual(await self.store.get_record("r1", kind, "a"), {"zero": 0, "null": None, "flags": []})
            self.assertIsNone(await self.store.get_record("r1", kind, "missing"))
        await self.store.put_record("r1", "observations", "a", {"views": 2})
        self.assertEqual(await self.store.get_record("r1", "observations", "a"), {"views": 2})

    async def test_errors_and_invalid_paths(self):
        await self.store.create_run("r1", {})
        with self.assertRaises(RecordAlreadyExists):
            await self.store.create_run("r1", {})
        with self.assertRaises(RecordNotFound):
            await self.store.update_run("absent", {"status": "FAILED"})
        with self.assertRaises(ValueError):
            await self.store.get_run("run/other")
        with self.assertRaises(ValueError):
            await self.store.update_run("r1", {"nested.field": 0})
        with self.assertRaises(ValueError):
            await self.store.get_record("r1", "unknown", "x")


class FirestoreMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_read_update_and_nested_path(self):
        client = MagicMock()
        doc = client.collection.return_value.document.return_value
        doc.get.return_value.exists = True
        doc.get.return_value.to_dict.return_value = {"zero": 0, "null": None}
        storage = FirestoreStorage(client)
        await storage.create_run("r", {"status": "PENDING"})
        doc.create.assert_called_once_with({"status": "PENDING"})
        await storage.update_run("r", {"status": "RUNNING"})
        doc.update.assert_called_once_with({"status": "RUNNING"})
        self.assertEqual(await storage.get_run("r"), {"zero": 0, "null": None})
        await storage.put_record("r", "observations", "o", {"views": 0})
        doc.collection.assert_called_with("observations")
        doc.collection.return_value.document.return_value.set.assert_called_once_with({"views": 0})
        doc.get.return_value.exists = False
        self.assertIsNone(await storage.get_run("r"))

    async def test_provider_errors_preserve_cause(self):
        client = MagicMock()
        doc = client.collection.return_value.document.return_value
        storage = FirestoreStorage(client)
        doc.create.side_effect = AlreadyExists("exists")
        with self.assertRaises(RecordAlreadyExists):
            await storage.create_run("r", {})
        doc.update.side_effect = NotFound("missing")
        with self.assertRaises(RecordNotFound):
            await storage.update_run("r", {"status": "FAILED"})
        doc.get.side_effect = PermissionDenied("denied")
        with self.assertRaises(StorageError) as raised:
            await storage.get_run("r")
        self.assertIsInstance(raised.exception.__cause__, PermissionDenied)
