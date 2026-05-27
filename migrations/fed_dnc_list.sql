-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — Federal DNC List (Phase 4 Layer 1)
-- Date: 2026-05-24
-- Run once in Supabase SQL editor (project: dhzpwobfihrprlcxqjbq).
--
-- Creates:
--   fed_dnc_list — flat lookup of phones present on the FCC/FTC federal
--                  Do-Not-Call registry. Loaded in bulk from the daily file.
--                  PK is the phone (E.164) so lookups are PK-index hits;
--                  no extra indexes needed for the channel-aware DNC pre-filter.
--
-- Idempotent. Safe to re-run.
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS fed_dnc_list (
    phone           TEXT PRIMARY KEY,                 -- E.164 normalized: +15551234567
    registered_at   TIMESTAMPTZ,                      -- nullable; original FCC registration date if available
    imported_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE fed_dnc_list DISABLE ROW LEVEL SECURITY;
GRANT ALL ON fed_dnc_list TO service_role;


-- ── Verification queries (safe to run any time) ─────────────────────────────
-- SELECT count(*) FROM fed_dnc_list;
-- SELECT date_trunc('day', imported_at) AS day, count(*)
--   FROM fed_dnc_list GROUP BY 1 ORDER BY 1 DESC LIMIT 7;
-- SELECT * FROM fed_dnc_list WHERE phone = '+15551234567';
