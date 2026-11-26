import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const appUrl = new URL('../public/app.js', import.meta.url);

test('initial dashboard uses one bootstrap read and does not preflight a separate dataset read', async () => {
  const app = await readFile(appUrl, 'utf8');
  const bootstrapStart = app.indexOf('async function loadDashboardBootstrap()');
  const bootstrapEnd = app.indexOf('\nfunction renderQualitySnapshot', bootstrapStart);
  assert.notEqual(bootstrapStart, -1);
  assert.notEqual(bootstrapEnd, -1);
  const bootstrap = app.slice(bootstrapStart, bootstrapEnd);
  assert.match(bootstrap, /getJson\('\/api\/v1\/dashboard\/bootstrap\?' \+ query/);
  assert.match(bootstrap, /loadAnalytics\(data\.analytics\)/);
  assert.match(bootstrap, /loadKeywords\(data\.keywords\)/);

  const viewLoaderStart = app.indexOf('function loadDashboardView()');
  const viewLoaderEnd = app.indexOf('\nfunction renderQualitySnapshot', viewLoaderStart);
  assert.notEqual(viewLoaderStart, -1);
  assert.notEqual(viewLoaderEnd, -1);
  const viewLoader = app.slice(viewLoaderStart, viewLoaderEnd);
  assert.match(viewLoader, /return loadDashboardBootstrap\(\)\.then\(\(data\) =>/);
  assert.match(viewLoader, /if \(!data \|\| !data\.item\)[\s\S]*startupCapabilities\?\.then\(\(\) => recoverLatestVercelCollectionSnapshot\(\)\)/);

  const showViewStart = app.indexOf('function showView(');
  const showViewEnd = app.indexOf('\nfunction setDashboardTab', showViewStart);
  assert.notEqual(showViewStart, -1);
  assert.notEqual(showViewEnd, -1);
  assert.match(app.slice(showViewStart, showViewEnd), /selected === 'overview' && loadDashboard\) loadDashboardView\(\)/);

  const startupStart = app.lastIndexOf('applyLastKnownCapabilities();');
  assert.notEqual(startupStart, -1);
  const startup = app.slice(startupStart);
  assert.match(startup, /startupCapabilities = loadPublicCapabilities\(\);/);
  assert.match(startup, /showView\(window\.location\.hash\.slice\(1\)\)/);
  assert.doesNotMatch(startup, /const startupDataset = loadLatestDataset\(\)/);
  assert.doesNotMatch(startup, /void loadAnalytics\(\)/);
  assert.doesNotMatch(startup, /if \(item\) void loadKeywords\(\)/);
});
