import { getJson, requestJson } from './modules/api.js';
import { acceptsDiscovery, canStartFetch, isLocalLiveDemo, loadCapabilities, renderSourceCapabilities } from './modules/capabilities.js';
import { PublicFetchController } from './modules/fetch.js';
import { formatDate, formatNumber, formatTimestamp } from './modules/format.js';
import { applyLocale, getLocale, initializeLocale, t } from './modules/i18n.js';
import { initializeTheme } from './modules/theme.js';
import { initializeKeywordWorkbench } from './modules/keyword-workbench.js';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const activeStatuses = new Set(['QUEUED', 'FETCHING', 'NORMALIZING', 'ANALYZING', 'PERSISTING']);
const workbench = initializeKeywordWorkbench();
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
  collectionSelectedId: null,
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
  analytics: null,
  runHistoryOffset: 0,
  runHistoryQuery: '',
  sourceLibrary: [],
  researchKeywords: [],
};
state.query = new URLSearchParams(window.location.search).get('q') || '';

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

function compactTimestamp(value) {
  const instant = new Date(value);
  if (!Number.isFinite(instant.valueOf())) return '—';
  return new Intl.DateTimeFormat(getLocale(), { dateStyle: 'short', timeStyle: 'short' }).format(instant);
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

function renderDataTable(root, headers, rows, note = '') {
  if (!root) return;
  root.replaceChildren();
  if (note) root.append(element('p', note, 'fine-print'));
  if (!rows.length) {
    root.append(element('p', t('analysis.no-data'), 'analysis-state--empty'));
    return;
  }
  const table = element('table', null, 'data-table');
  const head = element('thead'); const headRow = element('tr');
  headers.forEach((label) => headRow.append(element('th', label)));
  head.append(headRow); table.append(head);
  const body = element('tbody');
  rows.forEach((values) => { const tr = element('tr'); values.forEach((value) => tr.append(element('td', value == null ? t('common.unavailable') : value))); body.append(tr); });
  table.append(body); const wrap=element('div',null,'table-wrap'); wrap.append(table); root.append(wrap);
}

async function loadAnalytics() {
  try {
    const data = await getJson('/api/v1/analytics/overview', { locale: getLocale() });
    state.analytics = data;
    if (!state.algorithmCatalog) {
      try { state.algorithmCatalog = await getJson('/api/v1/algorithms', { locale: getLocale() }); }
      catch { state.algorithmCatalog = null; }
    }
    const totals = data.keyword_totals || {};
    $('#metric-records-stored').textContent = number(totals.unique);
    $('#metric-records-stored-note').textContent = t('analysis.unique-keywords');
    $('#metric-topics-found').textContent = number(totals.classified);
    $('#metric-topics-found-note').textContent = t('analysis.classified-keywords');
    $('#metric-unclassified').textContent = number(totals.unclassified);
    $('#metric-unclassified-note').textContent = t('analysis.unclassified-breakdown',{ambiguous:number(totals.ambiguous),unknown:number(totals.unknown)});
    $('#metric-active-sources').textContent = number((data.active_sources || []).length);
    $('#metric-active-sources-note').textContent = t('analysis.sources-in-ledger');
    const latest = data.latest_dataset;
    const successful = data.last_successful_fetch;
    if (successful?.fetched_at) {
      $('#metric-last-fetch').textContent = compactTimestamp(successful.fetched_at);
      $('#metric-last-fetch-note').textContent = t('analysis.latest-fetch-status', { status: successful.status || 'UNKNOWN' })
        + ' · ' + t('analysis.fetch-age',{age:formatAge(successful.age_seconds)});
    }
    renderDataTable($('#analysis-source-volume'), [t('analysis.source'), t('analysis.keyword-count'), t('analysis.period')],
      (data.source_volume || []).map(row => [row.source, number(row.keywords), `${row.first_seen || '—'} – ${row.last_seen || '—'}`]), t('analysis.source-volume-note'));
    renderDataTable($('#analysis-algorithms'), [t('analysis.algorithm'), t('analysis.result-state'), t('analysis.count')],
      (data.algorithm_distribution || []).map(row => [localizedAlgorithmName(row.algorithm_id), translatedAlgorithmCode('result', row.status), number(row.count)]), t('analysis.algorithm-distribution-note'));
    renderDataTable($('#analysis-effectiveness'), [t('analysis.evaluation'), t('analysis.reason')],
      [[data.algorithm_effectiveness?.status || 'NOT_YET_EVALUATED', data.algorithm_effectiveness?.reason || t('analysis.no-comparable-evaluation')]], t('analysis.effectiveness-note'));
    renderDataTable($('#analysis-source-operations'), [t('analysis.fetch-time'), t('analysis.source'), t('analysis.result-state'), t('analysis.records')],
      (data.source_operations || []).flatMap(run => (run.source_manifest || []).map(source => [run.fetched_at || '—', source.source_id || '—', source.status || run.status || '—', source.accepted_records ?? source.record_count ?? '—'])), t('analysis.source-operations-note'));
    renderDataTable($('#analysis-coverage'), [t('analysis.source'), t('analysis.keyword-count'), t('analysis.period')],
      (data.multi_source_coverage?.sources || []).map(row => [row.source, number(row.keywords), `${row.first_seen || '—'} – ${row.last_seen || '—'}`]),
      data.multi_source_coverage?.status === 'INSUFFICIENT_SOURCES' ? t('analysis.coverage-insufficient') : t('analysis.coverage-caveat'));
    renderDataTable($('#analysis-history'), [t('analysis.date'), t('analysis.source'), t('analysis.observed'), t('analysis.unavailable')],
      (data.observations_over_time || []).map(row => [row.observation_date, row.source, number(row.observed), number(row.unavailable)]), t('analysis.history-note'));
    const top = data.top_observed || [];
    const target = $('#overview-top-keywords');
    renderObservedVolumeChart(top, data.top_observed_context || {});
    if (target) {
      const c=data.top_observed_context || {};
      const scope=Array.isArray(c.scope) ? c.scope.slice(1,4).join(' · ') : '—';
      const note=t('analysis.top-observed-context',{source:c.source || '—',scope,date:c.observation_date || '—',cohort:number(c.cohort_size),flags:(c.quality_flags || []).join(', ')});
      renderDataTable(target, [t('analysis.rank'),t('analysis.keyword'),t('analysis.metric'),t('analysis.value')], top.map(row => [number(row.rank),row.keyword,`${row.metric || '—'} (${row.unit || '—'})`,number(row.observed_value)]), note);
    }
  } catch (error) {
    for (const id of ['analysis-source-volume','analysis-algorithms','analysis-effectiveness','analysis-source-operations','analysis-coverage','analysis-history']) {
      const root = $('#' + id); if (root) root.replaceChildren(element('p', getErrorMessage(error, 'analysis.error')));
    }
  }
}

function localizedAlgorithmName(id) {
  const method = state.algorithmCatalog?.algorithms?.find((item) => item.algorithm_id === id);
  const key = `algorithms.name.${String(id || '').toLowerCase()}`;
  const translated = t(key);
  if (translated !== key) return translated;
  return method?.name || t('algorithms.unknown-name');
}

function renderObservedVolumeChart(rows, context) {
  const chart = $('#pageviews-chart');
  const empty = $('#pageviews-empty');
  if (!chart || !empty) return;
  chart.replaceChildren();
  const comparable = (Array.isArray(rows) ? rows : []).filter((row) => Number.isFinite(row.observed_value) && row.observed_value >= 0).slice(0, 5);
  empty.hidden = comparable.length > 0;
  if (!comparable.length) return;
  const maximum = Math.max(...comparable.map((row) => row.observed_value), 1);
  chart.setAttribute('aria-label', t('analysis.top-observed-title'));
  for (const rowData of comparable) {
    const row = element('li', null, 'chart-row');
    const label = element('span', rowData.keyword || '—', 'chart-label');
    const track = element('span', null, 'chart-track');
    const bar = element('span', null, 'chart-bar');
    bar.setAttribute('aria-hidden', 'true');
    bar.style.width = `${Math.max(3, (rowData.observed_value / maximum) * 100)}%`;
    track.append(bar);
    const value = element('strong', number(rowData.observed_value), 'chart-value');
    row.title = `${number(rowData.rank)} · ${rowData.metric || '—'} (${rowData.unit || '—'}) · ${context.observation_date || '—'}`;
    row.append(label, track, value);
    chart.append(row);
  }
}

function formatAge(seconds) {
  if (!Number.isFinite(seconds)) return t('common.unavailable');
  if (seconds < 3600) return t('time.age-minutes',{count:Math.floor(seconds/60)});
  if (seconds < 86400) return t('time.age-hours',{count:Math.floor(seconds/3600)});
  return t('time.age-days',{count:Math.floor(seconds/86400)});
}

async function loadRunHistory({ append = false } = {}) {
  const root = $('#run-history');
  if (!root) return;
  if (!append) state.runHistoryOffset = 0;
  const params = new URLSearchParams({ limit: '20', offset: String(state.runHistoryOffset) });
  const q = $('#run-history-search')?.value.trim() || '';
  const status = $('#run-history-status')?.value || '';
  if (q) params.set('q', q); if (status) params.set('status', status);
  try {
    const data = await getJson('/api/v1/runs/history?' + params.toString(), { locale: getLocale() });
    const list = append ? root.querySelector('tbody') : null;
    if (!append) root.replaceChildren();
    let table = root.querySelector('table');
    if (!table) {
      table = element('table', null, 'data-table'); const thead=element('thead'); const tr=element('tr');
      [t('runs.date'),t('runs.type'),t('runs.sources'),t('runs.status'),t('runs.records'),t('runs.mode'),t('runs.outcome'),t('runs.actions')].forEach(label=>tr.append(element('th',label)));
      thead.append(tr); table.append(thead,element('tbody')); root.append(table);
    }
    if (!append && data.history_truncated) root.append(element('p', t('runs.history-limited'), 'fine-print'));
    else if (!append && data.manual_history_status === 'PARTIAL') root.append(element('p', t('runs.manual-history-partial'), 'fine-print'));
    else if (!append && data.manual_history_status === 'LOCAL_LEDGER_ONLY') root.append(element('p', t('runs.manual-history-unavailable'), 'fine-print'));
    const body=table.querySelector('tbody');
    (data.items || []).forEach(run => {
      const tr=element('tr');
      const received=run.received_record_count ?? run.available;
      const retained=run.retained_keyword_count ?? run.processed;
      const recordSummary=[received == null ? null : `${t('runs.received')}: ${number(received)}`,
        retained == null ? null : `${t('runs.retained')}: ${number(retained)}`,
        run.rejected_record_count == null ? null : `${t('runs.rejected')}: ${number(run.rejected_record_count)}`].filter(Boolean).join(' · ') || '—';
      const outcomes=[run.error_code,run.stage,run.quality_flags?.join(', ')].filter(Boolean).join(' · ') || '—';
      [run.run_at || '—',run.run_type, (run.sources || []).join(', ') || '—',run.status,recordSummary,run.mode || '—',outcomes].forEach(value=>tr.append(element('td',value)));
      const actions=element('td');
      if (run.fetch_id) { const button=element('button',t('runs.details'),'button button-secondary'); button.type='button'; button.addEventListener('click',()=>loadRunHistoryDetail(run.fetch_id)); actions.append(button); }
      else actions.textContent='—';
      tr.append(actions); body.append(tr);
    });
    state.runHistoryOffset = data.next_offset ?? data.total;
    $('#run-history-more').hidden = data.next_offset == null;
    if (!data.items?.length && !append) root.append(element('p',t('runs.no-history')));
  } catch (error) { root.replaceChildren(element('p',getErrorMessage(error,'runs.history-error'))); }
}

async function loadRunHistoryDetail(fetchId) {
  const root=$('#run-history-detail'); root.hidden=false; root.replaceChildren(element('p',t('common.loading')));
  try {
    const response=await getJson('/api/v1/runs/history/'+encodeURIComponent(fetchId),{locale:getLocale()});
    const item=response.item || {}; root.replaceChildren();
    root.append(element('h2',t('runs.detail-title')));
    const list=element('dl','', 'detail-list detail-list--grid');
    const sourceSummary=(item.source_manifest || []).map(source=>`${source.source_id}: ${source.status || '—'} (${source.record_count ?? '—'})`).join(' · ') || '—';
    const values=[[t('runs.run-id'),item.fetch_id],[t('runs.status'),item.status],[t('runs.mode'),item.data_mode || '—'],
      [t('runs.sources'),sourceSummary],[t('runs.received'),item.received_record_count ?? item.available],
      [t('runs.retained'),item.retained_record_count ?? item.processed],[t('runs.rejected'),item.rejected_record_count],
      [t('runs.error'),item.error_code],[t('runs.quality'),(item.quality_flags || []).join(', ') || '—'],
      [t('runs.duration'),t('runs.duration-unavailable')],[t('runs.date'),item.fetched_at || item.updated_at || '—']];
    for(const [label,value] of values){const row=element('div');row.append(element('dt',label),element('dd',value == null ? t('common.unavailable') : value));list.append(row);}
    root.append(list);
  } catch(error) { root.replaceChildren(element('p',getErrorMessage(error,'runs.history-error'))); }
}

async function loadSourceLibrary() {
  const body=$('#source-library-rows'); if (!body) return;
  try {
    const data=await getJson('/api/v1/sources/library',{locale:getLocale()});
    state.sourceLibrary=data.items || [];
    $('#source-library-audit').textContent=t('sources.library-audit',{date:data.audit_date || '—',count:number(state.sourceLibrary.length)});
    renderSourceLibrary();
  } catch (error) { body.replaceChildren(); const row=element('tr'); row.append(element('td',getErrorMessage(error,'sources.library-error'))); body.append(row); }
}

async function loadRuntimeStatus() {
  const root=$('#settings-runtime-details'); if (!root) return;
  try {
    const data=await getJson('/api/v1/runtime',{locale:getLocale()});
    const e=data.embedding || {}; const l=data.llm || {}; const s=data.scheduler || {};
    root.replaceChildren();
    const fields=[
      [t('settings.embedding-model'),e.model], [t('settings.embedding-version'),`${e.package || 'FastEmbed'} ${e.package_version || '—'} · ${e.model_version || '—'}`],
      [t('settings.inference-device'),e.inference_device], [t('settings.embedding-cache'),`${number(e.cache_vectors || 0)} · ${e.vector_dimensions || '—'} ${t('settings.dimensions')}`],
      [t('settings.classification-policy'),e.classification_policy_version], [t('settings.taxonomy-version'),e.taxonomy_version],
      [t('settings.llm-state'),l.status], [t('settings.ai-usage'),l.usage_instrumented ? t('settings.measured') : t('settings.not-measured')],
    ];
    for(const [label,value] of fields){const row=element('div');row.append(element('dt',label),element('dd',value ?? t('common.unavailable')));root.append(row);}
    const schedulerStatus=$('#settings-scheduler-status');
    schedulerStatus.textContent=t(s.worker_running?'settings.scheduler-running':'settings.scheduler-disabled');
    schedulerStatus.classList.toggle('pill--green',Boolean(s.worker_running));
    schedulerStatus.classList.toggle('pill--gray',!s.worker_running);
    $('#settings-scheduler-reason').textContent=t(s.worker_running?'settings.scheduler-running-help':s.enabled?'settings.scheduler-configured-help':'settings.scheduler-help');
  } catch(error) { root.replaceChildren(element('div',getErrorMessage(error,'settings.runtime-error'))); }
}

async function loadResearchWorkspace() {
  await Promise.all([loadResearchKeywords(),loadResearchRecords()]);
}

async function loadResearchKeywords(query='') {
  const select=$('#research-keyword'); if(!select)return;
  const prior=select.value; const params=new URLSearchParams({limit:'100',sort:'recent'}); if(query)params.set('q',query);
  try {
    const data=await getJson('/api/v1/datasets/latest/keywords?'+params.toString(),{locale:getLocale()});
    state.researchKeywords=data.items || [];
    select.replaceChildren(new Option(t('research.no-linked-keyword'),''),...state.researchKeywords.map(item=>new Option(`${item.keyword} · ${item.source || '—'} · ${item.observed_at || item.observation_date || '—'}`,item.keyword_id || item.id)));
    if(state.researchKeywords.some(item=>(item.keyword_id || item.id)===prior))select.value=prior;
  } catch(error) { select.replaceChildren(new Option(t('research.keywords-unavailable'),'')); }
}

async function loadResearchRecords() {
  const root=$('#research-records'); if(!root)return;
  try { const result=await getJson('/api/v1/research?limit=50',{locale:getLocale()}); renderResearchRecords(result.items || []); }
  catch(error) { root.replaceChildren(element('p',getErrorMessage(error,'research.load-error'))); }
}

function renderResearchRecords(items) {
  const root=$('#research-records'); if(!root)return;
  root.replaceChildren();
  if(!items.length){root.append(element('p',t('research.empty')));return;}
  for(const item of items){
    const card=element('article',null,'research-record');
    const heading=element('div',null,'research-record-heading');
    heading.append(element('h3',item.subject));
    const status=element('select'); status.setAttribute('aria-label',t('research.status-for',{subject:item.subject}));
    for(const value of ['DRAFT','RESEARCHING','NEEDS_REVIEW','VALIDATED','ARCHIVED'])status.add(new Option(t(['research','status',value.toLowerCase()].join('.')),value));
    status.value=item.status; status.addEventListener('change',async()=>{
      status.disabled=true;
      try { await requestJson('/api/v1/research/'+encodeURIComponent(item.id),{method:'PATCH',body:{status:status.value},locale:getLocale()}); }
      catch(error){status.value=item.status;announce(getErrorMessage(error,'research.save-error'),true);}
      finally{status.disabled=false;}
    });
    heading.append(status); card.append(heading);
    const sourceMeta=item.keyword ? `${item.source || ''} · ${item.project || ''} · ${item.observed_at || ''} · ${item.data_mode || ''}` : t('research.no-linked-keyword');
    card.append(element('p',sourceMeta,'fine-print'));
    card.append(element('p',item.hypothesis));
    const details=element('details'); details.append(element('summary',t('research.evidence-details')));
    const list=element('dl','', 'detail-list');
    for(const [key,value] of [['research.category',item.product_category],['research.niche',item.niche],['research.competitors',item.competitor_notes],['research.market-evidence',item.market_evidence],['research.ip-notes',item.ip_notes],['research.risks',item.risk_notes],['research.findings',item.findings]]){
      if(!value)continue; const row=element('div'); row.append(element('dt',t(key)),element('dd',value)); list.append(row);
    }
    details.append(list); card.append(details); root.append(card);
  }
}

async function saveResearchRecord(event) {
  event.preventDefault();
  const selected=$('#research-keyword').selectedOptions[0];
  const inputSubject=$('#research-subject').value.trim();
  const payload={keyword_id:$('#research-keyword').value || null,
    subject:inputSubject || (selected?.value ? selected.textContent.split(' · ')[0] : ''),
    hypothesis:$('#research-hypothesis').value.trim(),product_category:$('#research-category').value.trim(),
    niche:$('#research-niche').value.trim(),competitor_notes:$('#research-competitors').value.trim(),
    market_evidence:$('#research-market-evidence').value.trim(),ip_notes:$('#research-ip').value.trim(),
    risk_notes:$('#research-risks').value.trim(),findings:$('#research-findings').value.trim(),status:$('#research-status').value};
  const status=$('#research-form-status');
  try { await requestJson('/api/v1/research',{method:'POST',body:payload,locale:getLocale()});
    status.textContent=t('research.saved'); $('#research-hypothesis').value=''; $('#research-competitors').value='';
    $('#research-market-evidence').value=''; $('#research-ip').value=''; $('#research-risks').value=''; $('#research-findings').value='';
    await loadResearchRecords();
  } catch(error) { status.textContent=getErrorMessage(error,'research.save-error'); }
}

function renderSourceLibrary() {
  const body=$('#source-library-rows'); if (!body) return;
  const query=($('#source-library-search')?.value || '').trim().toLocaleLowerCase();
  const items=state.sourceLibrary.filter(item=>`${item.source_id} ${item.display_name} ${item.category}`.toLocaleLowerCase().includes(query));
  body.replaceChildren();
  for (const item of items) {
    const row=element('tr');
    [item.display_name || item.source_id,item.category,t('sources.candidate-locked'),t('sources.candidate-not-integrated')].forEach(value=>row.append(element('td',value)));
    body.append(row);
  }
  if (!items.length) { const row=element('tr'); row.append(element('td',t('sources.no-candidates'))); body.append(row); }
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
        $('#workbench-status').textContent = t('collections.progress', {
        stage: t(`collections.stage-${job.stage || 'CLEANING'}`), eligible: number(job.eligible),
        processed: number(job.processed), cached: number(job.cached_embeddings),
        inferred: number(job.inferred_embeddings), assigned: number(job.assigned),
        created: number(job.created_collections), unassigned: number(job.unassigned),
        rejected: number(job.skipped),
        });
        window.setTimeout(() => poll(attempt + 1), 1000);
        return;
      }
      state.collectionJobStatus = job;
      if (job.status === 'MODEL_UNAVAILABLE') {
        $('#workbench-status').textContent = t('collections.model-unavailable');
      } else if (job.status === 'COMPLETED') {
        await loadCollections();
        const summary = $('#workbench-status');
        summary.replaceChildren(element('span', t('collections.completed', {
          assigned: number(job.assigned), collections: state.collections.filter((item) => item.membership_count > 0).length,
          created: number(job.created_collections), eligible: number(job.eligible),
          processed: number(job.processed), cached: number(job.cached_embeddings),
          unassigned: number(job.unassigned), rejected: number(job.skipped),
        })));
      }
      await loadLatestDataset();
      if (state.persistedDatasetAvailable) loadKeywords();
      if (window.location.hash === '#collections' && job.status !== 'COMPLETED') loadCollections();
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
  $('#metric-last-fetch').textContent = run.fetched_at ? compactTimestamp(run.fetched_at) : '—';
  $('#metric-last-fetch-note').textContent = run.fetched_at ? t('metric.from-analysis') : t('metric.not-reported');

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
  const lastFetch = state.publicFetchFinishedAt || fetchedAt;
  $('#metric-last-fetch').textContent = lastFetch ? compactTimestamp(lastFetch) : '—';
  $('#metric-last-fetch-note').textContent = lastFetch ? t('metric.last-fetch-context') : t('metric.fetch-to-begin');
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
  const chartRows = [...unique.values()].sort((a, b) => (a.provider_rank ?? 9999) - (b.provider_rank ?? 9999)).slice(0, 5);
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
      ? item.memberships.map((membership) => `${membership.name} (${t(membership.origin === 'LOCAL_AI' ? 'workbench.auto-assigned' : 'workbench.user-added')})`).join(', ')
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
  if (!isLocalLiveDemo(state.capabilities)) {
    renderAnalysisSourceVolume(null);
    return null;
  }
  try {
    const response = await getJson('/api/v1/datasets/latest', { locale: getLocale() });
    state.persistedDataset = response.item || null;
    state.persistedDatasetAvailable = Boolean(response.item);
    if (response.item) {
      const item = response.item;
      renderPersistedDataset(item);
      await loadCollections();
      renderCollectionFilters();
      return item;
    }
    $('#overview-context').textContent = t('overview.no-fetch-context');
    renderAnalysisSourceVolume(null);
    return null;
  } catch (error) {
    state.persistedDatasetAvailable = false;
    $('#analysis-source-volume').replaceChildren(element('p', t('analysis.error')));
    setNotice('#keyword-notice', getErrorMessage(error, 'error.keyword-fetch'));
    return null;
  }
}

function renderPersistedDataset(item) {
  renderAnalysisSourceVolume(item);
  const sourceNames = {
    wikimedia_pageviews_top_per_country: 'source.name.wikimedia',
    youtube_video_search: 'source.name.youtube',
    google_web_serp: 'source.name.google',
    bing_web_serp: 'source.name.bing',
  };
  const sources = (Array.isArray(item.sources) ? item.sources : [])
    .map((source) => t(sourceNames[source] || 'source.status-unknown')).join(', ') || t('common.unavailable');
  const status = item.status === 'LEGACY_CACHED'
    ? t('dataset.legacy-cached') : t('dataset.snapshot-status', { status: localizedStatus('status.', item.status) });
  const snapshotCoverage = item.snapshot_retained_keyword_count == null ? ''
    : t('dataset.latest-batch', {
      batch: number(item.snapshot_retained_keyword_count),
      ledger: item.retained_keyword_count == null ? t('common.unavailable') : number(item.retained_keyword_count),
    });
  const ledgerCaveat = item.ledger_coverage_status === 'MIXED_OR_UNKNOWN'
    || item.status === 'LEGACY_CACHED' ? t('dataset.ledger-coverage-unknown') : '';
  const eligible = item.eligible_keyword_count == null ? t('common.unavailable') : number(item.eligible_keyword_count);
  const unassigned = item.unassigned_keyword_count == null ? t('common.unavailable') : number(item.unassigned_keyword_count);
  const fetchedAt = item.fetched_at ? timestamp(item.fetched_at) : t('common.unavailable');
  const observationWindow = item.observation_start || item.observation_end
    ? t('dataset.observation-window', {
      dates: [item.observation_start, item.observation_end].filter(Boolean).map(date).join(' – '),
    }) : '';
  $('#overview-context').textContent = [status, snapshotCoverage, ledgerCaveat, sources,
    t('dataset.eligible-count', { count: eligible }),
    t('dataset.unassigned-count', { count: unassigned }),
    observationWindow,
    fetchedAt].filter(Boolean).join(' · ');
  $('#metric-records-returned').textContent = item.provider_record_count == null ? t('common.unavailable') : number(item.provider_record_count);
  $('#metric-records-returned-note').textContent = item.status === 'LEGACY_CACHED'
    ? t('metric.legacy-fetch-count') : t('metric.provider-snapshot');
  $('#metric-records-stored').textContent = item.retained_keyword_count == null ? t('common.unavailable') : number(item.retained_keyword_count);
  $('#metric-records-stored-note').textContent = t('metric.ledger-stored');
  $('#metric-topics-found').textContent = eligible;
  $('#metric-topics-found-note').textContent = t('metric.topic-assignment', {
    assigned: item.assigned_keyword_count == null ? t('common.unavailable') : number(item.assigned_keyword_count),
    unassigned,
  });
  $('#metric-last-fetch').textContent = item.fetched_at ? compactTimestamp(item.fetched_at) : '—';
  $('#metric-last-fetch-note').textContent = item.fetched_at
    ? t('metric.cached-snapshot-note') : t('metric.cached-time-unavailable');
  $('#keyword-count').textContent = item.retained_keyword_count == null ? t('common.unavailable') : number(item.retained_keyword_count);
  $('#scored-count').textContent = item.assigned_keyword_count == null ? t('common.unavailable') : number(item.assigned_keyword_count);
  $('#results-count').textContent = eligible;
}

function renderAnalysisSourceVolume(dataset) {
  const root = $('#analysis-source-volume');
  if (!dataset) {
    root.replaceChildren(element('p', t('analysis.no-snapshot')));
    return;
  }
  const sources = Array.isArray(dataset.sources) ? dataset.sources : [];
  if (sources.length !== 1 || dataset.snapshot_retained_keyword_count == null) {
    root.replaceChildren(element('p', t('analysis.insufficient-source-volume')));
    return;
  }
  const sourceId = sources[0];
  const sourceNames = {
    wikimedia_pageviews_top_per_country: 'source.name.wikimedia',
    youtube_video_search: 'source.name.youtube',
    google_web_serp: 'source.name.google',
    bing_web_serp: 'source.name.bing',
  };
  root.replaceChildren(
    element('strong', number(dataset.snapshot_retained_keyword_count), 'analysis-value'),
    element('p', t('analysis.source-volume-note', { source: t(sourceNames[sourceId] || 'source.status-unknown') })),
  );
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
  const legacyOverview = name === 'results' || name === 'keywords';
  const dashboardTab = name === 'analysis' ? 'analysis' : 'overview';
  const valid = new Set(['overview', 'analysis', 'collections', 'sources', 'runs', 'algorithms', 'quality', 'research', 'settings']);
  const selected = legacyOverview ? 'collections' : !valid.has(name) ? 'overview' : name === 'analysis' ? 'overview' : name;
  if (legacyOverview) {
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}#collections`);
  } else if (name && !valid.has(name)) {
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}#overview`);
  }
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
  $('#breadcrumb-current').textContent = t(selected === 'overview' ? `dashboard.${dashboardTab}` : 'nav.' + selected);
  setDashboardTab(dashboardTab, false);
  $('#breadcrumb-current').textContent = t(selected === 'overview' ? `dashboard.${dashboardTab}` : 'nav.' + selected);
  if (selected === 'collections') loadCollections();
  if (selected === 'algorithms') loadAlgorithmCatalog();
  if (selected === 'runs') loadRunHistory();
  if (selected === 'sources') loadSourceLibrary();
  if (selected === 'settings') loadRuntimeStatus();
  if (selected === 'research') loadResearchWorkspace();
  closeMobileNav();
}

function setDashboardTab(tab, updateRoute = true, focus = false) {
  const selected = tab === 'analysis' ? 'analysis' : 'overview';
  $$('[data-dashboard-tab]').forEach((button) => {
    const active = button.dataset.dashboardTab === selected;
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
    if (focus && active) button.focus();
  });
  $$('[data-dashboard-panel]').forEach((panel) => {
    panel.hidden = panel.dataset.dashboardPanel !== selected;
  });
  if (updateRoute && window.location.hash !== `#${selected}`) {
    window.history.pushState(null, '', `${window.location.pathname}${window.location.search}#${selected}`);
  }
  $('#breadcrumb-current').textContent = t(`dashboard.${selected}`);
}

function updateNavigationLabels() {
  $$('[data-nav]').forEach((link) => {
    const label = t('nav.' + link.dataset.nav);
    link.setAttribute('aria-label', label);
    link.title = label;
  });
  const advanced = $('.nav-advanced summary');
  if (advanced) {
    advanced.setAttribute('aria-label', t('nav.advanced'));
    advanced.title = t('nav.advanced');
  }
  const collapsed = $('#app-shell').classList.contains('sidebar-collapsed');
  $('#sidebar-collapse').setAttribute('aria-label', t(collapsed ? 'a11y.expand-nav' : 'a11y.collapse-nav'));
}

async function loadCollections({ appendInbox = false } = {}) {
  await workbench.refresh();
  state.collections = workbench.items();
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
  if (visible) {
    const firstLink = panel.querySelector('a[href], button:not(:disabled), summary');
    firstLink?.focus();
  }
}

function closeMobileNav() {
  if (isMobileNav()) {
    const wasOpen = $('#app-shell').classList.contains('menu-open');
    setMobileNav(false);
    if (wasOpen) $('#menu-toggle').focus();
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
    let runtimeCapabilities = null;
    try { runtimeCapabilities = await getJson('/api/v1/capabilities', { locale: getLocale() }); }
    catch { /* registry metadata remains useful when runtime health is unavailable */ }
    state.algorithmCatalog = catalogResult;
    state.algorithmResults = executionResult;
    state.runtimeCapabilities = runtimeCapabilities;
    populateAlgorithmFilters(state.algorithmCatalog.algorithms || []);
    renderAlgorithmCatalog();
    $('#algorithm-evaluation-status').textContent = `${translatedAlgorithmCode('evaluation',state.algorithmCatalog.evaluation_status)} · ${t('algorithms.evaluation-note')}`;
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
  family.replaceChildren(new Option(t('algorithms.all-families'), ''), ...families.map((value) => new Option(translatedAlgorithmCode('family',value), value)));
  source.replaceChildren(new Option(t('algorithms.all-sources'), ''), ...sources.map((value) => new Option(value.replaceAll('_',' '), value)));
  family.value = priorFamily;
  source.value = priorSource;
}

function renderAlgorithmCatalog() {
  const root = $('#algorithm-catalog');
  const all = state.algorithmCatalog?.algorithms || [];
  renderAlgorithmSignalStatus(all);
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
    title.append(element('p', translatedAlgorithmCode('family', item.family), 'section-kicker'));
    title.append(element('h2', item.name));
    heading.append(title);
    const compatible = item.sources.filter((entry) => entry.status !== 'INCOMPATIBLE');
    heading.append(element('span', translatedAlgorithmCode('status', compatible.length ? compatible[0].status : 'INCOMPATIBLE'), 'pill pill--gray'));
    card.append(heading);
    card.append(element('p', item.description));
    const counts = (state.algorithmResults?.summary || []).find((entry) => entry.algorithm_id === item.algorithm_id);
    card.append(element('p', counts
      ? t('algorithms.execution-summary', { completed: number(counts.completed), skipped: number(counts.skipped), failed: number(counts.failed) })
      : t('algorithms.no-results'), 'algorithm-execution-summary'));
    const examples = (state.algorithmResults?.items || []).filter((entry) => entry.algorithm_id === item.algorithm_id).slice(0, 3);
    if (examples.length) {
      const outputDetails = element('details', null, 'algorithm-details');
      outputDetails.append(element('summary', t('algorithms.latest-results')));
      const outputList = element('ul');
      examples.forEach((entry) => {
        let result = {};
        try { result = JSON.parse(entry.result_json || '{}'); } catch { /* malformed old rows remain display-safe */ }
        const flags = Array.isArray(result.quality_flags) ? result.quality_flags.join(', ') : '';
      const message = [entry.keyword, translatedAlgorithmCode('result', entry.status),
          entry.value === null ? translatedAlgorithmCode('reason', entry.reason_code) : `${entry.value} ${entry.unit}`,
          flags].filter(Boolean).join(' · ');
        outputList.append(element('li', message));
      });
      outputDetails.append(outputList);
      card.append(outputDetails);
    }
    const sourceRow = element('div', null, 'algorithm-source-icons');
    item.sources.forEach((entry) => {
      const sourceBadge = element('span', null, 'algorithm-source-badge');
      const currentSource = state.runtimeCapabilities?.sources?.find((source) => source.source_id === entry.source_id);
      const activeLive = state.runtimeCapabilities?.data_mode === 'LIVE'
        && state.runtimeCapabilities?.local_live_demo === true
        && currentSource?.enabled_for_current_operation === true;
      sourceBadge.title = `${entry.display_name || entry.source_id}: ${entry.status}`
        + ` · runtime ${activeLive ? 'LIVE active' : (entry.runtime_availability || 'unavailable')}`
        + ` · local ${entry.local_live_verification_status || 'UNKNOWN'}`
        + ` · ${entry.production_readiness || 'UNKNOWN'}`
        + (entry.reason ? ` · ${entry.reason}` : '');
      const glyph = element('span', entry.icon || '•', 'algorithm-source-glyph');
      glyph.classList.toggle('algorithm-source-glyph--active', activeLive);
      sourceBadge.append(glyph);
      sourceBadge.append(element('span', entry.display_name || entry.source_id.replaceAll('_', ' ')));
      sourceRow.append(sourceBadge);
    });
    card.append(sourceRow);
    const detail = element('details', null, 'algorithm-details');
    detail.append(element('summary', t('algorithms.requirements')));
    const list = element('dl', null, 'detail-list');
    addAlgorithmDetail(list, t('algorithms.input'), `${item.input_metric} (${item.input_unit})`);
    addAlgorithmDetail(list, t('algorithms.output'), `${item.metric} (${item.unit})`);
    addAlgorithmDetail(list, t('algorithms.minimum-samples'), item.minimum_samples);
    addAlgorithmDetail(list, t('algorithms.windows'), (item.supported_windows || []).join(', '));
    addAlgorithmDetail(list, t('algorithms.missing-data'), item.missing_data);
    addAlgorithmDetail(list, t('algorithms.runtime'), item.runtime_limit);
    addAlgorithmDetail(list, t('algorithms.interpretation'), item.interpretation || t('algorithms.descriptive-output'));
    addAlgorithmDetail(list, t('algorithms.limitations'), (item.limitations || []).join(' '));
    detail.append(list);
    card.append(detail);
    root.append(card);
  });
}

function translatedAlgorithmCode(kind, code) {
  if (!code) return t('common.unavailable');
  const key = `algorithms.${kind}.${String(code).toLowerCase()}`;
  const translated = t(key);
  return translated === key ? String(code).toLowerCase().replaceAll('_', ' ') : translated;
}

function renderAlgorithmSignalStatus(algorithms) {
  const root = $('#algorithm-signal-status');
  if (!root) return;
  const summaries = new Map((state.algorithmResults?.summary || [])
    .map((item) => [item.algorithm_id, item]));
  const groups = [
    { key: 'popularity', family: 'popularity' },
    { key: 'growth', family: 'growth' },
    { key: 'anomaly', family: 'anomaly' },
    { key: 'seasonality', family: 'seasonality' },
  ];
  const cards = groups.map(({ key, family }) => {
    const methods = algorithms.filter((item) => item.family === family);
    const totals = methods.reduce((sum, item) => {
      const result = summaries.get(item.algorithm_id);
      return {
        completed: sum.completed + (result?.completed || 0),
        skipped: sum.skipped + (result?.skipped || 0),
        failed: sum.failed + (result?.failed || 0),
      };
    }, { completed: 0, skipped: 0, failed: 0 });
    const status = !methods.length || (!totals.completed && totals.skipped)
      ? 'INSUFFICIENT_DATA' : totals.completed ? 'OBSERVED' : 'NOT_RUN';
    const card = element('article', null, 'algorithm-signal-card');
    card.append(element('h2', t(`algorithms.signal.${key}`)));
    card.append(element('strong', translatedAlgorithmCode('evaluation',status), `algorithm-signal-status algorithm-signal-status--${status.toLowerCase()}`));
    card.append(element('p', t(`algorithms.signal-counts.${key}`, {
      methods: methods.length, completed: totals.completed, skipped: totals.skipped,
    })));
    return card;
  });
  const skipped = [...summaries.values()].reduce((total, item) => total + (item.skipped || 0), 0);
  const insufficient = element('article', null, 'algorithm-signal-card algorithm-signal-card--wide');
  insufficient.append(element('h2', t('algorithms.signal.insufficient')));
  insufficient.append(element('strong', 'INSUFFICIENT_DATA', 'algorithm-signal-status algorithm-signal-status--insufficient_data'));
  insufficient.append(element('p', t('algorithms.signal-counts.insufficient', { skipped })));
  root.replaceChildren(...cards, insufficient);
}

function addAlgorithmDetail(list, label, value) {
  const row = element('div');
  row.append(element('dt', label), element('dd', value));
  list.append(row);
}

function bindEvents() {
  $('#menu-toggle').addEventListener('click', () => setMobileNav(!$('#app-shell').classList.contains('menu-open')));
  $('#sidebar-collapse').addEventListener('click', () => {
    if (isMobileNav()) return;
    const shell = $('#app-shell');
    const collapsed = shell.classList.toggle('sidebar-collapsed');
    $('#sidebar-collapse').setAttribute('aria-expanded', String(!collapsed));
    $('#sidebar-collapse').setAttribute('aria-label', t(collapsed ? 'a11y.expand-nav' : 'a11y.collapse-nav'));
    try { localStorage.setItem('trendscout.sidebar-collapsed', String(collapsed)); } catch {}
    updateNavigationLabels();
  });
  try {
    const collapsed = localStorage.getItem('trendscout.sidebar-collapsed') === 'true';
    $('#app-shell').classList.toggle('sidebar-collapsed', collapsed);
    $('#sidebar-collapse').setAttribute('aria-expanded', String(!collapsed));
    $('#sidebar-collapse').setAttribute('aria-label', t(collapsed ? 'a11y.expand-nav' : 'a11y.collapse-nav'));
  } catch {}
  updateNavigationLabels();
  $('#nav-scrim').addEventListener('click', closeMobileNav);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeMobileNav();
    }
    if (isMobileNav() && $('#app-shell').classList.contains('menu-open') && event.key === 'Tab') {
      const focusable = $$('a[href], button:not(:disabled), summary', $('#workspace-nav-panel'))
        .filter((node) => !node.closest('[hidden]') && node.getClientRects().length > 0);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });
  window.addEventListener('resize', () => setMobileNav($('#app-shell').classList.contains('menu-open')));
  window.addEventListener('hashchange', () => showView(window.location.hash.slice(1)));
  window.addEventListener('popstate', () => showView(window.location.hash.slice(1)));
  $$('[data-dashboard-tab]').forEach((button) => {
    button.addEventListener('click', () => setDashboardTab(button.dataset.dashboardTab));
    button.addEventListener('keydown', (event) => {
      const buttons = $$('[data-dashboard-tab]');
      const index = buttons.indexOf(button);
      let target = null;
      if (event.key === 'ArrowRight') target = buttons[(index + 1) % buttons.length];
      if (event.key === 'ArrowLeft') target = buttons[(index - 1 + buttons.length) % buttons.length];
      if (event.key === 'Home') target = buttons[0];
      if (event.key === 'End') target = buttons.at(-1);
      if (target) {
        event.preventDefault();
        setDashboardTab(target.dataset.dashboardTab, true, true);
      }
    });
  });
  $$('[data-nav]').forEach((link) => link.addEventListener('click', closeMobileNav));
  $$('[data-goto]').forEach((link) => link.addEventListener('click', closeMobileNav));
  $$('[data-close]').forEach((button) => {
    button.addEventListener('click', () => document.getElementById(button.dataset.close).close());
  });

  $('#load-run').addEventListener('click', () => loadRun($('#run-id').value));
  $('#run-history-refresh').addEventListener('click', () => loadRunHistory());
  $('#run-history-more').addEventListener('click', () => loadRunHistory({ append: true }));
  $('#run-history-search').addEventListener('input', () => { clearTimeout(window.runHistoryTimer); window.runHistoryTimer=setTimeout(()=>loadRunHistory(),250); });
  $('#run-history-status').addEventListener('change', () => loadRunHistory());
  $('#source-library-search').addEventListener('input', renderSourceLibrary);
  $('#research-form').addEventListener('submit',saveResearchRecord);
  $('#research-keyword-search').addEventListener('input',()=>{clearTimeout(window.researchSearchTimer);window.researchSearchTimer=setTimeout(()=>loadResearchKeywords($('#research-keyword-search').value.trim()),250);});
  $('#research-keyword').addEventListener('change',event=>{const item=state.researchKeywords.find(value=>(value.keyword_id || value.id)===event.currentTarget.value);if(item&&!$('#research-subject').value.trim())$('#research-subject').value=item.keyword;});
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
  $('#search').value = state.query;
  $('#search').addEventListener('input', (event) => {
    clearTimeout(searchTimer);
    const query = event.currentTarget.value;
    searchTimer = setTimeout(() => {
      state.query = query;
      const params = new URLSearchParams(window.location.search);
      if (query) params.set('q', query);
      else params.delete('q');
      const search = params.toString();
      window.history.replaceState(null, '', `${window.location.pathname}${search ? `?${search}` : ''}${window.location.hash}`);
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
    updateNavigationLabels();
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
    if (state.persistedDataset) renderPersistedDataset(state.persistedDataset);
    if (state.analytics) loadAnalytics();
    if (window.location.hash === '#runs') loadRunHistory();
    if (window.location.hash === '#sources') loadSourceLibrary();
    if (window.location.hash === '#settings') loadRuntimeStatus();
    if (window.location.hash === '#research') loadResearchWorkspace();
    renderFetchPresentation();
    $('#breadcrumb-current').textContent = t(window.location.hash === '#analysis'
      ? 'dashboard.analysis' : 'nav.' + (window.location.hash.slice(1) || 'overview'));
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
void loadPublicCapabilities().then(() => Promise.all([loadLatestDataset(), loadAnalytics()])).then(() => {
  if (state.persistedDatasetAvailable) loadKeywords();
});
