"""Google Trends contracts and local guard behavior, exercised offline only."""

import hashlib
import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.collections.store import CollectionStore, keyword_record, saved_source_record
from app.google_live_settings import GoogleLiveSettings
from app.jobs.distributed_worker import SourceWorkContext
from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.jobs.shared_cache import CacheGrant, SharedCache
from app.normal_wikimedia_live_policy import GOOGLE_STATE_KEY, normal_google_live_policy
from app.orchestration.normal_google_live_composition import (
    _GoogleLocalApproval,
    _youtube_scope_anchor,
    normal_google_live_composition,
)
from app.sources.catalog import GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.sources.google_trends.handler import GoogleTrendsDiscoveryHandler
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
from app.sources.live_gateway import (
    GatewayError,
    GatewayPage,
    LiveGatewayFailureDiagnostic,
)
from app.sources.refresh_eligibility import GOOGLE_REFRESH_STATE_KEY
from app.sources.serpapi.account import (
    SerpApiAccountClient,
    parse_account_response,
)
from app.sources.serpapi.errors import SerpApiError
from app.sources.serpapi.models import ProviderPage
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.storage.local_sqlite_snapshot_blobs import LocalSQLiteSnapshotBlobs

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
        "plan_renewal_date": "2026-12-14",
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
        self.assertEqual(snapshot.plan_renewal_date, "2026-12-14")
        self.assertNotIn(FAKE_KEY, repr(snapshot))
        self.assertNotIn("private@example.invalid", repr(snapshot))
        self.assertEqual(set(snapshot.public_metadata()), {
            "observed_at", "monthly_limit", "monthly_remaining", "total_remaining",
            "month_to_date_searches", "current_hour_searches", "hourly_limit",
            "plan_renewal_date",
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
    async def test_google_refreshes_stale_cache_once_per_eligible_user_slot(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        atomic = LocalSQLiteAtomicStateStore.for_local_live(root / "state.sqlite")
        cache = SharedCache(atomic)
        current = [datetime(2026, 11, 16, 3, 5, tzinfo=timezone.utc)]

        class TrendsGateway:
            data_mode = "LIVE"

            def __init__(self):
                self.calls = []

            async def fetch_page(self, *, context, on_page_receipt, **_kwargs):
                self.calls.append(context)
                payload = _trending_response([_trend_row()])
                page = parse_trending_now_response(
                    payload, request=context, sensitive_key=FAKE_KEY
                )
                gateway_page = GatewayPage(GOOGLE_SOURCE_ID, "LIVE", page)
                on_page_receipt(gateway_page)
                return gateway_page

        gateway = TrendsGateway()
        budget_checks = []

        async def check_budget():
            budget_checks.append(current[0])
            return {"budget_status": "AVAILABLE", "total_remaining": 240}

        handler = GoogleTrendsDiscoveryHandler(
            trends_gateway=gateway, policy_fingerprint="a" * 64,
            utcnow=lambda: current[0], cache=cache,
            resolve_cache_grant=lambda _source, now: CacheGrant(
                GOOGLE_SOURCE_ID, "NORMAL_GOOGLE_LIVE_V1", now + timedelta(days=7),
                3600, 7 * 24 * 3600, 7 * 24 * 3600, allowed=True,
            ),
            force_refresh=True, atomic_store=atomic,
            allow_initial_acceptance=True, ensure_paid_budget=check_budget,
        )

        async def fetch_at(index, local_time):
            current[0] = local_time.astimezone(timezone.utc)
            context = SourceWorkContext(
                f"{index:032x}", "test-worker", 1, GOOGLE_SOURCE_ID, "a" * 64,
                current[0] + timedelta(minutes=5), None, lambda _value: 1,
                lambda: "ACTIVE",
            )
            return await handler(context)

        try:
            first = await fetch_at(1, datetime(2026, 11, 16, 10, 5, tzinfo=ZoneInfo(
                "Asia/Ho_Chi_Minh")))
            self.assertEqual(first["delivery_state"], "FRESH")
            self.assertEqual(first["record_count"], 1)
            self.assertEqual(first["coverage"]["eligible_slot"], "INITIAL_ACCEPTANCE")
            self.assertEqual(gateway.calls[0].force_refresh, True)

            same_slot = await fetch_at(2, datetime(2026, 11, 16, 10, 6, tzinfo=ZoneInfo(
                "Asia/Ho_Chi_Minh")))
            self.assertEqual(same_slot["delivery_state"], "CACHED")
            self.assertEqual(len(gateway.calls), 1)

            too_soon = await fetch_at(3, datetime(2026, 11, 16, 15, 5, tzinfo=ZoneInfo(
                "Asia/Ho_Chi_Minh")))
            self.assertEqual(too_soon["delivery_state"], "CACHED")
            self.assertEqual(too_soon["coverage"]["refresh_state"], "NOT_YET_ELIGIBLE")
            self.assertEqual(len(gateway.calls), 1)

            evening = await fetch_at(4, datetime(2026, 11, 16, 23, 5, tzinfo=ZoneInfo(
                "Asia/Ho_Chi_Minh")))
            self.assertEqual(evening["delivery_state"], "FRESH")
            self.assertEqual(evening["coverage"]["eligible_slot"],
                             "2026-11-16T23:00:00+07:00")

            next_morning = await fetch_at(5, datetime(2026, 11, 17, 7, 5, tzinfo=ZoneInfo(
                "Asia/Ho_Chi_Minh")))
            self.assertEqual(next_morning["delivery_state"], "FRESH")
            self.assertEqual(next_morning["coverage"]["eligible_slot"],
                             "2026-11-17T07:00:00+07:00")
            self.assertEqual(len(gateway.calls), 3)
            self.assertEqual(len(budget_checks), 3)
            self.assertTrue(all(call.force_refresh for call in gateway.calls))
            state = atomic.transact(
                GOOGLE_REFRESH_STATE_KEY, lambda value: (value, value)
            )
            self.assertEqual(state["last_slot"], "2026-11-17T07:00:00+07:00")
        finally:
            directory.cleanup()

    async def test_google_failure_returns_safe_transport_diagnostic(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        atomic = LocalSQLiteAtomicStateStore.for_local_live(root / "state.sqlite")

        class TrendsGateway:
            data_mode = "LIVE"

            async def fetch_page(self, **_kwargs):
                diagnostic = LiveGatewayFailureDiagnostic(
                    GOOGLE_SOURCE_ID, "BODY_STREAM", 200, "ReadError", 23,
                    1_000_000, "sanitized-attempt-id", "UPSTREAM_ERROR",
                )
                raise GatewayError("UPSTREAM_ERROR", diagnostic=diagnostic)

        async def check_budget():
            return {"budget_status": "AVAILABLE", "total_remaining": 10}

        handler = GoogleTrendsDiscoveryHandler(
            trends_gateway=TrendsGateway(), policy_fingerprint="a" * 64,
            utcnow=lambda: NOW, cache=SharedCache(atomic),
            resolve_cache_grant=lambda _source, now: CacheGrant(
                GOOGLE_SOURCE_ID, "NORMAL_GOOGLE_LIVE_V1", now + timedelta(days=7),
                3600, 7 * 24 * 3600, 7 * 24 * 3600, allowed=True,
            ),
            atomic_store=atomic, allow_initial_acceptance=True,
            ensure_paid_budget=check_budget,
        )
        context = SourceWorkContext(
            "diagnostic-job", "test-worker", 1, GOOGLE_SOURCE_ID, "a" * 64,
            NOW + timedelta(minutes=5), None, lambda _value: 1, lambda: "ACTIVE",
        )

        try:
            result = await handler(context)
        finally:
            directory.cleanup()

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error_code"], "UPSTREAM_ERROR")
        self.assertEqual(result["coverage"]["transport_diagnostic"], {
            "source_id": GOOGLE_SOURCE_ID,
            "failure_phase": "BODY_STREAM",
            "http_status": 200,
            "exception_class": "ReadError",
            "bytes_received": 23,
            "response_size_limit": 1_000_000,
            "attempt_id": "sanitized-attempt-id",
            "error_code": "UPSTREAM_ERROR",
        })

    async def test_cache_only_google_discovery_serves_saved_records_without_paid_checks(self):
        candidate = _candidate("portable outdoor blanket", ["Home & Garden"])
        candidate.update({
            "source_id": GOOGLE_SOURCE_ID,
            "provider_id": "serpapi_google_trends_trending_now",
            "data_mode": "LIVE",
            "delivery_state": "CACHED",
            "fetched_at": "2026-11-15T17:50:24.177260+00:00",
            "provider_created_at": "2026-11-15T16:14:45+00:00",
        })
        cached_payload = {
            "status": "SUCCESS", "data_mode": "LIVE", "delivery_state": "CACHED",
            "records": [candidate], "record_count": 1,
            "coverage": {"provider_record_count": 1}, "quality_flags": [],
        }

        class Cache:
            def get(self, _scope, *, grant, now):
                return {"status": "STALE", "fetched_at": "2026-11-15T17:50:23.705698+00:00",
                        "payload": cached_payload}

            def put(self, *_args, **_kwargs):
                raise AssertionError("cache-only delivery must not refresh its saved age")

        grant = CacheGrant(
            GOOGLE_SOURCE_ID, "NORMAL_GOOGLE_LIVE_V1", NOW + timedelta(hours=1),
            3600, 7200, 7200, allowed=True,
        )

        async def paid_budget_must_not_be_checked():
            raise AssertionError("cache delivery must not call the paid account API")

        handler = GoogleTrendsDiscoveryHandler(
            policy_fingerprint="a" * 64, utcnow=lambda: NOW, cache=Cache(),
            resolve_cache_grant=lambda _source, _now: grant,
            ensure_paid_budget=paid_budget_must_not_be_checked,
            data_mode="LIVE",
        )
        context = SourceWorkContext(
            "cache-only-job", "worker", 1, GOOGLE_SOURCE_ID, "a" * 64,
            NOW + timedelta(minutes=5), None, lambda _value: 1, lambda: "ACTIVE",
        )

        result = await handler(context)

        self.assertEqual(handler.__call_mode__(), "LIVE")
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["delivery_state"], "CACHED")
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["records"][0]["delivery_state"], "CACHED")
        self.assertEqual(result["records"][0]["fetched_at"],
                         "2026-11-15T17:50:24.177260+00:00")
        self.assertEqual(result["coverage"]["shared_cache_state"], "STALE")
        self.assertEqual(result["records"][0]["serp_verification"]["status"],
                         "NOT_CONFIGURED")

    async def test_cache_only_google_cache_miss_is_unavailable_not_empty_success(self):
        class Cache:
            def get(self, *_args, **_kwargs):
                return {"status": "UNAVAILABLE", "reason": "CACHE_MISS"}

        grant = CacheGrant(
            GOOGLE_SOURCE_ID, "NORMAL_GOOGLE_LIVE_V1", NOW + timedelta(hours=1),
            3600, 7200, 7200, allowed=True,
        )
        handler = GoogleTrendsDiscoveryHandler(
            policy_fingerprint="a" * 64, utcnow=lambda: NOW, cache=Cache(),
            resolve_cache_grant=lambda _source, _now: grant, data_mode="LIVE",
        )
        context = SourceWorkContext(
            "cache-miss-job", "worker", 1, GOOGLE_SOURCE_ID, "a" * 64,
            NOW + timedelta(minutes=5), None, lambda _value: 1, lambda: "ACTIVE",
        )

        result = await handler(context)

        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["error_code"], "SOURCE_NOT_CONFIGURED")
        self.assertEqual(result["record_count"], 0)
        self.assertEqual(result["coverage"]["attempted_calls"], 0)

    async def test_cached_trends_candidates_can_be_serp_verified_without_refreshing_trends(self):
        candidate = _candidate("portable outdoor blanket", ["Home & Garden"], increase=900)
        candidate.update({
            "source_id": GOOGLE_SOURCE_ID,
            "provider_id": "serpapi_google_trends_trending_now",
            "transport_id": "serpapi",
            "data_mode": "LIVE",
            "delivery_state": "FRESH",
            "fetched_at": "2026-11-15T11:55:02+00:00",
        })
        cached_payload = {
            "status": "SUCCESS", "data_mode": "LIVE", "delivery_state": "FRESH",
            "records": [candidate], "record_count": 1,
            "coverage": {"provider_record_count": 1, "archive_search_id": "archived-search"},
            "quality_flags": [],
        }

        class Cache:
            def get(self, _scope, *, grant, now):
                self.grant, self.now = grant, now
                return {
                    "status": "STALE",
                    "fetched_at": "2026-11-15T09:55:02+00:00",
                    "payload": cached_payload,
                }

            def put(self, *_args, **_kwargs):
                raise AssertionError("cached data age must not be refreshed")

        class TrendsGateway:
            data_mode = "LIVE"

            async def fetch_page(self, **_kwargs):
                raise AssertionError("cached discovery must not repeat Trending Now")

        class SerpGateway:
            data_mode = "LIVE"

            def __init__(self):
                self.queries = []

            async def fetch_page(self, *, context, **_kwargs):
                self.queries.append(context.canonical_query)
                page = ProviderPage(
                    items=({"link": "https://example.com/item", "title": "Example",
                            "snippet": "Result snippet", "position": 1},),
                    next_offset=None, http_status=200, cache_state="FRESH",
                    provider_created_at=NOW,
                )
                return GatewayPage(GOOGLE_SOURCE_ID, "LIVE", page)

        grant = CacheGrant(
            GOOGLE_SOURCE_ID, "NORMAL_GOOGLE_LIVE_V1", NOW + timedelta(hours=1),
            3600, 7200, 7200, allowed=True,
        )
        cache, serp = Cache(), SerpGateway()
        paid_budget_checks = []

        async def check_paid_budget():
            paid_budget_checks.append("checked")
            return {"monthly_remaining": 200}

        handler = GoogleTrendsDiscoveryHandler(
            trends_gateway=TrendsGateway(), policy_fingerprint="a" * 64,
            utcnow=lambda: NOW, cache=cache,
            resolve_cache_grant=lambda _source, _now: grant,
            serp_gateway=serp,
            ensure_paid_budget=check_paid_budget,
        )
        context = SourceWorkContext(
            "job-1", "worker", 1, GOOGLE_SOURCE_ID, "a" * 64,
            NOW + timedelta(minutes=5), None, lambda _value: 1, lambda: "ACTIVE",
        )

        result = await handler(context)

        self.assertEqual(serp.queries, ["portable outdoor blanket"])
        self.assertEqual(len(paid_budget_checks), 1)
        self.assertEqual(result["delivery_state"], "CACHED")
        self.assertEqual(result["records"][0]["fetched_at"], "2026-11-15T11:55:02+00:00")
        self.assertEqual(result["records"][0]["delivery_state"], "CACHED")
        self.assertEqual(result["coverage"]["shared_cache_state"], "STALE")
        self.assertIn("STALE_SHARED_CACHE_REUSED", result["quality_flags"])
        self.assertEqual(result["records"][0]["serp_verification"]["status"], "VERIFIED")
        self.assertEqual(result["coverage"]["archive_search_id"], "archived-search")

    async def test_resumed_trends_receipt_skips_already_attempted_serp_candidate(self):
        failed = _candidate("iran attacks bahrain", ["News"], increase=1000)
        failed["serp_verification"] = {
            "status": "FAILED", "verification_mode": "GOOGLE_SERP",
            "query": "iran attacks bahrain", "error_code": "UPSTREAM_ERROR", "results": [],
        }
        pending = _candidate("portable outdoor blanket", ["Home & Garden"], increase=900)
        pending["serp_verification"] = {"status": "NOT_CONFIGURED", "results": []}
        second_pending = _candidate("folding solar lantern", ["Outdoors"], increase=800)
        second_pending["serp_verification"] = {"status": "NOT_CONFIGURED", "results": []}
        saved_result = {
            "status": "SUCCESS", "data_mode": "LIVE", "delivery_state": "CACHED",
            "records": [failed, pending, second_pending], "record_count": 3,
            "coverage": {"archive_search_id": "archived-search"},
            "quality_flags": [],
        }

        class TrendsGateway:
            data_mode = "LIVE"

            async def fetch_page(self, **_kwargs):
                raise AssertionError("resuming a receipt must not repeat discovery")

        class SerpGateway:
            data_mode = "LIVE"

            def __init__(self):
                self.queries = []

            async def fetch_page(self, *, context, **_kwargs):
                self.queries.append(context.canonical_query)
                page = ProviderPage(
                    items=({"link": "https://example.com/item", "title": "Example",
                            "snippet": "Result snippet", "position": 1},),
                    next_offset=None, http_status=200, cache_state="FRESH",
                    provider_created_at=NOW,
                )
                return GatewayPage(GOOGLE_SOURCE_ID, "LIVE", page)

        serp = SerpGateway()
        context = SourceWorkContext(
            "job-resume", "worker", 2, GOOGLE_SOURCE_ID, "a" * 64,
            NOW + timedelta(minutes=5),
            {"progress": {"google_trends_receipt": {"result": saved_result}}},
            lambda _value: 1, lambda: "ACTIVE",
        )
        handler = GoogleTrendsDiscoveryHandler(
            trends_gateway=TrendsGateway(), policy_fingerprint="a" * 64,
            utcnow=lambda: NOW, serp_gateway=serp,
            remaining_paid_attempts=lambda: 1,
        )

        result = await handler(context)

        self.assertEqual(serp.queries, ["portable outdoor blanket"])
        self.assertEqual(result["delivery_state"], "CACHED")
        self.assertEqual(result["coverage"]["serp_verification_attempted"], 2)
        self.assertEqual(result["coverage"]["serp_verification_failed_count"], 1)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["records"][0]["serp_verification"]["error_code"],
                         "UPSTREAM_ERROR")
        self.assertEqual(result["records"][1]["serp_verification"]["status"], "VERIFIED")

    def test_google_local_approval_is_stable_during_a_guarded_attempt(self):
        approval = _GoogleLocalApproval(
            GOOGLE_SOURCE_ID, enabled=True, clock=lambda: NOW
        )
        first = approval.current(GOOGLE_SOURCE_ID)
        self.assertIsNotNone(first)
        self.assertIs(approval.current(GOOGLE_SOURCE_ID), first)
        self.assertIsNone(approval.current("bing_web_serp"))

    def test_youtube_initial_acceptance_scope_uses_actual_attempt_timestamp(self):
        attempt = "2026-11-16T16:53:18+00:00"
        self.assertEqual(
            _youtube_scope_anchor({
                "last_slot": "INITIAL_ACCEPTANCE",
                "last_attempt_at": attempt,
            }).isoformat(),
            attempt,
        )
        with self.assertRaises(ValueError):
            _youtube_scope_anchor({"last_slot": "INITIAL_ACCEPTANCE"})

    async def test_owner_acceptance_preserves_expired_blobs_without_bypassing_schedule(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "google-state"
            state_dir.mkdir(mode=0o700)
            atomic = LocalSQLiteAtomicStateStore.for_local_live(state_dir / "state.sqlite")
            blobs = LocalSQLiteSnapshotBlobs(atomic)
            expired = blobs.put(
                [{"retained_fixture": "existing"}],
                now=NOW - timedelta(days=10),
                retain_until=NOW - timedelta(days=9),
            )
            collection_store = CollectionStore(
                root / "collections.sqlite", data_mode_scope="LIVE"
            )

            class NeverReadCredentials:
                def resolve(self, _reference):
                    raise AssertionError("credentials must not be read without LIVE approval")

            async with normal_google_live_composition(
                user_agent="TrendScoutTest/1.0 (https://example.invalid)",
                state_directory=state_dir,
                google_settings=GoogleLiveSettings(),
                credential_provider=NeverReadCredentials(),
                collection_store=collection_store,
                utcnow=lambda: NOW,
                preserve_expired_data=True,
                youtube_max_queries=1,
            ) as facade:
                schedule = facade.refresh_schedule_metadata()["sources"][YOUTUBE_SOURCE_ID]
                self.assertFalse(schedule["initial_acceptance_available"])
                self.assertIsNotNone(schedule["next_eligible_at"])
                self.assertIsNone(facade.youtube_retention_cleanup)

            with closing(atomic._connect()) as connection:
                retained = connection.execute(
                    "SELECT 1 FROM source_snapshot_blobs WHERE digest=?",
                    (expired["digest"],),
                ).fetchone()
            self.assertIsNotNone(retained)

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
            serpapi_daily_budget=3,
            serpapi_monthly_budget=3,
            rss_daily_budget=1,
            rss_monthly_budget=1,
            fetch_starts_per_hour=2,
        )
        state_dir = root / "google-state"
        state_dir.mkdir(mode=0o700)
        atomic_state = LocalSQLiteAtomicStateStore.for_local_live(
            state_dir / "state.sqlite"
        )
        old_policy = normal_google_live_policy(
            serpapi_daily_budget=3, serpapi_monthly_budget=3,
            rss_daily_budget=1, rss_monthly_budget=1,
            fetch_starts_per_hour=1,
        )
        old_admission = PublicAdmission(
            atomic_state, old_policy, state_key=GOOGLE_STATE_KEY
        )
        old_state = {
            "schema": 1, "policy_digest": old_admission.policy_digest,
            "operational_enabled": True, "jobs": {}, "keys": {},
            "starts": [NOW.isoformat()], "quota": {}, "inflight": {},
        }
        atomic_state.transact(GOOGLE_STATE_KEY, lambda _state: (old_state, None))
        try:
            async with normal_google_live_composition(
                user_agent="TrendScoutTest/1.0 (https://example.invalid)",
                state_directory=state_dir,
                google_settings=config,
                credential_provider=NeverReadCredentials(),
                collection_store=store,
                utcnow=lambda: NOW,
            ) as facade:
                self.assertTrue(facade.normal_google_live)
                self.assertEqual(facade.data_mode, "LIVE")
                self.assertEqual(facade.google_capabilities["trends_discovery"],
                                 "CACHE_ONLY")
                self.assertEqual(facade.google_capabilities["rss_discovery"],
                                 "NOT_CONFIGURED")
                self.assertEqual(facade.google_capabilities["serp_verification"],
                                 "NOT_CONFIGURED")
                self.assertEqual(facade.google_account_quota_state["status"],
                                 "NOT_CONFIGURED")
                self.assertEqual(facade.admission.policy.pools["serpapi"].daily_requests, 3)
                self.assertEqual(facade.admission.policy.pools["google_rss"].daily_requests, 1)
                self.assertEqual(facade.admission.policy.max_starts_per_window, 2)
                self.assertIn(YOUTUBE_SOURCE_ID, facade.admission.policy.enabled_sources)
                self.assertFalse(facade.normal_youtube_live)
                self.assertEqual(facade.youtube_capabilities["live_status"], "CACHE_ONLY")
                self.assertEqual(facade.youtube_quota_metadata()["provider_quota"]["status"],
                                 "UNKNOWN")
            migrated = atomic_state.transact(GOOGLE_STATE_KEY, lambda value: (value, value))
            self.assertEqual(migrated["starts"], old_state["starts"])
            self.assertEqual(migrated["policy_migration_audit"][0]["changed_admission_limits"],
                             ["max_starts_per_window"])
            self.assertEqual(migrated["policy_migration_audit"][1]["changed_admission_limits"],
                             ["enabled_sources"])
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

    def test_cached_google_reclassification_preserves_source_and_bing_evidence(self):
        common = {
            "data_source": "GOOGLE_TRENDS",
            "canonical_keyword": "portable outdoor blanket",
            "canonical_keyword_identity": hashlib.sha256(
                b"portable outdoor blanket"
            ).hexdigest(),
            "provider_id": "serpapi_google_trends_trending_now",
            "transport_id": "serpapi", "discovery_mode": "SERPAPI_TRENDING_NOW",
            "region": "US", "language": "en", "time_window_hours": 24,
            "fetched_at": NOW.isoformat(), "delivery_state": "FRESH",
            "source_url": "https://trends.google.com/trending?geo=US",
            "quality_flags": [], "trend_clusters": [{"trend_cluster_id": "cluster-1"}],
            "bing_verification": {"status": "VERIFIED", "query": "portable outdoor blanket"},
            "bing_verification_history": [{"status": "VERIFIED"}],
        }
        initial = keyword_record(common, GOOGLE_SOURCE_ID, "LIVE")
        saved = {
            "source": initial["source"], "keyword": initial["keyword"],
            "project": initial["project"], "observed_at": initial["observed_at"],
            "data_mode": initial["data_mode"], "evidence": initial["evidence"],
        }
        replayed = keyword_record(saved_source_record(saved), GOOGLE_SOURCE_ID, "LIVE")

        self.assertIsNotNone(replayed)
        self.assertEqual(replayed["id"], initial["id"])
        evidence = json.loads(replayed["evidence"])
        self.assertEqual(evidence["bing_verification"], common["bing_verification"])
        self.assertEqual(evidence["bing_verification_history"], common["bing_verification_history"])

    def test_google_pool_limits_are_separate_and_validated(self):
        policy = normal_google_live_policy(serpapi_daily_budget=3,
                                           serpapi_monthly_budget=90,
                                           rss_daily_budget=10,
                                           rss_monthly_budget=300)
        self.assertEqual(policy.pools["serpapi"].daily_requests, 3)
        self.assertEqual(policy.pools["serpapi"].monthly_requests, 90)
        self.assertEqual(policy.pools["google_rss"].daily_requests, 10)
        with self.assertRaises(ValueError):
            normal_google_live_policy(serpapi_daily_budget=11)

    def test_owner_authorized_serpapi_policy_migration_preserves_quota_and_audits(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        store = LocalSQLiteAtomicStateStore.for_local_live(root / "quota.sqlite")
        previous = normal_google_live_policy(
            serpapi_daily_budget=3, serpapi_monthly_budget=3,
            rss_daily_budget=1, rss_monthly_budget=1, fetch_starts_per_hour=2,
        )
        updated = normal_google_live_policy(
            serpapi_daily_budget=10, serpapi_monthly_budget=10,
            rss_daily_budget=1, rss_monthly_budget=1, fetch_starts_per_hour=2,
        )
        old_admission = PublicAdmission(store, previous, state_key=GOOGLE_STATE_KEY)
        new_admission = PublicAdmission(store, updated, state_key=GOOGLE_STATE_KEY)
        original_state = {
            "schema": 1, "policy_digest": old_admission.policy_digest,
            "operational_enabled": True, "jobs": {"keep": {"state": "FAILED"}},
            "keys": {"receipt": {"job_id": "keep"}}, "starts": [NOW.isoformat()],
            "quota": {
                "serpapi:day:2026-11-15": {"requests": 2, "cost": 2},
                "serpapi:month:2026-11": {"requests": 2, "cost": 2},
                "google_rss:day:2026-11-15": {"requests": 1, "cost": 1},
                "google_rss:month:2026-11": {"requests": 1, "cost": 1},
            },
            "inflight": {},
        }
        store.transact(GOOGLE_STATE_KEY, lambda _state: (original_state, None))
        try:
            audit = new_admission.migrate_bounded_quota_policy(
                previous,
                authorization_note="OWNER_AUTHORIZED_MAX_10_PAID_SEARCH_ATTEMPTS",
                now=NOW,
            )
            self.assertTrue(audit["quota_preserved"])
            self.assertEqual(audit["changed_pools"], ["serpapi"])
            serpapi_daily = new_admission.quota_snapshot("serpapi", now=NOW)["daily"]
            self.assertEqual(
                (serpapi_daily["limit"], serpapi_daily["used"],
                 serpapi_daily["remaining"]),
                (10, 2, 8),
            )
            state = store.transact(GOOGLE_STATE_KEY, lambda value: (value, value))
            self.assertEqual(state["quota"], original_state["quota"])
            self.assertEqual(state["jobs"], original_state["jobs"])
            self.assertEqual(state["keys"], original_state["keys"])
            self.assertEqual(state["starts"], original_state["starts"])
            self.assertEqual(state["policy_migration_audit"], [audit])

            rss_change = normal_google_live_policy(
                serpapi_daily_budget=10, serpapi_monthly_budget=10,
                rss_daily_budget=2, rss_monthly_budget=2, fetch_starts_per_hour=2,
            )
            rss_admission = PublicAdmission(store, rss_change, state_key=GOOGLE_STATE_KEY)
            with self.assertRaises(AdmissionDenied) as error:
                rss_admission.migrate_bounded_quota_policy(
                    updated,
                    authorization_note="SERPAPI_ONLY_MIGRATION_TEST",
                    now=NOW,
                )
            self.assertEqual(error.exception.code, "POLICY_MIGRATION_SCOPE_INVALID")
        finally:
            directory.cleanup()

    def test_local_cache_capacity_migration_preserves_old_admission_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalSQLiteAtomicStateStore.for_local_live(
                Path(directory) / "capacity.sqlite"
            )
            previous = normal_google_live_policy(fetch_starts_per_hour=1)
            updated = normal_google_live_policy(fetch_starts_per_hour=60)
            old_admission = PublicAdmission(store, previous, state_key=GOOGLE_STATE_KEY)
            new_admission = PublicAdmission(store, updated, state_key=GOOGLE_STATE_KEY)
            original = {
                "schema": 1, "policy_digest": old_admission.policy_digest,
                "operational_enabled": True, "jobs": {"old": {"state": "FAILED"}},
                "keys": {}, "starts": [NOW.isoformat()], "quota": {}, "inflight": {},
            }
            store.transact(GOOGLE_STATE_KEY, lambda _state: (original, None))

            audit = new_admission.migrate_bounded_quota_policy(
                previous,
                authorization_note="LOCAL_FETCH_CACHE_CAPACITY_POLICY_V1",
                now=NOW,
                allow_fetch_capacity_increase=True,
            )

            state = store.transact(GOOGLE_STATE_KEY, lambda value: (value, value))
            self.assertEqual(new_admission.policy.max_starts_per_window, 60)
            self.assertEqual(state["starts"], original["starts"])
            self.assertEqual(state["jobs"], original["jobs"])
            self.assertEqual(state["quota"], original["quota"])
            self.assertEqual(audit["changed_admission_limits"], ["max_starts_per_window"])

            smaller = PublicAdmission(
                store, normal_google_live_policy(fetch_starts_per_hour=30),
                state_key=GOOGLE_STATE_KEY,
            )
            with self.assertRaises(AdmissionDenied) as error:
                smaller.migrate_bounded_quota_policy(
                    updated,
                    authorization_note="CAPACITY_DECREASE_MUST_FAIL",
                    now=NOW,
                    allow_fetch_capacity_increase=True,
                )
            self.assertEqual(error.exception.code, "POLICY_MIGRATION_SCOPE_INVALID")

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
