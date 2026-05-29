-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — Phase 4 Layer 2 Slice 2B
-- Soft TTL with async background refresh for dnc_cache
-- Date: 2026-05-28
--
-- Adds:
--   dnc_cache.hard_expires_at          — point past which a stale row is no
--                                        longer served (forces fresh lookup)
--   dnc_cache.refresh_in_flight        — DB-level CAS flag, set by background
--                                        refresh worker to prevent dogpile
--   dnc_cache.last_refresh_attempt_at  — diagnostic (when the last background
--                                        refresh worker won the CAS race)
--
-- Plus a partial index over (expires_at, hard_expires_at) limited to rows that
-- are not currently being refreshed — supports fast soft-TTL lookups under load.
--
-- Backfills hard_expires_at on existing rows to (expires_at + 7d). Inserts the
-- admin_settings entry that drives the soft-TTL extension window.
--
-- Idempotent. Safe to re-run.
-- ────────────────────────────────────────────────────────────────────────────

ALTER TABLE dnc_cache ADD COLUMN IF NOT EXISTS hard_expires_at timestamptz;
ALTER TABLE dnc_cache ADD COLUMN IF NOT EXISTS refresh_in_flight boolean DEFAULT false;
ALTER TABLE dnc_cache ADD COLUMN IF NOT EXISTS last_refresh_attempt_at timestamptz;

CREATE INDEX IF NOT EXISTS dnc_cache_soft_ttl_idx
  ON dnc_cache (expires_at, hard_expires_at)
  WHERE refresh_in_flight = false;

UPDATE dnc_cache
  SET hard_expires_at = expires_at + interval '7 days'
  WHERE hard_expires_at IS NULL;

INSERT INTO admin_settings (key, value)
VALUES ('dnc_soft_ttl_extension_days', '7')
ON CONFLICT (key) DO NOTHING;

GRANT ALL ON dnc_cache TO service_role;

-- ── Verification queries (safe to run any time) ─────────────────────────────
-- SELECT column_name FROM information_schema.columns
--   WHERE table_name='dnc_cache'
--     AND column_name IN ('hard_expires_at','refresh_in_flight','last_refresh_attempt_at');
-- SELECT value FROM admin_settings WHERE key='dnc_soft_ttl_extension_days';
