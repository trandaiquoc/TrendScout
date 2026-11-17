"""Offline source-selection integration through the guarded Fetch worker."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import PublicAdmission
from app.jobs.source_checkpoints import CheckpointGrant
from app.normal_wikimedia_live_policy import normal_google_live_policy
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.sources.catalog import GOOGLE_SOURCE_ID, WIKIMEDIA_SOURCE_ID
from app.sources.google_trends.rss_gateway import GoogleTrendsRSSGateway
from app.sources.google_trends.rss_handler import GoogleTrendsRSSHandler
from app.sources.google_trends.workflow import GoogleDiscoveryFallbackHandler
from app.sources.guarded_collector import CollectionLimits
from app.sources.live_gateway import SourceApproval
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.wikimedia.live_gateway import WikimediaTopPageGateway
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.workers.d5_source_handlers import D5SourceHandlers
from app.workers.guarded_attempt import GuardedAttemptRunner

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
USER_AGENT = "TrendScoutTest/1.0 (https://example.invalid/contact)"
RSS_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="http://www.google.com/trends/hottrends"><channel>
  <title>Trending</title><link>https://trends.google.com/</link>
  <lastBuildDate>Sun, 15 Nov 2026 11:00:00 GMT</lastBuildDate>
  <item><title>Portable outdoor blanket</title>
    <link>https://trends.google.com/trending?geo=US</link>
    <pubDate>Sun, 15 Nov 2026 10:00:00 GMT</pubDate>
    <ht:approx_traffic>20K+</ht:approx_traffic>
  </item>
</channel></rss>'''


class _OfflineApprovals:
    """Test-only approval values; both transports below are MockTransport."""

    def current(self, source_id):
        return SourceApproval(
            source_id=source_id,
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            expires_at=NOW + timedelta(minutes=5),
            policy_version="OFFLINE_FETCH_SELECTION_TEST",
            approval_id=f"OFFLINE_FETCH_SELECTION_TEST:{source_id}",
            source_enabled=True,
            global_enabled=True,
            usage_approved=True,
            retention_approved=True,
            quota_approved=True,
            cost_approved=True,
            security_approved=True,
            distributed_controls_verified=True,
            egress_controls_verified=True,
            telemetry_controls_verified=True,
            scope="PRODUCTION",
        )


class GoogleFetchSourceSelectionTests(unittest.IsolatedAsyncioTestCase):
    def test_default_windows_launcher_always_starts_wikimedia_live_profile(self):
        root = Path(__file__).resolve().parents[2]
        windows_script = (root / "start.ps1").read_text(encoding="utf-8")
        wikimedia_entrypoint = (
            root / "Backend" / "scripts" / "run_wikimedia_live_local.py"
        ).read_text(encoding="utf-8")
        self.assertIn("scripts.run_wikimedia_live_local", windows_script)
        self.assertNotIn("scripts.run_google_live_local", windows_script)
        self.assertIn('"TREND_SCOUT_RUN_MODE": "live"', wikimedia_entrypoint)
        self.assertIn("workers=1", wikimedia_entrypoint)
        self.assertIn("TREND_SCOUT_DEMO_PORT", windows_script)
        self.assertNotIn("--synthetic", windows_script)
        self.assertNotIn("-Cloud", windows_script)

    async def _fetch(self, source_ids):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalSQLiteAtomicStateStore(root / "fetch.sqlite", environment_profile="test")
            policy = normal_google_live_policy()
            admission = PublicAdmission(store, policy, state_key="google-fetch-selection-test")
            runner = GuardedAttemptRunner(admission, clock=lambda: NOW)
            approvals = _OfflineApprovals()
            google_requests = []
            wikimedia_requests = []
            observation_day = (NOW - timedelta(days=2)).date()

            def google_response(request):
                google_requests.append(request)
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/rss+xml"},
                    content=RSS_XML,
                    request=request,
                )

            def wikimedia_response(request):
                wikimedia_requests.append(request)
                return httpx.Response(200, json={"items": [{
                    "country": "US",
                    "access": "all-access",
                    "year": observation_day.strftime("%Y"),
                    "month": observation_day.strftime("%m"),
                    "day": observation_day.strftime("%d"),
                    "articles": [{
                        "article": "Coffee",
                        "project": "en.wikipedia",
                        "views_ceil": 100,
                        "rank": 1,
                    }],
                }]}, request=request)

            google_gateway = GoogleTrendsRSSGateway.offline_fixture(
                approvals,
                runner,
                transport=httpx.MockTransport(google_response),
                clock=lambda: NOW,
            )
            wikimedia_gateway = WikimediaTopPageGateway.offline_fixture(
                approvals,
                runner,
                user_agent=USER_AGENT,
                transport=httpx.MockTransport(wikimedia_response),
                clock=lambda: NOW,
            )
            try:
                google_handler = GoogleDiscoveryFallbackHandler(
                    rss=GoogleTrendsRSSHandler(
                        gateway=google_gateway,
                        policy_fingerprint=policy.plan_fingerprint,
                        utcnow=lambda: NOW,
                    )
                )
                all_handlers = D5SourceHandlers(
                    query_collectors={
                        "google_web_serp": None,
                        "bing_web_serp": None,
                        "youtube_video_search": None,
                    },
                    wikimedia=wikimedia_gateway,
                    google_trends_handler=google_handler,
                    plan=DEFAULT_QUERY_PLAN,
                    limits=CollectionLimits(
                        max_queries=1,
                        max_pages_per_query=1,
                        max_requests=3,
                        max_retries_per_page=0,
                        deadline_seconds=60,
                        max_results=10,
                    ),
                    observation_date=lambda: observation_day,
                    unavailable_mode="SYNTHETIC",
                    utcnow=lambda: NOW,
                ).build()
                handlers = {source_id: all_handlers[source_id]
                            for source_id in policy.enabled_sources}

                def grant(source_id, now):
                    return CheckpointGrant(
                        source_id=source_id,
                        data_mode="SYNTHETIC",
                        policy_version="OFFLINE_FETCH_SELECTION_TEST",
                        expires_at=now + timedelta(minutes=10),
                        max_retention_seconds=600,
                        allowed=True,
                    )

                worker = DistributedFetchWorker(
                    admission,
                    handlers=handlers,
                    resolve_grant=grant,
                    utcnow=lambda: NOW,
                )
                fetch = DistributedPublicFetch(
                    admission,
                    worker,
                    resolve_grant=grant,
                    utcnow=lambda: NOW,
                )
                accepted = fetch.submit(
                    "google-fetch-selection-" + ("both" if len(source_ids) == 2 else "google"),
                    source_ids=source_ids,
                )
                await fetch.run_once("offline-google-selection-test")
                status = fetch.status(accepted["job_id"], include_records=True)
                self.assertEqual(status["status"], "COMPLETED")
                self.assertEqual(status["selected_sources"], sorted(source_ids))
                self.assertEqual(
                    {row["source_id"] for row in status["sources"]}, set(source_ids)
                )
                self.assertEqual(status["data_mode"], "SYNTHETIC")
                return status, google_requests, wikimedia_requests
            finally:
                await google_gateway.aclose()
                await wikimedia_gateway.aclose()

    async def test_google_only_fetch_runs_only_the_selected_google_handler(self):
        status, google_requests, wikimedia_requests = await self._fetch([GOOGLE_SOURCE_ID])

        self.assertEqual(len(google_requests), 1)
        self.assertEqual(wikimedia_requests, [])
        google = status["sources"][0]
        self.assertEqual(google["source_id"], GOOGLE_SOURCE_ID)
        self.assertEqual(google["data_mode"], "SYNTHETIC")
        self.assertEqual(google["coverage"]["discovery_mode"], "RSS_LIMITED_DISCOVERY")
        self.assertEqual(google["coverage"]["http_status"], 200)
        self.assertEqual(google["records"][0]["canonical_keyword"], "Portable outdoor blanket")

    async def test_google_and_wikimedia_fetch_runs_both_selected_handlers(self):
        selected = [GOOGLE_SOURCE_ID, WIKIMEDIA_SOURCE_ID]
        status, google_requests, wikimedia_requests = await self._fetch(selected)

        self.assertEqual(len(google_requests), 1)
        self.assertEqual(len(wikimedia_requests), 1)
        self.assertEqual(status["selected_sources"], sorted(selected))
        by_source = {row["source_id"]: row for row in status["sources"]}
        self.assertEqual(by_source[GOOGLE_SOURCE_ID]["record_count"], 1)
        self.assertEqual(by_source[WIKIMEDIA_SOURCE_ID]["record_count"], 1)
        self.assertEqual(
            by_source[WIKIMEDIA_SOURCE_ID]["records"][0]["article_original"], "Coffee"
        )


if __name__ == "__main__":
    unittest.main()
