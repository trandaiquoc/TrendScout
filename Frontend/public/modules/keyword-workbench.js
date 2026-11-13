import { getJson, requestJson } from './api.js';
import { formatDate, formatNumber } from './format.js';
import { getLocale, t } from './i18n.js';

const $ = (selector, root = document) => root.querySelector(selector);
const make = (tag, value = '', className = '') => {
  const node = document.createElement(tag);
  if (value !== '') node.textContent = String(value);
  if (className) node.className = className;
  return node;
};
const labels = (key, fallback = key) => { const value = t(key); return value === key ? fallback : value; };
const formatted = (key, values, fallback = key) => { const value = t(key, values); return value === key ? fallback.replace(/\{([a-zA-Z0-9_]+)\}/g, (_, name) => String(values[name] ?? '')) : value; };
const number = (value) => formatNumber(value, getLocale());
const date = (value) => value ? formatDate(value, getLocale()) : labels('common.unavailable', '—');
const state = { items: [], suggestions: [], summary: null, rows: [], total: null, offset: 0, limit: 25, sequence: 0, treeRenderSequence: 0,
  selected: new Set(), allFiltered: false, mode: 'AUTO_CREATE', expanded: new Set(), query: '',
  collectionId: '', filterState: 'all', classification: 'all', source: '', evidence: 'all', sort: 'recent',
  dateFrom: '', dateTo: '', detailSequence: 0, collectionSequence: 0, mobileOpen: false };

function option(select, value, key, fallback = key) {
  const node = make('option', labels(key, fallback)); node.value = value; select.append(node); return node;
}
function safeEvidenceUrl(value) { try { const url=new URL(value);return url.protocol==='https:'&&!url.username&&!url.password&&/^(?:[a-z0-9-]+\.)*(?:wikipedia|wikimedia|wiktionary|wikiquote|wikibooks|wikinews|wikisource|wikiversity|wikivoyage|wikidata|mediawiki|wikifunctions)\.org$/i.test(url.hostname)?url.href:null; } catch { return null; } }
function sourceName(value) { const keys={wikimedia_pageviews_top_per_country:'source.name.wikimedia',youtube_video_search:'source.name.youtube',google_web_serp:'source.name.google',bing_web_serp:'source.name.bing'};return keys[value]?labels(keys[value],value):value||labels('common.unavailable','—'); }
function labelled(select, key, options, fallback = key) {
  const label = make('label', labels(key, fallback), 'workbench-filter'); label.append(select); return label;
}
function button(text, className = 'button button-secondary') { const node = make('button', text, className); node.type = 'button'; return node; }
let collectionPickerSequence = 0;
function collectionPicker(options, label) {
  const root = make('div', '', 'collection-picker');
  const trigger = button(labels('workbench.choose-target', 'Choose a target Collection'), 'collection-picker-trigger');
  trigger.setAttribute('role', 'combobox');
  trigger.setAttribute('aria-haspopup', 'listbox');
  trigger.setAttribute('aria-expanded', 'false');
  trigger.setAttribute('aria-label', label);
  const id = `collection-picker-${++collectionPickerSequence}`;
  const list = make('ul', '', 'collection-picker-list');
  list.id = id;
  list.setAttribute('role', 'listbox');
  list.setAttribute('aria-label', label);
  list.hidden = true;
  trigger.setAttribute('aria-controls', id);
  const choices = options.map((item, index) => {
    const optionNode = make('li', '', 'collection-picker-option');
    optionNode.setAttribute('role', 'option');
    optionNode.setAttribute('aria-selected', 'false');
    optionNode.tabIndex = -1;
    optionNode.dataset.value = item.value;
    optionNode.textContent = item.label;
    optionNode.addEventListener('pointermove', () => setActive(index));
    optionNode.addEventListener('click', () => choose(index));
    list.append(optionNode);
    return optionNode;
  });
  let value = '';
  let active = 0;
  let typeahead = '';
  let typeaheadTimer;
  const setActive = (index) => {
    if (!choices.length) return;
    active = (index + choices.length) % choices.length;
    choices.forEach((node, position) => {
      node.classList.toggle('is-active', position === active);
      if (position === active) list.setAttribute('aria-activedescendant', node.id || (node.id = `${id}-${position}`));
    });
    const choice = choices[active];
    if (choice.offsetTop < list.scrollTop) list.scrollTop = choice.offsetTop;
    else if (choice.offsetTop + choice.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = choice.offsetTop + choice.offsetHeight - list.clientHeight;
  };
  const close = (restoreFocus = false) => {
    list.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    root.classList.remove('is-open');
    if (restoreFocus) trigger.focus();
  };
  const open = () => {
    if (!choices.length) return;
    list.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    root.classList.add('is-open');
    const selectedIndex = options.findIndex((item) => item.value === value);
    setActive(selectedIndex >= 0 ? selectedIndex : 0);
    choices[active].focus();
  };
  const choose = (index) => {
    const item = options[index];
    if (!item) return;
    value = item.value;
    trigger.textContent = item.label;
    trigger.setAttribute('aria-label', `${label}: ${item.label}`);
    choices.forEach((node, position) => node.setAttribute('aria-selected', String(position === index)));
    close(true);
  };
  trigger.addEventListener('click', () => list.hidden ? open() : close());
  trigger.addEventListener('keydown', (event) => {
    if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) { event.preventDefault(); open(); }
  });
  list.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); setActive(active + (event.key === 'ArrowDown' ? 1 : -1)); choices[active].focus(); }
    else if (event.key === 'Home' || event.key === 'End') { event.preventDefault(); setActive(event.key === 'Home' ? 0 : choices.length - 1); choices[active].focus(); }
    else if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); choose(active); }
    else if (event.key === 'Escape') { event.preventDefault(); close(true); }
    else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      typeahead += event.key.toLocaleLowerCase();
      clearTimeout(typeaheadTimer);
      typeaheadTimer = setTimeout(() => { typeahead = ''; }, 650);
      const start = active + 1;
      const match = [...options.slice(start), ...options.slice(0, start)].findIndex((item) => item.label.toLocaleLowerCase().startsWith(typeahead));
      if (match >= 0) { setActive((start + match) % options.length); choices[active].focus(); }
    }
  });
  root.addEventListener('keydown', (event) => { if (event.key === 'Escape' && !list.hidden) { event.preventDefault(); close(true); } });
  root.append(trigger, list);
  queueMicrotask(() => {
    const dialog = root.closest('dialog');
    if (!dialog) return;
    const dismissOutside = (event) => { if (!root.contains(event.target)) close(); };
    const cleanup = () => { dialog.removeEventListener('pointerdown', dismissOutside); clearTimeout(typeaheadTimer); };
    dialog.addEventListener('pointerdown', dismissOutside);
    dialog.addEventListener('close', cleanup, { once: true });
  });
  Object.defineProperty(root, 'value', { get: () => value });
  root.focus = () => trigger.focus();
  return root;
}
function status(text, error = false) { const node = $('#workbench-status'); node.textContent = text; node.classList.toggle('notice--error', error); }
function idsInQuery() { return state.collectionId ? [state.collectionId] : []; }

function build() {
  const root = $('#workbench-root'); root.replaceChildren();
  const toolbar = make('div', '', 'workbench-toolbar');
  const toggle = button(labels('workbench.filters', 'Collections and filters'), 'button button-secondary workbench-menu-toggle');
  toggle.id = 'workbench-filter-toggle'; toggle.setAttribute('aria-expanded', 'false'); toggle.setAttribute('aria-controls', 'workbench-sidebar');
  toggle.addEventListener('click', () => setMobileOpen(!state.mobileOpen));
  const modeLabel = make('label', labels('collections.mode', 'Automatic classification'), 'workbench-mode');
  const mode = make('select'); mode.id = 'workbench-mode';
  option(mode, 'AUTO_CREATE', 'collections.mode-AUTO_CREATE', 'Create Collections');
  option(mode, 'SUGGEST_ONLY', 'collections.mode-SUGGEST_ONLY', 'Suggest only');
  option(mode, 'OFF', 'collections.mode-OFF', 'Off');
  mode.addEventListener('change', async () => {
    try { const result = await requestJson('/api/v1/collections/mode', { method: 'POST', body: { mode: mode.value }, locale: getLocale() }); state.mode = result.mode; status(labels('workbench.mode-saved', 'Classification setting saved')); }
    catch (error) { status(error.message || labels('collections.error', 'Could not save setting'), true); mode.value = state.mode; }
  });
  modeLabel.append(mode);
  const reclassify=button(labels('workbench.reclassify','Organize saved keywords again'),'button button-secondary');reclassify.addEventListener('click',async()=>{reclassify.disabled=true;status(labels('workbench.reclassify-running','Organizing from saved evidence…'));try{const job=await requestJson('/api/v1/collections/reclassify',{method:'POST',locale:getLocale()});await refresh();if(job.status==='MODEL_UNAVAILABLE')status(labels('collections.model-unavailable','Keywords could not be organized automatically. Existing data was kept.'),true);else if(job.status==='COMPLETED')status(formatted('workbench.reclassify-complete',{processed:number(job.processed),eligible:number(job.eligible),assigned:number(job.assigned),unassigned:number(job.unassigned)},'Reviewed {processed} of {eligible} keywords; placed {assigned}; left {unassigned} outside Collections.'));else if(job.status==='FAILED')status(labels('workbench.reclassify-failed','Keywords could not be organized. Existing data was kept.'),true);else status(labels('workbench.reclassify-pending','Organization is still running. Check back shortly.'));}catch(error){status(error.message||labels('collections.error','Could not organize keywords'),true);}finally{reclassify.disabled=false;}});
  toolbar.append(toggle, modeLabel,reclassify);
  root.append(toolbar);
  const statusNode = make('p', '', 'notice notice--info'); statusNode.id = 'workbench-status'; statusNode.setAttribute('role', 'status'); statusNode.setAttribute('aria-live', 'polite'); root.append(statusNode);
  const layout = make('div', '', 'workbench-layout');
  const scrim=button(labels('a11y.close-nav','Close Collection filters'),'workbench-scrim');scrim.setAttribute('aria-label',labels('a11y.close-nav','Close Collection filters'));scrim.hidden=true;scrim.addEventListener('click',()=>setMobileOpen(false));
  const sidebar = make('aside', '', 'panel workbench-sidebar'); sidebar.id = 'workbench-sidebar';
  sidebar.inert=window.innerWidth<=900;
  const sideHead = make('div', '', 'workbench-side-head'); sideHead.append(make('h2', labels('workbench.collections', 'Collections')));
  const treeSearch = make('input'); treeSearch.type = 'search'; treeSearch.maxLength = 100; treeSearch.placeholder = labels('collections.search-placeholder', 'Find a Collection'); treeSearch.setAttribute('aria-label', labels('collections.search', 'Search Collections'));
  treeSearch.addEventListener('input', renderTree);
  sideHead.append(treeSearch); sidebar.append(sideHead);
  const tree = make('nav', '', 'workbench-tree'); tree.id = 'workbench-tree'; tree.setAttribute('aria-label', labels('workbench.collection-tree', 'Collection tree')); sidebar.append(tree);
  const all = button(labels('workbench.all-keywords', 'All keywords'), 'workbench-tree-item'); all.addEventListener('click', () => selectView('', 'all')); all.dataset.virtual = 'all'; tree.append(all);
  const unassigned = button(labels('workbench.unclassified', 'Not in a Collection'), 'workbench-tree-item'); unassigned.addEventListener('click', () => selectView('', 'unassigned')); unassigned.dataset.virtual = 'unassigned'; tree.append(unassigned);
  const proposals=make('details','','workbench-proposals');proposals.id='workbench-proposals';const proposalSummary=make('summary');proposalSummary.id='workbench-proposals-summary';proposals.append(proposalSummary);const proposalList=make('div','','workbench-proposal-list');proposalList.id='workbench-proposal-list';proposals.append(proposalList);sidebar.append(proposals);

  const main = make('main', '', 'workbench-main');
  const summary = make('div', '', 'workbench-summary'); summary.id = 'workbench-summary'; main.append(summary);
  const filters = make('form', '', 'workbench-filters'); filters.addEventListener('submit', (event) => event.preventDefault());
  const query = make('input'); query.type = 'search'; query.maxLength = 100; query.placeholder = labels('keywords.search-placeholder', 'Search keywords'); query.setAttribute('aria-label', labels('keywords.search-label', 'Search keywords')); query.id = 'workbench-query';
  query.addEventListener('input', debounce(() => { state.query = query.value.trim(); state.offset = 0; changedFilters(); }, 250));
  const statusSelect = make('select'); statusSelect.id = 'workbench-state';
  option(statusSelect, 'all', 'workbench.state-all', 'All placement methods'); option(statusSelect, 'unassigned', 'workbench.unclassified', 'Not in a Collection');
  option(statusSelect, 'ai', 'workbench.auto-assigned', 'Organized automatically'); option(statusSelect, 'manual', 'workbench.user-added', 'Added by you');
  option(statusSelect, 'mixed', 'workbench.state-mixed', 'Mixed'); option(statusSelect, 'excluded', 'dataset.state-excluded', 'Excluded');
  statusSelect.addEventListener('change', () => { state.filterState = statusSelect.value; state.offset = 0; changedFilters(); });
  const classification = make('select'); classification.id = 'workbench-classification';
  option(classification, 'all', 'workbench.classification-all', 'All classification results');
  option(classification, 'CLASSIFIED', 'workbench.classified', 'Identified'); option(classification, 'AMBIGUOUS', 'workbench.ambiguous', 'Needs review'); option(classification, 'UNKNOWN', 'workbench.unknown', 'Not enough context');
  classification.addEventListener('change', () => { state.classification = classification.value; state.offset = 0; changedFilters(); });
  const source = make('select'); source.id = 'workbench-source'; option(source, '', 'workbench.all-sources', 'All sources');
  source.addEventListener('change', () => { state.source = source.value; state.offset = 0; changedFilters(); });
  const evidence = make('select'); evidence.id = 'workbench-evidence'; option(evidence, 'all', 'workbench.all-evidence', 'All evidence'); option(evidence, 'metric', 'workbench.has-metric', 'Has metric'); option(evidence, 'algorithm', 'workbench.has-algorithm', 'Has algorithm result'); option(evidence, 'missing', 'workbench.missing-evidence', 'No metric or algorithm');
  evidence.addEventListener('change', () => { state.evidence = evidence.value; state.offset = 0; changedFilters(); });
  const sort = make('select'); sort.id = 'workbench-sort'; option(sort, 'recent', 'dataset.sort-recent', 'Most recent'); option(sort, 'keyword', 'sort.keyword', 'Keyword'); option(sort, 'views', 'sort.views', 'Views'); option(sort, 'rank', 'dataset.sort-rank', 'Rank');
  sort.addEventListener('change', () => { state.sort = sort.value; state.offset = 0; changedFilters(); });
  const from = make('input'); from.type = 'date'; from.setAttribute('aria-label', labels('workbench.from', 'Observed from')); from.id = 'workbench-from'; from.addEventListener('change', () => { state.dateFrom = from.value; state.offset = 0; changedFilters(); });
  const to = make('input'); to.type = 'date'; to.setAttribute('aria-label', labels('workbench.to', 'Observed through')); to.id = 'workbench-to'; to.addEventListener('change', () => { state.dateTo = to.value; state.offset = 0; changedFilters(); });
  const advanced=make('details','','workbench-advanced-filters');
  const advancedSummary=make('summary',labels('workbench.more-filters','More filters'));
  const advancedFields=make('div','','workbench-advanced-fields');
  for (const [control, key, fallback] of [[statusSelect,'workbench.assignment','Assignment'],[classification,'workbench.classification','Classification'],[source,'workbench.source','Source'],[evidence,'workbench.evidence','Evidence']]) advancedFields.append(labelled(control,key,null,fallback));
  const dateRange = make('div', '', 'workbench-date-range'); dateRange.append(from,to); advancedFields.append(dateRange);
  advanced.append(advancedSummary,advancedFields);
  filters.append(labelled(query,'keywords.search-label','Search'),labelled(sort,'keywords.sort-label','Sort'),advanced);
  query.value=state.query;statusSelect.value=state.filterState;classification.value=state.classification;evidence.value=state.evidence;sort.value=state.sort;
  from.value=state.dateFrom;to.value=state.dateTo;
  main.append(filters);
  const bulk = make('div', '', 'workbench-bulk'); bulk.id = 'workbench-bulk';
  const pageCheck = make('input'); pageCheck.type = 'checkbox'; pageCheck.id = 'workbench-page-check'; pageCheck.addEventListener('change', () => { if(pageCheck.checked&&state.selected.size+state.rows.filter(item=>!state.selected.has(item.id)).length>500){pageCheck.checked=false;status(labels('workbench.selection-limit','Select at most 500 rows manually, or use Select all matching for larger results.'),true);return;}state.allFiltered = false; for (const item of state.rows) pageCheck.checked ? state.selected.add(item.id) : state.selected.delete(item.id); renderTable(); });
  const pageLabel = make('label', labels('dataset.select-page', 'Select page')); pageLabel.prepend(pageCheck); bulk.append(pageLabel);
  const allButton = button(labels('dataset.select-filtered', 'Select all matching results'), 'text-button'); allButton.addEventListener('click', () => { state.allFiltered = true; state.selected.clear(); renderTable(); }); bulk.append(allButton);
  const clearSelection=button(labels('workbench.clear-selection','Clear selection'),'text-button');clearSelection.addEventListener('click',()=>{state.allFiltered=false;state.selected.clear();renderTable();});bulk.append(clearSelection);
  const count = make('span'); count.id = 'workbench-selection-count'; bulk.append(count);
  const action = make('select'); action.id = 'workbench-action'; option(action, '', 'workbench.choose-action', 'Choose action');
  for (const [value,key,fallback] of [['ADD','dataset.action-add','Add'],['COPY','dataset.action-copy','Copy'],['MOVE','dataset.action-move','Move'],['REMOVE','dataset.action-remove','Remove'],['APPROVE','dataset.action-approve','Approve'],['REJECT','dataset.action-reject','Reject'],['EXCLUDE','dataset.action-exclude','Exclude'],['RESTORE','dataset.action-restore','Restore']]) option(action,value,key,fallback);
  const target = make('select'); target.id = 'workbench-target'; target.hidden = true; option(target, '', 'dataset.choose-collection', 'Choose Collection');
  const sourceTarget = make('select'); sourceTarget.id = 'workbench-source-target'; option(sourceTarget, '', 'workbench.source-collection', 'Source Collection'); sourceTarget.hidden = true;
  action.addEventListener('change', () => { const needs = !['','EXCLUDE','RESTORE'].includes(action.value); target.hidden = !needs; sourceTarget.hidden = action.value !== 'MOVE'; });
  const apply = button(labels('dataset.apply', 'Apply')); apply.addEventListener('click', runBulk);
  bulk.append(action,target,sourceTarget,apply); main.append(bulk);
  const tablePanel = make('div', '', 'panel table-panel workbench-table-panel');
  const tableWrap = make('div', '', 'table-wrap workbench-table-wrap');
  const table = make('table', '', 'workbench-table');
  const head = make('thead'); const header = make('tr'); header.append(make('th',labels('dataset.select-page','Select')));
  for (const key of ['workbench.keyword','workbench.collection','workbench.source','workbench.metric','table.date','workbench.classification']) header.append(make('th', labels(key, key)));
  const actionHead = make('th', labels('table.details','Details')); header.append(actionHead); head.append(header); table.append(head);
  const body = make('tbody'); body.id = 'workbench-rows'; table.append(body); tableWrap.append(table); tablePanel.append(tableWrap);
  const empty = make('div', '', 'empty-state'); empty.id = 'workbench-empty'; tablePanel.append(empty);
  const footer = make('div', '', 'table-footer workbench-footer'); const pageInfo = make('span'); pageInfo.id='workbench-page-info'; footer.append(pageInfo);
  const pager=make('div','','pagination'); const prev=button('←'); prev.id='workbench-prev'; prev.setAttribute('aria-label',labels('a11y.previous-page','Previous page')); prev.addEventListener('click',()=>{state.offset=Math.max(0,state.offset-state.limit);loadRows();});
  const next=button('→'); next.id='workbench-next'; next.setAttribute('aria-label',labels('a11y.next-page','Next page')); next.addEventListener('click',()=>{state.offset+=state.limit;loadRows();}); pager.append(prev,next); footer.append(pager); tablePanel.append(footer); main.append(tablePanel);
  layout.append(scrim,sidebar,main); root.append(layout);
  const dialog=make('dialog','','collection-dialog workbench-detail'); dialog.id='workbench-detail'; dialog.addEventListener('close',()=>{state.detailSequence+=1;}); root.append(dialog);
  const formDialog=make('dialog','','collection-dialog'); formDialog.id='workbench-collection-dialog'; root.append(formDialog);
  renderTree();
}

function debounce(fn, wait) { let timer; return (...args) => { clearTimeout(timer); timer=setTimeout(()=>fn(...args),wait); }; }
function queryParams() {
  const params=new URLSearchParams({q:state.query,state:state.filterState,classification:state.classification,evidence:state.evidence,sort:state.sort,limit:String(state.limit),offset:String(state.offset)});
  if(state.source) params.set('source',state.source); if(state.dateFrom) params.set('date_from',state.dateFrom); if(state.dateTo) params.set('date_to',state.dateTo);
  if(state.collectionId) params.append('collection_ids',state.collectionId); return params;
}
function updateRoute() {
  const url=new URL(window.location.href); for(const key of ['q','state','classification','source','evidence','sort','from','to','collection_id','offset']) url.searchParams.delete(key);
  if(state.query) url.searchParams.set('q',state.query); if(state.filterState!=='all') url.searchParams.set('state',state.filterState);
  if(state.classification!=='all') url.searchParams.set('classification',state.classification); if(state.source) url.searchParams.set('source',state.source);
  if(state.evidence!=='all') url.searchParams.set('evidence',state.evidence); if(state.sort!=='recent') url.searchParams.set('sort',state.sort);
  if(state.dateFrom) url.searchParams.set('from',state.dateFrom); if(state.dateTo) url.searchParams.set('to',state.dateTo); if(state.collectionId) url.searchParams.set('collection_id',state.collectionId); if(state.offset) url.searchParams.set('offset',String(state.offset));
  window.history.replaceState(null,'',url.pathname+url.search+url.hash);
}
function changedFilters() { state.selected.clear(); state.allFiltered=false; updateRoute(); loadRows(); }
function selectView(id, filter) { state.collectionId=id; state.filterState=filter; state.offset=0; $('#workbench-state').value=filter; state.selected.clear(); state.allFiltered=false; if(window.innerWidth<=900&&state.mobileOpen)setMobileOpen(false); renderTree(); updateRoute(); loadRows(); }
function nameOf(item) { const local=t(`topic.${item.category_id}`); return item.category_id&&local!==`topic.${item.category_id}`?local:item.name; }
function renderTree() {
  const tree=$('#workbench-tree'); if(!tree) return;
  const sidebar=tree.closest('.workbench-sidebar');const scrollTop=sidebar?.scrollTop??0;const pageX=window.scrollX,pageY=window.scrollY;const renderSequence=++state.treeRenderSequence;
  const focused=document.activeElement;const focusedRow=focused?.closest?.('[data-collection-node]');const focusId=focusedRow?.dataset.collectionId;const focusClass=focused?.classList.contains('workbench-disclosure')?'workbench-disclosure':focused?.classList.contains('workbench-tree-item')?'workbench-tree-item':focused?.classList.contains('workbench-context-button')?'workbench-context-button':'';
  const search=$('input[type="search"]',tree.parentElement)?.value.trim().toLocaleLowerCase()||'';
  for(const node of [...tree.querySelectorAll('[data-collection-node],.workbench-tree-children')]) node.remove();
  const children=new Map(); for(const item of state.items){const key=item.parent_id||''; if(!children.has(key))children.set(key,[]); children.get(key).push(item);}
  if(state.collectionId){const byId=new Map(state.items.map(item=>[item.id,item]));let parent=byId.get(state.collectionId)?.parent_id;const seen=new Set();while(parent&&byId.has(parent)&&!seen.has(parent)){seen.add(parent);state.expanded.add(parent);parent=byId.get(parent).parent_id;}}
  const hasMatch=(item,seen=new Set())=>{if(!search)return false;if(seen.has(item.id))return false;seen.add(item.id);if(`${nameOf(item)} ${item.description||''}`.toLocaleLowerCase().includes(search))return true;return(children.get(item.id)||[]).some(child=>hasMatch(child,seen));};
  const append=(parent,parentId='',depth=0)=>{for(const item of children.get(parentId)||[]){const matches=!search||`${nameOf(item)} ${item.description||''}`.toLocaleLowerCase().includes(search); const descendants=children.get(item.id)||[];
       const row=make('div','','workbench-tree-row'); row.dataset.collectionNode='';row.dataset.collectionId=item.id; row.style.setProperty('--tree-depth',depth); const active=state.collectionId===item.id;const descendantMatch=descendants.some(child=>hasMatch(child));if(search&&descendantMatch)state.expanded.add(item.id);
       if(descendants.length){const disclosure=button(state.expanded.has(item.id)?'⌄':'›','workbench-disclosure'); disclosure.setAttribute('aria-expanded',String(state.expanded.has(item.id))); disclosure.setAttribute('aria-label',labels(state.expanded.has(item.id)?'workbench.collapse':'workbench.expand',state.expanded.has(item.id)?'Collapse':'Expand')); disclosure.addEventListener('click',()=>{state.expanded.has(item.id)?state.expanded.delete(item.id):state.expanded.add(item.id);renderTree();}); row.append(disclosure);} else row.append(make('span','','workbench-disclosure-spacer'));
      const choose=button(nameOf(item),'workbench-tree-item'); choose.classList.toggle('is-active',active); choose.setAttribute('aria-current',active?'page':'false'); choose.addEventListener('click',()=>selectView(item.id,'all'));
       const countValue=descendants.length?(item.subtree_unique_keyword_count??item.direct_membership_count??item.membership_count):(item.direct_membership_count??item.membership_count);const count=make('span',displayCount(countValue),'workbench-tree-count'); count.title=countValue===null||countValue===undefined?labels('workbench.count-unavailable','Keyword count unavailable'):formatted(descendants.length?'workbench.subtree-count':'workbench.direct-count',{count:number(countValue)},descendants.length?'{count} unique keywords in this Collection and its subcollections':'{count} keywords in this Collection'); count.setAttribute('aria-label',count.title); choose.append(count); row.append(choose);
      const menu=button('•••','workbench-context-button'); menu.setAttribute('aria-label',labels('workbench.collection-actions','Collection actions')); menu.addEventListener('click',()=>openManageDialog(item)); row.append(menu);
       if(!matches&&search&&!descendantMatch) row.hidden=true; parent.append(row);
       if(descendants.length&&(state.expanded.has(item.id)||search&&descendantMatch)){const group=make('div','','workbench-tree-children');group.setAttribute('role','group');append(group,item.id,depth+1);parent.append(group);}
    }};
  append(tree); for(const node of tree.querySelectorAll('[data-virtual]'))node.classList.toggle('is-active',node.dataset.virtual==='all'?!state.collectionId&&state.filterState==='all':!state.collectionId&&state.filterState==='unassigned');
  const replacement=focusId&&focusClass?[...tree.querySelectorAll('[data-collection-node]')].find(row=>row.dataset.collectionId===focusId)?.querySelector(`.${focusClass}`):null;
  requestAnimationFrame(()=>{if(renderSequence!==state.treeRenderSequence)return;if(sidebar)sidebar.scrollTop=scrollTop;window.scrollTo(pageX,pageY);replacement?.focus({preventScroll:true});});
}
function setMobileOpen(open){state.mobileOpen=open;const sidebar=$('#workbench-sidebar'),toggle=$('#workbench-filter-toggle'),scrim=$('.workbench-scrim'); sidebar?.classList.toggle('is-open',open);if(sidebar)sidebar.inert=window.innerWidth<=900&&!open;if(scrim)scrim.hidden=!open; if(toggle){toggle.setAttribute('aria-expanded',String(open));if(open)sidebar.querySelector('input')?.focus({preventScroll:true});else toggle.focus({preventScroll:true});}}

async function refresh() {
  try {
    const sequence=++state.collectionSequence;
    const response=await getJson('/api/v1/collections',{locale:getLocale()});if(sequence!==state.collectionSequence)return;state.items=response.items||[];state.suggestions=response.suggestions||[];state.summary=response.summary??null;state.mode=response.mode||'AUTO_CREATE';
    const summaryFields=['eligible_keywords','assigned_keywords','unclassified_keywords','excluded_keywords','classified_keywords','ambiguous_keywords','unknown_keywords','ineligible_topic_keywords','ai_assigned_keywords','manually_managed_keywords','ai_created_collections'];
    if(!state.summary||summaryFields.some(key=>typeof state.summary[key]!=='number'))status(labels('workbench.summary-unavailable','Some keyword counts are not available yet. Check the connection and reload.'),true);
    const mode=$('#workbench-mode');if(mode)mode.value=state.mode;
    const source=$('#workbench-source');if(source){const current=state.source;source.replaceChildren();option(source,'','workbench.all-sources','All sources');for(const value of response.summary?.sources||[]){const op=make('option',sourceName(value));op.value=value;source.append(op);}source.value=current;}
    const target=$('#workbench-target'),moveSource=$('#workbench-source-target'); if(target){target.replaceChildren();option(target,'','dataset.choose-collection','Choose Collection');for(const item of state.items){const op=make('option',nameOf(item));op.value=item.id;target.append(op);}}
    if(moveSource){moveSource.replaceChildren();option(moveSource,'','workbench.source-collection','Source Collection');for(const item of state.items){const op=make('option',nameOf(item));op.value=item.id;moveSource.append(op);}}
    renderTree();renderSuggestions();renderSummary();await loadRows();
  } catch(error){state.items=[];state.suggestions=[];state.summary=null;state.rows=[];state.total=null;state.selected.clear();state.allFiltered=false;renderTree();renderSuggestions();renderSummary();renderTable();status(error.message||labels('collections.error','Could not load Collections'),true);}
}
function displayCount(value){return value===null||value===undefined?labels('common.unavailable','—'):number(value);}
function renderSummary(){const summary=$('#workbench-summary');if(!summary)return;summary.replaceChildren();const s=state.summary||{};for(const [key,value] of [['workbench.keywords',s.eligible_keywords],['workbench.in-collections',s.assigned_keywords],['workbench.unclassified',s.unclassified_keywords],['workbench.excluded',s.excluded_keywords]]){const card=make('div','','workbench-stat');card.append(make('span',labels(key,key)),make('strong',displayCount(value)));summary.append(card);}const note=make('p','','workbench-summary-note');note.textContent=formatted('workbench.summary-detail',{classified:displayCount(s.classified_keywords),ambiguous:displayCount(s.ambiguous_keywords),unknown:displayCount(s.unknown_keywords),ineligible:displayCount(s.ineligible_topic_keywords),automatic:displayCount(s.ai_assigned_keywords),manual:displayCount(s.manually_managed_keywords),created:displayCount(s.ai_created_collections)},'Classification: {classified} clear, {ambiguous} uncertain, {unknown} lacking context, {ineligible} not a topic. Placed automatically: {automatic}; added by you: {manual}; new Collections: {created}.');summary.append(note);}
function renderSuggestions(){const details=$('#workbench-proposals'),summary=$('#workbench-proposals-summary'),list=$('#workbench-proposal-list');if(!details||!summary||!list)return;summary.textContent=formatted('workbench.proposals',{count:number(state.suggestions.length)},'Suggested topics ({count})');list.replaceChildren();details.hidden=!state.suggestions.length;for(const proposal of state.suggestions){const card=make('article','','workbench-proposal');card.append(make('strong',proposal.name),make('p',proposal.description||labels('common.unavailable','Unavailable')));let support=[];try{support=Array.isArray(proposal.support_ids)?proposal.support_ids:JSON.parse(proposal.support_ids||'[]');}catch{}card.append(make('small',formatted('workbench.proposal-support',{count:number(support.length)},'Supported by {count} keywords')));const actions=make('div','','workbench-proposal-actions');const accept=button(labels('collections.accept','Accept'));accept.addEventListener('click',async()=>{try{await requestJson(`/api/v1/collections/suggestions/${encodeURIComponent(proposal.id)}/accept`,{method:'POST',locale:getLocale()});await refresh();status(labels('workbench.proposal-accepted','Topic proposal added as a Collection.'));}catch(error){status(error.message||labels('collections.error','Could not accept proposal'),true);}});const reject=button(labels('collections.reject','Dismiss'));reject.addEventListener('click',async()=>{try{await requestJson(`/api/v1/collections/suggestions/${encodeURIComponent(proposal.id)}/reject`,{method:'POST',locale:getLocale()});await refresh();status(labels('workbench.proposal-dismissed','Topic proposal dismissed.'));}catch(error){status(error.message||labels('collections.error','Could not dismiss proposal'),true);}});actions.append(accept,reject);card.append(actions);list.append(card);}}
async function loadRows(){const seq=++state.sequence;const body=$('#workbench-rows');if(!body)return;try{const result=await getJson('/api/v1/datasets/latest/keywords?'+queryParams(),{locale:getLocale()});if(seq!==state.sequence)return;state.rows=Array.isArray(result.items)?result.items:[];state.total=Number.isInteger(result.total)&&result.total>=0?result.total:null;renderTable();if(state.total===null)status(labels('workbench.rows-unavailable','Keyword count is unavailable. Reload to try again.'),true);}catch(error){if(seq===state.sequence){state.rows=[];state.total=null;renderTable();status(error.message||labels('error.keyword-fetch','Could not load keywords'),true);}}}
function renderTable(){const body=$('#workbench-rows');if(!body)return;body.replaceChildren();const empty=$('#workbench-empty');empty.replaceChildren();empty.hidden=state.rows.length>0; if(!state.rows.length){const failed=state.total===null;empty.append(make('strong',failed?labels('workbench.rows-unavailable','Keyword count is unavailable. Reload to try again.'):state.total>0?labels('workbench.no-filter-results','No keywords match these filters'):labels('workbench.no-keywords','No stored keywords yet')),make('p',failed?labels('workbench.rows-unavailable-help','The keyword list could not be confirmed. Your saved data has not been changed.'):labels('workbench.empty-help','Change filters or collect data to populate the workbench.')));if(state.total===0&&([state.query,state.source,state.dateFrom,state.dateTo,state.collectionId].some(Boolean)||state.filterState!=='all'||state.classification!=='all'||state.evidence!=='all')){const clear=button(labels('workbench.clear-filters','Clear filters'),'text-button');clear.addEventListener('click',()=>{state.query='';state.source='';state.dateFrom='';state.dateTo='';state.collectionId='';state.filterState='all';state.classification='all';state.evidence='all';state.offset=0;$('#workbench-query').value='';$('#workbench-state').value='all';$('#workbench-classification').value='all';$('#workbench-evidence').value='all';$('#workbench-source').value='';$('#workbench-from').value='';$('#workbench-to').value='';renderTree();changedFilters();});empty.append(clear);}}
  for(const item of state.rows){const row=make('tr');const selection=make('td');selection.dataset.label=labels('dataset.select-page','Select');const check=make('input');check.type='checkbox';check.checked=state.selected.has(item.id);check.setAttribute('aria-label',`${labels('workbench.select','Select')} ${item.keyword}`);check.addEventListener('change',()=>{if(check.checked&&state.selected.size>=500){check.checked=false;status(labels('workbench.selection-limit','Select at most 500 rows manually, or use Select all matching for larger results.'),true);return;}state.allFiltered=false;check.checked?state.selected.add(item.id):state.selected.delete(item.id);renderSelection();});selection.append(check);row.append(selection);
    const title=make('td');title.dataset.label=labels('workbench.keyword','Keyword');title.append(make('strong',item.keyword),make('small',sourceName(item.source)));row.append(title);
    const collection=make('td');collection.dataset.label=labels('workbench.collection','Collection');collection.append(make('span',(item.memberships||[]).map(m=>nameOf(m)).join(', ')||labels(item.eligible===0?'dataset.excluded':'workbench.unclassified',item.eligible===0?'Excluded':'Not in a Collection')));row.append(collection);
    const source=make('td',sourceName(item.source));source.dataset.label=labels('workbench.source','Source');row.append(source);
    const metric=make('td');metric.dataset.label=labels('workbench.metric','Metric');const evidence=item.evidence&&typeof item.evidence==='object'?item.evidence:{};const evs=Array.isArray(evidence.evidence)?evidence.evidence:[];const measured=evs.find(e=>e.views_ceil!=null||e.approximate_views!=null)||evidence;const metricValue=measured.views_ceil??measured.approximate_views;metric.textContent=metricValue!=null?`${number(metricValue)} ${labels('workbench.views','views')}`:labels('workbench.no-metric','No metric');row.append(metric);
    const observed=make('td',date(item.observed_at));observed.dataset.label=labels('table.date','Observed');row.append(observed);
    const classification=make('td',labels(`classification.${item.classification_status||'UNKNOWN'}`,item.classification_status||'Unknown'));classification.dataset.label=labels('workbench.classification','Classification');row.append(classification);
    const details=make('td');details.dataset.label=labels('table.details','Details');const open=button(labels('workbench.details','Open details'),'text-button');open.addEventListener('click',()=>openDetail(item.id));details.append(open);row.append(details);body.append(row);}
  const prev=$('#workbench-prev'),next=$('#workbench-next');prev.disabled=state.total===null||state.offset===0;next.disabled=state.total===null||state.offset+state.rows.length>=state.total;$('#workbench-page-info').textContent=state.total===null?displayCount(null):state.total?`${number(state.offset+1)}–${number(state.offset+state.rows.length)} / ${number(state.total)}`:labels('table.zero-results','No results');$('#workbench-page-check').checked=!!state.rows.length&&state.rows.every(item=>state.selected.has(item.id));$('#workbench-page-check').disabled=state.allFiltered||state.total===null;renderSelection();
}
function renderSelection(){const count=state.allFiltered?state.total:state.selected.size;$('#workbench-selection-count').textContent=state.allFiltered?formatted('dataset.all-filtered-selected',{count:number(count)},'All {count} filtered keywords selected'):formatted('dataset.selected',{count:number(count)},'{count} selected');$('#workbench-bulk').classList.toggle('has-selection',count>0);}
async function runBulk(){const action=$('#workbench-action').value;if(!action||(!state.allFiltered&&!state.selected.size))return;const body={action,keyword_ids:state.allFiltered?[]:[...state.selected],all_filtered:state.allFiltered};if(!['EXCLUDE','RESTORE'].includes(action)){if(!$('#workbench-target').value){status(labels('dataset.choose-collection-error','Choose a Collection first'),true);return;}body.collection_id=$('#workbench-target').value;}if(state.allFiltered){const p=queryParams();body.query=p.get('q');body.state=p.get('state');body.classification=p.get('classification');body.evidence=p.get('evidence');body.source=p.get('source')||'';body.date_from=p.get('date_from');body.date_to=p.get('date_to');body.collection_ids=p.getAll('collection_ids');}if(action==='MOVE'){if(!$('#workbench-source-target').value){status(labels('workbench.choose-source','Choose the source Collection'),true);return;}body.source_collection_id=$('#workbench-source-target').value;}
  try{const result=await requestJson('/api/v1/collections/bulk',{method:'POST',body,locale:getLocale()});state.selected.clear();state.allFiltered=false;status(formatted('collections.bulk-result',{updated:number(result.updated??0)},'{updated} updated'));await refresh();}catch(error){status(error.message||labels('collections.error','Could not apply action'),true);}}

async function openDetail(id){const dialog=$('#workbench-detail');const sequence=++state.detailSequence;dialog.replaceChildren();const head=make('div','','panel-heading');head.append(make('h2',labels('detail.loading','Loading keyword details')));const close=button('×','text-button');close.setAttribute('aria-label',labels('collections.cancel','Close'));close.addEventListener('click',()=>dialog.close());head.append(close);dialog.append(head);if(!dialog.open)dialog.showModal();try{const detail=await getJson(`/api/v1/collections/keywords/${encodeURIComponent(id)}`,{locale:getLocale()});if(sequence!==state.detailSequence||!dialog.open)return;dialog.replaceChildren();const title=make('div','','panel-heading');title.append(make('h2',detail.keyword));const done=button('×','text-button');done.setAttribute('aria-label',labels('collections.cancel','Close'));done.addEventListener('click',()=>dialog.close());title.append(done);dialog.append(title);
  const fields=make('dl','','workbench-detail-grid');const add=(label,value)=>{const wrap=make('div');wrap.append(make('dt',label),make('dd',value??labels('common.unavailable','Unavailable')));fields.append(wrap);};
  const evidenceData=detail.evidence&&typeof detail.evidence==='object'?detail.evidence:{};const evidenceRows=Array.isArray(evidenceData.evidence)?evidenceData.evidence:[];const metricRecord=evidenceRows.find(item=>item.views_ceil!=null||item.approximate_views!=null)||evidenceData;const metricValue=metricRecord.views_ceil??metricRecord.approximate_views;const metricText=metricValue==null?labels('workbench.no-metric','No metric'):`${number(metricValue)} ${labels('workbench.views','approx. pageviews')}`;
  const decision=detail.user_decision;const decisionLabel=labels(`workbench.decision-${decision?.decision||'NONE'}`,decision?.decision||labels('workbench.no-decision','No decision recorded'));add(labels('workbench.source','Source'),sourceName(detail.source));add(labels('table.date','Observed'),date(detail.observed_at));add(labels('workbench.metric','Observed metric'),metricText);add(labels('workbench.classification','Classification'),labels(`classification.${detail.classification_status||'UNKNOWN'}`,detail.classification_status||'Unknown'));add(labels('workbench.assignment','Assignment'),(detail.memberships||[]).map(m=>`${nameOf(m)} · ${labels(m.origin==='LOCAL_AI'?'workbench.auto-assigned':'workbench.user-added',m.origin==='LOCAL_AI'?'Organized automatically':'Added by you')}${m.pinned?' · pinned':''}`).join(', ')||labels('dataset.unassigned','Not in a Collection'));add(labels('workbench.decision','User decision'),[decisionLabel,decision?.reason].filter(Boolean).join(' · '));if(detail.user_feedback?.length)add(labels('workbench.feedback','User feedback'),detail.user_feedback.map(f=>[labels(`workbench.feedback-${f.action}`,f.action),f.name||labels('workbench.removed-collection','Deleted Collection'),date(f.created_at)].filter(Boolean).join(' · ')).join(' / '));
  const classificationDetails=make('details');classificationDetails.append(make('summary',labels('workbench.classification-evidence','How this keyword was classified')));const explanation=make('dl','','workbench-detail-grid');const addExplanation=(label,value)=>{const wrap=make('div');wrap.append(make('dt',label),make('dd',value??labels('common.unavailable','Unavailable')));explanation.append(wrap);};addExplanation(labels('workbench.reason','Classification evidence'),(detail.classification_rule_evidence||[]).map(entry=>entry.matched_text||entry.description||entry.kind).filter(Boolean).join(' · ')||labels('workbench.no-evidence','No stored explanation'));addExplanation(labels('workbench.entity','Entity context'),[detail.classified_entity_description||detail.entity_description,detail.classified_entity_type||detail.entity_type].filter(Boolean).join(' · ')||labels('common.unavailable','Unavailable'));addExplanation(labels('workbench.method','Method and version'),[detail.classification_method,detail.classification_version,detail.taxonomy_version].filter(Boolean).join(' · ')||labels('common.unavailable','Unavailable'));classificationDetails.append(explanation);
  const sourceUrl=safeEvidenceUrl(detail.evidence?.source_url||detail.entity_source_url);if(sourceUrl){const link=make('a',labels('workbench.open-source','Open source evidence'));link.href=sourceUrl;link.target='_blank';link.rel='noopener noreferrer';dialog.append(link);}
  const evidence=make('details');evidence.append(make('summary',labels('workbench.raw-evidence','Source evidence')));const pre=make('pre');pre.textContent=JSON.stringify(detail.evidence||{},null,2);evidence.append(pre);dialog.append(fields,classificationDetails,evidence);
  const algorithm=make('details');algorithm.append(make('summary',labels('workbench.algorithm-evidence','Algorithm results')));const algorithmPre=make('pre');algorithmPre.textContent=JSON.stringify(detail.algorithm_results||[],null,2);algorithm.append(algorithmPre);dialog.append(algorithm);
  const note=make('p',labels('workbench.detail-limit','Classification and algorithm outputs describe stored evidence; they are not validated trend probabilities.'));note.className='detail-limit';const limits=make('details');limits.append(make('summary',labels('workbench.data-limitations','Data coverage limitations')));limits.append(make('p',labels('workbench.metric-limit','Approximate Wikimedia pageviews are not search volume, sales volume, or purchase intent. A single top-list observation does not prove growth. Missing observation days are unavailable, not zero.')));dialog.append(note,limits);dialog.querySelector('button')?.focus();
 }catch(error){if(sequence===state.detailSequence){dialog.replaceChildren(make('p',error.message||labels('detail.load-error','Unable to load detail')));}}}

function openCollectionDialog(item=null){const dialog=$('#workbench-collection-dialog');dialog.replaceChildren();const form=make('form');form.addEventListener('submit',async event=>{event.preventDefault();const fields=new FormData(form);const payload={name:String(fields.get('name')||'').trim(),description:String(fields.get('description')||'').trim(),examples:String(fields.get('examples')||'').split(';').map(v=>v.trim()).filter(Boolean),exclusions:String(fields.get('exclusions')||'').split(';').map(v=>v.trim()).filter(Boolean),auto_match:fields.get('auto_match')==='on'};const parent=String(fields.get('parent_id')||'');if(!item&&parent)payload.parent_id=parent;try{await requestJson(item?`/api/v1/collections/${encodeURIComponent(item.id)}`:'/api/v1/collections',{method:item?'PATCH':'POST',body:payload,locale:getLocale()});dialog.close();status(labels('collections.saved','Collection saved'));await refresh();}catch(error){status(error.message||labels('collections.error','Could not save Collection'),true);}});
 const heading=make('div','','panel-heading');heading.append(make('h2',labels(item?'collections.edit':'collections.create',item?'Edit Collection':'New Collection')));const close=button('×','text-button');close.setAttribute('aria-label',labels('collections.cancel','Close'));close.addEventListener('click',()=>dialog.close());heading.append(close);form.append(heading);
 const field=(name,key,value='')=>{const label=make('label',labels(key,key));const input=name==='description'||name==='examples'||name==='exclusions'?make('textarea'):make('input');input.name=name;input.value=value;label.append(input);form.append(label);};field('name','collections.name',item?.name||'');form.querySelector('[name="name"]').required=true;field('description','collections.context',item?.description||'');field('examples','collections.examples',(item?.examples||[]).join('; '));field('exclusions','collections.exclusions',(item?.exclusions||[]).join('; '));
 if(!item){const parentLabel=make('label',labels('workbench.parent','Parent Collection'));const parent=make('select');parent.name='parent_id';option(parent,'','workbench.no-parent','No parent');for(const candidate of state.items){const opt=make('option',nameOf(candidate));opt.value=candidate.id;parent.append(opt);}parentLabel.append(parent);form.append(parentLabel);}
 const autoLabel=make('label','','collection-auto-setting');const auto=make('input');auto.type='checkbox';auto.name='auto_match';auto.checked=item?.auto_match??true;autoLabel.append(auto,make('span',labels('collections.enable-auto','Allow automatic matching')));form.append(autoLabel);
 const actions=make('div','','collection-actions');const cancel=button(labels('collections.cancel','Cancel'));cancel.addEventListener('click',()=>dialog.close());const save=make('button',labels('collections.save','Save'),'button button-primary');save.type='submit';actions.append(cancel,save);form.append(actions);dialog.append(form);dialog.showModal();form.querySelector('[name="name"]').focus();}

function openManageDialog(item){
  const dialog=$('#workbench-collection-dialog');dialog.replaceChildren();const panel=make('div','','collection-manage-panel');
  const heading=make('div','','panel-heading');heading.append(make('h2',nameOf(item)));const close=button('×','text-button');close.setAttribute('aria-label',labels('collections.cancel','Close'));close.addEventListener('click',()=>dialog.close());heading.append(close);panel.append(heading);
  panel.append(make('p',labels(item.created_by==='AI'?'workbench.created-automatically':'workbench.created-by-you',item.created_by==='AI'?'Created automatically':'Created by you'),'workbench-origin-note'));

  const settings=make('section','','collection-manage-section');settings.append(make('h3',labels('workbench.manage-settings','Collection settings')));const settingsActions=make('div','','collection-manage-actions');
  const edit=button(labels('collections.edit','Edit'));edit.addEventListener('click',()=>{dialog.close();openCollectionDialog(item);});settingsActions.append(edit);
  const auto=button(labels(item.auto_match?'workbench.disable-auto':'workbench.enable-auto',item.auto_match?'Stop automatic keyword placement':'Allow automatic keyword placement'));auto.setAttribute('aria-pressed',String(item.auto_match));auto.addEventListener('click',async()=>{try{await requestJson(`/api/v1/collections/${encodeURIComponent(item.id)}`,{method:'PATCH',body:{auto_match:!item.auto_match},locale:getLocale()});dialog.close();await refresh();status(labels('collections.saved','Collection saved'));}catch(error){status(error.message||labels('collections.error','Could not update Collection'),true);}});settingsActions.append(auto);settings.append(settingsActions);panel.append(settings);

  const mergeSection=make('section','','collection-manage-section');mergeSection.append(make('h3',labels('workbench.manage-merge','Merge into another Collection')));
  if(item.category_id){
    mergeSection.append(make('p',labels('workbench.merge-system-help','System Collections cannot be merged. Create a personal Collection to organize keywords separately.')));
  }else{
    const byId=new Map(state.items.map(value=>[value.id,value]));const isDescendant=value=>{let parentId=value.parent_id;const seen=new Set();while(parentId&&byId.has(parentId)&&!seen.has(parentId)){if(parentId===item.id)return true;seen.add(parentId);parentId=byId.get(parentId).parent_id;}return false;};
    const targets=state.items.filter(value=>value.id!==item.id&&!value.category_id&&!isDescendant(value));
    if(!targets.length){
      const empty=make('div','','collection-no-merge-target');empty.append(make('p',labels('workbench.no-merge-target','There is no other Collection available as a destination. Create one first.')));const create=button(labels('collections.create','Create Collection'),'button button-secondary');create.addEventListener('click',()=>{dialog.close();openCollectionDialog();});empty.append(create);mergeSection.append(empty);
    }else{
      mergeSection.append(make('p',labels('workbench.merge-help','Choose where to move keywords and nested Collections. You will review the change before merging.')));
      const mergeControls=make('div','','collection-merge-controls');const mergeLabel=make('div','','collection-manage-label');mergeLabel.append(make('span',labels('workbench.merge-into','Target Collection')));const target=collectionPicker(targets.map((other)=>({value:other.id,label:nameOf(other)})),labels('workbench.merge-into','Target Collection'));mergeLabel.append(target);mergeControls.append(mergeLabel);
      const merge=button(labels('workbench.merge-action','Merge'),'button button-primary');merge.addEventListener('click',()=>{if(!target.value){status(labels('workbench.choose-target-error','Choose a target Collection first.'),true);target.focus();return;}openMergeConfirmation(item,target.value);});mergeControls.append(merge);mergeSection.append(mergeControls);
    }
  }
  panel.append(mergeSection);

  const deleteSection=make('section','','collection-manage-section collection-manage-delete');deleteSection.append(make('h3',labels('workbench.manage-delete','Delete this Collection')),make('p',labels('workbench.delete-description','This removes the Collection and its subcollections. Keywords and their evidence are kept.')));const impact=displayCount(item.subtree_unique_keyword_count??item.membership_count);const remove=button(labels('collections.delete','Delete'),'collection-danger-button');remove.addEventListener('click',()=>openDeleteConfirmation(item,impact));deleteSection.append(remove);panel.append(deleteSection);
  dialog.append(panel);dialog.showModal();
}
function openDeleteConfirmation(item,impact){const dialog=$('#workbench-collection-dialog');dialog.replaceChildren();const title=make('h2',labels('workbench.delete-title','Delete Collection?'));const copy=make('p',formatted('workbench.delete-impact',{count:impact},'This removes the Collection and its nested Collections. {count} unique keyword memberships will be removed; source observations and keyword decisions are retained.'));const actions=make('div','','collection-actions');const cancel=button(labels('collections.cancel','Cancel'));cancel.addEventListener('click',()=>dialog.close());const confirm=button(labels('workbench.delete-confirm','Delete Collection'),'button button-primary');confirm.addEventListener('click',async()=>{confirm.disabled=true;try{await requestJson(`/api/v1/collections/${encodeURIComponent(item.id)}`,{method:'DELETE',locale:getLocale()});dialog.close();if(state.collectionId===item.id)state.collectionId='';await refresh();status(labels('collections.deleted','Collection deleted'));}catch(error){confirm.disabled=false;status(error.message||labels('collections.error','Could not delete Collection'),true);}});actions.append(cancel,confirm);dialog.append(title,copy,actions);dialog.showModal();}

function openMergeConfirmation(item,targetId){const dialog=$('#workbench-collection-dialog');const target=state.items.find(value=>value.id===targetId);dialog.replaceChildren();const title=make('h2',labels('workbench.merge-title','Merge Collections?'));const copy=make('p',formatted('workbench.merge-impact',{source:nameOf(item),target:nameOf(target||{name:''})},'Move memberships from “{source}” and its descendants to “{target}”, then remove the source Collections. Keyword evidence and user decisions are retained.'));const actions=make('div','','collection-actions');const cancel=button(labels('collections.cancel','Cancel'));cancel.addEventListener('click',()=>dialog.close());const confirm=button(labels('workbench.merge-confirm','Confirm merge'),'button button-primary');confirm.addEventListener('click',async()=>{confirm.disabled=true;try{const result=await requestJson(`/api/v1/collections/${encodeURIComponent(item.id)}/merge`,{method:'POST',body:{target_id:targetId},locale:getLocale()});state.collectionId=result.target_id;dialog.close();await refresh();status(labels('collections.merged','Collections merged'));}catch(error){confirm.disabled=false;status(error.message||labels('collections.error','Could not merge Collections'),true);}});actions.append(cancel,confirm);dialog.append(title,copy,actions);dialog.showModal();}
function renderManageActions(){const root=$('#workbench-tree');if(!root)return;}
export function initializeKeywordWorkbench(){const params=new URLSearchParams(window.location.search);state.query=params.get('q')||'';state.filterState=params.get('state')||'all';state.classification=params.get('classification')||'all';state.source=params.get('source')||'';state.evidence=params.get('evidence')||'all';state.sort=params.get('sort')||'recent';state.dateFrom=params.get('from')||'';state.dateTo=params.get('to')||'';state.collectionId=params.get('collection_id')||params.get('collection_ids')||'';state.offset=Number(params.get('offset')||0)||0;build();$('#collection-create').addEventListener('click',()=>openCollectionDialog());document.addEventListener('keydown',event=>{if(event.key==='Escape'&&state.mobileOpen)setMobileOpen(false);});window.addEventListener('resize',()=>{if(window.innerWidth>900)setMobileOpen(false);});document.addEventListener('trendscout:locale',()=>{if($('#workbench-root')){build();refresh();}});window.addEventListener('hashchange',()=>{const params=new URLSearchParams(window.location.search);state.query=params.get('q')||'';state.filterState=params.get('state')||'all';state.classification=params.get('classification')||'all';state.source=params.get('source')||'';state.evidence=params.get('evidence')||'all';state.sort=params.get('sort')||'recent';state.dateFrom=params.get('from')||'';state.dateTo=params.get('to')||'';state.collectionId=params.get('collection_id')||params.get('collection_ids')||'';state.offset=Number(params.get('offset')||0)||0;});return {refresh,items:()=>state.items};}
