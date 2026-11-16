"""Operator-verified YouTube deletion preserves unrelated local data."""

import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone

from local_security_helpers import private_test_directory

from app.collections.store import CollectionStore
from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.normal_wikimedia_live_policy import GOOGLE_POLICY_VERSION, GOOGLE_STATE_KEY
from app.sources.catalog import GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.youtube.deletion import (
    LocalYouTubeDeletionWorkflow,
    YouTubeDeletionError,
    inspect_youtube_summaries,
)
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.storage.local_sqlite_snapshot_blobs import LocalSQLiteSnapshotBlobs


class YouTubeDeletionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.private = private_test_directory(self.temporary.name)
        self.now = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        self.state_path = self.private / "state.sqlite"
        self.store = LocalSQLiteAtomicStateStore.for_local_live(self.state_path)
        self.blobs = LocalSQLiteSnapshotBlobs(self.store)
        self.scope = CacheScope(
            source_id=YOUTUBE_SOURCE_ID,
            provider_id="youtube_data_api_v3",
            query=DEFAULT_QUERY_PLAN.fingerprint,
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            market=DEFAULT_QUERY_PLAN.market,
            language=DEFAULT_QUERY_PLAN.language,
            window=DEFAULT_QUERY_PLAN.time_filter_rule,
            parser_version="source-v1",
            policy_version=GOOGLE_POLICY_VERSION,
        )
        self.collection = CollectionStore(
            self.private / "collections.sqlite", data_mode_scope="LIVE"
        )
        self.workflow = LocalYouTubeDeletionWorkflow(
            self.store, cache_scope=self.scope,
            collection_database=self.collection.path,
        )
        self.youtube_blob = self.blobs.put(
            [{"provider_video_id": "AbCdEf12345", "title": "fixture title"}],
            now=self.now - timedelta(hours=2),
            retain_until=self.now + timedelta(hours=1),
        )
        self.google_blob = self.blobs.put(
            [{"query": "fixture google evidence"}],
            now=self.now - timedelta(hours=2),
            retain_until=self.now + timedelta(hours=1),
        )
        self._seed_state()
        self._seed_cache()
        self._seed_collection_history()

    def tearDown(self):
        self.temporary.cleanup()

    def _seed_state(self, *, active=False):
        expires_at = (self.now + timedelta(minutes=5)).isoformat()
        self.store.transact(GOOGLE_STATE_KEY, lambda _state: ({
            "jobs": {
                "youtube-job": {
                    "job_id": "youtube-job",
                    "state": "RUNNING" if active else "COMPLETED",
                    "expires_at": expires_at,
                    "request_metadata": {"sources": [YOUTUBE_SOURCE_ID]},
                    "source_checkpoints": {
                        YOUTUBE_SOURCE_ID: {
                            "snapshot_storage": "EXTERNAL_RECORDS",
                            "snapshot_ref": {**self.youtube_blob, "path": ["result", "records"]},
                            "snapshot": {"phase": "COMPLETE"},
                        },
                        GOOGLE_SOURCE_ID: {
                            "snapshot_storage": "EXTERNAL_RECORDS",
                            "snapshot_ref": {**self.google_blob, "path": ["result", "records"]},
                            "snapshot": {"phase": "COMPLETE"},
                        },
                    },
                },
            },
            "quota": {"youtube": {"used": 2}, "serpapi": {"used": 4}},
        }, None))
        self.store.transact("normal-youtube-search-schedule-v1", lambda _state: ({
            "last_successful_slot": "2026-11-16T07:00:00+07:00",
        }, None))

    def _seed_cache(self):
        grant = CacheGrant(
            YOUTUBE_SOURCE_ID, GOOGLE_POLICY_VERSION,
            self.now + timedelta(days=7), 60, 7 * 86400, 7 * 86400, allowed=True,
        )
        SharedCache(self.store).put(
            self.scope, grant=grant, origin="LIVE",
            payload={"data_mode": "LIVE", "records": [{"title": "fixture"}]},
            fetched_at=self.now - timedelta(minutes=5), now=self.now,
        )

    def _seed_collection_history(self):
        mixed_manifest = [
            {"source_id": YOUTUBE_SOURCE_ID, "status": "SUCCESS", "record_count": 2,
             "received_record_count": 2, "quality_flags": []},
            {"source_id": GOOGLE_SOURCE_ID, "status": "SUCCESS", "record_count": 3,
             "received_record_count": 2, "quality_flags": []},
        ]
        youtube_manifest = [{
            "source_id": YOUTUBE_SOURCE_ID, "status": "SUCCESS", "record_count": 1,
            "received_record_count": 1, "quality_flags": [],
        }]
        with self.collection.connect() as db:
            for dataset_id, manifest, provider, received, flags in (
                ("mixed-fetch", mixed_manifest, 5, 4,
                 ["YOUTUBE_KEYWORD_EXTRACTION_DISABLED", "GOOGLE_FIXTURE"]),
                ("youtube-fetch", youtube_manifest, 1, 1,
                 ["YOUTUBE_KEYWORD_EXTRACTION_DISABLED"]),
            ):
                db.execute(
                    "INSERT INTO dataset_snapshots(dataset_id,status,data_mode,source_manifest,"
                    "provider_record_count,received_record_count,retained_keyword_count,"
                    "rejected_record_count,quality_flags,processing_status,created_at) "
                    "VALUES(?,?,?,?,?,?,0,0,?,'COMPLETED',?)",
                    (dataset_id, "COMPLETED", "LIVE",
                     json.dumps(manifest, sort_keys=True), provider, received,
                     json.dumps(flags), self.now.isoformat()),
                )
                db.execute(
                    "INSERT INTO fetch_jobs(fetch_id,status,available,updated_at) VALUES(?,?,?,?)",
                    (dataset_id, "COMPLETED", received, self.now.isoformat()),
                )
            db.execute(
                "INSERT INTO keywords(id,keyword,normalized,source,project,observed_at,"
                "data_mode,evidence,context) VALUES('google-keyword','Search topic',"
                "'search topic',?,'fixture','2026-11-16','LIVE','{}','')",
                (GOOGLE_SOURCE_ID,),
            )

    def test_valid_request_purges_youtube_data_and_preserves_other_state(self):
        ticket = self.workflow.receive_request(now=self.now)
        with self.assertRaises(PermissionError):
            self.workflow.fulfill_request(
                ticket, requester_verified=False, application_stopped=True, now=self.now
            )
        with self.assertRaises(PermissionError):
            self.workflow.fulfill_request(
                ticket, requester_verified=True, application_stopped=False, now=self.now
            )
        with self.assertRaises(ValueError):
            self.workflow.fulfill_request(
                "not-a-ticket", requester_verified=True, application_stopped=True, now=self.now
            )
        result = self.workflow.fulfill_request(
            ticket, requester_verified=True, application_stopped=True, now=self.now
        )

        self.assertEqual(result["youtube_cache_entries_removed"], 1)
        self.assertEqual(result["youtube_checkpoints_removed"], 1)
        self.assertEqual(result["youtube_snapshot_blobs_removed"], 1)
        self.assertEqual(result["youtube_dataset_snapshots_redacted"], 1)
        self.assertEqual(result["youtube_only_snapshots_removed"], 1)
        self.assertFalse(result["external_youtube_data_deleted"])
        self.assertTrue(result["browser_storage_requires_user_action"])

        with closing(self.store._connect()) as db:
            state = json.loads(db.execute(
                "SELECT state_json FROM atomic_state WHERE state_key=?", (GOOGLE_STATE_KEY,)
            ).fetchone()[0])
            schedule = json.loads(db.execute(
                "SELECT state_json FROM atomic_state WHERE state_key=?",
                ("normal-youtube-search-schedule-v1",),
            ).fetchone()[0])
            cache = db.execute(
                "SELECT 1 FROM atomic_state WHERE state_key=?", (self.scope.key,)
            ).fetchone()
            blob_digests = {row[0] for row in db.execute(
                "SELECT digest FROM source_snapshot_blobs")}
        self.assertEqual(set(state["jobs"]["youtube-job"]["source_checkpoints"]),
                         {GOOGLE_SOURCE_ID})
        self.assertEqual(state["quota"], {"youtube": {"used": 2}, "serpapi": {"used": 4}})
        self.assertIn("last_successful_slot", schedule)
        self.assertIsNone(cache)
        self.assertNotIn(self.youtube_blob["digest"], blob_digests)
        self.assertIn(self.google_blob["digest"], blob_digests)

        with self.collection.connect() as db:
            mixed = db.execute(
                "SELECT status,source_manifest,provider_record_count,received_record_count,"
                "quality_flags FROM dataset_snapshots WHERE dataset_id='mixed-fetch'"
            ).fetchone()
            self.assertEqual(json.loads(mixed["source_manifest"])[0]["source_id"], GOOGLE_SOURCE_ID)
            self.assertEqual((mixed["provider_record_count"], mixed["received_record_count"]), (3, 2))
            self.assertEqual(json.loads(mixed["quality_flags"]), ["GOOGLE_FIXTURE"])
            self.assertEqual(db.execute(
                "SELECT available FROM fetch_jobs WHERE fetch_id='mixed-fetch'"
            ).fetchone()[0], 2)
            self.assertIsNone(db.execute(
                "SELECT 1 FROM dataset_snapshots WHERE dataset_id='youtube-fetch'"
            ).fetchone())
            self.assertIsNone(db.execute(
                "SELECT 1 FROM fetch_jobs WHERE fetch_id='youtube-fetch'"
            ).fetchone())
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM keywords WHERE id='google-keyword'"
            ).fetchone())

        repeated = self.workflow.fulfill_request(
            ticket, requester_verified=True, application_stopped=True, now=self.now
        )
        self.assertEqual(repeated, result)
        reopened = LocalSQLiteAtomicStateStore.for_local_live(self.state_path)
        with closing(reopened._connect()) as db:
            saved = json.loads(db.execute(
                "SELECT state_json FROM atomic_state WHERE state_key=?", (GOOGLE_STATE_KEY,)
            ).fetchone()[0])
        self.assertEqual(set(saved["jobs"]["youtube-job"]["source_checkpoints"]),
                         {GOOGLE_SOURCE_ID})

    def test_active_youtube_fetch_blocks_deletion_before_any_store_changes(self):
        self._seed_state(active=True)
        ticket = self.workflow.receive_request(now=self.now)
        with self.assertRaisesRegex(YouTubeDeletionError, "queued or running"):
            self.workflow.fulfill_request(
                ticket, requester_verified=True, application_stopped=True, now=self.now
            )
        self.assertTrue(self.workflow.preview(now=self.now)["youtube_cache_entry"])
        self.assertEqual(inspect_youtube_summaries(self.collection.path)[
            "youtube_dataset_snapshots"], 2)

    def test_malformed_mixed_summary_fails_without_partial_summary_changes(self):
        with self.collection.connect() as db:
            db.execute(
                "UPDATE dataset_snapshots SET source_manifest=? WHERE dataset_id='mixed-fetch'",
                (json.dumps([{"source_id": YOUTUBE_SOURCE_ID, "record_count": 2},
                             {"source_id": GOOGLE_SOURCE_ID, "record_count": 3,
                              "received_record_count": 2}]),),
            )
        ticket = self.workflow.receive_request(now=self.now)
        with self.assertRaisesRegex(YouTubeDeletionError, "lacks source-level counts"):
            self.workflow.fulfill_request(
                ticket, requester_verified=True, application_stopped=True, now=self.now
            )
        self.assertFalse(self.workflow.preview(now=self.now)["youtube_cache_entry"])
        with self.collection.connect() as db:
            manifest = json.loads(db.execute(
                "SELECT source_manifest FROM dataset_snapshots WHERE dataset_id='mixed-fetch'"
            ).fetchone()[0])
        self.assertEqual(len(manifest), 2)


if __name__ == "__main__":
    unittest.main()
