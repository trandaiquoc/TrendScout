"""Rebuild synthetic offline evidence and independently replay existing cleaning rules."""
from datetime import date, datetime
from app.models.observations import DatasetSnapshot, FetchStatus, PageObservation
from app.processing.wikimedia_cleaning import clean_snapshot
from app.analysis.keywords import extract_keywords
from app.analysis.trend import ALGORITHM_VERSION, DayData, score_trends
from app.taxonomy.legacy import CLASSIFICATION_VERSION
from app.processing.contracts import PIPELINE_VERSION
from app.sources.wikimedia.adapter import BASE_URL, _parse


def validate_source_evidence(manifest, shards, quality):
    if manifest.get('source_id') == 'wikimedia_pageviews_top_per_country':
        versions={'source_version':'wikimedia-top-per-country-v1','processing_version':PIPELINE_VERSION,
                  'cleaning_version':PIPELINE_VERSION,'classification_version':CLASSIFICATION_VERSION,'algorithm_version':ALGORITHM_VERSION}
        if any(manifest.get(key)!=value for key,value in versions.items()): return False
    required={'snapshots-v1','raw-observations-v1','scores-v1'}
    if not required <= shards.keys() or not isinstance(quality,dict): return False
    try:
        snapshots=shards['snapshots-v1']['items']; raw=shards['raw-observations-v1']['items']
        reports=quality['snapshots']; scores=shards['scores-v1']['items']
        if not isinstance(snapshots,list) or not snapshots or len(snapshots)!=len(raw) or len(raw)!=len(reports): return False
        if quality.get('keyword_count') != len(scores) or quality.get('processing_version') != PIPELINE_VERSION: return False
        seen=set(); expected_keywords=None; days=[]
        for data, group, declared in zip(snapshots,raw,reports):
            values=dict(data); values['observation_date']=date.fromisoformat(values['observation_date'])
            values['fetched_at']=datetime.fromisoformat(values['fetched_at']); values['fetch_status']=FetchStatus(values['fetch_status'])
            values['rejected_records']=tuple(values['rejected_records'])
            snapshot=DatasetSnapshot(**values); day=snapshot.observation_date.isoformat()
            if day in seen or group['date']!=day or declared['date']!=day: return False
            seen.add(day)
            if (snapshot.source,snapshot.country_scope,snapshot.access) != (manifest['source_id'],manifest['country'],manifest['access']): return False
            if snapshot.fetch_status in (FetchStatus.SUCCESS,FetchStatus.PARTIAL) and snapshot.http_status != 200: return False
            if snapshot.fetch_status in (FetchStatus.SUCCESS,FetchStatus.PARTIAL):
                if type(snapshot.rejected_count) is not int or snapshot.rejected_count != len(snapshot.rejected_records): return False
                if snapshot.fetch_status == FetchStatus.SUCCESS and snapshot.rejected_count != 0: return False
                if snapshot.fetch_status == FetchStatus.PARTIAL and (snapshot.rejected_count == 0 or not group['rows']): return False
            if snapshot.fetch_status == FetchStatus.DAY_UNAVAILABLE and snapshot.http_status != 404: return False
            if snapshot.fetch_status == FetchStatus.FETCH_ERROR and snapshot.http_status == 404: return False
            if snapshot.source == 'wikimedia_pageviews_top_per_country' and snapshot.request_url != f'{BASE_URL}/{snapshot.observation_date:%Y/%m/%d}': return False
            if not manifest['window_start'] <= day <= manifest['window_end']: return False
            if snapshot.fetch_status in (FetchStatus.FETCH_ERROR,FetchStatus.DAY_UNAVAILABLE):
                if snapshot.item_count is not None or group['rows']: return False
            if snapshot.rejected_records:
                rejected_payload={'items':[{'country':snapshot.country_scope,'access':snapshot.access,
                    'year':snapshot.observation_date.strftime('%Y'),'month':snapshot.observation_date.strftime('%m'),
                    'day':snapshot.observation_date.strftime('%d'),'articles':list(snapshot.rejected_records)}]}
                parsed, _, rejected, _ = _parse(rejected_payload, snapshot.observation_date, snapshot.fetched_at)
                if parsed or rejected != len(snapshot.rejected_records): return False
            rows=[]
            for raw_row in group['rows']:
                item=dict(raw_row); item['observation_date']=date.fromisoformat(item['observation_date'])
                item['fetched_at']=datetime.fromisoformat(item['fetched_at']) if item.get('fetched_at') else None
                item['quality_flags']=tuple(item['quality_flags']); row=PageObservation(**item)
                original=row.raw_record
                if not isinstance(original,dict) or (original.get('project'),original.get('article'),original.get('views_ceil'),original.get('rank')) != (row.project,row.article_original,row.views_ceil,row.provider_rank): return False
                rows.append(row)
            if rows:
                raw_payload={'items':[{'country':snapshot.country_scope,'access':snapshot.access,
                    'year':snapshot.observation_date.strftime('%Y'),'month':snapshot.observation_date.strftime('%m'),
                    'day':snapshot.observation_date.strftime('%d'),'articles':[row.raw_record for row in rows]}]}
                reconstructed, _, rejected, _ = _parse(raw_payload,snapshot.observation_date,snapshot.fetched_at)
                if rejected or reconstructed != rows: return False
            cleaned_result=clean_snapshot(snapshot,rows)
            cleaned=cleaned_result.report
            keywords=tuple(extract_keywords(cleaned_result.accepted))
            days.append(DayData(snapshot,keywords,tuple(rows)))
            if day == manifest['analysis_date']:
                expected_keywords={row.keyword_id:row for row in keywords}
            calculated={'date':day,'status':snapshot.fetch_status.value,'raw_count':cleaned.raw_count,
                        'accepted_count':cleaned.accepted_count,'rejected_count':cleaned.rejected_count,
                        'duplicate_count':cleaned.duplicate_count,'rejection_reasons':cleaned.rejection_reasons,
                        'quality_flags':list(cleaned.quality_flags),'source_url':snapshot.request_url}
            if declared != calculated: return False
            if day == manifest['analysis_date'] and snapshot.request_url != manifest['provenance']: return False
        score_ids=[row.get('keyword_id') for row in scores if isinstance(row,dict)]
        if (manifest['analysis_date'] not in seen or expected_keywords is None or len(score_ids)!=len(scores)
            or len(score_ids)!=len(set(score_ids)) or set(score_ids)!=set(expected_keywords)): return False
        # Reuse the existing scoring engine over independently reconstructed
        # snapshots; do not trust a caller-supplied status or missing-day reason.
        recomputed={item.keyword_id:item.to_record() for item in score_trends(date.fromisoformat(manifest['analysis_date']),days)}
        if {item['keyword_id']:item for item in scores} != recomputed: return False
        for score in scores:
            keyword=expected_keywords[score['keyword_id']]
            if (score.get('project'),score.get('article_original'),score.get('current_views_ceil')) != (keyword.project,keyword.article_original,keyword.views_ceil): return False
            status=score.get('status'); value=score.get('score')
            if status == 'INSUFFICIENT_DATA':
                if value is not None: return False
            elif status not in ('EMERGING','STABLE','FALLING') or type(value) is not int or not 0 <= value <= 100:
                return False
        return True
    except (KeyError,ValueError,TypeError,AttributeError,OverflowError):
        return False
