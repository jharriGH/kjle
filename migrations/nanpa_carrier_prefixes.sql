-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — NANPA carrier prefix DB (Phase 4 Layer 1)
-- Date: 2026-05-24
-- Run once in Supabase SQL editor (project: dhzpwobfihrprlcxqjbq).
--
-- Creates:
--   nanpa_carrier_prefixes — NANPA thousands-block (NPA + NXX + block digit)
--                            keyed lookup for carrier / line-type detection.
--
-- Granularity note:
--   The original spec called for NPA-NXX (6 chars). That was wrong — NANPA
--   pooling assigns numbers at the THOUSANDS-BLOCK level (7 chars:
--   NPA + NXX + one thousands-block digit, 0-9). A single NPA-NXX can be
--   split across multiple carriers via pooling, so 7-char keys are required
--   for accurate line-type detection.
--
--   Example: '2125550' covers +1-212-555-0000 through +1-212-555-0999.
--
-- Idempotent. Safe to re-run.
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS nanpa_carrier_prefixes (
    npa_nxx_x       TEXT PRIMARY KEY,    -- 7 chars, e.g. '2125550' (NPA + NXX + thousands-block digit)
    npa             TEXT NOT NULL,       -- 3 chars
    nxx             TEXT NOT NULL,       -- 3 chars
    block           TEXT NOT NULL,       -- 1 char (the thousands-block digit, 0-9)
    carrier         TEXT,                -- company name from NANPA
    line_type       TEXT,                -- mobile | landline | voip | unknown — DERIVED from OCN/carrier patterns
    ocn             TEXT,                -- Operating Company Number
    state           TEXT,                -- 2 char
    region          TEXT,                -- from NANPA Augmented file
    status          TEXT,                -- AS (Assigned) | RT (Retained) | AV/AP/AF (Available variants)
    last_updated    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_nanpa_npa_nxx    ON nanpa_carrier_prefixes(npa, nxx);
CREATE INDEX IF NOT EXISTS idx_nanpa_carrier    ON nanpa_carrier_prefixes(carrier);
CREATE INDEX IF NOT EXISTS idx_nanpa_line_type  ON nanpa_carrier_prefixes(line_type);
CREATE INDEX IF NOT EXISTS idx_nanpa_state      ON nanpa_carrier_prefixes(state);

ALTER TABLE nanpa_carrier_prefixes DISABLE ROW LEVEL SECURITY;
GRANT ALL ON nanpa_carrier_prefixes TO service_role;


-- ── Verification queries (safe to run any time) ─────────────────────────────
-- SELECT count(*) FROM nanpa_carrier_prefixes;
-- SELECT line_type, count(*) FROM nanpa_carrier_prefixes GROUP BY 1 ORDER BY 2 DESC;
-- SELECT carrier, count(*) FROM nanpa_carrier_prefixes
--   GROUP BY 1 ORDER BY 2 DESC LIMIT 20;
-- SELECT * FROM nanpa_carrier_prefixes WHERE npa_nxx_x = '2125550';
