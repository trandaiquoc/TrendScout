"""Small contracts for auditable, source-aware observation processing."""
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from app.models.observations import DatasetSnapshot, FetchStatus, PageObservation

PIPELINE_VERSION = 'cleaning-pipeline-v0.1'
RejectionReason = str


@dataclass
class ProcessingContext:
    snapshot: DatasetSnapshot
    seen: set[tuple[str, str, str, str, str, date]] = field(default_factory=set)


@dataclass(frozen=True)
class StageResult:
    observation: PageObservation
    reason: RejectionReason | None = None
    duplicate: bool = False


class ProcessingStage(Protocol):
    def __call__(self, row: PageObservation, context: ProcessingContext) -> StageResult: ...


@dataclass(frozen=True)
class RejectedRecord:
    reason: RejectionReason
    observation: PageObservation


@dataclass(frozen=True)
class CleaningReport:
    observation_date: date
    fetch_status: FetchStatus
    raw_count: int | None
    accepted_count: int | None
    rejected_count: int | None
    duplicate_count: int | None
    rejection_reasons: dict[str, int]
    quality_flags: tuple[str, ...]

    @property
    def pipeline_version(self) -> str:
        return PIPELINE_VERSION


QualityReport = CleaningReport


@dataclass(frozen=True)
class CleaningResult:
    report: CleaningReport
    accepted: tuple[PageObservation, ...]
    rejected: tuple[RejectedRecord, ...]
    duplicates: tuple[PageObservation, ...]
    adapter_rejected: tuple[object, ...] = ()


PipelineResult = CleaningResult
