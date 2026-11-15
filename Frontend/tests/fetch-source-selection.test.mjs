import assert from 'node:assert/strict';
import test from 'node:test';

import { liveFetchMessageKey, PublicFetchController } from '../public/modules/fetch.js';

const COMPLETED_ID = 'a'.repeat(32);

async function activateWithSources(sourceIds) {
  const requests = [];
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path: String(path), ...options });
    if (path === '/api/v1/fetches') {
      return new Response(JSON.stringify({ fetch_id: COMPLETED_ID, data_mode: 'SYNTHETIC' }), {
        status: 202,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    if (path === '/api/v1/fetches/' + COMPLETED_ID) {
      return new Response(JSON.stringify({ status: 'COMPLETED', run_id: null }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    throw new Error('Unexpected Fetch controller request: ' + path);
  };

  try {
    const controller = new PublicFetchController({
      onState: () => {},
      onTerminal: () => {},
      locale: () => 'en',
      sourceIds: () => sourceIds,
    });
    await controller.activate();
    return requests;
  } finally {
    globalThis.fetch = previousFetch;
  }
}

test('posts the selected Google source for a Google-only Fetch', async () => {
  const requests = await activateWithSources(['google_web_serp']);

  assert.equal(requests[0].path, '/api/v1/fetches');
  assert.deepEqual(JSON.parse(requests[0].body), { sources: ['google_web_serp'] });
  assert.equal(requests[1].path, '/api/v1/fetches/' + COMPLETED_ID);
});

test('posts Google and Wikimedia together when both are selected', async () => {
  const sources = ['google_web_serp', 'wikimedia_pageviews_top_per_country'];
  const requests = await activateWithSources(sources);

  assert.deepEqual(JSON.parse(requests[0].body), { sources });
});

test('omits source selection when the UI leaves the default all-sources choice', async () => {
  const requests = await activateWithSources(null);

  assert.deepEqual(JSON.parse(requests[0].body), {});
});

test('uses source-neutral LIVE messages for Google and mixed-source fetches', () => {
  assert.equal(liveFetchMessageKey('failed', 'latest', ['google_web_serp']),
    'fetch.failed-local-live');
  assert.equal(liveFetchMessageKey('partial', 'latest', [
    'google_web_serp', 'wikimedia_pageviews_top_per_country',
  ]), 'fetch.partial-local-live');
});

test('preserves Wikimedia-only and Wikimedia-history LIVE messages', () => {
  assert.equal(liveFetchMessageKey('completed', 'latest', [
    'wikimedia_pageviews_top_per_country',
  ]), 'fetch.completed-live');
  assert.equal(liveFetchMessageKey('failed', 'history', ['google_web_serp']),
    'fetch.history-failed-live');
});
