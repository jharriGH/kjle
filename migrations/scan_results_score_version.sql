ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS score_formula_version text;
ANALYZE scan_results;
