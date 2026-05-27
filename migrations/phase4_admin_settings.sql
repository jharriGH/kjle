-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — Phase 4 admin_settings seed (channel-aware DNC + Local Scraper)
-- Date: 2026-05-24
-- Run AFTER:
--   migrations/dnc_admin_settings.sql  (Phase 1 baseline)
--   migrations/fed_dnc_list.sql
--   migrations/nanpa_carrier_prefixes.sql
--   migrations/local_scraper_ingest.sql
--
-- Seeds Phase 4 operational toggles. Idempotent — ON CONFLICT DO NOTHING
-- preserves any values you've already toggled.
--
-- NOTE: admin_settings is (key, value, updated_at). Documentation for each
--       toggle lives here as comments and in the design doc — there is no
--       description column.
-- ────────────────────────────────────────────────────────────────────────────

-- dnc_defer_to_contact_time:
--   When 'true', DNC checks only fire when a lead is pushed to a phone-bearing
--   channel (SMS / voice), not at ingest. Saves spend on never-contacted leads.
-- dnc_sms_fallback_searchbug:
--   When 'true', SMS channel will pay Searchbug if all free DNC checks
--   (fed_dnc, internal suppressions, NANPA) are inconclusive. Default 'false'
--   to keep spend at zero until Jim flips it.
-- fed_dnc_enabled:
--   Enable federal DNC list pre-filter check in channel-aware DNC pipeline.
-- nanpa_enabled:
--   Enable NANPA carrier prefix DB lookup for line-type detection (SMS channel).
-- local_scraper_ingest_enabled:
--   Enable Local Scraper webhook ingest endpoint.

INSERT INTO admin_settings (key, value, updated_at) VALUES
    ('dnc_defer_to_contact_time',     'true',  NOW()),
    ('dnc_sms_fallback_searchbug',    'false', NOW()),
    ('fed_dnc_enabled',               'true',  NOW()),
    ('nanpa_enabled',                 'true',  NOW()),
    ('local_scraper_ingest_enabled',  'true',  NOW())
ON CONFLICT (key) DO NOTHING;


-- ── Toggle commands (run separately when you want to pause/unpause) ─────────
-- Pause Local Scraper ingest:
--   UPDATE admin_settings SET value='false', updated_at=NOW()
--    WHERE key = 'local_scraper_ingest_enabled';
--
-- Enable paid Searchbug fallback for SMS channel:
--   UPDATE admin_settings SET value='true', updated_at=NOW()
--    WHERE key = 'dnc_sms_fallback_searchbug';


-- ── Verification ─────────────────────────────────────────────────────────────
-- SELECT key, value FROM admin_settings
--  WHERE key IN (
--      'dnc_defer_to_contact_time',
--      'dnc_sms_fallback_searchbug',
--      'fed_dnc_enabled',
--      'nanpa_enabled',
--      'local_scraper_ingest_enabled'
--  )
--  ORDER BY key;
