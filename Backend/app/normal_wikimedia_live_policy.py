"""Versioned local policy for ordinary, user-initiated Wikimedia Fetches."""

from app.jobs.public_admission import AdmissionPolicy, PoolLimit
from app.sources.catalog import GOOGLE_SOURCE_ID, WIKIMEDIA_SOURCE_ID
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


GOOGLE_POLICY_VERSION = "NORMAL_GOOGLE_LIVE_V1"
GOOGLE_STATE_KEY = "normal-google-live-v1"


def normal_google_live_policy(*, serpapi_daily_budget: int = 6,
                              serpapi_monthly_budget: int = 180,
                              rss_daily_budget: int = 30,
                              rss_monthly_budget: int = 900,
                              batches_per_hour: int = 1) -> AdmissionPolicy:
    """Local, owner-only Google policy with explicit SerpApi and RSS ledgers.

    SerpApi has a conservative 6-attempt daily / 180-attempt monthly ceiling;
    the Google RSS feed uses a distinct no-credit pool. A single Fetch can make
    one Trends call and at most two SERP calls. One batch per hour bounds the
    combined SerpApi ceiling to three attempts per hour.
    """
    for value, low, high, name in (
        (serpapi_daily_budget, 1, 6, "SerpApi daily budget"),
        (serpapi_monthly_budget, 1, 180, "SerpApi monthly budget"),
        (rss_daily_budget, 1, 30, "RSS daily budget"),
        (rss_monthly_budget, 1, 900, "RSS monthly budget"),
        (batches_per_hour, 1, 2, "Google batches per hour"),
    ):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} is outside the reviewed bounded range")
    return AdmissionPolicy(
        enabled_sources=frozenset({WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID}),
        pools={
            "wikimedia": PoolLimit(
                daily_requests=500, monthly_requests=10_000,
                daily_cost_units=500, monthly_cost_units=10_000,
                cost_per_attempt=1, max_inflight=1, attempt_timeout_seconds=20,
            ),
            "serpapi": PoolLimit(
                daily_requests=serpapi_daily_budget,
                monthly_requests=serpapi_monthly_budget,
                daily_cost_units=serpapi_daily_budget,
                monthly_cost_units=serpapi_monthly_budget,
                cost_per_attempt=1, max_inflight=1, attempt_timeout_seconds=30,
            ),
            "google_rss": PoolLimit(
                daily_requests=rss_daily_budget,
                monthly_requests=rss_monthly_budget,
                daily_cost_units=rss_daily_budget,
                monthly_cost_units=rss_monthly_budget,
                cost_per_attempt=1, max_inflight=1, attempt_timeout_seconds=30,
            ),
        },
        plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
        max_starts_per_window=batches_per_hour, window_seconds=3600, cooldown_seconds=0,
        lease_seconds=60, max_attempts=3, max_job_seconds=300,
        receipt_seconds=3600, max_records=500, max_status_polls=120,
        enabled=True,
    )
