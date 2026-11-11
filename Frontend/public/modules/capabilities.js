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
      const key = descriptor?.enabled_for_current_operation
        ? 'source.offline-enabled'
        : descriptor?.adapter_status === 'IMPLEMENTED'
          ? 'source.live-unverified'
          : descriptor?.approval_status === 'PENDING_APPROVAL'
            ? 'source.pending'
            : 'source.unknown';
      badge.textContent = translate(key);
      badge.classList.toggle('pill--green', Boolean(descriptor?.enabled_for_current_operation));
    }
  }
}
