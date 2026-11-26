-- Business schema. Timestamp-only fields are UTC-aware; observation bounds
-- remain text because legacy records contain both dates and instants and the
-- original precision must survive migration. A later ingestion contract can
-- add typed projections without inventing a time for a date-only observation.
CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE collections (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    category_id TEXT UNIQUE, parent_id TEXT REFERENCES collections(id) ON DELETE CASCADE,
    examples JSONB NOT NULL DEFAULT '[]'::jsonb, exclusions JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by TEXT NOT NULL CHECK (created_by IN ('USER', 'AI')),
    auto_match INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'ACTIVE',
    version INTEGER NOT NULL DEFAULT 1, reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE keywords (
    id TEXT PRIMARY KEY, keyword TEXT NOT NULL, normalized TEXT NOT NULL,
    source TEXT NOT NULL, project TEXT NOT NULL DEFAULT '', observed_at TEXT NOT NULL,
    data_mode TEXT NOT NULL, evidence JSONB NOT NULL, context TEXT NOT NULL DEFAULT '',
    eligible INTEGER NOT NULL DEFAULT 1, rejection_reason TEXT
);
CREATE INDEX keywords_normalized ON keywords (normalized);
CREATE INDEX keywords_source_mode ON keywords (source, data_mode);
CREATE TABLE memberships (
    collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    keyword_id TEXT NOT NULL REFERENCES keywords(id) ON DELETE RESTRICT,
    origin TEXT NOT NULL CHECK (origin IN ('USER', 'LOCAL_AI')),
    pinned INTEGER NOT NULL DEFAULT 0, score DOUBLE PRECISION,
    reason TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (collection_id, keyword_id)
);
CREATE INDEX memberships_keyword_id ON memberships (keyword_id);
-- Feedback and reconciliation items historically survive deletion of the
-- referenced entities, so no new foreign keys are imposed on those fields.
CREATE TABLE feedback (
    collection_id TEXT NOT NULL, keyword_id TEXT NOT NULL, action TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (collection_id, keyword_id)
);
CREATE TABLE cache (
    fingerprint TEXT PRIMARY KEY, model_version TEXT NOT NULL,
    collection_version INTEGER NOT NULL, text_hash TEXT NOT NULL,
    vector BYTEA NOT NULL, dimension INTEGER NOT NULL
);
CREATE TABLE proposals (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
    support_ids JSONB NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE suppression (
    fingerprint TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
    label TEXT NOT NULL DEFAULT ''
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE fetch_jobs (
    fetch_id TEXT PRIMARY KEY, status TEXT NOT NULL, available INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0, assigned INTEGER NOT NULL DEFAULT 0,
    unassigned INTEGER NOT NULL DEFAULT 0, created_collections INTEGER NOT NULL DEFAULT 0,
    suggestions INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
    error_code TEXT, updated_at TIMESTAMPTZ NOT NULL, stage TEXT NOT NULL DEFAULT 'QUEUED',
    eligible INTEGER NOT NULL DEFAULT 0, cached_embeddings INTEGER NOT NULL DEFAULT 0,
    inferred_embeddings INTEGER NOT NULL DEFAULT 0, membership_reconciliation_id TEXT
);
CREATE TABLE algorithm_observations (
    keyword_id TEXT NOT NULL, source TEXT NOT NULL, observation_date TEXT NOT NULL,
    country TEXT NOT NULL, access TEXT NOT NULL, project TEXT NOT NULL, metric TEXT NOT NULL,
    unit TEXT NOT NULL, status TEXT NOT NULL, value BIGINT, rank BIGINT,
    snapshot_status TEXT NOT NULL, input_digest TEXT NOT NULL,
    PRIMARY KEY (keyword_id, source, observation_date, metric, unit)
);
CREATE INDEX algorithm_observations_date ON algorithm_observations (observation_date);
CREATE TABLE algorithm_results (
    keyword_id TEXT NOT NULL, algorithm_id TEXT NOT NULL, version TEXT NOT NULL,
    status TEXT NOT NULL, reason_code TEXT, value DOUBLE PRECISION, metric TEXT NOT NULL,
    -- This JSON text is exposed verbatim by the existing keyword API; keeping
    -- it as text preserves that public representation, including formatting.
    unit TEXT NOT NULL, result_json TEXT NOT NULL, input_digest TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (keyword_id, algorithm_id)
);
CREATE INDEX algorithm_results_updated_at ON algorithm_results (updated_at DESC, keyword_id);
CREATE TABLE dataset_snapshots (
    dataset_id TEXT PRIMARY KEY, status TEXT NOT NULL, data_mode TEXT NOT NULL,
    source_manifest JSONB NOT NULL, provider_record_count INTEGER NOT NULL,
    received_record_count INTEGER NOT NULL, retained_keyword_count INTEGER NOT NULL,
    rejected_record_count INTEGER NOT NULL, observation_start TEXT, observation_end TEXT,
    fetched_at TIMESTAMPTZ, quality_flags JSONB NOT NULL, processing_status TEXT NOT NULL,
    eligible_keyword_count INTEGER NOT NULL DEFAULT 0,
    assigned_keyword_count INTEGER NOT NULL DEFAULT 0,
    unassigned_keyword_count INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX dataset_snapshots_mode_created ON dataset_snapshots (data_mode, created_at DESC);
CREATE TABLE keyword_decisions (
    keyword_id TEXT PRIMARY KEY REFERENCES keywords(id) ON DELETE CASCADE,
    decision TEXT NOT NULL CHECK (decision IN ('EXCLUDED', 'RESTORED')),
    reason TEXT NOT NULL DEFAULT '', updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE entity_context (
    keyword_id TEXT PRIMARY KEY REFERENCES keywords(id) ON DELETE CASCADE,
    title TEXT NOT NULL, page_id BIGINT, entity_id TEXT, description TEXT NOT NULL DEFAULT '',
    page_namespace INTEGER, is_disambiguation INTEGER NOT NULL DEFAULT 0,
    is_main_page INTEGER NOT NULL DEFAULT 0, is_main_page_known INTEGER NOT NULL DEFAULT 0,
    redirected_from TEXT NOT NULL DEFAULT '', page_language TEXT NOT NULL DEFAULT '',
    entity_type TEXT NOT NULL, occupations JSONB NOT NULL DEFAULT '[]'::jsonb,
    category_ids JSONB NOT NULL DEFAULT '[]'::jsonb, instance_types JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_url TEXT NOT NULL DEFAULT '', method TEXT NOT NULL, status TEXT NOT NULL,
    context_version TEXT NOT NULL, captured_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE classification_results (
    keyword_id TEXT PRIMARY KEY REFERENCES keywords(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL, entity_id TEXT, entity_description TEXT NOT NULL DEFAULT '',
    topic_category_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    subcategory_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    classification_status TEXT NOT NULL CHECK (classification_status IN ('CLASSIFIED','AMBIGUOUS','UNKNOWN','INELIGIBLE')),
    confidence_level TEXT NOT NULL, commercial_relevance TEXT NOT NULL DEFAULT 'NOT_ASSESSED',
    classification_method TEXT NOT NULL, classification_version TEXT NOT NULL, taxonomy_version TEXT NOT NULL,
    rule_evidence JSONB NOT NULL DEFAULT '[]'::jsonb, source_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    review_status TEXT NOT NULL, classified_at TIMESTAMPTZ NOT NULL,
    classification_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE membership_reconciliation_runs (
    id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE membership_reconciliation_items (
    run_id TEXT NOT NULL, collection_id TEXT NOT NULL, keyword_id TEXT NOT NULL,
    origin TEXT NOT NULL, pinned INTEGER NOT NULL, score DOUBLE PRECISION,
    reason TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (run_id, collection_id, keyword_id)
);
CREATE TABLE research_records (
    id TEXT PRIMARY KEY, keyword_id TEXT REFERENCES keywords(id) ON DELETE SET NULL,
    subject TEXT NOT NULL, hypothesis TEXT NOT NULL, product_category TEXT NOT NULL DEFAULT '',
    niche TEXT NOT NULL DEFAULT '', competitor_notes TEXT NOT NULL DEFAULT '',
    market_evidence TEXT NOT NULL DEFAULT '', ip_notes TEXT NOT NULL DEFAULT '',
    risk_notes TEXT NOT NULL DEFAULT '', findings TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('DRAFT','RESEARCHING','NEEDS_REVIEW','VALIDATED','ARCHIVED')),
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
);

-- Evidence is source-scoped and can be linked to several datasets or keywords.
-- Provider timestamps, observation scope and geography remain distinct.
CREATE TABLE source_evidence (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
    external_id TEXT, content_digest TEXT NOT NULL, query_scope JSONB NOT NULL,
    geography JSONB, observed_at TIMESTAMPTZ, collected_at TIMESTAMPTZ NOT NULL,
    payload JSONB, expires_at TIMESTAMPTZ
);
CREATE INDEX source_evidence_source_collected ON source_evidence (source_id, collected_at DESC);
CREATE INDEX source_evidence_source_digest ON source_evidence (source_id, content_digest);
CREATE TABLE dataset_evidence (
    dataset_id TEXT NOT NULL REFERENCES dataset_snapshots(dataset_id),
    evidence_id TEXT NOT NULL REFERENCES source_evidence(id),
    PRIMARY KEY (dataset_id, evidence_id)
);
CREATE TABLE evidence_keywords (
    evidence_id TEXT NOT NULL REFERENCES source_evidence(id),
    keyword_id TEXT NOT NULL REFERENCES keywords(id),
    PRIMARY KEY (evidence_id, keyword_id)
);
