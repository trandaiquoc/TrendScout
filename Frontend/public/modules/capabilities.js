import { getJson } from './api.js';

const SOURCE_CARDS = new Map([
  ['wikimedia_pageviews_top_per_country', 'wikimedia'],
  ['youtube_video_search', 'youtube'],
  ['google_web_serp', 'google-search'],
  ['bing_web_serp', 'bing-search'],
]);

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

export function renderSourceCapabilities(capabilities, translate) {
  const descriptors = new Map(
    (Array.isArray(capabilities?.sources) ? capabilities.sources : [])
      .filter((source) => SOURCE_CARDS.has(source.source_id))
      .map((source) => [source.source_id, source]),
  );

  for (const [sourceId, cardId] of SOURCE_CARDS) {
    const card = document.querySelector(`[data-source="${cardId}"]`);
    const descriptor = descriptors.get(sourceId);
    if (!card) {
      continue;
    }
    const status = descriptor
      ? String(descriptor.adapter_status || 'UNKNOWN').toLowerCase()
        + '-' + String(descriptor.live_verification_status || 'UNKNOWN').toLowerCase()
      : 'unknown';
    card.dataset.sourceStatus = status;
    const badge = card.querySelector('.source-card-head .pill');
    if (badge) {
      const available = canStartFetch(capabilities) && descriptor?.enabled_for_current_operation === true;
      const key = available
        ? (isLocalLiveDemo(capabilities) ? 'source.live-enabled' : 'source.offline-enabled')
        : descriptor?.adapter_status === 'IMPLEMENTED'
          ? 'source.live-unverified'
          : descriptor?.approval_status === 'PENDING_APPROVAL'
            ? 'source.pending'
            : 'source.unknown';
      badge.textContent = translate(key);
      badge.classList.toggle('pill--green', available);
    }
  }
}
