"""Strict offline evaluation. Labels are never passed to algorithm execution."""
from collections import Counter
from datetime import date, datetime, timedelta
import json
import math

LABELS = {'POSITIVE', 'NEGATIVE', 'UNCERTAIN', 'NOT_REVIEWED'}
# These are ranking policies, not comparable numerical scales.
POLICIES = {
    'volume-only@v0.1': ('views_ceil', 'approx_pageviews_upper_bound'),
    'wikimedia-trend@v0.1': ('trend_score', 'points_0_100'),
    'growth-ratio@v0.1': ('growth_ratio', 'dimensionless'),
}
NONCOMPARABLE = {'median-baseline@v0.1', 'ewma@v0.1', 'robust-z-mad@v0.1'}
SCOPE_KEYS = ('source', 'country', 'access', 'project', 'metric', 'unit')


def _day(value):
    if not isinstance(value, str):
        raise ValueError('date must be ISO formatted')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('invalid date') from exc
    if parsed.isoformat() != value:
        raise ValueError('date must be canonical ISO format')
    return parsed


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def load_dataset(path):
    """Load and validate an immutable-by-convention JSON dataset; no network access."""
    with open(path, encoding='utf-8') as handle:
        dataset = json.load(handle)
    return validate_dataset(dataset)


def validate_dataset(dataset):
    if not isinstance(dataset, dict):
        raise ValueError('invalid dataset')
    for key in ('dataset_id', 'dataset_version', 'label_policy_version', 'mode'):
        if not _nonempty(dataset.get(key)):
            raise ValueError('missing dataset metadata: ' + key)
    if dataset['mode'] not in ('SYNTHETIC', 'HUMAN_REVIEWED'):
        raise ValueError('invalid mode')
    scope = dataset.get('scope')
    if not isinstance(scope, dict) or any(not _nonempty(scope.get(k)) for k in SCOPE_KEYS):
        raise ValueError('invalid scope')
    day = _day(dataset.get('evaluation_date'))
    window = dataset.get('window_days')
    if type(window) is not int or not 1 <= window <= 90:
        raise ValueError('invalid window')
    rows = dataset.get('entities')
    if not isinstance(rows, list):
        raise ValueError('missing entities')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or any(not _nonempty(row.get(k)) for k in ('entity_id', 'title', 'provenance')):
            raise ValueError('invalid entity')
        if row['entity_id'] in seen:
            raise ValueError('duplicate entity')
        seen.add(row['entity_id'])
        if row.get('scope') != scope:
            raise ValueError('inconsistent entity scope')
        if _day(row.get('current_date')) != day:
            raise ValueError('invalid current date')
        if type(row.get('current_value')) is not int or row['current_value'] < 0:
            raise ValueError('invalid current value')
        history = row.get('history')
        if not isinstance(history, list):
            raise ValueError('missing history')
        dates = set()
        for point in history:
            point_day = _day(point.get('date'))
            if not day - timedelta(days=window) <= point_day < day:
                raise ValueError('current or future leakage / outside window')
            if point_day in dates:
                raise ValueError('duplicate history date')
            dates.add(point_day)
            if type(point.get('value')) is not int or point['value'] < 0:
                raise ValueError('invalid history value')
        label = row.get('label')
        if not isinstance(label, dict) or label.get('value') not in LABELS:
            raise ValueError('missing or invalid label')
        if _day(label.get('evidence_as_of')) > day:
            raise ValueError('future label evidence')
        if label['value'] != 'NOT_REVIEWED':
            if not _nonempty(label.get('reviewer_id')) or not _nonempty(label.get('review_timestamp')):
                raise ValueError('missing reviewer metadata')
            try:
                timestamp = datetime.fromisoformat(label['review_timestamp'].replace('Z', '+00:00'))
            except ValueError as exc:
                raise ValueError('invalid review timestamp') from exc
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError('review timestamp needs timezone')
        if label.get('disagreement') and label['value'] != 'UNCERTAIN':
            raise ValueError('unresolved disagreement requires UNCERTAIN')
    return dataset


def prediction_from_result(result, *, scope, evaluation_date, feature_dates):
    """Adapt a genuine AlgorithmResult while retaining the input context supplied by caller."""
    return {'entity_id': result.keyword_id,
            'algorithm': f'{result.algorithm_id}@{result.algorithm_version}',
            'status': result.status, 'value': result.value,
            'metric': result.metric_name, 'unit': result.metric_unit,
            'reason': result.reason_code, 'window_days': result.window_days,
            'scope': scope, 'evaluation_date': evaluation_date,
            'feature_dates': list(feature_dates)}


def _validate_prediction(p, dataset, entities, algorithm):
    if p.get('entity_id') not in entities or p.get('algorithm') != algorithm:
        raise ValueError('unknown entity or algorithm')
    if p.get('scope') != dataset['scope'] or p.get('evaluation_date') != dataset['evaluation_date'] or p.get('window_days') != dataset['window_days']:
        raise ValueError('incompatible scope/date/window')
    if (p.get('metric'), p.get('unit')) != POLICIES[algorithm]:
        raise ValueError('incompatible ranking metric/unit')
    dates = p.get('feature_dates')
    if not isinstance(dates, list):
        raise ValueError('missing feature dates')
    parsed = [_day(d) for d in dates]
    day = _day(dataset['evaluation_date'])
    if len(set(parsed)) != len(parsed) or any(d > day or d < day - timedelta(days=dataset['window_days']) for d in parsed):
        raise ValueError('invalid feature dates')
    allowed = {_day(x['date']) for x in entities[p['entity_id']]['history']} | {day}
    if not set(parsed) <= allowed:
        raise ValueError('feature date not present in dataset')
    status, value = p.get('status'), p.get('value')
    if status not in ('COMPLETED', 'SKIPPED', 'FAILED'):
        raise ValueError('invalid result status')
    if status == 'COMPLETED':
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('invalid completed value')
        if algorithm == 'volume-only@v0.1' and value != entities[p['entity_id']]['current_value']:
            raise ValueError('volume must equal current observation')
    elif value is not None:
        raise ValueError('skipped/failed value must be null')
    elif not _nonempty(p.get('reason')):
        raise ValueError('missing exclusion reason')


def evaluate(dataset, predictions, k, evaluation_id):
    """Compare predefined rankings on the intersection of COMPLETED entities.

    Precision is withheld unless exactly K ranked entities have binary labels.
    FP share is conditional on binary-reviewed entries in the top K, not FPR.
    """
    validate_dataset(dataset)
    if type(k) is not int or k <= 0 or not _nonempty(evaluation_id):
        raise ValueError('invalid K or evaluation id')
    entities = {row['entity_id']: row for row in dataset['entities']}
    if not isinstance(predictions, dict) or not predictions:
        raise ValueError('no algorithms')
    prepared, reports = {}, {}
    for algorithm, values in sorted(predictions.items()):
        if algorithm in NONCOMPARABLE or algorithm not in POLICIES:
            reports[algorithm] = {'status': 'NOT_COMPARABLE', 'reason': 'No approved trend ranking policy for this output'}
            continue
        by_id = {}
        for p in values:
            _validate_prediction(p, dataset, entities, algorithm)
            if p['entity_id'] in by_id:
                raise ValueError('duplicate prediction')
            by_id[p['entity_id']] = p
        if set(by_id) != set(entities):
            raise ValueError('every entity needs an explicit result')
        prepared[algorithm] = by_id
    common = set(entities)
    for by_id in prepared.values():
        common &= {entity for entity, p in by_id.items() if p['status'] == 'COMPLETED'}
    if not prepared:
        common = set()
    for algorithm, by_id in prepared.items():
        counts = Counter(p['status'] for p in by_id.values())
        reasons = Counter(f"{p['status']}:{p['reason']}" for p in by_id.values() if p['status'] != 'COMPLETED')
        ranked = sorted(common, key=lambda entity: (-by_id[entity]['value'], entity))
        top = ranked[:k]
        labels = [entities[entity]['label']['value'] for entity in top]
        reviewed = [v for v in labels if v in ('POSITIVE', 'NEGATIVE')]
        reason = 'INSUFFICIENT_RESULTS' if len(top) < k else ('INSUFFICIENT_LABELS' if len(reviewed) < k else None)
        reports[algorithm] = {
            'status': reason or 'EVALUATED', 'ranking_metric': by_id[next(iter(by_id))]['metric'] if by_id else POLICIES[algorithm][0],
            'ranking_unit': POLICIES[algorithm][1], 'direction': 'descending', 'tie_break': 'entity_id_ascending',
            'eligible_count': len(common), 'ranked_entity_ids': ranked, 'top_k_entity_ids': top,
            'considered': len(entities), 'completed': counts['COMPLETED'], 'skipped': counts['SKIPPED'], 'failed': counts['FAILED'],
            'coverage': counts['COMPLETED'] / len(entities) if entities else None,
            'skip_rate': counts['SKIPPED'] / len(entities) if entities else None,
            'failed_rate': counts['FAILED'] / len(entities) if entities else None,
            'exclusion_reasons': dict(sorted(reasons.items())),
            'labeled_count': sum(row['label']['value'] in ('POSITIVE', 'NEGATIVE') for row in entities.values()),
            'top_k_reviewed_count': len(reviewed),
            'precision_at_k': None if reason else reviewed.count('POSITIVE') / k,
            'false_positive_count_reviewed_top_k': reviewed.count('NEGATIVE'),
            'false_positive_share_reviewed_top_k': reviewed.count('NEGATIVE') / len(reviewed) if reviewed else None,
        }
    return {'evaluation_id': evaluation_id, 'dataset_id': dataset['dataset_id'], 'dataset_version': dataset['dataset_version'],
            'mode': dataset['mode'], 'evaluation_date': dataset['evaluation_date'], 'window_days': dataset['window_days'],
            'scope': dataset['scope'], 'k': k, 'common_universe': sorted(common), 'algorithms': reports,
            'notes': 'SYNTHETIC is test-only; not real-world accuracy' if dataset['mode'] == 'SYNTHETIC' else 'Subject to independent label and sampling review'}
