-- KJLE — Add partial id-cursor indexes for classify_segments warm/cold labels
-- Date: 2026-09-25
-- Run via psql (CONCURRENTLY not supported in Supabase SQL editor).
--
-- WHY: classify_segments was timing out (57014) on warm/cold UPDATE chunks.
-- The hot label had idx_leads_seg_hot_idcursor. Warm and cold needed the same
-- treatment: partial indexes on (id) WHERE is_active AND <pain filter> allow
-- the cursor-paginated SELECT and range UPDATE to use Index Only Scans rather
-- than sequential scans over the full ~400k cold row set.
--
-- CONCURRENTLY: acquires no write lock — safe to run against live table.
-- IF NOT EXISTS: safe to re-run.

SET statement_timeout = 0;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_leads_seg_warm_idcursor
    ON leads (id)
    WHERE is_active = true AND pain_score >= 15 AND pain_score < 30;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_leads_seg_cold_idcursor
    ON leads (id)
    WHERE is_active = true AND pain_score < 15;
