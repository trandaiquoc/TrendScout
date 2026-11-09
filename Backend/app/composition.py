"""Application composition: select concrete dependencies at the outer boundary."""

import os
from pathlib import Path

from app.adapters.wikimedia import WikimediaAdapter
from app.analysis.keywords import extract_keywords
from app.analysis.trend import DayData, score_trends
from app.processing.wikimedia_cleaning import clean_snapshot
from app.storage.firestore import FirestoreStorage


def build_run_service(*, storage, adapter_factory, lock_path, mode, lock_factory, service_factory):
    """Build the local service; callers must never invoke this in read-only mode."""
    if storage is None and os.environ.get("GOOGLE_CLOUD_PROJECT"):
        storage = FirestoreStorage.from_environment()
    if storage is None:
        return None
    path = lock_path or os.environ.get(
        "TREND_SCOUT_LOCK_PATH", str(Path.home() / ".cache/trendscout/manual-run.lock")
    )
    lock = lock_factory(path, cooldown_seconds=int(os.environ.get("TREND_SCOUT_RUN_COOLDOWN_SECONDS", "60")))
    return service_factory(
        storage, lock, adapter_factory=adapter_factory or WikimediaAdapter, mode=mode,
        clean_snapshot=clean_snapshot, extract_keywords=extract_keywords,
        score_trends=score_trends, day_data_factory=DayData,
    )


def read_only_storage(oidc_token):
    """Construct a per-request WIF client without a local ADC fallback."""
    from app.storage.vercel_identity import storage_for_request
    return storage_for_request(oidc_token)
