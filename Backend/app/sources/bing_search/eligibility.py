"""Compatibility exports for the shared user-triggered paid-source schedule."""

from app.sources.refresh_eligibility import (
    MIN_SOURCE_SPACING,
    SCHEDULE_SLOTS,
    SCHEDULE_TIMEZONE,
    SLOT_GRACE,
    eligible_slot,
)

__all__ = [
    "MIN_SOURCE_SPACING",
    "SCHEDULE_SLOTS",
    "SCHEDULE_TIMEZONE",
    "SLOT_GRACE",
    "eligible_slot",
]
