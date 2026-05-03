-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — Stage 3 / Stage 4 nightly job kill-switches
-- Date: 2026-05-02
-- Run once in Supabase SQL editor (project: dhzpwobfihrprlcxqjbq).
--
-- Adds two admin_settings flags consumed by api/routes/scheduler.py
-- (job_enrich_stage3_nightly + job_enrich_stage4_nightly). When 'false',
-- the job exits immediately with status='skipped' before any DB reads,
-- API key checks, or budget guard logic.
--
-- Default 'true' preserves existing behavior on first install.
-- ON CONFLICT DO NOTHING — re-running the migration will NOT clobber
-- existing values (so toggling to 'false' via UPDATE is safe across re-runs).
-- ────────────────────────────────────────────────────────────────────────────

INSERT INTO admin_settings (key, value, updated_at) VALUES
    ('stage3_nightly_enabled', 'true', NOW()),
    ('stage4_nightly_enabled', 'true', NOW())
ON CONFLICT (key) DO NOTHING;


-- ── Toggle commands (run separately when you want to pause/unpause) ─────────
-- Pause both:
--   UPDATE admin_settings SET value='false', updated_at=NOW()
--    WHERE key IN ('stage3_nightly_enabled','stage4_nightly_enabled');
--
-- Resume both:
--   UPDATE admin_settings SET value='true', updated_at=NOW()
--    WHERE key IN ('stage3_nightly_enabled','stage4_nightly_enabled');


-- ── Verification ─────────────────────────────────────────────────────────────
-- SELECT key, value, updated_at FROM admin_settings
--  WHERE key IN ('stage3_nightly_enabled','stage4_nightly_enabled');
