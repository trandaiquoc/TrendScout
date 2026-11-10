"""Rebuild synthetic offline evidence and independently replay existing cleaning rules."""
from datetime import date, datetime
from app.models.observations import DatasetSnapshot, FetchStatus, PageObservation
from app.processing.wikimedia_cleaning import clean_snapshot
from app.processing.contracts import PIPELINE_VERSION


def validate_source_evidence(manifest, shards, quality):
    required={'snapshots-v1','raw-observations-v1','scores-v1'}
    if not required <= shards.keys() or not isinstance(quality,dict): return False
    try:
        snapshots=shards['snapshots-v1']['items']; raw=shards['raw-observations-v1']['items']
        reports=quality['snapshots']; scores=shards['scores-v1']['items']
        if not isinstance(snapshots,list) or not snapshots or len(snapshots)!=len(raw) or len(raw)!=len(reports): return False
        if quality.get('keyword_count') != len(scores) or quality.get('processing_version') != PIPELINE_VERSION: return False
        seen=set()
        for data, group, declared in zip(snapshots,raw,reports):
            values=dict(data); values['observation_date']=date.fromisoformat(values['observation_date'])
            values['fetched_at']=datetime.fromisoformat(values['fetched_at']); values['fetch_status']=FetchStatus(values['fetch_status'])
            values['rejected_records']=tuple(values['rejected_records'])
            snapshot=DatasetSnapshot(**values); day=snapshot.observation_date.isoformat()
            if day in seen or group['date']!=day or declared['date']!=day: return False
            seen.add(day)
            if (snapshot.source,snapshot.country_scope,snapshot.access) != (manifest['source_id'],manifest['country'],manifest['access']): return False
            if not manifest['window_start'] <= day <= manifest['window_end']: return False
            if snapshot.fetch_status in (FetchStatus.FETCH_ERROR,FetchStatus.DAY_UNAVAILABLE):
                if snapshot.item_count is not None or group['rows']: return False
            rows=[]
            for raw_row in group['rows']:
                item=dict(raw_row); item['observation_date']=date.fromisoformat(item['observation_date'])
                item['fetched_at']=datetime.fromisoformat(item['fetched_at']) if item.get('fetched_at') else None
                item['quality_flags']=tuple(item['quality_flags']); rows.append(PageObservation(**item))
            cleaned=clean_snapshot(snapshot,rows).report
            calculated={'date':day,'status':snapshot.fetch_status.value,'raw_count':cleaned.raw_count,
                        'accepted_count':cleaned.accepted_count,'rejected_count':cleaned.rejected_count,
                        'duplicate_count':cleaned.duplicate_count,'rejection_reasons':cleaned.rejection_reasons,
                        'quality_flags':list(cleaned.quality_flags),'source_url':snapshot.request_url}
            if declared != calculated: return False
            if day == manifest['analysis_date'] and snapshot.request_url != manifest['provenance']: return False
        return manifest['analysis_date'] in seen
    except (KeyError,ValueError,TypeError,AttributeError,OverflowError):
        return False
