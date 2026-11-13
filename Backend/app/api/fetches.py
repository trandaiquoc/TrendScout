"""Anonymous one-click Fetch API; execution remains offline synthetic only."""

import ipaddress
import json
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response

from app.jobs.public_admission import AdmissionDenied
from app.orchestration.public_fetch import (
    FETCH_ID_RE,
    FETCH_WINDOW_SECONDS,
    IDEMPOTENCY_KEY_RE,
    PublicFetchBusy,
    PublicFetchNotFound,
    PublicFetchPollLimit,
    PublicFetchRateLimited,
    request_fingerprint,
)
from app.orchestration.runs import TERMINAL, RunBusy, RunCooldown
from app.ports.storage import StorageError

router = APIRouter()
MAX_BODY_BYTES = 1024
MAX_IDEMPOTENCY_KEY_LENGTH = 128


def _origin_tuple(value: str):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid origin")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), parsed.hostname.lower().rstrip("."), port


def _allowed_local_host(host: str, profile: str) -> bool:
    normalized = host.lower().rstrip(".")
    if normalized == "localhost":
        return True
    if profile == "test" and normalized in {"testserver", "test"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_public_request_origin(request: Request, profile: str) -> None:
    """Reject ambiguous host/proxy context; these checks are not caller authentication."""
    if any(
        name in request.headers
        for name in (
            "forwarded",
            "x-forwarded-host",
            "x-forwarded-proto",
            "x-forwarded-port",
            "x-forwarded-for",
            "x-forwarded-prefix",
            "x-forwarded-ssl",
            "x-real-ip",
            "x-original-host",
            "x-original-url",
            "via",
        )
    ):
        raise HTTPException(403, {"code": "UNTRUSTED_PROXY_HEADERS"})

    host = request.url.hostname
    if not host or not _allowed_local_host(host, profile):
        raise HTTPException(403, {"code": "HOST_NOT_ALLOWED"})

    origin = request.headers.get("origin")
    if origin is not None:
        try:
            expected = _origin_tuple(str(request.base_url).rstrip("/"))
            supplied = _origin_tuple(origin)
        except ValueError:
            raise HTTPException(403, {"code": "ORIGIN_NOT_ALLOWED"}) from None
        if supplied != expected:
            raise HTTPException(403, {"code": "ORIGIN_NOT_ALLOWED"})

    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site.lower() not in {"same-origin", "none"}:
        raise HTTPException(403, {"code": "CROSS_SITE_REQUEST"})


async def _read_small_json_object(request: Request) -> dict:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(415, {"code": "JSON_REQUIRED"})
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                raise HTTPException(413, {"code": "REQUEST_TOO_LARGE"})
        except ValueError:
            raise HTTPException(400, {"code": "INVALID_CONTENT_LENGTH"}) from None

    parts = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise HTTPException(413, {"code": "REQUEST_TOO_LARGE"})
        parts.append(chunk)
    try:
        value = json.loads(b"".join(parts))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, {"code": "INVALID_JSON"}) from None
    if not isinstance(value, dict):
        raise HTTPException(422, {"code": "EMPTY_OBJECT_REQUIRED"})
    if value:
        raise HTTPException(422, {"code": "UNKNOWN_REQUEST_FIELDS"})
    return value


def _require_enabled(request: Request) -> None:
    if not request.app.state.public_fetch_enabled:
        raise HTTPException(
            503,
            {
                "code": "PUBLIC_FETCH_DISABLED",
                "reason": request.app.state.public_fetch_block_reason,
            },
            headers={"Cache-Control": "no-store"},
        )


@router.post("/fetches", status_code=202)
async def create_fetch(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    response.headers["Cache-Control"] = "no-store"
    _require_enabled(request)
    validate_public_request_origin(request, request.app.state.settings.environment_profile)
    if not idempotency_key or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise HTTPException(400, {"code": "IDEMPOTENCY_KEY_REQUIRED"})
    if not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
        raise HTTPException(400, {"code": "INVALID_IDEMPOTENCY_KEY"})
    await _read_small_json_object(request)

    distributed = request.app.state.distributed_public_fetch
    if distributed is not None:
        try:
            accepted = distributed.submit(idempotency_key)
        except AdmissionDenied as exc:
            _raise_distributed_admission_error(exc.code, distributed)
        except ValueError:
            raise HTTPException(503, {"code": "ADMISSION_STATE_UNAVAILABLE"}) from None
        # Background work is only enabled by explicit local/test composition.
        # The durable queue remains authoritative if this process stops.
        if accepted["status"] in {"QUEUED", "RUNNING"}:
            background_tasks.add_task(
                distributed.run_once, f"api-{secrets.token_hex(12)}"
            )
        fetch_id = accepted["job_id"]
        response.headers["Idempotent-Replay"] = "true" if accepted["result"] == "REPLAY" else "false"
        return {
            "fetch_id": fetch_id,
            "status": accepted["status"],
            "data_mode": distributed.data_mode,
            "sources": sorted(distributed.admission.policy.enabled_sources),
            "created_at": accepted.get("created_at"),
            "status_url": f"/api/v1/fetches/{fetch_id}",
        }

    run_service = request.app.state.run_service
    if run_service is None:
        raise HTTPException(503, {"code": "SYNTHETIC_RUNTIME_UNAVAILABLE"})
    try:
        record, replay = await request.app.state.fetch_registry.accept(
            idempotency_key,
            request_fingerprint(request.app.state.settings, request.app.state.public_fetch_source_ids),
            start=lambda: run_service.start(
                execution_timeout_seconds=request.app.state.public_fetch_deadline_seconds
            ),
            storage=run_service.storage,
        )
    except ValueError as exc:
        code = "IDEMPOTENCY_KEY_CONFLICT" if "conflicts" in str(exc) else "INVALID_IDEMPOTENCY_KEY"
        status = 409 if code == "IDEMPOTENCY_KEY_CONFLICT" else 400
        raise HTTPException(status, {"code": code}) from None
    except PublicFetchBusy:
        raise HTTPException(409, {"code": "FETCH_ALREADY_RUNNING"}) from None
    except PublicFetchRateLimited:
        raise HTTPException(
            429,
            {"code": "PUBLIC_FETCH_RATE_LIMITED", "retry_after_seconds": FETCH_WINDOW_SECONDS},
            headers={"Retry-After": str(FETCH_WINDOW_SECONDS)},
        ) from None
    except RunCooldown:
        cooldown = request.app.state.settings.run_cooldown_seconds
        retry_after = max(1, cooldown)
        raise HTTPException(
            429,
            {"code": "RUN_COOLDOWN_ACTIVE", "retry_after_seconds": retry_after},
            headers={"Retry-After": str(retry_after)},
        ) from None
    except RunBusy:
        raise HTTPException(409, {"code": "FETCH_ALREADY_RUNNING"}) from None
    except StorageError:
        raise HTTPException(503, {"code": "FETCH_STORAGE_UNAVAILABLE"}) from None

    accepted_status = "QUEUED"
    if replay:
        try:
            existing_run = await run_service.storage.get_run(record.run_id)
            if existing_run:
                accepted_status = existing_run.get("status", "QUEUED")
        except StorageError:
            raise HTTPException(503, {"code": "FETCH_STATUS_UNAVAILABLE"}) from None
    response.headers["Idempotent-Replay"] = "true" if replay else "false"
    return {
        "fetch_id": record.fetch_id,
        "status": accepted_status,
        "data_mode": "SYNTHETIC",
        "sources": list(request.app.state.public_fetch_source_ids),
        "query_plan_id": request.app.state.settings.query_plan_id,
        "query_plan_version": request.app.state.settings.query_plan_version,
        "created_at": record.created_at,
        "status_url": f"/api/v1/fetches/{record.fetch_id}",
    }


@router.get("/fetches/{fetch_id}")
async def get_fetch(fetch_id: str, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    _require_enabled(request)
    if not FETCH_ID_RE.fullmatch(fetch_id):
        raise HTTPException(404, {"code": "FETCH_NOT_FOUND"})
    distributed = request.app.state.distributed_public_fetch
    if distributed is not None:
        validate_public_request_origin(request, request.app.state.settings.environment_profile)
        try:
            result = distributed.status(fetch_id)
        except AdmissionDenied as exc:
            _raise_distributed_admission_error(exc.code, distributed)
        if result is None:
            raise HTTPException(404, {"code": "FETCH_NOT_FOUND"})
        return result
    try:
        record = await request.app.state.fetch_registry.get(fetch_id)
    except PublicFetchNotFound:
        raise HTTPException(404, {"code": "FETCH_NOT_FOUND"}) from None
    except PublicFetchPollLimit:
        raise HTTPException(429, {"code": "STATUS_POLL_LIMIT"}) from None

    service = request.app.state.run_service
    try:
        run = await service.storage.get_run(record.run_id)
    except StorageError:
        raise HTTPException(503, {"code": "FETCH_STATUS_UNAVAILABLE"}) from None
    if run is None:
        raise HTTPException(404, {"code": "FETCH_NOT_FOUND"})
    status = run.get("status", "QUEUED")
    error_code = {
        "PARTIAL": "SOURCE_COVERAGE_PARTIAL",
        "FAILED": "FETCH_FAILED",
        "ABORTED": "FETCH_ABORTED",
    }.get(status)
    return {
        "fetch_id": record.fetch_id,
        "run_id": record.run_id,
        "status": status,
        "stage": status,
        "data_mode": "SYNTHETIC",
        "sources": list(request.app.state.public_fetch_source_ids),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "updated_at": run.get("updated_at") or run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "error_code": error_code,
        "message": "Synthetic offline fetch completed with partial source coverage."
        if status == "PARTIAL"
        else "Synthetic offline fetch failed safely."
        if status in {"FAILED", "ABORTED"}
        else None,
        "terminal": status in TERMINAL,
    }


def _raise_distributed_admission_error(code: str, service) -> None:
    if code == "JOB_NOT_FOUND":
        raise HTTPException(404, {"code": "FETCH_NOT_FOUND"}) from None
    if code in {"RATE_LIMITED", "PROVIDER_RATE_LIMITED", "COOLDOWN", "STATUS_POLL_LIMIT"}:
        retry_after = service.admission.policy.window_seconds
        provider_retry_after = getattr(service, "provider_retry_after_seconds", None)
        if code == "PROVIDER_RATE_LIMITED" and callable(provider_retry_after):
            try:
                retry_after = max(1, provider_retry_after())
            except Exception:
                raise HTTPException(503, {"code": "ADMISSION_STATE_UNAVAILABLE"}) from None
        if code == "COOLDOWN":
            retry_after = max(1, service.admission.policy.cooldown_seconds)
        raise HTTPException(429, {"code": code}, headers={"Retry-After": str(retry_after)}) from None
    if code in {"LEASE_BUSY", "FETCH_ALREADY_RUNNING"}:
        raise HTTPException(409, {"code": "FETCH_ALREADY_RUNNING"}) from None
    raise HTTPException(503, {"code": code}) from None
