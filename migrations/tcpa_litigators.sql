-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — Phase 4 Layer 3 Slice 3A
-- TCPA litigator list mirror — pre-cache pre-Searchbug categorical block.
-- Date: 2026-05-29
--
-- Local mirror of the active TCPA litigator-list vendor (default
-- tcpalitigatorlist.com; swappable via admin_settings.tcpa_list_provider).
-- A phone in this table is treated as DNC regardless of consent — "never
-- contact under any circumstance" — and short-circuits both the cache and
-- the Searchbug provider call in /dnc/check.
--
-- PK is the E.164 phone for fast PK lookup from the request path. Other
-- columns are optional because vendor schemas differ; metadata jsonb
-- preserves whatever the vendor exports.
--
-- Idempotent. Safe to re-run.
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tcpa_litigators (
  phone text PRIMARY KEY,
  added_at timestamptz DEFAULT now(),
  source text,
  name text,
  state text,
  case_count int,
  last_refreshed_at timestamptz,
  metadata jsonb DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS tcpa_litigators_added_at_idx
  ON tcpa_litigators (added_at DESC);

GRANT ALL ON tcpa_litigators TO service_role;

INSERT INTO admin_settings (key, value)
VALUES
  ('tcpa_list_provider', 'tcpalitigatorlist'),
  ('tcpa_list_refresh_cadence', 'weekly'),
  ('tcpa_list_last_refresh_at', '')
ON CONFLICT (key) DO NOTHING;

-- ── Verification queries (safe to run any time) ─────────────────────────────
-- SELECT column_name FROM information_schema.columns
--   WHERE table_name='tcpa_litigators';
-- SELECT key, value FROM admin_settings WHERE key LIKE 'tcpa_list_%';
