-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — Website Quality Audit Signal Columns
-- Date: 2026-06-20
-- Run in Supabase SQL editor (no downtime — ADD COLUMN IF NOT EXISTS is safe).
--
-- Adds four net-new boolean columns written ONLY by the website_audit module.
-- These are AUDIT signals, not enrichment fields:
--
--   has_chatbot       — live-chat / chatbot widget detected in page HTML
--   mobile_friendly   — <meta name="viewport"> or responsive CSS present
--   has_schema_markup — JSON-LD or microdata schema.org markup found
--   is_parked         — fetch failure, parking template, or near-empty page
--
-- fit_schema_ranker is intentionally NOT touched here. It is ingest-owned
-- (scripts/ingest.py, derived from seo_schema_present) and populated by the
-- ingest pipeline only. The "No schema markup" UI filter targets
-- has_schema_markup. fit_schema_ranker is read-only from the audit's POV.
--
-- enrichment_stage, pain_score, fit_*, and firecrawl_* are never written
-- by the audit module and have no migration changes here.
-- ────────────────────────────────────────────────────────────────────────────

ALTER TABLE leads
  ADD COLUMN IF NOT EXISTS has_chatbot       BOOLEAN,
  ADD COLUMN IF NOT EXISTS mobile_friendly   BOOLEAN,
  ADD COLUMN IF NOT EXISTS has_schema_markup BOOLEAN,
  ADD COLUMN IF NOT EXISTS is_parked         BOOLEAN;

-- Partial indexes skip unaudited (NULL) rows — keeps planner fast on the
-- filtered queries that only care about audited leads.
CREATE INDEX IF NOT EXISTS idx_leads_has_chatbot
    ON leads(has_chatbot)       WHERE has_chatbot IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_mobile_friendly
    ON leads(mobile_friendly)   WHERE mobile_friendly IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_has_schema_markup
    ON leads(has_schema_markup) WHERE has_schema_markup IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_is_parked
    ON leads(is_parked)         WHERE is_parked IS NOT NULL;

-- service_role already holds ALL on leads; re-grant is idempotent.
GRANT ALL ON leads TO service_role;


-- ── admin_settings seeds ─────────────────────────────────────────────────────
-- Caps: primary source of truth is admin_settings; _DEFAULT_CAPS in code is
-- the DB-down fallback only. Cap changes here take effect without a redeploy.

-- G: daily cap for website_audit service (distinct from firecrawl Stage 4 cap)
INSERT INTO admin_settings (key, value, updated_at)
VALUES ('daily_audit_cap_usd', '5.00', NOW())
ON CONFLICT (key) DO NOTHING;

-- A: disable nightly paid enrichment crons — deliverable-triggered only.
-- To re-enable via SQL: UPDATE admin_settings SET value='true' WHERE key IN (...)
INSERT INTO admin_settings (key, value, updated_at)
VALUES ('stage3_nightly_enabled', 'false', NOW())
ON CONFLICT (key) DO NOTHING;

INSERT INTO admin_settings (key, value, updated_at)
VALUES ('stage4_nightly_enabled', 'false', NOW())
ON CONFLICT (key) DO NOTHING;


-- ── Verification ─────────────────────────────────────────────────────────────
-- SELECT column_name
--   FROM information_schema.columns
--  WHERE table_name = 'leads'
--    AND column_name IN ('has_chatbot','mobile_friendly','has_schema_markup','is_parked')
--  ORDER BY column_name;
-- → 4 rows
