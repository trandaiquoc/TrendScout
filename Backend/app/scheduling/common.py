"""Shared schedule validation and UTC calculation, independent of storage."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
FREQUENCIES = {"DAILY": timedelta(days=1), "WEEKLY": timedelta(days=7)}


class ScheduleBusyError(ValueError):
    """A schedule cannot be edited while one of its occurrences is running."""


class ScheduleLeaseLost(ValueError):
    """An occurrence is owned by another worker or its lease has expired."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(UTC).isoformat()


def _valid_zone(name: str) -> ZoneInfo:
    if not isinstance(name, str) or len(name) > 64:
        raise ValueError("invalid timezone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("unknown timezone") from None


def _next_run(now: datetime, frequency: str, timezone_name: str, hour: int, minute: int) -> datetime:
    if frequency not in FREQUENCIES:
        raise ValueError("unsupported schedule frequency")
    zone = _valid_zone(timezone_name)
    local_now = now.astimezone(zone)
    days = 1 if frequency == "DAILY" else 7
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    candidate += timedelta(days=(days - candidate.weekday()) % days) if frequency == "WEEKLY" else timedelta(0)
    if candidate <= local_now:
        candidate += timedelta(days=days)
    return candidate.astimezone(UTC)
