"""Fixed-anchor UTC scheduling. Explicit invocation only, no background loop."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import threading


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError('UTC-aware time required')
    return value


@dataclass(frozen=True)
class Schedule:
    schedule_id: str
    version: str
    anchor: datetime
    interval: timedelta = timedelta(days=7)
    cooldown: timedelta = timedelta(0)
    max_catch_up: int = 1
    max_concurrent: int = 1
    enabled: bool = False

    def __post_init__(self):
        utc(self.anchor)
        if not self.schedule_id or not self.version or not isinstance(self.interval,timedelta) or self.interval <= timedelta(0) or not isinstance(self.cooldown,timedelta) or self.cooldown < timedelta(0):
            raise ValueError('invalid schedule')
        if type(self.max_catch_up) is not int or self.max_catch_up != 1 or type(self.max_concurrent) is not int or self.max_concurrent < 1 or type(self.enabled) is not bool:
            raise ValueError('unsupported scheduler policy')

    def occurrence(self, instant):
        instant = utc(instant)
        delta = instant - self.anchor
        if delta < timedelta(0) or delta % self.interval:
            raise ValueError('instant is not an occurrence')
        return hashlib.sha256(f'{self.schedule_id}\0{self.version}\0{instant.isoformat()}'.encode()).hexdigest()

    def next_after(self, instant):
        instant = utc(instant)
        if instant < self.anchor: return self.anchor
        return self.anchor + (int((instant-self.anchor)//self.interval)+1)*self.interval

    def latest_due(self, instant):
        instant = utc(instant)
        if instant < self.anchor: return None
        return self.anchor + int((instant-self.anchor)//self.interval)*self.interval


class FakeScheduler:
    """Single-process policy demo; no durable scheduling or distributed CAS."""
    def __init__(self, schedule):
        self.schedule = schedule
        self._lock = threading.RLock()
        self.paused = False
        self.stopped = False
        self.dispatched = set()
        self.last_dispatch = None

    def pause(self): self.paused = True
    def resume(self):
        if self.stopped: raise ValueError('stopped schedule')
        self.paused = False
    def stop(self): self.stopped = True

    def dispatch(self, now, *, active_jobs, budget_ok):
        with self._lock:
            return self._dispatch(now, active_jobs=active_jobs, budget_ok=budget_ok)

    def _dispatch(self, now, *, active_jobs, budget_ok):
        utc(now)
        if type(active_jobs) is not int or active_jobs < 0 or type(budget_ok) is not bool: raise ValueError('invalid dispatch inputs')
        if not self.schedule.enabled or self.paused or self.stopped or not budget_ok or active_jobs >= self.schedule.max_concurrent:
            return None
        if self.last_dispatch is not None and now - self.last_dispatch < self.schedule.cooldown: return None
        due = self.schedule.latest_due(now)
        if due is None: return None
        key = self.schedule.occurrence(due)
        if key in self.dispatched: return None
        self.dispatched.add(key); self.last_dispatch = now
        return {'occurrence_id': key, 'scheduled_at': due, 'schedule_id': self.schedule.schedule_id, 'version': self.schedule.version}
