const REQUEST_TIMEOUT_MS = 15000;

export class ApiError extends Error {
  constructor(message, status = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
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

export async function getJson(path, { locale = 'vi', timeoutMs = REQUEST_TIMEOUT_MS } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal: controller.signal,
      credentials: 'same-origin',
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
      throw new ApiError(httpMessages[locale]?.[response.status] || (locale === 'en'
        ? 'The request could not be completed (HTTP ' + response.status + ').'
        : 'Không thể hoàn tất yêu cầu (HTTP ' + response.status + ').'), response.status);
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
