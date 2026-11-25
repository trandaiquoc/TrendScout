import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const appUrl = new URL('../public/app.js', import.meta.url);

test('initial dashboard restores overview and first keyword page in one request', async () => {
  const app = await readFile(appUrl, 'utf8');
  const bootstrapStart = app.indexOf('async function loadDashboardBootstrap()');
  const bootstrapEnd = app.indexOf('\nfunction renderQualitySnapshot', bootstrapStart);
  assert.notEqual(bootstrapStart, -1);
  assert.notEqual(bootstrapEnd, -1);
  const bootstrap = app.slice(bootstrapStart, bootstrapEnd);
  assert.match(bootstrap, /getJson\('\/api\/v1\/dashboard\/bootstrap\?' \+ query/);
  assert.match(bootstrap, /loadAnalytics\(data\.analytics\)/);
  assert.match(bootstrap, /loadKeywords\(data\.keywords\)/);

  const startupStart = app.indexOf('const startupCapabilities = loadPublicCapabilities();');
  assert.notEqual(startupStart, -1);
  const startup = app.slice(startupStart);
  assert.match(startup, /if \(item\) void loadDashboardBootstrap\(\)/);
  assert.doesNotMatch(startup, /void loadAnalytics\(\)/);
  assert.doesNotMatch(startup, /if \(item\) void loadKeywords\(\)/);
});
