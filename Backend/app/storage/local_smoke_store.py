"""Enforce one reserved attempt per source for a local smoke ledger.

This wrapper is a storage boundary, not an approval or a network switch.
The wrapped store must provide atomic transactions across local processes.
"""

from app.jobs.public_admission import AdmissionDenied
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID

_SOURCES = frozenset({WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID})
_COUNTS = "local_smoke_source_counts"


def _receipts(state):
    jobs = state.get("jobs", {})
    if not isinstance(jobs, dict):
        raise AdmissionDenied("SMOKE_LEDGER_INVALID")
    receipts = {}
    for job_id, job in jobs.items():
        if not isinstance(job, dict) or not isinstance(job.get("receipts", {}), dict):
            raise AdmissionDenied("SMOKE_LEDGER_INVALID")
        for receipt_id, receipt in job.get("receipts", {}).items():
            if not isinstance(receipt, dict) or receipt.get("source_id") not in _SOURCES:
                raise AdmissionDenied("SMOKE_LEDGER_INVALID")
            receipts[(job_id, receipt_id)] = receipt["source_id"]
    return receipts


class LocalSmokeStore:
    def __init__(self, store):
        if not callable(getattr(store, "transact", None)):
            raise TypeError("atomic local store required")
        self.store = store

    def transact(self, key, operation):
        def guarded(state):
            before = _receipts(state)
            counts = state.get(_COUNTS)
            if counts is None:
                if before:
                    raise AdmissionDenied("SMOKE_LEDGER_INVALID")
                counts = {}
            if (not isinstance(counts, dict) or not set(counts) <= _SOURCES
                    or any(type(value) is not int or value != 1 for value in counts.values())
                    or not set(before.values()) <= set(counts)):
                raise AdmissionDenied("SMOKE_LEDGER_INVALID")
            counts = dict(counts)
            updated, result = operation(state)
            after = _receipts(updated)
            if not updated and not after:
                # Capability/status inspection must not turn an empty ledger
                # into a truthy, schema-less admission document.
                return updated, result
            for receipt, source in after.items():
                if receipt in before:
                    if before[receipt] != source:
                        raise AdmissionDenied("SMOKE_LEDGER_INVALID")
                elif source in counts:
                    raise AdmissionDenied("SMOKE_SOURCE_LIMIT")
                else:
                    counts[source] = 1
            updated[_COUNTS] = counts
            return updated, result
        return self.store.transact(key, guarded)
