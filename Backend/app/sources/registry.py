"""Explicit source registration with lazy adapter construction."""
from collections.abc import Callable

from app.adapters.base import CollectionSource
from app.sources.base import SourceMetadata


class SourceRegistry:
    def __init__(self):
        self._entries: dict[str, tuple[SourceMetadata, Callable[[], CollectionSource]]] = {}

    def register(self, metadata: SourceMetadata, factory: Callable[[], CollectionSource]) -> None:
        if not metadata.source_id or not metadata.source_id.strip():
            raise ValueError("Source ID is required")
        if metadata.source_id in self._entries:
            raise ValueError(f"Source already registered: {metadata.source_id}")
        if not callable(factory):
            raise TypeError("Source factory must be callable")
        self._entries[metadata.source_id] = (metadata, factory)

    def metadata(self, source_id: str) -> SourceMetadata:
        return self._entry(source_id)[0]

    def factory(self, source_id: str) -> Callable[[], CollectionSource]:
        return self._entry(source_id)[1]

    def create(self, source_id: str) -> CollectionSource:
        return self.factory(source_id)()

    def _entry(self, source_id: str):
        try:
            return self._entries[source_id]
        except KeyError:
            raise ValueError(f"Unsupported source: {source_id}") from None
