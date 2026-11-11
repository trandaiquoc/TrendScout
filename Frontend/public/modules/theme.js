import { t } from './i18n.js';

const STORAGE_KEY = 'trendscout.theme';
const VALID_THEMES = new Set(['light', 'dark']);

function storedTheme() {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return VALID_THEMES.has(value) ? value : 'light';
  } catch {
    return 'light';
  }
}

function syncThemeControls(theme) {
  document.querySelectorAll('[data-theme-label]').forEach((node) => {
    node.textContent = t('theme.' + theme);
  });
  document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
    button.setAttribute('aria-pressed', String(theme === 'dark'));
    button.setAttribute('aria-label', t('theme.toggle'));
  });
}

export function setTheme(theme, { persist = true } = {}) {
  const next = VALID_THEMES.has(theme) ? theme : 'light';
  document.documentElement.dataset.theme = next;
  syncThemeControls(next);
  if (persist) {
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {}
  }
  return next;
}

export function initializeTheme() {
  setTheme(storedTheme(), { persist: false });
  document.addEventListener('trendscout:locale', () => syncThemeControls(document.documentElement.dataset.theme));
  document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
    });
  });
}
