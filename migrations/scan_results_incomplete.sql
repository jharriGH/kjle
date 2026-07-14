ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS incomplete_count integer NOT NULL DEFAULT 0;
ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS incomplete jsonb NOT NULL DEFAULT '[]'::jsonb;
ANALYZE scan_results;
