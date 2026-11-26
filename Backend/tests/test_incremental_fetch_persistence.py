"""Offline contract for PostgreSQL's source-batched Fetch persistence."""

from app.collections.service import CollectionService
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, WIKIMEDIA_SOURCE_ID


class IncrementalStore:
    data_mode_scope = "LIVE"
    supports_incremental_fetch_persistence = True
    supports_source_evidence_lineage = True

    def __init__(self):
        self.persistence_calls = []
        self.snapshots = []
        self.completed = []

    def begin_fetch_processing(self, fetch_id, sources, **_options):
        return {
            "action": "PROCESS",
            "retry_existing_snapshot": False,
            "replay_available": 0,
            "replay_skipped": 0,
        }

    def update_fetch_job(self, _fetch_id, _values):
        return None

    def persist_cleaned_keywords(self, records, *, rejected=(), bing_updates=(),
                                 source_evidence_rows=None):
        self.persistence_calls.append({
            "records": list(records),
            "rejected": tuple(rejected),
            "bing_updates": list(bing_updates),
            "source_evidence_rows": list(source_evidence_rows or ()),
        })
        return {
            "records": list(records),
            "skipped_bing_updates": 0,
            "id_remap": {},
        }

    def persist_dataset_snapshot(self, dataset_id, **snapshot):
        self.snapshots.append((dataset_id, snapshot))

    def complete_empty_fetch(self, fetch_id, result):
        self.completed.append((fetch_id, result))


def test_postgres_persists_source_batches_before_deferred_bing_evidence():
    store = IncrementalStore()
    service = CollectionService(store)
    sources = [
        {
            "source_id": WIKIMEDIA_SOURCE_ID,
            "data_mode": "LIVE",
            "status": "SUCCESS",
            "records": [{
                "article_display": "",
                "project": "en.wikipedia",
                "observation_date": "2026-11-25",
                "fetched_at": "2026-11-26T12:00:00Z",
                "provider_rank": 1,
                "views_ceil": 1,
            }],
        },
        {
            "source_id": GOOGLE_SOURCE_ID,
            "data_mode": "LIVE",
            "status": "SUCCESS",
            "records": [],
        },
        {
            "source_id": BING_SOURCE_ID,
            "data_mode": "LIVE",
            "status": "SUCCESS",
            "records": [{
                "candidate_keyword_id": "google-candidate",
                "bing_verification": {
                    "fetched_at": "2026-11-26T12:05:00Z",
                    "query": "solar generator",
                    "results": [],
                },
            }],
        },
    ]

    result = service._process_fetch("incremental-fetch", sources)

    assert result["status"] == "COMPLETED"
    assert len(store.persistence_calls) == 3
    assert store.persistence_calls[0]["source_evidence_rows"][0]["source_id"] == (
        WIKIMEDIA_SOURCE_ID
    )
    assert store.persistence_calls[0]["records"] == []
    assert store.persistence_calls[1]["source_evidence_rows"] == []
    assert store.persistence_calls[2]["bing_updates"] == []
    assert store.persistence_calls[2]["source_evidence_rows"][0]["source_id"] == (
        BING_SOURCE_ID
    )
    assert store.persistence_calls[2]["source_evidence_rows"][0]["keyword_ids"] == (
        "google-candidate",
    )
    assert store.snapshots and store.completed
