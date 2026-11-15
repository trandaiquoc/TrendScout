"""Google Trends contracts and local guard behavior, exercised offline only."""

import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.collections.store import CollectionStore, keyword_record
from app.google_live_settings import GoogleLiveSettings
from app.jobs.public_admission import PublicAdmission
from app.normal_wikimedia_live_policy import GOOGLE_STATE_KEY, normal_google_live_policy
from app.orchestration.normal_google_live_composition import (
    _GoogleLocalApproval,
    normal_google_live_composition,
)
from app.sources.catalog import GOOGLE_SOURCE_ID
from app.sources.google_trends.rss import parse_google_trends_rss
from app.sources.google_trends.trending_now import (
    TRENDING_NOW_ENGINE,
    GoogleTrendsRequest,
    build_trending_now_params,
    normalize_keyword,
    normalize_trending_candidates,
    parse_trending_now_response,
)
from app.sources.google_trends.verification_plan import (
    MAX_VERIFICATION_QUERIES,
    build_verification_plan,
    select_verification_candidates,
    validate_dynamic_context,
)
from app.sources.serpapi.account import (
    SerpApiAccountClient,
    parse_account_response,
)
from app.sources.serpapi.errors import SerpApiError
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
FAKE_KEY = "fixture-serpapi-key-never-real"


def _trend_row(query="portable outdoor blanket", **updates):
    value = {
        "query": query,
        "start_timestamp": 1_794_739_200,
        "end_timestamp": None,
        "active": True,
        "search_volume": 200000,
        "search_volume_display": "200K+",
        "increase_percentage": 980,
        "categories": [{"id": 185, "name": "Home & Garden"}],
        "trend_breakdown": ["portable outdoor blanket", "folding picnic mat"],
    }
    return {**value, **updates}


def _trending_response(rows, *, status="Success", content_type="application/json"):
    return httpx.Response(200, headers={"Content-Type": content_type}, json={
        "search_metadata": {
            "status": status,
            "created_at": "2026-11-15T11:55:00Z",
            "processed_at": "2026-11-15T11:55:02Z",
        },
        "search_parameters": {
            "engine": TRENDING_NOW_ENGINE, "geo": "US", "hl": "en", "hours": 24,
        },
        "trending_searches": rows,
    })


def _candidate(query, categories, *, active=True, increase=100):
    _, normalized = normalize_keyword(query)
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    return {
        "canonical_keyword": query,
        "canonical_keyword_identity": digest,
        "trend_clusters": [{
            "active": active,
            "increase_percentage": increase,
            "categories": [{"id": index + 1, "name": name}
                           for index, name in enumerate(categories)],
        }],
    }


class GoogleTrendsParserTests(unittest.TestCase):
    def setUp(self):
        self.request = GoogleTrendsRequest(GOOGLE_SOURCE_ID, "a" * 64)

    def test_request_is_fixed_and_normalization_is_deterministic(self):
        self.assertEqual(build_trending_now_params(self.request), {
            "engine": TRENDING_NOW_ENGINE, "geo": "US", "hl": "en",
            "hours": 24, "output": "json",
        })
        self.assertEqual(normalize_keyword("  Ｐortable\t Outdoor Blanket "),
                         ("Portable Outdoor Blanket", "portable outdoor blanket"))
        with self.assertRaises(SerpApiError):
            build_trending_now_params(GoogleTrendsRequest(
                GOOGLE_SOURCE_ID, "a" * 64, geo="CA"
            ))

    def test_trending_now_keeps_provider_buckets_and_missing_values(self):
        payload = _trending_response([
            _trend_row(),
            _trend_row("PORTABLE OUTDOOR BLANKET", increase_percentage=1200),
            _trend_row("Handmade patio privacy screen", active=None,
                       search_volume=None, search_volume_display=None,
                       increase_percentage=None, categories=None, trend_breakdown=None),
        ])
        page = parse_trending_now_response(
            payload, request=self.request, sensitive_key=FAKE_KEY
        )
        candidates = normalize_trending_candidates(page, fetched_at=NOW, data_mode="LIVE")
        self.assertEqual(len(candidates), 2)
        portable = next(row for row in candidates
                        if row["canonical_keyword"] == "portable outdoor blanket")
        self.assertEqual(len(portable["trend_clusters"]), 2)
        self.assertEqual(portable["trend_clusters"][0]["search_volume_semantics"],
                         "PROVIDER_REPORTED_APPROXIMATE_BUCKET")
        missing = next(row for row in candidates
                       if row["canonical_keyword"].startswith("Handmade"))
        cluster = missing["trend_clusters"][0]
        self.assertIsNone(cluster["search_volume"])
        self.assertIsNone(cluster["search_volume_display"])
        self.assertIsNone(cluster["increase_percentage"])
        self.assertIsNone(cluster["active"])
        self.assertIsNone(cluster["categories"])
        self.assertEqual(missing["data_mode"], "LIVE")

    def test_trending_now_rejects_wrong_schema_type_and_reflected_key(self):
        with self.assertRaises(SerpApiError):
            parse_trending_now_response(
                _trending_response([_trend_row()], content_type="text/html"),
                request=self.request, sensitive_key=FAKE_KEY,
            )
        invalid = _trending_response([_trend_row(search_volume=True)])
        page = parse_trending_now_response(invalid, request=self.request,
                                           sensitive_key=FAKE_KEY)
        self.assertEqual(page.rejected_record_count, 1)
        reflected = _trending_response([_trend_row(query=FAKE_KEY)])
        with self.assertRaises(SerpApiError):
            parse_trending_now_response(reflected, request=self.request,
                                        sensitive_key=FAKE_KEY)

    def test_trending_now_live_parser_carries_synthetic_mode_without_relabeling(self):
        page = parse_trending_now_response(
            _trending_response([_trend_row()]), request=self.request, sensitive_key=FAKE_KEY
        )
        candidates = normalize_trending_candidates(page, fetched_at=NOW, data_mode="SYNTHETIC")
        self.assertEqual(candidates[0]["data_mode"], "SYNTHETIC")

    def test_rss_is_limited_and_retains_approximate_traffic(self):
        xml = b'''<?xml version="1.0" encoding="UTF-8"?>
        <rss xmlns:ht="http://www.google.com/trends/hottrends"><channel>
          <title>Trending</title><link>https://trends.google.com/</link>
          <lastBuildDate>Sun, 15 Nov 2026 11:00:00 GMT</lastBuildDate>
          <item><title>Portable outdoor blanket</title>
            <link>https://trends.google.com/trending?geo=US</link>
            <pubDate>Sun, 15 Nov 2026 10:00:00 GMT</pubDate>
            <ht:approx_traffic>20K+</ht:approx_traffic>
            <description>Provider limited sample</description>
          </item>
          <item><title>Bad link</title><link>https://attacker.invalid/</link></item>
        </channel></rss>'''
        page = parse_google_trends_rss(
            httpx.Response(200, headers={"Content-Type": "application/rss+xml"}, content=xml),
            fetched_at=NOW, data_mode="LIVE",
        )
        self.assertEqual(page.provider_record_count, 2)
        self.assertEqual(page.rejected_record_count, 1)
        self.assertEqual(len(page.records), 1)
        row = page.records[0]
        self.assertEqual(row["discovery_mode"], "RSS_LIMITED_DISCOVERY")
        self.assertIsNone(row["time_window_hours"])
        self.assertIsNone(row["trend_clusters"][0]["search_volume"])
        self.assertEqual(row["trend_clusters"][0]["search_volume_display"], "20K+")
        self.assertEqual(row["trend_clusters"][0]["search_volume_semantics"],
                         "RSS_APPROX_TRAFFIC_BUCKET")

    def test_rss_rejects_entity_declarations_and_wrong_root(self):
        for body in (b"<!DOCTYPE rss [<!ENTITY x SYSTEM 'file:///C:/secret'>]><rss/>",
                     b"<html><channel/></html>"):
            with self.subTest(body=body), self.assertRaises(SerpApiError):
                parse_google_trends_rss(
                    httpx.Response(200, headers={"Content-Type": "application/rss+xml"},
                                   content=body),
                    fetched_at=NOW,
                )


class GoogleVerificationPlanTests(unittest.TestCase):
    def test_selection_is_deterministic_diverse_and_limited_to_two(self):
        candidates = (
            _candidate("linen wrap dress", ["Fashion"], increase=20),
            _candidate("portable outdoor blanket", ["Home & Living"], increase=900),
            _candidate("folding solar lantern", ["Outdoors"], increase=400),
        )
        selected = select_verification_candidates(candidates, limit=MAX_VERIFICATION_QUERIES)
        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[0]["canonical_keyword"], "portable outdoor blanket")
        selected_categories = {
            item["trend_clusters"][0]["categories"][0]["name"] for item in selected
        }
        self.assertIn("Home & Living", selected_categories)
        self.assertIn("Outdoors", selected_categories)
        repeated = select_verification_candidates(candidates, recently_verified=frozenset({
            item["canonical_keyword_identity"] for item in candidates[:2]
        }))
        self.assertEqual([item["canonical_keyword"] for item in repeated],
                         ["folding solar lantern"])

    def test_dynamic_plan_accepts_only_server_selected_candidates(self):
        candidate = _candidate("portable outdoor blanket", ["Home & Garden"])
        plan = build_verification_plan((candidate,), policy_fingerprint="b" * 64,
                                       started_at=NOW)
        self.assertEqual(plan.plan_id, "google-trend-candidate-serp")
        self.assertEqual(len(plan.contexts), 1)
        self.assertTrue(validate_dynamic_context(plan.contexts[0], policy_fingerprint="b" * 64))
        modified = plan.contexts[0].__class__(
            **{**plan.contexts[0].__dict__, "canonical_query": "arbitrary browser query"}
        )
        self.assertFalse(validate_dynamic_context(modified, policy_fingerprint="b" * 64))
        with self.assertRaises(ValueError):
            build_verification_plan((candidate, candidate, candidate),
                                    policy_fingerprint="b" * 64, started_at=NOW)


def _account_response(*, remaining=9, plan_remaining=9, hour_used=2, hour_limit=50,
                      key=FAKE_KEY, status="Active"):
    return httpx.Response(200, headers={"Content-Type": "application/json"}, json={
        "account_status": status,
        "api_key": key,
        "account_email": "private@example.invalid",
        "searches_per_month": 250,
        "plan_searches_left": plan_remaining,
        "total_searches_left": remaining,
        "this_month_usage": 241,
        "this_hour_searches": hour_used,
        "account_rate_limit_per_hour": hour_limit,
    })


class SerpApiAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_client_injects_key_at_transport_and_returns_only_numeric_metadata(self):
        seen = []

        def handler(request):
            seen.append(request)
            return _account_response()

        client = SerpApiAccountClient.offline_fixture(
            FAKE_KEY, transport=httpx.MockTransport(handler), clock=lambda: NOW
        )
        try:
            snapshot = await client.fetch()
        finally:
            await client.aclose()
        self.assertEqual(seen[0].url.host, "serpapi.com")
        self.assertEqual(seen[0].url.path, "/account.json")
        self.assertEqual(seen[0].url.params["api_key"], FAKE_KEY)
        self.assertEqual(snapshot.total_remaining, 9)
        self.assertEqual(snapshot.monthly_remaining, 9)
        self.assertNotIn(FAKE_KEY, repr(snapshot))
        self.assertNotIn("private@example.invalid", repr(snapshot))
        self.assertEqual(set(snapshot.public_metadata()), {
            "observed_at", "monthly_limit", "monthly_remaining", "total_remaining",
            "month_to_date_searches", "current_hour_searches", "hourly_limit",
        })

    async def test_account_budget_fails_closed_when_remaining_is_below_reserve(self):
        client = SerpApiAccountClient.offline_fixture(
            FAKE_KEY,
            transport=httpx.MockTransport(lambda _request: _account_response(
                remaining=3, plan_remaining=3
            )),
            clock=lambda: NOW,
        )
        from app.sources.google_trends.budget import SerpApiAccountBudget

        budget = SerpApiAccountBudget(client, cross_machine_reserve=3)
        try:
            with self.assertRaises(SerpApiError) as error:
                await budget.ensure_attempt_allowed()
            self.assertEqual(error.exception.code, "QUOTA_BUDGET_EXHAUSTED")
            self.assertNotIn(FAKE_KEY, str(error.exception))
        finally:
            await client.aclose()

    def test_account_parser_rejects_incomplete_and_inactive_accounts(self):
        with self.assertRaises(SerpApiError):
            parse_account_response(
                httpx.Response(200, headers={"Content-Type": "application/json"},
                              json={"account_status": "Active"}),
                observed_at=NOW,
            )
        with self.assertRaises(SerpApiError):
            parse_account_response(_account_response(status="Suspended"), observed_at=NOW)


class GoogleRuntimeAndStorageTests(unittest.IsolatedAsyncioTestCase):
    def test_google_local_approval_is_stable_during_a_guarded_attempt(self):
        approval = _GoogleLocalApproval(
            GOOGLE_SOURCE_ID, enabled=True, clock=lambda: NOW
        )
        first = approval.current(GOOGLE_SOURCE_ID)
        self.assertIsNotNone(first)
        self.assertIs(approval.current(GOOGLE_SOURCE_ID), first)
        self.assertIsNone(approval.current("bing_web_serp"))

    async def test_google_runtime_constructs_without_provider_requests_by_default(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        if hasattr(root, "chmod"):
            try:
                root.chmod(0o700)
            except OSError:
                pass
        store = CollectionStore(root / "collections.sqlite", data_mode_scope="LIVE")

        class NeverReadCredentials:
            def resolve(self, _reference):
                raise AssertionError("credential lookup is forbidden without paid-path approval")

        config = GoogleLiveSettings(
            local_runtime_approved=True,
            serpapi_daily_budget=3,
            serpapi_monthly_budget=3,
            rss_daily_budget=1,
            rss_monthly_budget=1,
            batches_per_hour=2,
        )
        try:
            async with normal_google_live_composition(
                user_agent="TrendScoutTest/1.0 (https://example.invalid)",
                state_directory=root / "google-state",
                google_settings=config,
                credential_provider=NeverReadCredentials(),
                collection_store=store,
                utcnow=lambda: NOW,
            ) as facade:
                self.assertTrue(facade.normal_google_live)
                self.assertEqual(facade.data_mode, "LIVE")
                self.assertEqual(facade.google_capabilities["trends_discovery"],
                                 "NOT_CONFIGURED")
                self.assertEqual(facade.google_capabilities["rss_discovery"],
                                 "NOT_CONFIGURED")
                self.assertEqual(facade.google_capabilities["serp_verification"],
                                 "NOT_CONFIGURED")
                self.assertEqual(facade.google_account_quota_state["status"],
                                 "NOT_CONFIGURED")
                self.assertEqual(facade.admission.policy.pools["serpapi"].daily_requests, 3)
                self.assertEqual(facade.admission.policy.pools["google_rss"].daily_requests, 1)
                self.assertEqual(facade.admission.policy.max_starts_per_window, 2)
        finally:
            directory.cleanup()

    def test_provider_switch_does_not_create_duplicate_keyword_identity(self):
        common = {
            "data_source": "GOOGLE_TRENDS",
            "canonical_keyword": "portable outdoor blanket",
            "canonical_keyword_identity": hashlib.sha256(
                b"portable outdoor blanket"
            ).hexdigest(),
            "region": "US", "language": "en", "time_window_hours": 24,
            "fetched_at": NOW.isoformat(),
            "delivery_state": "FRESH", "data_mode": "LIVE",
            "source_url": "https://trends.google.com/trending?geo=US",
            "quality_flags": [], "trend_clusters": [{"trend_cluster_id": "cluster-1"}],
        }
        trends = keyword_record({
            **common, "provider_id": "serpapi_google_trends_trending_now",
            "transport_id": "serpapi", "discovery_mode": "SERPAPI_TRENDING_NOW",
        }, GOOGLE_SOURCE_ID, "LIVE")
        rss = keyword_record({
            **common, "provider_id": "google_trends_rss",
            "transport_id": "google_trends_rss", "discovery_mode": "RSS_LIMITED_DISCOVERY",
            "time_window_hours": None,
        }, GOOGLE_SOURCE_ID, "LIVE")
        self.assertIsNotNone(trends)
        self.assertIsNotNone(rss)
        self.assertEqual(trends["id"], rss["id"])
        self.assertEqual(trends["project"], "google_trends:US:en")

    def test_google_pool_limits_are_separate_and_validated(self):
        policy = normal_google_live_policy(serpapi_daily_budget=3,
                                           serpapi_monthly_budget=90,
                                           rss_daily_budget=10,
                                           rss_monthly_budget=300)
        self.assertEqual(policy.pools["serpapi"].daily_requests, 3)
        self.assertEqual(policy.pools["serpapi"].monthly_requests, 90)
        self.assertEqual(policy.pools["google_rss"].daily_requests, 10)
        with self.assertRaises(ValueError):
            normal_google_live_policy(serpapi_daily_budget=7)

    def test_rss_and_paid_serp_reservations_use_separate_pools_and_replay_once(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        store = LocalSQLiteAtomicStateStore.for_local_live(root / "quota.sqlite")
        admission = PublicAdmission(
            store, normal_google_live_policy(), state_key=GOOGLE_STATE_KEY
        )
        key = "google-rss-idempotency-0001"
        try:
            accepted = admission.admit(
                key, now=NOW, request_metadata={"sources": [GOOGLE_SOURCE_ID]}
            )
            replay = admission.admit(
                key, now=NOW, request_metadata={"sources": [GOOGLE_SOURCE_ID]}
            )
            self.assertEqual(replay["result"], "REPLAY")
            self.assertEqual(replay["job_id"], accepted["job_id"])
            lease = admission.claim(accepted["job_id"], "offline-worker", now=NOW)
            admission.reserve_attempt(
                accepted["job_id"], "offline-worker", lease["fence"],
                source_id=GOOGLE_SOURCE_ID, attempt_id="google-rss-attempt-0001",
                pool_name="google_rss", now=NOW,
            )
            admission.reserve_attempt(
                accepted["job_id"], "offline-worker", lease["fence"],
                source_id=GOOGLE_SOURCE_ID, attempt_id="google-serp-attempt-0001",
                pool_name="serpapi", now=NOW,
            )
            admission.reserve_attempt(
                accepted["job_id"], "offline-worker", lease["fence"],
                source_id=GOOGLE_SOURCE_ID, attempt_id="google-serp-attempt-0001",
                pool_name="serpapi", now=NOW,
            )
            self.assertEqual(admission.quota_snapshot("google_rss", now=NOW)["daily"]["used"], 1)
            self.assertEqual(admission.quota_snapshot("serpapi", now=NOW)["daily"]["used"], 1)
        finally:
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
