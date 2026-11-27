"""Public machine-readable registry view; frontend must not own algorithm lists."""
from dataclasses import asdict, dataclass, replace

from app.algorithms.popularity import (
    ObservedPercentileRank,
    ObservedVolumeRanking,
    RelativeSourceRank,
)
from app.algorithms.registry import AlgorithmRegistry
from app.algorithms.series import KendallTrend, RelativeChange, RollingMeanBaseline, TheilSenSlope
from app.algorithms.statistical import EWMA, RobustZMad
from app.algorithms.strategies import GrowthRatio, MedianBaseline, WikimediaLegacy
from app.analysis.trend import score_trends
from app.sources.catalog import SOURCE_CAPABILITIES


@dataclass(frozen=True)
class PersistedInputAdapter:
    """Describe a source projection the collection runner can persist and reuse.

    Source adapters translate provider records into these normalized capabilities;
    the algorithm selection itself stays metadata-driven and source-agnostic.
    """

    adapter_id: str
    source_id: str
    metric: str
    unit: str
    source_capabilities: tuple[str, ...]
    runtime_capabilities: tuple[str, ...]
    max_history_days: int
    minimum_cohort_size: int = 0

    def supports(self, metadata):
        if self.source_id in metadata.excluded_sources:
            return False
        if metadata.source not in {"*", self.source_id}:
            return False
        if metadata.input_metric not in {None, "*", self.metric}:
            return False
        if metadata.input_unit not in {None, "*", self.unit}:
            return False
        if not set(metadata.supported_capabilities).issubset(self.source_capabilities):
            return False
        if not set(metadata.runtime_requirements).issubset(self.runtime_capabilities):
            return False
        if ("top_list_censored" in self.source_capabilities
                and not metadata.accepts_censored_top_list):
            return False
        if metadata.family == "popularity":
            return metadata.minimum_samples <= self.minimum_cohort_size
        minimum_window = min(metadata.supported_windows or (metadata.minimum_samples,))
        required_history = max(metadata.minimum_samples, minimum_window)
        return required_history <= self.max_history_days


# This registry lists materialized PostgreSQL input projections, not all provider
# capabilities. A future source is eligible only after its adapter persists the
# same scope, bounded history/cohort, digest, and lineage contracts.
PERSISTED_INPUT_ADAPTERS = (
    PersistedInputAdapter(
        adapter_id="wikimedia_pageviews_observation_projection",
        source_id="wikimedia_pageviews_top_per_country",
        metric="views_ceil",
        unit="approx_pageviews_upper_bound",
        source_capabilities=(
            "daily_snapshot", "seven_day_history", "top_list_censored",
        ),
        runtime_capabilities=(
            "daily_observations", "observation_history", "snapshot_cohort",
            "input_digest", "evidence_lineage",
        ),
        # The Fetch retention contract keeps the current snapshot and six prior days.
        max_history_days=6,
        minimum_cohort_size=2,
    ),
)


def _execution_for(metadata):
    adapters = [adapter for adapter in PERSISTED_INPUT_ADAPTERS
                if adapter.supports(metadata)]
    if adapters:
        return {
            "status": "PERSISTED_SOURCE_ADAPTER",
            "sources": tuple(adapter.source_id for adapter in adapters),
            "input_adapters": tuple(adapter.adapter_id for adapter in adapters),
            "reason": "Selected by the registered persisted input contract when compatible observations are available.",
        }
    if "legacy_result" in metadata.runtime_requirements:
        return {
            "status": "LEGACY_PATH_ONLY",
            "sources": (metadata.source,),
            "input_adapters": (),
            "reason": "The versioned legacy score is produced by the Wikimedia dataset pipeline and is not persisted by the collection algorithm runner.",
        }
    if "query_cohort" in metadata.runtime_requirements:
        return {
            "status": "REGISTERED_NO_QUERY_COHORT",
            "sources": (),
            "input_adapters": (),
            "reason": "No persisted same-provider, same-query, same-snapshot rank cohort is available; YouTube-derived ranks remain policy restricted.",
        }
    if "daily_numeric_series" in metadata.supported_capabilities:
        available_history = max(
            (adapter.max_history_days for adapter in PERSISTED_INPUT_ADAPTERS),
            default=0,
        )
        minimum_window = min(metadata.supported_windows or (metadata.minimum_samples,))
        required_history = max(metadata.minimum_samples, minimum_window)
        if required_history > available_history:
            return {
                "status": "NOT_EXECUTED_SOURCE_AND_RETENTION",
                "sources": (),
                "input_adapters": (),
                "reason": (
                    "No current source provides an uncensored daily numeric series; "
                    f"the minimum history is {required_history} prior days, while the "
                    f"persisted input projection retains {available_history}."
                ),
            }
        return {
            "status": "REGISTERED_NO_COMPATIBLE_SOURCE",
            "sources": (),
            "input_adapters": (),
            "reason": "No persisted source supplies the complete, uncensored daily numeric series required by this method.",
        }
    return {
        "status": "REGISTERED_NO_PERSISTED_INPUT_ADAPTER",
        "sources": (),
        "input_adapters": (),
        "reason": "The strategy is registered but no persisted source input adapter satisfies its metric, scope, and runtime requirements.",
    }


def source_compatibility(metadata, source):
    """Return a source capability status and reason for a registered method."""
    if source.source_id in metadata.excluded_sources:
        return "INCOMPATIBLE", "POLICY_RESTRICTED"
    if metadata.source != "*" and source.source_id != metadata.source:
        return "INCOMPATIBLE", "SOURCE_NOT_SUPPORTED"
    if ((metadata.input_metric and metadata.input_metric not in {"*", source.metric_name})
            or (metadata.input_unit and metadata.input_unit not in {"*", source.metric_unit})):
        return "INCOMPATIBLE", "METRIC_OR_UNIT_NOT_SUPPORTED"
    if metadata.supported_capabilities and not set(metadata.supported_capabilities).issubset(source.capabilities):
        return "INCOMPATIBLE", "REQUIRED_CAPABILITY_UNAVAILABLE"
    if "top_list_censored" in source.capabilities and not metadata.accepts_censored_top_list:
        return "INCOMPATIBLE", "CENSORED_SOURCE_NOT_ACCEPTED"
    if source.readiness == "IMPLEMENTED" and source.adapter_status == "IMPLEMENTED":
        return "SUPPORTED_AND_AVAILABLE", None
    return "SUPPORTED_BUT_NOT_LIVE", source.readiness


def persisted_keys_for_source(algorithm_registry, source_id, *, input_adapters=None):
    """Return strategies whose metadata fits a persisted source input contract."""
    adapters = PERSISTED_INPUT_ADAPTERS if input_adapters is None else tuple(input_adapters)
    source_adapters = [adapter for adapter in adapters if adapter.source_id == source_id]
    return tuple(
        key for key in algorithm_registry.keys()
        if any(adapter.supports(algorithm_registry.get(key).metadata)
               for adapter in source_adapters)
    )


def registry(scorer=score_trends):
    result = AlgorithmRegistry()
    legacy = WikimediaLegacy(scorer)
    legacy.metadata = replace(legacy.metadata, runtime_requirements=("legacy_result",))
    for method in (legacy, GrowthRatio(), MedianBaseline(),
                   RobustZMad(), EWMA(), RollingMeanBaseline(), RelativeChange(),
                   TheilSenSlope(), KendallTrend(), ObservedVolumeRanking(),
                   ObservedPercentileRank(), RelativeSourceRank()):
        result.register(method)
    return result


def catalog(source_capabilities=SOURCE_CAPABILITIES):
    sources = {source.source_id: source for source in source_capabilities}
    methods = []
    registered = registry()
    for key in registered.keys():
        meta = registered.get(key).metadata
        item = asdict(meta)
        item['key'] = meta.key
        item['description'] = meta.description or meta.name
        item['supported_windows'] = item['supported_windows'] or tuple(range(1, meta.max_window_days + 1))
        item['input_metric'] = item['input_metric'] or meta.metric
        item['input_unit'] = item['input_unit'] or meta.unit
        item['execution'] = _execution_for(meta)
        compatible = []
        for source_id, source in sources.items():
            status, reason = source_compatibility(meta, source)
            compatible.append({'source_id': source_id, 'provider_id': source.provider_id,
                'display_name': source.display_name, 'icon': source.icon,
                'status': status, 'reason': reason, 'metric': source.metric_name,
                'unit': source.metric_unit, 'capabilities': source.capabilities,
                'adapter_status': source.adapter_status,
                'runtime_availability': 'LIVE_CAPABLE' if 'LIVE' in source.supported_data_modes else 'OFFLINE_ONLY',
                'local_live_verification_status': source.local_live_verification_status,
                'production_readiness': source.production_readiness})
        item['sources'] = compatible
        methods.append(item)
    return {'version': 'algorithm-registry-v1', 'evaluation_status': 'NOT_YET_EVALUATED',
        'evaluation_reason': 'No compatible labeled historical backtest is currently available.',
        'algorithms': methods}
