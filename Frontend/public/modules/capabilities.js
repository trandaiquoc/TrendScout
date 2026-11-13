import { getJson } from './api.js';

const SOURCE_CARDS = new Map([
  ['wikimedia_pageviews_top_per_country', 'wikimedia'],
  ['youtube_video_search', 'youtube'],
  ['google_web_serp', 'google-search'],
  ['bing_web_serp', 'bing-search'],
]);

const STATUS_FIELDS = [
  ['sources.adapter', 'adapter_status'],
  ['sources.configuration', 'configuration_status'],
  ['sources.approval', 'approval_status'],
  ['sources.local-verification', 'local_live_verification_status'],
  ['sources.current-operation', 'enabled_for_current_operation'],
  ['sources.current-health', null],
  ['sources.production-readiness', 'production_readiness'],
];

export function loadCapabilities(locale = 'vi') {
  return getJson('/api/v1/capabilities', { locale });
}

export function isLocalLiveDemo(capabilities) {
  return capabilities?.data_mode === 'LIVE' && capabilities.local_live_demo === true
    && capabilities.environment_mode === 'local' && capabilities.read_only === false;
}

export function canStartFetch(capabilities) {
  return capabilities?.public_fetch_enabled === true && capabilities.fetch_enabled === true
    && capabilities.authentication_required === false
    && (capabilities.data_mode === 'SYNTHETIC' || isLocalLiveDemo(capabilities))
    && Array.isArray(capabilities.sources)
    && capabilities.sources.some((source) => source.enabled_for_current_operation === true);
}

export function acceptsDiscovery(capabilities, discovery) {
  return (capabilities?.data_mode === 'SYNTHETIC' && discovery?.data_mode === 'SYNTHETIC')
    || (isLocalLiveDemo(capabilities) && ['LIVE', 'MIXED'].includes(discovery?.data_mode));
}

function localizedStatus(value, translate) {
  if (value === true) return translate('common.enabled');
  if (value === false) return translate('common.disabled');
  if (typeof value !== 'string' || !value) return translate('source.status-unknown');
  return translate('source.status.' + value.toLowerCase());
}

function renderDescriptor(card, descriptor, translate) {
  const list = card.querySelector('[data-source-capabilities]');
  if (list) {
    list.replaceChildren();
    for (const [labelKey, field] of STATUS_FIELDS) {
      const row = document.createElement('div');
      const label = document.createElement('dt');
      const value = document.createElement('dd');
      label.textContent = translate(labelKey);
      value.textContent = field === null
        ? translate('source.health-unreported')
        : localizedStatus(descriptor?.[field], translate);
      row.append(label, value);
      list.append(row);
    }
  }
  const badge = card.querySelector('.source-card-head .pill');
  const enabled = descriptor?.enabled_for_current_operation === true;
  card.classList.toggle('source-card--active', enabled);
  if (badge) {
    const verified = descriptor?.local_live_verification_status === 'LOCAL_LIVE_VERIFIED';
    const key = enabled ? (verified ? 'source.enabled-local-verified' : 'source.enabled')
      : descriptor ? 'source.disabled' : 'source.status-unknown';
    badge.textContent = translate(key);
    badge.classList.toggle('pill--green', enabled);
    badge.classList.toggle('pill--gray', !enabled);
  }
}

export function renderSourceCapabilities(capabilities, translate) {
  const descriptors = new Map(
    (Array.isArray(capabilities?.sources) ? capabilities.sources : [])
      .filter((source) => SOURCE_CARDS.has(source.source_id))
      .map((source) => [source.source_id, source]),
  );

  for (const [sourceId, cardId] of SOURCE_CARDS) {
    const card = document.querySelector(`[data-source="${cardId}"]`);
    if (!card) continue;
    const descriptor = descriptors.get(sourceId);
    card.dataset.sourceStatus = descriptor
      ? `${descriptor.adapter_status || 'UNKNOWN'}-${descriptor.local_live_verification_status || 'UNKNOWN'}`.toLowerCase()
      : 'unknown';
    renderDescriptor(card, descriptor, translate);
  }

  const wikimedia = descriptors.get('wikimedia_pageviews_top_per_country');
  const activeCount = document.querySelector('#metric-active-sources');
  const activeNote = document.querySelector('#metric-active-sources-note');
  if (activeCount) {
    const count = Array.isArray(capabilities?.sources)
      ? capabilities.sources.filter((source) => source.enabled_for_current_operation === true).length
      : null;
    activeCount.textContent = count == null ? '—'
      : new Intl.NumberFormat(document.documentElement.lang || 'vi').format(count);
    if (activeNote) activeNote.textContent = translate('metric.active-sources-help');
  }
  const enabled = wikimedia?.enabled_for_current_operation === true;
  const sidebarStatus = document.querySelector('#sidebar-source-status');
  const sidebarDot = document.querySelector('#sidebar-source-dot');
  const mainStatus = document.querySelector('#main-source-status');
  if (wikimedia?.local_live_verification_status === 'LOCAL_LIVE_VERIFIED') {
    for (const badge of [sidebarStatus, mainStatus]) {
      if (badge) badge.textContent = translate(enabled ? 'source.enabled-local-verified' : 'source.historical-local-verified');
    }
  } else {
    for (const badge of [sidebarStatus, mainStatus]) {
      if (badge) badge.textContent = translate(enabled ? 'source.enabled' : 'source.disabled');
    }
  }
  if (sidebarDot) sidebarDot.className = `status-dot ${enabled ? 'status-dot--green' : 'status-dot--muted'}`;
  if (mainStatus) {
    mainStatus.classList.toggle('pill--green', enabled);
    mainStatus.classList.toggle('pill--gray', !enabled);
  }
}
