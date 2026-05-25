-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — leads.dnc_status columns (Phase 4 Layer 1)
-- Date: 2026-05-24
-- Run once in Supabase SQL editor (project: dhzpwobfihrprlcxqjbq).
--
-- Pure additive — no data migration. Adds three columns to leads:
--
--   dnc_status              TEXT  DEFAULT 'unchecked'
--   dnc_last_checked_at     TIMESTAMPTZ
--   dnc_channel_results     JSONB DEFAULT '{}'::jsonb
--
-- dnc_status value enum (channel-aware DNC pipeline):
--   unchecked                — never run through DNC pipeline
--   fed_dnc_flagged          — found on federal DNC list
--   tcpa_litigator_flagged   — TCPA litigator hit (internal list or provider)
--   searchbug_clean          — paid Searchbug check returned clean
--   searchbug_dnc            — paid Searchbug check returned DNC
--   internal_suppression     — present in dnc_suppressions (unsub, complaint, etc.)
--   leadcrap_filtered        — filtered out by LeadCrap/junk-data heuristic
--
-- Idempotent. Safe to re-run.
-- ────────────────────────────────────────────────────────────────────────────

ALTER TABLE leads ADD COLUMN IF NOT EXISTS dnc_status           TEXT        DEFAULT 'unchecked';
ALTER TABLE leads ADD COLUMN IF NOT EXISTS dnc_last_checked_at  TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS dnc_channel_results  JSONB       DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_leads_dnc_status ON leads(dnc_status);


-- ── Verification queries (safe to run any time) ─────────────────────────────
-- SELECT dnc_status, count(*) FROM leads GROUP BY dnc_status ORDER BY 2 DESC;
-- SELECT count(*) FROM leads WHERE dnc_last_checked_at IS NOT NULL;
-- SELECT id, dnc_status, dnc_last_checked_at, dnc_channel_results
--   FROM leads
--  WHERE dnc_status <> 'unchecked'
--  ORDER BY dnc_last_checked_at DESC NULLS LAST
--  LIMIT 10;
