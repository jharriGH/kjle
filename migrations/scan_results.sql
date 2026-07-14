CREATE TABLE IF NOT EXISTS scan_results (
  id bigserial PRIMARY KEY,
  lead_id uuid, client_id uuid, url text NOT NULL,
  scanned_at timestamptz NOT NULL DEFAULT now(),
  axe_version text NOT NULL,
  scan_status text NOT NULL DEFAULT 'ok',
  error text,
  accessibility_score numeric(5,2),
  critical_count integer NOT NULL DEFAULT 0,
  serious_count integer NOT NULL DEFAULT 0,
  moderate_count integer NOT NULL DEFAULT 0,
  minor_count integer NOT NULL DEFAULT 0,
  violations jsonb NOT NULL DEFAULT '[]'::jsonb,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_scan_results_url_scanned ON scan_results (url, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_scan_results_lead_scanned ON scan_results (lead_id, scanned_at DESC) WHERE lead_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_scan_results_client_scanned ON scan_results (client_id, scanned_at DESC) WHERE client_id IS NOT NULL;
ALTER TABLE scan_results DISABLE ROW LEVEL SECURITY;
GRANT ALL ON scan_results TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role;
ANALYZE scan_results;
