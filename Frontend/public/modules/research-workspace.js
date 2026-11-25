import { getJson, requestJson } from './api.js';
import { getLocale, t } from './i18n.js';
import { formatNumber } from './format.js';

const $ = (selector) => document.querySelector(selector);
const number = (value) => formatNumber(value, getLocale());
const element = (tag, text = null, className = '') => {
  const node = document.createElement(tag);
  if (text !== null && text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
};
const getErrorMessage = (error, fallback) => error && typeof error.message === 'string' ? error.message : t(fallback);

export function initializeResearchWorkspace({ announce, localizedSourceName }) {
  const state = { researchKeywords: [], researchEditId: null, researchRecords: [], researchNext: null, researchSequence: 0 };
  let listTimer, keywordTimer;
  let keywordSequence = 0;
async function loadResearchWorkspace() {
  const keywordSeq = ++keywordSequence;
  const recordSeq = ++state.researchSequence;
  const priorKeyword = $('#research-keyword')?.value || '';
  const params = new URLSearchParams();
  const query = $('#research-list-search')?.value.trim() || '';
  const status = $('#research-list-status')?.value || '';
  if (query) params.set('q', query);
  if (status) params.set('status', status);
  const suffix = params.toString();
  try {
    const bootstrap = await getJson('/api/v1/research/bootstrap' + (suffix ? '?' + suffix : ''), {
      locale: getLocale(),
    });
    if (keywordSeq === keywordSequence && bootstrap?.keywords) {
      renderResearchKeywords(bootstrap.keywords, priorKeyword);
    }
    if (recordSeq === state.researchSequence && bootstrap?.research) {
      renderResearchPage(bootstrap.research);
    }
  } catch (error) {
    // Keep compatibility with deployments that have not yet picked up the
    // combined endpoint; do not repeat a failed snapshot read as split calls.
    if (error?.status === 404) {
      await Promise.all([loadResearchKeywords(), loadResearchRecords()]);
      return;
    }
    if (keywordSeq === keywordSequence) {
      $('#research-keyword')?.replaceChildren(new Option(t('research.keywords-unavailable'), ''));
    }
    if (recordSeq === state.researchSequence) {
      const root = $('#research-records');
      if (root) root.replaceChildren(element('p', getErrorMessage(error, 'research.load-error')));
      $('#research-list-count').textContent = '';
      $('#research-list-more').hidden = true;
    }
  }
}

function renderResearchKeywords(data, prior) {
  const select = $('#research-keyword');
  if (!select) return;
  state.researchKeywords = Array.isArray(data.items) ? data.items : [];
  select.replaceChildren(new Option(t('research.no-linked-keyword'), ''), ...state.researchKeywords.map(item => new Option(`${item.keyword} · ${item.source || '—'} · ${item.observed_at || item.observation_date || '—'}`, item.keyword_id || item.id)));
  if (state.researchKeywords.some(item => (item.keyword_id || item.id) === prior)) select.value = prior;
}

function renderResearchPage(result) {
  state.researchRecords = Array.isArray(result.items) ? result.items : [];
  state.researchNext = result.next_offset ?? null;
  $('#research-list-more').hidden = state.researchNext === null;
  $('#research-list-count').textContent = Number.isInteger(result.total)
    ? t('workbench.results-count', { count: number(result.total) }) : t('common.unavailable');
  renderResearchRecords(state.researchRecords);
}

async function loadResearchKeywords(query='') {
  const select=$('#research-keyword'); if(!select)return;
  const sequence=++keywordSequence;
  const prior=select.value; const params=new URLSearchParams({limit:'100',sort:'recent',include_algorithm_results:'false'}); if(query)params.set('q',query);
  try {
    const data=await getJson('/api/v1/datasets/latest/keywords?'+params.toString(),{locale:getLocale()});
    if(sequence!==keywordSequence)return;
    renderResearchKeywords(data, prior);
  } catch(error) { if(sequence===keywordSequence)select.replaceChildren(new Option(t('research.keywords-unavailable'),'')); }
}

async function loadResearchRecords({append = false} = {}) {
  const root=$('#research-records'); if(!root)return;
  const sequence=++state.researchSequence;
  const params=new URLSearchParams({limit:'50',offset:String(append ? state.researchNext ?? 0 : 0)});
  const query=$('#research-list-search').value.trim(), status=$('#research-list-status').value;
  if(query)params.set('q',query);if(status)params.set('status',status);
  try {
    const result=await getJson('/api/v1/research?'+params.toString(),{locale:getLocale()});
    if(sequence!==state.researchSequence)return;
    if (append) {
      state.researchRecords = [...state.researchRecords, ...(result.items || [])];
      state.researchNext = result.next_offset ?? null;
      $('#research-list-more').hidden = state.researchNext === null;
      $('#research-list-count').textContent = Number.isInteger(result.total)
        ? t('workbench.results-count', { count: number(result.total) }) : t('common.unavailable');
      renderResearchRecords(state.researchRecords);
    } else renderResearchPage(result);
  } catch(error) { if(sequence===state.researchSequence){root.replaceChildren(element('p',getErrorMessage(error,'research.load-error')));$('#research-list-count').textContent='';$('#research-list-more').hidden=true;} }
}

function renderResearchRecords(items) {
  const root=$('#research-records'); if(!root)return;
  root.replaceChildren();
  if(!items.length){
    const filtered=Boolean($('#research-list-search').value.trim()||$('#research-list-status').value);
    root.append(element('p',t(filtered?'phoenix.research-no-match':'research.empty')));
    if(filtered){const clear=element('button',t('phoenix.clear-research-filters'),'text-button');clear.type='button';clear.addEventListener('click',()=>{$('#research-list-search').value='';$('#research-list-status').value='';loadResearchRecords();});root.append(clear);}
    return;
  }
  for(const item of items){
    const card=element('article',null,'research-record');
    const heading=element('div',null,'research-record-heading');
    heading.append(element('h3',item.subject));
    const edit=element('button',t('research.edit'),'button button-secondary');edit.type='button';edit.addEventListener('click',()=>beginResearchEdit(item));
    heading.append(edit);
    const status=element('select'); status.setAttribute('aria-label',t('research.status-for',{subject:item.subject}));
    for(const value of ['DRAFT','RESEARCHING','NEEDS_REVIEW','VALIDATED','ARCHIVED'])status.add(new Option(t(['research','status',value.toLowerCase()].join('.')),value));
    status.value=item.status; status.addEventListener('change',async()=>{
      const previous=item.status;
      status.disabled=true;
      try { await requestJson('/api/v1/research/'+encodeURIComponent(item.id),{method:'PATCH',body:{status:status.value},locale:getLocale()});item.status=status.value;await loadResearchRecords(); }
      catch(error){status.value=previous;announce(getErrorMessage(error,'research.save-error'),true);}
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

function beginResearchEdit(item) {
  clearTimeout(keywordTimer);
  keywordSequence++;
  state.researchEditId=item.id;
  const fields={
    '#research-subject':item.subject,
    '#research-hypothesis':item.hypothesis,
    '#research-category':item.product_category,
    '#research-niche':item.niche,
    '#research-competitors':item.competitor_notes,
    '#research-market-evidence':item.market_evidence,
    '#research-ip':item.ip_notes,
    '#research-risks':item.risk_notes,
    '#research-findings':item.findings,
    '#research-status':item.status,
  };
  for(const [selector,value] of Object.entries(fields))$(selector).value=value||'';
  const linkedKeyword=$('#research-keyword');
  if(item.keyword_id && ![...linkedKeyword.options].some(option=>option.value===item.keyword_id)) {
    linkedKeyword.add(new Option([item.keyword || item.keyword_id, localizedSourceName(item.source), item.observed_at || '—'].join(' · '), item.keyword_id));
  }
  linkedKeyword.value=item.keyword_id||'';
  linkedKeyword.disabled=true;
  linkedKeyword.setAttribute('aria-describedby','research-keyword-lock-note');
  $('#research-keyword-search').disabled=true;
  $('#research-keyword-lock-note').hidden=false;
  $('#research-editor-heading').textContent=t('research.edit-title');
  $('#research-form').querySelector('[type="submit"]').textContent=t('research.update');
  $('#research-cancel-edit').hidden=false;
  $('#research-form-status').textContent='';
  $('#research-subject').focus();
  $('#research-form').scrollIntoView({behavior:'smooth',block:'start'});
}

function cancelResearchEdit() {
  state.researchEditId=null;
  $('#research-form').reset();
  $('#research-keyword').value='';
  $('#research-keyword').disabled=false;
  $('#research-keyword').removeAttribute('aria-describedby');
  $('#research-keyword-search').disabled=false;
  $('#research-keyword-lock-note').hidden=true;
  $('#research-editor-heading').textContent=t('research.create-title');
  $('#research-form').querySelector('[type="submit"]').textContent=t('research.save');
  $('#research-cancel-edit').hidden=true;
  $('#research-form-status').textContent='';
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
  const editId=state.researchEditId;
  if(editId)delete payload.keyword_id;
  try { await requestJson(editId?'/api/v1/research/'+encodeURIComponent(editId):'/api/v1/research',{method:editId?'PATCH':'POST',body:payload,locale:getLocale()});
    cancelResearchEdit();
    status.textContent=t(editId?'research.updated':'research.saved');
    await loadResearchRecords();
  } catch(error) { status.textContent=getErrorMessage(error,'research.save-error'); }
}

  $('#research-form').addEventListener('submit', saveResearchRecord);
  $('#research-list-search').addEventListener('input', () => { clearTimeout(listTimer); listTimer = setTimeout(() => loadResearchRecords(), 250); });
  $('#research-list-status').addEventListener('change', () => loadResearchRecords());
  $('#research-list-more').addEventListener('click', () => loadResearchRecords({ append: true }));
  $('#research-cancel-edit').addEventListener('click', cancelResearchEdit);
  $('#research-keyword-search').addEventListener('input', () => { clearTimeout(keywordTimer); keywordTimer = setTimeout(() => loadResearchKeywords($('#research-keyword-search').value.trim()), 250); });
  $('#research-keyword').addEventListener('change', (event) => {
    const item = state.researchKeywords.find(value => (value.keyword_id || value.id) === event.currentTarget.value);
    if (item && !$('#research-subject').value.trim()) $('#research-subject').value = item.keyword;
  });
  return { load: loadResearchWorkspace };
}
