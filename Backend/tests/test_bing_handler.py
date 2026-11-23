import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.collections.service import CollectionService
from app.collections.store import CollectionStore, keyword_record
from app.jobs.distributed_worker import SourceWorkContext
from app.jobs.public_admission import (
    MANUAL_SOURCE_REFRESH_MARKER,
    OWNER_FRESH_ACCEPTANCE_MARKER,
)
from app.jobs.shared_cache import CacheGrant, SharedCache
from app.sources.bing_search.handler import BingSavedCandidateHandler
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID
from app.sources.google_trends.trending_now import normalize_keyword
from app.sources.refresh_eligibility import BING_REFRESH_STATE_KEY, reserve_manual_source_slot
from app.sources.live_gateway import GatewayPage
from app.sources.serpapi.models import ProviderPage
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore

NOW = datetime(2026, 11, 16, 0, 5, tzinfo=timezone.utc)  # 07:05 Asia/Ho_Chi_Minh
POLICY = "a" * 64


def insert_candidate(store, keyword="Pleated tote"):
    _, identity = normalize_keyword(keyword)
    raw = {
        "canonical_keyword": keyword,
        "canonical_keyword_identity": hashlib.sha256(identity.encode()).hexdigest(),
        "data_source": "GOOGLE_TRENDS",
        "provider_id": "serpapi_google_trends_trending_now",
        "transport_id": "serpapi",
        "discovery_mode": "SERPAPI_TRENDING_NOW",
        "region": "US", "language": "en", "time_window_hours": 24,
        "fetched_at": (NOW - timedelta(minutes=5)).isoformat(),
        "provider_data_at": (NOW - timedelta(minutes=10)).isoformat(),
        "provider_created_at": (NOW - timedelta(minutes=4)).isoformat(),
        "source_url": "https://trends.google.com/trending?geo=US",
        "delivery_state": "FRESH",
        "trend_clusters": [{"active": True, "increase_percentage": 400,
                            "categories": [{"name": "Fashion"}]}],
        "quality_flags": [],
    }
    value = keyword_record(raw, GOOGLE_SOURCE_ID, "LIVE")
    assert value
    with store.connect() as db:
        db.execute("""INSERT INTO keywords(id,keyword,normalized,source,project,observed_at,
            data_mode,evidence,context,eligible) VALUES(?,?,?,?,?,?,?,?,?,1)""",
            tuple(value[key] for key in (
                "id", "keyword", "normalized", "source", "project", "observed_at",
                "data_mode", "evidence", "context",
            )))
    return value


class FakeBingGateway:
    data_mode = "LIVE"

    def __init__(self):
        self.calls = []
        self.items = ({
            "link": "https://example.com/pleated-tote?utm_source=serpapi",
            "title": "Pleated tote bags",
            "snippet": "Organic result snippet",
            "position": 2,
        },)

    async def fetch_page(self, *, context, cursor, on_page_receipt=None, **kwargs):
        self.calls.append({"context": context, "cursor": cursor, **kwargs})
        page = ProviderPage(self.items, None, 200, "UNKNOWN", NOW - timedelta(minutes=1))
        gateway_page = GatewayPage(BING_SOURCE_ID, "LIVE", page)
        if on_page_receipt is not None:
            on_page_receipt(gateway_page)
        return gateway_page


def work_context(now=NOW, *, checkpointed=None, request_metadata=None, job_id="b" * 32):
    checkpointed = checkpointed if checkpointed is not None else []
    return SourceWorkContext(
        job_id=job_id, owner="test-worker", fence=1, source_id=BING_SOURCE_ID,
        plan_fingerprint=POLICY, job_expires_at=now + timedelta(minutes=5),
        resume_snapshot=None, checkpoint=lambda value: checkpointed.append(value) or 1,
        heartbeat=lambda: "ok",
        request_metadata=request_metadata or {"sources": [BING_SOURCE_ID]},
    )


class BingSavedCandidateHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.collections = CollectionStore(root / "collections.sqlite", data_mode_scope="LIVE")
        self.atomic = LocalSQLiteAtomicStateStore.for_local_live(root / "state.sqlite")
        self.candidate = insert_candidate(self.collections)
        self.gateway = FakeBingGateway()
        self.budget_calls = []
        self.handler = BingSavedCandidateHandler(
            gateway=self.gateway, collection_store=self.collections, atomic_store=self.atomic,
            policy_fingerprint=POLICY,
            ensure_paid_budget=lambda: self._budget(), utcnow=lambda: NOW,
        )

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def _budget(self):
        self.budget_calls.append(True)
        return {"budget_status": "AVAILABLE", "total_remaining": 246}

    async def test_one_saved_candidate_runs_one_request_and_persists_linked_evidence(self):
        receipts = []
        result = await self.handler(work_context(checkpointed=receipts))
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(len(self.budget_calls), 1)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["delivery_state"], "FRESH")
        record = result["records"][0]
        self.assertEqual(record["candidate_keyword_id"], self.candidate["id"])
        verification = record["bing_verification"]
        self.assertEqual(verification["verification_mode"], "BING_SERP")
        self.assertEqual(verification["provider_id"], "serpapi_bing")
        self.assertEqual(verification["results"][0]["canonical_url"],
                         "https://example.com/pleated-tote")
        self.assertEqual(verification["limitations"][0], "SERP_POSITION_IS_NOT_SEARCH_VOLUME")
        self.assertEqual(len(receipts), 1)

        collection_result = await CollectionService(self.collections).process_fetch(
            "bing-fetch-test-0001", [{
                "source_id": BING_SOURCE_ID, **result,
            }],
        )
        self.assertEqual(collection_result["status"], "COMPLETED")
        with self.collections.connect() as db:
            row = db.execute("SELECT source,observed_at,evidence FROM keywords WHERE id=?",
                             (self.candidate["id"],)).fetchone()
            snapshot = db.execute("SELECT source_manifest FROM dataset_snapshots WHERE dataset_id=?",
                                  ("bing-fetch-test-0001",)).fetchone()
        self.assertEqual(row["source"], GOOGLE_SOURCE_ID)
        self.assertEqual(row["observed_at"], self.candidate["observed_at"])
        evidence = json.loads(row["evidence"])
        self.assertEqual(evidence["bing_verification"]["query"], "Pleated tote")
        self.assertEqual(len(evidence["bing_verification_history"]), 1)
        self.assertIn(BING_SOURCE_ID, json.loads(snapshot["source_manifest"])[0]["source_id"])

    async def test_outside_slot_returns_no_data_without_budget_or_provider_call(self):
        later = NOW + timedelta(hours=3)
        later_handler = BingSavedCandidateHandler(
            gateway=self.gateway, collection_store=self.collections, atomic_store=self.atomic,
            policy_fingerprint=POLICY,
            ensure_paid_budget=lambda: self._budget(), utcnow=lambda: later,
        )
        result = await later_handler(work_context(later))
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["error_code"], "NOT_YET_ELIGIBLE")
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.budget_calls, [])

    async def test_initial_acceptance_is_one_shot_and_requires_explicit_enablement(self):
        acceptance_time = NOW + timedelta(hours=2)
        handler = BingSavedCandidateHandler(
            gateway=self.gateway, collection_store=self.collections, atomic_store=self.atomic,
            policy_fingerprint=POLICY,
            ensure_paid_budget=lambda: self._budget(), utcnow=lambda: acceptance_time,
            allow_initial_acceptance=True,
        )
        accepted = await handler(work_context(acceptance_time))
        self.assertEqual(accepted["coverage"]["eligible_slot"], "INITIAL_ACCEPTANCE")
        self.assertEqual(len(self.gateway.calls), 1)

        repeated = await handler(work_context(acceptance_time + timedelta(minutes=1)))
        self.assertEqual(repeated["error_code"], "NOT_YET_ELIGIBLE")
        self.assertEqual(len(self.gateway.calls), 1)

    async def test_persisted_evidence_is_cache_first_and_preserves_original_time(self):
        result = await self.handler(work_context())
        await CollectionService(self.collections).process_fetch(
            "bing-fetch-cache-seed", [{"source_id": BING_SOURCE_ID, **result}],
        )
        later = NOW + timedelta(hours=2)
        cached_handler = BingSavedCandidateHandler(
            gateway=self.gateway, collection_store=self.collections, atomic_store=self.atomic,
            policy_fingerprint=POLICY,
            ensure_paid_budget=lambda: self._budget(), utcnow=lambda: later,
        )
        cached = await cached_handler(work_context(later))
        self.assertEqual(cached["delivery_state"], "STALE_CACHE")
        self.assertEqual(cached["records"][0]["bing_verification"]["delivery_state"],
                         "STALE_CACHE")
        self.assertEqual(cached["provider_request_count"], 0)
        self.assertEqual(cached["coverage"]["provider_request_count"], 0)
        self.assertEqual(cached["coverage"]["cache_state"], "STALE")
        self.assertEqual(cached["coverage"]["query"], "Pleated tote")
        self.assertEqual(cached["coverage"]["accepted_result_count"], 1)
        self.assertEqual(cached["coverage"]["candidate_source_id"], GOOGLE_SOURCE_ID)
        self.assertEqual(cached["records"][0]["bing_verification"]["fetched_at"],
                         result["records"][0]["bing_verification"]["fetched_at"])
        await CollectionService(self.collections).process_fetch(
            "bing-fetch-cached-delivery", [{"source_id": BING_SOURCE_ID, **cached}],
        )
        with self.collections.connect() as db:
            snapshot = db.execute(
                "SELECT fetched_at,source_manifest FROM dataset_snapshots WHERE dataset_id=?",
                ("bing-fetch-cached-delivery",),
            ).fetchone()
        self.assertEqual(snapshot["fetched_at"], cached["coverage"]["fetched_at"])
        self.assertEqual(
            json.loads(snapshot["source_manifest"])[0]["coverage"]["provider_request_count"],
            0,
        )
        self.assertEqual(len(self.gateway.calls), 1)

    async def test_no_persisted_google_candidates_is_not_an_empty_live_success(self):
        with self.collections.connect() as db:
            db.execute("DELETE FROM keywords WHERE id=?", (self.candidate["id"],))
        result = await self.handler(work_context())
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["error_code"], "NO_SAVED_CANDIDATE")
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.budget_calls, [])

    async def test_shared_google_candidate_loader_supports_bing_without_local_sqlite(self):
        evidence = json.loads(self.candidate["evidence"])
        shared_candidate = {
            "record_id": self.candidate["id"],
            "canonical_keyword": self.candidate["keyword"],
            "source_id": self.candidate["source"],
            "project": self.candidate["project"],
            "observed_at": self.candidate["observed_at"],
            "fetched_at": evidence["fetched_at"],
            "data_mode": "LIVE",
            "evidence": evidence,
        }
        handler = BingSavedCandidateHandler(
            gateway=self.gateway,
            collection_store=None,
            atomic_store=self.atomic,
            policy_fingerprint=POLICY,
            ensure_paid_budget=lambda: self._budget(),
            utcnow=lambda: NOW,
            allow_initial_acceptance=True,
            candidate_loader=lambda _now: (shared_candidate,),
        )

        result = await handler(work_context())

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["records"][0]["candidate_keyword_id"], self.candidate["id"])
        self.assertEqual(self.gateway.calls[0]["context"].canonical_query, "Pleated tote")

    async def test_same_job_google_checkpoint_supplies_bing_before_collections_processing(self):
        evidence = json.loads(self.candidate["evidence"])
        same_job_candidate = {
            "record_id": self.candidate["id"],
            "canonical_keyword": self.candidate["keyword"],
            "source_id": self.candidate["source"],
            "project": self.candidate["project"],
            "observed_at": self.candidate["observed_at"],
            "fetched_at": evidence["fetched_at"],
            "data_mode": "LIVE",
            "evidence": evidence,
        }
        older_shared_candidate = {
            **same_job_candidate,
            "fetched_at": (NOW - timedelta(minutes=40)).isoformat(),
        }
        handler = BingSavedCandidateHandler(
            gateway=self.gateway,
            collection_store=None,
            atomic_store=self.atomic,
            policy_fingerprint=POLICY,
            ensure_paid_budget=lambda: self._budget(),
            utcnow=lambda: NOW,
            shared_cache=SharedCache(self.atomic),
            resolve_cache_grant=lambda source_id, now: CacheGrant(
                source_id, "test-bing-cache", now + timedelta(days=1),
                3600, 7200, 86400, allowed=True,
            ),
            candidate_loader=lambda now: (older_shared_candidate,),
            checkpoint_candidate_loader=lambda context, now: (same_job_candidate,),
        )

        result = await handler(work_context(request_metadata={
            "sources": [GOOGLE_SOURCE_ID, BING_SOURCE_ID],
            "manual_source_refresh": MANUAL_SOURCE_REFRESH_MARKER,
        }))

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["records"][0]["candidate_keyword_id"], self.candidate["id"])
        self.assertEqual(result["coverage"]["candidate_fetched_at"],
                         same_job_candidate["fetched_at"])
        self.assertEqual(self.gateway.calls[0]["context"].canonical_query, "Pleated tote")

    async def test_manual_bing_cooldown_reports_exact_next_eligible_time(self):
        reserve_manual_source_slot(
            self.atomic, BING_REFRESH_STATE_KEY, NOW,
            job_id="a" * 32, error_code="BING_SCHEDULE_STATE_INVALID",
        )
        evidence = json.loads(self.candidate["evidence"])
        shared_candidate = {
            "record_id": self.candidate["id"],
            "canonical_keyword": self.candidate["keyword"],
            "source_id": self.candidate["source"],
            "project": self.candidate["project"],
            "observed_at": self.candidate["observed_at"],
            "fetched_at": evidence["fetched_at"],
            "data_mode": "LIVE",
            "evidence": evidence,
        }
        later = NOW + timedelta(hours=3)
        handler = BingSavedCandidateHandler(
            gateway=self.gateway,
            collection_store=None,
            atomic_store=self.atomic,
            policy_fingerprint=POLICY,
            ensure_paid_budget=lambda: self._budget(),
            utcnow=lambda: later,
            candidate_loader=lambda now: (shared_candidate,),
        )

        result = await handler(work_context(
            later, request_metadata={
                "sources": [GOOGLE_SOURCE_ID, BING_SOURCE_ID],
                "manual_source_refresh": MANUAL_SOURCE_REFRESH_MARKER,
            },
            job_id="c" * 32,
        ))

        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["error_code"], "MANUAL_REFRESH_COOLDOWN")
        self.assertEqual(result["coverage"]["next_eligible_at"], "2026-11-16T08:05:00+00:00")
        self.assertEqual(self.budget_calls, [])
        self.assertEqual(self.gateway.calls, [])

    async def test_owner_acceptance_bing_receipt_is_cached_for_a_new_provider_free_fetch(self):
        evidence = json.loads(self.candidate["evidence"])
        shared_candidate = {
            "record_id": self.candidate["id"],
            "canonical_keyword": self.candidate["keyword"],
            "source_id": self.candidate["source"],
            "project": self.candidate["project"],
            "observed_at": self.candidate["observed_at"],
            "fetched_at": evidence["fetched_at"],
            "data_mode": "LIVE",
            "evidence": evidence,
        }
        cache = SharedCache(self.atomic)
        current = [NOW]

        def grant_for(source_id, now):
            return CacheGrant(
                source_id, "D6_BING_ACCEPTANCE_CACHE_V1", now + timedelta(days=7),
                24 * 3600, 2 * 24 * 3600, 7 * 24 * 3600, allowed=True,
            )

        handler = BingSavedCandidateHandler(
            gateway=self.gateway, collection_store=None, atomic_store=self.atomic,
            policy_fingerprint=POLICY, ensure_paid_budget=lambda: self._budget(),
            utcnow=lambda: current[0], candidate_loader=lambda _now: (shared_candidate,),
            shared_cache=cache, resolve_cache_grant=grant_for,
        )
        sources = sorted({BING_SOURCE_ID, GOOGLE_SOURCE_ID,
                          "wikimedia_pageviews_top_per_country", "youtube_video_search"})
        first = await handler(work_context(
            request_metadata={"sources": sources,
                              "owner_fresh_acceptance": OWNER_FRESH_ACCEPTANCE_MARKER},
            job_id="1" * 32,
        ))
        self.assertEqual(first["delivery_state"], "FRESH")
        self.assertEqual(first["provider_request_count"], 1)
        self.assertTrue(first["coverage"]["owner_fresh_acceptance"])

        current[0] += timedelta(minutes=1)
        second = await handler(work_context(
            current[0], request_metadata={"sources": sources}, job_id="2" * 32,
        ))

        self.assertEqual(second["delivery_state"], "CACHED")
        self.assertEqual(second["provider_request_count"], 0)
        self.assertEqual(second["coverage"]["provider_request_count"], 0)
        self.assertEqual(second["coverage"]["cached_provider_request_count"], 1)
        self.assertEqual(second["records"][0]["bing_verification"]["fetched_at"],
                         first["records"][0]["bing_verification"]["fetched_at"])

        current[0] = NOW + timedelta(hours=9)
        third = await handler(work_context(
            current[0], request_metadata={
                "sources": sources,
                "manual_source_refresh": MANUAL_SOURCE_REFRESH_MARKER,
            }, job_id="3" * 32,
        ))

        self.assertEqual(third["status"], "SUCCESS")
        self.assertEqual(third["delivery_state"], "FRESH")
        self.assertEqual(third["provider_request_count"], 1)
        self.assertEqual(len(self.gateway.calls), 2)
        self.assertEqual(len(self.budget_calls), 2)


if __name__ == "__main__":
    unittest.main()
