"""YouTube-only cache deletion at provider retention boundaries."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.main import _run_youtube_retention_sweeper
from app.sources.catalog import YOUTUBE_SOURCE_ID
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.youtube.retention import purge_expired_youtube_cache
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore


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


if __name__ == "__main__":
    unittest.main()
