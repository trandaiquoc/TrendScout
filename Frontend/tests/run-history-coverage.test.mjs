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
  });
});
