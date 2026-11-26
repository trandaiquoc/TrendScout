-- Import progress is separate from business data. A source digest identifies
-- the exact immutable SQLite backup, not a database image stored in PostgreSQL.
CREATE TABLE import_sources (
    sha256 CHAR(64) PRIMARY KEY,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE import_progress (
    source_sha256 CHAR(64) NOT NULL REFERENCES import_sources(sha256),
    table_name TEXT NOT NULL,
    rows_processed BIGINT NOT NULL DEFAULT 0 CHECK (rows_processed >= 0),
    completed BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_sha256, table_name)
);
