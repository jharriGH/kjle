-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — DNC Architecture (Phase 1)
-- Date: 2026-05-04
-- Run once in Supabase SQL editor (project: dhzpwobfihrprlcxqjbq).
--
-- Creates:
--   dnc_cache         — per-phone cached provider lookups (TTL'd)
--   dnc_audit_log     — every /dnc/check call (cache hits, fresh, errors, halts)
--   dnc_suppressions  — manually suppressed phones (unsubs, complaints, etc.)
--
-- Idempotent. Safe to re-run.
-- ────────────────────────────────────────────────────────────────────────────

-- ── dnc_cache ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dnc_cache (
    phone            TEXT PRIMARY KEY,                -- E.164: +15551234567
    is_dnc           BOOLEAN,
    dnc_reason       TEXT,                             -- raw codes (FED, CA, CPL, etc.)
    tcpa_litigator   BOOLEAN,
    line_type        TEXT,                             -- 'mobile'|'landline'|'voip'|'unknown'
    carrier          TEXT,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at       TIMESTAMPTZ NOT NULL,
    raw_response     JSONB,
    source_provider  TEXT NOT NULL DEFAULT 'searchbug'
);

CREATE INDEX IF NOT EXISTS idx_dnc_cache_expires_at ON dnc_cache(expires_at);

ALTER TABLE dnc_cache DISABLE ROW LEVEL SECURITY;
GRANT ALL ON dnc_cache TO service_role;


-- ── dnc_audit_log ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dnc_audit_log (
    id                  BIGSERIAL PRIMARY KEY,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    phone               TEXT,                          -- normalized E.164 when known
    source              TEXT NOT NULL,                 -- 'telehealth', 'demoboosterz', etc.
    result              TEXT NOT NULL,                 -- 'cache_hit'|'fresh_lookup'|'internal_suppression'|'error'
    is_dnc              BOOLEAN,                       -- nullable (error rows may have no answer)
    cost_usd            NUMERIC(10, 5) NOT NULL DEFAULT 0,
    requesting_lead_id  UUID,
    error               TEXT,                          -- error code/message when result='error'
    metadata            JSONB
);

CREATE INDEX IF NOT EXISTS idx_dnc_audit_occurred_at        ON dnc_audit_log(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_dnc_audit_source_occurred_at ON dnc_audit_log(source, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_dnc_audit_phone_occurred_at  ON dnc_audit_log(phone, occurred_at DESC);

ALTER TABLE dnc_audit_log DISABLE ROW LEVEL SECURITY;
GRANT ALL ON dnc_audit_log TO service_role;


-- ── dnc_suppressions ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dnc_suppressions (
    phone           TEXT PRIMARY KEY,                 -- E.164
    reason          TEXT NOT NULL,                    -- 'unsubscribed'|'replied_remove'|'spam_complaint'|'manual'|...
    source          TEXT NOT NULL,                    -- which app/system added it
    suppressed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes           TEXT,
    metadata        JSONB
);

CREATE INDEX IF NOT EXISTS idx_dnc_suppressions_suppressed_at ON dnc_suppressions(suppressed_at DESC);

ALTER TABLE dnc_suppressions DISABLE ROW LEVEL SECURITY;
GRANT ALL ON dnc_suppressions TO service_role;


-- ── Sequence grants (required for BIGSERIAL on dnc_audit_log) ────────────────
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role;


-- ── Verification queries (safe to run any time) ─────────────────────────────
-- SELECT count(*) FROM dnc_cache;
-- SELECT count(*) FROM dnc_audit_log;
-- SELECT count(*) FROM dnc_suppressions;
-- SELECT * FROM dnc_audit_log ORDER BY occurred_at DESC LIMIT 10;
