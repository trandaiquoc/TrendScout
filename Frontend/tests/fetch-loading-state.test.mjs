import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import {
  capabilityRetryDelayMs,
  isRetryableCapabilityCheckError,
  loadCapabilities,
} from '../public/modules/capabilities.js';

const source = new URL('../public/', import.meta.url);

test('initial Fetch card reports a capability check instead of a policy block', async () => {
  const [html, app, vi, en] = await Promise.all([
    readFile(new URL('index.html', source), 'utf8'),
    readFile(new URL('app.js', source), 'utf8'),
    readFile(new URL('modules/i18n/vi.js', source), 'utf8'),
    readFile(new URL('modules/i18n/en.js', source), 'utf8'),
  ]);

  assert.match(html, /id="fetch-badge"[^>]*data-i18n="fetch\.checking"/);
  assert.match(html, /id="fetch-readiness"[^>]*data-i18n="fetch\.capabilities-loading"/);
  assert.match(app, /fetchCapabilitiesLoading: true/);
  assert.match(app, /capabilityCheckPending \? t\('fetch\.capabilities-loading'\)/);
  assert.match(app, /isRetryableCapabilityCheckError\(error\)/);
  assert.match(app, /scheduleCapabilityRefresh\(retryDelay\)/);
  assert.match(app, /capabilityRetryPending \? t\('fetch\.capabilities-retrying'\)/);
  assert.match(vi, /'fetch\.capabilities-loading': 'Đang kiểm tra kết nối và trạng thái Fetch/);
  assert.match(en, /'fetch\.capabilities-loading': 'Checking the connection and Fetch availability/);
  assert.match(vi, /'fetch\.capabilities-retrying': 'Kết nối đang chậm/);
  assert.match(en, /'fetch\.capabilities-retrying': 'The connection is taking longer/);
});

test('capability status retries transient failures with a finite backoff', () => {
  assert.deepEqual([0, 1, 2, 3].map(capabilityRetryDelayMs), [2_000, 5_000, 10_000, null]);
  assert.equal(capabilityRetryDelayMs(-1), null);
  assert.equal(capabilityRetryDelayMs(1.5), null);
  assert.equal(isRetryableCapabilityCheckError({ status: null }), true);
  assert.equal(isRetryableCapabilityCheckError({ status: 503 }), true);
  assert.equal(isRetryableCapabilityCheckError({ status: 429 }), true);
  assert.equal(isRetryableCapabilityCheckError({ status: 401 }), false);
  assert.equal(isRetryableCapabilityCheckError({ status: 404 }), false);
});

test('capability GET allows a Vercel cold start longer than ordinary API calls', async () => {
  const previous = {
    fetch: globalThis.fetch,
    setTimeout: globalThis.setTimeout,
    clearTimeout: globalThis.clearTimeout,
  };
  let timeoutMs = null;
  let request = null;
  globalThis.fetch = async (url, options) => {
    request = { url, method: options.method };
    return { ok: true, status: 200, json: async () => ({}) };
  };
  globalThis.setTimeout = (_callback, delay) => { timeoutMs = delay; return 1; };
  globalThis.clearTimeout = () => {};
  try {
    await loadCapabilities();
    assert.deepEqual(request, { url: '/api/v1/capabilities', method: 'GET' });
    assert.equal(timeoutMs, 25_000);
  } finally {
    globalThis.fetch = previous.fetch;
    globalThis.setTimeout = previous.setTimeout;
    globalThis.clearTimeout = previous.clearTimeout;
  }
});
