ALTER TABLE dataset_snapshots
    ADD COLUMN source_ingestion_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    ADD COLUMN materialization_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    ADD COLUMN algorithm_processing_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    ADD COLUMN ai_classification_status TEXT NOT NULL DEFAULT 'UNKNOWN';

UPDATE dataset_snapshots SET
    source_ingestion_status = CASE
        WHEN status = 'COMPLETED' THEN 'COMPLETED'
        WHEN status = 'PARTIAL' THEN 'PARTIAL'
        ELSE 'FAILED'
    END,
    materialization_status = CASE
        WHEN processing_status IN ('COMPLETED', 'MODEL_UNAVAILABLE') THEN 'COMPLETED'
        WHEN processing_status = 'PROCESSING' THEN 'PROCESSING'
        ELSE 'FAILED'
    END,
    algorithm_processing_status = CASE
        WHEN processing_status IN ('COMPLETED', 'MODEL_UNAVAILABLE') THEN 'COMPLETED'
        ELSE 'UNKNOWN'
    END,
    ai_classification_status = CASE
        WHEN processing_status = 'COMPLETED' THEN 'COMPLETED'
        WHEN processing_status = 'MODEL_UNAVAILABLE' THEN 'MODEL_UNAVAILABLE'
        ELSE 'UNKNOWN'
    END;
