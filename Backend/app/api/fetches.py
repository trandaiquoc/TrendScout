"""Manual Fetch API; the active backend composition owns its data mode."""

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
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)
from app.sources.youtube.policy import YOUTUBE_POLICY_VERSION_HEADER

router = APIRouter()
MAX_BODY_BYTES = 1024
MAX_IDEMPOTENCY_KEY_LENGTH = 128


def _distributed_for_request(request):
    return (getattr(request.state, "distributed_public_fetch", None)
            or getattr(request.app.state, "distributed_public_fetch", None))


def _require_youtube_policy_consent(request: Request, distributed) -> None:
    """Require reviewed policy and explicit browser acceptance for API Client use."""
    workflow = getattr(distributed, "youtube_capabilities", None)
    if not isinstance(workflow, dict):
        return
    if workflow.get("policy_status") != "APPROVED":
        raise HTTPException(409, {"code": "YOUTUBE_POLICY_NOT_APPROVED"})
    version = workflow.get("policy_version")
    if (not isinstance(version, str) or not version
            or request.headers.get(YOUTUBE_POLICY_VERSION_HEADER.lower()) != version):
        raise HTTPException(428, {"code": "YOUTUBE_POLICY_CONSENT_REQUIRED"})


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
    if profile == "vercel":
        host_header = request.headers.get("host")
        try:
            parsed_host = urlsplit(f"https://{host_header}")
            if (not host_header or parsed_host.hostname is None
                    or parsed_host.username is not None or parsed_host.password is not None
                    or parsed_host.path or parsed_host.query or parsed_host.fragment
                    or (parsed_host.port not in {None, 443})):
                raise ValueError("invalid host")
            expected = _origin_tuple(f"https://{parsed_host.netloc}")
        except ValueError:
            raise HTTPException(403, {"code": "HOST_NOT_ALLOWED"}) from None

        origin = request.headers.get("origin")
        if origin is not None or request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            try:
                supplied = _origin_tuple(origin or "")
            except ValueError:
                raise HTTPException(403, {"code": "ORIGIN_NOT_ALLOWED"}) from None
            if supplied != expected or supplied[0] != "https":
                raise HTTPException(403, {"code": "ORIGIN_NOT_ALLOWED"})

        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site and fetch_site.lower() not in {"same-origin", "none"}:
            raise HTTPException(403, {"code": "CROSS_SITE_REQUEST"})
        return

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


async def _read_small_json_object(request: Request, *, allowed_fields=()) -> dict:
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
    if set(value) - set(allowed_fields):
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


async def _process_fetch_collections(request: Request, distributed, fetch_id: str, *,
                                     cached_context_only: bool = False):
    service = (getattr(getattr(request, "state", None), "collection_service", None)
               or getattr(request.app.state, "collection_service", None))
    if service is None:
        return
    result = distributed.status(fetch_id, include_records=True)
    if result and result.get("terminal"):
        if cached_context_only:
            processed = await service.process_fetch(
                fetch_id, result.get("sources", []), cached_context_only=True
            )
        else:
            processed = await service.process_fetch(fetch_id, result.get("sources", []))
        if processed.get("status") == "FAILED":
            raise RuntimeError("Fetch Collections processing failed")


async def _run_fetch_and_collections(request: Request, distributed, fetch_id: str,
                                     owner: str, *, run_worker: bool = True):
    """Run or resume a durable Fetch, then classify retained records."""
    if run_worker:
        # The bounded one-worker queue can contain at most one admitted live Fetch at a time.
        try:
            result = await distributed.run_once(owner)
        except AdmissionDenied as exc:
            if exc.code == "LEASE_BUSY":
                # Another invocation still owns the fenced attempt. Its durable
                # checkpoints remain authoritative; status polling will resume
                # this same idempotent job after that lease expires.
                return
            raise
        if (result and result.get("job_id") != fetch_id
                and result.get("state") in {"COMPLETED", "PARTIAL"}):
            await distributed.run_once(owner)
    shared_live = bool(getattr(distributed, "firestore_shared_live", False))
    await _process_fetch_collections(
        request, distributed, fetch_id, cached_context_only=shared_live
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
    body = await _read_small_json_object(request, allowed_fields={"sources"})
    selected_sources = body.get("sources")
    if selected_sources is not None and (
        not isinstance(selected_sources, list)
        or not selected_sources
        or any(not isinstance(source, str) for source in selected_sources)
        or len(selected_sources) != len(set(selected_sources))
    ):
        raise HTTPException(422, {"code": "INVALID_SOURCE_SELECTION"})

    distributed = _distributed_for_request(request)
    if distributed is not None:
        available_sources = getattr(
            distributed, "available_sources", distributed.admission.policy.enabled_sources
        )
        if (selected_sources is not None
                and not set(selected_sources) <= available_sources):
            raise HTTPException(422, {"code": "INVALID_SOURCE_SELECTION"})
        requested_sources = (selected_sources if selected_sources is not None
                             else sorted(available_sources))
        effective_sources = set(requested_sources)
        # Bing verification requires a saved Google Trends observation. Make
        # that dependency explicit in the durable job so a Bing-only click
        # first serves/refreshes Google through the shared cache, then runs Bing.
        if (BING_SOURCE_ID in effective_sources
                and getattr(distributed, "data_mode", None) == "LIVE"
                and getattr(distributed, "normal_bing_live", False)):
            effective_sources.add(GOOGLE_SOURCE_ID)
        request_sources = sorted(effective_sources)
        if YOUTUBE_SOURCE_ID in effective_sources:
            _require_youtube_policy_consent(request, distributed)
        shared_state_policy_sync = None
        policy_sync = (getattr(distributed, "prepare_for_shared_fetch", None)
                       or getattr(distributed, "prepare_for_wikimedia_fetch", None))
        if callable(policy_sync):
            try:
                shared_state_policy_sync = policy_sync()
            except AdmissionDenied as exc:
                _raise_distributed_admission_error(exc.code, distributed)
            except ValueError:
                raise HTTPException(503, {"code": "ADMISSION_STATE_UNAVAILABLE"}) from None
        try:
            accepted = distributed.submit(idempotency_key, source_ids=request_sources)
        except AdmissionDenied as exc:
            _raise_distributed_admission_error(exc.code, distributed)
        except ValueError:
            raise HTTPException(503, {"code": "ADMISSION_STATE_UNAVAILABLE"}) from None
        # Vercel's request-scoped middleware keeps Firestore resources alive
        # through this worker; the durable queue remains authoritative on retry.
        if accepted["status"] in {"QUEUED", "RUNNING"}:
            background_tasks.add_task(
                _run_fetch_and_collections, request, distributed, accepted["job_id"],
                f"api-{secrets.token_hex(12)}"
            )
        elif (accepted.get("result") == "REPLAY"
              and accepted["status"] in {"COMPLETED", "PARTIAL", "FAILED", "ABORTED", "EXPIRED"}
              and getattr(distributed, "firestore_shared_live", False)):
            # A completed source job can still need Collections persistence
            # after a function was interrupted. Re-read its fenced checkpoints;
            # never start another source worker for a terminal Fetch.
            background_tasks.add_task(
                _run_fetch_and_collections, request, distributed, accepted["job_id"],
                f"api-{secrets.token_hex(12)}", run_worker=False,
            )
        fetch_id = accepted["job_id"]
        response.headers["Idempotent-Replay"] = "true" if accepted["result"] == "REPLAY" else "false"
        return {
            "fetch_id": fetch_id,
            "status": accepted["status"],
            "data_mode": distributed.data_mode,
            "requested_sources": sorted(set(requested_sources)),
            "sources": request_sources,
            "created_at": accepted.get("created_at"),
            "status_url": f"/api/v1/fetches/{fetch_id}",
            **({"shared_state_policy_sync": shared_state_policy_sync}
               if shared_state_policy_sync is not None else {}),
        }
    if selected_sources is not None:
        raise HTTPException(409, {"code": "SOURCE_SELECTION_UNAVAILABLE"})

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


@router.post("/fetches/{fetch_id}/collections")
async def recover_fetch_collections(fetch_id: str, request: Request, response: Response):
    """Persist retained LIVE source checkpoints without admitting provider work."""
    response.headers["Cache-Control"] = "no-store"
    _require_enabled(request)
    validate_public_request_origin(request, request.app.state.settings.environment_profile)
    if not FETCH_ID_RE.fullmatch(fetch_id):
        raise HTTPException(404, {"code": "FETCH_NOT_FOUND"})
    distributed = _distributed_for_request(request)
    service = (getattr(getattr(request, "state", None), "collection_service", None)
               or getattr(request.app.state, "collection_service", None))
    if (distributed is None
            or not getattr(distributed, "firestore_shared_live", False)
            or getattr(distributed, "data_mode", None) != "LIVE"
            or service is None
            or getattr(service.store, "data_mode_scope", None) != "LIVE"):
        raise HTTPException(409, {"code": "LIVE_CHECKPOINT_RECOVERY_UNAVAILABLE"})
    try:
        result = distributed.status(fetch_id, include_records=True)
    except AdmissionDenied as exc:
        _raise_distributed_admission_error(exc.code, distributed)
    if result is None:
        raise HTTPException(404, {"code": "FETCH_NOT_FOUND"})
    if result.get("terminal") is not True:
        raise HTTPException(409, {"code": "FETCH_NOT_TERMINAL"})
    if result.get("data_mode") not in {None, "LIVE"} or any(
        source.get("data_mode") not in {None, "LIVE"}
        for source in result.get("sources", []) if isinstance(source, dict)
    ):
        raise HTTPException(409, {"code": "FETCH_MODE_MISMATCH"})

    recovered = await service.process_fetch(
        fetch_id, result.get("sources", []), cached_context_only=True
    )
    if recovered.get("status") == "FAILED":
        raise HTTPException(503, {"code": "COLLECTION_PROCESSING_FAILED"})
    return {"fetch_id": fetch_id, "status": recovered.get("status", "FAILED"),
            "error_code": recovered.get("error_code")}


@router.post("/fetches/history", status_code=202)
async def create_history_fetch(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Explicitly collect at most two recent LIVE Wikimedia observation dates."""
    response.headers["Cache-Control"] = "no-store"
    _require_enabled(request)
    validate_public_request_origin(request, request.app.state.settings.environment_profile)
    if not idempotency_key or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise HTTPException(400, {"code": "IDEMPOTENCY_KEY_REQUIRED"})
    if not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
        raise HTTPException(400, {"code": "INVALID_IDEMPOTENCY_KEY"})
    body = await _read_small_json_object(request, allowed_fields={"days"})
    days = body.get("days")
    if set(body) != {"days"} or type(days) is not int or not 1 <= days <= 2:
        raise HTTPException(422, {"code": "HISTORY_WINDOW_OUT_OF_RANGE", "maximum_days": 2})
    distributed = _distributed_for_request(request)
    if distributed is None or not getattr(distributed, "normal_wikimedia_live", False):
        raise HTTPException(409, {"code": "HISTORICAL_FETCH_UNAVAILABLE"})
    try:
        accepted = distributed.submit(idempotency_key, history_days=days)
    except AdmissionDenied as exc:
        _raise_distributed_admission_error(exc.code, distributed)
    except ValueError:
        raise HTTPException(503, {"code": "ADMISSION_STATE_UNAVAILABLE"}) from None
    if accepted["status"] in {"QUEUED", "RUNNING"}:
        background_tasks.add_task(
            _run_fetch_and_collections, request, distributed, accepted["job_id"],
            f"api-{secrets.token_hex(12)}"
        )
    response.headers["Idempotent-Replay"] = "true" if accepted["result"] == "REPLAY" else "false"
    return {
        "fetch_id": accepted["job_id"],
        "status": accepted["status"],
        "data_mode": "LIVE",
        "sources": [WIKIMEDIA_SOURCE_ID],
        "request_kind": "WIKIMEDIA_HISTORY",
        "history_days": days,
        "created_at": accepted.get("created_at"),
        "status_url": f"/api/v1/fetches/{accepted['job_id']}",
    }


@router.get("/fetches/{fetch_id}")
async def get_fetch(fetch_id: str, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    _require_enabled(request)
    if not FETCH_ID_RE.fullmatch(fetch_id):
        raise HTTPException(404, {"code": "FETCH_NOT_FOUND"})
    distributed = _distributed_for_request(request)
    if distributed is not None:
        validate_public_request_origin(request, request.app.state.settings.environment_profile)
        try:
            result = distributed.status(fetch_id)
        except AdmissionDenied as exc:
            _raise_distributed_admission_error(exc.code, distributed)
        if result is None:
            raise HTTPException(404, {"code": "FETCH_NOT_FOUND"})
        if YOUTUBE_SOURCE_ID in result.get("selected_sources", []):
            _require_youtube_policy_consent(request, distributed)
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
    if code in {"LEASE_BUSY", "FETCH_ALREADY_RUNNING", "POLICY_MIGRATION_BUSY"}:
        raise HTTPException(409, {"code": "FETCH_ALREADY_RUNNING"}) from None
    if code in {"IDEMPOTENCY_KEY_CONFLICT", "HISTORICAL_FETCH_UNAVAILABLE"}:
        raise HTTPException(409, {"code": code}) from None
    raise HTTPException(503, {"code": code}) from None
