ALTER TABLE proposals
    ADD COLUMN expires_at TIMESTAMPTZ,
    ADD COLUMN accepted_collection_id TEXT REFERENCES collections(id) ON DELETE SET NULL;

CREATE INDEX proposals_pending_expiry ON proposals(status, expires_at);
