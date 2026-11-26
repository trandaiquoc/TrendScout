-- Durable coordination state may reference source payload blobs. The index is
-- maintained in the same transaction as each coordination state replacement.
CREATE TABLE source_payload_references (
    scope TEXT NOT NULL,
    reference_path TEXT NOT NULL,
    digest CHAR(64) NOT NULL REFERENCES source_payload_blobs(digest) ON DELETE RESTRICT,
    retain_until TIMESTAMPTZ,
    PRIMARY KEY (scope, reference_path)
);
CREATE INDEX source_payload_references_digest ON source_payload_references (digest);
CREATE INDEX source_payload_references_retention ON source_payload_references (retain_until);
