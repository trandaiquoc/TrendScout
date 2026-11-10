"""Explicit offline composition using the existing Wikimedia analysis components."""
import asyncio
from contextvars import ContextVar
from dataclasses import asdict
import json
from datetime import date, datetime, timedelta, timezone
import httpx

from app.analysis.keywords import extract_keywords
from app.analysis.trend import DayData
from app.composition import algorithm_engine
from app.models.observations import FetchStatus
from app.processing.wikimedia_cleaning import clean_snapshot
from app.processing.contracts import PIPELINE_VERSION
from app.sources.registry import SourceRegistry
from app.sources.wikimedia.adapter import METADATA, WikimediaAdapter, _retry_delay
from app.workers.safety import ErrorCode, RefreshError


class OfflineWikimediaPipeline:
    """Requires mock transport; never constructs a network-capable source client."""
    def __init__(self, transport, analysis_date, dates, *, clock=None):
        if not isinstance(transport, httpx.MockTransport): raise ValueError('offline mock transport required')
        self.transport=transport
        self.clock=clock or (lambda: datetime.now(timezone.utc))
        self._request_guard=ContextVar('wikimedia_request_guard',default=None)
        self._reports=ContextVar('wikimedia_cleaning_reports',default=None)
        self.analysis_date=analysis_date
        self.dates=tuple(dates)
        if type(analysis_date) is not date or not self.dates or self.dates[0] != analysis_date:
            raise ValueError('analysis date must be first')

    def set_request_guard(self, guard):
        return self._request_guard.set(guard)

    def reset_request_guard(self, token):
        self._request_guard.reset(token)

    def fetch(self):
        if not isinstance(self.transport, httpx.MockTransport): raise ValueError('offline mock transport required')
        retry_after=None
        request_guard=self._request_guard.get()
        async def collect():
            async def observe(response):
                nonlocal retry_after
                if response.status_code == 429:
                    retry_after=response.headers.get('Retry-After')
            async with httpx.AsyncClient(transport=self.transport, event_hooks={'response':[observe]}) as client:
                registry=SourceRegistry()
                registry.register(METADATA,lambda: WikimediaAdapter(client,max_retries=0))
                source=registry.create(METADATA.source_id)
                results=[]; rate_limited=False
                for day in self.dates:
                    if request_guard is not None: request_guard()
                    result=await source.fetch_day(day)
                    results.append(result)
                    # A historical response can rate-limit the entire source;
                    # do not issue another day's request before Retry-After.
                    if result[0].http_status == 429:
                        delay=_retry_delay(retry_after,0,self.clock())
                        if day == self.analysis_date or delay > 0:
                            rate_limited=True
                            break
                return results,rate_limited
        snapshots,rate_limited=asyncio.run(collect())
        if rate_limited:
            delay=_retry_delay(retry_after,0,self.clock())
            if delay > 3600: raise RefreshError(ErrorCode.PERMANENT_SOURCE_ERROR)
            raise RefreshError(ErrorCode.RATE_LIMITED,retry_after=delay)
        current=snapshots[0][0]
        if current.fetch_status not in (FetchStatus.SUCCESS, FetchStatus.PARTIAL):
            if current.http_status is not None and current.http_status >= 500: raise RefreshError(ErrorCode.RETRYABLE_SOURCE_ERROR)
            if current.http_status == 404: raise RefreshError(ErrorCode.PERMANENT_SOURCE_ERROR)
            if current.http_status is None and current.error and ('Timeout' in current.error or 'Error' in current.error): raise RefreshError(ErrorCode.RETRYABLE_SOURCE_ERROR)
            raise RefreshError(ErrorCode.INVALID_SOURCE_DATA)
        return snapshots

    def clean(self, snapshots):
        days=[]
        reports=[]
        for snapshot, rows in snapshots:
            cleaned=clean_snapshot(snapshot,rows)
            report=cleaned.report
            reports.append({'date':snapshot.observation_date.isoformat(),'status':snapshot.fetch_status.value,
                                 'raw_count':report.raw_count,'accepted_count':report.accepted_count,
                                 'rejected_count':report.rejected_count,'duplicate_count':report.duplicate_count,
                                 'rejection_reasons':report.rejection_reasons,'quality_flags':list(report.quality_flags),
                                 'source_url':snapshot.request_url})
            days.append(DayData(snapshot,tuple(extract_keywords(cleaned.accepted)),tuple(rows)))
        if not days[0].keywords: raise ValueError('no valid observations')
        self._reports.set(reports)
        return days

    def analyze(self, days):
        # One invocation of the existing v0.1 batch scorer via the engine.
        scores=algorithm_engine().legacy_results(self.analysis_date,days)
        items=[score.to_record() for score in scores]
        snapshots=[day.snapshot for day in days]
        source=snapshots[0].source
        dates=[snap.observation_date for snap in snapshots]
        projects=sorted({row.project for day in days for row in day.keywords})
        if not projects: raise ValueError('missing project evidence')
        manifest_input={'config_version':'v1','source_id':source,'country':snapshots[0].country_scope,'access':snapshots[0].access,
                        'project':'all','analysis_date':self.analysis_date.isoformat(),
                        'window_start':(self.analysis_date-timedelta(days=7)).isoformat(),'window_end':self.analysis_date.isoformat(),
                        'source_version':'wikimedia-top-per-country-v1','processing_version':PIPELINE_VERSION,
                        'cleaning_version':PIPELINE_VERSION,'classification_version':'title-rules-v0.1',
                        'algorithm_version':'wikimedia-trend-v0.1',
                        'provenance':snapshots[0].request_url,'quality_report_ref':'quality-v1'}
        def encode(value):
            return json.loads(json.dumps(asdict(value),ensure_ascii=False,default=str))
        snapshot_rows=[encode(day.snapshot) for day in days]
        raw_rows=[{'date':day.snapshot.observation_date.isoformat(),'rows':[encode(row) for row in day.source_rows]} for day in days]
        shards={'scores-v1':{'items':items}, 'snapshots-v1':{'items':snapshot_rows},
                'raw-observations-v1':{'items':raw_rows}}
        reports=self._reports.get()
        if reports is None: raise ValueError('cleaning reports required in current run')
        report={'policy_version':'offline-quality-v1','status':'PASS','observed_count':sum(len(x['items']) for x in shards.values()),
                'keyword_count':len(items),
                'source_id':source,'processing_version':PIPELINE_VERSION,'snapshots':reports}
        return {'manifest_input':manifest_input,'quality_report':report,'shards':shards}
