-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — pain_score_version column (formula versioning)
-- Date: 2026-05-06
-- Run once in Supabase SQL editor (project: dhzpwobfihrprlcxqjbq).
--
-- Adds an explicit formula-version marker to the leads table. The column has
-- been populated by compute_pain_score_v1 since v1 (where the function set
-- pain_score_version = 1). Making the DDL explicit so future formula
-- revisions can A/B compare or selectively rollback.
--
-- Version semantics:
--   1 = original v1 formula (degenerate distribution: 95% of leads in 50-59 band)
--   2 = surgical rebalance, deployed 2026-05-06:
--       - null-aware penalties (NULL no longer treated as known-absent)
--       - re-weighted composite (rep 0.40, web 0.20, seo 0.10, social 0.10, biz 0.20)
--   3+ = future revisions
--
-- Backfill of existing v1 leads to v2 happens via scripts/recompute_pain.py
-- after this migration runs and the new compute_pain_score_v1 is deployed.
--
-- Idempotent. Safe to re-run.
-- ────────────────────────────────────────────────────────────────────────────

ALTER TABLE leads
    ADD COLUMN IF NOT EXISTS pain_score_version INTEGER;

CREATE INDEX IF NOT EXISTS idx_leads_pain_score_version ON leads(pain_score_version);


-- ── Verification queries (safe to run any time) ─────────────────────────────
-- Show formula-version distribution after backfill:
--   SELECT pain_score_version, COUNT(*) FROM leads GROUP BY pain_score_version;
--
-- Identify leads still on v1 (i.e., not yet backfilled):
--   SELECT COUNT(*) FROM leads WHERE pain_score_version = 1;
--
-- Spot-check a few v2 leads:
--   SELECT id, business_name, pain_score, pain_score_version, pain_score_computed_at
--     FROM leads WHERE pain_score_version = 2 LIMIT 10;
