"""Application entry point."""

import base64
import binascii
import hmac
import logging
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.algorithms import router as algorithms_router
from app.api.capabilities import router as capabilities_router
from app.api.collections import router as collections_router
from app.api.datasets import router as datasets_router
from app.api.fetches import router as fetches_router
from app.api.health import router as health_router
from app.api.runs import router as runs_router
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
from app.settings import Settings
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)

_logger = logging.getLogger(__name__)


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
):
    settings = settings or Settings.from_environment()
    run_mode = settings.run_mode if run_mode is None else run_mode
    read_only = (
        settings.read_only
        if read_only is None
        else read_only or settings.environment_profile == "vercel"
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
        expected_sources = ({WIKIMEDIA_SOURCE_ID}
            if getattr(distributed_public_fetch, "normal_wikimedia_live", False) else {
                WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID,
            })
        if (not isinstance(distributed_public_fetch, DistributedPublicFetch)
                or settings.environment_profile not in {"local", "test"}
                or read_only
                or (local_live_opt_in and (run_mode != "live"
                    or distributed_public_fetch.data_mode != "LIVE"
                    or not (getattr(distributed_public_fetch, "local_live", False)
                            or getattr(distributed_public_fetch, "normal_wikimedia_live", False))))
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
        runtime_guard = None
        try:
            if local_live_opt_in:
                async with local_live_context_factory() as local_live_fetch:
                    _validate_local_live_facade(local_live_fetch)
                    app.state.distributed_public_fetch = local_live_fetch
                    try:
                        from app.collections.service import CollectionService
                        from app.collections.store import CollectionStore
                        collection_store = CollectionStore.local()
                        app.state.collection_service = CollectionService(
                            collection_store,
                            entity_context_resolver=WikimediaEntityContextResolver(collection_store),
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
                        app.state.distributed_public_fetch = None
                        app.state.collection_service = None
                        app.state.public_fetch_enabled = False
                        app.state.public_fetch_block_reason = "LOCAL_RUNTIME_STOPPED"
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
            app.state.operator_token = (
                operator_token if operator_token is not None else settings.operator_token
            )
            yield
        finally:
            if app.state.run_service:
                await app.state.run_service.shutdown()
            if runtime_guard is not None:
                runtime_guard.release()

    application = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        docs_url=None if read_only else "/docs",
        redoc_url=None if read_only else "/redoc",
        openapi_url=None if read_only else "/openapi.json",
    )
    application.state.read_only = read_only
    application.state.settings = settings
    application.state.public_fetch_enabled = public_fetch_enabled
    application.state.public_fetch_block_reason = public_fetch_block_reason
    application.state.distributed_public_fetch = distributed_public_fetch
    application.state.collection_service = None
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

    @application.middleware("http")
    async def protect_read_only(request: Request, call_next):
        path = request.url.path
        if (
            read_only
            and (path == "/api/v1/fetches" or path.startswith("/api/v1/fetches/"))
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        ):
            return JSONResponse(
                {"detail": "Read-only deployment"},
                status_code=405,
                headers={"Cache-Control": "no-store"},
            )
        protected = path == "/api/runs" or path.startswith("/api/runs/")
        if not read_only or not protected:
            return await call_next(request)
        if path.rstrip("/") == "/api/runs" and request.method == "POST":
            return JSONResponse(
                {"detail": "Read-only deployment"},
                status_code=405,
                headers={"Cache-Control": "no-store"},
            )
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
    application.include_router(fetches_router, prefix="/api/v1")
    application.include_router(collections_router, prefix="/api/v1")
    application.include_router(datasets_router, prefix="/api/v1")
    frontend = Path(__file__).resolve().parents[2] / "Frontend" / "public"
    if read_only:

        @application.get("/dashboard/")
        @application.get("/dashboard/{asset:path}")
        async def dashboard(asset: str = "index.html"):
            asset = asset or "index.html"
            requested = PurePosixPath(asset)
            if requested.is_absolute() or any(part in ("", ".", "..") for part in requested.parts):
                return Response(status_code=404)
            try:
                from app.dashboard_assets import ASSETS

                content = ASSETS[requested.as_posix()]
            except ImportError:
                if settings.environment_profile == "vercel":
                    return Response(status_code=503)
                candidate = (frontend / requested).resolve()
                if frontend.resolve() not in candidate.parents or not candidate.is_file():
                    return Response(status_code=404)
                content = candidate.read_bytes()
            except KeyError:
                return Response(status_code=404)
            media_type = mimetypes.guess_type(requested.name)[0] or "application/octet-stream"
            return Response(content=content, media_type=media_type)
    elif frontend.is_dir():
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


def run_normal_wikimedia_live(application, *, port=8001):
    """Serve the normal local Wikimedia Fetch app on loopback with one worker."""
    if (type(application) is not LocalLiveGuard or type(port) is not int
            or not 1 <= port <= 65535):
        raise ValueError("guarded normal LIVE app and valid port are required")
    import uvicorn

    uvicorn.run(application, host="127.0.0.1", port=port, workers=1, proxy_headers=False)


def _validate_local_live_facade(facade):
    if (not isinstance(facade, DistributedPublicFetch)
            or facade.data_mode != "LIVE"
            or not (getattr(facade, "local_live", False)
                    or getattr(facade, "normal_wikimedia_live", False))):
        raise ValueError("local composition did not provide a guarded LIVE facade")


def _public_fetch_policy(settings, *, read_only, run_mode, storage, adapter_factory,
                         distributed_public_fetch=None):
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
