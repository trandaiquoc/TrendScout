-- Canonical lineage for derived embedding-cache entries. Each cache row is
-- invalidated when any of its supporting provider evidence reaches expiry.
CREATE TABLE cache_evidence (
    fingerprint TEXT NOT NULL REFERENCES cache(fingerprint) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES source_evidence(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (fingerprint, evidence_id)
);
CREATE INDEX cache_evidence_expires_at ON cache_evidence (expires_at);

-- Aggregate counts make each bounded retention batch auditable without
-- retaining provider content or per-record identifiers in the audit ledger.
ALTER TABLE retention_sweep_runs
    ADD COLUMN source_evidence_blocked_count INTEGER NOT NULL DEFAULT 0
        CHECK (source_evidence_blocked_count >= 0),
    ADD COLUMN dependent_cleanup_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN has_more BOOLEAN NOT NULL DEFAULT FALSE;
