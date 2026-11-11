"""Application entry point."""

import base64
import binascii
import hmac
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.capabilities import router as capabilities_router
from app.api.fetches import router as fetches_router
from app.api.health import router as health_router
from app.api.runs import router as runs_router
from app.composition import build_run_service, read_only_storage
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
    )
    effective_lock_path = lock_path or settings.lock_path

    @asynccontextmanager
    async def lifespan(app):
        app.state.run_service = None
        runtime_guard = None
        try:
            if read_only:
                yield
                return
            if app.state.public_fetch_enabled:
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
    application.state.fetch_registry = PublicFetchRegistry()
    application.state.public_fetch_source_ids = PUBLIC_SOURCE_IDS
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
        protected = (
            path == "/dashboard"
            or path.startswith("/dashboard/")
            or path == "/api/runs"
            or path.startswith("/api/runs/")
        )
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

    @application.get("/api/config")
    async def app_config():
        return {"read_only": read_only}

    application.include_router(health_router, prefix="/api")
    application.include_router(runs_router, prefix="/api")
    application.include_router(capabilities_router, prefix="/api/v1")
    application.include_router(fetches_router, prefix="/api/v1")
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

    return application


def _public_fetch_policy(settings, *, read_only, run_mode, storage, adapter_factory):
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
