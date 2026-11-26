from datetime import datetime, timedelta, timezone

import pytest

from app.retention.policy import (
    DEFAULT_BUSINESS_TIMEZONE,
    effective_retention_deadline,
    rolling_retention_window,
)


def test_seven_local_dates_use_midnight_cutoff_and_keep_boundary_day():
    now = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
    window = rolling_retention_window(now)

    assert window.business_timezone == DEFAULT_BUSINESS_TIMEZONE
    assert window.business_date.isoformat() == "2026-11-26"
    assert window.first_retained_date.isoformat() == "2026-11-20"
    assert window.cutoff_at == datetime(2026, 11, 19, 17, 0, tzinfo=timezone.utc)
    assert window.evaluated_at == now


def test_deadline_is_seven_local_calendar_days_and_provider_can_shorten_it():
    collected = datetime(2026, 11, 26, 4, 0, tzinfo=timezone.utc)
    policy_deadline = datetime(2026, 12, 2, 17, 0, tzinfo=timezone.utc)
    provider_deadline = policy_deadline - timedelta(hours=1)

    assert effective_retention_deadline(collected) == policy_deadline
    assert effective_retention_deadline(
        collected, provider_expires_at=provider_deadline
    ) == provider_deadline
    assert effective_retention_deadline(
        collected, provider_expires_at=policy_deadline + timedelta(days=3)
    ) == policy_deadline


@pytest.mark.parametrize(
    "kwargs",
    [
        {"now": datetime(2026, 11, 26, 12, 0)},
        {"now": datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc), "retention_days": 0},
        {"now": datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc), "business_timezone": "bad/zone"},
    ],
)
def test_invalid_retention_window_is_rejected(kwargs):
    with pytest.raises(ValueError):
        rolling_retention_window(**kwargs)


def test_naive_collection_and_provider_timestamps_are_rejected():
    with pytest.raises(ValueError, match="collected_at"):
        effective_retention_deadline(datetime(2026, 11, 26))
    with pytest.raises(ValueError, match="provider_expires_at"):
        effective_retention_deadline(
            datetime(2026, 11, 26, tzinfo=timezone.utc),
            provider_expires_at=datetime(2026, 11, 27),
        )
