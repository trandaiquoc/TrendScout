import test from 'node:test';
import assert from 'node:assert/strict';
import { pathForView, resolveLocation } from '../public/modules/router.js';

test('clean routes resolve to a page and active navigation destination', () => {
  assert.deepEqual(resolveLocation({ pathname: '/' }), {
    page: 'overview', nav: 'overview', tab: 'overview', pathname: '/', search: '', replace: false,
  });
  assert.equal(resolveLocation({ pathname: '/trends' }).tab, 'analysis');
  assert.equal(resolveLocation({ pathname: '/keywords' }).focus, 'discover-results-heading');
  assert.equal(resolveLocation({ pathname: '/collections' }).mode, 'collections');
  assert.equal(resolveLocation({ pathname: '/sources/google_web_serp' }).itemId, 'google_web_serp');
});

test('legacy hashes migrate to clean paths while preserving query parameters', () => {
  const route = resolveLocation({ pathname: '/', search: '?q=coffee', hash: '#keywords' });
  assert.equal(route.pathname, '/keywords');
  assert.equal(route.search, '?q=coffee');
  assert.equal(route.replace, true);
  assert.equal(resolveLocation({ pathname: '/', hash: '#analysis' }).pathname, '/trends');
  assert.equal(resolveLocation({ pathname: '/', hash: '#results' }).pathname, '/keywords');
});

test('unknown paths fall back to overview without dropping the query', () => {
  const route = resolveLocation({ pathname: '/not-a-view', search: '?source=google_web_serp' });
  assert.equal(route.pathname, '/');
  assert.equal(route.search, '?source=google_web_serp');
  assert.equal(route.invalid, true);
});

test('route names have no dashboard prefix', () => {
  for (const name of ['overview', 'keywords', 'collections', 'products', 'research', 'trends', 'sources', 'algorithms', 'runs', 'quality', 'settings']) {
    assert.equal(pathForView(name).includes('dashboard'), false);
  }
});

test('malformed encoded item routes fall back without throwing', () => {
  const route = resolveLocation({ pathname: '/sources/%E0%A4%A' });
  assert.equal(route.page, 'overview');
  assert.equal(route.invalid, true);
});
