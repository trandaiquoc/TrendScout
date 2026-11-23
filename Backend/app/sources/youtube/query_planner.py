"""Deterministic, time-distributed YouTube query selection."""

from datetime import date, datetime, timezone

from app.sources.query_plan import DEFAULT_QUERY_PLAN, QueryPlan
from app.sources.refresh_eligibility import SCHEDULE_SLOTS, SCHEDULE_TIMEZONE, SLOT_GRACE

YOUTUBE_QUERY_PLANNER_VERSION = "youtube-curated-slot-and-manual-rotation-v3"
NORMAL_DEMO = "NORMAL_DEMO"
OWNER_MANUAL_LIVE = "OWNER_MANUAL_LIVE"
AUTHENTICATED_MANUAL = "AUTHENTICATED_MANUAL"
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
_MANUAL_PRODUCT_SCOPES = _PRIORITY_SCOPES[:5]


def select_youtube_query_scopes(
    *, now: datetime, plan: QueryPlan = DEFAULT_QUERY_PLAN, max_queries: int = 2,
    mode: str = NORMAL_DEMO,
) -> tuple[str, ...]:
    """Select registered scopes without authorizing a provider request.

    NORMAL_DEMO rotates on the existing Vietnam schedule. OWNER_MANUAL_LIVE
    rotates by the actual UTC minute, so query selection works at any real time
    without claiming that the time is a scheduled slot.
    """
    if (plan != DEFAULT_QUERY_PLAN or not isinstance(now, datetime)
            or now.tzinfo is None or now.utcoffset() is None
            or type(max_queries) is not int or not 1 <= max_queries <= 2
            or not isinstance(mode, str)
            or mode not in {NORMAL_DEMO, OWNER_MANUAL_LIVE, AUTHENTICATED_MANUAL}):
        raise ValueError("registered plan, timezone-aware time, mode and a 1..2 query cap required")
    if mode in {OWNER_MANUAL_LIVE, AUTHENTICATED_MANUAL}:
        minute_ordinal = int(now.astimezone(timezone.utc).timestamp() // 60)
        rotation_scopes = (_MANUAL_PRODUCT_SCOPES if mode == OWNER_MANUAL_LIVE
                           else _PRIORITY_SCOPES)
        start = minute_ordinal % len(rotation_scopes)
    else:
        local = now.astimezone(SCHEDULE_TIMEZONE)
        slot_index = next((index for index, slot in enumerate(SCHEDULE_SLOTS)
                           if datetime.combine(local.date(), slot, tzinfo=SCHEDULE_TIMEZONE)
                           <= local
                           < datetime.combine(local.date(), slot, tzinfo=SCHEDULE_TIMEZONE)
                           + SLOT_GRACE), None)
        day_offset = (local.date() - _ANCHOR_DATE).days
        if slot_index is None:
            raise ValueError("YouTube query selection is available only inside a scheduled slot")
        ordinal = day_offset * len(SCHEDULE_SLOTS) + slot_index
        rotation_scopes = _PRIORITY_SCOPES
        start = (ordinal * max_queries) % len(_PRIORITY_SCOPES)
    ordered = tuple(rotation_scopes[(start + offset) % len(rotation_scopes)]
                    for offset in range(len(rotation_scopes)))
    registered = {seed.scope for seed in plan.seeds}
    selected = tuple(scope for scope in ordered if scope in registered)[:max_queries]
    if len(selected) != max_queries:
        raise ValueError("curated YouTube query scopes do not match the registered plan")
    return selected
