const SOURCE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const SOURCE_TERMINAL = new Set(['SUCCESS', 'PARTIAL', 'FAILED', 'UNAVAILABLE']);

function count(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function timestamp(value) {
  return typeof value === 'string' && Number.isFinite(Date.parse(value)) ? value : null;
}

/**
 * Normalize the public Fetch status response before it reaches the progress UI.
 * Unknown fields are ignored; no provider response is treated as trusted markup.
 *
 * @param {unknown} value
 * @returns {null | {
 *   selectedSources: string[], outcomes: Array<{sourceId: string, status: string, value: object}>,
 *   completedSources: number | null, totalSources: number | null,
 *   startedAt: string | null, finishedAt: string | null, terminal: boolean
 * }}
 */
export function normalizeFetchProgress(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;

  const payload = value;
  const rawSources = Array.isArray(payload.sources) ? payload.sources : [];
  const outcomes = [];
  const seen = new Set();
  for (const source of rawSources) {
    if (!source || typeof source !== 'object' || Array.isArray(source)
        || typeof source.source_id !== 'string' || !SOURCE_ID.test(source.source_id)
        || seen.has(source.source_id)) continue;
    seen.add(source.source_id);
    const status = typeof source.status === 'string'
      && /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(source.status)
      ? source.status.toUpperCase() : 'UNKNOWN';
    outcomes.push({ sourceId: source.source_id, status, value: source });
  }

  const listedIds = rawSources.every((source) => typeof source === 'string')
    ? rawSources : [];
  const requestedIds = Array.isArray(payload.selected_sources)
    ? payload.selected_sources : listedIds.length ? listedIds : outcomes.map((item) => item.sourceId);
  const selectedSources = [...new Set(requestedIds.filter((sourceId) =>
    typeof sourceId === 'string' && SOURCE_ID.test(sourceId)))];

  const rawProgress = payload.progress && typeof payload.progress === 'object'
    && !Array.isArray(payload.progress) ? payload.progress : {};
  const rawTotal = count(rawProgress.total_sources);
  const totalSources = rawTotal ?? (selectedSources.length || outcomes.length || null);
  const rawCompleted = count(rawProgress.completed_sources);
  const outcomeCompleted = outcomes.filter((item) => SOURCE_TERMINAL.has(item.status)).length;
  const candidateCompleted = rawCompleted ?? (outcomes.length ? outcomeCompleted : null);
  const completedSources = candidateCompleted !== null && totalSources !== null
    && candidateCompleted <= totalSources ? candidateCompleted : null;

  return {
    selectedSources,
    outcomes,
    completedSources,
    totalSources,
    startedAt: timestamp(payload.started_at),
    finishedAt: timestamp(payload.finished_at),
    terminal: payload.terminal === true,
  };
}
