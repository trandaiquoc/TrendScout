ALTER TABLE fetch_jobs
    ADD COLUMN ai_jobs JSONB NOT NULL DEFAULT '[]'::jsonb;
