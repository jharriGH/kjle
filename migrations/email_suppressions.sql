-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — Email suppressions (Phase 3 companion to dnc_suppressions)
-- Date: 2026-05-04
-- Run once in Supabase SQL editor (project: dhzpwobfihrprlcxqjbq).
--
-- Holds email opt-outs from ReachInbox webhooks, generic suppression webhook,
-- spam complaints, hard bounces, etc. Mirrors dnc_suppressions for phone.
-- Idempotent. Safe to re-run.
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS email_suppressions (
    email          TEXT PRIMARY KEY,                  -- lowercase + stripped
    reason         TEXT NOT NULL,                     -- 'unsubscribed'|'bounced_hard'|'complained'|'manual'|...
    source         TEXT NOT NULL,                     -- 'reachinbox'|'qa'|<app name>
    suppressed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes          TEXT,
    metadata       JSONB
);

CREATE INDEX IF NOT EXISTS idx_email_suppressions_suppressed_at
    ON email_suppressions(suppressed_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_suppressions_source
    ON email_suppressions(source, suppressed_at DESC);

ALTER TABLE email_suppressions DISABLE ROW LEVEL SECURITY;
GRANT ALL ON email_suppressions TO service_role;


-- ── Verification queries (safe to run any time) ─────────────────────────────
-- SELECT count(*) FROM email_suppressions;
-- SELECT email, reason, source, suppressed_at FROM email_suppressions
--   ORDER BY suppressed_at DESC LIMIT 10;
