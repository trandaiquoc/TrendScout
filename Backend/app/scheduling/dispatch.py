"""Explicit fake dispatch; coordinator owns durable-in-fake idempotency."""


def dispatch_job(scheduler, now, *, active_jobs, budget_ok, coordinator, payload_factory, policy):
    # Serialize the fake reservation and create call. If create fails (including
    # an ambiguous response), release the in-memory reservation; the next call
    # uses the same occurrence key and coordinator idempotency.
    with scheduler._lock:
        previous=scheduler.last_dispatch
        occurrence=scheduler.dispatch(now,active_jobs=active_jobs,budget_ok=budget_ok)
        if occurrence is None: return None
        try:
            payload=dict(payload_factory(occurrence))
            payload.update(schedule_id=occurrence['schedule_id'],schedule_version=occurrence['version'])
            return coordinator.create('schedule:'+occurrence['occurrence_id'],payload,policy)
        except BaseException:
            scheduler.dispatched.discard(occurrence['occurrence_id'])
            scheduler.last_dispatch=previous
            raise
