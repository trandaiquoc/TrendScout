"""Explicit fake dispatch; coordinator is responsible for idempotent creation."""

def dispatch_job(scheduler, now, *, active_jobs, budget_ok, coordinator, payload_factory, policy):
    occurrence=scheduler.dispatch(now,active_jobs=active_jobs,budget_ok=budget_ok)
    if occurrence is None: return None
    # Deterministic key survives re-instantiation when the same fake store is reused.
    payload=payload_factory(occurrence)
    return coordinator.create('schedule:'+occurrence['occurrence_id'],payload,policy)
