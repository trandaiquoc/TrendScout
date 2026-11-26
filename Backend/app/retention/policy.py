"""Shared rolling source-data retention window semantics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_RETENTION_DAYS = 7
DEFAULT_BUSINESS_TIMEZONE = "Asia/Ho_Chi_Minh"
POLICY_VERSION = "rolling-local-calendar-days-v1"


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware timestamp")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class RollingRetentionWindow:
    policy_version: str
    business_timezone: str
    business_date: date
    first_retained_date: date
    cutoff_at: datetime
    evaluated_at: datetime


def rolling_retention_window(
    now: datetime,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    business_timezone: str = DEFAULT_BUSINESS_TIMEZONE,
) -> RollingRetentionWindow:
    """Return the UTC cutoff for keeping the latest local calendar dates.

    On business date D, a seven-day policy retains D-6 through D. The cutoff
    is midnight at the start of D-6 in the business timezone, represented as
    an aware UTC instant. ``observed_at`` is deliberately not involved.
    """
    evaluated_at = _aware_utc(now, field="now")
    if type(retention_days) is not int or not 1 <= retention_days <= 366:
        raise ValueError("retention_days must be an integer between 1 and 366")
    if not isinstance(business_timezone, str) or not business_timezone.strip():
        raise ValueError("business_timezone is required")
    try:
        zone = ZoneInfo(business_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("business_timezone must be a valid IANA timezone") from None

    business_date = evaluated_at.astimezone(zone).date()
    first_retained_date = business_date - timedelta(days=retention_days - 1)
    cutoff_at = datetime.combine(first_retained_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    return RollingRetentionWindow(
        policy_version=POLICY_VERSION,
        business_timezone=business_timezone,
        business_date=business_date,
        first_retained_date=first_retained_date,
        cutoff_at=cutoff_at,
        evaluated_at=evaluated_at,
    )


def effective_retention_deadline(
    collected_at: datetime,
    *,
    provider_expires_at: datetime | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    business_timezone: str = DEFAULT_BUSINESS_TIMEZONE,
) -> datetime:
    """Return the earlier of the rolling-policy and provider deadlines."""
    collected_utc = _aware_utc(collected_at, field="collected_at")
    if provider_expires_at is not None:
        provider_expires_at = _aware_utc(provider_expires_at, field="provider_expires_at")
    if type(retention_days) is not int or not 1 <= retention_days <= 366:
        raise ValueError("retention_days must be an integer between 1 and 366")
    try:
        zone = ZoneInfo(business_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("business_timezone must be a valid IANA timezone") from None
    local_collected_date = collected_utc.astimezone(zone).date()
    policy_deadline = datetime.combine(
        local_collected_date + timedelta(days=retention_days), time.min, tzinfo=zone
    ).astimezone(timezone.utc)
    if provider_expires_at is None:
        return policy_deadline
    return min(policy_deadline, provider_expires_at)
