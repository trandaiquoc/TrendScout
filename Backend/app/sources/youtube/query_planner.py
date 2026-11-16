"""Deterministic, time-distributed YouTube query selection."""

from datetime import date, datetime

from app.sources.query_plan import DEFAULT_QUERY_PLAN, QueryPlan
from app.sources.refresh_eligibility import SCHEDULE_SLOTS, SCHEDULE_TIMEZONE, SLOT_GRACE

YOUTUBE_QUERY_PLANNER_VERSION = "youtube-curated-slot-rotation-v1"
_ANCHOR_DATE = date(2026, 11, 16)
_PRIORITY_SCOPES = (
    "fashion",
    "home_living",
    "beauty_personal_care",
    "travel_lifestyle",
    "technology",
    "gaming",
    "food_beverage",
    "health_fitness",
    "sports",
    "business_finance",
    "entertainment",
    "open_discovery",
    "news_politics",
    "celebrity_public_figures",
)


def select_youtube_query_scopes(
    *, now: datetime, plan: QueryPlan = DEFAULT_QUERY_PLAN, max_queries: int = 2,
    initial_acceptance: bool = False,
) -> tuple[str, ...]:
    """Select up to two curated scopes for the current Vietnam refresh slot.

    The first slot prioritizes fashion and home living. Subsequent slots rotate
    through the remaining registered topics so other industries retain coverage.
    This function only plans queries; it performs no provider requests.
    """
    if (plan != DEFAULT_QUERY_PLAN or not isinstance(now, datetime)
            or now.tzinfo is None or now.utcoffset() is None
            or type(max_queries) is not int or not 1 <= max_queries <= 2
            or type(initial_acceptance) is not bool):
        raise ValueError("registered plan, timezone-aware time and a 1..2 query cap required")
    local = now.astimezone(SCHEDULE_TIMEZONE)
    slot_index = next((index for index, slot in enumerate(SCHEDULE_SLOTS)
                       if datetime.combine(local.date(), slot, tzinfo=SCHEDULE_TIMEZONE)
                       <= local
                       < datetime.combine(local.date(), slot, tzinfo=SCHEDULE_TIMEZONE)
                       + SLOT_GRACE), None)
    day_offset = (local.date() - _ANCHOR_DATE).days
    if slot_index is None:
        if not initial_acceptance:
            raise ValueError("YouTube query selection is available only inside a scheduled slot")
        slot_index = next((index for index, slot in enumerate(SCHEDULE_SLOTS)
                           if slot > local.timetz().replace(tzinfo=None)), None)
        if slot_index is None:
            slot_index = 0
            day_offset += 1
    ordinal = day_offset * len(SCHEDULE_SLOTS) + slot_index
    start = (ordinal * max_queries) % len(_PRIORITY_SCOPES)
    ordered = tuple(_PRIORITY_SCOPES[(start + offset) % len(_PRIORITY_SCOPES)]
                    for offset in range(len(_PRIORITY_SCOPES)))
    registered = {seed.scope for seed in plan.seeds}
    selected = tuple(scope for scope in ordered if scope in registered)[:max_queries]
    if len(selected) != max_queries:
        raise ValueError("curated YouTube query scopes do not match the registered plan")
    return selected
