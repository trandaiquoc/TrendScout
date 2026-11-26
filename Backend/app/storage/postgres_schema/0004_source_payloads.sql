-- Raw JSON payloads for source caches and checkpoints only. Full SQLite
-- database images are never a valid payload in the new runtime.
CREATE TABLE source_payload_blobs (
    digest CHAR(64) PRIMARY KEY,
    raw_data BYTEA NOT NULL,
    raw_bytes INTEGER NOT NULL CHECK (raw_bytes >= 0),
    item_count INTEGER NOT NULL CHECK (item_count >= 0),
    retain_until TIMESTAMPTZ NOT NULL,
    CHECK (octet_length(raw_data) = raw_bytes)
);
CREATE INDEX source_payload_blobs_retention ON source_payload_blobs (retain_until);
