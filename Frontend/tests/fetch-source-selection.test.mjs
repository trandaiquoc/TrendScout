import assert from 'node:assert/strict';
import test from 'node:test';

import { isVercelSharedLive } from '../public/modules/capabilities.js';
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

test('uses Vercel runtime wording for shared LIVE Fetches', () => {
  assert.equal(liveFetchMessageKey('running', 'latest', [
    'google_web_serp', 'bing_web_serp',
  ], 'VERCEL_SHARED_LIVE'), 'fetch.running-vercel-live');
  assert.equal(liveFetchMessageKey('auto-resuming', 'latest', [
    'google_web_serp', 'bing_web_serp',
  ], 'VERCEL_SHARED_LIVE'), 'fetch.auto-resuming-vercel-live');
  assert.equal(liveFetchMessageKey('running', 'latest', [
    'wikimedia_pageviews_top_per_country',
  ], 'VERCEL_SHARED_LIVE'), 'fetch.running-live');
});

test('selects Vercel wording from the published runtime capability', () => {
  const capabilities = {
    data_mode: 'LIVE', vercel_shared_live: true,
    environment_mode: 'vercel', read_only: false,
  };
  assert.equal(isVercelSharedLive(capabilities), true);
  assert.equal(liveFetchMessageKey('running', 'latest', [
    'google_web_serp', 'bing_web_serp', 'youtube_video_search',
  ], isVercelSharedLive(capabilities) ? 'VERCEL_SHARED_LIVE' : 'LOCAL'),
  'fetch.running-vercel-live');
  assert.equal(isVercelSharedLive({ ...capabilities, environment_mode: 'local' }), false);
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

test('automatically resumes a recovered durable job with the same idempotent request', async () => {
  const previousFetch = globalThis.fetch;
  const previousWindow = globalThis.window;
  const requests = [];
  const states = [];
  let statusReads = 0;
  globalThis.window = {
    setTimeout(callback) { queueMicrotask(callback); return 1; },
    clearTimeout() {},
  };
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path: String(path), ...options });
    if (path === '/api/v1/fetches') {
      return new Response(JSON.stringify({ fetch_id: COMPLETED_ID, data_mode: 'LIVE' }), {
        status: 202, headers: { 'Content-Type': 'application/json' },
      });
    }
    statusReads += 1;
    const body = statusReads === 1
      ? { status: 'QUEUED', recovery_available: true, terminal: false }
      : { status: 'COMPLETED', recovery_available: false, terminal: true, run_id: null };
    return new Response(JSON.stringify(body), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const controller = new PublicFetchController({
      onState: (state) => states.push(state.kind),
      onTerminal: () => {},
      locale: () => 'en',
      sourceIds: () => ['google_web_serp'],
    });
    await controller.activate();
    const submissions = requests.filter((request) => request.path === '/api/v1/fetches');
    assert.equal(submissions.length, 2);
    assert.equal(submissions[0].headers['Idempotency-Key'], submissions[1].headers['Idempotency-Key']);
    assert.equal(submissions[0].body, submissions[1].body);
    assert.equal(states.includes('auto-resuming'), true);
    assert.equal(statusReads, 2);
    assert.equal(states.at(-1), 'completed');
  } finally {
    globalThis.fetch = previousFetch;
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test('automatically resumes a recovered job and reaches four final source states', async () => {
  const previousFetch = globalThis.fetch;
  const previousWindow = globalThis.window;
  const requests = [];
  const states = [];
  let statusReads = 0;
  globalThis.window = {
    setTimeout(callback) { queueMicrotask(callback); return 1; },
    clearTimeout() {},
  };
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path: String(path), ...options });
    if (path === '/api/v1/fetches') {
      return new Response(JSON.stringify({
        fetch_id: COMPLETED_ID, data_mode: 'LIVE', status: 'QUEUED',
      }), { status: 202, headers: { 'Content-Type': 'application/json' } });
    }
    statusReads += 1;
    const body = statusReads === 1
      ? { status: 'QUEUED', recovery_available: true, terminal: false }
      : {
        status: 'PARTIAL', recovery_available: false, terminal: true,
        sources: [
          { source_id: 'wikimedia_pageviews_top_per_country', status: 'SUCCESS', provider_request_count: 1 },
          { source_id: 'google_web_serp', status: 'FAILED', error_code: 'RESULT_PERSISTENCE_FAILED' },
          { source_id: 'bing_web_serp', status: 'UNAVAILABLE', error_code: 'NO_SAVED_CANDIDATE' },
          { source_id: 'youtube_video_search', status: 'FAILED', error_code: 'SOURCE_EXECUTION_FAILED' },
        ],
      };
    return new Response(JSON.stringify(body), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const controller = new PublicFetchController({
      onState: (state) => states.push(state.kind),
      onTerminal: () => {},
      locale: () => 'en',
      sourceIds: () => [
        'wikimedia_pageviews_top_per_country', 'google_web_serp',
        'bing_web_serp', 'youtube_video_search',
      ],
      youtubePolicyVersion: () => 'd6-youtube-v2',
    });
    await controller.activate();

    const submissions = requests.filter((request) => request.path === '/api/v1/fetches');
    assert.equal(submissions.length, 2);
    assert.equal(submissions[0].headers['Idempotency-Key'], submissions[1].headers['Idempotency-Key']);
    assert.equal(submissions[0].body, submissions[1].body);
    assert.equal(submissions[0].headers['X-YouTube-Policy-Version'], 'd6-youtube-v2');
    assert.ok(states.includes('auto-resuming'));
    assert.equal(states.at(-1), 'partial');
    assert.equal(statusReads, 2);
  } finally {
    globalThis.fetch = previousFetch;
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test('redispatches a stalled queued Vercel job with the same idempotency key', async () => {
  const previousFetch = globalThis.fetch;
  const previousWindow = globalThis.window;
  const requests = [];
  const states = [];
  let statusReads = 0;
  const createdAt = new Date(Date.now() - 10_000).toISOString();
  globalThis.window = {
    setTimeout(callback) { queueMicrotask(callback); return 1; },
    clearTimeout() {},
  };
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path: String(path), ...options });
    if (path === '/api/v1/fetches') {
      return new Response(JSON.stringify({
        fetch_id: COMPLETED_ID, data_mode: 'LIVE', status: 'QUEUED',
      }), { status: 202, headers: { 'Content-Type': 'application/json' } });
    }
    statusReads += 1;
    const body = statusReads === 1
      ? { status: 'QUEUED', created_at: createdAt, updated_at: createdAt, terminal: false }
      : { status: 'COMPLETED', terminal: true };
    return new Response(JSON.stringify(body), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const controller = new PublicFetchController({
      onState: (state) => states.push(state.kind),
      onTerminal: () => {},
      locale: () => 'en',
      sourceIds: () => ['wikimedia_pageviews_top_per_country', 'google_web_serp'],
    });
    await controller.activate();

    const posts = requests.filter((request) => request.path === '/api/v1/fetches');
    assert.equal(posts.length, 2);
    assert.equal(posts[0].headers['Idempotency-Key'], posts[1].headers['Idempotency-Key']);
    assert.equal(posts[0].body, posts[1].body);
    assert.equal(states.includes('auto-resuming'), true);
    assert.equal(statusReads, 2);
    assert.equal(states.at(-1), 'completed');
  } finally {
    globalThis.fetch = previousFetch;
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test('continues a deferred Fetch when its persisted retry time arrives', async () => {
  const previousFetch = globalThis.fetch;
  const previousWindow = globalThis.window;
  const requests = [];
  const states = [];
  let statusReads = 0;
  globalThis.window = {
    setTimeout(callback) { queueMicrotask(callback); return 1; },
    clearTimeout() {},
  };
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path: String(path), ...options });
    if (path === '/api/v1/fetches') {
      return new Response(JSON.stringify({ fetch_id: COMPLETED_ID, data_mode: 'LIVE' }), {
        status: 202, headers: { 'Content-Type': 'application/json' },
      });
    }
    statusReads += 1;
    const body = statusReads === 1
      ? { status: 'QUEUED', retry_at: new Date(Date.now() - 1).toISOString(), terminal: false }
      : { status: 'COMPLETED', terminal: true };
    return new Response(JSON.stringify(body), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const controller = new PublicFetchController({
      onState: (state) => states.push(state.kind),
      onTerminal: () => {},
      locale: () => 'en',
      sourceIds: () => ['wikimedia_pageviews_top_per_country'],
    });
    await controller.activate();

    const posts = requests.filter((request) => request.path === '/api/v1/fetches');
    assert.equal(posts.length, 2);
    assert.equal(posts[0].headers['Idempotency-Key'], posts[1].headers['Idempotency-Key']);
    assert.equal(states.includes('retry-wait'), true);
    assert.equal(statusReads, 2);
    assert.equal(states.at(-1), 'completed');
  } finally {
    globalThis.fetch = previousFetch;
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test('retries an uncertain Vercel submission with the same idempotency key', async () => {
  const previousFetch = globalThis.fetch;
  const previousWindow = globalThis.window;
  const requests = [];
  let submissions = 0;
  globalThis.window = {
    setTimeout(callback) { queueMicrotask(callback); return 1; },
    clearTimeout() {},
  };
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path: String(path), ...options });
    if (path === '/api/v1/fetches') {
      submissions += 1;
      if (submissions === 1) {
        return new Response(JSON.stringify({ detail: { code: 'FUNCTION_TIMEOUT' } }), {
          status: 504, headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ fetch_id: COMPLETED_ID, data_mode: 'LIVE' }), {
        status: 202, headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({ status: 'COMPLETED', terminal: true }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const controller = new PublicFetchController({
      onState: () => {}, onTerminal: () => {}, locale: () => 'en',
      sourceIds: () => ['google_web_serp'],
    });
    await controller.activate();
    const posts = requests.filter((request) => request.path === '/api/v1/fetches');
    assert.equal(posts.length, 2);
    assert.equal(posts[0].headers['Idempotency-Key'], posts[1].headers['Idempotency-Key']);
    assert.equal(posts[0].body, posts[1].body);
    assert.equal(requests.at(-1).path, '/api/v1/fetches/' + COMPLETED_ID);
  } finally {
    globalThis.fetch = previousFetch;
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test('Run History recovery resubmits the stored source scope after reload', async () => {
  const previousFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path: String(path), ...options });
    if (path === '/api/v1/fetches') {
      return new Response(JSON.stringify({ fetch_id: COMPLETED_ID, data_mode: 'LIVE' }), {
        status: 202, headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({
      status: 'QUEUED', recovery_available: true, terminal: false,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  };

  try {
    const controller = new PublicFetchController({
      onState: () => {}, onTerminal: () => {}, locale: () => 'en',
      sourceIds: () => [],
    });
    await controller.resumeAvailableJob({
      sources: ['google_web_serp', 'bing_web_search'], requestKind: 'LATEST',
    });
    const post = requests.find((request) => request.path === '/api/v1/fetches');
    assert.deepEqual(JSON.parse(post.body), { sources: ['google_web_serp', 'bing_web_search'] });
    assert.ok(post.headers['Idempotency-Key']);
    assert.equal(requests.filter((request) => request.path === '/api/v1/fetches').length, 1);
    assert.equal(requests.filter((request) => request.method === 'GET').length, 1);
  } finally {
    globalThis.fetch = previousFetch;
  }
});
