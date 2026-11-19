import assert from 'node:assert/strict';
import test from 'node:test';

import {
  defaultFetchSourceIds,
  initialFetchDefaultSourceIds,
} from '../public/modules/capabilities.js';

const KEY = 'trendscout.fetch-default-sources.v1';
const caps = {
  sources: [
    { source_id: 'wikimedia_pageviews_top_per_country', enabled_for_current_operation: true },
    { source_id: 'google_web_serp', enabled_for_current_operation: true },
    { source_id: 'bing_web_serp', enabled_for_current_operation: false },
    { source_id: 'youtube_video_search', enabled_for_current_operation: true },
  ],
};

function withStorage(value, run) {
  const previous = globalThis.localStorage;
  const entries = new Map(value === null ? [] : [[KEY, value]]);
  globalThis.localStorage = {
    getItem: (key) => entries.get(key) ?? null,
    setItem: (key, item) => entries.set(key, item),
  };
  try { return run(); } finally { globalThis.localStorage = previous; }
}

test('no saved preference leaves server enabled-source defaults in control', () => {
  withStorage(null, () => assert.equal(defaultFetchSourceIds(caps), null));
});

test('Vercel Shared LIVE settings initially select all four approved sources', () => {
  const vercelCaps = {
    ...caps,
    sources: caps.sources.map((source) => ({ ...source, enabled_for_current_operation: true })),
    youtube_workflow: { approval_status: 'APPROVED_FOR_VERCEL_SHARED_RUNTIME' },
  };
  assert.deepEqual(initialFetchDefaultSourceIds(vercelCaps), [
    'wikimedia_pageviews_top_per_country',
    'google_web_serp',
    'bing_web_serp',
    'youtube_video_search',
  ]);
});

test('unapproved YouTube stays out of initial Fetch defaults', () => {
  assert.deepEqual(initialFetchDefaultSourceIds(caps), [
    'wikimedia_pageviews_top_per_country',
    'google_web_serp',
  ]);
});

test('saved defaults are restricted to sources the server currently enables', () => {
  withStorage(JSON.stringify(['bing_web_serp', 'google_web_serp']), () => {
    assert.deepEqual(defaultFetchSourceIds(caps), ['google_web_serp']);
  });
});

test('YouTube can be explicitly included in saved Fetch defaults', () => {
  withStorage(JSON.stringify(['youtube_video_search', 'google_web_serp']), () => {
    assert.deepEqual(defaultFetchSourceIds(caps), ['youtube_video_search', 'google_web_serp']);
  });
});

test('invalid saved data fails back to server defaults', () => {
  withStorage('{broken', () => assert.equal(defaultFetchSourceIds(caps), null));
});
