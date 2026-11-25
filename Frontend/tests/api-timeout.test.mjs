import assert from 'node:assert/strict';
import test from 'node:test';

import { getJson, requestJson } from '../public/modules/api.js';
import { loadCapabilities } from '../public/modules/capabilities.js';

async function capturedTimeout(request) {
  const previous = {
    fetch: globalThis.fetch,
    setTimeout: globalThis.setTimeout,
    clearTimeout: globalThis.clearTimeout,
  };
  const delays = [];
  globalThis.fetch = async () => new Response('{}', {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
  globalThis.setTimeout = (_callback, delay) => {
    delays.push(delay);
    return 1;
  };
  globalThis.clearTimeout = () => {};
  try {
    await request();
    return delays[0];
  } finally {
    globalThis.fetch = previous.fetch;
    globalThis.setTimeout = previous.setTimeout;
    globalThis.clearTimeout = previous.clearTimeout;
  }
}

async function requestedTimeout(path, options = {}) {
  return capturedTimeout(() => getJson(path, options));
}

test('slow Firestore-backed dashboard reads allow Vercel cold starts', async () => {
  for (const path of [
    '/api/v1/collections',
    '/api/v1/collections/collection-1',
    '/api/v1/dashboard/bootstrap',
    '/api/v1/datasets/latest',
    '/api/v1/datasets/latest/keywords?limit=50',
    '/api/v1/analytics/overview',
    '/api/v1/runs/history?limit=5&cursor=',
    '/api/v1/runs/history/abc123',
    '/api/health',
    '/api/v1/capabilities',
  ]) {
    assert.equal(await requestedTimeout(path), 65_000, path);
  }
});

test('Fetch submissions stop before Vercel while checkpoint persistence keeps its snapshot allowance', async () => {
  assert.equal(await requestedTimeout('/api/v1/fetches'), 55_000);
  assert.equal(await requestedTimeout(`/api/v1/fetches/${'a'.repeat(32)}`), 55_000);
  assert.equal(await requestedTimeout(`/api/v1/fetches/${'a'.repeat(32)}/collections`), 65_000);
});

test('capability checks allow a measured Vercel cold start within the function limit', async () => {
  assert.equal(await capturedTimeout(() => loadCapabilities()), 55_000);
});

test('ordinary API calls keep the standard timeout and callers may override it', async () => {
  assert.equal(await requestedTimeout('/api/v1/algorithms'), 15_000);
  assert.equal(await requestedTimeout('/api/v1/collections', { timeoutMs: 7_000 }), 7_000);
});

test('identical concurrent page reads share one same-origin request', async () => {
  const previousFetch = globalThis.fetch;
  let fetchCount = 0;
  let resolveFetch;
  globalThis.fetch = () => {
    fetchCount += 1;
    return new Promise((resolve) => { resolveFetch = resolve; });
  };
  const path = `/api/v1/datasets/latest?dedupe=${Date.now()}`;
  try {
    const first = getJson(path);
    const second = getJson(path);
    await Promise.resolve();
    assert.equal(fetchCount, 1);
    resolveFetch(new Response('{"dataset_id":"one"}', {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));
    const [left, right] = await Promise.all([first, second]);
    assert.deepEqual(left, { dataset_id: 'one' });
    assert.deepEqual(right, { dataset_id: 'one' });
    assert.notEqual(left, right);
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test('page read cache expires quickly and mutations invalidate it', async () => {
  const previousFetch = globalThis.fetch;
  const previousNow = Date.now;
  let now = 10_000;
  let pageReads = 0;
  globalThis.fetch = async (path, options) => {
    if (path.startsWith('/api/v1/analytics/overview')) {
      pageReads += 1;
      return new Response(JSON.stringify({ revision: pageReads }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response('{"saved":true}', {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };
  Date.now = () => now;
  const path = `/api/v1/analytics/overview?ttl=${previousNow()}`;
  try {
    assert.deepEqual(await getJson(path), { revision: 1 });
    assert.deepEqual(await getJson(path), { revision: 1 });
    assert.equal(pageReads, 1);

    now += 3_001;
    assert.deepEqual(await getJson(path), { revision: 2 });
    assert.equal(pageReads, 2);

    await requestJson('/api/v1/collections', {
      method: 'POST',
      body: { name: 'test' },
    });
    assert.deepEqual(await getJson(path), { revision: 3 });
    assert.equal(pageReads, 3);
  } finally {
    globalThis.fetch = previousFetch;
    Date.now = previousNow;
  }
});

test('dashboard bootstrap response is cached briefly and invalidated by writes', async () => {
  const previousFetch = globalThis.fetch;
  let reads = 0;
  globalThis.fetch = async () => new Response(JSON.stringify({ revision: ++reads }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
  try {
    const path = '/api/v1/dashboard/bootstrap';
    assert.deepEqual(await getJson(path), { revision: 1 });
    assert.deepEqual(await getJson(path), { revision: 1 });
    assert.equal(reads, 1);
    await requestJson('/api/v1/collections', { method: 'POST', body: { name: 'test' } });
    assert.deepEqual(await getJson(path), { revision: 3 });
    assert.equal(reads, 3);
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test('Collections job polling bypasses the response cache', async () => {
  const previousFetch = globalThis.fetch;
  let reads = 0;
  const path = `/api/v1/collections/jobs/${Date.now()}`;
  globalThis.fetch = async () => {
    reads += 1;
    return new Response(JSON.stringify({ status: `state-${reads}` }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };
  try {
    assert.deepEqual(await getJson(path), { status: 'state-1' });
    assert.deepEqual(await getJson(path), { status: 'state-2' });
    assert.equal(reads, 2);
  } finally {
    globalThis.fetch = previousFetch;
  }
});
