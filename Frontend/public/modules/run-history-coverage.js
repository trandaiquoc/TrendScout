function count(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function firstCount(...values) {
  for (const value of values) {
    const parsed = count(value);
    if (parsed !== null) return parsed;
  }
  return null;
}

export function extractRunSourceCoverage(item) {
  const manifests = Array.isArray(item?.source_manifest) ? item.source_manifest : [];
  const sources = manifests.map(source => {
    const coverage = source?.coverage && typeof source.coverage === 'object'
      ? source.coverage : {};
    const sourceId = typeof source?.source_id === 'string' ? source.source_id : '';
    const acceptedQueries = sourceId === 'youtube_video_search' && Array.isArray(coverage.queries)
      ? coverage.queries.map(query => count(query?.accepted)).filter(value => value !== null)
      : [];
    return {
      sourceId,
      status: typeof source?.status === 'string' ? source.status : 'UNKNOWN',
      providerReturnedRecords: firstCount(
        coverage.provider_returned_records,
        coverage.item_count,
        ...(acceptedQueries.length ? [acceptedQueries.reduce((sum, value) => sum + value, 0)] : []),
      ),
      retainedRecords: firstCount(
        coverage.accepted_records,
        source?.record_count,
      ),
      rejectedRecords: firstCount(
        coverage.rejected_records,
        coverage.rejected,
      ),
      omittedRecords: firstCount(
        coverage.explicitly_omitted_records,
        coverage.records_omitted_storage_limit,
      ),
      googleDiscoveryMode: sourceId === 'google_web_serp'
        && ['SERPAPI_TRENDING_NOW', 'RSS_LIMITED_DISCOVERY'].includes(coverage.discovery_mode)
        ? coverage.discovery_mode : null,
      googleCandidateCount: sourceId === 'google_web_serp'
        ? count(coverage.trending_candidate_count) : null,
      googleDistinctKeywordCount: sourceId === 'google_web_serp'
        ? count(coverage.distinct_keyword_count) : null,
      googleVerifiedCount: sourceId === 'google_web_serp'
        ? count(coverage.serp_verified_candidate_count) : null,
      googleProviderDataAt: sourceId === 'google_web_serp'
        && typeof coverage.provider_data_at === 'string' ? coverage.provider_data_at : null,
      googleFetchedAt: sourceId === 'google_web_serp'
        && typeof coverage.fetched_at === 'string' ? coverage.fetched_at : null,
      deliveryState: ['SUCCESS', 'PARTIAL'].includes(source?.status)
        && ['FRESH', 'CACHED'].includes(source?.delivery_state)
        ? source.delivery_state : null,
      sourceFetchedAt: [coverage.source_fetched_at, coverage.fetched_at,
        coverage.provider_data_at, coverage.cache_fetched_at]
        .find(value => typeof value === 'string' && !Number.isNaN(Date.parse(value))) || null,
      cacheDeliveryAt: typeof coverage.cache_delivery_at === 'string'
        && !Number.isNaN(Date.parse(coverage.cache_delivery_at))
        ? coverage.cache_delivery_at : null,
      providerMethod: typeof coverage.provider_method === 'string' ? coverage.provider_method : null,
      query: sourceId === 'youtube_video_search' && Array.isArray(coverage.queries)
        ? coverage.queries.map(query => typeof query?.original_query === 'string'
          ? query.original_query : null).filter(Boolean).join(' · ') || null : null,
      queryScopes: Array.isArray(coverage.query_scopes)
        ? coverage.query_scopes.filter(value => typeof value === 'string') : [],
      marketFilter: typeof coverage.market_filter === 'string' ? coverage.market_filter : null,
      languageFilter: typeof coverage.language_filter === 'string' ? coverage.language_filter : null,
    };
  });

  const providerCounts = sources.map(source => source.providerReturnedRecords);
  const providerRecordCount = providerCounts.length > 0
    && providerCounts.every(value => value !== null)
    ? providerCounts.reduce((sum, value) => sum + value, 0)
    : firstCount(item?.provider_record_count);

  return { providerRecordCount, sources };
}
