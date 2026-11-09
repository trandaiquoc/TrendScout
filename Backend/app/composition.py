"""Application composition: select concrete dependencies at the outer boundary."""

import os
from pathlib import Path

from app.sources.wikimedia.adapter import METADATA, WikimediaAdapter
from app.sources.registry import SourceRegistry
from app.analysis.keywords import extract_keywords
from app.analysis.trend import DayData, score_trends
from app.algorithms.registry import AlgorithmRegistry
from app.algorithms.runner import AlgorithmRunner
from app.algorithms.strategies import WikimediaLegacy, GrowthRatio, MedianBaseline
from app.processing.wikimedia_cleaning import clean_snapshot
from app.storage.firestore import FirestoreStorage


def source_registry():
    """Register factories only; neither creation nor import fetches a source."""
    registry = SourceRegistry()
    registry.register(METADATA, lambda: WikimediaAdapter())
    return registry


def algorithm_engine():
    registry = AlgorithmRegistry()
    for strategy in (WikimediaLegacy(score_trends), GrowthRatio(), MedianBaseline()):
        registry.register(strategy)
    return AlgorithmRunner(registry)


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
        storage, lock, adapter_factory=adapter_factory or source_registry().factory(METADATA.source_id), mode=mode,
        clean_snapshot=clean_snapshot, extract_keywords=extract_keywords,
        score_trends=algorithm_engine().legacy_results, day_data_factory=DayData,
    )


def read_only_storage(oidc_token):
    """Construct a per-request WIF client without a local ADC fallback."""
    from app.storage.vercel_identity import storage_for_request
    return storage_for_request(oidc_token)
