"""Explicit offline composition using the existing Wikimedia analysis components."""
import asyncio
from datetime import date, datetime, timezone
import httpx

from app.analysis.keywords import extract_keywords
from app.analysis.trend import DayData
from app.composition import algorithm_engine
from app.models.observations import FetchStatus
from app.processing.wikimedia_cleaning import clean_snapshot
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
        self.analysis_date=analysis_date
        self.dates=tuple(dates)
        if type(analysis_date) is not date or not self.dates or self.dates[0] != analysis_date:
            raise ValueError('analysis date must be first')

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
                return [await source.fetch_day(day) for day in self.dates]
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
        for snapshot, rows in snapshots:
            cleaned=clean_snapshot(snapshot,rows)
            days.append(DayData(snapshot,tuple(extract_keywords(cleaned.accepted)),tuple(rows)))
        if not days[0].keywords: raise ValueError('no valid observations')
        return days

    def analyze(self, days):
        # One invocation of the existing v0.1 batch scorer via the engine.
        scores=algorithm_engine().legacy_results(self.analysis_date,days)
        items=[score.to_record() for score in scores]
        return {'metadata':{'source':METADATA.source_id,'country':'US','access':'all-access',
                            'analysis_date':self.analysis_date.isoformat(), 'pipeline_version':'offline-refresh-v1',
                            'classification_version':'title-rules-v0.1','algorithm_version':'wikimedia-trend-v0.1'},
                'shards':{'scores-v1':{'items':items}},'quality_ok':bool(items)}
