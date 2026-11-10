"""Offline-only budgets and bounded retry classification, not cloud quota telemetry."""
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum


class ErrorCode(str, Enum):
    RATE_LIMITED='RATE_LIMITED'
    RETRYABLE_SOURCE_ERROR='RETRYABLE_SOURCE_ERROR'
    RETRYABLE_STORAGE_ERROR='RETRYABLE_STORAGE_ERROR'
    PERMANENT_SOURCE_ERROR='PERMANENT_SOURCE_ERROR'
    INVALID_SOURCE_DATA='INVALID_SOURCE_DATA'
    QUOTA_EXCEEDED='QUOTA_EXCEEDED'
    VERIFICATION_FAILED='VERIFICATION_FAILED'
    PUBLISH_CONFLICT='PUBLISH_CONFLICT'
    LEASE_LOST='LEASE_LOST'
    CANCELLED='CANCELLED'


class RefreshError(ValueError):
    def __init__(self, code, retry_after=0):
        if not isinstance(code, ErrorCode) or type(retry_after) not in (int,float) or not 0 <= retry_after <= 3600:
            raise ValueError('invalid error metadata')
        self.code=code; self.retry_after=retry_after
        super().__init__(code.value)


@dataclass
class OfflineBudget:
    """Job-scoped estimated operation budget, not cloud billing telemetry."""
    max_reads: int
    max_writes: int
    reads: int=0
    writes: int=0
    metadata_operations: int=0
    heartbeats: int=0
    source_requests: int=0
    retry_operations: int=0

    def __post_init__(self):
        if any(type(x) is not int or x < 0 for x in (self.max_reads,self.max_writes,self.reads,self.writes,self.metadata_operations,self.heartbeats,self.source_requests,self.retry_operations)):
            raise ValueError('nonnegative integer budget required')

    def charge(self, *, reads=0, writes=0, kind=None):
        if type(reads) is not int or type(writes) is not int or reads < 0 or writes < 0: raise ValueError('invalid charge')
        if kind not in (None, 'metadata', 'heartbeat', 'retry', 'source'): raise ValueError('invalid budget category')
        if self.reads+reads > self.max_reads or self.writes+writes > self.max_writes: raise RefreshError(ErrorCode.QUOTA_EXCEEDED)
        self.reads+=reads; self.writes+=writes
        if kind == 'metadata': self.metadata_operations+=1
        elif kind == 'heartbeat': self.heartbeats+=1
        elif kind == 'retry': self.retry_operations+=1
        if kind == 'source': self.source_requests+=1


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_seconds: int
    max_seconds: int
    version: str='offline-backoff-v1'

    def __post_init__(self):
        if any(type(v) is not int for v in (self.max_attempts,self.base_seconds,self.max_seconds)) or self.max_attempts < 1 or self.base_seconds < 0 or self.max_seconds < self.base_seconds:
            raise ValueError('invalid retry policy')

    def delay(self, error, attempt, now, deadline):
        retryable={ErrorCode.RATE_LIMITED, ErrorCode.RETRYABLE_SOURCE_ERROR, ErrorCode.RETRYABLE_STORAGE_ERROR}
        if not isinstance(error,RefreshError) or error.code not in retryable or type(attempt) is not int or not 1 <= attempt < self.max_attempts:
            return None
        delay=max(min(self.max_seconds,self.base_seconds*(2**(attempt-1))),error.retry_after)
        if delay > self.max_seconds or now+timedelta(seconds=delay) >= deadline: return None
        return timedelta(seconds=delay)
