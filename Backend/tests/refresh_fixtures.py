"""Synthetic strict inputs backed by the same cleaning evidence contract."""
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
import json
from app.models.observations import DatasetSnapshot, FetchStatus
from app.sources.wikimedia.adapter import _parse
from app.processing.wikimedia_cleaning import clean_snapshot
from app.processing.contracts import PIPELINE_VERSION
from app.analysis.keywords import extract_keywords
from app.analysis.trend import DayData, score_trends


def strict_output(output):
    if 'manifest_input' in output: return output
    output=dict(output); shards=dict(output.get('shards', {}))
    day=date(2026,1,8); fetched=datetime(2026,1,9,tzinfo=timezone.utc)
    response={'items':[{'country':'US','access':'all-access','year':'2026','month':'01','day':'08',
                        'articles':[{'project':'en.wikipedia','article':'Synthetic_article','views_ceil':10,'rank':1}]}]}
    observations,count,rejected,invalid=_parse(response,day,fetched)
    snap=DatasetSnapshot('wikimedia_pageviews_top_per_country','US','all-access',day,fetched,FetchStatus.SUCCESS,200,count,'https://wikimedia.org/api/rest_v1/metrics/pageviews/top-per-country/US/all-access/2026/01/08',1,rejected,None,invalid)
    cleaned=clean_snapshot(snap,observations).report
    encode=lambda obj:json.loads(json.dumps(asdict(obj),ensure_ascii=False,default=str))
    keywords=tuple(extract_keywords(clean_snapshot(snap,observations).accepted))
    score=score_trends(day,[DayData(snap,keywords,tuple(observations))])[0].to_record()
    shards.update({'snapshots-v1':{'items':[encode(snap)]},'raw-observations-v1':{'items':[{'date':day.isoformat(),'rows':[encode(x) for x in observations]}]},
                   'scores-v1':{'items':[score]}})
    output['shards']=shards
    output['manifest_input']={'source_id':'wikimedia_pageviews_top_per_country','country':'US','access':'all-access','project':'all',
        'analysis_date':'2026-01-08','window_start':'2026-01-01','window_end':'2026-01-08',
        'source_version':'wikimedia-top-per-country-v1','processing_version':PIPELINE_VERSION,'cleaning_version':PIPELINE_VERSION,
        'classification_version':'title-rules-v0.1','algorithm_version':'wikimedia-trend-v0.1','config_version':'v1',
        'provenance':snap.request_url,'quality_report_ref':'quality-v1'}
    output['quality_report']={'policy_version':'offline-quality-v1','status':'PASS' if output.get('quality_ok') else 'FAIL',
        'source_id':'wikimedia_pageviews_top_per_country','processing_version':PIPELINE_VERSION,'observed_count':sum(len(x['items']) for x in shards.values()),'keyword_count':1,
        'snapshots':[{'date':day.isoformat(),'status':snap.fetch_status.value,'source_url':snap.request_url,
                      'quality_flags':list(cleaned.quality_flags),'rejection_reasons':cleaned.rejection_reasons,
                      'raw_count':cleaned.raw_count,'accepted_count':cleaned.accepted_count,
                      'rejected_count':cleaned.rejected_count,'duplicate_count':cleaned.duplicate_count}]}
    return output


def strict_worker(worker_class, *args, **kwargs):
    """Inject only MockTransport for synthetic source/fault scenarios."""
    import httpx
    from app.workers.offline_pipeline import OfflineWikimediaPipeline
    args=list(args)
    if len(args) >= 5:
        fetch,clean,analyze=args[2:5]
        if not isinstance(getattr(fetch,'__self__',None),OfflineWikimediaPipeline):
            holder={}
            def handler(request):
                holder['value']=fetch()
                return httpx.Response(200,json={'items':[{'country':'US','access':'all-access','year':'2026','month':'01','day':'08',
                    'articles':[{'project':'en.wikipedia','article':'Synthetic_article','views_ceil':10,'rank':1}]}]})
            pipeline=OfflineWikimediaPipeline(httpx.MockTransport(handler),date(2026,1,8),[date(2026,1,8)])
            args[2]=pipeline.fetch
            args[3]=lambda ignored: clean(holder['value'])
        def wrapped(value): return strict_output(analyze(value))
        args[4]=wrapped
    return worker_class(*args, **kwargs)


def fixture_policy(*args, **kwargs):
    """Supply an explicit, bounded synthetic policy for legacy job tests."""
    from app.jobs.contracts import Policy
    from app.workers.safety import OfflineBudget
    kwargs.setdefault('operation_budget', OfflineBudget(10000,10000))
    return Policy(*args, **kwargs)
