CREATE TABLE schedules (
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    frequency TEXT NOT NULL CHECK (frequency IN ('DAILY','WEEKLY')),
    timezone TEXT NOT NULL, hour INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
    minute INTEGER NOT NULL CHECK (minute BETWEEN 0 AND 59),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
    next_run_at TIMESTAMPTZ NOT NULL, last_run_at TIMESTAMPTZ, last_status TEXT,
    missed_runs INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX schedules_due ON schedules (next_run_at, id) WHERE enabled=1;
CREATE TABLE schedule_occurrences (
    id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL REFERENCES schedules(id),
    scheduled_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL,
    lease_until TIMESTAMPTZ, fence BIGINT NOT NULL DEFAULT 0 CHECK (fence >= 0),
    started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, error_code TEXT,
    UNIQUE (schedule_id, scheduled_at)
);
CREATE INDEX schedule_occurrences_running ON schedule_occurrences (lease_until, scheduled_at)
    WHERE status='RUNNING';
