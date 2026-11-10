"""Synthetic strict inputs for worker tests, never imported by application code."""
def strict_output(output):
    if 'manifest_input' in output: return output
    shards=output.get('shards', {})
    count=sum(len(shard.get('items', [])) for shard in shards.values())
    output=dict(output)
    output['manifest_input']={'source_id':'wikimedia','country':'US','access':'all-access','project':'synthetic',
        'analysis_date':'2026-01-08','window_start':'2026-01-01','window_end':'2026-01-08',
        'source_version':'synthetic-v1','processing_version':'synthetic-v1','cleaning_version':'synthetic-v1',
        'classification_version':'synthetic-v1','algorithm_version':'synthetic-v1',
        'provenance':'synthetic://fixture','quality_report_ref':'quality-v1'}
    output['quality_report']={'policy_version':'offline-quality-v1','status':'PASS' if output.get('quality_ok') else 'FAIL',
        'source_id':'wikimedia','processing_version':'synthetic-v1','observed_count':count,
        'snapshots':[{'date':'2026-01-08','status':'SUCCESS','source_url':'synthetic://fixture','quality_flags':[],'rejection_reasons':{},'raw_count':count,'accepted_count':count,'rejected_count':0,'duplicate_count':0}]}
    return output


def strict_worker(worker_class, *args, **kwargs):
    args=list(args)
    if len(args) >= 5:
        analyze=args[4]
        def wrapped(value): return strict_output(analyze(value))
        args[4]=wrapped
    return worker_class(*args, **kwargs)
