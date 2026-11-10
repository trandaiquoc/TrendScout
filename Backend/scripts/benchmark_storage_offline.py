"""Synthetic in-process benchmark; no credentials, network or Firestore."""
import asyncio
import json
import time
import tracemalloc
from types import SimpleNamespace
import httpx
from app.main import create_app
from app.storage.fake import FakeStorage
from app.storage_analysis.metrics import CountingStorage


async def benchmark(size):
    fake = FakeStorage()
    run = 'a' * 32
    await fake.create_run(run, {'run_id': run, 'status': 'COMPLETED', 'keyword_count': size})
    buckets = [[] for _ in range(16)]
    for i in range(size):
        key = f'{i % 16:x}{i:063x}'
        buckets[i % 16].append({'keyword_id': key, 'search_keyword': f'synthetic keyword {i}',
                               'categories': ['fashion'] if i % 3 == 0 else ['unclassified'],
                               'score': i % 101, 'current_views_ceil': i * 100, 'padding': 'x' * 300})
    for i, rows in enumerate(buckets):
        await fake.put_record(run, 'keyword_results', f'shard-{i:02d}', {'items': rows})
    counter = CountingStorage(fake)
    app = create_app(storage=fake)
    app.state.run_service = SimpleNamespace(storage=counter)
    tracemalloc.start()
    start = time.perf_counter()
    response_bytes = 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for i in range(10):
            response = await client.get(f'/api/runs/{run}/keywords', params={'limit': 50, 'offset': (i % 2)*50,
                                        'q': 'synthetic' if i % 2 else '', 'category': 'fashion' if i % 3 == 0 else 'all'})
            response.raise_for_status()
            response_bytes += len(response.content)
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    m = counter.metrics
    return {'entities': size, 'shards': 16, 'requests': 10, 'estimated_document_reads': m['get_run'].estimated_reads + m['list_records'].estimated_reads,
            'storage_json_response_bytes': m['get_run'].response_json_bytes + m['list_records'].response_json_bytes,
            'http_response_bytes': response_bytes, 'elapsed_seconds': round(elapsed, 4), 'peak_traced_bytes': peak,
            'cache': 'disabled; baseline equals current'}


async def main():
    for n in (1000, 5000, 10000):
        print(json.dumps(await benchmark(n), sort_keys=True))

if __name__ == '__main__':
    asyncio.run(main())
