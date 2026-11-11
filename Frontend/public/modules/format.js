export function formatNumber(value, locale) {
  return typeof value === 'number' && Number.isFinite(value)
    ? new Intl.NumberFormat(locale === 'en' ? 'en-US' : 'vi-VN', { maximumFractionDigits: 2 }).format(value)
    : '—';
}

export function formatDate(value, locale) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return '—';
  }
  const date = new Date(value + 'T00:00:00Z');
  return Number.isNaN(date.getTime())
    ? '—'
    : new Intl.DateTimeFormat(locale === 'en' ? 'en-US' : 'vi-VN', { dateStyle: 'medium', timeZone: 'UTC' }).format(date);
}

export function formatTimestamp(value, locale) {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) {
    return '—';
  }
  return new Intl.DateTimeFormat(locale === 'en' ? 'en-US' : 'vi-VN', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date);
}
