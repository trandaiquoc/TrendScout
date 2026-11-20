import assert from 'node:assert/strict';
import test from 'node:test';

import { getJson } from '../public/modules/api.js';

async function requestedTimeout(path, options = {}) {
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
    await getJson(path, options);
    return delays[0];
  } finally {
    globalThis.fetch = previous.fetch;
    globalThis.setTimeout = previous.setTimeout;
    globalThis.clearTimeout = previous.clearTimeout;
  }
}

test('Firestore-backed Collections reads and writes allow Vercel cold starts', async () => {
  for (const path of [
    '/api/v1/collections',
    '/api/v1/collections/collection-1',
    '/api/v1/datasets/latest',
    '/api/v1/datasets/latest/keywords?limit=50',
  ]) {
    assert.equal(await requestedTimeout(path), 65_000, path);
  }
});

test('serverless Fetch submissions and checkpoint persistence wait for the Vercel deadline', async () => {
  assert.equal(await requestedTimeout('/api/v1/fetches'), 65_000);
  assert.equal(await requestedTimeout(`/api/v1/fetches/${'a'.repeat(32)}/collections`), 65_000);
});

test('ordinary API calls keep the standard timeout and callers may override it', async () => {
  assert.equal(await requestedTimeout('/api/v1/capabilities'), 15_000);
  assert.equal(await requestedTimeout('/api/v1/collections', { timeoutMs: 7_000 }), 7_000);
});
