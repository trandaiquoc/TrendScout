const REQUEST_TIMEOUT_MS = 15000;

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

export async function requestJson(path, {
  method = 'GET',
  body: requestBody,
  headers = {},
  locale = 'vi',
  timeoutMs = REQUEST_TIMEOUT_MS,
} = {}) {
  if (typeof path !== 'string' || !path.startsWith('/api/')) {
    throw new ApiError(locale === 'en' ? 'Only same-origin API requests are allowed.' : 'Chỉ cho phép yêu cầu API cùng nguồn gốc.');
  }
  if (!['GET', 'POST', 'PATCH', 'DELETE'].includes(method)) {
    throw new ApiError(locale === 'en' ? 'This request method is not allowed.' : 'Phương thức yêu cầu này không được phép.');
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
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

export async function getJson(path, options = {}) {
  return requestJson(path, { ...options, method: 'GET' });
}
