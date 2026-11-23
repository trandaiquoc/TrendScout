"""Application entry point."""

import asyncio
import base64
import binascii
import hmac
import inspect
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Match

from app.api.algorithms import router as algorithms_router
from app.api.analytics import router as analytics_router
from app.api.capabilities import router as capabilities_router
from app.api.collections import router as collections_router
from app.api.datasets import router as datasets_router
from app.api.fetches import router as fetches_router
from app.api.health import router as health_router
from app.api.research import router as research_router
from app.api.runs import router as runs_router
from app.api.scheduler import router as scheduler_router
from app.collections.entity_context import WikimediaEntityContextResolver
from app.composition import build_run_service, read_only_storage
from app.local_live_guard import LocalLiveGuard
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.orchestration.public_fetch import (
    FETCH_TTL_SECONDS,
    FETCH_WINDOW_SECONDS,
    MAX_FETCHES_PER_WINDOW,
    MAX_STATUS_POLLS,
    PUBLIC_SOURCE_IDS,
    PublicFetchRegistry,
    SingleProcessGuard,
)
from app.orchestration.runs import HostRunLock, RunService
from app.ports.storage import StorageError
from app.scheduling.persistent import ScheduleWorker
from app.settings import Settings
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)

_logger = logging.getLogger(__name__)
_SAFE_EXCEPTION_CLASS = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_VERCEL_STATIC_API_TIMING_ROUTES = frozenset({
    "/api/v1/algorithms/results",
    "/api/v1/capabilities",
    "/api/v1/collections",
    "/api/v1/datasets/latest",
    "/api/v1/datasets/latest/keywords",
    "/api/v1/research",
    "/api/v1/runs/history",
    "/api/v1/runtime",
})
_SAFE_SNAPSHOT_FAILURE_REASONS = frozenset({
    "INVALID_SHARED_POINTER",
    "RESTORE_FAILED",
    "WORKING_DATABASE_UNAVAILABLE",
    "SNAPSHOT_PATH_EXISTS",
    "DATABASE_TOO_LARGE",
    "DATABASE_SNAPSHOT_FAILED",
    "COMPRESSED_SNAPSHOT_TOO_LARGE",
    "BLOB_UPLOAD_FAILED",
    "SAVE_CONFIRMATION_FAILED",
    "SNAPSHOT_COMMIT_FAILED",
})
_YOUTUBE_RETENTION_SWEEP_INTERVAL_SECONDS = 60 * 60


async def _run_youtube_retention_sweeper(cleanup, *, sleep=asyncio.sleep):
    """Run local YouTube-only cache expiry without making provider requests."""
    while True:
        try:
            await asyncio.to_thread(cleanup)
        except Exception:
            _logger.exception("Local YouTube cache retention sweep failed")
        await sleep(_YOUTUBE_RETENTION_SWEEP_INTERVAL_SECONDS)


async def _close_request_client(client):
    close = getattr(client, "close", None)
    if callable(close):
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass


def _is_vercel_shared_live_request(settings, method, path, *, enabled):
    if (settings.environment_profile != "vercel" or not enabled
            or not settings.vercel_shared_live_enabled):
        return False
    method = method.upper()
    return (
        method == "POST" and path == "/api/v1/fetches"
        or method == "POST" and _is_vercel_fetch_collections_recovery(path)
        or method == "GET" and (
            path.startswith("/api/v1/fetches/")
            or path == "/api/v1/runs/history"
            or path.startswith("/api/v1/runs/history/")
            or path.startswith("/api/v1/analytics/")
            or path == "/api/v1/algorithms/results"
            or path == "/api/v1/capabilities"
            or path == "/api/v1/runtime"
            or path == "/api/v1/research"
            # The Collections workbench reads keyword rows through these
            # legacy dataset routes. They must share its request-scoped
            # Firestore-backed CollectionService instead of the empty Vercel
            # app state.
            or path in {
                "/api/v1/datasets/latest",
                "/api/v1/datasets/latest/keywords",
            }
        )
        or path == "/api/v1/collections" and method in {"GET", "POST"}
        or path.startswith("/api/v1/collections/")
        and method in {"GET", "POST", "PATCH", "PUT", "DELETE"}
        or path == "/api/v1/research" and method == "POST"
        or path.startswith("/api/v1/research/") and method == "PATCH"
    )


def _is_vercel_fetch_submission(method, path):
    return method.upper() == "POST" and path == "/api/v1/fetches"


def _vercel_collection_snapshot_required(method, path):
    """Limit the large SQLite restore to routes that actually read or write it."""
    method = method.upper()
    if method in {"GET", "HEAD"}:
        return (
            path == "/api/v1/runtime"
            or path.startswith("/api/v1/analytics/")
            or path == "/api/v1/algorithms/results"
            or path == "/api/v1/research"
            or path.startswith("/api/v1/research/")
            or path == "/api/v1/collections"
            or path.startswith("/api/v1/collections/")
            or path == "/api/v1/datasets/latest"
            or path.startswith("/api/v1/datasets/latest/")
        )
    if method in {"POST", "PATCH", "PUT", "DELETE"}:
        return (
            path == "/api/v1/collections"
            or path.startswith("/api/v1/collections/")
            or path == "/api/v1/research"
            or path.startswith("/api/v1/research/")
            or _is_vercel_fetch_collections_recovery(path)
        )
    return False


def _vercel_collection_read_composition_required(method, path):
    """Use the read-only Collections context instead of building source workers."""
    return (method.upper() in {"GET", "HEAD"}
            and _vercel_collection_snapshot_required(method, path))


def _is_vercel_fetch_collections_recovery(path):
    parts = path.split("/")
    return (len(parts) == 6 and parts[:3] == ["", "api", "v1"]
            and parts[3] == "fetches" and re.fullmatch(r"[a-f0-9]{32}", parts[4])
            and parts[5] == "collections")


def _is_vercel_fetch_collection_action(method, path):
    return (method.upper() == "POST"
            and (_is_vercel_fetch_submission(method, path)
                 or _is_vercel_fetch_collections_recovery(path)))


def _safe_exception_class(error):
    value = type(error).__name__
    return value if _SAFE_EXCEPTION_CLASS.fullmatch(value) else "UnknownError"


def _elapsed_milliseconds(started_at, finished_at):
    return max(0, int((finished_at - started_at) * 1000))


def _vercel_timing_route(scope, application=None):
    route = getattr(scope.get("route"), "path", None)
    if isinstance(route, str) and route.startswith("/api/") and len(route) <= 128:
        return route

    pending = [scope.get("app"), application]
    seen = set()
    while pending:
        owner = pending.pop()
        if owner is None or id(owner) in seen:
            continue
        seen.add(id(owner))
        router = getattr(owner, "router", None)
        for route_owner in (router, owner):
            for candidate in getattr(route_owner, "routes", ()):
                route = getattr(candidate, "path", None)
                if (not isinstance(route, str) or not route.startswith("/api/")
                        or len(route) > 128):
                    continue
                request_path = scope.get("path")
                if "{" not in route and route == request_path:
                    return route
                try:
                    match, _child_scope = candidate.matches(scope)
                except Exception:
                    match = None
                if match is Match.FULL:
                    return route
                path_regex = getattr(candidate, "path_regex", None)
                if path_regex is not None and isinstance(request_path, str):
                    paths = [request_path]
                    root_path = scope.get("root_path")
                    if (isinstance(root_path, str) and root_path
                            and request_path.startswith(root_path)):
                        paths.append(request_path[len(root_path):] or "/")
                    if any(path_regex.fullmatch(path) for path in paths):
                        return route
        child_app = getattr(owner, "app", None)
        if child_app is not None:
            pending.append(child_app)
    request_path = scope.get("path")
    if request_path in _VERCEL_STATIC_API_TIMING_ROUTES:
        return request_path
    return "unmatched"


async def _save_vercel_collection_snapshot(distributed, *, failure_code):
    snapshot = getattr(distributed, "collection_snapshot", None)
    if snapshot is None:
        _logger.error(
            "Vercel collection snapshot commit failed "
            "failure_phase=COLLECTION_SNAPSHOT_COMMIT "
            "failure_reason=SNAPSHOT_UNAVAILABLE exception_class=SnapshotUnavailable"
        )
        return JSONResponse(
            {"detail": {"code": failure_code}}, status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    try:
        await asyncio.to_thread(snapshot.save)
    except Exception as error:
        from app.collections.firestore_snapshot import CollectionSnapshotConflict

        if isinstance(error, CollectionSnapshotConflict):
            return JSONResponse(
                {"detail": {"code": "COLLECTIONS_CONCURRENT_UPDATE"}},
                status_code=409, headers={"Cache-Control": "no-store"},
            )
        failure_reason = getattr(error, "failure_reason", None)
        if failure_reason not in _SAFE_SNAPSHOT_FAILURE_REASONS:
            failure_reason = "UNCLASSIFIED"
        cause_class = getattr(error, "cause_class", None)
        if not isinstance(cause_class, str) or not _SAFE_EXCEPTION_CLASS.fullmatch(cause_class):
            cause_class = "None"
        confirmation_exception_class = getattr(
            error, "confirmation_exception_class", None
        )
        if (not isinstance(confirmation_exception_class, str)
                or not _SAFE_EXCEPTION_CLASS.fullmatch(confirmation_exception_class)):
            confirmation_exception_class = "None"
        database_size_bytes = getattr(error, "database_size_bytes", None)
        if type(database_size_bytes) is not int or database_size_bytes < 0:
            database_size_bytes = "None"
        compressed_size_bytes = getattr(error, "compressed_size_bytes", None)
        if type(compressed_size_bytes) is not int or compressed_size_bytes < 0:
            compressed_size_bytes = "None"
        _logger.error(
            "Vercel collection snapshot commit failed "
            "failure_phase=COLLECTION_SNAPSHOT_COMMIT failure_reason=%s "
            "exception_class=%s cause_class=%s confirmation_exception_class=%s "
            "database_size_bytes=%s compressed_size_bytes=%s",
            failure_reason, _safe_exception_class(error), cause_class,
            confirmation_exception_class, database_size_bytes, compressed_size_bytes,
        )
        return JSONResponse(
            {"detail": {"code": failure_code}}, status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    return None


def _persists_vercel_collections(method, path):
    method = method.upper()
    return (
        (path == "/api/v1/collections" or path.startswith("/api/v1/collections/"))
        and method in {"POST", "PATCH", "PUT", "DELETE"}
    ) or (
        path == "/api/v1/research" and method == "POST"
        or path.startswith("/api/v1/research/") and method == "PATCH"
    )


class VercelSharedLiveMiddleware:
    """Keep Vercel Firestore and provider resources alive for the full ASGI request."""

    def __init__(self, app, *, settings, enabled, client_factory=None, context_factory=None):
        self.app = app
        self.settings = settings
        self.enabled = enabled
        self.client_factory = client_factory
        self.context_factory = context_factory

    async def __call__(self, scope, receive, send):
        if (scope.get("type") != "http"
                or not _is_vercel_shared_live_request(
                    self.settings,
                    scope.get("method", ""),
                    scope.get("path", ""),
                    enabled=self.enabled,
                )):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        if (_persists_vercel_collections(scope.get("method", ""), scope.get("path", ""))
                or _is_vercel_fetch_submission(
                    scope.get("method", ""), scope.get("path", "")
                )
                or _is_vercel_fetch_collections_recovery(scope.get("path", ""))):
            try:
                from app.api.fetches import validate_public_request_origin
                validate_public_request_origin(request, "vercel")
            except Exception:
                response = JSONResponse(
                    {"detail": {"code": "ORIGIN_NOT_ALLOWED"}},
                    status_code=403,
                    headers={"Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return

        oidc_token = request.headers.get("x-vercel-oidc-token")
        if not oidc_token:
            response = JSONResponse(
                {"detail": {"code": "FIRESTORE_IDENTITY_REQUIRED"}},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        request_started_at = perf_counter()
        client_ready_at = None
        composition_started_at = None
        composition_ready_at = None
        try:
            if self.client_factory is None:
                from app.storage.vercel_identity import firestore_client_for_request
                client = firestore_client_for_request(oidc_token, settings=self.settings)
            else:
                client = self.client_factory(oidc_token)
            client_ready_at = perf_counter()
        except Exception:
            response = JSONResponse(
                {"detail": {"code": "FIRESTORE_IDENTITY_REJECTED"}},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        context_manager = None
        context_entered = False
        try:
            context_factory = self.context_factory
            use_default_context = context_factory is None
            use_collection_read_context = False
            if context_factory is None:
                if _vercel_collection_read_composition_required(
                    scope.get("method", ""), scope.get("path", "")
                ):
                    from app.orchestration.vercel_collection_read_composition import (
                        vercel_collection_read_composition,
                    )
                    context_factory = vercel_collection_read_composition
                    use_collection_read_context = True
                else:
                    from app.orchestration.vercel_wikimedia_live_composition import (
                        vercel_shared_live_composition,
                    )
                    context_factory = vercel_shared_live_composition
            request_method = scope.get("method", "")
            request_path = scope.get("path", "")
            defer_collection_snapshot = (
                _is_vercel_fetch_submission(request_method, request_path)
                or not _vercel_collection_snapshot_required(request_method, request_path)
            )
            context_options = {"user_agent": self.settings.wikimedia_user_agent}
            if use_default_context:
                context_options["defer_collection_snapshot"] = defer_collection_snapshot
                if use_collection_read_context:
                    context_options["latest_dataset_summary_only"] = (
                        request_method.upper() == "GET"
                        and request_path == "/api/v1/datasets/latest"
                    )
                else:
                    context_options["capability_metadata_request"] = (
                        request_method.upper() == "GET"
                        and request_path == "/api/v1/capabilities"
                    )
            composition_started_at = perf_counter()
            context_manager = context_factory(client, **context_options)
            distributed = await context_manager.__aenter__()
            composition_ready_at = perf_counter()
            context_entered = True
        except Exception:
            _logger.error("Vercel shared LIVE composition failed")
            await _close_request_client(client)
            response = JSONResponse(
                {"detail": {"code": "SHARED_FETCH_UNAVAILABLE"}},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["distributed_public_fetch"] = (
            None if use_collection_read_context else distributed
        )
        state["collection_service"] = getattr(distributed, "collection_service", None)
        state["latest_dataset_summary_available"] = getattr(
            distributed, "latest_dataset_summary_available", False
        )
        state["latest_dataset_summary"] = getattr(
            distributed, "latest_dataset_summary", None
        )
        state["public_fetch_source_ids"] = tuple(sorted(
            getattr(distributed, "available_sources", {"wikimedia_pageviews_top_per_country"})
        ))

        async def send_no_store(message):
            if message.get("type") == "http.response.start":
                finished_at = perf_counter()
                route = _vercel_timing_route(scope, self.app)
                status_code = message.get("status")
                if type(status_code) is not int:
                    status_code = 0
                _logger.info(
                    "Vercel Shared LIVE request timing method=%s route=%s status_code=%d "
                    "client_setup_ms=%d composition_ms=%d handler_ms=%d total_ms=%d",
                    scope.get("method", "UNKNOWN"), route, status_code,
                    _elapsed_milliseconds(request_started_at, client_ready_at),
                    _elapsed_milliseconds(composition_started_at, composition_ready_at),
                    _elapsed_milliseconds(composition_ready_at, finished_at),
                    _elapsed_milliseconds(request_started_at, finished_at),
                )
                headers = [
                    (name, value) for name, value in message.get("headers", [])
                    if name.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        try:
            is_collection_write = _persists_vercel_collections(
                scope.get("method", ""), scope.get("path", "")
            )
            is_fetch_action = _is_vercel_fetch_collection_action(
                scope.get("method", ""), scope.get("path", "")
            )
            if is_fetch_action:
                # Keep replay/continuation invocations alive for their bounded
                # source slice and, on the terminal slice, Collections and
                # snapshot persistence. The first Fetch POST deliberately has
                # no BackgroundTask so its durable job ID can return promptly.
                messages = []

                async def capture_fetch_response(message):
                    messages.append(message)

                try:
                    await self.app(scope, receive, capture_fetch_response)
                except Exception as error:
                    _logger.error(
                        "Vercel Fetch execution failed failure_phase=REQUEST_EXECUTION exception_class=%s",
                        _safe_exception_class(error),
                    )
                    response = JSONResponse(
                        {"detail": {"code": "FETCH_EXECUTION_FAILED"}},
                        status_code=503,
                        headers={"Cache-Control": "no-store"},
                    )
                    await response(scope, receive, send)
                    return

                # Source checkpoints and shared provider caches commit to their
                # Firestore stores inside each slice. The larger Collections
                # SQLite snapshot is needed after terminal Fetch processing.
                needs_snapshot = not (
                    _is_vercel_fetch_submission(
                        scope.get("method", ""), scope.get("path", "")
                    )
                    and getattr(distributed, "firestore_shared_live", False)
                    and not getattr(request.state, "vercel_fetch_snapshot_required", False)
                )
                if needs_snapshot:
                    persistence_error = await _save_vercel_collection_snapshot(
                        distributed, failure_code="COLLECTIONS_PERSISTENCE_FAILED"
                    )
                    if persistence_error is not None:
                        await persistence_error(scope, receive, send)
                        return
                for message in messages:
                    await send_no_store(message)
                return

            if not is_collection_write:
                await self.app(scope, receive, send_no_store)
                return

            # A mutation must not report success until the request-local SQLite
            # working copy has been committed to the shared Firestore snapshot.
            messages = []

            async def capture(message):
                messages.append(message)

            await self.app(scope, receive, capture)
            persistence_error = await _save_vercel_collection_snapshot(
                distributed, failure_code="COLLECTIONS_PERSISTENCE_FAILED"
            )
            if persistence_error is not None:
                await persistence_error(scope, receive, send)
                return
            for message in messages:
                await send_no_store(message)
        finally:
            if context_entered:
                try:
                    await context_manager.__aexit__(None, None, None)
                except Exception:
                    _logger.error("Vercel shared LIVE cleanup failed")
            await _close_request_client(client)


def create_app(
    *,
    storage=None,
    adapter_factory=None,
    lock_path=None,
    operator_token=None,
    run_mode=None,
    read_only=None,
    read_storage_factory=None,
    settings: Settings | None = None,
    synthetic_supplement=None,
    distributed_public_fetch: DistributedPublicFetch | None = None,
    local_live_opt_in: bool = False,
    local_live_context_factory=None,
    scheduler_store=None,
    vercel_firestore_client_factory=None,
    vercel_live_context_factory=None,
):
    settings = settings or Settings.from_environment()
    run_mode = settings.run_mode if run_mode is None else run_mode
    read_only = (
        False if settings.environment_profile == "vercel"
        else settings.read_only if read_only is None else read_only
    )
    public_fetch_enabled, public_fetch_block_reason = _public_fetch_policy(
        settings,
        read_only=read_only,
        run_mode=run_mode,
        storage=storage,
        adapter_factory=adapter_factory,
        distributed_public_fetch=distributed_public_fetch,
    )
    if local_live_opt_in:
        if (type(local_live_opt_in) is not bool
                or not callable(local_live_context_factory)
                or settings.environment_profile != "local"
                or settings.run_mode != "live"
                or run_mode != "live"
                or read_only
                or settings.read_only
                or settings.google_cloud_project or settings.public_fetch_enabled
                or os.environ.get("VERCEL")):
            raise ValueError(
                "guarded local LIVE runtime requires explicit local-only configuration"
            )
        for variable in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
            raw = os.environ.get(variable)
            if raw is not None:
                try:
                    workers = int(raw)
                except ValueError:
                    raise ValueError("guarded local LIVE runtime requires one worker") from None
                if workers != 1:
                    raise ValueError("guarded local LIVE runtime requires one worker")
        public_fetch_enabled, public_fetch_block_reason = True, None
    elif local_live_context_factory is not None:
        raise ValueError("local live context requires explicit opt-in")
    if distributed_public_fetch is not None:
        if getattr(distributed_public_fetch, "normal_wikimedia_live", False):
            expected_sources = {WIKIMEDIA_SOURCE_ID}
        elif getattr(distributed_public_fetch, "normal_google_live", False):
            expected_sources = {WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID}
            if getattr(distributed_public_fetch, "normal_bing_live", False):
                expected_sources.add(BING_SOURCE_ID)
        elif getattr(distributed_public_fetch, "firestore_shared_live", False):
            expected_sources = set(getattr(
                distributed_public_fetch, "normal_google_live_sources", ()
            ))
        else:
            expected_sources = {
                WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID,
            }
        if (not isinstance(distributed_public_fetch, DistributedPublicFetch)
                or settings.environment_profile not in {"local", "test"}
                or read_only
                or (local_live_opt_in and (run_mode != "live"
                    or distributed_public_fetch.data_mode != "LIVE"
                    or not (getattr(distributed_public_fetch, "local_live", False)
                            or getattr(distributed_public_fetch, "normal_wikimedia_live", False)
                            or getattr(distributed_public_fetch, "normal_google_live", False)
                            or getattr(distributed_public_fetch, "normal_bing_live", False)
                            or getattr(distributed_public_fetch, "firestore_shared_live", False))))
                or (not local_live_opt_in and (run_mode != "synthetic"
                    or not settings.public_fetch_enabled
                    or distributed_public_fetch.data_mode != "SYNTHETIC"))
                or set(distributed_public_fetch.admission.policy.enabled_sources)
                != expected_sources):
            raise ValueError(
                "distributed Fetch injection is restricted to enabled local/test synthetic mode "
                "or an explicitly approved runtime composition"
            )
    if synthetic_supplement is not None and not (
        public_fetch_enabled and run_mode == "synthetic"
        and getattr(synthetic_supplement, "__trendscout_offline_only__", False)
    ):
        raise ValueError("Synthetic supplement requires an enabled offline fixture runtime")
    effective_lock_path = lock_path or settings.lock_path

    @asynccontextmanager
    async def lifespan(app):
        app.state.run_service = None
        app.state.operator_token = (
            operator_token if operator_token is not None else settings.operator_token
        )
        app.state.scheduler_store = scheduler_store
        runtime_guard = None
        try:
            if local_live_opt_in:
                async with local_live_context_factory() as local_live_fetch:
                    _validate_local_live_facade(local_live_fetch)
                    app.state.distributed_public_fetch = local_live_fetch
                    retention_task = None
                    youtube_retention_cleanup = getattr(
                        local_live_fetch, "youtube_retention_cleanup", None
                    )
                    if callable(youtube_retention_cleanup):
                        retention_task = asyncio.create_task(
                            _run_youtube_retention_sweeper(youtube_retention_cleanup),
                            name="trendscout-youtube-retention",
                        )
                    try:
                        from app.collections.service import CollectionService
                        from app.collections.store import CollectionStore
                        collection_store = CollectionStore.local()
                        app.state.collection_service = CollectionService(
                            collection_store,
                            entity_context_resolver=WikimediaEntityContextResolver(
                                collection_store,
                                before_request=getattr(local_live_fetch,
                                                       "entity_lookup_before_request", None),
                            ),
                        )
                    except Exception:
                        _logger.exception("Local Collections storage could not start; Wikimedia Fetch remains available")
                        app.state.collection_service = None
                    app.state.public_fetch_source_ids = tuple(sorted(
                        local_live_fetch.admission.policy.enabled_sources
                    ))
                    try:
                        yield
                    finally:
                        if retention_task is not None:
                            retention_task.cancel()
                            await asyncio.gather(retention_task, return_exceptions=True)
                        app.state.distributed_public_fetch = None
                        app.state.collection_service = None
                        app.state.public_fetch_enabled = False
                        app.state.public_fetch_block_reason = "LOCAL_RUNTIME_STOPPED"
                return
            # Vercel Functions are stateless between invocations. Shared LIVE
            # routes build their request-scoped services from Firestore; never
            # start the desktop SQLite RunService in a hosted function.
            if settings.environment_profile == "vercel":
                yield
                return
            if read_only:
                yield
                return
            if app.state.public_fetch_enabled and app.state.distributed_public_fetch is None:
                runtime_guard = SingleProcessGuard(
                    str(effective_lock_path) + ".public-fetch-runtime"
                )
                try:
                    acquired = runtime_guard.acquire()
                except OSError:
                    acquired = False
                if not acquired:
                    runtime_guard = None
                    app.state.public_fetch_enabled = False
                    app.state.public_fetch_block_reason = "MULTI_PROCESS_RUNTIME_UNSUPPORTED"
            app.state.run_service = build_run_service(
                storage=storage,
                adapter_factory=adapter_factory,
                lock_path=effective_lock_path,
                mode=run_mode,
                lock_factory=HostRunLock,
                service_factory=RunService,
                settings=settings,
                synthetic_supplement=synthetic_supplement,
            )
            if app.state.run_service is not None:
                await app.state.run_service.recover()
            app.state.scheduler_worker = None
            if settings.scheduler_enabled:
                if app.state.run_service is None or run_mode != "synthetic" or not app.state.public_fetch_enabled:
                    raise RuntimeError("Scheduler requires an enabled synthetic RunService")
                if app.state.scheduler_store is None:
                    from app.api.scheduler import _store
                    # Resolve storage through the same private local path used by the operator API.
                    app.state.scheduler_store = _store(SimpleNamespace(app=app))

                async def run_scheduled_fetch(_occurrence):
                    run_id = await app.state.run_service.start(
                        execution_timeout_seconds=settings.public_fetch_deadline_seconds,
                        run_id=_occurrence["occurrence_id"],
                    )
                    while True:
                        run = await app.state.run_service.storage.get_run(run_id)
                        if run and run.get("status") in {"COMPLETED", "PARTIAL", "FAILED", "ABORTED"}:
                            return run["status"]
                        await asyncio.sleep(0.5)

                app.state.scheduler_worker = ScheduleWorker(app.state.scheduler_store, run_scheduled_fetch)
                await app.state.scheduler_worker.start()
            yield
        finally:
            if getattr(app.state, "scheduler_worker", None):
                await app.state.scheduler_worker.stop()
            if app.state.run_service:
                await app.state.run_service.shutdown()
            if runtime_guard is not None:
                runtime_guard.release()

    application = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        # Keep interactive API/debug surfaces disabled on hosted Vercel.
        docs_url=("/docs" if not read_only and settings.environment_profile != "vercel" else None),
        redoc_url=("/redoc" if not read_only and settings.environment_profile != "vercel" else None),
        openapi_url=("/openapi.json" if not read_only and settings.environment_profile != "vercel" else None),
    )
    application.state.read_only = read_only
    application.state.settings = settings
    application.state.public_fetch_enabled = public_fetch_enabled
    application.state.public_fetch_block_reason = public_fetch_block_reason
    application.state.distributed_public_fetch = distributed_public_fetch
    application.state.collection_service = None
    application.state.scheduler_store = scheduler_store
    application.state.scheduler_worker = None
    application.state.fetch_registry = PublicFetchRegistry()
    application.state.public_fetch_source_ids = (
        tuple(sorted(distributed_public_fetch.admission.policy.enabled_sources))
        if distributed_public_fetch is not None
        else PUBLIC_SOURCE_IDS + (GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID)
        if synthetic_supplement is not None else PUBLIC_SOURCE_IDS
    )
    application.state.public_fetch_limit = MAX_FETCHES_PER_WINDOW
    application.state.public_fetch_window_seconds = FETCH_WINDOW_SECONDS
    application.state.public_fetch_poll_limit = MAX_STATUS_POLLS
    application.state.public_fetch_ttl_seconds = FETCH_TTL_SECONDS
    application.state.public_fetch_deadline_seconds = settings.public_fetch_deadline_seconds
    application.state.read_storage_factory = (
        read_storage_factory
        if read_storage_factory is not None
        else lambda oidc_token: read_only_storage(oidc_token, settings=settings)
    )
    application.state.vercel_firestore_client_factory = vercel_firestore_client_factory
    application.state.vercel_live_context_factory = vercel_live_context_factory

    @application.middleware("http")
    async def protect_read_only(request: Request, call_next):
        path = request.url.path
        vercel_fetch_post = (
            settings.environment_profile == "vercel"
            and public_fetch_enabled
            and path == "/api/v1/fetches"
            and request.method == "POST"
        )
        if (
            read_only
            and (path == "/api/v1/fetches" or path.startswith("/api/v1/fetches/"))
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not vercel_fetch_post
        ):
            return JSONResponse(
                {"detail": "Read-only deployment"},
                status_code=405,
                headers={"Cache-Control": "no-store"},
            )
        protected = path == "/api/runs" or path.startswith("/api/runs/")
        if not protected:
            return await call_next(request)
        if path.rstrip("/") == "/api/runs" and request.method == "POST":
            if read_only:
                return JSONResponse(
                    {"detail": "Read-only deployment"},
                    status_code=405,
                    headers={"Cache-Control": "no-store"},
                )
            return await call_next(request)
        if not read_only and settings.environment_profile != "vercel":
            return await call_next(request)
        password = settings.read_password or ""
        if len(password) < 32:
            return JSONResponse(
                {"detail": "Read access is not configured"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        try:
            scheme, encoded = request.headers.get("authorization", "").split(" ", 1)
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            username, supplied = decoded.split(":", 1)
            valid = (
                scheme.lower() == "basic"
                and hmac.compare_digest(username.encode(), b"viewer")
                and hmac.compare_digest(supplied.encode(), password.encode())
            )
        except (ValueError, UnicodeDecodeError, binascii.Error):
            valid = False
        if not valid:
            return JSONResponse(
                {"detail": "Authentication required"},
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Basic realm="TrendScout"',
                    "Cache-Control": "no-store",
                },
            )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/", include_in_schema=False)
    async def dashboard_root():
        return RedirectResponse(url="/dashboard/", status_code=307)

    @application.get("/api/config")
    async def app_config():
        return {"read_only": read_only}

    application.include_router(health_router, prefix="/api")
    application.include_router(runs_router, prefix="/api")
    application.include_router(capabilities_router, prefix="/api/v1")
    application.include_router(algorithms_router, prefix="/api/v1")
    application.include_router(analytics_router, prefix="/api/v1")
    application.include_router(research_router, prefix="/api/v1")
    application.include_router(scheduler_router, prefix="/api/v1")
    application.include_router(fetches_router, prefix="/api/v1")
    application.include_router(collections_router, prefix="/api/v1")
    application.include_router(datasets_router, prefix="/api/v1")
    if settings.environment_profile == "vercel":
        # Add last so this pure ASGI middleware wraps FastAPI's request middleware
        # and remains active through Starlette BackgroundTasks execution.
        application.add_middleware(
            VercelSharedLiveMiddleware,
            settings=settings,
            enabled=public_fetch_enabled and run_mode == "live" and not read_only,
            client_factory=vercel_firestore_client_factory,
            context_factory=vercel_live_context_factory,
        )
    frontend_source = Path(__file__).resolve().parents[2] / "Frontend" / "public"
    vercel_frontend = Path(__file__).resolve().parents[1] / "public" / "dashboard"
    frontend = (
        vercel_frontend if settings.environment_profile == "vercel" and vercel_frontend.is_dir()
        else frontend_source
    )
    if frontend.is_dir():
        application.mount(
            "/dashboard", StaticFiles(directory=frontend, html=True), name="dashboard"
        )

    @application.exception_handler(StorageError)
    async def storage_error(_request: Request, _exc: StorageError):
        return JSONResponse(status_code=503, content={"detail": "Storage operation failed"})

    return LocalLiveGuard(application) if local_live_opt_in else application


def create_local_live_app(*, enabled=False, workers=1, composition_factory=None, settings=None):
    """Create an explicitly opted-in, loopback-guarded local LIVE HTTP app.

    The caller supplies a context factory that yields the existing D6 local
    facade. Credentials and grants are never discovered by this factory.
    """
    if enabled is not True or type(workers) is not int or workers != 1 \
            or not callable(composition_factory):
        raise ValueError("explicit opt-in and a local live composition are required")
    settings = settings or Settings.from_environment()
    application = create_app(
        settings=settings,
        local_live_opt_in=True,
        local_live_context_factory=composition_factory,
    )
    return application


def run_local_live(application, *, port=8001):
    """Serve a guarded local app on loopback with one process and no proxy trust."""
    if type(application) is not LocalLiveGuard or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("guarded local app and valid port are required")
    import uvicorn

    uvicorn.run(application, host="127.0.0.1", port=port, workers=1, proxy_headers=False)


def create_normal_wikimedia_live_app(*, enabled=False, workers=1,
                                     composition_factory=None, settings=None):
    """Create a normal Wikimedia LIVE demo app with the local-only guard."""
    if (enabled is not True or type(workers) is not int or workers != 1
            or not callable(composition_factory)):
        raise ValueError("explicit opt-in and a normal Wikimedia composition are required")
    if settings is None:
        from app.sources.catalog import DEFAULT_WIKIMEDIA_USER_AGENT
        settings = Settings.from_environment({
            "TREND_SCOUT_ENVIRONMENT": "local", "TREND_SCOUT_RUN_MODE": "live",
            "TREND_SCOUT_READ_ONLY": "false", "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false",
            "WIKIMEDIA_USER_AGENT": DEFAULT_WIKIMEDIA_USER_AGENT,
        })
    return create_app(settings=settings, local_live_opt_in=True,
                      local_live_context_factory=composition_factory)


def create_normal_google_live_app(*, enabled=False, workers=1,
                                 composition_factory=None, settings=None):
    """Create the separately approved, local-only Wikimedia + Google LIVE app."""
    if (enabled is not True or type(workers) is not int or workers != 1
            or not callable(composition_factory)):
        raise ValueError("explicit opt-in and a normal Google composition are required")
    if settings is None:
        from app.sources.catalog import DEFAULT_WIKIMEDIA_USER_AGENT
        settings = Settings.from_environment({
            "TREND_SCOUT_ENVIRONMENT": "local", "TREND_SCOUT_RUN_MODE": "live",
            "TREND_SCOUT_READ_ONLY": "false", "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false",
            "WIKIMEDIA_USER_AGENT": DEFAULT_WIKIMEDIA_USER_AGENT,
        })
    return create_app(settings=settings, local_live_opt_in=True,
                      local_live_context_factory=composition_factory)


def run_normal_wikimedia_live(application, *, port=8001):
    """Serve the normal local Wikimedia Fetch app on loopback with one worker."""
    if (type(application) is not LocalLiveGuard or type(port) is not int
            or not 1 <= port <= 65535):
        raise ValueError("guarded normal LIVE app and valid port are required")
    import uvicorn

    uvicorn.run(application, host="127.0.0.1", port=port, workers=1, proxy_headers=False)


def run_normal_google_live(application, *, port=8001):
    """Serve the separately opted-in local Google runtime on loopback only."""
    if (type(application) is not LocalLiveGuard or type(port) is not int
            or not 1 <= port <= 65535):
        raise ValueError("guarded normal Google LIVE app and valid port are required")
    import uvicorn

    uvicorn.run(application, host="127.0.0.1", port=port, workers=1, proxy_headers=False)


def _validate_local_live_facade(facade):
    if (not isinstance(facade, DistributedPublicFetch)
            or facade.data_mode != "LIVE"
            or not (getattr(facade, "local_live", False)
                    or getattr(facade, "normal_wikimedia_live", False)
            or getattr(facade, "normal_google_live", False)
            or getattr(facade, "normal_bing_live", False)
            or getattr(facade, "firestore_shared_live", False))):
        raise ValueError("local composition did not provide a guarded LIVE facade")


def _public_fetch_policy(settings, *, read_only, run_mode, storage, adapter_factory,
                         distributed_public_fetch=None):
    if settings.environment_profile == "vercel":
        if settings.vercel_shared_live_enabled and not read_only and run_mode == "live":
            return True, None
        return False, "VERCEL_SHARED_LIVE_DISABLED"
    if not settings.public_fetch_enabled:
        return False, "FEATURE_DISABLED"
    if read_only:
        return False, "READ_ONLY_DEPLOYMENT"
    if settings.environment_profile not in {"local", "test"}:
        return False, "UNAPPROVED_ENVIRONMENT"
    if run_mode != "synthetic":
        return False, "LIVE_FETCH_NOT_APPROVED"
    if settings.google_cloud_project:
        return False, "CLOUD_STORAGE_NOT_ALLOWED"
    if distributed_public_fetch is not None:
        if (not isinstance(distributed_public_fetch, DistributedPublicFetch)
                or distributed_public_fetch.data_mode != "SYNTHETIC"
                or not distributed_public_fetch.admission.policy.enabled):
            return False, "DISTRIBUTED_SYNTHETIC_RUNTIME_REQUIRED"
        return True, None
    if storage is None or not getattr(storage, "__trendscout_offline_only__", False):
        return False, "OFFLINE_FAKE_STORAGE_REQUIRED"
    if adapter_factory is None or not getattr(
        adapter_factory, "__trendscout_offline_only__", False
    ):
        return False, "OFFLINE_FIXTURE_ADAPTER_REQUIRED"
    if not set(PUBLIC_SOURCE_IDS) <= settings.enabled_source_ids:
        return False, "SOURCE_NOT_ENABLED"
    for variable in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
        raw = os.environ.get(variable)
        if raw is not None:
            try:
                workers = int(raw)
            except ValueError:
                return False, "MULTI_PROCESS_RUNTIME_UNSUPPORTED"
            if workers != 1:
                return False, "MULTI_PROCESS_RUNTIME_UNSUPPORTED"
    return True, None


app = create_app()
