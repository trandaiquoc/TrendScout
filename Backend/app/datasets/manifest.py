"""Versioned offline manifest validation; no cloud or trust in caller booleans."""
from datetime import date

REQUIRED = ('dataset_id','job_id','occurrence_id','resource_id','source_id','country','access','project',
            'analysis_date','window_start','window_end','source_version','processing_version','cleaning_version',
            'classification_version','algorithm_version','provenance','quality_report_ref')


def validate_manifest(manifest, *, dataset_id, job_id, resource_id, metadata, shards, hash_fn):
    if not isinstance(manifest,dict) or manifest.get('manifest_version') != 'offline-manifest-v1': return False
    if any(not isinstance(manifest.get(k),str) or not manifest[k].strip() for k in REQUIRED): return False
    if (manifest['dataset_id'],manifest['job_id'],manifest['resource_id']) != (dataset_id,job_id,resource_id): return False
    if any(metadata.get(k) != manifest[k] for k in REQUIRED if k not in ('dataset_id','job_id','resource_id')): return False
    try:
        start,end,analysis=(date.fromisoformat(manifest[k]) for k in ('window_start','window_end','analysis_date'))
    except (ValueError,TypeError): return False
    if not start <= analysis <= end: return False
    required=manifest.get('required_partitions'); expected=manifest.get('shards'); report=manifest.get('quality_report')
    if not isinstance(required,list) or not required or any(not isinstance(x,str) or not x for x in required) or len(set(required)) != len(required): return False
    if not isinstance(expected,dict) or set(expected) != set(required) or set(shards) != set(required): return False
    if not isinstance(report,dict) or report.get('policy_version') != 'offline-quality-v1' or report.get('status') != 'PASS': return False
    total=0
    for key in required:
        shard=shards[key]; spec=expected[key]
        if not isinstance(shard,dict) or not isinstance(shard.get('items'),list) or not isinstance(spec,dict): return False
        count=len(shard['items'])
        if type(spec.get('count')) is not int or spec['count'] != count or spec.get('sha256') != hash_fn(shard): return False
        total+=count
    return type(manifest.get('expected_count')) is int and total == manifest['expected_count'] and report.get('observed_count') == total
