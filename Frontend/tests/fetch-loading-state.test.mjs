import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

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
  assert.match(vi, /'fetch\.capabilities-loading': 'Đang kiểm tra kết nối và trạng thái Fetch/);
  assert.match(en, /'fetch\.capabilities-loading': 'Checking the connection and Fetch availability/);
});
