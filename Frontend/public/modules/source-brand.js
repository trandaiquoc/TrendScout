/** @typedef {'wikimedia_pageviews_top_per_country'|'google_web_serp'|'bing_web_serp'|'youtube_video_search'} RuntimeSourceId */

const BRANDS = Object.freeze({
  wikimedia_pageviews_top_per_country: { mark: 'W', label: 'source.name.wikimedia', className: 'wikimedia' },
  google_web_serp: { mark: 'G', label: 'source.name.google', className: 'google' },
  bing_web_serp: { mark: 'B', label: 'source.name.bing', className: 'bing' },
  youtube_video_search: {
    mark: 'Y', label: 'source.name.youtube', className: 'youtube', image: '/assets/youtube-logo.svg',
  },
});

/** Return a decorative, local source mark. Provider wordmarks are not recreated. */
export function createSourceMark(sourceId, { showExistingLogo = true } = {}) {
  const brand = BRANDS[sourceId];
  const mark = document.createElement('span');
  mark.className = `source-brand-mark source-brand-mark--${brand?.className || 'generic'}`;
  mark.setAttribute('aria-hidden', 'true');
  if (showExistingLogo && brand?.image) {
    const image = document.createElement('img');
    image.src = brand.image;
    image.alt = '';
    mark.append(image);
  } else {
    mark.textContent = brand?.mark || '•';
  }
  return mark;
}

export function sourceLabelKey(sourceId) {
  return BRANDS[sourceId]?.label || null;
}
