"""Application composition: select concrete dependencies at the outer boundary."""

from app.algorithms.catalog import registry as algorithm_registry
from app.algorithms.runner import AlgorithmRunner
from app.analysis.keywords import extract_keywords
from app.analysis.trend import DayData, score_trends
from app.processing.wikimedia_cleaning import clean_snapshot
from app.settings import Settings
from app.sources.registry import SourceRegistry
from app.sources.wikimedia.adapter import METADATA, WikimediaAdapter
from app.storage.firestore import FirestoreStorage


def source_registry(settings: Settings | None = None):
    """Register enabled, implemented factories without constructing adapters early."""
    settings = settings or Settings.from_environment()
    registry = SourceRegistry()
    if METADATA.source_id in settings.enabled_source_ids:
        registry.register(
            METADATA,
            lambda: WikimediaAdapter(settings=settings),
        )
    return registry


def algorithm_engine():
    return AlgorithmRunner(algorithm_registry(scorer=score_trends))


def build_run_service(
    *,
    storage,
    adapter_factory,
    lock_path,
    mode,
    lock_factory,
    service_factory,
    settings: Settings | None = None,
    synthetic_supplement=None,
):
    """Build the local service; callers must never invoke this in read-only mode."""
    settings = settings or Settings.from_environment()
    if storage is None and settings.google_cloud_project:
        storage = FirestoreStorage.from_project(settings.google_cloud_project)
    if storage is None:
        return None
    path = lock_path or settings.lock_path
    lock = lock_factory(path, cooldown_seconds=settings.run_cooldown_seconds)
    extra = {"synthetic_supplement": synthetic_supplement} if synthetic_supplement else {}
    return service_factory(
        storage,
        lock,
        adapter_factory=adapter_factory or source_registry(settings).factory(METADATA.source_id),
        mode=mode,
        clean_snapshot=clean_snapshot,
        extract_keywords=extract_keywords,
        score_trends=algorithm_engine().legacy_results,
        day_data_factory=DayData,
        **extra,
    )


def read_only_storage(oidc_token, *, settings: Settings | None = None):
    """Construct a per-request WIF client without a local ADC fallback."""
    from app.storage.vercel_identity import storage_for_request

    settings = settings or Settings.from_environment()
    return storage_for_request(
        oidc_token,
        settings=settings,
    )
