import { getJson, requestJson } from './modules/api.js';
import { acceptsDiscovery, canStartFetch, isLocalLiveDemo, loadCapabilities, renderSourceCapabilities } from './modules/capabilities.js';
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
  publicFetchDiscovery: null,
  publicFetchSources: [],
  publicFetchFinishedAt: null,
  category: 'all',
  query: '',
  sort: 'recent',
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
  collections: [],
  collectionInbox: [],
  collectionSuggestions: [],
  collectionQuery: '',
  collectionJobStatus: null,
  collectionInboxTotal: 0,
  collectionSelected: new Set(),
  collectionMembershipSelected: {},
  algorithmCatalog: null,
  algorithmResults: null,
  persistedDataset: null,
  persistedDatasetAvailable: false,
  collectionFilters: new Set(),
  selectedKeywordIds: new Set(),
  selectAllFiltered: false,
  latestKeywordTotal: 0,
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
  const liveDemo = isLocalLiveDemo(state.capabilities);
  badge.textContent = t(state.fetchReady
    ? (liveDemo ? 'fetch.live-label' : 'fetch.synthetic-label') : 'fetch.unavailable');
  badge.classList.toggle('pill--green', state.fetchReady);
  badge.classList.toggle('pill--amber', !state.fetchReady);
  readiness.textContent = state.fetchReady ? t(liveDemo ? 'fetch.live-ready' : 'fetch.ready') : t(blockKey);
  status.hidden = !isActionState;
  const sourceSummary = Array.isArray(presentation.fetch?.sources)
    ? presentation.fetch.sources
      .filter((source) => source && typeof source === 'object')
      .map((source) => {
        const sourceKey = FETCH_SOURCE_LABELS[source.source_id];
        const sourceLabel = sourceKey ? t(sourceKey) : source.source_id;
        const visibleStatus = source.error_code === 'RATE_LIMITED' ? 'RATE_LIMITED' : source.status;
        const statusKey = `fetch.source-status.${visibleStatus}`;
        const sourceStatus = source.status && t(statusKey) !== statusKey
          ? t(statusKey) : source.status;
        return [sourceLabel, sourceStatus, source.data_mode, source.delivery_state,
          Number.isInteger(source.retry_after_seconds)
            ? t('fetch.retry-after', { seconds: source.retry_after_seconds }) : '']
          .filter((value) => typeof value === 'string' && value).join(' · ');
      })
      .join(' | ')
    : '';
  status.textContent = isActionState
    ? [t(liveDemo ? liveFetchMessage(presentation.kind) || messageKey : messageKey),
      sourceSummary ? t('fetch.source-summary', { sources: sourceSummary }) : '']
      .filter(Boolean).join(' ')
    : '';
}

function refreshCollectionJob(fetchId) {
  if (!isLocalLiveDemo(state.capabilities) || !fetchId) return;
  const poll = async (attempt = 0) => {
    try {
      const job = await getJson(`/api/v1/collections/jobs/${encodeURIComponent(fetchId)}`, { locale: getLocale() });
      if (job.status === 'PROCESSING' && attempt < 120) {
        window.setTimeout(() => poll(attempt + 1), 1000);
        return;
      }
      state.collectionJobStatus = job;
      if (job.status === 'MODEL_UNAVAILABLE') {
        $('#collections-status').textContent = t('collections.model-unavailable');
      } else if (job.status === 'COMPLETED') {
        $('#collections-status').textContent = t('collections.coverage', job);
      }
      await loadLatestDataset();
      if (state.persistedDatasetAvailable) loadKeywords();
      if (window.location.hash === '#collections') loadCollections();
    } catch {}
  };
  poll();
}

function liveFetchMessage(kind) {
  return ({
    submitting: 'fetch.submitting-live', queued: 'fetch.queued-live',
    running: 'fetch.running-live', partial: 'fetch.partial-live',
    completed: 'fetch.completed-live', failed: 'fetch.failed-live',
  })[kind] || null;
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
    state.fetchReady = canStartFetch(capabilities);
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
  onTerminal(runId, fetchStatus) {
    if (runId) {
      state.publicFetchDiscovery = null;
      state.publicFetchSources = [];
      state.publicFetchFinishedAt = null;
      loadRun(runId);
    } else if (acceptsDiscovery(state.capabilities, fetchStatus?.discovery)) {
      refreshCollectionJob(fetchStatus.fetch_id);
      const localLive = isLocalLiveDemo(state.capabilities);
      state.publicFetchDiscovery = localLive ? null : fetchStatus.discovery;
      state.publicFetchSources = Array.isArray(fetchStatus.sources) ? fetchStatus.sources : [];
      state.publicFetchFinishedAt = typeof fetchStatus.finished_at === 'string'
        ? fetchStatus.finished_at : null;
      state.runId = null;
      state.dataRunId = null;
      state.run = null;
      state.offset = 0;
      if (localLive) {
        showView('overview');
        window.location.hash = 'overview';
        void loadLatestDataset().then(() => loadKeywords());
      } else {
        renderPublicFetchDiscovery();
        showView('collections');
        loadKeywords();
      }
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

function safeExternalEvidenceUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password
      && !url.port && url.href.length <= 2048 ? url.href : null;
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
  $('#metric-records-returned').textContent = '—';
  $('#metric-records-returned-note').textContent = t('metric.not-reported');
  $('#metric-records-stored').textContent = '—';
  $('#metric-records-stored-note').textContent = t('metric.not-reported');
  $('#metric-topics-found').textContent = number(run.keyword_count);
  $('#metric-topics-found-note').textContent = t('metric.from-analysis');
  $('#metric-observation-date').textContent = date(run.data_date);
  $('#metric-last-fetch').textContent = run.fetched_at ? t('metric.last-fetch', { value: timestamp(run.fetched_at) }) : t('metric.not-reported');

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
  renderDiscoveryBundle(bundle);
}

function renderPublicFetchDiscovery() {
  const bundle = state.publicFetchDiscovery
    ? { ...state.publicFetchDiscovery, sources: state.publicFetchSources }
    : null;
  if (state.publicFetchDiscovery) updatePublicDiscoverySummary(state.publicFetchDiscovery, state.publicFetchSources);
  renderDiscoveryBundle(bundle);
}

function updatePublicDiscoverySummary(discovery, sources) {
  const source = (Array.isArray(sources) ? sources : []).find((item) => item?.source_id === 'wikimedia_pageviews_top_per_country');
  const recordCount = Number.isInteger(source?.record_count) ? source.record_count : null;
  const omitted = Number.isInteger(source?.coverage?.records_omitted_storage_limit)
    ? source.coverage.records_omitted_storage_limit : null;
  const candidates = Array.isArray(discovery?.candidates) ? discovery.candidates : [];
  const observations = candidates.flatMap((candidate) => Array.isArray(candidate.evidence) ? candidate.evidence : []);
  const observedDate = observations.find((item) => typeof item.observed_at === 'string')?.observed_at?.slice(0, 10);
  const fetchedAt = observations.find((item) => typeof item.fetched_at === 'string')?.fetched_at;
  const delivery = source?.delivery_state || observations[0]?.delivery_mode || 'FRESH';
  $('#metric-records-returned').textContent = recordCount == null ? '—' : number(recordCount + (omitted || 0));
  $('#metric-records-returned-note').textContent = t('metric.provider-accepted');
  $('#metric-records-stored').textContent = recordCount == null ? '—' : number(recordCount);
  $('#metric-records-stored-note').textContent = omitted == null ? t('metric.local-storage')
    : t('metric.storage-omitted', { count: number(omitted) });
  $('#metric-topics-found').textContent = number(discovery?.candidate_count ?? 0);
  $('#metric-topics-found-note').textContent = t('metric.candidates-shown', { count: number(discovery?.shown_count ?? candidates.length) });
  $('#metric-observation-date').textContent = date(observedDate);
  const lastFetch = state.publicFetchFinishedAt || fetchedAt;
  $('#metric-last-fetch').textContent = lastFetch
    ? t('metric.last-fetch', { value: timestamp(lastFetch) }) : t('metric.fetch-to-begin');
  const context = [t('chart.scope'), 'LIVE', delivery, observedDate ? date(observedDate) : '—',
    fetchedAt ? t('overview.context-source-fetched', { value: timestamp(fetchedAt) }) : '—'].join(' · ');
  $('#overview-context').replaceChildren(element('span', '◉', 'meta-mark'), element('span', context));

  const unique = new Map();
  for (const item of observations) {
    if (item.source_id !== 'wikimedia_pageviews_top_per_country'
        || !Number.isInteger(item.approximate_views) || item.approximate_views < 0) continue;
    const key = [item.project, item.article, item.observed_at, item.scope].join('|');
    if (!unique.has(key)) unique.set(key, item);
  }
  const chartRows = [...unique.values()].sort((a, b) => (a.provider_rank ?? 9999) - (b.provider_rank ?? 9999)).slice(0, 8);
  const chart = $('#pageviews-chart');
  chart.replaceChildren();
  $('#pageviews-empty').hidden = chartRows.length > 0;
  if (chartRows.length) {
    const max = Math.max(...chartRows.map((item) => item.approximate_views), 1);
    for (const item of chartRows) {
      const row = element('li', null, 'chart-row');
      const label = element('span', item.article || '—', 'chart-label');
      const track = element('span', null, 'chart-track');
      const bar = element('span', null, 'chart-bar');
      bar.style.width = `${Math.max(3, (item.approximate_views / max) * 100)}%`;
      track.append(bar);
      row.append(label, track, element('strong', number(item.approximate_views), 'chart-value'));
      chart.append(row);
    }
  }
}

function renderDiscoveryBundle(bundle) {
  const sourcePanel = $('#demo-source-results');
  const panel = $('#demo-discovery');
  sourcePanel.hidden = !bundle;
  panel.hidden = !bundle;
  const sourceList = $('#demo-source-list');
  const list = $('#demo-discovery-list');
  sourceList.replaceChildren();
  list.replaceChildren();
  if (!bundle) return;
  const liveData = bundle.data_mode === 'LIVE';
  $('#demo-discovery-title').textContent = t(liveData ? 'demo.live-discovery-title' : 'demo.discovery-title');
  $('#demo-discovery-help').textContent = t(liveData ? 'demo.live-discovery-help' : 'demo.discovery-help');
  if (bundle.error) {
    sourceList.append(element('li', t('demo.failed')));
    $('#demo-discovery-count').textContent = t('demo.failed');
    return;
  }
  for (const source of Array.isArray(bundle.sources) ? bundle.sources : []) {
    const name = ({ wikimedia_pageviews_top_per_country: 'Wikimedia',
      google_web_serp: 'Google / SerpApi', bing_web_serp: 'Bing / SerpApi',
      youtube_video_search: 'YouTube' })[source.source_id] || source.source_id || '—';
    const sourceStatus = source.error_code === 'RATE_LIMITED'
      ? t('fetch.source-status.RATE_LIMITED') : source.status;
    sourceList.append(element('li', `${name} · ${source.data_mode || '—'} · ${sourceStatus} · ${source.record_count ?? 0} · ${source.delivery_state || source.stop_reason || '—'}`));
  }
  const category = state.category === 'home_goods' ? 'home_living' : state.category;
  const query = state.query.normalize('NFC').trim().toLocaleLowerCase();
  const candidates = (Array.isArray(bundle.candidates) ? bundle.candidates : []).filter((candidate) => {
    const categories = Array.isArray(candidate.categories) ? candidate.categories : [];
    return (!query || String(candidate.keyword || '').normalize('NFC').toLocaleLowerCase().includes(query))
      && (category === 'all' || categories.includes(category));
  });
  $('#demo-discovery-count').textContent = t(liveData ? 'demo.live-count' : 'demo.count', {
    shown: candidates.length, total: bundle.candidate_count ?? 0,
  });
  for (const candidate of candidates) {
    const item = element('li');
    item.append(element('strong', candidate.keyword));
    const assessments = Array.isArray(candidate.assessments) ? candidate.assessments : [];
    const classifications = [...new Set(assessments.map((assessment) => assessment.classification))];
    const status = classifications.length ? classifications.join(', ')
      : localizedStatus('keywordstatus.', candidate.status || 'INSUFFICIENT_DATA');
    item.append(element('span', ` · ${candidate.data_mode || '—'} · ${status}`));
    const details = element('details');
    details.append(element('summary', t('demo.evidence')));
    const evidenceList = element('ul');
    for (const evidence of Array.isArray(candidate.evidence) ? candidate.evidence : []) {
      const observedDate = typeof evidence.observed_at === 'string'
        ? evidence.observed_at.slice(0, 10) : evidence.observed_at;
      evidenceList.append(element('li', [evidence.source_id, evidence.market_evidence, evidence.data_mode,
        evidence.delivery_mode, date(observedDate), timestamp(evidence.fetched_at), evidence.scope,
        evidence.article ? t('demo.evidence-article', { value: evidence.article }) : '',
        evidence.project ? t('demo.evidence-project', { value: evidence.project }) : '',
        Number.isInteger(evidence.provider_rank) ? t('demo.evidence-rank', { value: evidence.provider_rank }) : '',
        Number.isInteger(evidence.approximate_views) ? t('demo.evidence-views', { value: number(evidence.approximate_views) }) : '',
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
    state.publicFetchDiscovery = null;
    state.publicFetchSources = [];
    state.publicFetchFinishedAt = null;
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

function appendCell(row, value, className = '', label = '') {
  const cell = element('td');
  if (label) cell.dataset.label = label;
  cell.append(element('span', value, className));
  row.append(cell);
}

function renderRows(items) {
  const body = $('#rows');
  body.replaceChildren();
  for (const item of items) {
    const row = element('tr');
    const selectCell = element('td');
    const checkbox = element('input');
    checkbox.type = 'checkbox';
    const keywordId = item.id || item.keyword_id;
    checkbox.dataset.keywordId = keywordId || '';
    checkbox.disabled = !keywordId;
    checkbox.checked = Boolean(keywordId && state.selectedKeywordIds.has(keywordId));
    checkbox.setAttribute('aria-label', `Select ${item.display_keyword || item.keyword || 'keyword'}`);
    checkbox.addEventListener('change', () => {
      const id = item.id || item.keyword_id;
      if (checkbox.checked) state.selectedKeywordIds.add(id);
      else state.selectedKeywordIds.delete(id);
      state.selectAllFiltered = false;
      updateKeywordBulkUi();
    });
    selectCell.append(checkbox);
    row.append(selectCell);
    const title = element('td');
    title.dataset.label = t('table.topic');
    title.append(element('strong', item.display_keyword || item.canonical_keyword || item.keyword || '—'));
    title.append(element('small', item.project || '—'));
    row.append(title);
    const category = Array.isArray(item.memberships) && item.memberships.length
      ? item.memberships.map((membership) => `${membership.name} (${t(membership.origin === 'LOCAL_AI' ? 'dataset.ai' : 'dataset.manual')})`).join(', ')
      : Array.isArray(item.categories) && item.categories.length
        ? item.categories.map((value) => t('category.' + value)).join(', ') : t(item.eligible === 0 ? 'dataset.excluded' : 'dataset.unassigned');
    appendCell(row, category, '', t('table.category'));
    const evidence = item.current_views_ceil != null
      ? t('table.views-evidence', { value: number(item.current_views_ceil), rank: item.provider_rank ?? '—' })
      : item.score == null ? t('table.no-comparable-evidence') : number(item.score) + ' / 100';
    appendCell(row, evidence, 'score-cell', t('table.evidence'));
    appendCell(row, date(item.observation_date), '', t('table.date'));
    const status = item.status || '—';
    appendCell(row, localizedStatus('keywordstatus.', status), 'pill table-status ' + (
      ['EMERGING', 'STABLE', 'FALLING', 'INSUFFICIENT_DATA'].includes(status) ? 'pill--' + status.toLowerCase() : ''
    ), t('table.status'));
    const action = element('td');
    const button = element('button', '↗', 'icon-button row-detail');
    button.type = 'button';
    button.setAttribute('aria-label', t('table.open-detail', {
      name: item.display_keyword || item.canonical_keyword || t('common.keyword'),
    }));
    button.disabled = !state.dataRunId;
    if (button.disabled) button.title = 'Detailed run evidence is unavailable for this cached ledger row.';
    button.addEventListener('click', () => openDetail(item.keyword_id || item.id));
    action.append(button);
    row.append(action);
    body.append(row);
  }
  const selectPage = $('#keyword-select-page');
  if (selectPage) selectPage.checked = items.length > 0 && items.every((item) => state.selectedKeywordIds.has(item.id || item.keyword_id));
  updateKeywordBulkUi();
}

function currentDatasetQuery() {
  const params = new URLSearchParams({ q: state.query, state: $('#keyword-state-filter')?.value || 'all',
    sort: state.sort === 'score' ? 'recent' : state.sort, limit: String(state.limit), offset: String(state.offset) });
  for (const id of state.collectionFilters) params.append('collection_ids', id);
  return params;
}

function updateKeywordBulkUi() {
  const toolbar = $('#keyword-bulk-toolbar');
  if (!toolbar) return;
  toolbar.hidden = !state.persistedDatasetAvailable;
  const count = state.selectAllFiltered ? state.latestKeywordTotal : state.selectedKeywordIds.size;
  $('#keyword-selection-count').textContent = t('dataset.selected', { count });
  $('#keyword-filter-total').textContent = state.selectAllFiltered
    ? t('dataset.all-filtered-selected', { count: number(state.latestKeywordTotal) }) : '';
  $('#keyword-select-page').disabled = state.selectAllFiltered;
}

function renderCollectionFilters() {
  const box = $('#keyword-collection-filters');
  if (!box) return;
  box.replaceChildren();
  for (const collection of state.collections) {
    const label = element('label');
    const checkbox = element('input');
    checkbox.type = 'checkbox';
    checkbox.value = collection.id;
    checkbox.checked = state.collectionFilters.has(collection.id);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) state.collectionFilters.add(collection.id);
      else state.collectionFilters.delete(collection.id);
      state.offset = 0;
      state.selectedKeywordIds.clear();
      state.selectAllFiltered = false;
      loadKeywords();
    });
    label.append(checkbox, element('span', collection.name));
    box.append(label);
  }
}

async function loadLatestDataset() {
  if (!isLocalLiveDemo(state.capabilities)) return null;
  try {
    const response = await getJson('/api/v1/datasets/latest', { locale: getLocale() });
    state.persistedDataset = response.item || null;
    state.persistedDatasetAvailable = Boolean(response.item);
    if (response.item) {
      const item = response.item;
      const sources = (item.sources || []).join(', ') || 'Unknown source';
      const status = item.status === 'LEGACY_CACHED'
        ? 'Legacy cached local ledger; historical coverage is unknown.' : `${item.status} snapshot`;
      const snapshotCoverage = item.snapshot_retained_keyword_count == null ? ''
        : ` · latest batch ${number(item.snapshot_retained_keyword_count)}; ledger ${number(item.retained_keyword_count || 0)}`;
      const ledgerCaveat = item.ledger_coverage_status === 'MIXED_OR_UNKNOWN'
        || item.status === 'LEGACY_CACHED' ? ' · earlier ledger coverage is unknown' : '';
      $('#overview-context').textContent = `${status}${snapshotCoverage}${ledgerCaveat} · ${sources} · ${number(item.eligible_keyword_count || 0)} eligible · ${number(item.unassigned_keyword_count || 0)} unassigned · ${item.fetched_at || 'collection time unavailable'}`;
      $('#metric-records-returned').textContent = item.provider_record_count == null ? 'Unknown' : number(item.provider_record_count);
      $('#metric-records-returned-note').textContent = item.status === 'LEGACY_CACHED' ? 'Legacy fetch count; coverage may be incomplete' : 'Provider-reported for this snapshot';
      $('#metric-records-stored').textContent = number(item.retained_keyword_count || 0);
      $('#metric-records-stored-note').textContent = 'Cached in the local keyword ledger';
      $('#metric-topics-found').textContent = number(item.eligible_keyword_count || 0);
      $('#metric-topics-found-note').textContent = `${number(item.assigned_keyword_count || 0)} assigned · ${number(item.unassigned_keyword_count || 0)} unassigned`;
      $('#metric-observation-date').textContent = [item.observation_start, item.observation_end].filter(Boolean).join(' – ') || 'Unknown';
      $('#metric-last-fetch').textContent = item.fetched_at ? `Cached snapshot · ${item.fetched_at}` : 'Cached ledger; time unavailable';
      $('#main-source-status').textContent = item.status === 'LEGACY_CACHED' ? 'LEGACY CACHE' : item.status;
      $('#keyword-count').textContent = number(item.retained_keyword_count || 0);
      $('#scored-count').textContent = number(item.assigned_keyword_count || 0);
      $('#results-count').textContent = number(item.eligible_keyword_count || 0);
      await loadCollections();
      renderCollectionFilters();
      return item;
    }
    $('#overview-context').textContent = t('overview.no-fetch-context');
    return null;
  } catch (error) {
    state.persistedDatasetAvailable = false;
    setNotice('#keyword-notice', getErrorMessage(error, 'error.keyword-fetch'));
    return null;
  }
}

function showEmpty(title, detail) {
  const box = $('#empty');
  box.hidden = false;
  $('strong', box).textContent = title;
  $('p', box).textContent = detail;
}

async function loadKeywords() {
  if (state.publicFetchDiscovery) {
    const category = state.category === 'home_goods' ? 'home_living' : state.category;
    const query = state.query.normalize('NFC').trim().toLocaleLowerCase();
    let candidates = Array.isArray(state.publicFetchDiscovery.candidates)
      ? state.publicFetchDiscovery.candidates.filter((candidate) => {
        const categories = Array.isArray(candidate.categories) ? candidate.categories : [];
        return (!query || String(candidate.keyword || '').normalize('NFC').toLocaleLowerCase().includes(query))
          && (category === 'all' || categories.includes(category));
      })
      : [];
    const visibleLimit = Number.isInteger(state.publicFetchDiscovery.shown_count)
      ? state.publicFetchDiscovery.shown_count : candidates.length;
    const totalCandidates = Number.isInteger(state.publicFetchDiscovery.candidate_count)
      ? state.publicFetchDiscovery.candidate_count : candidates.length;
    const coverageNote = $('#candidate-coverage-note');
    coverageNote.hidden = totalCandidates <= visibleLimit;
    if (!coverageNote.hidden) {
      coverageNote.textContent = t('keywords.candidate-limit', {
        shown: number(visibleLimit), total: number(totalCandidates),
      });
    }
    if (state.sort === 'keyword') {
      candidates = [...candidates].sort((left, right) => String(left.keyword).localeCompare(String(right.keyword)));
    } else if (state.sort === 'views') {
      const views = (candidate) => Math.max(0, ...(candidate.evidence || []).map((evidence) => evidence.approximate_views || 0));
      candidates = [...candidates].sort((left, right) => views(right) - views(left));
    }
    const rows = candidates.map((candidate) => {
      const sources = [...new Set((candidate.evidence || []).map((item) => item.source_id))];
      const evidence = candidate.evidence?.find((item) => item.source_id === 'wikimedia_pageviews_top_per_country') || candidate.evidence?.[0];
      const sourceLabel = sources.map((sourceId) => {
        const key = FETCH_SOURCE_LABELS[sourceId];
        return key ? t(key) : sourceId;
      }).join(' · ');
      return {
        ...candidate,
        display_keyword: candidate.keyword,
        canonical_keyword: candidate.keyword,
        project: [sourceLabel, evidence?.project].filter(Boolean).join(' · '),
        observation_date: evidence?.observed_at?.slice(0, 10),
        score: null,
        status: candidate.status || 'INSUFFICIENT_DATA',
        current_views_ceil: evidence?.approximate_views ?? null,
        provider_rank: evidence?.provider_rank ?? null,
        baseline: null,
        ratio: null,
      };
    });
    const page = rows.slice(state.offset, state.offset + state.limit);
    renderRows(page);
    state.next = state.offset + state.limit < rows.length ? state.offset + state.limit : null;
    $('#results-count').textContent = number(rows.length);
    $('#pagination-info').textContent = rows.length
      ? t('table.range', {
        start: number(state.offset + 1),
        end: number(state.offset + page.length),
        total: number(rows.length),
      })
      : t('table.zero-results');
    $('#prev').disabled = state.offset === 0;
    $('#next').disabled = state.next == null;
    if (!page.length) {
      showEmpty(t('keywords.empty-title'), t('keywords.page-empty'));
    } else {
      $('#empty').hidden = true;
    }
    setNotice('#keyword-notice', '');
    return;
  }
  $('#candidate-coverage-note').hidden = true;
  if (state.persistedDatasetAvailable && !state.dataRunId) {
    const sequence = ++state.keywordSequence;
    try {
      const result = await getJson('/api/v1/datasets/latest/keywords?' + currentDatasetQuery(), { locale: getLocale() });
      if (sequence !== state.keywordSequence) return;
      const rows = result.items.map((item) => {
        const evidence = item.evidence && typeof item.evidence === 'object' ? item.evidence : {};
        const evidenceItems = Array.isArray(evidence.evidence) ? evidence.evidence : [];
        const primary = evidenceItems.find((entry) => entry.source_id === 'wikimedia_pageviews_top_per_country') || evidenceItems[0] || evidence;
        const sources = [...new Set([item.source, ...evidenceItems.map((entry) => entry.source_id)].filter(Boolean))];
        return {
          ...item,
          keyword_id: item.id,
          display_keyword: item.keyword,
          canonical_keyword: item.keyword,
          project: [sources.join(' · '), item.project].filter(Boolean).join(' · '),
          observation_date: item.observed_at?.slice(0, 10),
          score: null,
          status: item.eligible === 0 ? 'EXCLUDED' : 'INSUFFICIENT_DATA',
          current_views_ceil: primary.views_ceil ?? primary.approximate_views ?? null,
          provider_rank: primary.provider_rank ?? null,
        };
      });
      renderRows(rows);
      state.next = result.next_offset;
      state.latestKeywordTotal = result.total;
      updateKeywordBulkUi();
      $('#results-count').textContent = number(result.total);
      $('#pagination-info').textContent = result.total ? t('table.range', {
        start: number(state.offset + 1), end: number(state.offset + rows.length), total: number(result.total),
      }) : t('table.zero-results');
      $('#prev').disabled = state.offset === 0;
      $('#next').disabled = result.next_offset == null;
      if (!rows.length) showEmpty(t('keywords.empty-title'), t('keywords.page-empty'));
      else $('#empty').hidden = true;
      setNotice('#keyword-notice', '');
    } catch (error) {
      if (sequence === state.keywordSequence) {
        renderRows([]);
        setNotice('#keyword-notice', getErrorMessage(error, 'error.keyword-fetch'), 'error');
      }
    }
    return;
  }
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
  if (detail?.__publicFetchCandidate === true) {
    renderPublicCandidateDetail(detail);
    return;
  }
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

function renderPublicCandidateDetail(candidate) {
  const content = $('#detail-content');
  content.replaceChildren();
  $('#detail-title').textContent = candidate.keyword || t('detail.title-fallback');
  const sources = [...new Set((candidate.evidence || []).map((item) => item.source_id))];
  $('#detail-subtitle').textContent = [sources.join(' · '), candidate.data_mode || '—'].join(' · ');
  detailField(content, t('detail.score-status'), localizedStatus(
    'keywordstatus.', candidate.status || 'INSUFFICIENT_DATA'
  ));
  detailField(content, t('detail.algorithm'), 'phrase-rules-v1');
  detailField(content, t('detail.categories'), (candidate.categories || []).join(' · ') || 'unclassified');
  detailField(content, t('detail.quality-flags'), (candidate.quality_flags || []).join(' · ')
    || t('detail.no-flags'), true);
  for (const evidence of candidate.evidence || []) {
    const entry = element('div', null, 'detail-item detail-item--wide');
    entry.append(element('small', `${evidence.source_id} · ${evidence.market_evidence || 'UNKNOWN'}`));
    const details = [evidence.data_mode, evidence.delivery_mode, evidence.scope,
      evidence.article ? t('demo.evidence-article', { value: evidence.article }) : '',
      evidence.project ? t('demo.evidence-project', { value: evidence.project }) : '',
      Number.isInteger(evidence.provider_rank) ? t('demo.evidence-rank', { value: evidence.provider_rank }) : '',
      Number.isInteger(evidence.approximate_views) ? t('demo.evidence-views', { value: number(evidence.approximate_views) }) : '',
      evidence.observed_at, evidence.fetched_at, ...(evidence.query_evidence || [])]
      .filter(Boolean).join(' · ');
    entry.append(element('div', details));
    const url = safeExternalEvidenceUrl(evidence.source_url);
    if (url) {
      const link = element('a', t('detail.open-evidence'));
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      entry.append(link);
    }
    content.append(entry);
  }
  for (const assessment of candidate.assessments || []) {
    detailField(content, assessment.source_id,
      `${assessment.classification} · ${assessment.reason} · ${(assessment.limitations || []).join(', ')}`,
      true);
  }
}

async function openDetail(id) {
  if (!/^[a-f0-9]{64}$/i.test(id || '')) {
    return;
  }
  if (state.publicFetchDiscovery) {
    const candidate = state.publicFetchDiscovery.candidates?.find((item) => item.keyword_id === id);
    if (!candidate) return;
    const dialog = $('#detail-dialog');
    state.detail = { ...candidate, __publicFetchCandidate: true };
    renderDetail(state.detail);
    if (!dialog.open) dialog.showModal();
    return;
  }
  if (!state.dataRunId) return;
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
  const view = name === 'results' || name === 'keywords' ? 'overview' : name;
  if ((name === 'keywords' || name === 'results') && window.location.hash !== '#overview') {
    window.history.replaceState(null, '', '#overview');
  }
  const valid = new Set(['overview', 'collections', 'sources', 'runs', 'keywords', 'algorithms', 'quality', 'refresh', 'research', 'usage', 'settings']);
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
  if (selected === 'keywords' && !state.dataRunId && !state.publicFetchDiscovery && !state.persistedDatasetAvailable) {
    showEmpty(t('keywords.empty-title'), t('keywords.no-run'));
  }
  if (selected === 'collections') loadCollections();
  if (selected === 'keywords') loadKeywords();
  if (selected === 'algorithms') loadAlgorithmCatalog();
  closeMobileNav();
}

async function loadCollections({ appendInbox = false } = {}) {
  try {
    const inboxOffset = appendInbox ? state.collectionInbox.length : 0;
    const [data, inbox] = await Promise.all([
      getJson('/api/v1/collections', { locale: getLocale() }),
      getJson(`/api/v1/collections/inbox?limit=40&offset=${inboxOffset}`, { locale: getLocale() }),
    ]);
    state.collections = data.items || [];
    state.collectionSuggestions = data.suggestions || [];
    state.collectionInbox = appendInbox ? [...state.collectionInbox, ...(inbox.items || [])] : (inbox.items || []);
    state.collectionInboxTotal = inbox.total || 0;
    $('#collection-mode').value = data.mode || 'SUGGEST';
    renderCollections();
    renderCollectionFilters();
  } catch {
    $('#collections-status').textContent = t('collections.error');
    $('#collection-list').replaceChildren();
    $('#collection-inbox').replaceChildren();
  }
}

function renderCollections() {
  const query = state.collectionQuery.trim().toLocaleLowerCase();
  const container = $('#collection-list');
  container.replaceChildren();
  const items = state.collections.filter((item) => `${item.name} ${item.description}`.toLocaleLowerCase().includes(query));
  if (!items.length) container.append(element('p', t('collections.empty'), 'collection-empty'));
  items.forEach((item) => {
    const card = element('article', null, 'panel collection-card');
    const head = element('div', null, 'collection-card-head');
    const title = element('h2', item.name);
    const badge = element('span', t(item.created_by === 'AI' ? 'collections.ai' : 'collections.user'), 'pill pill--blue');
    head.append(title, badge);
    card.append(head, element('p', item.description || '—', 'collection-description'));
    card.append(element('p', `${item.membership_count || 0} · ${t('collections.open')}`, 'collection-count'));
    const autoMatch = element('button', t(item.auto_match ? 'collections.disable-auto' : 'collections.enable-auto'), 'text-button');
    autoMatch.type = 'button';
    autoMatch.setAttribute('aria-pressed', String(item.auto_match));
    autoMatch.addEventListener('click', async () => {
      const result = await requestJson(`/api/v1/collections/${encodeURIComponent(item.id)}`, {
        method: 'PATCH', body: { auto_match: !item.auto_match }, locale: getLocale(),
      });
      state.collectionJobStatus = result.reclassification;
      await loadCollections();
    });
    card.append(autoMatch, element('p', t(item.auto_match ? 'collections.auto-enabled' : 'collections.auto-disabled'), 'collection-description'));
    const actions = element('div', null, 'collection-actions');
    const edit = element('button', t('collections.edit'), 'button button-secondary');
    edit.type = 'button';
    edit.addEventListener('click', () => openCollectionDialog(item));
    const remove = element('button', t('collections.delete'), 'button button-secondary');
    remove.type = 'button';
    remove.addEventListener('click', async () => {
      if (!window.confirm(t('collections.confirm-delete'))) return;
      await requestJson(`/api/v1/collections/${encodeURIComponent(item.id)}`, { method: 'DELETE', locale: getLocale() });
      await loadCollections();
    });
    actions.append(edit, remove);
    card.append(actions);
    const keywords = element('div', null, 'collection-keywords');
    const selectedIds = state.collectionMembershipSelected[item.id]
      || (state.collectionMembershipSelected[item.id] = new Set());
    const bulkControls = element('div', null, 'collection-bulk');
    const selectAll = element('input');
    selectAll.type = 'checkbox';
    selectAll.setAttribute('aria-label', t('collections.select-page'));
    const approveSelected = element('button', t('collections.approve-selected'), 'button button-secondary');
    const rejectSelected = element('button', t('collections.reject-selected'), 'button button-secondary');
    approveSelected.type = rejectSelected.type = 'button';
    approveSelected.disabled = rejectSelected.disabled = !selectedIds.size;
    selectAll.addEventListener('change', async () => {
      const result = await getJson(`/api/v1/collections/${encodeURIComponent(item.id)}/keywords`, { locale: getLocale() });
      for (const keyword of result.items || []) {
        if (selectAll.checked) selectedIds.add(keyword.id);
        else selectedIds.delete(keyword.id);
      }
      await loadCollections();
    });
    const bulkAction = async (action) => {
      if (!selectedIds.size) return;
      const result = await requestJson('/api/v1/collections/bulk', {
        method: 'POST', body: { action, collection_id: item.id, keyword_ids: [...selectedIds] }, locale: getLocale(),
      });
      selectedIds.clear();
      $('#collections-status').textContent = t('collections.bulk-result', result);
      await loadCollections();
    };
    approveSelected.addEventListener('click', () => bulkAction('APPROVE'));
    rejectSelected.addEventListener('click', () => bulkAction('REJECT'));
    bulkControls.append(selectAll, element('span', t('collections.select-page')),
      approveSelected, rejectSelected);
    card.append(bulkControls);
    card.append(keywords);
    getJson(`/api/v1/collections/${encodeURIComponent(item.id)}/keywords`, { locale: getLocale() })
      .then((result) => (result.items || []).forEach((keyword) => {
        const row = element('div', null, 'collection-keyword');
        const checkbox = element('input');
        checkbox.type = 'checkbox';
        checkbox.checked = selectedIds.has(keyword.id);
        checkbox.setAttribute('aria-label', keyword.keyword);
        checkbox.addEventListener('change', () => {
          if (checkbox.checked) selectedIds.add(keyword.id);
          else selectedIds.delete(keyword.id);
          approveSelected.disabled = rejectSelected.disabled = !selectedIds.size;
        });
        let evidence = {};
        try { evidence = JSON.parse(keyword.evidence || '{}'); } catch {}
        const details = [keyword.source, keyword.project, keyword.observed_at,
          Number.isFinite(evidence.views_ceil) ? `${number(evidence.views_ceil)} views` : ''].filter(Boolean).join(' · ');
        const label = element('span', `${keyword.keyword} · ${keyword.origin} · ${keyword.reason || ''} · ${details}`);
        const destination = element('select');
        destination.setAttribute('aria-label', t('collections.copy'));
        state.collections.filter((value) => value.id !== item.id).forEach((collection) => {
          const option = element('option', collection.name);
          option.value = collection.id;
          destination.append(option);
        });
        const assign = async () => requestJson(`/api/v1/collections/${encodeURIComponent(destination.value)}/keywords`, {
          method: 'POST', body: { keyword_id: keyword.id }, locale: getLocale(),
        });
        const copy = element('button', t('collections.copy'), 'text-button');
        copy.type = 'button';
        copy.disabled = !destination.options.length;
        copy.addEventListener('click', async () => { await assign(); await loadCollections(); });
        const move = element('button', t('collections.move'), 'text-button');
        move.type = 'button';
        move.disabled = !destination.options.length;
        move.addEventListener('click', async () => {
          await assign();
          await requestJson(`/api/v1/collections/${encodeURIComponent(item.id)}/keywords/${encodeURIComponent(keyword.id)}`, { method: 'DELETE', locale: getLocale() });
          await loadCollections();
        });
        const unassign = element('button', t('collections.remove'), 'text-button');
        unassign.type = 'button';
        unassign.addEventListener('click', async () => {
          await requestJson(`/api/v1/collections/${encodeURIComponent(item.id)}/keywords/${encodeURIComponent(keyword.id)}`, { method: 'DELETE', locale: getLocale() });
          await loadCollections();
        });
        row.prepend(checkbox);
        row.append(label, destination, copy, move, unassign);
        keywords.append(row);
      })).catch(() => {});
    container.append(card);
  });
  const suggestions = $('#suggestion-list');
  suggestions.replaceChildren();
  state.collectionSuggestions.forEach((proposal) => {
      const card = element('article', null, 'panel collection-card');
      card.append(element('p', t('collections.suggestion'), 'section-kicker'));
      card.append(element('h2', proposal.name), element('p', proposal.description));
      const supportIds = JSON.parse(proposal.support_ids || '[]');
      card.append(element('p', `${t('collections.support')}: ${supportIds.length}`));
      const accept = element('button', t('collections.accept'), 'button button-primary');
      const reject = element('button', t('collections.reject'), 'button button-secondary');
      accept.type = reject.type = 'button';
      accept.addEventListener('click', async () => {
        await requestJson(`/api/v1/collections/suggestions/${encodeURIComponent(proposal.id)}/accept`, { method: 'POST', locale: getLocale() });
        await loadCollections();
      });
      reject.addEventListener('click', async () => {
        await requestJson(`/api/v1/collections/suggestions/${encodeURIComponent(proposal.id)}/reject`, { method: 'POST', locale: getLocale() });
        await loadCollections();
      });
      const actions = element('div', null, 'collection-actions');
      actions.append(accept, reject);
      card.append(actions);
      suggestions.append(card);
  });
  const inbox = $('#collection-inbox');
  inbox.replaceChildren();
  state.collectionInbox.forEach((keyword) => {
    const row = element('div', null, 'collection-inbox-row');
    const checkbox = element('input');
    checkbox.type = 'checkbox';
    checkbox.checked = state.collectionSelected.has(keyword.id);
    checkbox.setAttribute('aria-label', keyword.keyword);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) state.collectionSelected.add(keyword.id);
      else state.collectionSelected.delete(keyword.id);
      updateCollectionSelection();
    });
    row.append(element('span', `${keyword.keyword} · ${keyword.project} · ${keyword.observed_at}`));
    const select = element('select');
    select.setAttribute('aria-label', t('collections.open'));
    state.collections.forEach((collection) => {
      const option = element('option', collection.name);
      option.value = collection.id;
      select.append(option);
    });
    const add = element('button', t('collections.add'), 'button button-secondary');
    add.type = 'button';
    add.disabled = !state.collections.length;
    add.addEventListener('click', async () => {
      await requestJson(`/api/v1/collections/${encodeURIComponent(select.value)}/keywords`, {
        method: 'POST', body: { keyword_id: keyword.id }, locale: getLocale(),
      });
      await loadCollections();
    });
    row.prepend(checkbox);
    row.append(select, add);
    inbox.append(row);
  });
  if (!state.collectionInbox.length) inbox.append(element('p', t('collections.empty-inbox')));
  const more = $('#collection-inbox-more');
  more.hidden = state.collectionInbox.length >= state.collectionInboxTotal;
  more.disabled = state.collectionInbox.length >= state.collectionInboxTotal;
  $('#collection-unassigned-count').textContent = number(state.collectionInboxTotal);
  const bulkTarget = $('#collection-bulk-target');
  const previousTarget = bulkTarget.value;
  bulkTarget.replaceChildren();
  state.collections.forEach((collection) => {
    const option = element('option', collection.name);
    option.value = collection.id;
    bulkTarget.append(option);
  });
  if (state.collections.some((collection) => collection.id === previousTarget)) bulkTarget.value = previousTarget;
  const keywordTarget = $('#keyword-bulk-target');
  const previousKeywordTarget = keywordTarget.value;
  keywordTarget.replaceChildren(new Option(t('dataset.choose-collection'), ''));
  state.collections.forEach((collection) => keywordTarget.append(new Option(collection.name, collection.id)));
  if (state.collections.some((collection) => collection.id === previousKeywordTarget)) keywordTarget.value = previousKeywordTarget;
  bulkTarget.disabled = !state.collections.length;
  $('#collection-bulk-assign').disabled = !state.collections.length || !state.collectionSelected.size;
  $('#collection-select-page').checked = state.collectionInbox.length > 0
    && state.collectionInbox.every((keyword) => state.collectionSelected.has(keyword.id));
  updateCollectionSelection();
  $('#collections-status').textContent = state.collectionJobStatus?.status === 'COMPLETED'
    ? t('collections.coverage', state.collectionJobStatus) : t('collections.saved');
}

function updateCollectionSelection() {
  $('#collection-selection-count').textContent = t('collections.selected-count', { count: state.collectionSelected.size });
  const button = $('#collection-bulk-assign');
  if (button) button.disabled = !state.collections.length || !state.collectionSelected.size;
}

function isMobileNav() {
  return window.matchMedia('(max-width: 900px)').matches;
}

function openCollectionDialog(item = null) {
  const dialog = $('#collection-dialog');
  const form = $('#collection-form');
  form.dataset.collectionId = item?.id || '';
  $('#collection-dialog-title').textContent = t(item ? 'collections.edit' : 'collections.create');
  $('#collection-name').value = item?.name || '';
  $('#collection-description').value = item?.description || '';
  $('#collection-examples').value = (item?.examples || []).join('; ');
  $('#collection-exclusions').value = (item?.exclusions || []).join('; ');
  $('#collection-auto-match').checked = item?.auto_match ?? true;
  dialog.showModal();
  $('#collection-name').focus();
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

async function loadAlgorithmCatalog() {
  const root = $('#algorithm-catalog');
  root.replaceChildren(element('p', 'Loading algorithm registry…'));
  try {
    const [catalogResult, executionResult] = await Promise.all([
      getJson('/api/v1/algorithms', { locale: getLocale() }),
      getJson('/api/v1/algorithms/results', { locale: getLocale() }).catch(() => null),
    ]);
    state.algorithmCatalog = catalogResult;
    state.algorithmResults = executionResult;
    populateAlgorithmFilters(state.algorithmCatalog.algorithms || []);
    renderAlgorithmCatalog();
    $('#algorithm-evaluation-status').textContent = `${state.algorithmCatalog.evaluation_status}: ${state.algorithmCatalog.evaluation_reason}`;
  } catch (error) {
    root.replaceChildren(element('p', getErrorMessage(error, 'error.detail-fetch')));
  }
}

function populateAlgorithmFilters(items) {
  const family = $('#algorithm-family-filter');
  const source = $('#algorithm-source-filter');
  const priorFamily = family.value;
  const priorSource = source.value;
  const families = [...new Set(items.map((item) => item.family))].sort();
  const sources = [...new Set(items.flatMap((item) => item.sources.map((entry) => entry.source_id)))].sort();
  family.replaceChildren(new Option('All families', ''), ...families.map((value) => new Option(value, value)));
  source.replaceChildren(new Option('All sources', ''), ...sources.map((value) => new Option(value, value)));
  family.value = priorFamily;
  source.value = priorSource;
}

function renderAlgorithmCatalog() {
  const root = $('#algorithm-catalog');
  const all = state.algorithmCatalog?.algorithms || [];
  const family = $('#algorithm-family-filter').value;
  const source = $('#algorithm-source-filter').value;
  const status = $('#algorithm-status-filter').value;
  const methods = all.filter((item) => (!family || item.family === family)
    && (!source || item.sources.some((entry) => entry.source_id === source))
    && (!status || item.sources.some((entry) => entry.status === status)));
  root.replaceChildren();
  if (!methods.length) {
    root.append(element('p', 'No registered algorithms match these filters.'));
    return;
  }
  methods.forEach((item) => {
    const card = element('article', null, 'panel algorithm-card');
    const heading = element('div', null, 'panel-heading');
    const title = element('div');
    title.append(element('p', item.family.toUpperCase(), 'section-kicker'));
    title.append(element('h2', item.name));
    title.append(element('code', item.key));
    heading.append(title);
    const compatible = item.sources.filter((entry) => entry.status !== 'INCOMPATIBLE');
    heading.append(element('span', compatible.length ? compatible[0].status : 'INCOMPATIBLE', 'pill pill--gray'));
    card.append(heading);
    card.append(element('p', item.description));
    const counts = (state.algorithmResults?.summary || []).find((entry) => entry.algorithm_id === item.algorithm_id);
    card.append(element('p', counts
      ? `Persisted results: ${counts.completed} completed · ${counts.skipped} skipped · ${counts.failed} failed`
      : 'Persisted results: none yet', 'algorithm-execution-summary'));
    const examples = (state.algorithmResults?.items || []).filter((entry) => entry.algorithm_id === item.algorithm_id).slice(0, 3);
    if (examples.length) {
      const outputDetails = element('details', null, 'algorithm-details');
      outputDetails.append(element('summary', 'Latest persisted keyword results'));
      const outputList = element('ul');
      examples.forEach((entry) => {
        let result = {};
        try { result = JSON.parse(entry.result_json || '{}'); } catch { /* malformed old rows remain display-safe */ }
        const flags = Array.isArray(result.quality_flags) ? result.quality_flags.join(', ') : '';
        const message = [entry.keyword, entry.status,
          entry.value === null ? entry.reason_code : `${entry.value} ${entry.unit}`,
          flags].filter(Boolean).join(' · ');
        outputList.append(element('li', message));
      });
      outputDetails.append(outputList);
      card.append(outputDetails);
    }
    const sourceRow = element('div', null, 'algorithm-source-icons');
    item.sources.forEach((entry) => {
      const sourceBadge = element('span', null, 'algorithm-source-badge');
      sourceBadge.title = `${entry.source_id}: ${entry.status}${entry.reason ? ` · ${entry.reason}` : ''}`;
      sourceBadge.append(element('span', sourceGlyph(entry.source_id), 'algorithm-source-glyph'));
      sourceBadge.append(element('span', entry.source_id.replaceAll('_', ' ')));
      sourceRow.append(sourceBadge);
    });
    card.append(sourceRow);
    const detail = element('details', null, 'algorithm-details');
    detail.append(element('summary', 'Requirements, output and limitations'));
    const list = element('dl', null, 'detail-list');
    addAlgorithmDetail(list, 'Input', `${item.input_metric} (${item.input_unit})`);
    addAlgorithmDetail(list, 'Output', `${item.metric} (${item.unit})`);
    addAlgorithmDetail(list, 'Minimum samples', item.minimum_samples);
    addAlgorithmDetail(list, 'Windows (days)', (item.supported_windows || []).join(', '));
    addAlgorithmDetail(list, 'Missing data', item.missing_data);
    addAlgorithmDetail(list, 'Runtime', item.runtime_limit);
    addAlgorithmDetail(list, 'Interpretation', item.interpretation || 'Descriptive output.');
    addAlgorithmDetail(list, 'Limitations', (item.limitations || []).join(' '));
    detail.append(list);
    card.append(detail);
    root.append(card);
  });
}

function sourceGlyph(id) {
  if (id.startsWith('wikimedia')) return 'W';
  if (id.startsWith('google')) return 'G';
  if (id.startsWith('bing')) return 'B';
  if (id.startsWith('youtube')) return '▶';
  return '•';
}

function addAlgorithmDetail(list, label, value) {
  const row = element('div');
  row.append(element('dt', label), element('dd', value));
  list.append(row);
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
  $('#empty-fetch').addEventListener('click', () => {
    if (state.fetchReady) fetchController.activate();
    else { showView('overview'); window.location.hash = 'overview'; }
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
    if (state.publicFetchDiscovery) renderPublicFetchDiscovery();
    else if (state.run) renderSyntheticDiscovery(state.run);
    loadKeywords();
  }));
  let searchTimer;
  $('#search').addEventListener('input', (event) => {
    clearTimeout(searchTimer);
    const query = event.currentTarget.value;
    searchTimer = setTimeout(() => {
      state.query = query;
      state.offset = 0;
      state.selectedKeywordIds.clear();
      state.selectAllFiltered = false;
      if (state.publicFetchDiscovery) renderPublicFetchDiscovery();
      else if (state.run) renderSyntheticDiscovery(state.run);
      loadKeywords();
    }, 350);
  });
  $('#sort').addEventListener('change', (event) => {
    state.sort = event.currentTarget.value;
    state.offset = 0;
    loadKeywords();
  });
  $('#keyword-state-filter').addEventListener('change', () => {
    state.offset = 0;
    state.selectedKeywordIds.clear();
    state.selectAllFiltered = false;
    loadKeywords();
  });
  $('#keyword-select-page').addEventListener('change', (event) => {
    state.selectAllFiltered = false;
    for (const row of $$('#rows tr')) {
      const checkbox = $('input[type="checkbox"]', row);
      if (!checkbox || checkbox.disabled) continue;
      const id = checkbox.dataset.keywordId;
      if (event.currentTarget.checked) state.selectedKeywordIds.add(id);
      else state.selectedKeywordIds.delete(id);
    }
    updateKeywordBulkUi();
  });
  $('#keyword-select-filtered').addEventListener('click', () => {
    state.selectAllFiltered = true;
    state.selectedKeywordIds.clear();
    updateKeywordBulkUi();
  });
  $('#keyword-bulk-action').addEventListener('change', (event) => {
    const target = $('#keyword-bulk-target');
    target.disabled = ['EXCLUDE', 'RESTORE'].includes(event.currentTarget.value);
    target.required = !target.disabled;
  });
  $('#keyword-bulk-apply').addEventListener('click', async () => {
    if (!state.selectAllFiltered && !state.selectedKeywordIds.size) return;
    const action = $('#keyword-bulk-action').value;
    if (!['EXCLUDE', 'RESTORE'].includes(action) && !$('#keyword-bulk-target').value) {
      announce(t('dataset.choose-collection-error'));
      return;
    }
    const body = { action, keyword_ids: state.selectAllFiltered ? [] : [...state.selectedKeywordIds],
      all_filtered: state.selectAllFiltered };
    if (!['EXCLUDE', 'RESTORE'].includes(action)) body.collection_id = $('#keyword-bulk-target').value;
    const params = currentDatasetQuery();
    body.query = params.get('q');
    body.state = params.get('state');
    body.source = params.get('source') || '';
    body.collection_ids = params.getAll('collection_ids');
    const result = await requestJson('/api/v1/collections/bulk', {
      method: 'POST', body, locale: getLocale(),
    });
    state.selectedKeywordIds.clear();
    state.selectAllFiltered = false;
    $('#keyword-filter-total').textContent = `${result.updated} updated · ${result.skipped} skipped`;
    await loadLatestDataset();
    await loadKeywords();
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
  ['#algorithm-family-filter', '#algorithm-source-filter', '#algorithm-status-filter']
    .forEach((selector) => $(selector).addEventListener('change', renderAlgorithmCatalog));
  $('#collection-create').addEventListener('click', () => openCollectionDialog());
  $('#collection-dialog-close').addEventListener('click', () => $('#collection-dialog').close());
  $('#collection-dialog-cancel').addEventListener('click', () => $('#collection-dialog').close());
  $('#collection-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const collectionId = form.dataset.collectionId;
    const splitExamples = (selector) => $(selector).value.split(';').map((value) => value.trim()).filter(Boolean).slice(0, 30);
    const body = {
      name: $('#collection-name').value.trim(),
      description: $('#collection-description').value.trim(),
      examples: splitExamples('#collection-examples'),
      exclusions: splitExamples('#collection-exclusions'),
      auto_match: $('#collection-auto-match').checked,
    };
    const result = await requestJson(collectionId
      ? `/api/v1/collections/${encodeURIComponent(collectionId)}` : '/api/v1/collections', {
      method: collectionId ? 'PATCH' : 'POST', body, locale: getLocale(),
    });
    state.collectionJobStatus = result.reclassification;
    $('#collection-dialog').close();
    await loadCollections();
  });
  $('#collection-search').addEventListener('input', (event) => {
    state.collectionQuery = event.currentTarget.value;
    renderCollections();
  });
  $('#collection-mode').addEventListener('change', async (event) => {
    await requestJson('/api/v1/collections/mode', { method: 'POST', body: { mode: event.currentTarget.value }, locale: getLocale() });
  });
  $('#collections-refresh').addEventListener('click', async () => {
    const result = await requestJson('/api/v1/collections/organize', { method: 'POST', locale: getLocale() });
    $('#collections-status').textContent = `${result.status} · ${result.processed || 0} / ${result.available || 0} · ${result.assigned || 0} · ${result.unassigned || 0}`;
    await loadCollections();
  });
  $('#collection-inbox-more').addEventListener('click', () => loadCollections({ appendInbox: true }));
  $('#collection-select-page').addEventListener('change', (event) => {
    for (const keyword of state.collectionInbox) {
      if (event.currentTarget.checked) state.collectionSelected.add(keyword.id);
      else state.collectionSelected.delete(keyword.id);
    }
    renderCollections();
  });
  $('#collection-bulk-assign').addEventListener('click', async () => {
    if (!state.collectionSelected.size || !$('#collection-bulk-target').value) return;
    const result = await requestJson('/api/v1/collections/bulk', {
      method: 'POST', body: { action: 'ADD', collection_id: $('#collection-bulk-target').value,
        keyword_ids: [...state.collectionSelected] }, locale: getLocale(),
    });
    state.collectionSelected.clear();
    $('#collections-status').textContent = t('collections.bulk-result', result);
    await loadCollections();
  });
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
    if (state.publicFetchDiscovery) {
      renderPublicFetchDiscovery();
    }
    if (window.location.hash === '#algorithms') loadAlgorithmCatalog();
    if (state.dataRunId || state.publicFetchDiscovery || state.persistedDatasetAvailable) {
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
setMobileNav(false);
document.documentElement.classList.remove('js-pending');
checkBackend();
void loadPublicCapabilities().then(() => loadLatestDataset()).then(() => {
  if (state.persistedDatasetAvailable) loadKeywords();
});
