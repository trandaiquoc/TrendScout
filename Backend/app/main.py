"""Application entry point."""

import base64
import binascii
import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.health import router as health_router
from app.api.runs import router as runs_router
from app.composition import build_run_service, read_only_storage
from app.orchestration.runs import HostRunLock, RunService
from app.ports.storage import StorageError
from app.settings import Settings


def create_app(
    *, storage=None, adapter_factory=None, lock_path=None, operator_token=None,
    run_mode=None, read_only=None, read_storage_factory=None, settings: Settings | None = None,
):
    settings = settings or Settings.from_environment()
    run_mode = settings.run_mode if run_mode is None else run_mode
    read_only = (
        settings.read_only
        if read_only is None
        else read_only or settings.environment_profile == "vercel"
    )

    @asynccontextmanager
    async def lifespan(app):
        app.state.run_service = None
        if read_only:
            yield
            return
        app.state.run_service = build_run_service(
            storage=storage,
            adapter_factory=adapter_factory,
            lock_path=lock_path,
            mode=run_mode,
            lock_factory=HostRunLock,
            service_factory=RunService,
            settings=settings,
        )
        if app.state.run_service is not None:
            await app.state.run_service.recover()
        app.state.operator_token = operator_token if operator_token is not None else settings.operator_token
        try:
            yield
        finally:
            if app.state.run_service:
                await app.state.run_service.shutdown()

    application = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        docs_url=None if read_only else "/docs",
        redoc_url=None if read_only else "/redoc",
        openapi_url=None if read_only else "/openapi.json",
    )
    application.state.read_only = read_only
    application.state.settings = settings
    application.state.read_storage_factory = (
        read_storage_factory if read_storage_factory is not None
        else lambda oidc_token: read_only_storage(oidc_token, settings=settings)
    )

    @application.middleware("http")
    async def protect_read_only(request: Request, call_next):
        path = request.url.path
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
    frontend = Path(__file__).resolve().parents[2] / "Frontend" / "public"
    if read_only:
        @application.get("/dashboard/")
        @application.get("/dashboard/{asset}")
        async def dashboard(asset: str = "index.html"):
            types = {
                "index.html": "text/html",
                "app.js": "text/javascript",
                "styles.css": "text/css",
            }
            if asset not in types:
                return Response(status_code=404)
            try:
                from app.dashboard_assets import ASSETS
                content = ASSETS[asset]
            except ImportError:
                if settings.environment_profile == "vercel":
                    return Response(status_code=503)
                content = (frontend / asset).read_bytes()
            return Response(content=content, media_type=types[asset])
    elif frontend.is_dir():
        application.mount("/dashboard", StaticFiles(directory=frontend, html=True), name="dashboard")

    @application.exception_handler(StorageError)
    async def storage_error(_request: Request, _exc: StorageError):
        return JSONResponse(status_code=503, content={"detail": "Storage operation failed"})

    return application


app = create_app()
