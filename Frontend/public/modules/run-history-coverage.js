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
    return {
      sourceId: typeof source?.source_id === 'string' ? source.source_id : '',
      status: typeof source?.status === 'string' ? source.status : 'UNKNOWN',
      providerReturnedRecords: firstCount(
        coverage.provider_returned_records,
        coverage.item_count,
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
    };
  });

  const providerCounts = sources.map(source => source.providerReturnedRecords);
  const providerRecordCount = providerCounts.length > 0
    && providerCounts.every(value => value !== null)
    ? providerCounts.reduce((sum, value) => sum + value, 0)
    : firstCount(item?.provider_record_count);

  return { providerRecordCount, sources };
}
