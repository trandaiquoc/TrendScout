-- Aggregate retention run summaries contain no source payloads or row IDs.
-- Rows are written in the same transaction as each bounded cleanup batch.
CREATE TABLE retention_sweep_runs (
    id TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL,
    business_timezone TEXT NOT NULL,
    business_date DATE NOT NULL,
    cutoff_at TIMESTAMPTZ NOT NULL,
    evaluated_at TIMESTAMPTZ NOT NULL,
    dry_run BOOLEAN NOT NULL,
    batch_limit INTEGER NOT NULL CHECK (batch_limit BETWEEN 1 AND 500),
    status TEXT NOT NULL CHECK (status IN ('COMPLETED', 'DRY_RUN')),
    payload_candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (payload_candidate_count >= 0),
    payload_deleted_count INTEGER NOT NULL DEFAULT 0 CHECK (payload_deleted_count >= 0),
    payload_blocked_count INTEGER NOT NULL DEFAULT 0 CHECK (payload_blocked_count >= 0),
    payload_has_more BOOLEAN NOT NULL DEFAULT FALSE,
    source_evidence_due_count INTEGER NOT NULL DEFAULT 0 CHECK (source_evidence_due_count >= 0),
    source_evidence_deleted_count INTEGER NOT NULL DEFAULT 0 CHECK (source_evidence_deleted_count >= 0),
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX retention_sweep_runs_finished ON retention_sweep_runs (finished_at DESC, id);
CREATE INDEX source_evidence_expires_at ON source_evidence (expires_at)
    WHERE expires_at IS NOT NULL;
CREATE INDEX source_evidence_collected_at ON source_evidence (collected_at);
