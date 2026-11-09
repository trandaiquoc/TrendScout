"""Application entry point."""

from contextlib import asynccontextmanager
import os
import base64
import binascii
import hmac
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.health import router as health_router
from app.api.runs import router as runs_router
from app.orchestration.runs import HostRunLock, RunService
from app.settings import Settings
from app.storage.base import StorageError
from app.storage.firestore import FirestoreStorage


def create_app(*, storage=None, adapter_factory=None, lock_path=None, operator_token=None, run_mode="live", read_only=None, read_storage_factory=None):
    read_only = os.environ.get("VERCEL") == "1" or (read_only if read_only is not None else os.environ.get("TREND_SCOUT_READ_ONLY") == "1")
    @asynccontextmanager
    async def lifespan(app):
        app.state.run_service = None
        if read_only:
            yield
            return
        chosen_storage = storage
        if chosen_storage is None and os.environ.get("GOOGLE_CLOUD_PROJECT"):
            chosen_storage = FirestoreStorage.from_environment()
        app.state.run_service = None
        if chosen_storage is not None:
            path = lock_path or os.environ.get("TREND_SCOUT_LOCK_PATH", str(Path.home() / ".cache/trendscout/manual-run.lock"))
            lock = HostRunLock(path, cooldown_seconds=int(os.environ.get("TREND_SCOUT_RUN_COOLDOWN_SECONDS", "60")))
            app.state.run_service = RunService(chosen_storage, lock, mode=run_mode, **({"adapter_factory": adapter_factory} if adapter_factory else {}))
            await app.state.run_service.recover()
        app.state.operator_token = operator_token if operator_token is not None else os.environ.get("TREND_SCOUT_OPERATOR_TOKEN")
        try:
            yield
        finally:
            if app.state.run_service:
                await app.state.run_service.shutdown()

    application = FastAPI(title=Settings.from_environment().app_name, lifespan=lifespan,
                          docs_url=None if read_only else "/docs", redoc_url=None if read_only else "/redoc",
                          openapi_url=None if read_only else "/openapi.json")
    application.state.read_only = read_only
    application.state.read_storage_factory = read_storage_factory

    @application.middleware("http")
    async def protect_read_only(request: Request, call_next):
        path = request.url.path
        protected = path == "/dashboard" or path.startswith("/dashboard/") or path == "/api/runs" or path.startswith("/api/runs/")
        if not read_only or not protected:
            return await call_next(request)
        if path.rstrip("/") == "/api/runs" and request.method == "POST":
            return JSONResponse({"detail": "Read-only deployment"}, status_code=405, headers={"Cache-Control": "no-store"})
        password = os.environ.get("TREND_SCOUT_READ_PASSWORD", "")
        if len(password) < 32:
            return JSONResponse({"detail": "Read access is not configured"}, status_code=503, headers={"Cache-Control": "no-store"})
        try:
            scheme, encoded = request.headers.get("authorization", "").split(" ", 1)
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            username, supplied = decoded.split(":", 1)
            valid = scheme.lower() == "basic" and hmac.compare_digest(username.encode(), b"viewer") and hmac.compare_digest(supplied.encode(), password.encode())
        except (ValueError, UnicodeDecodeError, binascii.Error):
            valid = False
        if not valid:
            return JSONResponse({"detail": "Authentication required"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="TrendScout"', "Cache-Control": "no-store"})
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
            types = {"index.html": "text/html", "app.js": "text/javascript", "styles.css": "text/css"}
            if asset not in types:
                return Response(status_code=404)
            try:
                from app.dashboard_assets import ASSETS
                content = ASSETS[asset]
            except ImportError:
                if os.environ.get("VERCEL") == "1":
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
