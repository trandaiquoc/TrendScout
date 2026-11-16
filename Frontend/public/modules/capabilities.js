import { getJson } from './api.js';

// Translate only exact known registry text; changed provider metadata remains visible as supplied.
const SOURCE_TEXT = {
  "wikimedia_pageviews_top_per_country": {
    "meaning": "Approximate upper-bound pageviews for listed pages",
    "scope": "US country-scoped Wikimedia top list"
  },
  "youtube_video_search": {
    "meaning": "Position in query-scoped YouTube search results",
    "scope": "US regionCode filter; not author or viewer residence"
  },
  "google_web_serp": {
    "meaning": "Position in the returned Google SERP sample",
    "scope": "Google gl=us localization filter; not author or viewer residence"
  },
  "bing_web_serp": {
    "meaning": "Position in the returned Bing SERP sample",
    "scope": "Bing mkt=en-US localization filter; not author or viewer residence"
  }
};

const STATUS_FIELDS = [
  ['sources.adapter', 'adapter_status'],
  ['sources.configuration', 'configuration_status'],
  ['sources.approval', 'approval_status'],
  ['sources.local-verification', 'local_live_verification_status'],
  ['sources.current-operation', 'enabled_for_current_operation'],
  ['sources.latest-fetch', null],
  ['sources.production-readiness', 'production_readiness'],
];
const FETCH_DEFAULTS_KEY = 'trendscout.fetch-default-sources.v1';

function enabledSourceIds(capabilities) {
  return (Array.isArray(capabilities?.sources) ? capabilities.sources : [])
    .filter((source) => source?.enabled_for_current_operation === true)
    .map((source) => source.source_id)
    .filter((sourceId) => typeof sourceId === 'string');
}

function readFetchDefaults() {
  try {
    const parsed = JSON.parse(localStorage.getItem(FETCH_DEFAULTS_KEY) || 'null');
    return Array.isArray(parsed) && parsed.every((item) => typeof item === 'string')
      ? [...new Set(parsed)] : null;
  } catch {
    return null;
  }
}

export function defaultFetchSourceIds(capabilities) {
  const enabled = enabledSourceIds(capabilities);
  const saved = readFetchDefaults();
  if (!saved) return null;
  const selected = saved.filter((sourceId) => enabled.includes(sourceId));
  return selected.length ? selected : null;
}

export function renderFetchDefaults(capabilities, translate) {
  const fieldset = document.querySelector('#fetch-default-sources');
  const status = document.querySelector('#fetch-default-status');
  if (!fieldset) return;
  const saved = readFetchDefaults();
  const enabled = enabledSourceIds(capabilities);
  const selected = new Set(saved ? saved.filter((sourceId) => enabled.includes(sourceId)) : enabled);
  const labels = {
    wikimedia_pageviews_top_per_country: 'fetch.source.wikimedia',
    google_web_serp: 'fetch.source.google',
    bing_web_serp: 'fetch.source.bing',
    youtube_video_search: 'fetch.source.youtube',
  };
  fieldset.replaceChildren();
  for (const source of Array.isArray(capabilities?.sources) ? capabilities.sources : []) {
    const sourceId = source?.source_id;
    if (typeof sourceId !== 'string') continue;
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = sourceId;
    input.checked = selected.has(sourceId);
    input.disabled = source.enabled_for_current_operation !== true;
    const label = document.createElement('label');
    const nameKey = labels[sourceId];
    label.append(input, document.createTextNode(' ' + (nameKey ? translate(nameKey) : source.display_name || sourceId)));
    fieldset.append(label);
  }
  fieldset.onchange = () => {
    const values = [...fieldset.querySelectorAll('input[type="checkbox"]:checked')]
      .map((input) => input.value);
    if (!values.length) {
      fieldset.querySelector('input[type="checkbox"]:enabled')?.click();
      if (status) status.textContent = translate('sources.fetch-defaults-required');
      return;
    }
    try {
      localStorage.setItem(FETCH_DEFAULTS_KEY, JSON.stringify(values));
      if (status) status.textContent = translate('sources.fetch-defaults-saved');
    } catch {
      if (status) status.textContent = translate('sources.fetch-defaults-storage-error');
    }
  };
  if (status) status.textContent = saved
    ? translate('sources.fetch-defaults-saved') : translate('sources.fetch-defaults-all');
}

function renderSerpApiProviderBudget(capabilities, translate) {
  const section = document.querySelector('#serpapi-provider-budget');
  const values = document.querySelector('#serpapi-provider-budget-values');
  if (!section || !values) return;
  const sourceIds = new Set((Array.isArray(capabilities?.sources) ? capabilities.sources : [])
    .map((source) => source?.source_id));
  section.hidden = !sourceIds.has('google_web_serp') && !sourceIds.has('bing_web_serp');
  values.replaceChildren();
  if (section.hidden) return;
  const quota = capabilities?.google_quota || {};
  const provider = quota.provider_account || {};
  const workflow = capabilities?.google_workflow || {};
  const googleAllocation = quota.serpapi_google || quota.serpapi;
  const bingAllocation = quota.serpapi_bing;
  const format = (value) => Number.isInteger(value)
    ? new Intl.NumberFormat(document.documentElement.lang || 'vi').format(value)
    : translate('common.unavailable');
  const observed = provider.observed_at ? new Date(provider.observed_at) : null;
  const checkedAt = observed && !Number.isNaN(observed.getTime())
    ? new Intl.DateTimeFormat(document.documentElement.lang || 'vi', {dateStyle:'medium',timeStyle:'short'}).format(observed)
    : translate('common.unavailable');
  capabilityRow(values, translate('sources.provider-status'),
    localizedStatus(quota.provider_account_status || workflow.account_api, translate));
  capabilityRow(values, translate('sources.provider-usage'), translate('source.provider-quota-summary', {
    limit: format(provider.monthly_limit), used: format(provider.month_to_date_searches),
    remaining: format(provider.monthly_remaining ?? provider.total_remaining),
  }));
  capabilityRow(values, translate('sources.provider-reserve'),
    format(workflow.cross_machine_reserve));
  capabilityRow(values, translate('sources.provider-reset'),
    provider.plan_renewal_date || translate('common.unavailable'));
  capabilityRow(values, translate('sources.provider-checked'), checkedAt);
  capabilityRow(values, translate('sources.google-allocation'),
    googleAllocation?.monthly?.limit == null ? translate('common.unavailable')
      : translate('sources.local-allocation-summary', {
        limit: format(googleAllocation.monthly.limit),
        used: format(googleAllocation.monthly.used),
        remaining: format(googleAllocation.monthly.remaining),
      }));
  capabilityRow(values, translate('sources.bing-allocation'),
    bingAllocation?.monthly?.limit == null ? translate('common.unavailable')
      : translate('sources.local-allocation-summary', {
        limit: format(bingAllocation.monthly.limit),
        used: format(bingAllocation.monthly.used),
        remaining: format(bingAllocation.monthly.remaining),
      }));
}

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

function localizedSourceText(descriptor, field, value, translate) {
  if(!value)return '—';
  if(SOURCE_TEXT[descriptor.source_id]?.[field]!==value)return value;
  const key=`phoenix.source.${descriptor.source_id}.${field}`;
  const translated=translate(key);
  return translated===key?value:translated;
}

function localizedMetric(value, kind, translate) {
  if(!value)return '—';
  const key=`algorithms.${kind}.${String(value).toLowerCase().replaceAll(' ','_')}`;
  const translated=translate(key);
  return translated===key?value:translated;
}

function capabilityRow(list, label, value) {
  const dt=document.createElement('dt');dt.textContent=label;
  const dd=document.createElement('dd');dd.textContent=value;
  const row=document.createElement('div');row.append(dt,dd);list.append(row);
}

function quotaUse(value, translate) {
  if (!value || !Number.isInteger(value.used) || !Number.isInteger(value.limit)
      || !Number.isInteger(value.remaining)) return translate('common.unavailable');
  const format = (number) => new Intl.NumberFormat(document.documentElement.lang || 'vi').format(number);
  return translate('source.quota-use', {
    used: format(value.used), limit: format(value.limit), remaining: format(value.remaining),
  });
}

function savedFetchSummary(activity, translate) {
  if (!activity) return translate('source.fetch-no-saved-data');
  const status = ['SUCCESS', 'PARTIAL', 'FAILED'].includes(activity.status)
    ? translate(`source.fetch.${activity.status.toLowerCase()}`)
    : translate('source.status-unknown');
  const count = Number.isInteger(activity.record_count) && activity.record_count >= 0
    ? `${new Intl.NumberFormat(document.documentElement.lang || 'vi').format(activity.record_count)} ${translate('source.fetch-records')}`
    : translate('source.fetch-count-unavailable');
  const fetched = activity.fetched_at ? new Date(activity.fetched_at) : null;
  const date = fetched && !Number.isNaN(fetched.getTime())
    ? new Intl.DateTimeFormat(document.documentElement.lang || 'vi', {dateStyle:'medium',timeStyle:'short'}).format(fetched)
    : translate('source.fetch-time-unavailable');
  return `${status} · ${count} · ${date}`;
}

function scheduleTimestamp(value, timezone) {
  const timestamp = typeof value === 'string' ? new Date(value) : null;
  return timestamp && !Number.isNaN(timestamp.getTime())
    ? new Intl.DateTimeFormat(document.documentElement.lang || 'vi', {
      dateStyle: 'medium', timeStyle: 'short', timeZone: timezone,
    }).format(timestamp)
    : null;
}

export function renderSourceCapabilities(capabilities, translate) {
  renderSerpApiProviderBudget(capabilities, translate);
  const descriptors = new Map((Array.isArray(capabilities?.sources) ? capabilities.sources : []).map(source => [source.source_id, source]));
  const grid = document.querySelector('#source-registry-list');
  const state = document.querySelector('#source-registry-state');
  if (state) {
    state.hidden = Boolean(capabilities && Array.isArray(capabilities.sources) && descriptors.size);
    state.textContent = translate(!capabilities || !Array.isArray(capabilities.sources) ? 'phoenix.source-load-error' : 'phoenix.source-empty');
  }
  const retry = document.querySelector('#source-capabilities-retry');
  if (retry) retry.hidden = Boolean(capabilities && Array.isArray(capabilities.sources));
  if (grid) {
    grid.replaceChildren();
    for (const descriptor of descriptors.values()) {
      const card=document.createElement('article'); card.className='panel source-card';
      const head=document.createElement('div'); head.className='source-card-head';
      const icon=document.createElement('span'); icon.className='source-monogram'; icon.textContent=descriptor.icon || '•';
      const badge=document.createElement('span'); badge.className='pill pill--gray';
      const badges=document.createElement('div'); badges.className='source-card-badges';
      const adapterBadge=document.createElement('span'); adapterBadge.className='source-adapter-badge';
      const statusBadge = descriptor.local_live_verification_status === 'LOCAL_LIVE_VERIFIED'
        ? descriptor.local_live_verification_status : descriptor.adapter_status;
      adapterBadge.textContent=localizedStatus(statusBadge,translate);
      badges.append(adapterBadge,badge); head.append(icon,badges); card.append(head);
      const title=document.createElement('h2'); title.textContent=descriptor.display_name || descriptor.source_id; card.append(title);
      const summary=document.createElement('p'); summary.textContent=localizedSourceText(descriptor,'meaning',descriptor.metric?.meaning,translate); card.append(summary);
      const list=document.createElement('dl'); list.className='detail-list';
      for (const [labelKey,field] of STATUS_FIELDS) {
        const label=document.createElement('dt'); label.textContent=translate(labelKey);
        const value=document.createElement('dd'); value.textContent=field===null ? savedFetchSummary(descriptor.last_saved_fetch,translate) : localizedStatus(descriptor[field],translate);
        const row=document.createElement('div'); row.append(label,value); list.append(row);
      }
      if (descriptor.source_id === 'google_web_serp') {
        const workflow = capabilities?.google_workflow || {};
        const quota = capabilities?.google_quota || {};
        const googleSerpQuota = quota.serpapi_google || quota.serpapi;
        capabilityRow(list, translate('source.google-trends-discovery'), localizedStatus(workflow.trends_discovery, translate));
        capabilityRow(list, translate('source.google-rss-discovery'), localizedStatus(workflow.rss_discovery, translate));
        capabilityRow(list, translate('source.google-serp-verification'), localizedStatus(workflow.serp_verification, translate));
        capabilityRow(list, translate('source.google-account-api'), localizedStatus(workflow.account_api, translate));
        capabilityRow(list, translate('source.google-serpapi-budget'),
          `${translate('source.quota-daily')}: ${quotaUse(googleSerpQuota?.daily, translate)} · ${translate('source.quota-monthly')}: ${quotaUse(googleSerpQuota?.monthly, translate)}`);
        capabilityRow(list, translate('source.google-rss-budget'),
          `${translate('source.quota-daily')}: ${quotaUse(quota.google_rss?.daily, translate)} · ${translate('source.quota-monthly')}: ${quotaUse(quota.google_rss?.monthly, translate)}`);
        const provider = quota.provider_account || {};
        const formatProviderCount = (value) => Number.isInteger(value)
          ? new Intl.NumberFormat(document.documentElement.lang || 'vi').format(value)
          : translate('common.unavailable');
        capabilityRow(list, translate('source.google-provider-quota'), translate('source.provider-quota-summary', {
          limit: formatProviderCount(provider.monthly_limit),
          used: formatProviderCount(provider.month_to_date_searches),
          remaining: formatProviderCount(provider.monthly_remaining ?? provider.total_remaining),
        }));
        capabilityRow(list, translate('source.google-provider-reset'),
          provider.plan_renewal_date || translate('common.unavailable'));
        capabilityRow(list, translate('source.google-provider-remaining'),
          formatProviderCount(provider.total_remaining));
        const saved = descriptor.last_saved_fetch?.google_coverage;
        if (saved && typeof saved === 'object') {
          const modeKey = saved.discovery_mode === 'RSS_LIMITED_DISCOVERY'
            ? 'fetch.discovery.rss-limited' : 'fetch.discovery.trending-now';
          const count = Number.isInteger(saved.trending_candidate_count)
            ? new Intl.NumberFormat(document.documentElement.lang || 'vi').format(saved.trending_candidate_count)
            : translate('common.unavailable');
          const verified = Number.isInteger(saved.serp_verified_candidate_count)
            ? new Intl.NumberFormat(document.documentElement.lang || 'vi').format(saved.serp_verified_candidate_count)
            : translate('common.unavailable');
          capabilityRow(list, translate('source.google-latest-discovery'),
            `${translate(modeKey)} · ${translate('fetch.google-counts', { candidates: count, verified })}`);
          capabilityRow(list, translate('source.google-source-time'),
            saved.provider_data_at || saved.provider_created_at || translate('common.unavailable'));
          const delivered = ['SUCCESS', 'PARTIAL'].includes(descriptor.last_saved_fetch?.status)
            ? saved.delivery_state : null;
          capabilityRow(list, translate('source.google-delivery'),
            delivered ? translate(`fetch.delivery.${delivered}`) : translate('common.unavailable'));
        }
      }
      if (descriptor.source_id === 'bing_web_serp') {
        const workflow = capabilities?.google_workflow || {};
        const quota = capabilities?.google_quota || {};
        const allocation = quota.serpapi_bing;
        capabilityRow(list, translate('source.bing-serp-verification'),
          localizedStatus(workflow.bing_serp_verification, translate));
        capabilityRow(list, translate('source.bing-serpapi-budget'),
          `${translate('source.quota-daily')}: ${quotaUse(allocation?.daily, translate)} · ${translate('source.quota-monthly')}: ${quotaUse(allocation?.monthly, translate)}`);
        const saved = descriptor.last_saved_fetch;
        if (saved && ['SUCCESS', 'PARTIAL'].includes(saved.status)) {
          capabilityRow(list, translate('source.bing-latest-fetch'),
            savedFetchSummary(saved, translate));
          const coverage = saved.bing_coverage;
          if (coverage && typeof coverage === 'object') {
            if (typeof coverage.query === 'string') {
              capabilityRow(list, translate('source.bing-latest-query'), coverage.query);
            }
            const resultCount = Number.isInteger(coverage.accepted_result_count)
              ? new Intl.NumberFormat(document.documentElement.lang || 'vi')
                .format(coverage.accepted_result_count)
              : translate('common.unavailable');
            capabilityRow(list, translate('source.bing-latest-results'), resultCount);
            capabilityRow(list, translate('source.bing-latest-delivery'),
              coverage.delivery_state
                ? translate(`fetch.delivery.${coverage.delivery_state}`)
                : translate('common.unavailable'));
            capabilityRow(list, translate('source.bing-latest-provider-time'),
              coverage.provider_created_at || translate('common.unavailable'));
            capabilityRow(list, translate('source.bing-latest-cache'),
              coverage.provider_cache_state || translate('common.unavailable'));
          }
        }
      }
      if (['google_web_serp', 'bing_web_serp'].includes(descriptor.source_id)) {
        const schedule = capabilities?.refresh_schedule?.sources?.[descriptor.source_id];
        const policy = capabilities?.refresh_schedule || {};
        if (schedule && schedule.status !== 'UNAVAILABLE') {
          const slots = Array.isArray(schedule.slots) ? schedule.slots.join(' · ') : '';
          capabilityRow(list, translate('source.refresh-schedule'),
            slots ? translate('source.refresh-schedule-summary', {
              slots,
              timezone: schedule.timezone || policy.timezone || 'Asia/Ho_Chi_Minh',
              spacing: schedule.minimum_spacing_hours || policy.minimum_spacing_hours || 8,
              count: schedule.max_attempts_per_day || policy.max_attempts_per_day || 3,
            }) : translate('common.unavailable'));
          const next = schedule.initial_acceptance_available
            ? translate('source.refresh-initial-available')
            : scheduleTimestamp(schedule.next_eligible_at, schedule.timezone || policy.timezone);
          capabilityRow(list, translate('source.refresh-next'),
            next || translate('common.unavailable'));
          capabilityRow(list, translate('source.refresh-last'),
            scheduleTimestamp(schedule.last_attempt_at, schedule.timezone || policy.timezone)
              || translate('common.unavailable'));
        }
      }
      const categoryLabel=translate(`sources.category.${descriptor.category}`);
      for (const [label,value] of [[translate('sources.category'),categoryLabel.startsWith('sources.category.')?descriptor.category:categoryLabel],[translate('sources.metric'),`${localizedMetric(descriptor.metric?.name,'metric',translate)} (${localizedMetric(descriptor.metric?.unit,'unit',translate)})`],[translate('sources.scope'),localizedSourceText(descriptor,'scope',descriptor.market_scope,translate)],[translate('sources.quota'),translate('sources.quota-unknown')]]) {
        const dt=document.createElement('dt'); dt.textContent=label; const dd=document.createElement('dd'); dd.textContent=value; const row=document.createElement('div'); row.append(dt,dd); list.append(row);
      }
      const details=document.createElement('details'); details.className='source-capability-details';
      const detailSummary=document.createElement('summary'); detailSummary.textContent=translate('phoenix.source-details'); details.append(detailSummary,list);
      try {
        const url=new URL(descriptor.documentation_url);
        if(url.protocol==='https:'&&!url.username&&!url.password){const link=document.createElement('a');link.textContent=translate('sources.official-docs');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';details.append(link);}
      } catch { /* An absent or invalid provider link is not shown. */ }
      card.append(details);
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
