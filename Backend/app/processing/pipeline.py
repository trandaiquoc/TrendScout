"""Deterministic processing stages; source-specific rules are injected."""
from collections import Counter
from dataclasses import replace
from datetime import date, datetime
import re
import unicodedata
from urllib.parse import unquote_to_bytes

from app.models.observations import DatasetSnapshot, FetchStatus, PageObservation
from app.processing.contracts import (CleaningReport, CleaningResult, ProcessingContext,
                                      RejectedRecord, StageResult)


def schema_validation(row, context):
    if not all(isinstance(x, str) and x.strip() for x in (row.source, row.country_scope, row.access, row.project, row.article_original)):
        return StageResult(row, 'MISSING_REQUIRED_FIELD')
    if type(row.observation_date) is not date or row.observation_date != context.snapshot.observation_date:
        return StageResult(row, 'INVALID_OBSERVATION_DATE')
    if not isinstance(row.fetched_at, datetime) or row.fetched_at.tzinfo is None or row.fetched_at.utcoffset() is None:
        return StageResult(row, 'INVALID_FETCH_TIMESTAMP')
    return StageResult(row)


def scope_validation(row, context):
    snapshot = context.snapshot
    if (row.source, row.country_scope, row.access) != (snapshot.source, snapshot.country_scope, snapshot.access):
        return StageResult(row, 'SNAPSHOT_SCOPE_MISMATCH')
    return StageResult(row)


def normalize_title(title):
    if re.search(r'%[0-9a-fA-F]{2}', title):
        title = unquote_to_bytes(title).decode('utf-8', errors='strict')
    return ' '.join(unicodedata.normalize('NFC', title).replace('_', ' ').split())


def normalization(row, context):
    try:
        title = normalize_title(row.article_original)
    except UnicodeDecodeError:
        return StageResult(row, 'INVALID_TITLE_ENCODING')
    if not title:
        return StageResult(row, 'EMPTY_TITLE')
    return StageResult(replace(row, article_display=title, canonical_keyword=title))


def exact_dedup(row, context):
    key = (row.source, row.country_scope, row.access, row.project, row.article_original, row.observation_date)
    if key in context.seen:
        return StageResult(row, duplicate=True)
    context.seen.add(key)
    return StageResult(row)


def process_snapshot(snapshot: DatasetSnapshot, rows: list[PageObservation], *, source_rule, validate_source):
    if snapshot.item_count is None:
        if rows:
            raise ValueError('Snapshot without a source count cannot contain observations')
        report = CleaningReport(snapshot.observation_date, snapshot.fetch_status, None, None, None, None, {}, ('SOURCE_UNAVAILABLE',))
        return CleaningResult(report, (), (), ())
    if (type(snapshot.item_count) is not int or snapshot.item_count < 0
            or type(snapshot.rejected_count) is not int or snapshot.rejected_count < 0
            or snapshot.item_count != len(rows) + snapshot.rejected_count):
        raise ValueError('Adapter input counts do not reconcile')
    accepted, rejected, duplicates = [], [], []
    reasons = Counter()
    if snapshot.rejected_count:
        reasons['ADAPTER_INVALID'] = snapshot.rejected_count
    context = ProcessingContext(snapshot)
    for original in rows:
        current = original
        reason = None
        for stage in (schema_validation, validate_source, normalization):
            outcome = stage(current, context)
            if outcome.reason:
                reason = outcome.reason
                break
            current = outcome.observation
        if reason is None:
            reason = source_rule(current.project, current.canonical_keyword)
        if reason:
            reasons[reason] += 1
            rejected.append(RejectedRecord(reason, original))
            continue
        if exact_dedup(current, context).duplicate:
            duplicates.append(original)
        else:
            accepted.append(current)
    flags = []
    if snapshot.fetch_status != FetchStatus.SUCCESS:
        flags.append('SOURCE_PARTIAL_OR_ERROR')
    if snapshot.rejected_count and len(snapshot.rejected_records) != snapshot.rejected_count:
        flags.append('ADAPTER_REJECTIONS_WITHOUT_ROW_DETAILS')
    if duplicates:
        flags.append('DUPLICATES_REMOVED_FIRST_WINS')
    report = CleaningReport(snapshot.observation_date, snapshot.fetch_status, snapshot.item_count,
                            len(accepted), sum(reasons.values()), len(duplicates), dict(reasons), tuple(flags))
    return CleaningResult(report, tuple(accepted), tuple(rejected), tuple(duplicates), snapshot.rejected_records)
