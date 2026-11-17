import assert from 'node:assert/strict';
import test from 'node:test';

import { liveFetchMessageKey, PublicFetchController } from '../public/modules/fetch.js';

const COMPLETED_ID = 'a'.repeat(32);

async function activateWithSources(sourceIds, { youtubePolicyVersion = null } = {}) {
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
      youtubePolicyVersion: () => youtubePolicyVersion,
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

test('posts only Bing for a Bing-only Fetch', async () => {
  const requests = await activateWithSources(['bing_web_serp']);

  assert.equal(requests[0].path, '/api/v1/fetches');
  assert.deepEqual(JSON.parse(requests[0].body), { sources: ['bing_web_serp'] });
  assert.equal(requests[1].path, '/api/v1/fetches/' + COMPLETED_ID);
});

test('sends the accepted YouTube policy version for submit and status polling', async () => {
  const requests = [];
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path: String(path), ...options });
    if (path === '/api/v1/fetches') {
      return new Response(JSON.stringify({ fetch_id: COMPLETED_ID, data_mode: 'LIVE' }), {
        status: 202, headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({ status: 'COMPLETED', run_id: null }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const controller = new PublicFetchController({
      onState: () => {}, onTerminal: () => {}, locale: () => 'en',
      sourceIds: () => ['youtube_video_search'],
      youtubePolicyVersion: () => 'd6-youtube-v1',
    });
    await controller.activate();

    assert.equal(requests[0].headers['X-YouTube-Policy-Version'], 'd6-youtube-v1');
    assert.equal(requests[1].headers['X-YouTube-Policy-Version'], 'd6-youtube-v1');
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test('does not submit a YouTube Fetch without an accepted policy version', async () => {
  const previousFetch = globalThis.fetch;
  const requests = [];
  const kinds = [];
  globalThis.fetch = async (path) => {
    requests.push(String(path));
    throw new Error('A YouTube request must not be sent without consent.');
  };

  try {
    const controller = new PublicFetchController({
      onState: (state) => kinds.push(state.kind), onTerminal: () => {},
      locale: () => 'en', sourceIds: () => ['youtube_video_search'],
    });
    await controller.activate();

    assert.deepEqual(requests, []);
    assert.equal(kinds.at(-1), 'policy-consent-required');
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test('posts Google and Bing together when both search sources are selected', async () => {
  const sources = ['google_web_serp', 'bing_web_serp'];
  const requests = await activateWithSources(sources);

  assert.deepEqual(JSON.parse(requests[0].body), { sources });
});

test('posts Google and Wikimedia together when both are selected', async () => {
  const sources = ['google_web_serp', 'wikimedia_pageviews_top_per_country'];
  const requests = await activateWithSources(sources);

  assert.deepEqual(JSON.parse(requests[0].body), { sources });
});

test('submits all four selected sources with current YouTube policy consent', async () => {
  const sources = [
    'wikimedia_pageviews_top_per_country',
    'google_web_serp',
    'bing_web_serp',
    'youtube_video_search',
  ];
  const requests = await activateWithSources(sources, {
    youtubePolicyVersion: 'd6-youtube-v2',
  });

  assert.deepEqual(JSON.parse(requests[0].body), { sources });
  assert.equal(requests[0].headers['X-YouTube-Policy-Version'], 'd6-youtube-v2');
  assert.equal(requests[1].headers['X-YouTube-Policy-Version'], 'd6-youtube-v2');
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
  assert.equal(liveFetchMessageKey('completed', 'latest', ['bing_web_serp']),
    'fetch.completed-local-live');
});

test('preserves Wikimedia-only and Wikimedia-history LIVE messages', () => {
  assert.equal(liveFetchMessageKey('completed', 'latest', [
    'wikimedia_pageviews_top_per_country',
  ]), 'fetch.completed-live');
  assert.equal(liveFetchMessageKey('failed', 'history', ['google_web_serp']),
    'fetch.history-failed-live');
});

test('reports a status-poll allowance separately from provider rate limits', async () => {
  const previousFetch = globalThis.fetch;
  const kinds = [];
  globalThis.fetch = async (path) => {
    if (path === '/api/v1/fetches') {
      return new Response(JSON.stringify({ fetch_id: COMPLETED_ID, data_mode: 'LIVE' }), {
        status: 202, headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({ detail: { code: 'STATUS_POLL_LIMIT' } }), {
      status: 429, headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const controller = new PublicFetchController({
      onState: (state) => kinds.push(state.kind),
      onTerminal: () => {},
      locale: () => 'en',
      sourceIds: () => ['google_web_serp'],
    });
    await controller.activate();
    assert.equal(kinds.at(-1), 'poll-limit');
    assert.equal(kinds.includes('rate-limited'), false);
  } finally {
    globalThis.fetch = previousFetch;
  }
});
