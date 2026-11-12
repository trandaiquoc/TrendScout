"""Fixed, default-disabled admission limits for the proposed local smoke.

These counters measure attempts, not money or the provider's remaining quota.
They become meaningful only with an owner-only persistent local atomic store.
"""

from app.jobs.public_admission import AdmissionPolicy, PoolLimit
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.sources.query_plan import DEFAULT_QUERY_PLAN


def local_smoke_policy() -> AdmissionPolicy:
    return AdmissionPolicy(
        enabled_sources=frozenset({WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID,
                                   BING_SOURCE_ID, YOUTUBE_SOURCE_ID}),
        pools={
            "wikimedia": PoolLimit(1, 1, 1, 1, 1, max_inflight=1, attempt_timeout_seconds=15),
            "serpapi": PoolLimit(2, 2, 2, 2, 1, max_inflight=1, attempt_timeout_seconds=15),
            "youtube": PoolLimit(1, 1, 1, 1, 1, max_inflight=1, attempt_timeout_seconds=15),
        },
        plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
        max_starts_per_window=1, window_seconds=86400, cooldown_seconds=86400,
        lease_seconds=60, max_attempts=1, max_job_seconds=120,
        receipt_seconds=600, max_records=100, max_status_polls=30, enabled=False,
    )
