"""Manual Fetch API; the active backend composition owns its data mode."""

import asyncio
import ipaddress
import json
import logging
import secrets
from datetime import datetime, timezone
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
from app.retention.source_policy import retained_live_wikimedia_evidence_status
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
_logger = logging.getLogger(__name__)


def _has_fresh_live_wikimedia_checkpoint(value) -> bool:
    """Check compact manifests or retained checkpoint maps without reading records."""
    if isinstance(value, dict):
        candidates = []
        for source_id, entry in value.items():
            snapshot = entry.get("snapshot") if isinstance(entry, dict) else None
            result = snapshot.get("result") if isinstance(snapshot, dict) else None
            if isinstance(result, dict):
                # Terminal checkpoint results are keyed by source ID and may
                # omit that ID from the compact provider payload itself.
                candidates.append({
                    **result,
                    **({"source_id": source_id}
                       if isinstance(source_id, str) and "source_id" not in result else {}),
                })
    elif isinstance(value, list):
        candidates = [item for item in value if isinstance(item, dict)]
    else:
        candidates = []
    for source in candidates:
        if retained_live_wikimedia_evidence_status(source) not in {
                "CURRENT", "REQUIRES_RECORD_VALIDATION"}:
            continue
        count = source.get("record_count")
        records = source.get("records")
        if (type(count) is int and count > 0) or (isinstance(records, list) and records):
            return True
    return False


def _algorithm_recovery_pending(status, sources) -> bool:
    return (status in {"CACHE_ONLY", "PENDING", "UNKNOWN"}
            and _has_fresh_live_wikimedia_checkpoint(sources))


def _distributed_for_request(request):
    return (getattr(request.state, "distributed_public_fetch", None)
            or getattr(request.app.state, "distributed_public_fetch", None))


def _is_vercel_postgres_runtime(distributed) -> bool:
    return bool(getattr(distributed, "vercel_postgres_live", False))


def _collection_processing_grants(distributed):
    resolver = getattr(distributed, "resolve_processing_grants", None)
    if not callable(resolver):
        return ()
    try:
        grants = resolver(datetime.now(timezone.utc))
    except Exception:
        return ()
    return tuple(grants) if isinstance(grants, (list, tuple)) else ()


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


async def _recover_expired_source_checkpoints(distributed, fetch_id: str, sources) -> list[dict]:
    """Use retained LIVE source caches only when a terminal checkpoint expired."""
    if not isinstance(sources, list):
        return []
    recovered = [dict(source) for source in sources if isinstance(source, dict)]
    cache_reader = getattr(distributed, "read_cached_source_result", None)
    if not callable(cache_reader):
        return recovered

    history_manifest = {}
    try:
        history_item = next((item for item in distributed.history(limit=100)
                             if isinstance(item, dict) and item.get("fetch_id") == fetch_id), None)
        manifest = history_item.get("source_manifest", []) if history_item else []
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        if isinstance(manifest, list):
            history_manifest = {
                item["source_id"]: item for item in manifest
                if isinstance(item, dict) and isinstance(item.get("source_id"), str)
            }
    except Exception:
        history_manifest = {}

    for index, source in enumerate(recovered):
        if source.get("error_code") not in {
                "RESULT_UNAVAILABLE", "FETCH_DEADLINE_EXCEEDED"}:
            continue
        source_id = source.get("source_id")
        if not isinstance(source_id, str):
            continue
        try:
            cached = await asyncio.to_thread(cache_reader, source_id)
        except Exception:
            cached = None
        records = cached.get("records") if isinstance(cached, dict) else None
        if (isinstance(cached, dict)
                and cached.get("source_id") == source_id
                and cached.get("data_mode") == "LIVE"
                and cached.get("status") in {"SUCCESS", "PARTIAL"}
                and cached.get("delivery_state") in {"CACHED", "STALE_CACHE"}
                and isinstance(records, list)
                and type(cached.get("record_count")) is int
                and cached["record_count"] == len(records)):
            recovered[index] = cached
            continue

        # Keep the terminal reason for sources that were already unavailable
        # without records. A successful old result with no retained payload stays
        # explicitly RESULT_UNAVAILABLE instead of being advertised as saved.
        previous = history_manifest.get(source_id)
        if (isinstance(previous, dict)
                and previous.get("status") in {"FAILED", "UNAVAILABLE"}
                and previous.get("record_count") in {None, 0}):
            source["status"] = previous["status"]
            source["error_code"] = previous.get("error_code") or source["error_code"]
            source["delivery_state"] = previous.get("delivery_state")
            source["coverage"] = previous.get("coverage", {})
        source["data_mode"] = "LIVE"
        source["record_count"] = 0
        source["records"] = []
    return recovered


def _terminal_worker_sources(worker_result: dict | None, fetch_id: str) -> list[dict] | None:
    """Use validated in-memory outcomes only for the worker that just finished this job."""
    if (not isinstance(worker_result, dict)
            or worker_result.get("job_id") != fetch_id
            or worker_result.get("state") not in {"COMPLETED", "PARTIAL"}
            or worker_result.get("dataset_reused") is True
            or not isinstance(worker_result.get("sources"), dict)
            or not worker_result["sources"]):
        return None
    sources = []
    for source_id, outcome in sorted(worker_result["sources"].items()):
        if (not isinstance(source_id, str) or not source_id
                or not isinstance(outcome, dict)
                or not isinstance(outcome.get("records"), list)
                or type(outcome.get("record_count")) is not int
                or outcome["record_count"] != len(outcome["records"])):
            return None
        sources.append({**outcome, "source_id": source_id})
    return sources


async def _process_fetch_collections(request: Request, distributed, fetch_id: str, *,
                                     cached_context_only: bool = False,
                                     worker_result: dict | None = None):
    firestore_shared_live = getattr(distributed, "firestore_shared_live", False)
    shared_store_live = firestore_shared_live or getattr(distributed, "postgres_live", False)
    if shared_store_live:
        reuse_check = getattr(distributed, "collection_reuse_for_fetch", None)
        reusable = (await asyncio.to_thread(reuse_check, fetch_id)
                    if callable(reuse_check) else None)
        if (isinstance(reusable, dict)
                and reusable.get("terminal") is True
                and reusable.get("collection_input_unchanged") is True):
            if firestore_shared_live:
                request.state.vercel_fetch_snapshot_unchanged = True
            _logger.info(
                "Fetch Collections skipped reason=SOURCE_INPUT_UNCHANGED"
            )
            return

    worker_sources = (_terminal_worker_sources(worker_result, fetch_id)
                      if shared_store_live else None)
    if worker_sources is not None:
        result = {
            "terminal": True,
            "fetch_mode": "CACHE_ONLY" if cached_context_only else "LIVE",
            "sources": worker_sources,
        }
    else:
        result = await asyncio.to_thread(
            distributed.status, fetch_id, include_records=True, include_discovery=False
        )
    if result and result.get("terminal"):
        if firestore_shared_live:
            load_snapshot = getattr(distributed, "load_collection_snapshot", None)
            if callable(load_snapshot):
                await asyncio.to_thread(load_snapshot)
        service = (getattr(getattr(request, "state", None), "collection_service", None)
                   or getattr(distributed, "collection_service", None)
                   or getattr(request.app.state, "collection_service", None))
        if service is None:
            return
        if cached_context_only or result.get("fetch_mode") == "CACHE_ONLY":
            processed = await service.process_fetch(
                fetch_id, result.get("sources", []), cached_context_only=True
            )
        else:
            processed = await service.process_fetch(
                fetch_id, result.get("sources", []),
                processing_grants=_collection_processing_grants(distributed),
            )
        if processed.get("status") == "FAILED":
            raise RuntimeError("Fetch Collections processing failed")
        if firestore_shared_live and processed.get("dataset_reused") is True:
            request.state.vercel_fetch_snapshot_unchanged = True
        elif firestore_shared_live:
            request.state.vercel_fetch_snapshot_required = True


async def _run_fetch_and_collections(request: Request, distributed, fetch_id: str,
                                     owner: str, *, run_worker: bool = True,
                                     process_collections: bool = True,
                                     cached_context_only: bool = False):
    """Run or resume a durable Fetch, then classify retained records."""
    result = None
    shared_live = bool(getattr(distributed, "firestore_shared_live", False))
    vercel_shared_live = (
        (shared_live or _is_vercel_postgres_runtime(distributed))
        and request.app.state.settings.environment_profile == "vercel"
    )
    if run_worker:
        terminal_race = False
        # Vercel runs one source per Function invocation. The next same-key POST
        # continues this exact job from its durable source checkpoints.
        try:
            result = (await distributed.run_slice(owner, fetch_id, max_sources=1)
                      if vercel_shared_live else await distributed.run_once(owner))
        except AdmissionDenied as exc:
            if (exc.code == "LEASE_BUSY"
                    or vercel_shared_live and exc.code == "JOB_DEFERRED"):
                # Another invocation owns the lease, or the source has a
                # scheduled retry. The same idempotent job will resume later.
                return
            if not (vercel_shared_live and exc.code in {"JOB_TERMINAL", "JOB_EXPIRED"}):
                raise
            terminal_race = True
            result = None
        if (vercel_shared_live and not terminal_race
                and not (result is not None and result.get("job_id") == fetch_id
                         and result.get("state") in {"COMPLETED", "PARTIAL"})):
            return result
        elif (result and result.get("job_id") != fetch_id
              and result.get("state") in {"COMPLETED", "PARTIAL"}):
            await distributed.run_once(owner)
    if (run_worker and isinstance(result, dict)
            and result.get("job_id") == fetch_id
            and result.get("state") in {"COMPLETED", "PARTIAL"}):
        persist_receipt = getattr(distributed, "persist_terminal_status_receipt", None)
        if callable(persist_receipt):
            try:
                await asyncio.to_thread(persist_receipt, result)
            except Exception as exc:
                receipt_error = str(exc)
                safe_receipt_errors = {
                    "bounded terminal status receipt required",
                    "invalid terminal status source receipt",
                    "terminal status receipt cannot include source record blobs",
                    "invalid terminal status source projection",
                    "terminal status receipt must be finite UTF-8 JSON",
                    "terminal status receipt exceeds its storage budget",
                }
                if receipt_error not in safe_receipt_errors:
                    receipt_error = type(exc).__name__
                _logger.warning(
                    "terminal Fetch status receipt unavailable error=%s", receipt_error,
                )
    if process_collections:
        await _process_fetch_collections(
            request, distributed, fetch_id,
            cached_context_only=shared_live or cached_context_only,
            worker_result=result if run_worker else None,
        )
    return result


@router.post("/fetches", status_code=202)
async def create_fetch(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    fetch_continuation: bool = Header(
        default=False, alias="X-TrendScout-Fetch-Continuation"
    ),
):
    response.headers["Cache-Control"] = "no-store"
    _require_enabled(request)
    validate_public_request_origin(request, request.app.state.settings.environment_profile)
    if not idempotency_key or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise HTTPException(400, {"code": "IDEMPOTENCY_KEY_REQUIRED"})
    if not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
        raise HTTPException(400, {"code": "INVALID_IDEMPOTENCY_KEY"})
    body = await _read_small_json_object(request, allowed_fields={"sources", "mode"})
    fetch_mode = body.get("mode", "LIVE")
    if not isinstance(fetch_mode, str) or fetch_mode not in {"LIVE", "CACHE_ONLY"}:
        raise HTTPException(422, {"code": "INVALID_FETCH_MODE"})
    cache_only = fetch_mode == "CACHE_ONLY"
    selected_sources = body.get("sources")
    if selected_sources is not None and (
        not isinstance(selected_sources, list)
        or not selected_sources
        or any(not isinstance(source, str) for source in selected_sources)
        or len(selected_sources) != len(set(selected_sources))
    ):
        raise HTTPException(422, {"code": "INVALID_SOURCE_SELECTION"})

    distributed = _distributed_for_request(request)
    ensure_postgres_readiness = getattr(
        distributed, "ensure_postgres_live_state_ready", None
    ) if distributed is not None else None
    if callable(ensure_postgres_readiness):
        try:
            ensure_postgres_readiness()
        except AdmissionDenied as exc:
            _raise_distributed_admission_error(exc.code, distributed)
    if cache_only and (distributed is None or getattr(distributed, "data_mode", None) != "LIVE"):
        raise HTTPException(409, {"code": "CACHE_ONLY_UNAVAILABLE"})
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
        vercel_shared_live = (
            (getattr(distributed, "firestore_shared_live", False)
             or _is_vercel_postgres_runtime(distributed))
            and request.app.state.settings.environment_profile == "vercel"
            and getattr(distributed, "data_mode", None) == "LIVE"
        )
        manual_source_refresh = not cache_only and vercel_shared_live and bool(
            effective_sources & {GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID}
        )
        # Vercel SSO Deployment Protection is the authentication boundary for
        # every Production route. Provider and operator secrets stay server-side.
        if YOUTUBE_SOURCE_ID in effective_sources:
            _require_youtube_policy_consent(request, distributed)
        shared_state_policy_sync = None
        policy_sync = (getattr(distributed, "prepare_for_shared_fetch", None)
                       or getattr(distributed, "prepare_for_wikimedia_fetch", None))
        try:
            submit_options = {"source_ids": request_sources}
            if cache_only:
                submit_options["cache_only"] = True
            if manual_source_refresh:
                submit_options["manual_source_refresh"] = True
            accepted = await asyncio.to_thread(distributed.submit, idempotency_key,
                                               **submit_options)
        except AdmissionDenied as exc:
            # Current-policy Fetches use the admission transaction as their
            # single policy check. Only a recognized old ledger needs the
            # explicit, quota-preserving migration before retrying admission.
            if (exc.code != "POLICY_MISMATCH" or not callable(policy_sync)
                    or vercel_shared_live and fetch_continuation):
                _raise_distributed_admission_error(exc.code, distributed)
            try:
                shared_state_policy_sync = policy_sync()
            except AdmissionDenied as sync_error:
                _raise_distributed_admission_error(sync_error.code, distributed)
            except ValueError:
                raise HTTPException(503, {"code": "ADMISSION_STATE_UNAVAILABLE"}) from None
            try:
                accepted = await asyncio.to_thread(distributed.submit, idempotency_key,
                                                   **submit_options)
            except AdmissionDenied as retry_error:
                _raise_distributed_admission_error(retry_error.code, distributed)
            except ValueError:
                raise HTTPException(503, {"code": "ADMISSION_STATE_UNAVAILABLE"}) from None
        except ValueError:
            raise HTTPException(503, {"code": "ADMISSION_STATE_UNAVAILABLE"}) from None
        slice_result = None
        # Do not attach a worker to the first Vercel POST. FastAPI runs
        # BackgroundTasks before the ASGI app returns, and the Vercel shared
        # middleware buffers that response while it keeps Firestore alive.
        # The browser must receive the durable job ID first; its same-key
        # continuation POST then runs exactly one bounded source slice.
        if accepted["status"] in {"QUEUED", "RUNNING"}:
            if vercel_shared_live:
                if (accepted.get("result") in {"REPLAY", "JOINED"}
                        and fetch_continuation):
                    slice_result = await _run_fetch_and_collections(
                        request, distributed, accepted["job_id"],
                        f"api-{secrets.token_hex(12)}",
                        process_collections=not vercel_shared_live,
                        cached_context_only=fetch_mode == "CACHE_ONLY",
                    )
            else:
                background_tasks.add_task(
                    _run_fetch_and_collections, request, distributed, accepted["job_id"],
                    f"api-{secrets.token_hex(12)}",
                    cached_context_only=fetch_mode == "CACHE_ONLY",
                )
        elif (accepted.get("result") == "REPLAY"
              and accepted["status"] in {"COMPLETED", "PARTIAL", "FAILED", "ABORTED", "EXPIRED"}
              and (getattr(distributed, "firestore_shared_live", False)
                   or _is_vercel_postgres_runtime(distributed))):
            # A completed source job can still need Collections persistence
            # after a function was interrupted. Re-read its fenced checkpoints;
            # never start another source worker for a terminal Fetch.
            if vercel_shared_live:
                await _run_fetch_and_collections(
                    request, distributed, accepted["job_id"],
                    f"api-{secrets.token_hex(12)}", run_worker=False,
                    process_collections=not vercel_shared_live,
                    cached_context_only=fetch_mode == "CACHE_ONLY",
                )
            else:
                background_tasks.add_task(
                    _run_fetch_and_collections, request, distributed, accepted["job_id"],
                    f"api-{secrets.token_hex(12)}", run_worker=False,
                    cached_context_only=fetch_mode == "CACHE_ONLY",
                )
        fetch_id = accepted["job_id"]
        terminal_fetch = None
        if (vercel_shared_live and fetch_continuation
                and isinstance(slice_result, dict)
                and slice_result.get("job_id") == fetch_id
                and slice_result.get("state") in {"COMPLETED", "PARTIAL"}):
            results = slice_result.get("sources")
            if (isinstance(results, dict) and set(results) == set(request_sources)
                    and all(isinstance(item, dict) for item in results.values())):
                try:
                    build_terminal_status = getattr(
                        distributed, "terminal_status_from_worker_result", None
                    )
                    status = (build_terminal_status(
                        slice_result, created_at=accepted.get("created_at")
                    ) if callable(build_terminal_status) else None)
                    if (isinstance(status, dict) and status.get("fetch_id") == fetch_id
                            and status.get("terminal") is True
                            and status.get("status") in {"COMPLETED", "PARTIAL"}):
                        status["fetch_mode"] = fetch_mode
                        terminal_fetch = status
                except Exception as exc:
                    # The worker has already committed. The normal status GET
                    # remains the recovery path if this optional read fails.
                    _logger.warning("Terminal Fetch status prefetch failed: %s", type(exc).__name__)
        response.headers["Idempotent-Replay"] = "true" if accepted["result"] == "REPLAY" else "false"
        return {
            "fetch_id": fetch_id,
            "status": accepted["status"],
            "data_mode": distributed.data_mode,
            "fetch_mode": fetch_mode,
            "requested_sources": sorted(set(requested_sources)),
            "sources": request_sources,
            "created_at": accepted.get("created_at"),
            "status_url": f"/api/v1/fetches/{fetch_id}",
            **({"continuation_ready": bool(
                isinstance(slice_result, dict)
                and slice_result.get("job_id") == fetch_id
                and slice_result.get("state") == "QUEUED"
            )} if vercel_shared_live and fetch_continuation else {}),
            **({"terminal_fetch": terminal_fetch} if terminal_fetch is not None else {}),
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
    if (distributed is None
            or not (getattr(distributed, "firestore_shared_live", False)
                    or getattr(distributed, "postgres_live", False))
            or getattr(distributed, "data_mode", None) != "LIVE"):
        raise HTTPException(409, {"code": "LIVE_CHECKPOINT_RECOVERY_UNAVAILABLE"})

    # The compact Fetch receipt and latest dataset pointer are sufficient for
    # unchanged cache-only recovery. Avoid restoring the complete SQLite image
    # and downloading source record blobs unless Collections really needs work.
    firestore_shared_live = getattr(distributed, "firestore_shared_live", False)
    shared_store_live = firestore_shared_live or getattr(distributed, "postgres_live", False)
    resume_pending_algorithms = False
    reuse_check = getattr(distributed, "collection_reuse_for_fetch", None)
    reusable = (await asyncio.to_thread(reuse_check, fetch_id)
                if callable(reuse_check) else None)
    if (shared_store_live and isinstance(reusable, dict)
            and reusable.get("terminal") is True
            and reusable.get("collection_input_unchanged") is True):
        resume_pending_algorithms = _algorithm_recovery_pending(
            reusable.get("algorithm_processing_status"), reusable.get("source_manifest"),
        )
        if not resume_pending_algorithms:
            if firestore_shared_live:
                request.state.vercel_fetch_snapshot_unchanged = True
            _logger.info(
                "Fetch Collections recovery skipped reason=SOURCE_INPUT_UNCHANGED"
            )
            return {
                "fetch_id": fetch_id,
                "status": "REUSED",
                "error_code": None,
                "collection_input_unchanged": True,
                **({"dataset_id": reusable["dataset_id"]}
                   if isinstance(reusable.get("dataset_id"), str) else {}),
            }

    load_snapshot = getattr(distributed, "load_collection_snapshot", None)
    if firestore_shared_live and callable(load_snapshot):
        await asyncio.to_thread(load_snapshot)
    service = (getattr(getattr(request, "state", None), "collection_service", None)
               or getattr(distributed, "collection_service", None)
               or getattr(request.app.state, "collection_service", None))
    if (service is None or getattr(service.store, "data_mode_scope", None) != "LIVE"):
        raise HTTPException(409, {"code": "LIVE_CHECKPOINT_RECOVERY_UNAVAILABLE"})

    # A completed, persisted Collections job is already the outcome that
    # process_fetch would return for these retained receipts. Avoid downloading
    # and rediscovering the same source records merely to replay that result.
    read_collection_result = getattr(service.store, "fetch_collection_processing_result", None)
    read_job = getattr(getattr(distributed, "admission", None), "read_job", None)
    clock = getattr(distributed, "utcnow", None)
    read_collection_job = getattr(service.store, "collection_job", None)
    collection_job = None
    if callable(read_collection_job):
        collection_job, _created = await asyncio.to_thread(read_collection_job, fetch_id)
        collection_job = dict(collection_job) if collection_job is not None else None
        stored_ai_jobs = collection_job.get("ai_jobs", []) if collection_job else []
        if isinstance(stored_ai_jobs, str):
            try:
                stored_ai_jobs = json.loads(stored_ai_jobs)
            except (TypeError, ValueError):
                stored_ai_jobs = []
        if (collection_job
                and collection_job.get("status") == "PROCESSING"
                and isinstance(stored_ai_jobs, list) and stored_ai_jobs):
            return {"fetch_id": fetch_id, "status": "PROCESSING",
                    "ai_job_ids": [item["job_id"] for item in stored_ai_jobs
                                   if isinstance(item, dict)
                                   and isinstance(item.get("job_id"), str)]}
    if callable(read_collection_result) and callable(read_job) and callable(clock):
        prior = await asyncio.to_thread(read_collection_result, fetch_id)
        if prior is not None:
            try:
                job = await asyncio.to_thread(read_job, fetch_id, now=clock())
            except AdmissionDenied as exc:
                _raise_distributed_admission_error(exc.code, distributed)
            checkpoints = job.get("source_checkpoints") if isinstance(job, dict) else None
            if (isinstance(job, dict)
                    and job.get("state") in {"COMPLETED", "PARTIAL", "FAILED", "EXPIRED", "ABORTED"}
                    and isinstance(checkpoints, dict)
                    and all(isinstance(entry, dict)
                            and isinstance(entry.get("snapshot"), dict)
                            and entry["snapshot"].get("data_mode") == "LIVE"
                            for entry in checkpoints.values())):
                resume_pending_algorithms = resume_pending_algorithms or _algorithm_recovery_pending(
                    (collection_job or {}).get("algorithm_processing_status"), checkpoints,
                )
                if not resume_pending_algorithms:
                    if firestore_shared_live:
                        request.state.vercel_fetch_snapshot_unchanged = True
                    stored_ai_jobs = collection_job.get("ai_jobs", []) if collection_job else []
                    if isinstance(stored_ai_jobs, str):
                        try:
                            stored_ai_jobs = json.loads(stored_ai_jobs)
                        except (TypeError, ValueError):
                            stored_ai_jobs = []
                    return {"fetch_id": fetch_id, "status": prior["status"],
                            "error_code": prior["error_code"],
                            "ai_job_ids": [item["job_id"] for item in stored_ai_jobs
                                           if isinstance(item, dict)
                                           and isinstance(item.get("job_id"), str)]}
    try:
        result = await asyncio.to_thread(
            distributed.status, fetch_id, include_records=True,
            include_discovery=False,
        )
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

    sources = await _recover_expired_source_checkpoints(
        distributed, fetch_id, result.get("sources", [])
    )
    resume_options = ({"resume_model_unavailable": True}
                      if resume_pending_algorithms else {})
    recovered = await service.process_fetch(
        fetch_id, sources, cached_context_only=True, **resume_options,
    )
    if recovered.get("status") == "FAILED":
        raise HTTPException(503, {"code": "COLLECTION_PROCESSING_FAILED"})
    if firestore_shared_live and recovered.get("dataset_reused") is True:
        request.state.vercel_fetch_snapshot_unchanged = True
    return {"fetch_id": fetch_id, "status": recovered.get("status", "FAILED"),
            "error_code": recovered.get("error_code"),
            **({"collection_input_unchanged": True,
                "dataset_id": recovered.get("dataset_id")}
               if recovered.get("dataset_reused") is True else {})}


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
        accepted = await asyncio.to_thread(
            distributed.submit, idempotency_key, history_days=days
        )
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
            result = await asyncio.to_thread(distributed.status, fetch_id)
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
    if code in {"IDEMPOTENCY_KEY_CONFLICT", "HISTORICAL_FETCH_UNAVAILABLE",
                "CACHE_ONLY_UNAVAILABLE"}:
        raise HTTPException(409, {"code": code}) from None
    raise HTTPException(503, {"code": code}) from None
