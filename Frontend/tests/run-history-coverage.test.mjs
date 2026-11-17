import assert from 'node:assert/strict';
import test from 'node:test';

import { extractRunSourceCoverage } from '../public/modules/run-history-coverage.js';

test('uses provider coverage and preserves explicit zero counts', () => {
  const result = extractRunSourceCoverage({
    provider_record_count: 1999,
    source_manifest: [{
      source_id: 'wikimedia_pageviews_top_per_country',
      status: 'SUCCESS',
      record_count: 1999,
      coverage: {
        provider_returned_records: 1999,
        accepted_records: 1999,
        rejected_records: 0,
        explicitly_omitted_records: 0,
      },
    }],
  });

  assert.equal(result.providerRecordCount, 1999);
  assert.deepEqual(result.sources[0], {
    sourceId: 'wikimedia_pageviews_top_per_country',
    status: 'SUCCESS',
    providerReturnedRecords: 1999,
    retainedRecords: 1999,
    rejectedRecords: 0,
    omittedRecords: 0,
    googleDiscoveryMode: null,
    googleCandidateCount: null,
    googleDistinctKeywordCount: null,
    googleVerifiedCount: null,
    googleProviderDataAt: null,
    googleFetchedAt: null,
    deliveryState: null,
    sourceFetchedAt: null,
    cacheDeliveryAt: null,
    providerMethod: null,
    query: null,
    queryScopes: [],
    marketFilter: null,
    languageFilter: null,
  });
});

test('shows legacy upstream count and storage omissions instead of retained count as provider total', () => {
  const result = extractRunSourceCoverage({
    provider_record_count: 212,
    source_manifest: [{
      source_id: 'wikimedia_pageviews_top_per_country',
      status: 'PARTIAL',
      record_count: 212,
      coverage: {
        item_count: 1000,
        rejected: 0,
        records_omitted_storage_limit: 788,
      },
    }],
  });

  assert.equal(result.providerRecordCount, 1000);
  assert.equal(result.sources[0].retainedRecords, 212);
  assert.equal(result.sources[0].rejectedRecords, 0);
  assert.equal(result.sources[0].omittedRecords, 788);
});

test('keeps unavailable source coverage unknown and uses stored aggregate only as fallback', () => {
  const result = extractRunSourceCoverage({
    provider_record_count: 42,
    source_manifest: [{ source_id: 'legacy-source', status: 'PARTIAL', record_count: 40 }],
  });

  assert.equal(result.providerRecordCount, 42);
  assert.deepEqual(result.sources[0], {
    sourceId: 'legacy-source',
    status: 'PARTIAL',
    providerReturnedRecords: null,
    retainedRecords: 40,
    rejectedRecords: null,
    omittedRecords: null,
    googleDiscoveryMode: null,
    googleCandidateCount: null,
    googleDistinctKeywordCount: null,
    googleVerifiedCount: null,
    googleProviderDataAt: null,
    googleFetchedAt: null,
    deliveryState: null,
    sourceFetchedAt: null,
    cacheDeliveryAt: null,
    providerMethod: null,
    query: null,
    queryScopes: [],
    marketFilter: null,
    languageFilter: null,
  });
});

test('extracts Google discovery provenance without treating RSS records as verified', () => {
  const result = extractRunSourceCoverage({ source_manifest: [{
    source_id: 'google_web_serp',
    status: 'SUCCESS',
    record_count: 8,
    delivery_state: 'CACHED',
    coverage: {
      discovery_mode: 'RSS_LIMITED_DISCOVERY',
      trending_candidate_count: 8,
      distinct_keyword_count: 7,
      serp_verified_candidate_count: 0,
      provider_data_at: '2026-11-15T02:00:00Z',
      fetched_at: '2026-11-15T02:03:00Z',
    },
  }] });

  assert.deepEqual(result.sources[0], {
    sourceId: 'google_web_serp',
    status: 'SUCCESS',
    providerReturnedRecords: null,
    retainedRecords: 8,
    rejectedRecords: null,
    omittedRecords: null,
    googleDiscoveryMode: 'RSS_LIMITED_DISCOVERY',
    googleCandidateCount: 8,
    googleDistinctKeywordCount: 7,
    googleVerifiedCount: 0,
    googleProviderDataAt: '2026-11-15T02:00:00Z',
    googleFetchedAt: '2026-11-15T02:03:00Z',
    deliveryState: 'CACHED',
    sourceFetchedAt: '2026-11-15T02:03:00Z',
    cacheDeliveryAt: null,
    providerMethod: null,
    query: null,
    queryScopes: [],
    marketFilter: null,
    languageFilter: null,
  });
});

test('keeps YouTube provider count, storage omission, observation and cache delivery distinct', () => {
  const result = extractRunSourceCoverage({
    provider_record_count: 47,
    source_manifest: [{
      source_id: 'youtube_video_search',
      status: 'PARTIAL',
      data_mode: 'LIVE',
      delivery_state: 'CACHED',
      record_count: 47,
      coverage: {
        records_omitted_storage_limit: 3,
        source_fetched_at: '2026-11-17T02:12:41.397682Z',
        cache_delivery_at: '2026-11-17T02:16:52.963245Z',
        provider_method: 'search.list',
        query_scopes: ['beauty_personal_care'],
        market_filter: 'US',
        language_filter: 'en-US',
        queries: [{ accepted: 50, original_query: 'beauty skincare cosmetics haircare trends' }],
      },
    }],
  });

  assert.equal(result.providerRecordCount, 50);
  assert.deepEqual(result.sources[0], {
    sourceId: 'youtube_video_search',
    status: 'PARTIAL',
    providerReturnedRecords: 50,
    retainedRecords: 47,
    rejectedRecords: null,
    omittedRecords: 3,
    googleDiscoveryMode: null,
    googleCandidateCount: null,
    googleDistinctKeywordCount: null,
    googleVerifiedCount: null,
    googleProviderDataAt: null,
    googleFetchedAt: null,
    deliveryState: 'CACHED',
    sourceFetchedAt: '2026-11-17T02:12:41.397682Z',
    cacheDeliveryAt: '2026-11-17T02:16:52.963245Z',
    providerMethod: 'search.list',
    query: 'beauty skincare cosmetics haircare trends',
    queryScopes: ['beauty_personal_care'],
    marketFilter: 'US',
    languageFilter: 'en-US',
  });
});

test('does not label a failed source as fresh or cached delivery', () => {
  const result = extractRunSourceCoverage({source_manifest: [{
    source_id: 'google_web_serp',
    status: 'FAILED',
    record_count: 0,
    delivery_state: 'FRESH',
    coverage: {discovery_mode: 'SERPAPI_TRENDING_NOW'},
  }]});

  assert.equal(result.sources[0].deliveryState, null);
});

test('preserves unavailable YouTube diagnostics and stale cache delivery', () => {
  const result = extractRunSourceCoverage({ source_manifest: [{
    source_id: 'youtube_video_search',
    status: 'UNAVAILABLE',
    data_mode: 'LIVE',
    delivery_state: 'NOT_ELIGIBLE',
    error_code: 'NO_CACHE_AVAILABLE',
    provider_request_count: 0,
    coverage: {
      provider_request_count: 0,
      next_eligible_at: '2026-11-17T08:00:00Z',
      cache_status: 'UNAVAILABLE',
      cache_reason: 'CACHE_MISS',
    },
  }, {
    source_id: 'google_web_serp',
    status: 'PARTIAL',
    delivery_state: 'STALE_CACHE',
    record_count: 4,
    coverage: { provider_request_count: 0, cache_freshness: 'STALE_CACHE' },
  }] });

  assert.equal(result.sources[0].deliveryState, 'NOT_ELIGIBLE');
  assert.equal(result.sources[0].errorCode, 'NO_CACHE_AVAILABLE');
  assert.equal(result.sources[0].providerRequestCount, 0);
  assert.equal(result.sources[0].nextEligibleAt, '2026-11-17T08:00:00Z');
  assert.equal(result.sources[1].deliveryState, 'STALE_CACHE');
  assert.equal(result.sources[1].cacheFreshness, 'STALE_CACHE');
});
