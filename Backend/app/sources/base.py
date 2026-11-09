"""Source registration metadata; collection contracts remain backwards compatible."""
from dataclasses import dataclass

from app.adapters.base import CollectionSource, SourceAdapter
from app.models.observations import CollectionResult


@dataclass(frozen=True)
class SourceMetadata:
    source_id: str
    name: str
    country_scope: str
    access: str
    metric: str
    metric_description: str
    capabilities: tuple[str, ...]
    configuration: tuple[str, ...]
