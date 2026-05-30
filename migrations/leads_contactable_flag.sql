-- KJLE — Phase 4 Layer 3 Slice 3B
-- Add lead-quality classifier columns to leads table.
--
-- contactable      → false when any hard-block / government / test-data
--                    pattern matches OR when phone is carrier-pattern garbage.
--                    Default true so existing rows are not silently flipped.
-- filter_reasons   → audit trail of which classifier patterns hit, persisted
--                    as a JSON array of short string reasons (e.g.
--                    ["permanently_closed","government_office"]).
--
-- Non-contactable leads are NOT deleted — they remain in the table with
-- contactable=false so research / analytics retain the data.

ALTER TABLE leads ADD COLUMN IF NOT EXISTS contactable boolean DEFAULT true;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS filter_reasons jsonb DEFAULT '[]'::jsonb;

-- Partial index: only the non-contactable minority gets indexed. Keeps the
-- index small (most rows are contactable=true) while still making
--   SELECT ... WHERE contactable = false
-- fast for research / cleanup queries.
CREATE INDEX IF NOT EXISTS leads_contactable_idx
  ON leads (contactable)
  WHERE contactable = false;

GRANT ALL ON leads TO service_role;
