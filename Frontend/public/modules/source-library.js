import { getJson } from './api.js';
import { getLocale, t } from './i18n.js';
import { formatNumber } from './format.js';

const $ = (selector) => document.querySelector(selector);
const number = (value) => formatNumber(value, getLocale());
const element = (tag, text = null, className = '') => {
  const node = document.createElement(tag);
  if (text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
};
const getErrorMessage = (error, fallback) => error && typeof error.message === 'string' ? error.message : t(fallback);
let catalog = [];
let loadSequence = 0;
let runtimeSources = null;
// The research inventory and runtime registry use different, stable IDs for these four sources.
const runtimeIdByResearchId = {
  wikimedia_pageviews: 'wikimedia_pageviews_top_per_country',
  youtube: 'youtube_video_search',
  google_serp: 'google_web_serp',
  bing_serp: 'bing_web_serp',
};

async function loadSourceLibrary() {
  const body=$('#source-library-rows'); if (!body) return;
  const sequence=++loadSequence;
  try {
    const data=await getJson('/api/v1/sources/library',{locale:getLocale()});
    if(sequence!==loadSequence)return;
    $('#source-library-retry').hidden=true;
    $('#source-library-search').disabled=false;$('#source-library-category').disabled=false;
    catalog=data.items || [];
    const category=$('#source-library-category');
    const previous=category.value;
    category.replaceChildren(new Option(t('phoenix.all-categories'), ''));
    for(const group of [...new Set(catalog.map(item=>item.category?.match(/^([A-E])\./)?.[1]).filter(Boolean))].sort()) category.add(new Option(t(`sources.group.${group}`),group));
    category.value=previous;
    $('#source-library-audit').textContent=t('sources.library-audit',{date:data.audit_date || '—',count:number(catalog.length)});
    renderSourceLibrary();
  } catch (error) { if(sequence!==loadSequence)return;catalog=[];$('#source-library-count').textContent='';$('#source-library-audit').textContent=t('sources.library-error');$('#source-library-retry').hidden=false;$('#source-library-search').disabled=true;$('#source-library-category').disabled=true;body.replaceChildren(); const row=element('tr'); row.append(element('td',getErrorMessage(error,'sources.library-error'))); body.append(row); }
}

function renderSourceLibrary() {
  const body=$('#source-library-rows'); if (!body) return;
  const expanded=new Set([...body.querySelectorAll('tr[data-source-id]')]
    .filter(row=>row.querySelector('details')?.open).map(row=>row.dataset.sourceId));
  const query=($('#source-library-search')?.value || '').trim().toLocaleLowerCase();
  const category=$('#source-library-category')?.value || '';
  const items=catalog.filter(item=>`${item.source_id} ${item.display_name} ${item.category}`.toLocaleLowerCase().includes(query)&&(!category||item.category?.startsWith(`${category}.`)));
  $('#source-library-count').textContent=t('workbench.results-count',{count:number(items.length)});
  body.replaceChildren();
  for (const item of items) {
    const row=element('tr');
    row.dataset.sourceId=item.source_id || '';
    const candidate=element('td');candidate.append(element('strong',item.display_name || t('sources.candidate-unnamed')));row.append(candidate);
    const group=item.category?.match(/^([A-E])\./)?.[1];
    row.append(element('td',group?t(`sources.group.${group}`):item.category));
    const runtime=runtimeSources?.get(runtimeIdByResearchId[item.source_id] || item.source_id);
    const runtimeStatus=runtime ? `${t(runtime.enabled_for_current_operation === true ? 'source.enabled' : 'source.disabled')} · ${runtime.display_name || runtime.source_id}` : t(runtimeSources ? 'sources.candidate-locked' : 'phoenix.source-runtime-unknown');
    row.append(element('td',runtimeStatus));
    const adapterKey=runtime?.adapter_status ? `source.status.${String(runtime.adapter_status).toLowerCase()}` : '';
    const adapterText=adapterKey?t(adapterKey):'';
    const availability=element('td');availability.append(element('span',runtime ? (adapterText && adapterText!==adapterKey ? adapterText : t('source.status-unknown')) : t(runtimeSources ? 'phoenix.source-not-in-runtime' : 'phoenix.source-runtime-unknown')));
    const reference=typeof item.source_reference==='string'?item.source_reference.trim().replace(/^[A-Z];\s*/,''):'';
    const noteKey=typeof item.revalidation_note_key==='string'?item.revalidation_note_key:'';
    const translatedNote=noteKey?t(noteKey):'';
    const reviewNote=translatedNote&&translatedNote!==noteKey?translatedNote:(typeof item.revalidation_note==='string'?item.revalidation_note.trim():'');
    const references=Array.isArray(item.revalidation_links)?item.revalidation_links:[];
    const referenceUrls=new Set();
    if(reference||reviewNote||references.length){
      const details=element('details','','source-candidate-details');
      details.open=expanded.has(item.source_id);
      const summary=element('summary',t('sources.candidate-details'));
      details.append(summary);
      if(item.revalidated_on)details.append(element('p',t('sources.revalidated-on',{date:item.revalidated_on}),'fine-print'));
      if(reviewNote)details.append(element('p',reviewNote));
      if(reference){
        const note=element('p','','source-reference-note');
        const matcher=/\[([^\]]+)\]\((https:\/\/[^)\s]+)\)/g;
        let offset=0,match;
        while((match=matcher.exec(reference))){
          note.append(document.createTextNode(reference.slice(offset,match.index).replace(/\bD\d[A-Z]\s+synthetic\.?/gi,t('sources.synthetic-fixture'))));
          try{
            const url=new URL(match[2]);
            if(url.protocol==='https:'&&!url.username&&!url.password){referenceUrls.add(url.href);const link=element('a',match[1]);link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';note.append(link);}
            else note.append(document.createTextNode(match[0]));
          }catch{note.append(document.createTextNode(match[0]));}
          offset=matcher.lastIndex;
        }
        note.append(document.createTextNode(reference.slice(offset).replace(/\bD\d[A-Z]\s+synthetic\.?/gi,t('sources.synthetic-fixture'))));
        details.append(note);
      }
      if(references.length){
        const links=element('ul','','source-reference-links');
        for(const itemLink of references){
          if(typeof itemLink?.label!=='string'||typeof itemLink?.url!=='string')continue;
          try{const url=new URL(itemLink.url);if(url.protocol!=='https:'||url.username||url.password||referenceUrls?.has(url.href))continue;const link=element('a',itemLink.label);link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';const entry=element('li');entry.append(link);links.append(entry);}catch{}
        }
        if(links.childElementCount)details.append(links);
      }
      availability.append(details);
    }
    row.append(availability);
    body.append(row);
  }
  if (!items.length) { const row=element('tr'); row.append(element('td',t('sources.no-candidates'))); body.append(row); }
}

export function initializeSourceLibrary() {
  $('#source-library-search').addEventListener('input', () => {
    if ($('#source-library-search').value.trim()) $('#source-library-disclosure').open = true;
    renderSourceLibrary();
  });
  $('#source-library-category').addEventListener('change', renderSourceLibrary);
  $('#source-library-retry').addEventListener('click', loadSourceLibrary);
  return { load: loadSourceLibrary, setCapabilities(capabilities) {
    runtimeSources=Array.isArray(capabilities?.sources)?new Map(capabilities.sources.map(source=>[source.source_id,source])):null;
    if(catalog.length)renderSourceLibrary();
  } };
}
