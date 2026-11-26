-- Digests and provenance for explicitly staged legacy Firestore documents.
-- Payload content lives in the coordination/source tables, not in this audit.
CREATE TABLE legacy_firestore_imports (
    document_id TEXT PRIMARY KEY,
    content_sha256 CHAR(64) NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('COORDINATION', 'SQLITE_POINTER', 'ANALYSIS')),
    captured_at TIMESTAMPTZ NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
