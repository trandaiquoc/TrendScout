"""Local multi-source LIVE composition with SQLite default and Firestore opt-in."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.discovery.contracts import ProcessingGrant
from app.google_live_settings import GoogleLiveSettings
from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.jobs.source_checkpoints import CheckpointGrant
from app.normal_wikimedia_live_policy import (
    GOOGLE_POLICY_VERSION,
    GOOGLE_STATE_KEY,
    normal_google_live_policy,
)
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.sources.bing_search.handler import BingSavedCandidateHandler
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)
from app.sources.google_trends.budget import SerpApiAccountBudget
from app.sources.google_trends.handler import (
    GoogleTrendsDiscoveryHandler,
    trending_now_cache_scope,
)
from app.sources.google_trends.rss_gateway import GoogleTrendsRSSGateway
from app.sources.google_trends.rss_handler import GoogleTrendsRSSHandler
from app.sources.google_trends.workflow import GoogleDiscoveryFallbackHandler
from app.sources.guarded_collector import CollectionLimits, GuardedQueryCollector
from app.sources.live_gateway import LivePageGateway, SourceApproval
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.refresh_eligibility import (
    BING_REFRESH_STATE_KEY,
    GOOGLE_REFRESH_STATE_KEY,
    MIN_SOURCE_SPACING,
    OWNER_ACCEPTANCE_SLOT_PREFIX,
    SCHEDULE_SLOTS,
    SCHEDULE_TIMEZONE,
    SLOT_GRACE,
    YOUTUBE_REFRESH_STATE_KEY,
    candidate_slot,
    reserve_owner_acceptance_slot,
    reserve_source_slot,
    schedule_metadata,
)
from app.sources.serpapi.account import SerpApiAccountClient
from app.sources.serpapi.errors import SerpApiError
from app.sources.wikimedia.live_gateway import WikimediaTopPageGateway
from app.sources.youtube.policy import YOUTUBE_POLICY_VERSION
from app.sources.youtube.query_planner import (
    NORMAL_DEMO,
    OWNER_MANUAL_LIVE,
    select_youtube_query_scopes,
)
from app.sources.youtube.retention import (
    purge_expired_youtube_cache,
    purge_expired_youtube_checkpoints,
)
from app.storage.local_file_security import secure_private_directory
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.storage.local_sqlite_snapshot_blobs import LocalSQLiteSnapshotBlobs
from app.workers.d5_source_handlers import D5SourceHandlers
from app.workers.guarded_attempt import GuardedAttemptRunner

WINDOWS2_OWNER_HOST = "DESKTOP-ICECOET"


def owner_manual_runtime_supported(platform_name, host_name):
    """Keep the explicit owner path local to Linux or the designated Windows 2 host."""
    if not isinstance(platform_name, str) or not isinstance(host_name, str):
        return False
    return (platform_name.startswith("linux")
            or (platform_name.startswith("win")
                and host_name.strip().casefold() == WINDOWS2_OWNER_HOST.casefold()))


_MAX_CACHE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_CANDIDATE_HISTORY = 5_000


def _shared_google_candidate_rows(cache, resolve_cache_grant, *, now):
    """Read Google candidates from the shared source cache for a bounded Bing run."""
    try:
        grant = resolve_cache_grant(GOOGLE_SOURCE_ID, now)
        if not isinstance(grant, CacheGrant):
            return ()
        scope = trending_now_cache_scope(DEFAULT_QUERY_PLAN.fingerprint, grant)
        if scope is None:
            return ()
        cached = cache.get(scope, grant=grant, now=now)
    except Exception:
        return ()
    if (not isinstance(cached, dict) or cached.get("status") not in {"CACHED", "STALE"}
            or cached.get("origin") != "LIVE"):
        return ()
    payload = cached.get("payload")
    records = payload.get("records") if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get("data_mode") != "LIVE"
            or not isinstance(records, list) or len(records) > _MAX_CANDIDATE_HISTORY):
        return ()
    from app.sources.google_trends.trending_now import normalize_keyword

    rows = []
    for record in records:
        if not isinstance(record, dict) or record.get("data_mode") != "LIVE":
            continue
        try:
            keyword, normalized = normalize_keyword(record.get("canonical_keyword"))
        except (TypeError, ValueError):
            continue
        identity = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if record.get("canonical_keyword_identity") != identity:
            continue
        fetched_at = record.get("fetched_at")
        observed_at = (record.get("provider_data_at")
                       or record.get("provider_created_at") or fetched_at)
        if (not isinstance(fetched_at, str) or not isinstance(observed_at, str)
                or not isinstance(record.get("trend_clusters"), list)):
            continue
        project = f"google_trends:{record.get('plan_id', 'trending-now')}:{record.get('plan_version', 1)}"
        rows.append({
            "record_id": identity,
            "canonical_keyword": keyword,
            "source_id": GOOGLE_SOURCE_ID,
            "project": project,
            "observed_at": observed_at,
            "fetched_at": fetched_at,
            "data_mode": "LIVE",
            "evidence": record,
        })
    return tuple(rows)


class _GoogleRuntimeApproval:
    """Short-lived source approval for local LIVE or pinned Vercel Shared LIVE."""
    def __init__(self, source_id, *, enabled, clock, scope="LOCAL_NORMAL_GOOGLE",
                 local_controls_verified=True):
        self.source_id, self.enabled, self.clock = source_id, enabled, clock
        self.scope = scope
        self.local_controls_verified = local_controls_verified
        self._current = None

    def current(self, source_id):
        if source_id != self.source_id or self.enabled is not True:
            return None
        now = self.clock()
        if self._current is not None and self._current.expires_at > now:
            return self._current
        self._current = SourceApproval(
            source_id=source_id,
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            expires_at=now + timedelta(minutes=5),
            policy_version=GOOGLE_POLICY_VERSION,
            approval_id=f"{GOOGLE_POLICY_VERSION}:{source_id}",
            source_enabled=True,
            global_enabled=True,
            usage_approved=True,
            retention_approved=True,
            quota_approved=True,
            cost_approved=True,
            security_approved=True,
            egress_controls_verified=True,
            telemetry_controls_verified=True,
            scope=self.scope,
            local_controls_verified=self.local_controls_verified,
        )
        return self._current


# Keep the local-only name available to existing tests and callers; local
# approval defaults are unchanged, while Vercel explicitly supplies its scope.
_GoogleLocalApproval = _GoogleRuntimeApproval


class _PinnedSerpApiCredential:
    """Keep all approved SerpApi calls on the account used for budget checks."""

    def __init__(self, value):
        self._value = value

    def resolve(self, reference):
        return self._value if reference == "SERPAPI_API_KEY" else None


def _candidate_history(collection_store, *, now):
    """Read only persisted Google Trends identities and recent SERP evidence."""
    observed, verified, prior_verification = set(), set(), {}
    cutoff = now.astimezone(timezone.utc) - timedelta(days=7)
    with collection_store.connect() as db:
        rows = db.execute(
            "SELECT normalized,evidence FROM keywords WHERE source=? AND data_mode='LIVE' "
            "AND project LIKE 'google_trends:%' ORDER BY observed_at DESC LIMIT ?",
            (GOOGLE_SOURCE_ID, _MAX_CANDIDATE_HISTORY),
        ).fetchall()
    for row in rows:
        normalized = row["normalized"]
        if not isinstance(normalized, str) or not normalized:
            continue
        identity = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        observed.add(identity)
        try:
            evidence = json.loads(row["evidence"])
            item = evidence.get("serp_verification") if isinstance(evidence, dict) else None
            if not isinstance(item, dict):
                continue
            if item.get("status") in {"VERIFIED", "FAILED"}:
                prior_verification[identity] = item
            if item.get("status") != "VERIFIED":
                continue
            fetched_at = datetime.fromisoformat(item.get("fetched_at", "").replace("Z", "+00:00"))
            if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
                continue
            fetched_at = fetched_at.astimezone(timezone.utc)
            if cutoff <= fetched_at <= now.astimezone(timezone.utc):
                verified.add(identity)
        except (TypeError, ValueError, AttributeError):
            continue
    return frozenset(observed), frozenset(verified), prior_verification


def _is_owner_acceptance_slot(value):
    return (value == "INITIAL_ACCEPTANCE"
            or isinstance(value, str) and value.startswith(OWNER_ACCEPTANCE_SLOT_PREFIX))


def _youtube_scope_anchor(schedule):
    """Use the real attempt time when the owner authorizes initial acceptance."""
    if not isinstance(schedule, dict):
        raise ValueError("YouTube schedule metadata is invalid")
    value = schedule.get("last_slot")
    if _is_owner_acceptance_slot(value):
        value = schedule.get("last_attempt_at")
    if not isinstance(value, str) or not value:
        raise ValueError("YouTube schedule has no reserved slot")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("YouTube schedule slot is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("YouTube schedule slot must include a timezone")
    return parsed


def _youtube_query_scopes_for_reserved_schedule(schedule, *, plan, max_queries):
    """Use minute rotation for the one-time acceptance outside normal slots."""
    try:
        reserved_slot = _youtube_scope_anchor(schedule)
    except ValueError:
        raise AdmissionDenied("YOUTUBE_SCHEDULE_STATE_INVALID") from None
    mode = (OWNER_MANUAL_LIVE if _is_owner_acceptance_slot(schedule.get("last_slot"))
            else NORMAL_DEMO)
    return select_youtube_query_scopes(
        now=reserved_slot, plan=plan, max_queries=max_queries, mode=mode,
    )


@asynccontextmanager
async def normal_google_live_composition(*, user_agent,
                                         utcnow=lambda: datetime.now(timezone.utc),
                                         state_directory=None,
                                         google_settings: GoogleLiveSettings | None = None,
                                         credential_provider=None,
                                         collection_store=None,
                                         force_refresh_trends: bool = False,
                                         allow_initial_google_acceptance: bool = False,
                                         allow_initial_bing_acceptance: bool = False,
                                         preserve_expired_data: bool = False,
                                         youtube_max_queries: int = 2,
                                         youtube_manual_live: bool = False,
                                         youtube_force_fresh_test_once: bool = False,
                                         shared_firestore_client=None,
                                         vercel_shared_live: bool = False,
                                         youtube_allow_initial_acceptance: bool = False):
    """Compose the local or explicitly injected Vercel worker without startup fetches."""
    config = google_settings or GoogleLiveSettings.from_environment()
    vercel_environment = os.environ.get("VERCEL")
    if (not isinstance(config, GoogleLiveSettings)
            or not isinstance(user_agent, str) or not user_agent.strip()
            or not callable(utcnow) or type(force_refresh_trends) is not bool
            or type(allow_initial_google_acceptance) is not bool
            or type(allow_initial_bing_acceptance) is not bool
            or type(preserve_expired_data) is not bool
            or type(vercel_shared_live) is not bool
            or type(youtube_allow_initial_acceptance) is not bool
            or type(youtube_manual_live) is not bool
            or type(youtube_force_fresh_test_once) is not bool
            or type(youtube_max_queries) is not int
            or not 1 <= youtube_max_queries <= 2
            or (youtube_force_fresh_test_once and not youtube_manual_live)
            or (youtube_manual_live and youtube_max_queries != 1)):
        raise ValueError("explicit Google LIVE runtime and valid configuration required")
    if ((vercel_environment is not None and
         (vercel_environment != "1" or not vercel_shared_live))
            or (vercel_shared_live and
                (vercel_environment != "1" or shared_firestore_client is None
                 or youtube_manual_live or youtube_force_fresh_test_once))):
        raise ValueError("Vercel Shared LIVE requires its request-scoped Firestore runtime")
    if credential_provider is None:
        from app.local_credentials import LocalCredentialProvider
        credential_provider = LocalCredentialProvider()
    if collection_store is None:
        from app.collections.store import CollectionStore
        collection_store = CollectionStore.local()

    if youtube_manual_live:
        runtime_host = socket.gethostname().strip()
        if (not owner_manual_runtime_supported(sys.platform, runtime_host)
                or not config.youtube_approval_configured
                or not config.youtube_live_host
                or runtime_host.casefold() != config.youtube_live_host.casefold()):
            raise ValueError("owner manual YouTube LIVE requires an approved local owner host")

    uses_firestore = shared_firestore_client is not None
    if uses_firestore:
        from app.orchestration.firestore_shared_live_composition import _client_project_id
        from app.storage.firestore_atomic_state import FirestoreAtomicStateStore
        from app.storage.firestore_snapshot_blobs import FirestoreSnapshotBlobs

        _client_project_id(shared_firestore_client)
        store = FirestoreAtomicStateStore(shared_firestore_client)
        snapshot_blobs = FirestoreSnapshotBlobs(shared_firestore_client)
    else:
        directory = Path(state_directory) if state_directory is not None else (
            Path.home() / ".local" / "state" / "trendscout" / GOOGLE_STATE_KEY
        )
        secure_private_directory(directory)
        store = LocalSQLiteAtomicStateStore.for_local_live(directory / "state.sqlite")
        snapshot_blobs = LocalSQLiteSnapshotBlobs(
            store, preserve_expired=preserve_expired_data
        )
        if not preserve_expired_data:
            snapshot_blobs.purge_expired(now=utcnow())
    policy = normal_google_live_policy(
        serpapi_daily_budget=config.serpapi_daily_budget,
        serpapi_monthly_budget=config.serpapi_monthly_budget,
        rss_daily_budget=config.rss_daily_budget,
        rss_monthly_budget=config.rss_monthly_budget,
        fetch_starts_per_hour=config.fetch_starts_per_hour,
        bing_enabled=config.paid_bing_approved,
        bing_daily_budget=config.bing_daily_budget,
        youtube_enabled=True,
    )
    admission = PublicAdmission(
        store, policy, state_key=GOOGLE_STATE_KEY,
        owner_manual_youtube_live=youtube_manual_live,
    )
    existing_state = store.transact(GOOGLE_STATE_KEY, lambda state: (state, state))
    # First launch has no quota state to migrate; any populated ledger must
    # still match one of the explicit migration paths below.
    if (uses_firestore and existing_state
            and existing_state.get("policy_digest") != admission.policy_digest
            and not vercel_shared_live):
        raise ValueError(
            "Firestore shared LIVE quota policy differs from stored state; "
            "refusing an implicit shared-state migration"
        )
    if (not uses_firestore and existing_state
            and existing_state.get("policy_digest") != admission.policy_digest):
        def make_policy(*, bing_enabled, youtube_enabled, fetch_starts_per_hour=None):
            return normal_google_live_policy(
                    serpapi_daily_budget=config.serpapi_daily_budget,
                    serpapi_monthly_budget=config.serpapi_monthly_budget,
                    rss_daily_budget=config.rss_daily_budget,
                    rss_monthly_budget=config.rss_monthly_budget,
                    fetch_starts_per_hour=(config.fetch_starts_per_hour
                                           if fetch_starts_per_hour is None
                                           else fetch_starts_per_hour),
                    bing_enabled=bing_enabled,
                    bing_daily_budget=config.bing_daily_budget,
                    youtube_enabled=youtube_enabled,
            )

        def migration_target(previous_policy, next_policy, note, *, allow_capacity=False):
            PublicAdmission(store, next_policy, state_key=GOOGLE_STATE_KEY).migrate_bounded_quota_policy(
                previous_policy,
                authorization_note=note,
                now=utcnow(),
                allow_fetch_capacity_increase=allow_capacity,
            )

        legacy_google_policy = normal_google_live_policy(
            serpapi_daily_budget=10,
            serpapi_monthly_budget=10,
            rss_daily_budget=1,
            rss_monthly_budget=1,
            fetch_starts_per_hour=2,
            bing_enabled=False,
            youtube_enabled=False,
        )
        legacy_google_digest = PublicAdmission(
            store, legacy_google_policy, state_key=GOOGLE_STATE_KEY
        ).policy_digest
        if existing_state.get("policy_digest") == legacy_google_digest:
            admission.migrate_legacy_google_live_policy(
                legacy_google_policy,
                authorization_note="OWNER_AUTHORIZED_D6_YOUTUBE_WINDOWS_ACCEPTANCE",
                now=utcnow(),
            )
        else:
            previous_without_youtube = make_policy(
                bing_enabled=config.paid_bing_approved, youtube_enabled=False
            )
            previous_without_youtube_digest = PublicAdmission(
                store, previous_without_youtube, state_key=GOOGLE_STATE_KEY
            ).policy_digest
            if existing_state.get("policy_digest") == previous_without_youtube_digest:
                migration_target(
                    previous_without_youtube, policy,
                    "GOAL_AUTHORIZED_ADDITIVE_YOUTUBE_BUDGET_V1",
                )
            elif config.paid_bing_approved:
                previous_without_bing = make_policy(bing_enabled=False, youtube_enabled=False)
                previous_without_bing_digest = PublicAdmission(
                    store, previous_without_bing, state_key=GOOGLE_STATE_KEY
                ).policy_digest
                if existing_state.get("policy_digest") == previous_without_bing_digest:
                    with_bing = make_policy(bing_enabled=True, youtube_enabled=False)
                    migration_target(
                        previous_without_bing, with_bing,
                        "OWNER_APPROVED_GOOGLE_BING_SHARED_ALLOCATION",
                    )
                    migration_target(
                        with_bing, policy,
                        "GOAL_AUTHORIZED_ADDITIVE_YOUTUBE_BUDGET_V1",
                    )
                else:
                    raise ValueError("stored Google LIVE policy does not match a supported local migration")
            elif config.fetch_starts_per_hour > 1:
                old_capacity_policy = make_policy(
                    bing_enabled=False, youtube_enabled=False, fetch_starts_per_hour=1
                )
                old_capacity_digest = PublicAdmission(
                    store, old_capacity_policy, state_key=GOOGLE_STATE_KEY
                ).policy_digest
                if existing_state.get("policy_digest") == old_capacity_digest:
                    current_without_youtube = make_policy(
                        bing_enabled=False, youtube_enabled=False
                    )
                    migration_target(
                        old_capacity_policy, current_without_youtube,
                        "LOCAL_FETCH_CACHE_CAPACITY_POLICY_V1", allow_capacity=True,
                    )
                    migration_target(
                        current_without_youtube, policy,
                        "GOAL_AUTHORIZED_ADDITIVE_YOUTUBE_BUDGET_V1",
                    )
                else:
                    raise ValueError("stored Google LIVE policy does not match a supported local migration")
            else:
                raise ValueError("stored Google LIVE policy does not match a supported local migration")
    runner = GuardedAttemptRunner(admission, clock=utcnow)
    cache = SharedCache(
        store,
        payload_blobs=snapshot_blobs if uses_firestore else None,
        max_inline_bytes=500_000 if uses_firestore else None,
    )
    youtube_cache_scope = CacheScope(
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
    def youtube_cache_grant_at(now):
        return CacheGrant(
            YOUTUBE_SOURCE_ID, GOOGLE_POLICY_VERSION,
            now + timedelta(seconds=_MAX_CACHE_RETENTION_SECONDS),
            max_fresh_seconds=8 * 60 * 60,
            max_stale_seconds=_MAX_CACHE_RETENTION_SECONDS,
            max_retention_seconds=_MAX_CACHE_RETENTION_SECONDS,
            allowed=True,
        )

    def cleanup_youtube_data():
        now = utcnow()
        cache_deleted = purge_expired_youtube_cache(
            cache, youtube_cache_scope, youtube_cache_grant_at(now), now=now
        )
        if uses_firestore:
            return {
                "cache_entries": int(cache_deleted),
                "expired_blobs": int(snapshot_blobs.purge_expired(now=now)),
            }
        checkpoint_counts = purge_expired_youtube_checkpoints(
            store, snapshot_blobs, now=now, state_key=GOOGLE_STATE_KEY
        )
        return {"cache_entries": int(cache_deleted), **checkpoint_counts}

    if not preserve_expired_data and not uses_firestore:
        cleanup_youtube_data()
    resources = []
    quota_state = {"status": "NOT_CONFIGURED", "snapshot": None}
    try:
        from app.orchestration.normal_wikimedia_live_composition import _PersistentOneSecondPacer

        pace_request = _PersistentOneSecondPacer(store, clock=utcnow)

        def runtime_approval(source_id):
            return _GoogleRuntimeApproval(
                source_id, enabled=True, clock=utcnow,
                scope=("VERCEL_SHARED_LIVE" if vercel_shared_live
                       else "LOCAL_NORMAL_GOOGLE"),
                local_controls_verified=not vercel_shared_live,
            )

        wiki_approval = runtime_approval(WIKIMEDIA_SOURCE_ID)
        wiki = await WikimediaTopPageGateway.controlled_network(
            wiki_approval, runner, user_agent=user_agent, clock=utcnow,
            before_request=pace_request,
            on_rate_limited=pace_request.defer,
        )
        resources.append(wiki)

        def resolve_cache_grant(source_id, now):
            if source_id not in {WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID,
                                 BING_SOURCE_ID, YOUTUBE_SOURCE_ID}:
                return None
            if source_id == YOUTUBE_SOURCE_ID:
                return youtube_cache_grant_at(now)
            return CacheGrant(
                source_id, GOOGLE_POLICY_VERSION,
                now + timedelta(seconds=_MAX_CACHE_RETENTION_SECONDS),
                max_fresh_seconds=(24 * 60 * 60 if source_id == WIKIMEDIA_SOURCE_ID
                                   else 8 * 60 * 60 if source_id == YOUTUBE_SOURCE_ID
                                   else 60 * 60),
                max_stale_seconds=_MAX_CACHE_RETENTION_SECONDS,
                max_retention_seconds=_MAX_CACHE_RETENTION_SECONDS,
                allowed=True,
            )

        def resolve_checkpoint(source_id, now):
            if source_id not in {WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID,
                                 BING_SOURCE_ID, YOUTUBE_SOURCE_ID}:
                return None
            return CheckpointGrant(
                source_id, "LIVE", GOOGLE_POLICY_VERSION,
                now + timedelta(seconds=policy.receipt_seconds), policy.receipt_seconds, True,
            )

        def resolve_processing(now):
            processed_sources = {WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID}
            if BING_SOURCE_ID in policy.enabled_sources:
                processed_sources.add(BING_SOURCE_ID)
            return tuple(ProcessingGrant(
                source_id, True, False, now + timedelta(minutes=15), GOOGLE_POLICY_VERSION
            ) for source_id in sorted(processed_sources))

        credential_status = "NOT_CONFIGURED"
        account_client = None
        budget = None
        serpapi_credentials = credential_provider
        paid_requested = (not youtube_manual_live and (
            config.paid_discovery_approved or config.paid_serp_approved
            or config.paid_bing_approved
        ))
        if paid_requested:
            try:
                key = credential_provider.resolve("SERPAPI_API_KEY")
                if (not isinstance(key, str) or not 8 <= len(key) <= 256
                        or any(ord(char) < 33 or ord(char) > 126 for char in key)):
                    key = None
                    credential_status = "MISSING"
                else:
                    credential_status = "PRESENT"
            except Exception:
                key = None
                credential_status = "INVALID_CONFIG"
            if key and config.account_api_approved:
                try:
                    account_client = await SerpApiAccountClient.controlled_network(
                        key, clock=utcnow
                    )
                    resources.append(account_client)
                    serpapi_credentials = _PinnedSerpApiCredential(key)

                    def on_account_snapshot(snapshot):
                        quota_state.update(
                            status=snapshot.get("budget_status", "AVAILABLE"),
                            snapshot={key: value for key, value in snapshot.items()
                                      if key != "budget_status"},
                        )
                        if policy.source_allocations and snapshot.get("budget_status") != "UNAVAILABLE":
                            renewal = snapshot.get("plan_renewal_date")
                            monthly_limit = snapshot.get("monthly_limit")
                            monthly_remaining = snapshot.get("monthly_remaining")
                            if (not renewal or monthly_limit != 250
                                    or type(monthly_remaining) is not int
                                    or not 0 <= monthly_remaining <= monthly_limit):
                                raise SerpApiError("QUOTA_ACCOUNT_PLAN_MISMATCH")
                            try:
                                admission.set_serpapi_billing_cycle(renewal, now=utcnow())
                                admission.reconcile_serpapi_account_usage(
                                    renewal, monthly_limit - monthly_remaining, now=utcnow()
                                )
                            except AdmissionDenied as exc:
                                raise SerpApiError(exc.code) from None

                    budget = SerpApiAccountBudget(
                        account_client,
                        cross_machine_reserve=config.cross_machine_reserve,
                        on_snapshot=on_account_snapshot,
                    )
                    quota_state["status"] = "NOT_CHECKED"
                except Exception:
                    quota_state["status"] = "UNAVAILABLE"
                    if account_client is not None:
                        await account_client.aclose()
                        resources.remove(account_client)
                        account_client = None
            elif config.account_api_approved:
                quota_state["status"] = "CREDENTIAL_UNAVAILABLE"
            else:
                quota_state["status"] = "ACCOUNT_API_NOT_APPROVED"

        trends_gateway = None
        serp_gateway = None
        paid_ready = budget is not None
        if paid_ready and config.trends_approved:
            trends_gateway = await LivePageGateway.controlled_network(
                GOOGLE_SOURCE_ID, serpapi_credentials,
                runtime_approval(GOOGLE_SOURCE_ID),
                runner, clock=utcnow,
            )
            resources.append(trends_gateway)
        if paid_ready and config.serp_approved:
            serp_gateway = await LivePageGateway.controlled_network(
                GOOGLE_SOURCE_ID, serpapi_credentials,
                runtime_approval(GOOGLE_SOURCE_ID),
                runner, clock=utcnow,
            )
            resources.append(serp_gateway)

        bing_gateway = None
        if paid_ready and config.paid_bing_approved:
            bing_gateway = await LivePageGateway.controlled_network(
                BING_SOURCE_ID, serpapi_credentials,
                runtime_approval(BING_SOURCE_ID),
                runner, clock=utcnow,
            )
            resources.append(bing_gateway)

        runtime_host = socket.gethostname().strip()
        youtube_host_status = (
            "VERCEL_SHARED_RUNTIME"
            if vercel_shared_live
            else "DESIGNATED"
            if config.youtube_live_host
            and runtime_host.casefold() == config.youtube_live_host.casefold()
            else "NOT_DESIGNATED"
        )
        youtube_credential_status = "MISSING"
        try:
            youtube_key = credential_provider.resolve("YOUTUBE_API_KEY")
            if (isinstance(youtube_key, str) and 8 <= len(youtube_key) <= 256
                    and all(33 <= ord(char) <= 126 for char in youtube_key)):
                youtube_credential_status = "PRESENT"
            else:
                youtube_key = None
        except Exception:
            youtube_key = None
            youtube_credential_status = "INVALID_CONFIG"
        youtube_gateway = None
        if (config.youtube_approval_configured
                and (youtube_host_status in {"DESIGNATED", "VERCEL_SHARED_RUNTIME"})
                and youtube_key is not None):
            youtube_gateway = await LivePageGateway.controlled_network(
                YOUTUBE_SOURCE_ID, credential_provider,
                runtime_approval(YOUTUBE_SOURCE_ID),
                runner, clock=utcnow,
            )
            resources.append(youtube_gateway)

        rss_gateway = None
        if config.rss_approved:
            rss_gateway = await GoogleTrendsRSSGateway.controlled_network(
                runtime_approval(GOOGLE_SOURCE_ID),
                runner, clock=utcnow,
            )
            resources.append(rss_gateway)

        def previously_observed():
            return _candidate_history(collection_store, now=utcnow())[0]

        def recently_verified():
            return _candidate_history(collection_store, now=utcnow())[1]

        def previous_verification():
            return _candidate_history(collection_store, now=utcnow())[2]

        def remaining_paid_attempts():
            snapshot = admission.quota_snapshot("serpapi", now=utcnow())
            return min(snapshot["daily"]["remaining"], snapshot["monthly"]["remaining"])

        trends_handler = GoogleTrendsDiscoveryHandler(
            trends_gateway=trends_gateway,
            data_mode="LIVE",
            policy_fingerprint=policy.plan_fingerprint,
            utcnow=utcnow,
            cache=cache,
            resolve_cache_grant=resolve_cache_grant,
            serp_gateway=serp_gateway,
            force_refresh=force_refresh_trends,
            atomic_store=store,
            allow_initial_acceptance=allow_initial_google_acceptance,
            ensure_paid_budget=budget.ensure_attempt_allowed if budget else None,
            remaining_paid_attempts=remaining_paid_attempts,
            previously_observed=previously_observed,
            recently_verified=recently_verified,
            previous_verification=previous_verification,
        )
        rss_handler = (GoogleTrendsRSSHandler(
            gateway=rss_gateway,
            policy_fingerprint=policy.plan_fingerprint,
            utcnow=utcnow,
            cache=cache,
            resolve_cache_grant=resolve_cache_grant,
            serp_gateway=serp_gateway,
            ensure_paid_budget=budget.ensure_attempt_allowed if budget else None,
            previously_observed=previously_observed,
            recently_verified=recently_verified,
            previous_verification=previous_verification,
        ) if rss_gateway is not None else None)
        google_handler = (GoogleDiscoveryFallbackHandler(
            trending_now=trends_handler, rss=rss_handler
        ) if trends_handler or rss_handler else None)
        bing_handler = (BingSavedCandidateHandler(
            gateway=bing_gateway,
            collection_store=collection_store,
            atomic_store=store,
            policy_fingerprint=policy.plan_fingerprint,
            ensure_paid_budget=budget.ensure_attempt_allowed,
            utcnow=utcnow,
            allow_initial_acceptance=allow_initial_bing_acceptance,
            shared_cache=cache,
            resolve_cache_grant=resolve_cache_grant,
            candidate_loader=lambda now: _shared_google_candidate_rows(
                cache, resolve_cache_grant, now=now
            ),
        ) if bing_gateway is not None and budget is not None else None)

        def resolve_youtube_provider_batch(source_id, now, *,
                                           owner_fresh_acceptance=False,
                                           acceptance_job_id=None):
            if source_id != YOUTUBE_SOURCE_ID:
                return None
            if not config.youtube_approval_configured:
                return {"allowed": False, "error_code": "SOURCE_NOT_APPROVED"}
            if youtube_host_status not in {"DESIGNATED", "VERCEL_SHARED_RUNTIME"}:
                return {"allowed": False, "error_code": "LIVE_HOST_NOT_DESIGNATED"}
            if youtube_credential_status != "PRESENT" or youtube_gateway is None:
                return {"allowed": False, "error_code": "CREDENTIAL_NOT_CONFIGURED"}
            try:
                quota = admission.quota_snapshot("youtube", now=now)
            except Exception:
                return {"allowed": False, "error_code": "QUOTA_STATE_UNAVAILABLE"}
            if (not youtube_manual_live and (quota["daily"]["remaining"] <= 0
                    or quota["monthly"]["remaining"] <= 0
                    or quota["daily"]["units_remaining"] <= 0
                    or quota["monthly"]["units_remaining"] <= 0)):
                return {"allowed": False, "error_code": "QUOTA_EXHAUSTED"}
            if youtube_manual_live:
                return {"allowed": True, "mode": OWNER_MANUAL_LIVE}
            if owner_fresh_acceptance:
                try:
                    reserved = reserve_owner_acceptance_slot(
                        store, YOUTUBE_REFRESH_STATE_KEY, now,
                        job_id=acceptance_job_id,
                        error_code="YOUTUBE_SCHEDULE_STATE_INVALID",
                    )
                except AdmissionDenied as error:
                    return {"allowed": False, "error_code": error.code}
                return {"allowed": True, "slot": reserved}
            try:
                slot = candidate_slot(
                    store, YOUTUBE_REFRESH_STATE_KEY, now,
                    error_code="YOUTUBE_SCHEDULE_STATE_INVALID",
                    allow_initial_acceptance=youtube_allow_initial_acceptance,
                )
                if slot is None:
                    schedule = schedule_metadata(
                        store, YOUTUBE_REFRESH_STATE_KEY, now,
                        error_code="YOUTUBE_SCHEDULE_STATE_INVALID",
                    )
                    return {"allowed": False, "error_code": "NOT_YET_ELIGIBLE",
                            "next_eligible_at": schedule.get("next_eligible_at")}
                reserved = reserve_source_slot(
                    store, YOUTUBE_REFRESH_STATE_KEY, now,
                    error_code="YOUTUBE_SCHEDULE_STATE_INVALID",
                    allow_initial_acceptance=youtube_allow_initial_acceptance,
                )
                if reserved is None:
                    schedule = schedule_metadata(
                        store, YOUTUBE_REFRESH_STATE_KEY, now,
                        error_code="YOUTUBE_SCHEDULE_STATE_INVALID",
                    )
                    return {"allowed": False, "error_code": "NOT_YET_ELIGIBLE",
                            "next_eligible_at": schedule.get("next_eligible_at")}
            except AdmissionDenied as error:
                return {"allowed": False, "error_code": error.code}
            return {"allowed": True, "slot": reserved}

        def resolve_youtube_query_scopes(source_id, now):
            if source_id != YOUTUBE_SOURCE_ID:
                return None
            if youtube_manual_live:
                return select_youtube_query_scopes(
                    now=now, plan=DEFAULT_QUERY_PLAN,
                    max_queries=youtube_max_queries, mode=OWNER_MANUAL_LIVE,
                )
            schedule = schedule_metadata(
                store, YOUTUBE_REFRESH_STATE_KEY, now,
                error_code="YOUTUBE_SCHEDULE_STATE_INVALID",
            )
            return _youtube_query_scopes_for_reserved_schedule(
                schedule, plan=DEFAULT_QUERY_PLAN, max_queries=youtube_max_queries,
            )

        youtube_collector = (GuardedQueryCollector(youtube_gateway, utcnow=utcnow)
                             if youtube_gateway is not None else None)
        # Leave time inside Vercel's existing 60-second Function ceiling for
        # Firestore checkpoint commits and request cleanup after provider work.
        source_deadline_seconds = 45 if vercel_shared_live else 60

        source_handlers = D5SourceHandlers(
            query_collectors={
                "google_web_serp": None,
                "bing_web_serp": None,
                "youtube_video_search": youtube_collector,
            },
            wikimedia=wiki,
            google_trends_handler=google_handler,
            bing_search_handler=bing_handler,
            plan=DEFAULT_QUERY_PLAN,
            limits=CollectionLimits(
                max_queries=1, max_pages_per_query=1, max_requests=3,
                max_retries_per_page=0, deadline_seconds=source_deadline_seconds,
            ),
            observation_date=lambda: (
                utcnow().astimezone(timezone.utc) - timedelta(days=2)
            ).date(),
            shared_cache=cache,
            resolve_cache_grant=resolve_cache_grant,
            source_limits={YOUTUBE_SOURCE_ID: CollectionLimits(
                max_queries=youtube_max_queries, max_pages_per_query=1,
                max_requests=youtube_max_queries,
                max_retries_per_page=0, deadline_seconds=source_deadline_seconds,
                max_results=50,
            )},
            resolve_provider_batch=resolve_youtube_provider_batch,
            resolve_query_scopes=resolve_youtube_query_scopes,
            max_wikimedia_date_attempts=3,
            include_wikimedia_transport_metrics=True,
            unavailable_mode="LIVE",
            youtube_query_mode=(OWNER_MANUAL_LIVE if youtube_manual_live else NORMAL_DEMO),
            force_youtube_refresh_once=youtube_force_fresh_test_once,
            utcnow=utcnow,
        )
        handlers = source_handlers.build()
        worker = DistributedFetchWorker(
            admission,
            handlers={source: handlers[source] for source in policy.enabled_sources},
            resolve_grant=resolve_checkpoint,
            utcnow=utcnow,
            snapshot_blobs=snapshot_blobs,
            source_order=tuple(source for source in (
                GOOGLE_SOURCE_ID, BING_SOURCE_ID, WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID,
            ) if source in policy.enabled_sources),
        )
        facade = DistributedPublicFetch(
            admission, worker, resolve_grant=resolve_checkpoint,
            utcnow=utcnow,
            normal_google_live=not uses_firestore,
            firestore_shared_live=uses_firestore,
            resolve_processing_grants=resolve_processing,
            provider_retry_after_seconds=pace_request.retry_after_seconds,
            owner_fresh_acceptance_once=(
                uses_firestore and vercel_shared_live
                and os.environ.get("VERCEL_ENV") == "production"
            ),
        )
        if uses_firestore:
            facade.normal_google_live_sources = frozenset(policy.enabled_sources)
        facade.entity_lookup_before_request = pace_request.wait_sync
        # The terminal replay endpoint may read a retained source cache when a
        # short-lived worker checkpoint has expired. This path is read-only with
        # respect to providers and never reserves quota or consumes acceptance.
        facade.read_cached_source_result = source_handlers.read_cached_source_result
        facade.normal_bing_live = bing_gateway is not None
        facade.normal_youtube_live = youtube_gateway is not None
        facade.google_account_quota_state = quota_state
        facade.google_capabilities = {
            "trends_discovery": "LIVE_CONFIGURED" if trends_gateway else "CACHE_ONLY",
            "rss_discovery": "LIVE_CONFIGURED" if rss_gateway else "NOT_CONFIGURED",
            "serp_verification": "LIVE_CONFIGURED" if serp_gateway else "NOT_CONFIGURED",
            "bing_serp_verification": "LIVE_CONFIGURED" if bing_gateway else "NOT_APPROVED",
            "account_api": quota_state["status"],
            "credential_status": credential_status,
            "local_serpapi_daily_budget": config.serpapi_daily_budget,
            "local_serpapi_monthly_budget": config.serpapi_monthly_budget,
            "cross_machine_reserve": config.cross_machine_reserve,
            "serp_max_queries_per_batch": 2,
            "serp_pages_per_query": 1,
            "local_only": not uses_firestore,
            "live_verified": False,
        }

        def refresh_schedule_metadata():
            sources = {}
            for source_id, state_key, initial_allowed in (
                (GOOGLE_SOURCE_ID, GOOGLE_REFRESH_STATE_KEY,
                 allow_initial_google_acceptance),
                (BING_SOURCE_ID, BING_REFRESH_STATE_KEY, allow_initial_bing_acceptance),
                (YOUTUBE_SOURCE_ID, YOUTUBE_REFRESH_STATE_KEY,
                 youtube_allow_initial_acceptance),
            ):
                if source_id == YOUTUBE_SOURCE_ID and youtube_manual_live:
                    sources[source_id] = {
                        "status": OWNER_MANUAL_LIVE,
                        "schedule_enforced": False,
                        "user_triggered_only": True,
                    }
                    continue
                try:
                    sources[source_id] = schedule_metadata(
                        store, state_key, utcnow(),
                        error_code=f"{source_id.upper()}_SCHEDULE_STATE_INVALID",
                        allow_initial_acceptance=initial_allowed,
                    )
                except AdmissionDenied as error:
                    sources[source_id] = {"status": "UNAVAILABLE", "error_code": error.code}
            return {
                "timezone": str(SCHEDULE_TIMEZONE),
                "slots": [slot.strftime("%H:%M") for slot in SCHEDULE_SLOTS],
                "slot_grace_minutes": int(SLOT_GRACE.total_seconds() // 60),
                "minimum_spacing_hours": int(MIN_SOURCE_SPACING.total_seconds() // 3600),
                "max_attempts_per_day": len(SCHEDULE_SLOTS),
                "user_triggered_only": True,
                "youtube_runtime_mode": (
                    OWNER_MANUAL_LIVE if youtube_manual_live else NORMAL_DEMO
                ),
                "sources": sources,
            }

        facade.refresh_schedule_metadata = refresh_schedule_metadata

        facade.youtube_capabilities = {
            "provider": "YouTube Data API v3",
            "approval_status": ("APPROVED_FOR_LOCAL_RUNTIME"
                                if config.youtube_live_approved
                                else "PENDING_LIVE_APPROVAL"),
            "policy_status": "APPROVED" if config.youtube_policy_approved else "DRAFT",
            "policy_version": YOUTUBE_POLICY_VERSION,
            "policy_agreement_required": True,
            "live_host_status": youtube_host_status,
            "configuration_status": youtube_credential_status,
            "live_status": "LIVE_CONFIGURED" if youtube_gateway else "CACHE_ONLY",
            "data_mode": "LIVE",
            "keyword_extraction": "DISABLED_PENDING_POLICY_APPROVAL",
            "derived_metrics": "DISABLED_PENDING_POLICY_APPROVAL",
            "provider_quota_status": "UNKNOWN",
            "provider_quota_reason": "PROJECT_SPECIFIC_LIMITS_REQUIRE_GOOGLE_CLOUD_CONSOLE_EVIDENCE",
            "runtime_mode": OWNER_MANUAL_LIVE if youtube_manual_live else NORMAL_DEMO,
        }

        def youtube_quota_metadata():
            try:
                return {
                    "provider_quota": {
                        "status": "UNKNOWN",
                        "search_calls_per_day": None,
                        "other_method_units_per_day": None,
                        "reason": "PROJECT_SPECIFIC_LIMITS_REQUIRE_GOOGLE_CLOUD_CONSOLE_EVIDENCE",
                    },
                    "search": admission.quota_snapshot("youtube", now=utcnow()),
                    "other_methods": admission.quota_snapshot("youtube_other", now=utcnow()),
                    "last_attempt_reserved_at": admission.last_attempt_reserved_at(
                        YOUTUBE_SOURCE_ID
                    ),
                    "last_provider_request_at": admission.last_provider_request_at(
                        YOUTUBE_SOURCE_ID
                    ),
                    "provider_timezone": "America/Los_Angeles",
                    "internal_monthly_timezone": "UTC",
                }
            except Exception:
                return {"status": "UNAVAILABLE"}

        facade.youtube_quota_metadata = youtube_quota_metadata
        facade.youtube_retention_cleanup = (
            None if preserve_expired_data or uses_firestore else cleanup_youtube_data
        )

        def google_quota_metadata():
            try:
                metadata = {
                    "serpapi": admission.quota_snapshot("serpapi", now=utcnow()),
                    "google_rss": admission.quota_snapshot("google_rss", now=utcnow()),
                    "provider_account": quota_state.get("snapshot"),
                    "provider_account_status": quota_state.get("status", "NOT_CONFIGURED"),
                }
                if "serpapi_google" in policy.pools:
                    metadata["serpapi_google"] = admission.quota_snapshot(
                        "serpapi_google", now=utcnow()
                    )
                if "serpapi_bing" in policy.pools:
                    metadata["serpapi_bing"] = admission.quota_snapshot(
                        "serpapi_bing", now=utcnow()
                    )
                return metadata
            except Exception:
                return {"status": "UNAVAILABLE"}

        facade.google_quota_metadata = google_quota_metadata
        yield facade
    finally:
        for resource in reversed(resources):
            try:
                await resource.aclose()
            except Exception:
                pass
