export const ROUTE_PATHS = Object.freeze({
  overview: '/',
  keywords: '/keywords',
  collections: '/collections',
  products: '/products',
  research: '/research',
  trends: '/trends',
  sources: '/sources',
  algorithms: '/algorithms',
  runs: '/runs',
  quality: '/quality',
  settings: '/settings',
});

const ROUTES = new Map([
  ['/', { page: 'overview', nav: 'overview', tab: 'overview' }],
  ['/keywords', { page: 'overview', nav: 'keywords', tab: 'overview', focus: 'discover-results-heading' }],
  ['/collections', { page: 'collections', nav: 'collections', mode: 'collections' }],
  ['/products', { page: 'products', nav: 'products' }],
  ['/research', { page: 'research', nav: 'research' }],
  ['/trends', { page: 'overview', nav: 'trends', tab: 'analysis' }],
  ['/sources', { page: 'sources', nav: 'sources' }],
  ['/algorithms', { page: 'algorithms', nav: 'algorithms' }],
  ['/runs', { page: 'runs', nav: 'runs' }],
  ['/quality', { page: 'quality', nav: 'quality' }],
  ['/settings', { page: 'settings', nav: 'settings' }],
]);

const LEGACY_HASHES = new Map([
  ['overview', '/'],
  ['analysis', '/trends'],
  ['keywords', '/keywords'],
  ['results', '/keywords'],
  ['collections', '/collections'],
  ['products', '/products'],
  ['research', '/research'],
  ['trends', '/trends'],
  ['sources', '/sources'],
  ['algorithms', '/algorithms'],
  ['runs', '/runs'],
  ['quality', '/quality'],
  ['settings', '/settings'],
]);

function normalizedPath(pathname) {
  if (typeof pathname !== 'string' || !pathname.startsWith('/')) return null;
  if (pathname === '/') return '/';
  return pathname.replace(/\/+$/, '') || '/';
}

function matchRoute(pathname) {
  const route = ROUTES.get(pathname);
  if (route) return { ...route, pathname };

  const [base, ...segments] = pathname.split('/').filter(Boolean);
  if (segments.length === 1 && ['sources', 'collections'].includes(base)) {
    try {
      return { ...ROUTES.get(`/${base}`), pathname, itemId: decodeURIComponent(segments[0]) };
    } catch {
      return null;
    }
  }
  return null;
}

export function resolveLocation({ pathname = '/', search = '', hash = '' } = {}) {
  const normalized = normalizedPath(pathname);
  if (normalized === null) {
    return { ...ROUTES.get('/'), pathname: '/', search: '', replace: true };
  }

  if (hash) {
    const legacyPath = LEGACY_HASHES.get(hash.replace(/^#/, '').split(/[?&]/, 1)[0]);
    if (legacyPath) {
      const route = matchRoute(legacyPath);
      return { ...route, pathname: legacyPath, search, replace: true, legacy: true };
    }
    return { ...ROUTES.get('/'), pathname: '/', search, replace: true, legacy: true };
  }

  const route = matchRoute(normalized);
  if (route) return { ...route, search, replace: normalized !== pathname };
  return { ...ROUTES.get('/'), pathname: '/', search, replace: true, invalid: true };
}

export function pathForView(view) {
  return ROUTE_PATHS[view] || ROUTE_PATHS.overview;
}
