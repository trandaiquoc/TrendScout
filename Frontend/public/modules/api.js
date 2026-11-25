const REQUEST_TIMEOUT_MS = 15000;
const COLLECTIONS_TIMEOUT_MS = 65000;
const FETCH_TIMEOUT_MS = 55000;
const PAGE_READ_CACHE_MS = 3000;
const inFlightGets = new Map();
const pageReadCache = new Map();
let readCacheGeneration = 0;

function pageReadCacheable(path) {
  if (typeof path !== 'string') return false;
  const pathname = path.split('?', 1)[0];
  // Job status is polled while Collections work is running. Keep only its
  // in-flight dedupe; a short response cache would delay visible progress.
  if (pathname.startsWith('/api/v1/collections/jobs/')) return false;
  return pathname === '/api/v1/collections'
    || pathname.startsWith('/api/v1/collections/')
    || pathname === '/api/v1/datasets/latest'
    || pathname.startsWith('/api/v1/datasets/latest/keywords')
    || pathname === '/api/v1/analytics/overview'
    || pathname === '/api/v1/algorithms'
    || pathname === '/api/v1/algorithms/results'
    || pathname === '/api/v1/sources/library'
    || pathname === '/api/v1/research';
}

function readRequestKey(path, locale, timeoutMs, headers) {
  const normalizedHeaders = Object.entries(headers || {})
    .map(([key, value]) => [key.toLowerCase(), String(value)])
    .sort(([left], [right]) => left.localeCompare(right));
  return JSON.stringify([path, locale, timeoutMs, normalizedHeaders]);
}

function cloneJson(value) {
  return JSON.parse(JSON.stringify(value));
}

function invalidatePageReadCache() {
  readCacheGeneration += 1;
  pageReadCache.clear();
  inFlightGets.clear();
}

function requestTimeoutMs(path, requestedTimeoutMs) {
  if (Number.isFinite(requestedTimeoutMs)) return requestedTimeoutMs;
  if (path === '/api/v1/fetches') {
    return FETCH_TIMEOUT_MS;
  }
  if (/^\/api\/v1\/fetches\/[a-f0-9]{32}\/collections(?:\?|$)/.test(path)) {
    return COLLECTIONS_TIMEOUT_MS;
  }
  if (/^\/api\/v1\/fetches\/[a-f0-9]{32}(?:\?|$)/.test(path)) {
    return FETCH_TIMEOUT_MS;
  }
  if (path === '/api/v1/collections' || path.startsWith('/api/v1/collections/')
      || path === '/api/v1/datasets/latest'
      || path.startsWith('/api/v1/datasets/latest/')
      || path === '/api/v1/analytics/overview'
      || path.startsWith('/api/v1/runs/history')
      || path === '/api/health'
      || path === '/api/v1/capabilities') {
    return COLLECTIONS_TIMEOUT_MS;
  }
  return REQUEST_TIMEOUT_MS;
}

export class ApiError extends Error {
  constructor(message, status = null, body = null, retryAfterSeconds = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

const httpMessages = {
  vi: {
    401: 'Bạn không có quyền đọc dữ liệu này.',
    403: 'Quyền truy cập đã bị từ chối.',
    404: 'Không tìm thấy run hoặc keyword.',
    409: 'Run đang chạy hoặc kết quả chưa sẵn sàng.',
    429: 'Máy chủ đang giới hạn yêu cầu. Vui lòng thử lại sau.',
    503: 'Backend hoặc storage chưa sẵn sàng.',
  },
  en: {
    401: 'You are not authorized to read this data.',
    403: 'Access was denied.',
    404: 'Run or keyword was not found.',
    409: 'The run is still processing or its result is not ready.',
    429: 'The server is rate limiting requests. Try again later.',
    503: 'The backend or storage is not ready.',
  },
};

async function sendRequestJson(path, {
  method = 'GET',
  body: requestBody,
  headers = {},
  locale = 'vi',
  timeoutMs = null,
} = {}) {
  if (typeof path !== 'string' || !path.startsWith('/api/')) {
    throw new ApiError(locale === 'en' ? 'Only same-origin API requests are allowed.' : 'Chỉ cho phép yêu cầu API cùng nguồn gốc.');
  }
  if (!['GET', 'POST', 'PATCH', 'DELETE'].includes(method)) {
    throw new ApiError(locale === 'en' ? 'This request method is not allowed.' : 'Phương thức yêu cầu này không được phép.');
  }
  const effectiveTimeoutMs = requestTimeoutMs(path, timeoutMs);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), effectiveTimeoutMs);
  try {
    const response = await fetch(path, {
      method,
      headers: {
        Accept: 'application/json',
        ...(requestBody === undefined ? {} : { 'Content-Type': 'application/json' }),
        ...headers,
      },
      ...(requestBody === undefined ? {} : { body: JSON.stringify(requestBody) }),
      signal: controller.signal,
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'error',
    });
    let body;
    try {
      body = await response.json();
    } catch {
      const message = controller.signal.aborted
        ? (locale === 'en' ? 'The request timed out.' : 'Yêu cầu đã quá thời gian chờ.')
        : (locale === 'en' ? 'The backend returned an invalid response.' : 'Backend trả về dữ liệu không hợp lệ.');
      throw new ApiError(message, response.status);
    }
    if (!response.ok) {
      const code = body?.detail?.code || body?.code;
      const knownMessage = {
        COLLECTION_ALREADY_EXISTS: locale === 'en'
          ? 'A Collection with this name already exists.' : 'Đã có Bộ sưu tập trùng tên.',
        COLLECTION_MERGE_SAME_TARGET: locale === 'en'
          ? 'Choose a different target Collection.' : 'Hãy chọn Bộ sưu tập đích khác.',
        TAXONOMY_COLLECTION_MERGE_FORBIDDEN: locale === 'en'
          ? 'Built-in topic Collections cannot be merged.' : 'Không thể gộp các Bộ sưu tập chủ đề hệ thống.',
      }[code];
      throw new ApiError(knownMessage || httpMessages[locale]?.[response.status] || (locale === 'en'
        ? 'The request could not be completed (HTTP ' + response.status + ').'
        : 'Không thể hoàn tất yêu cầu (HTTP ' + response.status + ').'),
      response.status, body, Number(response.headers.get('retry-after')) || null);
    }
    return body;
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    const message = controller.signal.aborted
      ? (locale === 'en' ? 'The request timed out.' : 'Yêu cầu đã quá thời gian chờ.')
      : (locale === 'en' ? 'Could not connect to the backend.' : 'Không kết nối được backend.');
    throw new ApiError(message, null);
  } finally {
    clearTimeout(timer);
  }
}

export async function requestJson(path, options = {}) {
  const method = options.method || 'GET';
  if (method !== 'GET') invalidatePageReadCache();
  if (method !== 'GET' || typeof path !== 'string' || !path.startsWith('/api/')) {
    return sendRequestJson(path, options);
  }

  const locale = options.locale || 'vi';
  const timeoutMs = requestTimeoutMs(path, options.timeoutMs);
  const key = readRequestKey(path, locale, timeoutMs, options.headers);
  const cacheable = pageReadCacheable(path);
  if (cacheable) {
    const cached = pageReadCache.get(key);
    if (cached && cached.expiresAt > Date.now()) return cloneJson(cached.value);
    if (cached) pageReadCache.delete(key);
  }
  const inFlight = inFlightGets.get(key);
  if (inFlight) return cloneJson(await inFlight);

  const generation = readCacheGeneration;
  const operation = sendRequestJson(path, options).then((value) => {
    if (cacheable && generation === readCacheGeneration) {
      pageReadCache.set(key, {
        value: cloneJson(value),
        expiresAt: Date.now() + PAGE_READ_CACHE_MS,
      });
    }
    if (/^\/api\/v1\/fetches\/[a-f0-9]{32}$/.test(path)
        && value?.terminal === true) {
      invalidatePageReadCache();
    }
    return value;
  });
  inFlightGets.set(key, operation);
  try {
    return cloneJson(await operation);
  } finally {
    if (inFlightGets.get(key) === operation) inFlightGets.delete(key);
  }
}

export async function getJson(path, options = {}) {
  return requestJson(path, { ...options, method: 'GET' });
}
