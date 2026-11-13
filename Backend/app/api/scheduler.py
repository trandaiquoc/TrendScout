"""Local operator API for persistent Fetch schedules."""

import hmac
import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.fetches import validate_public_request_origin
from app.scheduling.persistent import PersistentScheduleStore

router = APIRouter()


class ScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    frequency: Literal["DAILY", "WEEKLY"]
    timezone: str = Field(min_length=1, max_length=64)
    hour: int = Field(default=9, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)


class ScheduleEnabled(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def _store(request: Request) -> PersistentScheduleStore:
    store = getattr(request.app.state, "scheduler_store", None)
    if store is None:
        if request.app.state.settings.environment_profile == "vercel":
            raise HTTPException(503, {"code": "SCHEDULER_STORAGE_UNAVAILABLE"})
        if os.name == "nt":
            root = os.environ.get("LOCALAPPDATA")
            if not root:
                raise HTTPException(503, {"code": "SCHEDULER_STORAGE_UNAVAILABLE"})
            directory = Path(root) / "TrendScout" / "scheduler-v1"
        else:
            directory = Path.home() / ".local" / "state" / "trendscout" / "scheduler-v1"
        try:
            store = PersistentScheduleStore(directory / "schedules.sqlite")
        except (OSError, ValueError):
            raise HTTPException(503, {"code": "SCHEDULER_STORAGE_UNAVAILABLE"}) from None
        request.app.state.scheduler_store = store
    return store


def _operator(request: Request) -> None:
    validate_public_request_origin(request, request.app.state.settings.environment_profile)
    token = getattr(request.app.state, "operator_token", None)
    supplied = request.headers.get("authorization", "")
    if not token or len(token) < 24 or not supplied.startswith("Bearer ") or not hmac.compare_digest(supplied[7:].encode(), token.encode()):
        raise HTTPException(401, {"code": "OPERATOR_AUTHENTICATION_REQUIRED"})


@router.get("/scheduler")
def list_schedules(request: Request):
    validate_public_request_origin(request, request.app.state.settings.environment_profile)
    store = _store(request)
    settings = request.app.state.settings
    worker = getattr(request.app.state, "scheduler_worker", None)
    worker_running = bool(worker and worker._task and not worker._task.done())
    return {
        "items": store.list(),
        "implemented": True,
        "enabled": settings.scheduler_enabled,
        "worker_running": worker_running,
        "execution_policy": "LOCAL_SYNTHETIC_ONLY",
        "reason": None if settings.scheduler_enabled else "Scheduler is disabled in backend configuration.",
    }


@router.post("/scheduler", status_code=201)
def create_schedule(value: ScheduleInput, request: Request):
    _operator(request)
    try:
        item = _store(request).create(name=value.name, frequency=value.frequency,
            timezone_name=value.timezone, hour=value.hour, minute=value.minute)
    except ValueError as exc:
        raise HTTPException(422, {"code": "INVALID_SCHEDULE", "message": str(exc)}) from None
    return {"item": item}


@router.patch("/scheduler/{schedule_id}")
def update_schedule(schedule_id: str, value: ScheduleEnabled, request: Request):
    _operator(request)
    if len(schedule_id) != 32 or any(character not in "0123456789abcdef" for character in schedule_id):
        raise HTTPException(404, {"code": "SCHEDULE_NOT_FOUND"})
    item = _store(request).set_enabled(schedule_id, value.enabled)
    if item is None:
        raise HTTPException(404, {"code": "SCHEDULE_NOT_FOUND"})
    if value.enabled and not request.app.state.settings.scheduler_enabled:
        item["execution_blocked"] = "SCHEDULER_DISABLED_IN_BACKEND_CONFIGURATION"
    return {"item": item}
