"""Separately approved local LIVE composition for Wikimedia and Google Trends."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.discovery.contracts import ProcessingGrant
from app.google_live_settings import GoogleLiveSettings
from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import PublicAdmission
from app.jobs.shared_cache import CacheGrant, SharedCache
from app.jobs.source_checkpoints import CheckpointGrant
from app.normal_wikimedia_live_policy import (
    GOOGLE_POLICY_VERSION,
    GOOGLE_STATE_KEY,
    normal_google_live_policy,
)
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.sources.catalog import GOOGLE_SOURCE_ID, WIKIMEDIA_SOURCE_ID
from app.sources.google_trends.budget import SerpApiAccountBudget
from app.sources.google_trends.handler import GoogleTrendsDiscoveryHandler
from app.sources.google_trends.rss_gateway import GoogleTrendsRSSGateway
from app.sources.google_trends.rss_handler import GoogleTrendsRSSHandler
from app.sources.google_trends.workflow import GoogleDiscoveryFallbackHandler
from app.sources.guarded_collector import CollectionLimits
from app.sources.live_gateway import LivePageGateway, SourceApproval
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.serpapi.account import SerpApiAccountClient
from app.sources.wikimedia.live_gateway import WikimediaTopPageGateway
from app.storage.local_file_security import secure_private_directory
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.storage.local_sqlite_snapshot_blobs import LocalSQLiteSnapshotBlobs
from app.workers.d5_source_handlers import D5SourceHandlers
from app.workers.guarded_attempt import GuardedAttemptRunner

_MAX_CACHE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_CANDIDATE_HISTORY = 5_000


class _GoogleLocalApproval:
    def __init__(self, source_id, *, enabled, clock):
        self.source_id, self.enabled, self.clock = source_id, enabled, clock
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
            scope="LOCAL_NORMAL_GOOGLE",
            local_controls_verified=True,
        )
        return self._current


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


@asynccontextmanager
async def normal_google_live_composition(*, user_agent,
                                         utcnow=lambda: datetime.now(timezone.utc),
                                         state_directory=None,
                                         google_settings: GoogleLiveSettings | None = None,
                                         credential_provider=None,
                                         collection_store=None):
    """Compose one local worker; no Google call is made without its own approval."""
    config = google_settings or GoogleLiveSettings.from_environment()
    if (os.environ.get("VERCEL") or not isinstance(config, GoogleLiveSettings)
            or config.local_runtime_approved is not True
            or not isinstance(user_agent, str) or not user_agent.strip()
            or not callable(utcnow)):
        raise ValueError("explicit local Google LIVE opt-in and valid configuration required")
    if credential_provider is None:
        from app.local_credentials import LocalCredentialProvider
        credential_provider = LocalCredentialProvider()
    if collection_store is None:
        from app.collections.store import CollectionStore
        collection_store = CollectionStore.local()

    directory = Path(state_directory) if state_directory is not None else (
        Path.home() / ".local" / "state" / "trendscout" / GOOGLE_STATE_KEY
    )
    secure_private_directory(directory)
    store = LocalSQLiteAtomicStateStore.for_local_live(directory / "state.sqlite")
    snapshot_blobs = LocalSQLiteSnapshotBlobs(store)
    snapshot_blobs.purge_expired(now=utcnow())
    policy = normal_google_live_policy(
        serpapi_daily_budget=config.serpapi_daily_budget,
        serpapi_monthly_budget=config.serpapi_monthly_budget,
        rss_daily_budget=config.rss_daily_budget,
        rss_monthly_budget=config.rss_monthly_budget,
        batches_per_hour=config.batches_per_hour,
    )
    admission = PublicAdmission(store, policy, state_key=GOOGLE_STATE_KEY)
    runner = GuardedAttemptRunner(admission, clock=utcnow)
    cache = SharedCache(store)
    resources = []
    quota_state = {"status": "NOT_CONFIGURED", "snapshot": None}
    try:
        from app.orchestration.normal_wikimedia_live_composition import _PersistentOneSecondPacer

        pace_request = _PersistentOneSecondPacer(store, clock=utcnow)
        wiki_approval = _GoogleLocalApproval(WIKIMEDIA_SOURCE_ID,
                                             enabled=True, clock=utcnow)
        wiki = await WikimediaTopPageGateway.controlled_network(
            wiki_approval, runner, user_agent=user_agent, clock=utcnow,
            before_request=pace_request,
            on_rate_limited=pace_request.defer,
        )
        resources.append(wiki)

        def resolve_cache_grant(source_id, now):
            if source_id not in {WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID}:
                return None
            return CacheGrant(
                source_id, GOOGLE_POLICY_VERSION,
                now + timedelta(seconds=_MAX_CACHE_RETENTION_SECONDS),
                max_fresh_seconds=24 * 60 * 60 if source_id == WIKIMEDIA_SOURCE_ID else 60 * 60,
                max_stale_seconds=_MAX_CACHE_RETENTION_SECONDS,
                max_retention_seconds=_MAX_CACHE_RETENTION_SECONDS,
                allowed=True,
            )

        def resolve_checkpoint(source_id, now):
            if source_id not in {WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID}:
                return None
            return CheckpointGrant(
                source_id, "LIVE", GOOGLE_POLICY_VERSION,
                now + timedelta(seconds=policy.receipt_seconds), policy.receipt_seconds, True,
            )

        def resolve_processing(now):
            return tuple(ProcessingGrant(
                source_id, True, False, now + timedelta(minutes=15), GOOGLE_POLICY_VERSION
            ) for source_id in sorted({WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID}))

        credential_status = "NOT_CONFIGURED"
        account_client = None
        budget = None
        serpapi_credentials = credential_provider
        paid_requested = config.paid_discovery_approved or config.paid_serp_approved
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
                    budget = SerpApiAccountBudget(
                        account_client,
                        cross_machine_reserve=config.cross_machine_reserve,
                        on_snapshot=lambda snapshot: quota_state.update(
                            status=snapshot.get("budget_status", "AVAILABLE"),
                            snapshot={key: value for key, value in snapshot.items()
                                      if key != "budget_status"},
                        ),
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
                _GoogleLocalApproval(GOOGLE_SOURCE_ID, enabled=True, clock=utcnow),
                runner, clock=utcnow,
            )
            resources.append(trends_gateway)
        if paid_ready and config.serp_approved:
            serp_gateway = await LivePageGateway.controlled_network(
                GOOGLE_SOURCE_ID, serpapi_credentials,
                _GoogleLocalApproval(GOOGLE_SOURCE_ID, enabled=True, clock=utcnow),
                runner, clock=utcnow,
            )
            resources.append(serp_gateway)

        rss_gateway = None
        if config.rss_approved:
            rss_gateway = await GoogleTrendsRSSGateway.controlled_network(
                _GoogleLocalApproval(GOOGLE_SOURCE_ID, enabled=True, clock=utcnow),
                runner, clock=utcnow,
            )
            resources.append(rss_gateway)

        def previously_observed():
            return _candidate_history(collection_store, now=utcnow())[0]

        def recently_verified():
            return _candidate_history(collection_store, now=utcnow())[1]

        def previous_verification():
            return _candidate_history(collection_store, now=utcnow())[2]
        trends_handler = (GoogleTrendsDiscoveryHandler(
            trends_gateway=trends_gateway,
            policy_fingerprint=policy.plan_fingerprint,
            utcnow=utcnow,
            cache=cache,
            resolve_cache_grant=resolve_cache_grant,
            serp_gateway=serp_gateway,
            ensure_paid_budget=budget.ensure_attempt_allowed if budget else None,
            previously_observed=previously_observed,
            recently_verified=recently_verified,
            previous_verification=previous_verification,
        ) if trends_gateway is not None else None)
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

        handlers = D5SourceHandlers(
            query_collectors={
                "google_web_serp": None,
                "bing_web_serp": None,
                "youtube_video_search": None,
            },
            wikimedia=wiki,
            google_trends_handler=google_handler,
            plan=DEFAULT_QUERY_PLAN,
            limits=CollectionLimits(
                max_queries=1, max_pages_per_query=1, max_requests=3,
                max_retries_per_page=0, deadline_seconds=60,
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
        worker = DistributedFetchWorker(
            admission,
            handlers={source: handlers[source]
                      for source in (WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID)},
            resolve_grant=resolve_checkpoint,
            utcnow=utcnow,
            snapshot_blobs=snapshot_blobs,
        )
        facade = DistributedPublicFetch(
            admission, worker, resolve_grant=resolve_checkpoint,
            utcnow=utcnow, normal_google_live=True,
            resolve_processing_grants=resolve_processing,
            provider_retry_after_seconds=pace_request.retry_after_seconds,
        )
        facade.entity_lookup_before_request = pace_request.wait_sync
        facade.google_account_quota_state = quota_state
        facade.google_capabilities = {
            "trends_discovery": "LIVE_CONFIGURED" if trends_gateway else "NOT_CONFIGURED",
            "rss_discovery": "LIVE_CONFIGURED" if rss_gateway else "NOT_CONFIGURED",
            "serp_verification": "LIVE_CONFIGURED" if serp_gateway else "NOT_CONFIGURED",
            "account_api": quota_state["status"],
            "credential_status": credential_status,
            "local_serpapi_daily_budget": config.serpapi_daily_budget,
            "local_serpapi_monthly_budget": config.serpapi_monthly_budget,
            "cross_machine_reserve": config.cross_machine_reserve,
            "serp_max_queries_per_batch": 2,
            "serp_pages_per_query": 1,
            "local_only": True,
            "live_verified": False,
        }

        def google_quota_metadata():
            try:
                return {
                    "serpapi": admission.quota_snapshot("serpapi", now=utcnow()),
                    "google_rss": admission.quota_snapshot("google_rss", now=utcnow()),
                    "provider_account": quota_state.get("snapshot"),
                    "provider_account_status": quota_state.get("status", "NOT_CONFIGURED"),
                }
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
