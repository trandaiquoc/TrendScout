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
        compatible = []
        for source_id, source in sources.items():
            if meta.source != '*' and source_id != meta.source:
                status = 'INCOMPATIBLE'
                reason = 'SOURCE_NOT_SUPPORTED'
            elif (meta.input_metric and meta.input_metric not in {'*', source.metric_name}) or \
                    (meta.input_unit and meta.input_unit not in {'*', source.metric_unit}):
                status = 'INCOMPATIBLE'
                reason = 'METRIC_OR_UNIT_NOT_SUPPORTED'
            elif meta.supported_capabilities and not set(meta.supported_capabilities).issubset(source.capabilities):
                status = 'INCOMPATIBLE'
                reason = 'REQUIRED_CAPABILITY_UNAVAILABLE'
            elif 'top_list_censored' in source.capabilities and not meta.accepts_censored_top_list:
                status = 'INCOMPATIBLE'
                reason = 'CENSORED_SOURCE_NOT_ACCEPTED'
            elif source.readiness == 'IMPLEMENTED' and source.adapter_status == 'IMPLEMENTED':
                status, reason = 'SUPPORTED_AND_AVAILABLE', None
            else:
                status, reason = 'SUPPORTED_BUT_NOT_LIVE', source.readiness
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
