-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — Local Scraper Ingest Log (Phase 4 Layer 1)
-- Date: 2026-05-24
-- Run once in Supabase SQL editor (project: dhzpwobfihrprlcxqjbq).
--
-- Creates:
--   local_scraper_ingest_log — every webhook payload received from the
--                              local-scraper worker (raw + processing meta).
--
-- Idempotent. Safe to re-run.
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS local_scraper_ingest_log (
    id                BIGSERIAL PRIMARY KEY,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    worker_id         TEXT NOT NULL,
    scraper_id        TEXT,
    run_id            TEXT NOT NULL,
    raw_payload       JSONB,
    results_count     INT DEFAULT 0,
    inserted_count    INT DEFAULT 0,
    duplicate_count   INT DEFAULT 0,
    filtered_count    INT DEFAULT 0,
    error             TEXT,
    processing_ms     INT,
    status            TEXT DEFAULT 'received'    -- received | processed | error | replayed
);

CREATE INDEX IF NOT EXISTS idx_lsi_worker_received ON local_scraper_ingest_log(worker_id, received_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lsi_run_id   ON local_scraper_ingest_log(run_id);
CREATE INDEX IF NOT EXISTS idx_lsi_received_at     ON local_scraper_ingest_log(received_at DESC);

ALTER TABLE local_scraper_ingest_log DISABLE ROW LEVEL SECURITY;
GRANT ALL ON local_scraper_ingest_log TO service_role;


-- ── Sequence grants (required for BIGSERIAL) ────────────────────────────────
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role;


-- ── Verification queries (safe to run any time) ─────────────────────────────
-- SELECT count(*) FROM local_scraper_ingest_log;
-- SELECT status, count(*) FROM local_scraper_ingest_log GROUP BY status;
-- SELECT * FROM local_scraper_ingest_log ORDER BY received_at DESC LIMIT 10;
