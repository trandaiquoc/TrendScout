import { getJson, requestJson } from './api.js';
import { formatNumber, formatTimestamp } from './format.js';
import { getLocale, t } from './i18n.js';

const $ = (selector, root = document) => root.querySelector(selector);
const make = (tag, value = '', className = '') => {
  const node = document.createElement(tag);
  if (value !== '') node.textContent = String(value);
  if (className) node.className = className;
  return node;
};
const label = (key, fallback = key) => t(key) === key ? fallback : t(key);
const number = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value))
  ? formatNumber(Number(value), getLocale()) : label('common.unavailable', 'Unavailable');

let capabilities = null;
let capabilitiesLoadedAt = 0;
let capabilitySequence = 0;
let searchSequence = 0;
let selectionHydrationSequence = 0;
let selectedKeywords = new Map();
let requestKey = null;
let activeRecommendationJob = null;

function setStatus(text, error = false) {
  const node = $('#ai-product-status');
  if (!node) return;
  node.textContent = text;
  node.classList.toggle('notice--error', error);
  node.classList.toggle('notice--info', !error);
}

function localeSource(sourceId) {
  const key = ({
    wikimedia_pageviews_top_per_country: 'source.name.wikimedia',
    google_web_serp: 'source.name.google',
    bing_web_serp: 'source.name.bing',
    youtube_video_search: 'source.name.youtube',
    wikimedia_entity_context: 'source.name.wikimedia-content',
  })[sourceId];
  return key && t(key) !== key ? t(key) : sourceId || label('common.unavailable', 'Unavailable');
}

function renderCapabilityPanel() {
  const root = $('#ai-capabilities');
  if (!root) return;
  root.replaceChildren();
  if (!capabilities) {
    root.append(make('div', label('ai.settings.unavailable', 'AI capability status is unavailable.')));
    return;
  }
  const models = capabilities.models || {};
  const quota = capabilities.quota || {};
  const applicationBudget = capabilities.application_budget || {};
  const verification = capabilities.metadata_verification || {};
  const policy = capabilities.source_policy || {};
  const status = capabilities.enabled === true
    ? label('ai.settings.enabled', 'Enabled') : label('ai.settings.disabled', 'Disabled or unavailable');
  const rows = [
    [label('ai.settings.status', 'AI status'), status],
    [label('ai.settings.classification-model', 'Classification model'), models.classification || label('common.unavailable', 'Unavailable')],
    [label('ai.settings.recommendation-model', 'Recommendation model'), models.recommendation || label('common.unavailable', 'Unavailable')],
    [label('ai.settings.provider', 'Provider availability'), `${verification.classification?.status || 'NOT_CHECKED'} / ${verification.recommendation?.status || 'NOT_CHECKED'}`],
    [label('ai.settings.shared-quota', 'Provider quota usage'), `${number(quota.daily_used)} / ${number(quota.daily_limit)} daily · ${number(quota.minute_used)} / ${number(quota.minute_limit)} per minute`],
    [label('ai.settings.application-budget', 'Application request budget'), `${number(applicationBudget.daily_used)} / ${number(applicationBudget.daily_limit)} daily · ${number(applicationBudget.minute_used)} / ${number(applicationBudget.minute_limit)} per minute`],
    [label('ai.settings.admission', 'Admission mode'), capabilities.admission_mode === 'BOUNDED_PROVIDER_QUOTA_UNKNOWN' ? label('ai.settings.admission-bounded', 'Bounded internal budget; provider quota unknown') : capabilities.admission_mode === 'PROVIDER_QUOTA_VERIFIED' ? label('ai.settings.admission-verified', 'Provider quota verified') : label('ai.settings.admission-blocked', 'Blocked')],
    [label('ai.settings.provider-quota', 'Provider quota status'), capabilities.provider_quota_status || 'UNKNOWN'],
    [label('ai.settings.cooldown', 'Provider cooldown / retry'), capabilities.provider_retry_at ? formatTimestamp(capabilities.provider_retry_at, getLocale()) : label('common.unavailable', 'Unavailable')],
    [label('ai.settings.eligible-sources', 'AI-eligible sources'), Array.isArray(policy.eligible_sources) ? policy.eligible_sources.map(localeSource).join(', ') || label('common.none', 'None') : label('common.unavailable', 'Unavailable')],
    [label('ai.settings.restricted-sources', 'AI-restricted sources'), Array.isArray(policy.restricted_sources) ? policy.restricted_sources.map(localeSource).join(', ') || label('common.none', 'None') : label('common.unavailable', 'Unavailable')],
  ];
  for (const [term, value] of rows) {
    const row = make('div'); row.append(make('dt', term), make('dd', value)); root.append(row);
  }
  if (capabilities.error_code) {
    const row = make('div'); row.append(make('dt', label('ai.settings.error', 'Availability detail')),
      make('dd', label(`ai.error-code.${capabilities.error_code}`, capabilities.error_code)));
    root.append(row);
  }
  const badge = $('#settings-llm-state');
  if (badge) {
    badge.textContent = status;
    badge.classList.toggle('pill--green', capabilities.enabled === true);
    badge.classList.toggle('pill--gray', capabilities.enabled !== true);
  }
}

async function loadCapabilities(force = false) {
  if (!force && Date.now() - capabilitiesLoadedAt < 30_000 && capabilities) return capabilities;
  const sequence = ++capabilitySequence;
  try {
    const result = await getJson('/api/v1/ai/capabilities', { locale: getLocale() });
    if (sequence !== capabilitySequence) return capabilities;
    capabilities = result;
    capabilitiesLoadedAt = Date.now();
  } catch {
    if (sequence === capabilitySequence) capabilities = null;
  }
  renderCapabilityPanel();
  return capabilities;
}

function navigateToProducts(ids) {
  const unique = [...new Set(ids)].filter((id) => typeof id === 'string' && id.length > 0 && id.length <= 64);
  if (!unique.length || unique.length > 20) {
    setStatus(t('ai.products.scope-limit', { max: 20 }), true);
    return;
  }
  const url = new URL('/products', window.location.origin);
  unique.forEach((id) => url.searchParams.append('keyword_id', id));
  window.history.pushState(null, '', `${url.pathname}${url.search}`);
  window.dispatchEvent(new PopStateEvent('popstate'));
}

function renderSelectedKeywords() {
  const list = $('#ai-product-selected-list');
  const count = $('#ai-product-selection-count');
  if (!list || !count) return;
  list.replaceChildren();
  count.textContent = t('ai.products.selected-count', { count: number(selectedKeywords.size), max: 20 });
  for (const [id, keyword] of selectedKeywords) {
    const item = make('li', '', 'ai-product-selected-item');
    const title = make('span', keyword.name || id);
    const remove = make('button', label('ai.products.remove-keyword', 'Remove keyword'), 'text-button');
    remove.type = 'button';
    remove.setAttribute('aria-label', t('ai.products.remove-keyword-named', { name: keyword.name || id }));
    remove.addEventListener('click', () => {
      selectedKeywords.delete(id); requestKey = null; activeRecommendationJob = null;
      renderSelectedKeywords(); renderSearchResults([]);
    });
    item.append(title, remove);
    if (keyword.source) item.append(make('small', localeSource(keyword.source)));
    list.append(item);
  }
  const generate = $('#ai-product-generate');
  if (generate) generate.disabled = selectedKeywords.size < 1 || selectedKeywords.size > 20;
}

function renderSearchResults(items) {
  const root = $('#ai-product-search-results');
  if (!root) return;
  root.replaceChildren();
  if (!items.length) {
    root.append(make('p', label('ai.products.no-search-results', 'Search saved keywords to select product signals.')));
    return;
  }
  const list = make('ul');
  for (const item of items) {
    const id = item.id || item.keyword_id;
    if (typeof id !== 'string' || !id) continue;
    const name = item.display_keyword || item.keyword || item.canonical_keyword || id;
    const row = make('li');
    const choice = make('label', '', 'ai-product-search-choice');
    const input = make('input'); input.type = 'checkbox'; input.checked = selectedKeywords.has(id);
    input.setAttribute('aria-label', t('ai.products.select-keyword-named', { name }));
    input.addEventListener('change', () => {
      if (input.checked) {
        if (selectedKeywords.size >= 20) {
          input.checked = false; setStatus(t('ai.products.scope-limit', { max: 20 }), true); return;
        }
        selectedKeywords.set(id, { name, source: item.source || item.source_id || '' });
      } else selectedKeywords.delete(id);
      requestKey = null; activeRecommendationJob = null;
      renderSelectedKeywords();
    });
    choice.append(input, make('span', name));
    const source = item.source || item.source_id;
    if (source) choice.append(make('small', localeSource(source)));
    row.append(choice); list.append(row);
  }
  root.append(list);
}

async function searchKeywords() {
  const query = ($('#ai-product-query')?.value || '').trim();
  if (!query) { setStatus(label('ai.products.search-required', 'Enter a keyword to search saved signals.'), true); return; }
  const sequence = ++searchSequence;
  const root = $('#ai-product-search-results');
  root?.replaceChildren(make('p', label('common.loading', 'Loading')));
  try {
    const params = new URLSearchParams({ q: query, limit: '20', offset: '0', include_algorithm_results: 'false' });
    const result = await getJson(`/api/v1/datasets/latest/keywords?${params}`, { locale: getLocale() });
    if (sequence !== searchSequence) return;
    renderSearchResults(Array.isArray(result.items) ? result.items : []);
    setStatus(label('ai.products.instructions', 'Select saved keyword signals, then request product hypotheses.'), false);
  } catch {
    if (sequence === searchSequence) {
      renderSearchResults([]);
      setStatus(label('ai.products.search-failed', 'Saved keywords could not be loaded.'), true);
    }
  }
}

async function loadSelectedIdsFromRoute() {
  const sequence = ++selectionHydrationSequence;
  const params = new URLSearchParams(window.location.search);
  const ids = [...params.getAll('keyword_id'), ...params.getAll('keyword_ids').flatMap((value) => value.split(','))]
    .map((value) => value.trim()).filter(Boolean);
  const unique = [...new Set(ids)];
  if (unique.length > 20 || unique.some((id) => id.length > 64)) {
    setStatus(t('ai.products.scope-limit', { max: 20 }), true);
    return;
  }
  const resolved = await Promise.all(unique.map(async (id) => {
    try {
      const record = await getJson(`/api/v1/collections/keywords/${encodeURIComponent(id)}`, { locale: getLocale() });
      return [id, { name: record.keyword || record.display_keyword || id, source: record.source || '' }];
    } catch { return [id, { name: id, source: '' }]; }
  }));
  if (sequence !== selectionHydrationSequence) return;
  if (unique.length) selectedKeywords.clear();
  for (const [id, item] of resolved) selectedKeywords.set(id, item);
  renderSelectedKeywords();
}

function appendSection(card, heading, values) {
  const rows = Array.isArray(values) ? values.filter((value) => typeof value === 'string' && value.trim())
    : typeof values === 'string' && values.trim() ? [values] : [];
  if (!rows.length) return;
  const section = make('section', '', 'ai-product-output-section');
  section.append(make('h4', heading));
  if (rows.length === 1) section.append(make('p', rows[0]));
  else {
    const list = make('ul'); rows.forEach((value) => list.append(make('li', value)));
    section.append(list);
  }
  card.append(section);
}

function safeMarketplaceUrl(value) {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    return url.protocol === 'https:' && !url.username && !url.password
      && ['amazon.com', 'www.amazon.com', 'etsy.com', 'www.etsy.com'].includes(host) ? url.href : null;
  } catch { return null; }
}

function renderRecommendations(result) {
  const root = $('#ai-product-results');
  if (!root) return;
  root.replaceChildren();
  if (result.cached === true) root.append(make('p', label('ai.products.cache-hit', 'Reused a valid cached recommendation.')));
  else root.append(make('p', label('ai.products.cache-miss', 'New recommendation result.')));
  const lineage = result.lineage?.model_provider || {};
  const lineageFields = [lineage.provider, lineage.model || lineage.model_id].filter((value) => typeof value === 'string');
  if (lineageFields.length) root.append(make('p', t('ai.products.model-lineage', { value: lineageFields.join(' · ') })));
  const scopeRows = Array.isArray(result.evidence_scope) ? result.evidence_scope : [];
  for (const recommendation of Array.isArray(result.recommendations) ? result.recommendations : []) {
    const card = make('article', '', 'ai-product-card');
    card.append(make('span', recommendation.label || label('ai.products.hypothesis', 'AI-generated hypothesis'), 'pill pill--amber'));
    card.append(make('h3', recommendation.product_name || label('common.unavailable', 'Unavailable')));
    card.append(make('p', recommendation.product_category || label('common.unavailable', 'Unavailable'), 'fine-print'));
    appendSection(card, label('ai.products.related-keywords', 'Related keywords'),
      (recommendation.related_keyword_ids || []).map((id) => selectedKeywords.get(id)?.name || id));
    appendSection(card, label('ai.products.rationale', 'Why this product'), recommendation.why_this_product);
    appendSection(card, label('ai.products.consumer-segment', 'Potential US consumer segment'), recommendation.consumer_segment);
    appendSection(card, label('ai.products.seasonality', 'Seasonality'), recommendation.seasonality);
    appendSection(card, label('ai.products.materials', 'Possible materials'), recommendation.possible_materials);
    appendSection(card, label('ai.products.manufacturing', 'Manufacturing considerations'), recommendation.manufacturing_considerations);
    appendSection(card, label('ai.products.export', 'Vietnam-to-US export considerations'), recommendation.export_considerations);
    appendSection(card, label('ai.products.risks', 'Risks'), [
      ...(recommendation.competition_risks || []).map((value) => `${label('ai.products.competition-risk', 'Competition')}: ${value}`),
      ...(recommendation.ip_risks || []).map((value) => `${label('ai.products.ip-risk', 'Intellectual property')}: ${value}`),
      ...(recommendation.regulatory_risks || []).map((value) => `${label('ai.products.regulatory-risk', 'Regulatory')}: ${value}`),
    ]);
    appendSection(card, label('ai.products.uncertainty', 'Uncertainty'), recommendation.uncertainty);
    appendSection(card, label('ai.products.research-steps', 'Research steps'), recommendation.research_steps);
    appendSection(card, label('ai.products.target-market', 'Market framing'), recommendation.target_market);
    const evidenceIds = new Set(Array.isArray(recommendation.supporting_evidence_ids) ? recommendation.supporting_evidence_ids : []);
    const evidence = scopeRows.flatMap((row) => Array.isArray(row.evidence_context) ? row.evidence_context.map((item) => ({ ...item, source_id: row.source_id, observed_at: row.observed_at })) : [])
      .filter((item) => evidenceIds.has(item.evidence_id));
    const evidenceLines = evidence.map((item) => [
      item.source_id ? localeSource(item.source_id) : '', item.metric, item.unit, item.scope,
      item.observed_at, item.geography?.country_code || item.geography?.country,
      item.limitations,
    ].filter(Boolean).join(' · '));
    appendSection(card, label('ai.products.evidence', 'Supporting evidence'), evidenceLines.length ? evidenceLines : [...evidenceIds]);
    const links = Array.isArray(recommendation.marketplace_research_links) ? recommendation.marketplace_research_links : [];
    if (links.length) {
      const section = make('section', '', 'ai-product-output-section');
      section.append(make('h4', label('ai.products.marketplace-links', 'Marketplace research links')));
      const list = make('ul');
      for (const item of links) {
        const href = safeMarketplaceUrl(item.url);
        if (!href) continue;
        const anchor = make('a', item.marketplace || href); anchor.href = href;
        anchor.target = '_blank'; anchor.rel = 'noopener noreferrer';
        const linkRow = make('li'); linkRow.append(anchor); list.append(linkRow);
      }
      if (list.childElementCount) { section.append(list, make('p', label('ai.products.links-not-visited', 'These are research links; TrendScout has not visited or verified marketplace listings.'), 'fine-print')); card.append(section); }
    }
    root.append(card);
  }
  if (!Array.isArray(result.recommendations) || !result.recommendations.length) {
    root.append(make('p', label('ai.products.no-recommendations', 'No validated recommendations were returned.')));
  }
}

function safeRecommendationError(error) {
  const code = error?.body?.detail?.code || error?.body?.code;
  const retrySeconds = Number(error?.retryAfterSeconds);
  if (code === 'AI_RATE_LIMITED' || error?.status === 429) {
    return retrySeconds > 0 ? t('ai.error.rate-limited-retry', { seconds: number(retrySeconds) })
      : label('ai.error.rate-limited', 'AI provider rate limited the request. Try again after the reported cooldown.');
  }
  const known = {
    AI_QUOTA_EXHAUSTED: 'ai.error.quota', AI_PROVIDER_UNAVAILABLE: 'ai.error.unavailable',
    AI_MODEL_UNAVAILABLE: 'ai.error.unavailable', AI_KEY_MISSING: 'ai.error.unavailable',
    AI_INVALID_OUTPUT: 'ai.error.invalid-output',
    AI_POLICY_DENIED: 'ai.error.restricted', AI_DISABLED: 'ai.error.unavailable',
    AI_STORAGE_UNAVAILABLE: 'ai.error.storage', AI_INVALID_OUTPUT: 'ai.error.invalid-output',
  };
  if (known[code]) return label(known[code], code);
  return label('ai.error.request', 'The AI request could not be completed. Existing data was kept.');
}

async function renderJobAction(result) {
  const root = $('#ai-product-results');
  if (!root || !result?.job_id || result.status === 'COMPLETED') return;
  activeRecommendationJob = result.job_id;
  root.append(make('p', t('ai.products.job-state', { status: result.status || 'PENDING', retry: result.retry_at ? formatTimestamp(result.retry_at, getLocale()) : label('common.unavailable', 'Unavailable') })));
  const resume = make('button', label('ai.products.resume', 'Resume saved recommendation'), 'button button-secondary');
  resume.type = 'button'; resume.addEventListener('click', () => submitRecommendation({ resume: true })); root.append(resume);
}

async function submitRecommendation({ resume = false } = {}) {
  const ids = [...selectedKeywords.keys()];
  if (!ids.length || ids.length > 20) { setStatus(t('ai.products.scope-limit', { max: 20 }), true); return; }
  const formButton = $('#ai-product-generate');
  if (formButton) formButton.disabled = true;
  setStatus(label('ai.products.loading', 'Preparing recommendations…'));
  try {
    let result;
    if (resume && activeRecommendationJob) {
      result = await requestJson(`/api/v1/ai/jobs/${encodeURIComponent(activeRecommendationJob)}/resume`, { method: 'POST', locale: getLocale() });
    } else {
      requestKey ||= `products-${crypto.randomUUID()}`;
      result = await requestJson('/api/v1/ai/recommendations', {
        method: 'POST', locale: getLocale(), headers: { 'Idempotency-Key': requestKey },
        body: { keyword_ids: ids, regenerate: $('#ai-product-regenerate')?.checked === true },
      });
    }
    if (result.status === 'COMPLETED') {
      requestKey = null; activeRecommendationJob = null;
      renderRecommendations(result);
      setStatus(label('ai.products.complete', 'Recommendation request completed.'), false);
    } else {
      await renderJobAction(result);
      if (result.error_code) setStatus(safeRecommendationError({ body: { code: result.error_code } }), true);
      else setStatus(t('ai.products.pending', { status: result.status || 'PENDING' }), false);
    }
  } catch (error) {
    setStatus(safeRecommendationError(error), true);
    if (activeRecommendationJob) await renderJobAction({ job_id: activeRecommendationJob, status: 'DEFERRED' });
  } finally {
    if (formButton) formButton.disabled = selectedKeywords.size < 1 || selectedKeywords.size > 20;
    await loadCapabilities(true);
  }
}

function initializeAiUi() {
  $('#ai-product-search-form')?.addEventListener('submit', (event) => { event.preventDefault(); void searchKeywords(); });
  $('#ai-product-generate-form')?.addEventListener('submit', (event) => { event.preventDefault(); void submitRecommendation(); });
  $('#ai-product-regenerate')?.addEventListener('change', () => { requestKey = null; activeRecommendationJob = null; });
  document.addEventListener('trendscout:recommend-keywords', (event) => navigateToProducts(event.detail?.keywordIds || []));
  document.addEventListener('trendscout:route', (event) => {
    const page = event.detail?.page;
    if (page === 'products') void loadSelectedIdsFromRoute();
    if (page === 'products' || page === 'settings') void loadCapabilities();
  });
  document.addEventListener('trendscout:locale', () => { renderCapabilityPanel(); renderSelectedKeywords(); });
}

export { initializeAiUi };
