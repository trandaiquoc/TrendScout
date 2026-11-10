"""Typed contracts for independent algorithm results; no provider SDKs."""
from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class Scope:
    source: str
    country: str
    access: str
    project: str
    article: str
    metric: str
    unit: str


@dataclass(frozen=True)
class Observation:
    date: date
    scope: Scope
    status: str
    value: int | None
    snapshot_status: str = 'SUCCESS'


@dataclass(frozen=True)
class AlgorithmInput:
    keyword_id: str
    scope: Scope
    analysis_date: date
    current_value: int | None
    history: tuple[Observation, ...]
    window_days: int = 7
    minimum_samples: int = 3
    evidence_url: str | None = None
    legacy_result: object | None = None
    current_snapshot_status: str = 'UNKNOWN'


@dataclass(frozen=True)
class AlgorithmMetadata:
    algorithm_id: str
    version: str
    name: str
    source: str
    metric: str
    unit: str
    minimum_samples: int
    max_window_days: int
    input_metric: str | None = None
    input_unit: str | None = None

    @property
    def key(self):
        return f'{self.algorithm_id}@{self.version}'


@dataclass(frozen=True)
class AlgorithmResult:
    keyword_id: str
    algorithm_id: str
    algorithm_version: str
    status: str
    value: float | int | None
    metric_name: str
    metric_unit: str
    current_value: int | None
    baseline_value: float | None
    window_days: int
    sample_count: int
    evidence: tuple
    quality_flags: tuple[str, ...]
    reason_code: str | None = None
    reason_message: str | None = None
    minimum_samples: int = 3
    config_version: str = 'algorithm-input-v0.1'


class Strategy(Protocol):
    metadata: AlgorithmMetadata
    def applicable(self, item: AlgorithmInput) -> str | None: ...
    def execute(self, item: AlgorithmInput) -> AlgorithmResult: ...
