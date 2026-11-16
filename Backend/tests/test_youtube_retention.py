"""YouTube-only cache deletion at provider retention boundaries."""

import asyncio
import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from local_security_helpers import private_test_directory

from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.main import _run_youtube_retention_sweeper
from app.sources.catalog import GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.youtube.retention import (
    purge_expired_youtube_cache,
    purge_expired_youtube_checkpoints,
)
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.storage.local_sqlite_snapshot_blobs import LocalSQLiteSnapshotBlobs


class MemoryAtomicStore:
    def __init__(self):
        self.values = {}

    def transact(self, key, operation):
        updated, result = operation(self.values.get(key, {}))
        self.values[key] = updated
        return result


class YouTubeRetentionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        self.store = MemoryAtomicStore()
        self.cache = SharedCache(self.store)
        self.youtube_scope = CacheScope(
            source_id=YOUTUBE_SOURCE_ID,
            provider_id="youtube_data_api_v3",
            query=DEFAULT_QUERY_PLAN.fingerprint,
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            market="US",
            language="en-US",
            window=DEFAULT_QUERY_PLAN.time_filter_rule,
            parser_version="source-v1",
            policy_version="fixture-youtube-retention-v1",
        )
        self.other_scope = CacheScope(
            source_id="google_web_serp",
            provider_id="serpapi_google",
            query=DEFAULT_QUERY_PLAN.fingerprint,
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            market="US",
            language="en-US",
            window=DEFAULT_QUERY_PLAN.time_filter_rule,
            parser_version="source-v1",
            policy_version="fixture-google-retention-v1",
        )

    def _grant(self, source_id, now, *, seconds=7 * 24 * 60 * 60):
        return CacheGrant(
            source_id=source_id,
            policy_version=("fixture-youtube-retention-v1"
                            if source_id == YOUTUBE_SOURCE_ID
                            else "fixture-google-retention-v1"),
            expires_at=now + timedelta(seconds=seconds),
            max_fresh_seconds=60,
            max_stale_seconds=seconds,
            max_retention_seconds=seconds,
            allowed=True,
        )

    def _put(self, scope, now, grant):
        self.cache.put(
            scope,
            grant=grant,
            origin="LIVE",
            payload={"data_mode": "LIVE", "records": [{"title": "fixture"}]},
            fetched_at=now,
            now=now,
        )

    def test_expired_youtube_data_is_deleted_without_touching_other_sources(self):
        self._put(self.youtube_scope, self.now, self._grant(YOUTUBE_SOURCE_ID, self.now))
        self._put(self.other_scope, self.now, self._grant("google_web_serp", self.now,
                                                          seconds=30 * 24 * 60 * 60))

        expired_at = self.now + timedelta(days=8)
        deleted = purge_expired_youtube_cache(
            self.cache, self.youtube_scope,
            self._grant(YOUTUBE_SOURCE_ID, expired_at), now=expired_at,
        )

        self.assertTrue(deleted)
        self.assertEqual(self.store.values[self.youtube_scope.key], {})
        self.assertIn("payload", self.store.values[self.other_scope.key])

    def test_unexpired_youtube_data_is_preserved(self):
        grant = self._grant(YOUTUBE_SOURCE_ID, self.now)
        self._put(self.youtube_scope, self.now, grant)

        deleted = purge_expired_youtube_cache(
            self.cache, self.youtube_scope, grant, now=self.now + timedelta(hours=1)
        )

        self.assertFalse(deleted)
        self.assertIn("payload", self.store.values[self.youtube_scope.key])

    def test_purge_rejects_a_non_youtube_scope(self):
        now = self.now + timedelta(days=31)
        grant = self._grant(YOUTUBE_SOURCE_ID, now)
        with self.assertRaises(ValueError):
            purge_expired_youtube_cache(self.cache, self.other_scope, grant, now=now)

    def test_youtube_cache_survives_sqlite_store_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "youtube-cache.sqlite"
            cache = SharedCache(LocalSQLiteAtomicStateStore(
                path, environment_profile="test"
            ))
            grant = self._grant(YOUTUBE_SOURCE_ID, self.now)
            cache.put(
                self.youtube_scope,
                grant=grant,
                origin="LIVE",
                payload={"data_mode": "LIVE", "records": [{"provider_video_id": "AbCdEf12345"}]},
                fetched_at=self.now,
                now=self.now,
            )

            reopened = SharedCache(LocalSQLiteAtomicStateStore(
                path, environment_profile="test"
            ))
            cached = reopened.get(
                self.youtube_scope, grant=grant, now=self.now + timedelta(seconds=30)
            )

        self.assertEqual(cached["status"], "CACHED")
        self.assertEqual(cached["payload"]["records"][0]["provider_video_id"], "AbCdEf12345")


class YouTubeRetentionSweeperTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_sweeper_runs_cleanup_and_cancels_cleanly(self):
        calls = []

        def cleanup():
            calls.append("cleanup")

        async def cancel_after_first_wait(_seconds):
            raise asyncio.CancelledError

        task = asyncio.create_task(_run_youtube_retention_sweeper(
            cleanup, sleep=cancel_after_first_wait
        ))
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(calls, ["cleanup"])


class YouTubeCheckpointRetentionTests(unittest.TestCase):
    def test_owner_acceptance_preserves_expired_blobs_during_startup_and_new_puts(self):
        now = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            private = private_test_directory(temporary)
            store = LocalSQLiteAtomicStateStore.for_local_live(private / "state.sqlite")
            blobs = LocalSQLiteSnapshotBlobs(store, preserve_expired=True)
            old = blobs.put(
                [{"provider_video_id": "AbCdEf12345"}],
                now=now,
                retain_until=now + timedelta(hours=1),
            )
            later = now + timedelta(hours=2)

            self.assertEqual(blobs.purge_expired(now=later), 0)
            new = blobs.put(
                [{"provider_video_id": "ZyXwVu98765"}],
                now=later,
                retain_until=later + timedelta(hours=1),
            )
            with closing(store._connect()) as connection:
                digests = {row[0] for row in connection.execute(
                    "SELECT digest FROM source_snapshot_blobs"
                )}

            self.assertEqual(digests, {old["digest"], new["digest"]})

    def test_expired_youtube_checkpoints_and_only_unshared_expired_blobs_are_removed(self):
        now = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            private = private_test_directory(temporary)
            store = LocalSQLiteAtomicStateStore.for_local_live(private / "state.sqlite")
            blobs = LocalSQLiteSnapshotBlobs(store)
            retention_end = now - timedelta(hours=1)
            shared = blobs.put([{"provider_video_id": "AbCdEf12345"}],
                               now=now - timedelta(hours=3), retain_until=retention_end)
            youtube_only = blobs.put([{"provider_video_id": "ZyXwVu98765"}],
                                     now=now - timedelta(hours=3), retain_until=retention_end)
            later = blobs.put([{"provider_video_id": "LmNoPq54321"}],
                              now=now - timedelta(hours=3),
                              retain_until=now + timedelta(hours=1))
            expired = (now - timedelta(minutes=1)).isoformat()
            fresh = (now + timedelta(minutes=30)).isoformat()
            job_expiry = (now + timedelta(hours=1)).isoformat()

            def seed(_state):
                return ({"jobs": {
                    "expired-youtube": {
                        "expires_at": job_expiry,
                        "source_checkpoints": {YOUTUBE_SOURCE_ID: {
                            "retain_until": expired,
                            "snapshot_ref": {**shared, "path": ["result", "records"]},
                        }},
                    },
                    "expired-youtube-only": {
                        "expires_at": job_expiry,
                        "source_checkpoints": {YOUTUBE_SOURCE_ID: {
                            "retain_until": expired,
                            "snapshot_ref": {**youtube_only, "path": ["result", "records"]},
                        }},
                    },
                    "fresh-youtube": {
                        "expires_at": job_expiry,
                        "source_checkpoints": {YOUTUBE_SOURCE_ID: {
                            "retain_until": fresh,
                            "snapshot_ref": {**later, "path": ["result", "records"]},
                        }},
                    },
                    "other-source": {
                        "expires_at": job_expiry,
                        "source_checkpoints": {GOOGLE_SOURCE_ID: {
                            "retain_until": expired,
                            "snapshot_ref": {**shared, "path": ["result", "records"]},
                        }},
                    },
                }, "quota": {"youtube": {"used": 2}}}, None)

            store.transact("normal-google-live-v1", seed)
            result = purge_expired_youtube_checkpoints(
                store, blobs, now=now, state_key="normal-google-live-v1"
            )
            self.assertEqual(result, {
                "expired_checkpoints": 2,
                "invalid_checkpoints": 0,
                "deleted_blobs": 1,
                "retained_referenced_blobs": 1,
            })
            with closing(store._connect()) as connection:
                state = json.loads(connection.execute(
                    "SELECT state_json FROM atomic_state WHERE state_key=?",
                    ("normal-google-live-v1",),
                ).fetchone()[0])
                persisted_digests = {row[0] for row in connection.execute(
                    "SELECT digest FROM source_snapshot_blobs")}
            self.assertNotIn(YOUTUBE_SOURCE_ID,
                             state["jobs"]["expired-youtube"]["source_checkpoints"])
            self.assertIn(GOOGLE_SOURCE_ID,
                          state["jobs"]["other-source"]["source_checkpoints"])
            self.assertIn(YOUTUBE_SOURCE_ID,
                          state["jobs"]["fresh-youtube"]["source_checkpoints"])
            self.assertEqual(state["quota"], {"youtube": {"used": 2}})
            self.assertNotIn(youtube_only["digest"], persisted_digests)
            self.assertIn(shared["digest"], persisted_digests)
            self.assertIn(later["digest"], persisted_digests)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
