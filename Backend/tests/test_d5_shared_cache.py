"""Policy and provenance checks against the one-host SQLite test store."""

import multiprocessing
import sqlite3
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from test_d5_public_admission import SqliteTestStore

from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.sources.catalog import GOOGLE_SOURCE_ID

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
SCOPE = CacheScope(GOOGLE_SOURCE_ID, "serpapi_google", "linen pants", "a" * 64,
                   "gl=us", "en", "7d", "v1", "policy-v1")
GRANT = CacheGrant(GOOGLE_SOURCE_ID, "policy-v1", NOW + timedelta(days=1),
                   max_fresh_seconds=10, max_stale_seconds=20,
                   max_retention_seconds=30, allowed=True)


def write_cache_from_process(args):
    path, offset = args
    cache = SharedCache(SqliteTestStore(path))
    try:
        cache.put(SCOPE, grant=GRANT, origin="SYNTHETIC", payload={"title": f"version-{offset}"},
                  fetched_at=NOW + timedelta(seconds=offset), now=NOW + timedelta(seconds=2))
        return "STORED"
    except ValueError:
        return "OLDER_REJECTED"


class SharedCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "cache.sqlite")
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.cache = SharedCache(SqliteTestStore(self.path))

    def tearDown(self):
        self.directory.cleanup()

    def put(self, **kwargs):
        params = {"grant": GRANT, "origin": "SYNTHETIC", "payload": {"title": "sample"},
                  "fetched_at": NOW, "now": NOW}
        self.cache.put(SCOPE, **{**params, **kwargs})

    def test_fresh_stale_expired_and_origin_remain_distinct(self):
        self.put()
        fresh = self.cache.get(SCOPE, grant=GRANT, now=NOW + timedelta(seconds=9))
        self.assertEqual((fresh["status"], fresh["origin"]), ("CACHED", "SYNTHETIC"))
        self.assertEqual(self.cache.get(SCOPE, grant=GRANT, now=NOW + timedelta(seconds=10))["status"], "STALE")
        self.assertEqual(self.cache.get(SCOPE, grant=GRANT, now=NOW + timedelta(seconds=20))["status"], "EXPIRED")
        self.assertEqual(self.cache.get(SCOPE, grant=GRANT, now=NOW + timedelta(seconds=30))["reason"], "RETENTION_EXPIRED")
        self.assertEqual(self.cache.get(SCOPE, grant=GRANT, now=NOW + timedelta(seconds=31))["reason"], "CACHE_MISS")

    def test_new_policy_cannot_extend_old_retention(self):
        self.put()
        longer = replace(GRANT, max_fresh_seconds=100, max_stale_seconds=200,
                         max_retention_seconds=300)
        self.put(grant=longer)
        self.assertEqual(self.cache.get(SCOPE, grant=longer, now=NOW + timedelta(seconds=30))["status"], "EXPIRED")

    def test_restricted_and_revoked_grants_do_not_return_data(self):
        self.put()
        shorter = replace(GRANT, max_fresh_seconds=1, max_stale_seconds=2,
                          max_retention_seconds=3)
        self.assertEqual(self.cache.get(SCOPE, grant=shorter, now=NOW + timedelta(seconds=3))["status"], "EXPIRED")
        self.put()
        self.assertEqual(self.cache.get(SCOPE, grant=replace(GRANT, allowed=False), now=NOW)["reason"], "POLICY_REVOKED")
        self.assertEqual(self.cache.get(SCOPE, grant=GRANT, now=NOW)["reason"], "CACHE_MISS")

    def test_tightened_retention_persists_even_if_a_later_grant_is_broader(self):
        self.put()
        shorter = replace(GRANT, max_fresh_seconds=1, max_stale_seconds=2,
                          max_retention_seconds=3)
        self.assertEqual(self.cache.get(SCOPE, grant=shorter, now=NOW)["status"], "CACHED")
        self.assertEqual(self.cache.get(SCOPE, grant=GRANT, now=NOW + timedelta(seconds=3))["status"], "EXPIRED")

    def test_tightened_grant_on_reinsert_also_shortens_retention(self):
        self.put()
        shorter = replace(GRANT, max_fresh_seconds=1, max_stale_seconds=2,
                          max_retention_seconds=3)
        self.put(grant=shorter)
        self.assertEqual(self.cache.get(SCOPE, grant=GRANT, now=NOW + timedelta(seconds=3))["status"], "EXPIRED")

    def test_clock_regression_cannot_revive_stale_content(self):
        self.put()
        self.assertEqual(self.cache.get(SCOPE, grant=GRANT, now=NOW + timedelta(seconds=15))["status"], "STALE")
        self.assertEqual(self.cache.get(SCOPE, grant=GRANT, now=NOW + timedelta(seconds=5))["reason"], "INVALID_CACHE_TIME")

    def test_scope_keys_include_every_dimension(self):
        self.put()
        for changes in ({"query": "other"}, {"plan_fingerprint": "b" * 64}, {"market": "ca"},
                        {"language": "fr"}, {"window": "1d"}, {"parser_version": "v2"},
                        {"policy_version": "v2"}):
            with self.subTest(changes=changes):
                other = replace(SCOPE, **changes)
                self.assertNotEqual(other.key, SCOPE.key)
                self.assertEqual(self.cache.get(other, grant=GRANT, now=NOW)["reason"], "CACHE_MISS")

    def test_sensitive_or_conflicting_payload_rejected(self):
        for payload in ({"api_key": "no"}, {"nested": {"Authorization": "no"}},
                        {"url": "https://example.org/?api_key=not-a-real-key"},
                        {"url": "https://example.org/?api%5Fkey=not-a-real-key"},
                        {"url": "https://user:password@example.org/"},
                        {"raw_response": "not allowed"}, {"number": float("nan")}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.put(payload=payload)
        self.put()
        with self.assertRaises(ValueError):
            self.put(payload={"title": "conflict"})
        with self.assertRaises(ValueError):
            self.put(fetched_at=NOW - timedelta(seconds=1))

    def test_missing_and_expired_approval_fail_closed(self):
        with self.assertRaises(PermissionError):
            self.put(grant=replace(GRANT, allowed=False))
        self.put()
        self.assertEqual(self.cache.get(SCOPE, grant=None, now=NOW)["reason"], "POLICY_UNAVAILABLE")
        self.assertEqual(self.cache.get(SCOPE, grant=GRANT, now=NOW - timedelta(seconds=1))["status"], "REJECTED")

    def test_independent_processes_cannot_replace_newer_data_with_older_data(self):
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=2, mp_context=context) as processes:
            results = list(processes.map(write_cache_from_process, [(self.path, 0), (self.path, 1)]))
        self.assertIn("STORED", results)
        result = self.cache.get(SCOPE, grant=GRANT, now=NOW + timedelta(seconds=2))
        self.assertEqual(result["payload"]["title"], "version-1")
