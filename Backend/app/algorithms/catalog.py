"""Public machine-readable registry view; frontend must not own algorithm lists."""
from dataclasses import asdict

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


_RUNTIME_EXECUTION = {
    "wikimedia-trend": {
        "status": "LEGACY_PATH_ONLY",
        "sources": ("wikimedia_pageviews_top_per_country",),
        "reason": "The versioned legacy score is produced by the Wikimedia dataset pipeline; it is not persisted by the collection algorithm runner.",
    },
    "growth-ratio": {
        "status": "PERSISTED_WIKIMEDIA",
        "sources": ("wikimedia_pageviews_top_per_country",),
        "reason": "Runs only when a compatible Wikimedia baseline and current snapshot are available.",
    },
    "median-baseline": {
        "status": "PERSISTED_WIKIMEDIA",
        "sources": ("wikimedia_pageviews_top_per_country",),
        "reason": "Stores a conditional median of observed Wikimedia top-list days.",
    },
    "observed-volume-rank": {
        "status": "PERSISTED_WIKIMEDIA",
        "sources": ("wikimedia_pageviews_top_per_country",),
        "reason": "Ranks only within the current observed Wikimedia snapshot cohort.",
    },
    "observed-percentile-rank": {
        "status": "PERSISTED_WIKIMEDIA",
        "sources": ("wikimedia_pageviews_top_per_country",),
        "reason": "Computes a percentile only within the current observed Wikimedia snapshot cohort.",
    },
    "robust-z-mad": {
        "status": "NOT_EXECUTED_SOURCE_AND_RETENTION",
        "sources": (),
        "reason": "Current numeric source is top-list censored; the method rejects censored data and needs at least 14 historical days, beyond the stated seven-day retention window.",
    },
    "ewma": {
        "status": "NOT_EXECUTED_SOURCE_AND_RETENTION",
        "sources": (),
        "reason": "Current numeric source is top-list censored; the method requires complete daily history and rejects censored data.",
    },
    "rolling-mean-baseline": {
        "status": "REGISTERED_NO_COMPATIBLE_SOURCE",
        "sources": (),
        "reason": "No current source provides a complete, uncensored daily numeric series.",
    },
    "relative-change": {
        "status": "REGISTERED_NO_COMPATIBLE_SOURCE",
        "sources": (),
        "reason": "No current source provides a complete, uncensored daily numeric series.",
    },
    "theil-sen-slope": {
        "status": "REGISTERED_NO_COMPATIBLE_SOURCE",
        "sources": (),
        "reason": "No current source provides a complete, uncensored daily numeric series.",
    },
    "kendall-trend": {
        "status": "REGISTERED_NO_COMPATIBLE_SOURCE",
        "sources": (),
        "reason": "No current source provides a complete, uncensored daily numeric series.",
    },
    "relative-source-rank": {
        "status": "REGISTERED_NO_QUERY_COHORT",
        "sources": (),
        "reason": "Verification result ranks are not persisted as a comparable candidate cohort; YouTube-derived ranks remain policy-blocked.",
    },
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


def persisted_keys_for_source(algorithm_registry, source_id):
    """Keys that have a configured persisted execution path for this source."""
    return tuple(
        key for key in algorithm_registry.keys()
        if _RUNTIME_EXECUTION.get(algorithm_registry.get(key).metadata.algorithm_id, {}).get("status")
        == "PERSISTED_WIKIMEDIA"
        and source_id in _RUNTIME_EXECUTION.get(
            algorithm_registry.get(key).metadata.algorithm_id, {}
        ).get("sources", ())
    )


def registry(scorer=score_trends):
    result = AlgorithmRegistry()
    for method in (WikimediaLegacy(scorer), GrowthRatio(), MedianBaseline(),
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
        item['execution'] = _RUNTIME_EXECUTION.get(meta.algorithm_id, {
            "status": "REGISTERED_ONLY", "sources": (),
            "reason": "No persisted execution path is configured.",
        })
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
