"""Instrument AnalysisStorage calls without recording identifiers or payloads.

Counts are application-level estimates, not Firestore billing telemetry.
"""
import json
from collections import defaultdict
from dataclasses import dataclass
from time import perf_counter


def json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, default=str, separators=(',', ':')).encode('utf-8'))


@dataclass
class OperationMetrics:
    calls: int = 0
    successes: int = 0
    failures: int = 0
    returned_documents: int = 0
    estimated_reads: int = 0
    estimated_writes: int = 0
    request_json_bytes: int = 0
    response_json_bytes: int = 0
    elapsed_seconds: float = 0


class CountingStorage:
    """Opt-in diagnostic decorator. No raw payload or identifier is logged."""
    def __init__(self, delegate):
        self.delegate = delegate
        self.metrics = defaultdict(OperationMetrics)

    async def _call(self, name, *args, **kwargs):
        metric = self.metrics[name]
        metric.calls += 1
        start = perf_counter()
        if name in ('create_run', 'update_run', 'put_record'):
            metric.request_json_bytes += json_bytes(args[-1])
        try:
            result = await getattr(self.delegate, name)(*args, **kwargs)
        except BaseException:
            metric.failures += 1
            raise
        else:
            metric.successes += 1
            if name in ('create_run', 'update_run', 'put_record'):
                metric.estimated_writes += 1
            elif name in ('get_run', 'get_record'):
                metric.returned_documents += int(result is not None)
                metric.estimated_reads += 1
                if result is not None:
                    metric.response_json_bytes += json_bytes(result)
            elif name in ('list_records', 'list_runs', 'list_runs_after'):
                metric.returned_documents += len(result)
                metric.estimated_reads += max(1, len(result))
                metric.response_json_bytes += sum(json_bytes(document) for document in result)
            return result
        finally:
            metric.elapsed_seconds += perf_counter() - start

    async def create_run(self, run_id, data): return await self._call('create_run', run_id, data)
    async def get_run(self, run_id): return await self._call('get_run', run_id)
    async def list_runs(self, *, limit=100, offset=0): return await self._call('list_runs', limit=limit, offset=offset)
    async def list_runs_after(self, *, limit=100, after_run_id=None): return await self._call('list_runs_after', limit=limit, after_run_id=after_run_id)
    async def update_run(self, run_id, fields): return await self._call('update_run', run_id, fields)
    async def put_record(self, run_id, kind, record_id, data): return await self._call('put_record', run_id, kind, record_id, data)
    async def get_record(self, run_id, kind, record_id): return await self._call('get_record', run_id, kind, record_id)
    async def list_records(self, run_id, kind): return await self._call('list_records', run_id, kind)
