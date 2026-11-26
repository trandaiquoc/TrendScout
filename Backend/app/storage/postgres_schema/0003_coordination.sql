-- Coordination is split into independently writable fields, map entries and
-- source checkpoints. A transaction-scoped lock on scope serializes callbacks
-- until domain services use narrower row-level operations directly.
CREATE TABLE coordination_fields (
    scope TEXT NOT NULL, field TEXT NOT NULL, is_mapping BOOLEAN NOT NULL,
    value JSONB,
    CHECK ((is_mapping AND value IS NULL) OR (NOT is_mapping AND value IS NOT NULL)),
    PRIMARY KEY (scope, field)
);
CREATE TABLE coordination_entries (
    scope TEXT NOT NULL, field TEXT NOT NULL, item_key TEXT NOT NULL,
    value JSONB NOT NULL, has_checkpoints BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (scope, field, item_key),
    FOREIGN KEY (scope, field) REFERENCES coordination_fields(scope, field) ON DELETE CASCADE
);
CREATE TABLE coordination_checkpoints (
    scope TEXT NOT NULL, field TEXT NOT NULL DEFAULT 'jobs' CHECK (field = 'jobs'),
    job_id TEXT NOT NULL, source_id TEXT NOT NULL, value JSONB NOT NULL,
    PRIMARY KEY (scope, job_id, source_id),
    FOREIGN KEY (scope, field, job_id)
        REFERENCES coordination_entries(scope, field, item_key) ON DELETE CASCADE
);
