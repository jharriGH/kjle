-- WebSignalz Phase 3 Slice 7 — Accessibility summary columns on leads
-- DO NOT APPLY automatically — Jim runs this in the Supabase SQL editor.
-- NOTE: Run with SET statement_timeout=0 if the index build is slow on the 1M-row table.

ALTER TABLE leads ADD COLUMN IF NOT EXISTS accessibility_score numeric(5,2);
ALTER TABLE leads ADD COLUMN IF NOT EXISTS accessibility_violations integer;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS accessibility_critical integer;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS accessibility_scanned_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_leads_a11y_score ON leads (accessibility_score) WHERE accessibility_score IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_a11y_violations ON leads (accessibility_violations) WHERE accessibility_violations IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_a11y_scanned ON leads (accessibility_scanned_at) WHERE accessibility_scanned_at IS NOT NULL;

ANALYZE leads;
