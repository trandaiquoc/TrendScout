"""Application entry point."""

from fastapi import FastAPI

from app.api.health import router as health_router
from app.settings import Settings

settings = Settings.from_environment()
app = FastAPI(title=settings.app_name)
app.include_router(health_router, prefix="/api")
