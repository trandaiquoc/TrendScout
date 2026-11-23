"""Shared user-triggered refresh slots for paid sources."""

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.jobs.public_admission import AdmissionDenied

SCHEDULE_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
SCHEDULE_SLOTS = (time(7), time(15), time(23))
SLOT_GRACE = timedelta(minutes=10)
MIN_SOURCE_SPACING = timedelta(hours=8)
GOOGLE_REFRESH_STATE_KEY = "normal-google-trends-refresh-v1"
BING_REFRESH_STATE_KEY = "normal-bing-search-schedule-v1"
YOUTUBE_REFRESH_STATE_KEY = "normal-youtube-search-schedule-v1"
OWNER_ACCEPTANCE_SLOT_PREFIX = "D6_OWNER_ACCEPTANCE:"
MANUAL_SLOT_PREFIX = "MANUAL:"


def eligible_slot(now: datetime, *, last_attempt_at: datetime | None = None) -> str | None:
    """Return a current local slot only during its short admission window."""
    if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or last_attempt_at is not None and (
                not isinstance(last_attempt_at, datetime)
                or last_attempt_at.tzinfo is None or last_attempt_at.utcoffset() is None
            )):
        raise ValueError("timezone-aware schedule timestamps required")
    local_now = now.astimezone(SCHEDULE_TIMEZONE)
    last_attempt = (last_attempt_at.astimezone(SCHEDULE_TIMEZONE)
                    if last_attempt_at is not None else None)
    for slot_time in SCHEDULE_SLOTS:
        slot_start = datetime.combine(local_now.date(), slot_time, tzinfo=SCHEDULE_TIMEZONE)
        if not slot_start <= local_now < slot_start + SLOT_GRACE:
            continue
        if last_attempt is not None and local_now - last_attempt < MIN_SOURCE_SPACING:
            return None
        return slot_start.isoformat()
    return None


def _normalize_schedule_state(state, *, error_code: str) -> dict:
    if not state:
        return {"schema": 1, "last_attempt_at": None, "last_slot": None}
    if not isinstance(state, dict) or state.get("schema") != 1:
        raise AdmissionDenied(error_code)
    raw_attempt, last_slot = state.get("last_attempt_at"), state.get("last_slot")
    if raw_attempt is None:
        if last_slot is not None:
            raise AdmissionDenied(error_code)
        return {"schema": 1, "last_attempt_at": None, "last_slot": None}
    if not isinstance(raw_attempt, str) or not isinstance(last_slot, str):
        raise AdmissionDenied(error_code)
    try:
        parsed = datetime.fromisoformat(raw_attempt.replace("Z", "+00:00"))
    except ValueError:
        raise AdmissionDenied(error_code) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None or not last_slot:
        raise AdmissionDenied(error_code)
    return {"schema": 1, "last_attempt_at": parsed, "last_slot": last_slot}


def _schedule_state(atomic_store, state_key: str, *, error_code: str) -> dict:
    try:
        state = atomic_store.transact(state_key, lambda value: (value, value))
    except Exception:
        raise AdmissionDenied(error_code) from None
    return _normalize_schedule_state(state, error_code=error_code)


def candidate_slot(atomic_store, state_key: str, now: datetime, *, error_code: str,
                   allow_initial_acceptance: bool = False) -> str | None:
    """Check a slot without consuming it; the provider budget is checked next."""
    state = _schedule_state(atomic_store, state_key, error_code=error_code)
    slot = eligible_slot(now, last_attempt_at=state["last_attempt_at"])
    if slot is None and state["last_attempt_at"] is None and allow_initial_acceptance:
        slot = "INITIAL_ACCEPTANCE"
    if slot is not None and slot == state["last_slot"]:
        return None
    return slot


def reserve_source_slot(atomic_store, state_key: str, now: datetime, *, error_code: str,
                        allow_initial_acceptance: bool = False) -> str | None:
    """Atomically consume one slot so retries and parallel clicks cannot spend twice."""
    if not callable(getattr(atomic_store, "transact", None)):
        raise AdmissionDenied(error_code)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise AdmissionDenied(error_code)

    def operation(raw_state):
        if not raw_state:
            state = {"schema": 1, "last_attempt_at": None, "last_slot": None}
        elif isinstance(raw_state, dict) and raw_state.get("schema") == 1:
            raw_attempt, last_slot = raw_state.get("last_attempt_at"), raw_state.get("last_slot")
            if raw_attempt is None and last_slot is None:
                state = {"schema": 1, "last_attempt_at": None, "last_slot": None}
            elif isinstance(raw_attempt, str) and isinstance(last_slot, str) and last_slot:
                try:
                    parsed = datetime.fromisoformat(raw_attempt.replace("Z", "+00:00"))
                except ValueError:
                    raise AdmissionDenied(error_code) from None
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise AdmissionDenied(error_code)
                state = {"schema": 1, "last_attempt_at": parsed, "last_slot": last_slot}
            else:
                raise AdmissionDenied(error_code)
        else:
            raise AdmissionDenied(error_code)

        slot = eligible_slot(now, last_attempt_at=state["last_attempt_at"])
        if slot is None and state["last_attempt_at"] is None and allow_initial_acceptance:
            slot = "INITIAL_ACCEPTANCE"
        if slot is None or slot == state["last_slot"]:
            return raw_state, None
        value = {
            "schema": 1,
            "last_attempt_at": now.astimezone(timezone.utc).isoformat(),
            "last_slot": slot,
        }
        return value, slot

    try:
        return atomic_store.transact(state_key, operation)
    except AdmissionDenied:
        raise
    except Exception:
        raise AdmissionDenied(error_code) from None


def reserve_owner_acceptance_slot(atomic_store, state_key: str, now: datetime, *,
                                  job_id: str, error_code: str) -> str:
    """Record the one accepted fresh run while bypassing only the normal schedule."""
    if (not callable(getattr(atomic_store, "transact", None))
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id)):
        raise AdmissionDenied(error_code)
    slot = f"{OWNER_ACCEPTANCE_SLOT_PREFIX}{job_id}"

    def operation(raw_state):
        if not raw_state:
            state = {"schema": 1, "last_attempt_at": None, "last_slot": None}
        elif isinstance(raw_state, dict) and raw_state.get("schema") == 1:
            raw_attempt, last_slot = raw_state.get("last_attempt_at"), raw_state.get("last_slot")
            if raw_attempt is None and last_slot is None:
                state = {"schema": 1, "last_attempt_at": None, "last_slot": None}
            elif isinstance(raw_attempt, str) and isinstance(last_slot, str) and last_slot:
                try:
                    parsed = datetime.fromisoformat(raw_attempt.replace("Z", "+00:00"))
                except ValueError:
                    raise AdmissionDenied(error_code) from None
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise AdmissionDenied(error_code)
                state = {"schema": 1, "last_attempt_at": parsed, "last_slot": last_slot}
            else:
                raise AdmissionDenied(error_code)
        else:
            raise AdmissionDenied(error_code)

        if state["last_slot"] == slot:
            return raw_state, slot
        value = {
            "schema": 1,
            "last_attempt_at": now.astimezone(timezone.utc).isoformat(),
            "last_slot": slot,
        }
        return value, slot

    try:
        return atomic_store.transact(state_key, operation)
    except AdmissionDenied:
        raise
    except Exception:
        raise AdmissionDenied(error_code) from None


def manual_candidate_slot(atomic_store, state_key: str, now: datetime, *,
                          job_id: str, error_code: str) -> str | None:
    """Check a user-authenticated manual slot while preserving source spacing."""
    if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id)):
        raise AdmissionDenied(error_code)
    state = _schedule_state(atomic_store, state_key, error_code=error_code)
    slot = f"{MANUAL_SLOT_PREFIX}{job_id}"
    if state["last_slot"] == slot:
        return slot
    last_attempt = state["last_attempt_at"]
    if last_attempt is not None and now.astimezone(timezone.utc) - last_attempt.astimezone(
            timezone.utc) < MIN_SOURCE_SPACING:
        return None
    return slot


def reserve_manual_source_slot(atomic_store, state_key: str, now: datetime, *,
                               job_id: str, error_code: str) -> str | None:
    """Atomically reserve an authenticated manual refresh; retries are idempotent."""
    if (not callable(getattr(atomic_store, "transact", None))
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id)):
        raise AdmissionDenied(error_code)
    slot = f"{MANUAL_SLOT_PREFIX}{job_id}"

    def operation(raw_state):
        if not raw_state:
            state = {"schema": 1, "last_attempt_at": None, "last_slot": None}
        elif isinstance(raw_state, dict) and raw_state.get("schema") == 1:
            raw_attempt, last_slot = raw_state.get("last_attempt_at"), raw_state.get("last_slot")
            if raw_attempt is None and last_slot is None:
                state = {"schema": 1, "last_attempt_at": None, "last_slot": None}
            elif isinstance(raw_attempt, str) and isinstance(last_slot, str) and last_slot:
                try:
                    parsed = datetime.fromisoformat(raw_attempt.replace("Z", "+00:00"))
                except ValueError:
                    raise AdmissionDenied(error_code) from None
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise AdmissionDenied(error_code)
                state = {"schema": 1, "last_attempt_at": parsed, "last_slot": last_slot}
            else:
                raise AdmissionDenied(error_code)
        else:
            raise AdmissionDenied(error_code)

        if state["last_slot"] == slot:
            return raw_state, slot
        last_attempt = state["last_attempt_at"]
        if (last_attempt is not None
                and now.astimezone(timezone.utc) - last_attempt.astimezone(timezone.utc)
                < MIN_SOURCE_SPACING):
            return raw_state, None
        return {
            "schema": 1,
            "last_attempt_at": now.astimezone(timezone.utc).isoformat(),
            "last_slot": slot,
        }, slot

    try:
        return atomic_store.transact(state_key, operation)
    except AdmissionDenied:
        raise
    except Exception:
        raise AdmissionDenied(error_code) from None


def manual_schedule_metadata(atomic_store, state_key: str, now: datetime, *,
                             error_code: str) -> dict:
    """Return the exact next time the shared eight-hour source spacing permits."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise AdmissionDenied(error_code)
    state = _schedule_state(atomic_store, state_key, error_code=error_code)
    last_attempt = state["last_attempt_at"]
    next_at = now.astimezone(timezone.utc)
    if last_attempt is not None:
        next_at = max(next_at, last_attempt.astimezone(timezone.utc) + MIN_SOURCE_SPACING)
    return {
        "minimum_spacing_hours": int(MIN_SOURCE_SPACING.total_seconds() // 3600),
        "last_attempt_at": last_attempt.astimezone(timezone.utc).isoformat()
            if last_attempt else None,
        "next_eligible_at": next_at.isoformat(),
        "last_slot": state["last_slot"],
    }


def _validate_schedule_now(now: datetime, *, error_code: str) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise AdmissionDenied(error_code)


def _schedule_metadata(state, now: datetime, *, error_code: str,
                       allow_initial_acceptance: bool) -> dict:
    _validate_schedule_now(now, error_code=error_code)
    last_attempt = state["last_attempt_at"]
    next_at = None
    if last_attempt is None and allow_initial_acceptance:
        next_at = now.astimezone(SCHEDULE_TIMEZONE)
    else:
        local_now = now.astimezone(SCHEDULE_TIMEZONE)
        local_last = last_attempt.astimezone(SCHEDULE_TIMEZONE) if last_attempt else None
        for day_offset in range(9):
            slot_date = local_now.date() + timedelta(days=day_offset)
            for slot_time in SCHEDULE_SLOTS:
                start = datetime.combine(slot_date, slot_time, tzinfo=SCHEDULE_TIMEZONE)
                end = start + SLOT_GRACE
                if end <= local_now:
                    continue
                available = max(start, local_now)
                if local_last is not None:
                    available = max(available, local_last + MIN_SOURCE_SPACING)
                if available < end:
                    next_at = available
                    break
            if next_at is not None:
                break
    return {
        "timezone": str(SCHEDULE_TIMEZONE),
        "slots": [slot.strftime("%H:%M") for slot in SCHEDULE_SLOTS],
        "slot_grace_minutes": int(SLOT_GRACE.total_seconds() // 60),
        "minimum_spacing_hours": int(MIN_SOURCE_SPACING.total_seconds() // 3600),
        "max_attempts_per_day": len(SCHEDULE_SLOTS),
        "user_triggered_only": True,
        "requires_stale_or_missing_cache": True,
        "initial_acceptance_available": last_attempt is None and allow_initial_acceptance,
        "last_attempt_at": last_attempt.astimezone(timezone.utc).isoformat()
            if last_attempt else None,
        "next_eligible_at": next_at.astimezone(timezone.utc).isoformat()
            if next_at else None,
        "last_slot": state["last_slot"],
    }


def schedule_metadata_from_state(raw_state, now: datetime, *, error_code: str,
                                 allow_initial_acceptance: bool = False) -> dict:
    """Describe a source schedule from a previously fetched state document."""
    _validate_schedule_now(now, error_code=error_code)
    state = _normalize_schedule_state(raw_state, error_code=error_code)
    return _schedule_metadata(
        state, now, error_code=error_code,
        allow_initial_acceptance=allow_initial_acceptance,
    )


def schedule_metadata(atomic_store, state_key: str, now: datetime, *, error_code: str,
                      allow_initial_acceptance: bool = False) -> dict:
    """Describe the next manual refresh opportunity without reserving it."""
    _validate_schedule_now(now, error_code=error_code)
    state = _schedule_state(atomic_store, state_key, error_code=error_code)
    return _schedule_metadata(
        state, now, error_code=error_code,
        allow_initial_acceptance=allow_initial_acceptance,
    )


def schedule_metadata_for_sources(atomic_store, schedules, now: datetime, *,
                                  state_snapshot=None) -> dict:
    """Read independent source schedules together when the store supports batching."""
    schedules = tuple(schedules)
    if schedules:
        _validate_schedule_now(now, error_code=schedules[0][2])
    keys = tuple(item[1] for item in schedules)
    read_many = getattr(atomic_store, "read_many", None)
    states = None
    if (state_snapshot is not None and isinstance(state_snapshot, dict)
            and set(state_snapshot) == set(keys)):
        states = state_snapshot
    if schedules and states is None and callable(read_many):
        try:
            candidate = read_many(keys)
            if isinstance(candidate, dict) and set(candidate) == set(keys):
                states = candidate
        except Exception:
            # Preserve the prior per-source behavior if the optional batch read fails.
            states = None

    result = {}
    for source_id, state_key, error_code, allow_initial_acceptance in schedules:
        try:
            if states is None:
                value = schedule_metadata(
                    atomic_store, state_key, now, error_code=error_code,
                    allow_initial_acceptance=allow_initial_acceptance,
                )
            else:
                value = schedule_metadata_from_state(
                    states[state_key], now, error_code=error_code,
                    allow_initial_acceptance=allow_initial_acceptance,
                )
            result[source_id] = value
        except AdmissionDenied as error:
            result[source_id] = {"status": "UNAVAILABLE", "error_code": error.code}
    return result
