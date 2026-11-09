"""Application entry point."""

from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.health import router as health_router
from app.api.runs import router as runs_router
from app.orchestration.runs import HostRunLock, RunService
from app.settings import Settings
from app.storage.base import StorageError
from app.storage.firestore import FirestoreStorage


def create_app(*, storage=None, adapter_factory=None, lock_path=None, operator_token=None, run_mode="live"):
    @asynccontextmanager
    async def lifespan(app):
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

    application = FastAPI(title=Settings.from_environment().app_name, lifespan=lifespan)
    application.include_router(health_router, prefix="/api")
    application.include_router(runs_router, prefix="/api")
    frontend = Path(__file__).resolve().parents[2] / "Frontend" / "public"
    if frontend.is_dir():
        application.mount("/dashboard", StaticFiles(directory=frontend, html=True), name="dashboard")

    @application.exception_handler(StorageError)
    async def storage_error(_request: Request, _exc: StorageError):
        return JSONResponse(status_code=503, content={"detail": "Storage operation failed"})

    return application


app = create_app()
