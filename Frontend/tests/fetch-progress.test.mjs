import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeFetchProgress } from '../public/modules/fetch-progress.js';

test('uses reported source counts and preserves partial and failed outcomes', () => {
  const progress = normalizeFetchProgress({
    selected_sources: ['google_web_serp', 'youtube_video_search'],
    sources: [
      { source_id: 'google_web_serp', status: 'PARTIAL' },
      { source_id: 'youtube_video_search', status: 'RUNNING' },
    ],
    progress: { completed_sources: 1, total_sources: 2, percent: 50 },
    started_at: '2026-11-26T01:00:00Z',
  });
  assert.deepEqual(progress.selectedSources, ['google_web_serp', 'youtube_video_search']);
  assert.equal(progress.completedSources, 1);
  assert.equal(progress.totalSources, 2);
  assert.equal(progress.outcomes[0].status, 'PARTIAL');
  assert.equal(progress.outcomes[1].status, 'RUNNING');
  assert.equal(progress.startedAt, '2026-11-26T01:00:00Z');
});

test('derives completed count only from terminal source outcomes when progress is absent', () => {
  const progress = normalizeFetchProgress({
    selected_sources: ['one', 'two', 'three'],
    sources: [
      { source_id: 'one', status: 'SUCCESS' },
      { source_id: 'two', status: 'PARTIAL' },
      { source_id: 'three', status: 'QUEUED' },
    ],
  });
  assert.equal(progress.completedSources, 2);
  assert.equal(progress.totalSources, 3);
});

test('rejects malformed payloads and inconsistent counters', () => {
  assert.equal(normalizeFetchProgress(null), null);
  const progress = normalizeFetchProgress({
    selected_sources: ['a'],
    progress: { completed_sources: 3, total_sources: 1 },
    started_at: 'not-a-date',
  });
  assert.equal(progress.completedSources, null);
  assert.equal(progress.startedAt, null);
});

test('deduplicates source IDs and ignores invalid source records', () => {
  const progress = normalizeFetchProgress({
    selected_sources: ['valid_source', '../unsafe', 'valid_source'],
    sources: [
      { source_id: 'valid_source', status: 'FAILED' },
      { source_id: 'valid_source', status: 'SUCCESS' },
      { source_id: '../unsafe', status: 'SUCCESS' },
      '<img src=x>',
    ],
  });
  assert.deepEqual(progress.selectedSources, ['valid_source']);
  assert.equal(progress.outcomes.length, 1);
  assert.equal(progress.completedSources, 1);
});
