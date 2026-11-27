"""Ranks provider-reported Google Trends increases within one snapshot cohort."""

from __future__ import annotations

import math
from datetime import date

from app.algorithms.contracts import AlgorithmMetadata, AlgorithmResult
from app.sources.catalog import GOOGLE_SOURCE_ID

INCREASE_METRIC = "provider_increase_percentage"
INCREASE_UNIT = "provider_reported_percentage"


def _valid_percentage(value):
    if type(value) not in {int, float} or value < 0 or value > 1_000_000_000:
        return False
    return type(value) is int or math.isfinite(value)


class GoogleTrendsIncreaseRank:
    """Ordinal ranking of the provider's increase signal, never an app baseline."""

    metadata = AlgorithmMetadata(
        "google-trends-increase-rank", "v1", "Google Trends reported increase rank",
        GOOGLE_SOURCE_ID, "provider_increase_rank", "ordinal_rank", 2, 1,
        input_metric=INCREASE_METRIC, input_unit=INCREASE_UNIT,
        description=("Rank provider-reported increase percentages within one US/English "
                     "24-hour Google Trends snapshot."),
        family="trend", supported_capabilities=("trending_snapshot", "provider_increase"),
        supported_windows=(1,),
        missing_data=("Requires one valid provider increase percentage per keyword and "
                      "at least two comparable records in the same snapshot."),
        accepts_censored_top_list=False,
        runtime_limit="O(n log n) per source snapshot cohort.",
        interpretation=("Rank 1 is the largest provider-reported increase. Ties receive "
                        "the best tied rank; this is not an application-calculated growth rate."),
        limitations=(
            "The provider does not expose an application-compatible historical baseline.",
            "Google's US region filter does not establish US audience residence.",
            "A 24-hour trending snapshot is not a complete daily time series.",
            "Provider-reported approximate search-volume buckets are not this rank's input.",
            "The result is not a calibrated probability.",
        ),
        runtime_requirements=("snapshot_cohort", "input_digest", "evidence_lineage"),
    )

    def applicable(self, item):
        if item.scope.source != GOOGLE_SOURCE_ID:
            return "UNSUPPORTED_SOURCE"
        if item.scope.metric != INCREASE_METRIC:
            return "UNSUPPORTED_METRIC"
        if item.scope.unit != INCREASE_UNIT:
            return "UNSUPPORTED_UNIT"
        if (type(item.analysis_date) is not date or item.window_days != 1
                or item.minimum_samples != 2):
            return "INVALID_WINDOW"
        if (item.scope.country != "US_FILTER_ONLY"
                or item.scope.access != "en"
                or item.scope.project != "google_trends_us_en_24h"
                or not item.scope.article):
            return "INVALID_SCOPE"
        if item.current_snapshot_status != "SUCCESS":
            return "CURRENT_COVERAGE_UNCONFIRMED"
        if not _valid_percentage(item.current_value):
            return ("CURRENT_OBSERVATION_MISSING" if item.current_value is None
                    else "INVALID_CURRENT")
        peers = self._peer_values(item)
        if len(peers) + 1 < self.metadata.minimum_samples:
            return "INSUFFICIENT_COHORT"
        return None

    @staticmethod
    def _peer_values(item):
        peers = {}
        for point in item.cohort:
            scope = point.scope
            if (point.status != "OBSERVED" or point.snapshot_status != "SUCCESS"
                    or point.date != item.analysis_date
                    or scope.source != item.scope.source
                    or scope.country != item.scope.country
                    or scope.access != item.scope.access
                    or scope.project != item.scope.project
                    or scope.metric != item.scope.metric
                    or scope.unit != item.scope.unit
                    or not scope.article or scope.article == item.scope.article
                    or not _valid_percentage(point.value)):
                continue
            peers.setdefault(scope.article, set()).add(point.value)
        return [next(iter(peers[key])) for key in sorted(peers)
                if len(peers[key]) == 1]

    def execute(self, item):
        values = [*self._peer_values(item), item.current_value]
        rank = 1 + sum(value > item.current_value for value in values)
        return AlgorithmResult(
            item.keyword_id, self.metadata.algorithm_id, self.metadata.version,
            "COMPLETED", rank, self.metadata.metric, self.metadata.unit,
            item.current_value, None, 1, len(values),
            (("cohort_size", len(values)),
             ("observation_date", item.analysis_date.isoformat()),
             ("provider_reported_increase_percentage", item.current_value),
             ("source_profile", "google_trends_us_en_24h"),
             ("evidence_url", item.evidence_url)),
            ("PROVIDER_REPORTED_INCREASE", "SINGLE_SNAPSHOT_COHORT"),
            minimum_samples=2, config_version="google-trends-increase-rank-v1",
        )

    def skip_details(self, item, reason):
        count = len(self._peer_values(item)) + (1 if _valid_percentage(item.current_value) else 0)
        return (
            (("cohort_size", count), ("observation_date", item.analysis_date.isoformat()),
             ("source_profile", "google_trends_us_en_24h"),
             ("evidence_url", item.evidence_url)),
            ("PROVIDER_REPORTED_INCREASE", "SINGLE_SNAPSHOT_COHORT"),
            2, "google-trends-increase-rank-v1", count,
        )
