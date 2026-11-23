"""Request-scoped Vercel Wikimedia LIVE worker on the shared Firestore database.

This first online slice deliberately exposes only Wikimedia. Other source
handlers remain unavailable until their provider approvals and budgets are
wired into a later change.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from app.discovery.contracts import ProcessingGrant
from app.google_live_settings import GoogleLiveSettings
from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.jobs.shared_cache import CacheGrant, SharedCache
from app.jobs.source_checkpoints import CheckpointGrant
from app.normal_wikimedia_live_policy import (
    GOOGLE_POLICY_VERSION,
    GOOGLE_STATE_KEY,
    normal_google_live_policy,
)
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.orchestration.firestore_shared_live_composition import _client_project_id
from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from app.sources.guarded_collector import CollectionLimits
from app.sources.live_gateway import SourceApproval
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.wikimedia.live_gateway import WikimediaTopPageGateway
from app.storage.firestore_atomic_state import FirestoreAtomicStateStore
from app.storage.firestore_snapshot_blobs import FirestoreSnapshotBlobs
from app.workers.d5_source_handlers import D5SourceHandlers
from app.workers.guarded_attempt import GuardedAttemptRunner

_CACHE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_POLICY_SYNC_AUTHORIZATION = "OWNER_AUTHORIZED_VERCEL_WIKIMEDIA_FIRESTORE_POLICY_SYNC"


class _VercelWikimediaApproval:
    """Short-lived backend approval scoped to the pinned Vercel Firestore worker."""

    def __init__(self, clock):
        self.clock = clock
        self._current = None

    def current(self, source_id):
        if source_id != WIKIMEDIA_SOURCE_ID:
            return None
        now = self.clock()
        if self._current is not None and self._current.expires_at > now:
            return self._current
        self._current = SourceApproval(
            source_id=source_id,
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            expires_at=now + timedelta(minutes=5),
            policy_version=GOOGLE_POLICY_VERSION,
            approval_id=f"{GOOGLE_POLICY_VERSION}:{source_id}:vercel",
            source_enabled=True,
            global_enabled=True,
            usage_approved=True,
            retention_approved=True,
            quota_approved=True,
            cost_approved=True,
            security_approved=True,
            egress_controls_verified=True,
            telemetry_controls_verified=True,
            scope="VERCEL_SHARED_LIVE_WIKIMEDIA",
            local_controls_verified=False,
        )
        return self._current


class _VercelWikimediaPolicySync:
    """Recognize and atomically reconcile only the previously shipped policies."""

    def __init__(self, store, *, target_policy, previous_policies, state_key, utcnow):
        self.store = store
        self.target = PublicAdmission(store, target_policy, state_key=state_key)
        self.previous_policies = previous_policies
        self.state_key = state_key
        self.utcnow = utcnow

    def _read(self):
        read_many = getattr(self.store, "read_many", None)
        if callable(read_many):
            return read_many([self.state_key])[self.state_key]
        return self.store.transact(self.state_key, lambda state: (state, state))

    def status(self):
        state = self._read()
        if not state:
            return "CURRENT"
        if (state.get("schema") == 1
                and state.get("policy_digest") == self.target.policy_digest):
            return "CURRENT" if state.get("operational_enabled") is True else "FEATURE_DISABLED"
        if state.get("operational_enabled") is not True:
            return "FEATURE_DISABLED"
        if (state.get("schema") != 1
                or not isinstance(state.get("jobs"), dict)
                or not isinstance(state.get("inflight"), dict)
                or not isinstance(state.get("quota"), dict)):
            return "POLICY_MISMATCH"
        matches = [label for label, policy in self.previous_policies.items()
                   if PublicAdmission._policy_digest(policy) == state.get("policy_digest")]
        if len(matches) != 1:
            return "POLICY_MISMATCH"
        return "READY_TO_SYNC"

    def prepare_for_fetch(self):
        status = self.status()
        if status == "CURRENT":
            return "CURRENT"
        if status != "READY_TO_SYNC":
            raise AdmissionDenied(status if status in {"FEATURE_DISABLED", "POLICY_MISMATCH"}
                                  else "POLICY_MISMATCH")

        state = self._read()
        matches = [
            name for name, policy in self.previous_policies.items()
            if PublicAdmission._policy_digest(policy) == state.get("policy_digest")
        ]
        if len(matches) != 1:
            raise AdmissionDenied("POLICY_MISMATCH")
        label = matches[0]
        previous = self.previous_policies[label]
        if label == "legacy_low_budget_without_bing_or_youtube":
            self.target.migrate_legacy_google_live_policy(
                previous,
                authorization_note=_POLICY_SYNC_AUTHORIZATION,
                now=self.utcnow(),
            )
        else:
            self.target.migrate_bounded_quota_policy(
                previous,
                authorization_note=_POLICY_SYNC_AUTHORIZATION,
                now=self.utcnow(),
                allow_fetch_capacity_increase=(
                    label == "previous_capacity_one_without_bing_or_youtube"
                ),
            )

        final_state = self._read()
        if (final_state.get("policy_digest") != self.target.policy_digest
                or final_state.get("operational_enabled") is not True):
            raise AdmissionDenied("POLICY_MISMATCH")
        return "RECONCILED"


def _vercel_previous_policy_candidates(google):
    common = {
        "serpapi_daily_budget": google.serpapi_daily_budget,
        "serpapi_monthly_budget": google.serpapi_monthly_budget,
        "rss_daily_budget": google.rss_daily_budget,
        "rss_monthly_budget": google.rss_monthly_budget,
        "fetch_starts_per_hour": google.fetch_starts_per_hour,
        "bing_daily_budget": google.bing_daily_budget,
    }
    return {
        "previous_without_bing": normal_google_live_policy(
            **common, bing_enabled=False, youtube_enabled=True
        ),
        "previous_without_youtube": normal_google_live_policy(
            **common, bing_enabled=True, youtube_enabled=False
        ),
        "previous_without_bing_or_youtube": normal_google_live_policy(
            **common, bing_enabled=False, youtube_enabled=False
        ),
        "previous_capacity_one_without_bing_or_youtube": normal_google_live_policy(
            **{**common, "fetch_starts_per_hour": 1},
            bing_enabled=False,
            youtube_enabled=False,
        ),
        # This is the exact pre-D6 ledger policy used by the existing local migration.
        "legacy_low_budget_without_bing_or_youtube": normal_google_live_policy(
            serpapi_daily_budget=10,
            serpapi_monthly_budget=10,
            rss_daily_budget=1,
            rss_monthly_budget=1,
            fetch_starts_per_hour=2,
            bing_enabled=False,
            youtube_enabled=False,
        ),
    }


def _unavailable_handler(source_id):
    async def unavailable(_context):
        return {
            "status": "UNAVAILABLE",
            "data_mode": "LIVE",
            "records": [],
            "record_count": 0,
            "error_code": "SOURCE_NOT_ENABLED_FOR_VERCEL",
            "coverage": {"provider_request_count": 0},
        }

    unavailable.__trendscout_data_mode__ = "LIVE"
    unavailable.__name__ = f"vercel_unavailable_{source_id}"
    return unavailable


@asynccontextmanager
async def vercel_wikimedia_live_composition(
    client,
    *,
    user_agent: str,
    utcnow=lambda: datetime.now(timezone.utc),
):
    """Yield a Firestore-backed Wikimedia worker; construction sends no provider request."""
    if os.environ.get("VERCEL") != "1" or not isinstance(user_agent, str) or not user_agent.strip():
        raise ValueError("Vercel Wikimedia LIVE requires its deployed runtime")
    if not callable(utcnow):
        raise ValueError("timezone-aware clock required")
    _client_project_id(client)

    google = GoogleLiveSettings.from_environment()
    policy = normal_google_live_policy(
        serpapi_daily_budget=google.serpapi_daily_budget,
        serpapi_monthly_budget=google.serpapi_monthly_budget,
        rss_daily_budget=google.rss_daily_budget,
        rss_monthly_budget=google.rss_monthly_budget,
        fetch_starts_per_hour=google.fetch_starts_per_hour,
        # Share the same quota-policy shape as the Windows 2 Firestore LIVE
        # launcher. Bing remains unavailable below; this reserves the shared
        # pool without enabling its adapter or making provider requests.
        bing_enabled=True,
        bing_daily_budget=google.bing_daily_budget,
        youtube_enabled=True,
    )
    state_store = FirestoreAtomicStateStore(client)
    snapshot_blobs = FirestoreSnapshotBlobs(client)
    admission = PublicAdmission(state_store, policy, state_key=GOOGLE_STATE_KEY)
    policy_sync = _VercelWikimediaPolicySync(
        state_store,
        target_policy=policy,
        previous_policies=_vercel_previous_policy_candidates(google),
        state_key=GOOGLE_STATE_KEY,
        utcnow=utcnow,
    )
    runner = GuardedAttemptRunner(admission, clock=utcnow)
    approvals = _VercelWikimediaApproval(utcnow)
    from app.orchestration.normal_wikimedia_live_composition import _PersistentOneSecondPacer

    pacer = _PersistentOneSecondPacer(state_store, clock=utcnow)
    wiki = await WikimediaTopPageGateway.controlled_network(
        approvals,
        runner,
        user_agent=user_agent,
        clock=utcnow,
        before_request=pacer,
        on_rate_limited=pacer.defer,
    )
    try:
        cache = SharedCache(state_store, payload_blobs=snapshot_blobs, max_inline_bytes=500_000)

        def resolve_cache_grant(source_id, now):
            if source_id not in policy.enabled_sources:
                return None
            return CacheGrant(
                source_id,
                GOOGLE_POLICY_VERSION,
                now + timedelta(seconds=_CACHE_RETENTION_SECONDS),
                max_fresh_seconds=(24 * 60 * 60 if source_id == WIKIMEDIA_SOURCE_ID
                                   else 60 * 60),
                max_stale_seconds=_CACHE_RETENTION_SECONDS,
                max_retention_seconds=_CACHE_RETENTION_SECONDS,
                allowed=True,
            )

        def resolve_checkpoint(source_id, now):
            if source_id not in policy.enabled_sources:
                return None
            return CheckpointGrant(
                source_id,
                "LIVE",
                GOOGLE_POLICY_VERSION,
                now + timedelta(seconds=policy.receipt_seconds),
                policy.receipt_seconds,
                True,
            )

        def resolve_processing(now):
            return (ProcessingGrant(
                WIKIMEDIA_SOURCE_ID, True, False,
                now + timedelta(minutes=15), GOOGLE_POLICY_VERSION,
            ),)

        built_handlers = D5SourceHandlers(
            query_collectors={
                "google_web_serp": None,
                "bing_web_serp": None,
                "youtube_video_search": None,
            },
            wikimedia=wiki,
            plan=DEFAULT_QUERY_PLAN,
            limits=CollectionLimits(
                max_queries=1,
                max_pages_per_query=1,
                max_requests=3,
                max_retries_per_page=0,
                deadline_seconds=45,
            ),
            observation_date=lambda: (
                utcnow().astimezone(timezone.utc) - timedelta(days=2)
            ).date(),
            shared_cache=cache,
            resolve_cache_grant=resolve_cache_grant,
            max_wikimedia_date_attempts=3,
            include_wikimedia_transport_metrics=True,
            unavailable_mode="LIVE",
            utcnow=utcnow,
        ).build()
        handlers = {
            source_id: (built_handlers[source_id]
                        if source_id == WIKIMEDIA_SOURCE_ID
                        else _unavailable_handler(source_id))
            for source_id in policy.enabled_sources
        }
        worker = DistributedFetchWorker(
            admission,
            handlers=handlers,
            resolve_grant=resolve_checkpoint,
            utcnow=utcnow,
            snapshot_blobs=snapshot_blobs,
        )
        facade = DistributedPublicFetch(
            admission,
            worker,
            resolve_grant=resolve_checkpoint,
            utcnow=utcnow,
            firestore_shared_live=True,
            resolve_processing_grants=resolve_processing,
        )
        facade.normal_google_live_sources = frozenset(policy.enabled_sources)
        facade.available_sources = frozenset({WIKIMEDIA_SOURCE_ID})
        facade.vercel_wikimedia_live = True
        facade.vercel_wikimedia_policy_sync_status = policy_sync.status
        facade.prepare_for_wikimedia_fetch = policy_sync.prepare_for_fetch
        facade.google_capabilities = {
            "trends_discovery": "NOT_ENABLED_FOR_VERCEL",
            "rss_discovery": "NOT_ENABLED_FOR_VERCEL",
            "serp_verification": "NOT_ENABLED_FOR_VERCEL",
            "bing_serp_verification": "NOT_ENABLED_FOR_VERCEL",
            "account_api": "NOT_CHECKED",
            "live_verified": False,
        }
        yield facade
    finally:
        await wiki.aclose()


class _VercelEnvironmentCredentialProvider:
    """Resolve only the approved server-side provider credentials."""

    _ALLOWED_REFERENCES = frozenset({"SERPAPI_API_KEY", "YOUTUBE_API_KEY"})

    def resolve(self, reference):
        if reference not in self._ALLOWED_REFERENCES:
            return None
        return os.environ.get(reference)


@asynccontextmanager
async def vercel_shared_live_composition(
    client,
    *,
    user_agent: str,
    utcnow=lambda: datetime.now(timezone.utc),
    defer_collection_snapshot: bool = False,
):
    """Compose all owner-approved Vercel sources over the shared Firestore ledger.

    Constructing this request-scoped runtime does not contact any provider. The
    existing adapter, consent, schedule, quota and shared-cache guards run only
    after an authenticated same-origin Fetch is admitted.
    """
    if (os.environ.get("VERCEL") != "1" or not isinstance(user_agent, str)
            or not user_agent.strip() or not callable(utcnow)
            or type(defer_collection_snapshot) is not bool):
        raise ValueError("Vercel Shared LIVE requires its deployed runtime")
    _client_project_id(client)

    from pathlib import Path
    from tempfile import TemporaryDirectory

    from app.google_live_settings import GoogleLiveSettings
    from app.normal_wikimedia_live_policy import normal_google_live_policy
    from app.orchestration.normal_google_live_composition import (
        normal_google_live_composition,
    )
    from app.storage.firestore_atomic_state import FirestoreAtomicStateStore

    google = GoogleLiveSettings.from_environment()
    policy = normal_google_live_policy(
        serpapi_daily_budget=google.serpapi_daily_budget,
        serpapi_monthly_budget=google.serpapi_monthly_budget,
        rss_daily_budget=google.rss_daily_budget,
        rss_monthly_budget=google.rss_monthly_budget,
        fetch_starts_per_hour=google.fetch_starts_per_hour,
        bing_enabled=google.paid_bing_approved,
        bing_daily_budget=google.bing_daily_budget,
        youtube_enabled=True,
    )
    state_store = FirestoreAtomicStateStore(client)
    policy_sync = _VercelWikimediaPolicySync(
        state_store,
        target_policy=policy,
        previous_policies=_vercel_previous_policy_candidates(google),
        state_key=GOOGLE_STATE_KEY,
        utcnow=utcnow,
    )

    with TemporaryDirectory(prefix="trendscout-vercel-fetch-") as runtime_directory:
        from app.collections.firestore_snapshot import FirestoreCollectionSnapshot
        from app.collections.store import CollectionStore

        collection_path = Path(runtime_directory) / "collections.sqlite"
        collection_snapshot = FirestoreCollectionSnapshot(client, collection_path, utcnow=utcnow)
        collection_snapshot_loaded = not defer_collection_snapshot
        if collection_snapshot_loaded:
            collection_snapshot.load()
        collection_store = (None if defer_collection_snapshot else
                            CollectionStore(collection_path, data_mode_scope="LIVE"))
        async with normal_google_live_composition(
            user_agent=user_agent,
            utcnow=utcnow,
            google_settings=google,
            credential_provider=_VercelEnvironmentCredentialProvider(),
            collection_store=collection_store,
            force_refresh_trends=True,
            allow_initial_google_acceptance=True,
            allow_initial_bing_acceptance=True,
            preserve_expired_data=True,
            youtube_max_queries=1,
            shared_firestore_client=client,
            vercel_shared_live=True,
            youtube_allow_initial_acceptance=True,
        ) as facade:
            facade.available_sources = frozenset(policy.enabled_sources)
            facade.vercel_shared_live = True
            # Retain the old attribute for callers during the Wikimedia-only rollout.
            facade.vercel_wikimedia_live = True
            facade.vercel_shared_live_policy_sync_status = policy_sync.status
            facade.vercel_wikimedia_policy_sync_status = policy_sync.status
            facade.prepare_for_shared_fetch = policy_sync.prepare_for_fetch
            facade.prepare_for_wikimedia_fetch = policy_sync.prepare_for_fetch
            facade.google_capabilities = {
                **facade.google_capabilities,
                "runtime_scope": "VERCEL_SHARED_LIVE",
                "local_only": False,
            }
            facade.youtube_capabilities = {
                **facade.youtube_capabilities,
                "approval_status": (
                    "APPROVED_FOR_VERCEL_SHARED_RUNTIME"
                    if google.youtube_approval_configured
                    and facade.youtube_capabilities.get("configuration_status") == "PRESENT"
                    else "PENDING_LIVE_APPROVAL"
                ),
                "live_host_status": (
                    "VERCEL_SHARED_RUNTIME"
                    if facade.normal_youtube_live else "NOT_DESIGNATED"
                ),
                "runtime_mode": "VERCEL_OWNER_TRIGGERED",
            }
            from app.collections.entity_context import WikimediaEntityContextResolver
            from app.collections.service import CollectionService

            def build_collection_service(store):
                return CollectionService(
                    store,
                    entity_context_resolver=WikimediaEntityContextResolver(
                        store,
                        before_request=facade.entity_lookup_before_request,
                    ),
                    fetch_model_inference_enabled=False,
                )

            if collection_store is not None:
                facade.collection_service = build_collection_service(collection_store)
            else:
                facade.collection_service = None

            def load_collection_snapshot():
                nonlocal collection_snapshot_loaded, collection_store
                if not collection_snapshot_loaded:
                    collection_snapshot.load()
                    collection_snapshot_loaded = True
                if collection_store is None:
                    collection_store = CollectionStore(
                        collection_path, data_mode_scope="LIVE"
                    )
                    facade.collection_service = build_collection_service(collection_store)

            facade.collection_snapshot = collection_snapshot
            facade.load_collection_snapshot = load_collection_snapshot
            yield facade
