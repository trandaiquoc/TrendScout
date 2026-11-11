import { en } from './i18n/en.js';
import { vi } from './i18n/vi.js';

const catalogs = { en, vi };
const STORAGE_KEY = 'trendscout.locale';
let locale = 'vi';

export function t(key, values = {}) {
  const template = catalogs[locale][key] ?? catalogs.vi[key] ?? key;
  return template.replace(/\{([a-zA-Z0-9_]+)\}/g, (_, name) => String(values[name] ?? ''));
}

function storedLocale() {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value === 'en' || value === 'vi' ? value : 'vi';
  } catch {
    return 'vi';
  }
}

export function applyLocale(next = locale, { persist = true } = {}) {
  locale = next === 'en' ? 'en' : 'vi';
  document.documentElement.lang = locale;
  document.documentElement.dataset.locale = locale;
  document.title = locale === 'en' ? 'TrendScout — Insight workspace' : 'TrendScout — Không gian phân tích';
  document.querySelectorAll('[data-i18n]').forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach((node) => {
    node.setAttribute('placeholder', t(node.dataset.i18nPlaceholder));
  });
  document.querySelectorAll('[data-i18n-aria]').forEach((node) => {
    node.setAttribute('aria-label', t(node.dataset.i18nAria));
  });
  document.querySelectorAll('[data-locale-label]').forEach((node) => {
    node.textContent = locale.toUpperCase();
  });
  document.querySelectorAll('[data-locale-toggle]').forEach((button) => {
    button.setAttribute('aria-label', t('locale.switch'));
    button.setAttribute('aria-pressed', String(locale === 'en'));
  });
  if (persist) {
    try {
      localStorage.setItem(STORAGE_KEY, locale);
    } catch {}
  }
  document.dispatchEvent(new CustomEvent('trendscout:locale', { detail: { locale } }));
  return locale;
}

export function initializeLocale() {
  applyLocale(storedLocale(), { persist: false });
  document.querySelectorAll('[data-locale-toggle]').forEach((button) => {
    button.addEventListener('click', () => applyLocale(locale === 'en' ? 'vi' : 'en'));
  });
}

export function getLocale() {
  return locale;
}
