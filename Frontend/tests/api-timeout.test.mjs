import assert from 'node:assert/strict';
import test from 'node:test';

import { getJson } from '../public/modules/api.js';
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
    '/api/v1/datasets/latest',
    '/api/v1/datasets/latest/keywords?limit=50',
    '/api/v1/analytics/overview',
    '/api/v1/runs/history?limit=5&cursor=',
    '/api/v1/runs/history/abc123',
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
