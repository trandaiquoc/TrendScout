import { getJson } from './modules/api.js';
import { loadCapabilities, renderSourceCapabilities } from './modules/capabilities.js';
import { PublicFetchController } from './modules/fetch.js';
import { formatDate, formatNumber, formatTimestamp } from './modules/format.js';
import { applyLocale, getLocale, initializeLocale, t } from './modules/i18n.js';
import { initializeTheme } from './modules/theme.js';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const activeStatuses = new Set(['QUEUED', 'FETCHING', 'NORMALIZING', 'ANALYZING', 'PERSISTING']);
const state = {
  runId: null,
  dataRunId: null,
  run: null,
  category: 'all',
  query: '',
  sort: 'score',
  offset: 0,
  limit: 20,
  next: null,
  pollTimer: null,
  pollCount: 0,
  runSequence: 0,
  keywordSequence: 0,
  detailSequence: 0,
  detail: null,
  capabilityRetryTimer: null,
  capabilities: null,
  fetchReady: false,
  fetchBlockReason: null,
  fetchPresentation: { kind: 'disabled', busy: false },
};

function number(value) {
  return formatNumber(value, getLocale());
}

function date(value) {
  return formatDate(value, getLocale());
}

function timestamp(value) {
  const rendered = formatTimestamp(value, getLocale());
  return rendered === '—' ? rendered : rendered + ' · ' + t('time.local');
}

function element(tagName, text = null, className = '') {
  const node = document.createElement(tagName);
  if (text !== null && text !== undefined) {
    node.textContent = String(text);
  }
  if (className) {
    node.className = className;
  }
  return node;
}

function setNotice(target, message, kind = 'warning') {
  const box = $(target);
  if (!box) {
    return;
  }
  box.hidden = !message;
  box.classList.remove('notice--warning', 'notice--error', 'notice--info');
  box.classList.add('notice--' + kind);
  box.replaceChildren();
  if (!message) {
    return;
  }
  box.append(element('span', kind === 'error' ? '!' : 'i', 'notice-icon'));
  box.append(element('p', message));
}

function announce(message, error = false) {
  setNotice('#global-notice', message, error ? 'error' : 'warning');
}

function getErrorMessage(error, fallbackKey) {
  return error && typeof error.message === 'string' ? error.message : t(fallbackKey);
}

const FETCH_BLOCK_MESSAGES = {
  FEATURE_DISABLED: 'fetch.disabled',
  LIVE_FETCH_NOT_APPROVED: 'fetch.live-not-approved',
  READ_ONLY_DEPLOYMENT: 'fetch.read-only',
  CLOUD_STORAGE_NOT_ALLOWED: 'fetch.read-only',
  OFFLINE_FAKE_STORAGE_REQUIRED: 'fetch.offline-unavailable',
  OFFLINE_FIXTURE_ADAPTER_REQUIRED: 'fetch.offline-unavailable',
  SYNTHETIC_RUNTIME_UNAVAILABLE: 'fetch.offline-unavailable',
  ADMISSION_STATE_UNAVAILABLE: 'fetch.offline-unavailable',
  MULTI_PROCESS_RUNTIME_UNSUPPORTED: 'fetch.multi-process-unavailable',
  SOURCE_NOT_ENABLED: 'fetch.source-unavailable',
  FETCH_ALREADY_RUNNING: 'fetch.busy',
  PUBLIC_FETCH_RATE_LIMITED: 'fetch.rate-limited',
  RUN_COOLDOWN_ACTIVE: 'fetch.rate-limited',
};

const FETCH_STATE_MESSAGES = {
  submitting: 'fetch.submitting',
  queued: 'fetch.queued',
  running: 'fetch.running',
  completed: 'fetch.completed',
  partial: 'fetch.partial',
  failed: 'fetch.failed',
  aborted: 'fetch.aborted',
  'rate-limited': 'fetch.rate-limited',
  'busy-error': 'fetch.busy',
  retryable: 'fetch.retryable',
  'poll-limit': 'fetch.poll-limit',
  'poll-error': 'fetch.poll-error',
  expired: 'fetch.expired',
};
const FETCH_SOURCE_LABELS = {
  wikimedia_pageviews_top_per_country: 'fetch.source.wikimedia',
  google_web_serp: 'fetch.source.google',
  bing_web_serp: 'fetch.source.bing',
  youtube_video_search: 'fetch.source.youtube',
};

function renderFetchPresentation() {
  const presentation = state.fetchPresentation;
  const messageKey = FETCH_STATE_MESSAGES[presentation.kind];
  const blockKey = FETCH_BLOCK_MESSAGES[state.fetchBlockReason] || 'fetch.disabled';
  const isActionState = Boolean(messageKey);
  const button = $('#open-fetch');
  const badge = $('#fetch-badge');
  const readiness = $('#fetch-readiness');
  const status = $('#fetch-status');
  const canRetry = ['retryable', 'poll-limit', 'poll-error'].includes(presentation.kind);
  const busy = Boolean(presentation.busy);

  button.disabled = !state.fetchReady || busy || presentation.kind === 'rate-limited';
  const buttonKey = presentation.kind === 'retryable'
    ? 'fetch.retry'
    : canRetry ? 'fetch.check-status' : 'fetch.cta';
  button.querySelector('[data-i18n="fetch.cta"]').textContent = t(buttonKey);
  badge.textContent = t(state.fetchReady ? 'fetch.synthetic-label' : 'fetch.unavailable');
  badge.classList.toggle('pill--green', state.fetchReady);
  badge.classList.toggle('pill--amber', !state.fetchReady);
  readiness.textContent = state.fetchReady ? t('fetch.ready') : t(blockKey);
  status.hidden = !isActionState;
  const sourceSummary = Array.isArray(presentation.fetch?.sources)
    ? presentation.fetch.sources
      .filter((source) => source && typeof source === 'object')
      .map((source) => {
        const sourceKey = FETCH_SOURCE_LABELS[source.source_id];
        const sourceLabel = sourceKey ? t(sourceKey) : source.source_id;
        const statusKey = `fetch.source-status.${source.status}`;
        const sourceStatus = source.status && t(statusKey) !== statusKey
          ? t(statusKey) : source.status;
        return [sourceLabel, sourceStatus, source.data_mode, source.delivery_state]
          .filter((value) => typeof value === 'string' && value).join(' · ');
      })
      .join(' | ')
    : '';
  status.textContent = isActionState
    ? [t(messageKey), sourceSummary ? t('fetch.source-summary', { sources: sourceSummary }) : '']
      .filter(Boolean).join(' ')
    : '';
}

function scheduleCapabilityRefresh(delayMs) {
  clearTimeout(state.capabilityRetryTimer);
  state.capabilityRetryTimer = window.setTimeout(() => {
    state.capabilityRetryTimer = null;
    loadPublicCapabilities();
  }, delayMs);
}

async function loadPublicCapabilities() {
  try {
    const capabilities = await loadCapabilities(getLocale());
    state.capabilities = capabilities;
    state.fetchBlockReason = capabilities.fetch_block_reason || null;
    state.fetchReady = capabilities.public_fetch_enabled === true
      && capabilities.fetch_enabled === true
      && capabilities.authentication_required === false
      && capabilities.data_mode === 'SYNTHETIC'
      && Array.isArray(capabilities.sources)
      && capabilities.sources.some((source) => source.enabled_for_current_operation === true);
    if (!state.fetchReady && !state.fetchBlockReason) {
      state.fetchBlockReason = 'FEATURE_DISABLED';
    }
    if (state.fetchReady) {
      clearTimeout(state.capabilityRetryTimer);
      state.capabilityRetryTimer = null;
      if (state.fetchPresentation.kind === 'rate-limited') {
        state.fetchPresentation = { kind: 'disabled', busy: false };
      }
    } else if ([
      'FETCH_ALREADY_RUNNING',
      'PUBLIC_FETCH_RATE_LIMITED',
      'RUN_COOLDOWN_ACTIVE',
    ].includes(state.fetchBlockReason)) {
      scheduleCapabilityRefresh(60000);
    }
    renderSourceCapabilities(capabilities, t);
    renderFetchPresentation();
  } catch {
    state.capabilities = null;
    state.fetchReady = false;
    state.fetchBlockReason = null;
    renderSourceCapabilities(null, t);
    renderFetchPresentation();
    $('#fetch-readiness').textContent = t('fetch.capability-unavailable');
  }
}

const fetchController = new PublicFetchController({
  onState(presentation) {
    state.fetchPresentation = presentation;
    renderFetchPresentation();
    if (presentation.kind === 'busy-error' || presentation.kind === 'rate-limited') {
      const delay = presentation.error?.retryAfterSeconds || 5;
      scheduleCapabilityRefresh(Math.min(delay, 60) * 1000);
    }
  },
  onTerminal(runId) {
    if (runId) {
      loadRun(runId);
    }
    scheduleCapabilityRefresh(100);
  },
  locale: getLocale,
});

function localizedStatus(prefix, status) {
  if (typeof status !== 'string' || !status) {
    return '—';
  }
  const key = prefix + status;
  const value = t(key);
  return value === key ? status : value;
}

function safeEvidenceUrl(value) {
  try {
    const url = new URL(value);
    const trustedHost = /^(?:[a-z0-9-]+\.)*(?:wikipedia|wikimedia|wiktionary|wikiquote|wikibooks|wikinews|wikisource|wikiversity|wikivoyage|wikidata|mediawiki|wikifunctions)\.org$/i;
    return url.protocol === 'https:' && !url.username && !url.password && trustedHost.test(url.hostname)
      ? url.href
      : null;
  } catch {
    return null;
  }
}

function updateRun(run) {
  state.run = run;
  renderSyntheticDiscovery(run);
  const status = typeof run.status === 'string' ? run.status : '';
  $('#run-status').textContent = t('status.' + status) === 'status.' + status
    ? (status || 'NOT_AVAILABLE')
    : t('status.' + status);
  $('#run-note').textContent = run.run_id
    ? t('run.selected', { id: run.run_id.slice(0, 10) + '…' })
    : t('run.none');
  $('#data-date').textContent = date(run.data_date);
  $('#keyword-count').textContent = number(run.keyword_count);
  $('#scored-count').textContent = number(run.scored_count);
  $('#runs-current').textContent = run.run_id || 'NOT_AVAILABLE';
  $('#runs-state').textContent = status ? localizedStatus('status.', status) : 'NOT_AVAILABLE';
  $('#runs-count').textContent = run.keyword_count == null ? 'NOT_AVAILABLE' : number(run.keyword_count);

  const coverage = Array.isArray(run.coverage)
    ? run.coverage.map((item) => (item.date || '—') + ': ' + (item.status || 'NOT_AVAILABLE')
      + (item.http_status ? ' (' + item.http_status + ')' : '')).join(' · ') || 'NOT_AVAILABLE'
    : 'NOT_MEASURED';
  $('#runs-coverage').textContent = coverage;
  $('#quality-coverage').textContent = coverage;
  $('#quality-fetched').textContent = run.fetched_at ? timestamp(run.fetched_at) : 'NOT_AVAILABLE';

  const mode = run.mode === 'live' ? 'LIVE'
    : run.mode === 'synthetic' ? 'SYNTHETIC'
      : run.mode === 'replay' ? 'REPLAY'
        : 'UNKNOWN';
  const context = [];
  context.push('US · ' + (run.access || 'all-access') + ' · ' + mode);
  if (Array.isArray(run.coverage) && run.coverage.length) {
    context.push(run.coverage.map((item) => item.date || '—').join(' · '));
  }
  context.push(run.algorithm_version || 'NOT_AVAILABLE');
  context.push(run.fetched_at ? timestamp(run.fetched_at) : 'NOT_AVAILABLE');
  $('#overview-context').replaceChildren(
    element('span', '◉', 'meta-mark'),
    element('span', context.join('  /  ')),
  );

  const warnings = [];
  if (run.mode === 'synthetic' || run.mode === 'replay') {
    warnings.push(t('quality.synthetic', { mode }));
  } else if (!['live', 'synthetic', 'replay'].includes(run.mode)) {
    warnings.push(t('quality.unknown-mode'));
  }
  if (status === 'PARTIAL') {
    warnings.push(t('quality.partial'));
  }
  if (status === 'FAILED' || status === 'ABORTED') {
    warnings.push(t('error.run-failed', {
      status: t('status.' + status).toLowerCase(),
      message: typeof run.error === 'string' && run.error ? run.error : t('error.empty-detail'),
    }));
  }
  const observationTime = Date.parse(String(run.data_date || '') + 'T00:00:00Z');
  const isStale = Number.isFinite(observationTime) && Date.now() - observationTime > 3 * 86400000;
  $('#quality-staleness').textContent = isStale ? t('quality.stale') : t('quality.fresh');
  if (isStale) {
    warnings.push(t('quality.stale'));
  }
  const incomplete = Array.isArray(run.coverage)
    ? run.coverage.filter((item) => item.status !== 'SUCCESS')
    : [];
  if (incomplete.length) {
    warnings.push(t('quality.coverage-missing', {
      days: incomplete.map((item) => (item.date || '—') + ' (' + (item.http_status ?? item.status) + ')').join(', '),
    }));
  }
  if (state.dataRunId && state.dataRunId !== state.runId) {
    warnings.push(t('error.old-table'));
  }
  announce(warnings.join(' '), status === 'FAILED' || status === 'ABORTED');
}

function renderSyntheticDiscovery(run) {
  const bundle = run?.mode === 'synthetic' && run.synthetic_discovery?.data_mode === 'SYNTHETIC'
    ? run.synthetic_discovery : null;
  const sourcePanel = $('#demo-source-results');
  const panel = $('#demo-discovery');
  sourcePanel.hidden = !bundle;
  panel.hidden = !bundle;
  const sourceList = $('#demo-source-list');
  const list = $('#demo-discovery-list');
  sourceList.replaceChildren();
  list.replaceChildren();
  if (!bundle) return;
  if (bundle.error) {
    sourceList.append(element('li', t('demo.failed')));
    $('#demo-discovery-count').textContent = t('demo.failed');
    return;
  }
  for (const source of Array.isArray(bundle.sources) ? bundle.sources : []) {
    const name = ({ google_web_serp: 'Google / SerpApi', bing_web_serp: 'Bing / SerpApi',
      youtube_video_search: 'YouTube' })[source.source_id] || '—';
    sourceList.append(element('li', `${name} · SYNTHETIC · ${source.status} · ${source.record_count} · ${source.stop_reason || '—'}`));
  }
  const category = state.category === 'home_goods' ? 'home_living' : state.category;
  const query = state.query.normalize('NFC').trim().toLocaleLowerCase();
  const candidates = (Array.isArray(bundle.candidates) ? bundle.candidates : []).filter((candidate) => {
    const categories = Array.isArray(candidate.categories) ? candidate.categories : [];
    return (!query || String(candidate.keyword || '').normalize('NFC').toLocaleLowerCase().includes(query))
      && (category === 'all' || categories.includes(category));
  });
  $('#demo-discovery-count').textContent = t('demo.count', {
    shown: candidates.length, total: bundle.candidate_count ?? 0,
  });
  for (const candidate of candidates) {
    const item = element('li');
    item.append(element('strong', candidate.keyword));
    const assessments = Array.isArray(candidate.assessments) ? candidate.assessments : [];
    const classifications = [...new Set(assessments.map((assessment) => assessment.classification))];
    item.append(element('span', ` · SYNTHETIC · ${classifications.join(', ') || '—'}`));
    const details = element('details');
    details.append(element('summary', t('demo.evidence')));
    const evidenceList = element('ul');
    for (const evidence of Array.isArray(candidate.evidence) ? candidate.evidence : []) {
      evidenceList.append(element('li', [evidence.source_id, evidence.data_mode,
        evidence.delivery_mode, timestamp(evidence.fetched_at), evidence.scope,
        ...(Array.isArray(evidence.query_evidence) ? evidence.query_evidence : [])].join(' · ')));
    }
    details.append(evidenceList);
    item.append(details);
    list.append(item);
  }
}

async function loadRun(id, { poll = false } = {}) {
  const runId = String(id || '').trim().toLowerCase();
  if (!/^[a-f0-9]{32}$/i.test(runId)) {
    announce(t('error.run-id'), true);
    return;
  }
  if (!poll) {
    clearTimeout(state.pollTimer);
    state.pollCount = 0;
    state.runSequence += 1;
    state.detailSequence += 1;
    announce('');
  }
  const sequence = ++state.runSequence;
  try {
    const run = await getJson('/api/runs/' + encodeURIComponent(runId), { locale: getLocale() });
    if (sequence !== state.runSequence) {
      return;
    }
    if (!run || typeof run.status !== 'string') {
      throw new Error(t('error.run-invalid'));
    }
    state.runId = runId;
    $('#run-id').value = runId;
    updateRun(run);
    if (activeStatuses.has(run.status)) {
      if (state.pollCount >= 30) {
        announce(t('error.run-timeout', { count: state.pollCount }));
        return;
      }
      state.pollCount += 1;
      state.pollTimer = setTimeout(() => loadRun(runId, { poll: true }), 2000);
      return;
    }
    if ((run.status === 'COMPLETED' || run.status === 'PARTIAL') && Number.isInteger(run.keyword_count)) {
      state.dataRunId = runId;
      state.offset = 0;
      renderRows([]);
      showEmpty(t('keywords.empty-title'), t('keywords.page-empty'));
      await loadKeywords();
    }
  } catch (error) {
    if (sequence === state.runSequence) {
      announce(getErrorMessage(error, 'error.run-fetch'), true);
    }
  }
}

function appendCell(row, value, className = '') {
  const cell = element('td');
  cell.append(element('span', value, className));
  row.append(cell);
}

function renderRows(items) {
  const body = $('#rows');
  body.replaceChildren();
  for (const item of items) {
    const row = element('tr');
    const title = element('td');
    title.append(element('strong', item.display_keyword || item.canonical_keyword || '—'));
    title.append(element('small', item.project || '—'));
    row.append(title);
    appendCell(row, item.score == null ? '—' : number(item.score) + ' / 100', 'score-cell');
    const status = item.status || '—';
    appendCell(row, localizedStatus('keywordstatus.', status), 'pill table-status ' + (
      ['EMERGING', 'STABLE', 'FALLING', 'INSUFFICIENT_DATA'].includes(status) ? 'pill--' + status.toLowerCase() : ''
    ));
    appendCell(row, number(item.current_views_ceil));
    appendCell(row, number(item.baseline));
    appendCell(row, item.ratio == null ? '—' : number(item.ratio) + '×');
    appendCell(row, date(item.observation_date));
    const flags = Array.isArray(item.quality_flags) ? item.quality_flags : [];
    appendCell(row, flags.length ? t('table.flags', { count: number(flags.length) }) : '—');
    const action = element('td');
    const button = element('button', '↗', 'icon-button row-detail');
    button.type = 'button';
    button.setAttribute('aria-label', t('table.open-detail', {
      name: item.display_keyword || item.canonical_keyword || t('common.keyword'),
    }));
    button.addEventListener('click', () => openDetail(item.keyword_id));
    action.append(button);
    row.append(action);
    body.append(row);
  }
}

function showEmpty(title, detail) {
  const box = $('#empty');
  box.hidden = false;
  $('strong', box).textContent = title;
  $('p', box).textContent = detail;
}

async function loadKeywords() {
  if (!state.dataRunId) {
    renderRows([]);
    showEmpty(t('keywords.empty-title'), t('keywords.no-run'));
    $('#results-count').textContent = '—';
    $('#pagination-info').textContent = t('keywords.no-results');
    $('#prev').disabled = true;
    $('#next').disabled = true;
    return;
  }
  const sequence = ++state.keywordSequence;
  const params = new URLSearchParams({
    q: state.query,
    category: state.category,
    sort: state.sort,
    limit: String(state.limit),
    offset: String(state.offset),
  });
  try {
    const result = await getJson('/api/runs/' + encodeURIComponent(state.dataRunId) + '/keywords?' + params, {
      locale: getLocale(),
    });
    if (sequence !== state.keywordSequence) {
      return;
    }
    if (!result || !Array.isArray(result.items) || !Number.isInteger(result.total)) {
      throw new Error(t('error.keyword-invalid'));
    }
    renderRows(result.items);
    state.next = result.next_offset;
    $('#results-count').textContent = number(result.total);
    const shown = result.items.length;
    if (!result.total) {
      $('#pagination-info').textContent = t('table.zero-results');
    } else {
      $('#pagination-info').textContent = t('table.range', {
        start: number(state.offset + 1),
        end: number(state.offset + shown),
        total: number(result.total),
      });
    }
    $('#prev').disabled = state.offset === 0;
    $('#next').disabled = result.next_offset == null;
    if (!result.items.length) {
      showEmpty(result.empty_reason ? t('keywords.none-match') : t('keywords.page-empty'), t('keywords.empty-help'));
    } else {
      $('#empty').hidden = true;
    }
    setNotice('#keyword-notice', '');
  } catch (error) {
    if (sequence === state.keywordSequence) {
      renderRows([]);
      setNotice('#keyword-notice', getErrorMessage(error, 'error.keyword-fetch'), 'error');
      showEmpty(t('error.keyword-fetch'), t('keywords.empty-help'));
    }
  }
}

function detailField(container, label, value, full = false) {
  const field = element('div', null, 'detail-item' + (full ? ' detail-item--wide' : ''));
  field.append(element('small', label));
  field.append(element('div', value));
  container.append(field);
}

function renderDetail(detail) {
  const content = $('#detail-content');
  content.replaceChildren();
  $('#detail-title').textContent = detail.display_keyword || detail.canonical_keyword || t('detail.title-fallback');
  $('#detail-subtitle').textContent = [detail.source || '—', detail.project || '—', date(detail.observation_date)].join(' · ');
  detailField(content, t('detail.score-status'), (detail.score == null
    ? t('detail.insufficient')
    : number(detail.score) + ' / 100') + ' · ' + localizedStatus('keywordstatus.', detail.status));
  detailField(content, t('detail.views-baseline'), number(detail.current_views_ceil) + ' / ' + number(detail.baseline));
  detailField(content, t('detail.ratio-date'), (detail.ratio == null ? '—' : number(detail.ratio) + '×')
    + ' · ' + number(detail.observed_baseline_days) + ' / 7');
  detailField(content, t('detail.algorithm'), detail.algorithm_version || '—');
  detailField(content, t('detail.categories'), Array.isArray(detail.categories) && detail.categories.length
    ? detail.categories.join(' · ')
    : 'NOT_AVAILABLE');
  detailField(content, t('detail.entity-commercial'), (typeof detail.entity_type === 'string' ? detail.entity_type : 'NOT_AVAILABLE')
    + ' · ' + (typeof detail.commercial_relevance === 'string' ? detail.commercial_relevance : 'NOT_ASSESSED'));
  detailField(content, t('detail.article-scope'), (typeof detail.article_original === 'string' ? detail.article_original : 'NOT_AVAILABLE')
    + ' · ' + (typeof detail.country_scope === 'string' ? detail.country_scope : 'NOT_AVAILABLE')
    + ' / ' + (typeof detail.access === 'string' ? detail.access : 'NOT_AVAILABLE'), true);
  const components = detail.score_components;
  detailField(content, t('detail.score-components'), components
    ? t('detail.components', {
      growth: number(components.growth),
      volume: number(components.volume),
      views: number(components.max_views_ceil),
    })
    : t('detail.not-enough-score'), true);
  const missing = Array.isArray(detail.missing_baseline_dates)
    ? detail.missing_baseline_dates.map((item) => item.date + ': ' + item.reason).join(' · ')
    : '';
  detailField(content, t('detail.missing-dates'), missing || t('detail.no-missing'), true);
  const observations = Array.isArray(detail.baseline_observations)
    ? detail.baseline_observations.map((item) => item.date + ': ' + number(item.views_ceil)).join(' · ')
    : '';
  detailField(content, t('detail.observations'), observations || t('detail.no-observations'), true);
  const classification = Array.isArray(detail.classification_evidence)
    ? detail.classification_evidence.map((item) => item.category + ': ' + item.matched_text + ' (' + item.rule_id + ')').join(' · ')
    : '';
  detailField(content, t('detail.classification'), classification || t('detail.no-classification'), true);
  detailField(content, t('detail.quality-flags'), Array.isArray(detail.quality_flags) && detail.quality_flags.length
    ? detail.quality_flags.join(' · ')
    : t('detail.no-flags'), true);
  const sourceUrl = safeEvidenceUrl(detail.evidence_url || detail.source_url);
  if (sourceUrl) {
    const evidence = element('div', null, 'detail-item detail-item--wide');
    evidence.append(element('small', t('detail.evidence-source')));
    const link = element('a', t('detail.open-wikimedia'));
    link.href = sourceUrl;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    evidence.append(link);
    content.append(evidence);
  }
}

async function openDetail(id) {
  if (!state.dataRunId || !/^[a-f0-9]{64}$/i.test(id || '')) {
    return;
  }
  const runId = state.dataRunId;
  const sequence = ++state.detailSequence;
  const dialog = $('#detail-dialog');
  const content = $('#detail-content');
  state.detail = null;
  $('#detail-title').textContent = t('detail.loading');
  $('#detail-subtitle').textContent = '';
  content.replaceChildren();
  if (!dialog.open) {
    dialog.showModal();
  }
  try {
    const detail = await getJson('/api/runs/' + encodeURIComponent(runId) + '/keywords/' + encodeURIComponent(id), {
      locale: getLocale(),
    });
    if (sequence !== state.detailSequence || !dialog.open || state.dataRunId !== runId) {
      return;
    }
    state.detail = detail;
    renderDetail(detail);
  } catch (error) {
    if (sequence === state.detailSequence && dialog.open) {
      state.detail = null;
      $('#detail-title').textContent = t('detail.load-error');
      detailField(content, t('detail.load-error'), getErrorMessage(error, 'error.detail-fetch'), true);
    }
  }
}

function showView(name) {
  const view = name === 'results' ? 'keywords' : name;
  const valid = new Set(['overview', 'sources', 'runs', 'keywords', 'algorithms', 'quality', 'refresh', 'research', 'usage', 'settings']);
  const selected = valid.has(view) ? view : 'overview';
  $$('[data-view]').forEach((panel) => { panel.hidden = panel.dataset.view !== selected; });
  $$('[data-nav]').forEach((link) => {
    const active = link.dataset.nav === selected;
    link.classList.toggle('active', active);
    if (active) {
      link.setAttribute('aria-current', 'page');
    } else {
      link.removeAttribute('aria-current');
    }
  });
  $('#breadcrumb-current').textContent = t('nav.' + selected);
  if (selected === 'keywords' && !state.dataRunId) {
    showEmpty(t('keywords.empty-title'), t('keywords.no-run'));
  }
  closeMobileNav();
}

function isMobileNav() {
  return window.matchMedia('(max-width: 900px)').matches;
}

function setMobileNav(open) {
  const shell = $('#app-shell');
  const panel = $('#workspace-nav-panel');
  const toggle = $('#menu-toggle');
  const scrim = $('#nav-scrim');
  const visible = isMobileNav() && open;
  shell.classList.toggle('menu-open', visible);
  toggle.setAttribute('aria-expanded', String(visible));
  toggle.setAttribute('aria-label', t(visible ? 'a11y.close-nav' : 'a11y.open-nav'));
  scrim.hidden = !visible;
  panel.inert = isMobileNav() && !visible;
}

function closeMobileNav() {
  if (isMobileNav()) {
    setMobileNav(false);
  }
}

function describeComparison() {
  const states = {
    wikimedia: 'algorithms.fixture-wikimedia',
    robust: 'algorithms.fixture-robust',
    ewma: 'algorithms.fixture-ewma',
  };
  $('#compare-description').textContent = t(states[$('#compare-first').value]) + ' · '
    + t(states[$('#compare-second').value]) + ' · ' + t('algorithms.fixture-note');
}

function bindEvents() {
  $('#menu-toggle').addEventListener('click', () => setMobileNav(!$('#app-shell').classList.contains('menu-open')));
  $('#nav-scrim').addEventListener('click', closeMobileNav);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeMobileNav();
    }
  });
  window.addEventListener('resize', () => setMobileNav($('#app-shell').classList.contains('menu-open')));
  window.addEventListener('hashchange', () => showView(window.location.hash.slice(1)));
  $$('[data-nav]').forEach((link) => link.addEventListener('click', closeMobileNav));
  $$('[data-goto]').forEach((link) => link.addEventListener('click', closeMobileNav));
  $$('[data-close]').forEach((button) => {
    button.addEventListener('click', () => document.getElementById(button.dataset.close).close());
  });

  $('#load-run').addEventListener('click', () => loadRun($('#run-id').value));
  $('#open-fetch').addEventListener('click', () => {
    if (state.fetchReady) {
      fetchController.activate();
    }
  });
  $('#run-id').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      loadRun(event.currentTarget.value);
    }
  });

  $$('.filter-tab').forEach((button) => button.addEventListener('click', () => {
    $$('.filter-tab').forEach((tab) => {
      tab.classList.remove('active');
      tab.setAttribute('aria-pressed', 'false');
    });
    button.classList.add('active');
    button.setAttribute('aria-pressed', 'true');
    state.category = button.dataset.category;
    state.offset = 0;
    if (state.run) renderSyntheticDiscovery(state.run);
    loadKeywords();
  }));
  let searchTimer;
  $('#search').addEventListener('input', (event) => {
    clearTimeout(searchTimer);
    const query = event.currentTarget.value;
    searchTimer = setTimeout(() => {
      state.query = query;
      state.offset = 0;
      if (state.run) renderSyntheticDiscovery(state.run);
      loadKeywords();
    }, 350);
  });
  $('#sort').addEventListener('change', (event) => {
    state.sort = event.currentTarget.value;
    state.offset = 0;
    loadKeywords();
  });
  $('#prev').addEventListener('click', () => {
    state.offset = Math.max(0, state.offset - state.limit);
    loadKeywords();
  });
  $('#next').addEventListener('click', () => {
    if (state.next !== null && state.next !== undefined) {
      state.offset = state.next;
      loadKeywords();
    }
  });
  $('#compare-first').addEventListener('change', describeComparison);
  $('#compare-second').addEventListener('change', describeComparison);
  $('#detail-dialog').addEventListener('close', () => {
    state.detailSequence += 1;
    state.detail = null;
  });
  window.addEventListener('pagehide', () => {
    clearTimeout(state.pollTimer);
    clearTimeout(searchTimer);
    clearTimeout(state.capabilityRetryTimer);
    fetchController.dispose();
  });
  document.addEventListener('trendscout:locale', () => {
    if (state.run) {
      updateRun(state.run);
    }
    describeComparison();
    if (state.dataRunId) {
      loadKeywords();
    }
    if ($('#detail-dialog').open && state.detail) {
      renderDetail(state.detail);
    }
    if (state.capabilities) {
      renderSourceCapabilities(state.capabilities, t);
    }
    renderFetchPresentation();
  });
}

async function checkBackend() {
  let connected = false;
  try {
    await getJson('/api/health', { locale: getLocale() });
    connected = true;
    $('#connection-dot').className = 'status-dot status-dot--green';
    $('#connection-text').textContent = t('connection.online');
  } catch {
    $('#connection-dot').className = 'status-dot status-dot--red';
    $('#connection-text').textContent = t('connection.offline');
  }
  document.addEventListener('trendscout:locale', () => {
    $('#connection-text').textContent = t(connected ? 'connection.online' : 'connection.offline');
  });
}

initializeLocale();
initializeTheme();
bindEvents();
showView(window.location.hash.slice(1));
describeComparison();
setMobileNav(false);
document.documentElement.classList.remove('js-pending');
checkBackend();
loadPublicCapabilities();
