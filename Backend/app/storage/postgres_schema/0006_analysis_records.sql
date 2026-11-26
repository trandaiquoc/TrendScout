-- Legacy run metadata and source records are stored per document, not as one
-- database-wide JSON blob. Firestore permits a subcollection without a parent
-- document, so run_id is intentionally not a foreign key on analysis_records.
CREATE TABLE analysis_runs (
    run_id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    timestamp_paths JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_kind SMALLINT,
    created_number NUMERIC NOT NULL DEFAULT 0,
    created_seconds BIGINT NOT NULL DEFAULT 0,
    created_nanos INTEGER NOT NULL DEFAULT 0,
    created_text BYTEA NOT NULL DEFAULT '\x'::bytea
);
CREATE INDEX analysis_runs_created_sort ON analysis_runs
    (created_kind DESC, created_number DESC, created_seconds DESC, created_nanos DESC, created_text DESC);
CREATE TABLE analysis_records (
    record_key CHAR(64) PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('snapshots','observations','keyword_results','baseline_buckets')),
    record_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    timestamp_paths JSONB NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX analysis_records_run_kind ON analysis_records (run_id, kind);
