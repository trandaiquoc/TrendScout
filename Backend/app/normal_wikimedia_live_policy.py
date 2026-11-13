"""Versioned local policy for ordinary, user-initiated Wikimedia Fetches."""

from app.jobs.public_admission import AdmissionPolicy, PoolLimit
from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from app.sources.query_plan import DEFAULT_QUERY_PLAN

POLICY_VERSION = "NORMAL_WIKIMEDIA_LIVE_V1"
STATE_KEY = "normal-wikimedia-live-v1"


def normal_wikimedia_live_policy() -> AdmissionPolicy:
    """Bound concurrency and short-window traffic without an experiment lifetime cap."""
    return AdmissionPolicy(
        enabled_sources=frozenset({WIKIMEDIA_SOURCE_ID}),
        pools={"wikimedia": PoolLimit(
            daily_requests=500, monthly_requests=10_000,
            daily_cost_units=500, monthly_cost_units=10_000,
            cost_per_attempt=1, max_inflight=1, attempt_timeout_seconds=20,
        )},
        plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
        max_starts_per_window=60, window_seconds=60, cooldown_seconds=1,
        lease_seconds=60, max_attempts=3, max_job_seconds=180,
        receipt_seconds=900, max_records=200, max_status_polls=120,
        enabled=True,
    )
