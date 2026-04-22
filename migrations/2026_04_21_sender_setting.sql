-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — Daily Cost Report sender address
-- Date: 2026-04-21
-- Run once in Supabase SQL editor (project: dhzpwobfihrprlcxqjbq).
--
-- Adds admin_settings key 'daily_cost_report_sender' consumed by
-- api/routes/scheduler.py::job_daily_cost_report. Idempotent.
-- ────────────────────────────────────────────────────────────────────────────

INSERT INTO admin_settings (key, value, updated_at) VALUES
    ('daily_cost_report_sender', 'KJLE Reports <kjle@kjreportz.com>', NOW())
ON CONFLICT (key) DO UPDATE
    SET value      = EXCLUDED.value,
        updated_at = NOW();


-- ── Verification ─────────────────────────────────────────────────────────────
-- SELECT key, value, updated_at
--   FROM admin_settings
--  WHERE key = 'daily_cost_report_sender';
