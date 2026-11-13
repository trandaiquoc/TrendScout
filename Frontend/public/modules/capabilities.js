import { getJson } from './api.js';

const STATUS_FIELDS = [
  ['sources.adapter', 'adapter_status'],
  ['sources.configuration', 'configuration_status'],
  ['sources.approval', 'approval_status'],
  ['sources.local-verification', 'local_live_verification_status'],
  ['sources.current-operation', 'enabled_for_current_operation'],
  ['sources.current-health', null],
  ['sources.production-readiness', 'production_readiness'],
];

export function loadCapabilities(locale = 'vi') {
  return getJson('/api/v1/capabilities', { locale });
}

export function isLocalLiveDemo(capabilities) {
  return capabilities?.data_mode === 'LIVE' && capabilities.local_live_demo === true
    && capabilities.environment_mode === 'local' && capabilities.read_only === false;
}

export function canStartFetch(capabilities) {
  return capabilities?.public_fetch_enabled === true && capabilities.fetch_enabled === true
    && capabilities.authentication_required === false
    && (capabilities.data_mode === 'SYNTHETIC' || isLocalLiveDemo(capabilities))
    && Array.isArray(capabilities.sources)
    && capabilities.sources.some((source) => source.enabled_for_current_operation === true);
}

export function acceptsDiscovery(capabilities, discovery) {
  return (capabilities?.data_mode === 'SYNTHETIC' && discovery?.data_mode === 'SYNTHETIC')
    || (isLocalLiveDemo(capabilities) && ['LIVE', 'MIXED'].includes(discovery?.data_mode));
}

function localizedStatus(value, translate) {
  if (value === true) return translate('common.enabled');
  if (value === false) return translate('common.disabled');
  if (typeof value !== 'string' || !value) return translate('source.status-unknown');
  return translate('source.status.' + value.toLowerCase());
}

export function renderSourceCapabilities(capabilities, translate) {
  const descriptors = new Map((Array.isArray(capabilities?.sources) ? capabilities.sources : []).map(source => [source.source_id, source]));
  const grid = document.querySelector('#source-registry-list');
  if (grid) {
    grid.replaceChildren();
    for (const descriptor of descriptors.values()) {
      const card=document.createElement('article'); card.className='panel source-card';
      const head=document.createElement('div'); head.className='source-card-head';
      const icon=document.createElement('span'); icon.className='source-monogram'; icon.textContent=descriptor.icon || '•';
      const badge=document.createElement('span'); badge.className='pill pill--gray';
      head.append(icon,badge); card.append(head);
      const title=document.createElement('h2'); title.textContent=descriptor.display_name || descriptor.source_id; card.append(title);
      const summary=document.createElement('p'); summary.textContent=descriptor.metric?.meaning || ''; card.append(summary);
      const list=document.createElement('dl'); list.className='detail-list';
      for (const [labelKey,field] of STATUS_FIELDS) {
        const label=document.createElement('dt'); label.textContent=translate(labelKey);
        const value=document.createElement('dd'); value.textContent=field===null ? translate('source.health-unreported') : localizedStatus(descriptor[field],translate);
        const row=document.createElement('div'); row.append(label,value); list.append(row);
      }
      const categoryLabel=translate(`sources.category.${descriptor.category}`);
      for (const [label,value] of [[translate('sources.category'),categoryLabel.startsWith('sources.category.')?descriptor.category:categoryLabel],[translate('sources.metric'),`${descriptor.metric?.name || '—'} (${descriptor.metric?.unit || '—'})`],[translate('sources.scope'),descriptor.market_scope || '—'],[translate('sources.quota'),translate('sources.quota-unknown')]]) {
        const dt=document.createElement('dt'); dt.textContent=label; const dd=document.createElement('dd'); dd.textContent=value; const row=document.createElement('div'); row.append(dt,dd); list.append(row);
      }
      const link=document.createElement('a'); link.textContent=translate('sources.official-docs'); link.href=descriptor.documentation_url || '#'; link.target='_blank'; link.rel='noopener noreferrer'; card.append(list,link);
      const enabled=descriptor.enabled_for_current_operation===true; card.classList.toggle('source-card--active',enabled);
      badge.textContent=translate(enabled?'source.enabled':'source.disabled'); badge.classList.toggle('pill--green',enabled);
      grid.append(card);
    }
  }

  const wikimedia = descriptors.get('wikimedia_pageviews_top_per_country');
  const activeCount = document.querySelector('#metric-active-sources');
  const activeNote = document.querySelector('#metric-active-sources-note');
  if (activeCount) {
    const count = Array.isArray(capabilities?.sources)
      ? capabilities.sources.filter((source) => source.enabled_for_current_operation === true).length
      : null;
    activeCount.textContent = count == null ? '—'
      : new Intl.NumberFormat(document.documentElement.lang || 'vi').format(count);
    if (activeNote) activeNote.textContent = translate('metric.active-sources-help');
  }
  const enabled = wikimedia?.enabled_for_current_operation === true;
  const sidebarStatus = document.querySelector('#sidebar-source-status');
  const sidebarDot = document.querySelector('#sidebar-source-dot');
  const mainStatus = document.querySelector('#main-source-status');
  if (wikimedia?.local_live_verification_status === 'LOCAL_LIVE_VERIFIED') {
    for (const badge of [sidebarStatus, mainStatus]) {
      if (badge) badge.textContent = translate(enabled ? 'source.enabled-local-verified' : 'source.historical-local-verified');
    }
  } else {
    for (const badge of [sidebarStatus, mainStatus]) {
      if (badge) badge.textContent = translate(enabled ? 'source.enabled' : 'source.disabled');
    }
  }
  if (sidebarDot) sidebarDot.className = `status-dot ${enabled ? 'status-dot--green' : 'status-dot--muted'}`;
  if (mainStatus) {
    mainStatus.classList.toggle('pill--green', enabled);
    mainStatus.classList.toggle('pill--gray', !enabled);
  }
}
