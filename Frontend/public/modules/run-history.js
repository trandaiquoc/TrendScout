import { getJson } from './api.js';
import { getLocale, t } from './i18n.js';
import { formatDate, formatNumber, formatTimestamp } from './format.js';
import { extractRunSourceCoverage } from './run-history-coverage.js';

const $ = (selector) => document.querySelector(selector);
const number = (value) => formatNumber(value, getLocale());
const date = (value) => formatDate(value, getLocale());
const timestamp = (value) => { const rendered = formatTimestamp(value, getLocale()); return rendered === '—' ? rendered : rendered + ' · ' + t('time.local'); };
const element = (tag, text = null, className = '') => {
  const node = document.createElement(tag);
  if (text !== null && text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
};
const getErrorMessage = (error, fallback) => error && typeof error.message === 'string' ? error.message : t(fallback);

export function initializeRunHistory({ localizedRunType, localizedRunMode, localizedRunOutcome, localizedSourceName, localizedStatus }) {
  const state = { runHistoryOffset: 0, runHistoryCursor: null, runHistoryDetailSequence: 0, runHistoryDetailId: null, runHistoryDetailItem: null, runHistoryDetailTrigger: null };
  let searchTimer, listSequence = 0;
async function loadRunHistory({ append = false } = {}) {
  const root = $('#run-history');
  if (!root) return;
  const sequence = ++listSequence;
  root.setAttribute('aria-busy','true');$('#run-history-loading').hidden=false;
  if (!append) { state.runHistoryOffset = 0; state.runHistoryCursor = null; }
  const params = new URLSearchParams({ limit: '20' });
  params.set('cursor', state.runHistoryCursor || '');
  const q = $('#run-history-search')?.value.trim() || '';
  const status = $('#run-history-status')?.value || '';
  if (q) params.set('q', q); if (status) params.set('status', status);
  try {
    const data = await getJson('/api/v1/runs/history?' + params.toString(), { locale: getLocale() });
    if (sequence !== listSequence) return;
    if (!append) root.replaceChildren();
    const headers=[t('runs.date'),t('runs.type'),t('runs.sources'),t('runs.status'),t('runs.records'),t('runs.duration'),t('runs.mode'),t('runs.outcome'),t('runs.actions')];
    let table = root.querySelector('table');
    if (!table) {
      table = element('table', null, 'data-table'); const thead=element('thead'); const tr=element('tr');
      headers.forEach(label=>tr.append(element('th',label)));
      const wrap=element('div',null,'table-wrap');
      thead.append(tr); table.append(thead,element('tbody')); wrap.append(table); root.append(wrap);
    }
    if (!append && data.history_truncated) root.append(element('p', t('runs.history-limited'), 'fine-print'));
    else if (!append && data.manual_history_status === 'PARTIAL') root.append(element('p', t('runs.manual-history-partial'), 'fine-print'));
    else if (!append && data.manual_history_status === 'LOCAL_LEDGER_ONLY') root.append(element('p', t('runs.manual-history-unavailable'), 'fine-print'));
    else if (!append && data.manual_history_status === 'CURSOR_UNAVAILABLE') root.append(element('p', t('runs.manual-history-cursor-unavailable'), 'fine-print'));
    const body=table.querySelector('tbody');
    (data.items || []).forEach(run => {
      const tr=element('tr');
      const received=run.received_record_count ?? run.available;
      const retained=run.retained_keyword_count ?? run.processed;
      const recordSummary=[received == null ? null : `${t('runs.received')}: ${number(received)}`,
        retained == null ? null : `${t('runs.retained')}: ${number(retained)}`,
        run.rejected_record_count == null ? null : `${t('runs.rejected')}: ${number(run.rejected_record_count)}`].filter(Boolean).join(' · ') || '—';
      const duration=Number.isFinite(run.duration_seconds) && run.duration_seconds >= 0
        ? `${number(run.duration_seconds)} ${t('runs.duration-seconds')}` : t('runs.duration-unavailable');
      const values=[run.run_at ? timestamp(run.run_at) : '—',localizedRunType(run.run_type), (run.sources || []).map(localizedSourceName).join(', ') || '—',localizedStatus('status.',run.status),recordSummary,duration,localizedRunMode(run.mode),localizedRunOutcome(run)];
      tr.className='run-history-entry';
      values.forEach((value,index)=>{const cell=element('td',index===3?'':value);cell.dataset.label=headers[index];if(index===3){const badge=element('span',value,'run-history-status');badge.dataset.status=run.status || 'UNKNOWN';cell.append(badge);}tr.append(cell);});
      const actions=element('td');
      if (run.fetch_id) {
        const button=element('button',t('runs.details'),'button button-secondary');
        button.type='button'; button.setAttribute('aria-controls','run-history-detail');
        const expanded=!$('#run-history-detail').hidden&&state.runHistoryDetailId===run.fetch_id;
        button.setAttribute('aria-expanded',String(expanded));
        if(expanded)state.runHistoryDetailTrigger=button;
        button.addEventListener('click',()=>loadRunHistoryDetail(run.fetch_id,button)); actions.append(button);
      }
      else actions.textContent='—';
      actions.dataset.label=headers[8];
      tr.append(actions); body.append(tr);
    });
    state.runHistoryOffset = data.next_offset ?? state.runHistoryOffset;
    state.runHistoryCursor = data.next_cursor || null;
    $('#run-history-more').hidden = data.next_cursor == null && data.next_offset == null;
    if (!data.items?.length && !append) root.append(element('p',t('runs.no-history')));
  } catch (error) { if (sequence === listSequence) root.replaceChildren(element('p',getErrorMessage(error,'runs.history-error'))); }
  finally { if(sequence===listSequence){root.removeAttribute('aria-busy');$('#run-history-loading').hidden=true;} }
}

function renderRunHistoryDetail(item, errorMessage = null) {
  const root=$('#run-history-detail');
  root.replaceChildren();
  const title=element('h2',t('runs.detail-title')); title.id='run-history-detail-title';
  const close=element('button',t('runs.close-detail'),'button button-secondary');
  close.type='button'; close.addEventListener('click',closeRunHistoryDetail);
  const heading=element('div',null,'panel-heading'); heading.append(title,close);
  root.setAttribute('aria-labelledby',title.id);
  root.append(heading);
  if(errorMessage){const message=element('p',errorMessage); message.setAttribute('role','alert'); root.append(message); return;}
  const list=element('dl','', 'detail-list detail-list--grid');
  const coverage=extractRunSourceCoverage(item);
  const sourceSummary=(item.source_manifest || []).map(source=>{
    const status=localizedStatus('fetch.source-status.',source.status || 'UNKNOWN');
    return `${localizedSourceName(source.source_id)}: ${status}`;
  }).join(' · ') || t('common.unavailable');
  const sourceCoverageSummary=coverage.sources.map(source=>{
    const counts=[
      source.providerReturnedRecords == null ? null : `${t('runs.provider-records')}: ${number(source.providerReturnedRecords)}`,
      source.retainedRecords == null ? null : `${t('runs.source-retained')}: ${number(source.retainedRecords)}`,
      source.rejectedRecords == null ? null : `${t('runs.source-rejected')}: ${number(source.rejectedRecords)}`,
      source.omittedRecords == null ? null : `${t('runs.source-omitted')}: ${number(source.omittedRecords)}`,
    ].filter(Boolean).join(' · ');
    return `${localizedSourceName(source.sourceId)}: ${counts || t('common.unavailable')}`;
  }).join(' | ') || t('common.unavailable');
  const googleSummary=coverage.sources.filter(source=>source.sourceId==='google_web_serp').map(source=>{
    const mode=source.googleDiscoveryMode==='RSS_LIMITED_DISCOVERY'
      ? t('fetch.discovery.rss-limited') : source.googleDiscoveryMode==='SERPAPI_TRENDING_NOW'
        ? t('fetch.discovery.trending-now') : null;
    return [
      mode,
      source.googleCandidateCount==null?null:`${t('runs.google-candidates')}: ${number(source.googleCandidateCount)}`,
      source.googleDistinctKeywordCount==null?null:`${t('runs.google-distinct')}: ${number(source.googleDistinctKeywordCount)}`,
      source.googleVerifiedCount==null?null:`${t('runs.google-verified')}: ${number(source.googleVerifiedCount)}`,
      source.googleProviderDataAt?`${t('runs.google-source-time')}: ${timestamp(source.googleProviderDataAt)}`:null,
      source.googleFetchedAt?`${t('runs.google-fetched-at')}: ${timestamp(source.googleFetchedAt)}`:null,
      source.deliveryState?`${t('runs.google-delivery')}: ${t(`fetch.delivery.${source.deliveryState}`)}`:null,
    ].filter(Boolean).join(' Â· ');
  }).filter(Boolean).join(' | ');
  const quality=(item.quality_flags || []).length
    ? t('runs.outcome.quality-notes',{count:number(item.quality_flags.length)})
    : t('runs.outcome.no-notes');
  const observationStart=item.observation_start ? date(item.observation_start) : null;
  const observationEnd=item.observation_end ? date(item.observation_end) : null;
  const observationWindow=observationStart&&observationEnd
    ? `${observationStart} – ${observationEnd}` : observationStart || observationEnd || t('common.unavailable');
  const values=[[t('runs.run-id'),item.fetch_id],[t('runs.status'),localizedStatus('status.',item.status)],
    [t('runs.mode'),localizedRunMode(item.data_mode)],[t('runs.sources'),sourceSummary],
    [t('runs.source-coverage'),sourceCoverageSummary,'wide'],
    ...(googleSummary?[[t('runs.google-summary'),googleSummary,'wide']]:[]),
    [t('runs.provider-records'),coverage.providerRecordCount],
    [t('runs.observation-window'),observationWindow],
    [t('runs.received'),item.received_record_count ?? item.available],
    [t('runs.retained'),item.retained_record_count ?? item.processed],[t('runs.rejected'),item.rejected_record_count],
    [t('runs.outcome'),localizedRunOutcome(item)],[t('runs.quality'),quality],
    [t('runs.duration'),t('runs.duration-unavailable')],
    [t('runs.date'),item.fetched_at || item.updated_at ? timestamp(item.fetched_at || item.updated_at) : t('common.unavailable')]];
  for(const [label,value,width] of values){const row=element('div');if(width)row.className=width;row.append(element('dt',label),element('dd',typeof value==='number'?number(value):value == null?t('common.unavailable'):value));list.append(row);}
  root.append(list);
}

function closeRunHistoryDetail() {
  const root=$('#run-history-detail');
  if (!root || root.hidden) return;
  state.runHistoryDetailSequence += 1;
  root.hidden=true;
  root.replaceChildren();
  root.removeAttribute('aria-labelledby');
  state.runHistoryDetailId=null;
  state.runHistoryDetailItem=null;
  const trigger=state.runHistoryDetailTrigger;
  state.runHistoryDetailTrigger=null;
  if(trigger?.isConnected){trigger.setAttribute('aria-expanded','false');trigger.focus();}
}

async function loadRunHistoryDetail(fetchId, trigger) {
  const root=$('#run-history-detail');
  if(state.runHistoryDetailTrigger&&state.runHistoryDetailTrigger!==trigger)state.runHistoryDetailTrigger.setAttribute('aria-expanded','false');
  state.runHistoryDetailTrigger=trigger || null;
  state.runHistoryDetailId=fetchId;
  if(trigger)trigger.setAttribute('aria-expanded','true');
  const sequence=++state.runHistoryDetailSequence;
  root.hidden=false;
  const title=element('h2',t('runs.detail-title')); title.id='run-history-detail-title';
  const loading=element('p',t('common.loading')); loading.setAttribute('role','status');
  root.replaceChildren(title,loading); root.setAttribute('aria-labelledby',title.id); root.focus();
  try {
    const response=await getJson('/api/v1/runs/history/'+encodeURIComponent(fetchId),{locale:getLocale()});
    if(sequence!==state.runHistoryDetailSequence||root.hidden)return;
    state.runHistoryDetailItem=response.item || {};
    renderRunHistoryDetail(state.runHistoryDetailItem);
  } catch(error) {
    if(sequence!==state.runHistoryDetailSequence||root.hidden)return;
    state.runHistoryDetailItem=null;
    renderRunHistoryDetail({},getErrorMessage(error,'runs.history-error'));
  }
}

  $('#run-history-refresh').addEventListener('click', () => loadRunHistory());
  $('#run-history-detail').addEventListener('keydown', (event) => { if (event.key === 'Escape') { event.preventDefault(); closeRunHistoryDetail(); } });
  $('#run-history-more').addEventListener('click', () => loadRunHistory({ append: true }));
  $('#run-history-search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadRunHistory(), 250); });
  $('#run-history-status').addEventListener('change', () => loadRunHistory());
  return {
    load: loadRunHistory,
    relocalize() {
      if (window.location.hash === '#runs') loadRunHistory();
      if (!$('#run-history-detail').hidden && state.runHistoryDetailItem) renderRunHistoryDetail(state.runHistoryDetailItem);
    },
  };
}
