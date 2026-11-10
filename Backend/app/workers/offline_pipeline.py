"""Explicit offline composition using the existing Wikimedia analysis components."""
import asyncio
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
        self.retry_after = None
        self.request_guard=None
        self.analysis_date=analysis_date
        self.dates=tuple(dates)
        if type(analysis_date) is not date or not self.dates or self.dates[0] != analysis_date:
            raise ValueError('analysis date must be first')

    def set_request_guard(self, guard):
        self.request_guard=guard

    def fetch(self):
        self.retry_after = None
        async def collect():
            async def observe(response):
                if response.status_code == 429 and self.retry_after is None:
                    self.retry_after=response.headers.get('Retry-After')
            async with httpx.AsyncClient(transport=self.transport, event_hooks={'response':[observe]}) as client:
                registry=SourceRegistry()
                registry.register(METADATA,lambda: WikimediaAdapter(client,max_retries=0))
                source=registry.create(METADATA.source_id)
                results=[]
                for day in self.dates:
                    if self.request_guard is not None: self.request_guard()
                    results.append(await source.fetch_day(day))
                return results
        snapshots=asyncio.run(collect())
        current=snapshots[0][0]
        if current.fetch_status not in (FetchStatus.SUCCESS, FetchStatus.PARTIAL):
            if current.http_status == 429:
                delay=_retry_delay(self.retry_after,0,self.clock())
                # Reject excessive provider delays via RetryPolicy without shortening them.
                if delay > 3600: raise RefreshError(ErrorCode.PERMANENT_SOURCE_ERROR)
                raise RefreshError(ErrorCode.RATE_LIMITED,retry_after=delay)
            if current.http_status is not None and current.http_status >= 500: raise RefreshError(ErrorCode.RETRYABLE_SOURCE_ERROR)
            if current.http_status == 404: raise RefreshError(ErrorCode.PERMANENT_SOURCE_ERROR)
            if current.http_status is None and current.error and ('Timeout' in current.error or 'Error' in current.error): raise RefreshError(ErrorCode.RETRYABLE_SOURCE_ERROR)
            raise RefreshError(ErrorCode.INVALID_SOURCE_DATA)
        return snapshots

    def clean(self, snapshots):
        days=[]
        self.reports=[]
        for snapshot, rows in snapshots:
            cleaned=clean_snapshot(snapshot,rows)
            report=cleaned.report
            self.reports.append({'date':snapshot.observation_date.isoformat(),'status':snapshot.fetch_status.value,
                                 'raw_count':report.raw_count,'accepted_count':report.accepted_count,
                                 'rejected_count':report.rejected_count,'duplicate_count':report.duplicate_count,
                                 'rejection_reasons':report.rejection_reasons,'quality_flags':list(report.quality_flags),
                                 'source_url':snapshot.request_url})
            days.append(DayData(snapshot,tuple(extract_keywords(cleaned.accepted)),tuple(rows)))
        if not days[0].keywords: raise ValueError('no valid observations')
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
        manifest_input={'source_id':source,'country':snapshots[0].country_scope,'access':snapshots[0].access,
                        'project':','.join(projects),'analysis_date':self.analysis_date.isoformat(),
                        'window_start':(self.analysis_date-timedelta(days=7)).isoformat(),'window_end':self.analysis_date.isoformat(),
                        'source_version':'wikimedia-top-per-country-v1','processing_version':PIPELINE_VERSION,
                        'cleaning_version':PIPELINE_VERSION,'classification_version':'title-rules-v0.1',
                        'algorithm_version':'wikimedia-trend-v0.1',
                        'provenance':snapshots[0].request_url,'quality_report_ref':'quality-v1'}
        report={'policy_version':'offline-quality-v1','status':'PASS','observed_count':len(items),
                'source_id':source,'processing_version':PIPELINE_VERSION,'snapshots':self.reports}
        return {'manifest_input':manifest_input,'quality_report':report,'shards':{'scores-v1':{'items':items}}}
