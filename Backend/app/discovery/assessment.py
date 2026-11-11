"""Explainable, source-scoped trend assessment over explicitly covered samples."""

from collections import defaultdict
from datetime import date, datetime, timedelta

from app.discovery.contracts import Assessment, Candidate, CoverageDay, ProcessingGrant
from app.discovery.extraction import _utc

CURRENT_DAYS = 3
BASELINE_DAYS = 7


def _days(end: date, count: int) -> tuple[date, ...]:
    return tuple(end - timedelta(days=i) for i in range(count - 1, -1, -1))


def _coverage_map(rows: tuple[CoverageDay, ...]) -> dict:
    result = {}
    for row in rows:
        if (not isinstance(row, CoverageDay) or type(row.day) is not date
                or row.status not in {"COMPLETE", "PARTIAL", "UNAVAILABLE"}
                or row.data_mode not in {"LIVE", "SYNTHETIC"}
                or type(row.absence_observable_in_sample) is not bool
                or not all(isinstance(x, str) and x.strip() for x in (
                    row.source_id, row.scope, row.parser_version))
                or (row.eligible_documents is not None and (
                    type(row.eligible_documents) is not int
                    or not 0 <= row.eligible_documents <= 10_000_000))):
            raise ValueError("Invalid coverage record")
        key = (row.source_id, row.scope, row.parser_version, row.data_mode, row.day)
        if key in result:
            raise ValueError("Duplicate coverage record")
        result[key] = row
    return result


def _window(days, key, coverage, counts):
    entries = [coverage.get((*key, day)) for day in days]
    if any(row is None or row.status != "COMPLETE" or row.eligible_documents is None
           or not row.absence_observable_in_sample for row in entries):
        return None
    support = sum(len(counts.get(day, set())) for day in days)
    exposure = sum(row.eligible_documents for row in entries)
    if any(len(counts.get(day, set())) > row.eligible_documents for day, row in zip(days, entries)):
        raise ValueError("Candidate support exceeds eligible sample")
    if exposure == 0:
        return None
    active_days = sum(bool(counts.get(day)) for day in days)
    return support, exposure, active_days, support / exposure


def _seasonal(as_of, key, coverage, counts) -> bool:
    """Require two comparable past windows; this remains an unvalidated heuristic."""
    for years in (1, 2):
        historical_end = as_of - timedelta(days=365 * years)
        recent = _window(_days(historical_end, CURRENT_DAYS), key, coverage, counts)
        baseline_end = historical_end - timedelta(days=CURRENT_DAYS)
        previous = _window(_days(baseline_end, BASELINE_DAYS), key, coverage, counts)
        if (recent is None or previous is None or recent[0] < 2 or recent[2] < 2
                or previous[0] == 0 or recent[3] / previous[3] < 1.5):
            return False
    return True


def assess_candidate(candidate: Candidate, coverage: tuple[CoverageDay, ...], *,
                     as_of: date, now: datetime,
                     grants: tuple[ProcessingGrant, ...] = ()) -> tuple[Assessment, ...]:
    """Assess each source/scope/mode separately; absence is only within a complete sample."""
    if not isinstance(candidate, Candidate) or type(as_of) is not date:
        raise ValueError("Candidate and as-of date required")
    now = _utc(now)
    if as_of > now.date():
        raise ValueError("Assessment date cannot be in the future")
    grants_by_source = {item.source_id: item for item in grants}
    if len(grants_by_source) != len(grants):
        raise ValueError("Duplicate source processing grant")
    coverage_by_day = _coverage_map(coverage)
    partitions = defaultdict(list)
    for evidence in candidate.evidence:
        if (_utc(evidence.fetched_at) > now
                or _utc(evidence.observed_at) > _utc(evidence.fetched_at)):
            raise ValueError("Invalid evidence timestamp")
        key = (evidence.source_id, evidence.scope, evidence.parser_version, evidence.data_mode)
        if evidence.observed_at.date() <= as_of:
            partitions[key].append(evidence)
    current_days = _days(as_of, CURRENT_DAYS)
    baseline_days = _days(as_of - timedelta(days=CURRENT_DAYS), BASELINE_DAYS)
    results = []
    for key, evidence_rows in sorted(partitions.items()):
        source_id, scope, parser_version, mode = key
        if mode == "LIVE":
            grant = grants_by_source.get(source_id)
            if (not isinstance(grant, ProcessingGrant) or grant.derived_metrics_allowed is not True
                    or grant.extraction_allowed is not True or not grant.policy_version.strip()
                    or _utc(grant.expires_at) <= now):
                raise PermissionError("Source derived-metrics approval is unavailable")
        counts = defaultdict(set)
        for item in evidence_rows:
            counts[item.observed_at.date()].add(item.content_fingerprint)
        current = _window(current_days, key, coverage_by_day, counts)
        baseline = _window(baseline_days, key, coverage_by_day, counts)
        # The weakest evidence in the current window limits the claim. Historical
        # US-scoped records cannot confer a US label on unrelated current data.
        current_market = {item.market_evidence for item in evidence_rows
                          if item.observed_at.date() in current_days}
        market = "UNKNOWN"
        if current_market == {"US_COUNTRY_SCOPED"}:
            market = "US_COUNTRY_SCOPED"
        elif current_market and current_market <= {"US_COUNTRY_SCOPED", "US_FILTER_ONLY"}:
            market = "US_FILTER_ONLY"
        limits = ("SAMPLE_NOT_US_POPULATION", "RULE_BASED_NOT_PROBABILITY", "COVERAGE_LIMITED")
        if any(item.delivery_mode == "CACHED" for item in evidence_rows):
            limits += ("INCLUDES_CACHED_EVIDENCE",)
        classification, confidence, ratio = "INSUFFICIENT_EVIDENCE", "LOW", None
        reason = "Comparable complete sample coverage and baseline are required."
        if current is not None and baseline is not None:
            if baseline[0] == 0 and current[0] >= 2 and current[2] >= 2:
                classification = "EMERGING"
                reason = "New within the observed baseline sample; not new across the Internet."
            elif baseline[0] and baseline[3] > 0:
                ratio = current[3] / baseline[3]
                if ratio <= 2 / 3 and baseline[0] >= 2 and baseline[2] >= 2:
                    classification, reason = "DECLINING", "Sample presence rate declined from baseline."
                elif current[0] >= 2 and current[2] >= 2:
                    if ratio >= 1.5:
                        if _seasonal(as_of, key, coverage_by_day, counts):
                            classification = "SEASONAL"
                            reason = "Two comparable prior yearly windows show recurring increases."
                            limits += ("SEASONALITY_HEURISTIC_UNVALIDATED",)
                        else:
                            classification, reason = "ACCELERATING", "Sample presence rate rose across multiple days."
                    elif current[2] == CURRENT_DAYS and baseline[2] >= 3:
                        classification, reason = "SUSTAINED", "Evidence persists across current and baseline windows."
                    else:
                        classification, reason = "STABLE", "Sample presence is comparable to the baseline."
            if classification != "INSUFFICIENT_EVIDENCE" and current[0] >= 5:
                confidence = "MODERATE"
            if classification == "INSUFFICIENT_EVIDENCE":
                reason = "Support or temporal persistence is insufficient for classification."
        results.append(Assessment(
            candidate.keyword_id, source_id, scope, parser_version, mode, market, as_of,
            classification, confidence, current[0] if current else None,
            baseline[0] if baseline else None, current[1] if current else None,
            baseline[1] if baseline else None, current[3] if current else None,
            baseline[3] if baseline else None, ratio, (current_days[0], current_days[-1]),
            (baseline_days[0], baseline_days[-1]), reason, limits,
        ))
    return tuple(results)
