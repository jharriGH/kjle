-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — DNC admin_settings seed
-- Date: 2026-05-04
-- Run AFTER migrations/dnc_schema.sql.
--
-- Seeds DNC operational config. Idempotent — ON CONFLICT DO NOTHING preserves
-- any values you've already toggled.
-- ────────────────────────────────────────────────────────────────────────────

INSERT INTO admin_settings (key, value, updated_at) VALUES
    ('dnc_cache_ttl_days',      '14',          NOW()),
    ('dnc_provider',            'searchbug',   NOW()),
    ('dnc_searchbug_co_code',   '12729833',    NOW()),
    ('dnc_daily_cap_usd',       '3.00',        NOW()),
    ('dnc_enabled',             'true',        NOW())
ON CONFLICT (key) DO NOTHING;


-- ── Toggle commands (run separately when you want to pause/unpause) ─────────
-- Pause all DNC checks (apps will fail-closed):
--   UPDATE admin_settings SET value='false', updated_at=NOW()
--    WHERE key = 'dnc_enabled';
--
-- Adjust daily cap:
--   UPDATE admin_settings SET value='5.00', updated_at=NOW()
--    WHERE key = 'dnc_daily_cap_usd';


-- ── Verification ─────────────────────────────────────────────────────────────
-- SELECT key, value FROM admin_settings
--  WHERE key LIKE 'dnc_%'
--  ORDER BY key;
