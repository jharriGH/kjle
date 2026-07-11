-- ────────────────────────────────────────────────────────────────────────────
-- WebSignalz P1 -- New signal columns for /website-audit/batch-free
-- Date: 2026-07-11
-- Run manually in Supabase SQL editor. All ADD COLUMN IF NOT EXISTS -- safe to
-- re-run. Do NOT run automatically; Jim reviews and applies manually.
--
-- New columns written by the free httpx audit path (_parse_signals_full):
--   website_has_privacy_policy -- "privacy policy" text or link detected
--   website_has_terms          -- terms of service / T&C text or link detected
--   website_has_cookie_consent -- known cookie-consent platform signature
--   website_outdated_tech      -- deprecated HTML tags or table-layout heuristic
--   website_missing_lang       -- <html> tag has no lang= attribute (ADA)
--   website_has_skip_link      -- "skip to content/main" accessibility link
--   last_audited_at            -- timestamp of most recent free-path audit
--
-- Pre-existing columns (already in schema, listed for reference -- NOT added here):
--   has_chatbot, mobile_friendly, has_schema_markup, is_parked
--   website_has_ssl, website_has_contact_form, website_has_cta
--   website_has_video, website_has_blog, website_has_testimonials
--   website_has_booking, website_has_sitemap, website_meta_desc
--   website_h1_count, website_noindex, website_word_count
--   website_img_alt_missing, has_phone_on_page, has_address_on_page
-- ────────────────────────────────────────────────────────────────────────────

ALTER TABLE leads ADD COLUMN IF NOT EXISTS website_has_privacy_policy boolean;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS website_has_terms           boolean;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS website_has_cookie_consent  boolean;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS website_outdated_tech       boolean;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS website_missing_lang        boolean;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS website_has_skip_link       boolean;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_audited_at             timestamptz;

-- Partial index on last_audited_at to speed up the IS NULL bulk-fill query
CREATE INDEX IF NOT EXISTS idx_leads_last_audited_at_null
    ON leads(last_audited_at)
    WHERE last_audited_at IS NULL;

-- Verification query (run after applying):
-- SELECT column_name, data_type
--   FROM information_schema.columns
--  WHERE table_name = 'leads'
--    AND column_name IN (
--      'website_has_privacy_policy','website_has_terms','website_has_cookie_consent',
--      'website_outdated_tech','website_missing_lang','website_has_skip_link',
--      'last_audited_at'
--    )
--  ORDER BY column_name;
-- Expected: 7 rows
